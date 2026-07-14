from __future__ import annotations

import base64
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from PIL import Image

from .catalog import choose_placement_for_url, infer_media_type_from_spec, placement_site_urls, selectors_for_template
from .diagnostics import log_network_diagnostics
from .models import Placement, PlacementMediaType, ProofRequest, ProofResult
from .validation import ValidationError, merge_url_with_utm

logger = logging.getLogger(__name__)

AD_PAGE_LOAD_TIMEOUT_MESSAGE = "Истекло время загрузки рекламной страницы. Проверьте доступность площадки или попробуйте другой URL/селектор."
LANDING_CLICK_TIMEOUT_MESSAGE = "Истекло время клика по баннеру или открытия landing page. Проверьте landing URL и доступность страницы перехода."
SCREENSHOT_TIMEOUT_MESSAGE = "Истекло время создания скриншота. Попробуйте позже или уменьшите сложность страницы."
GENERATION_TIMEOUT_MESSAGE = "Истекло время генерации proof-скриншота. Попробуйте позже или упростите страницу/селектор."
AD_PAGE_FALLBACK_FAILED_MESSAGE = "Не удалось загрузить рекламную страницу: все fallback-площадки для выбранного типа размещения не открылись. Попробуйте другой URL рекламной страницы или повторите позже."
_EXCEPTION_MESSAGE_LIMIT = 240
_CHROMIUM_DIAGNOSTIC_LIMIT = 12

try:  # pragma: no cover - exercised only when Playwright is not installed
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright
except Exception as exc:  # pragma: no cover
    PlaywrightTimeoutError = TimeoutError  # type: ignore[assignment]
    async_playwright = None  # type: ignore[assignment]
    _PLAYWRIGHT_IMPORT_ERROR = exc
else:
    _PLAYWRIGHT_IMPORT_ERROR = None


class ScreenshotGenerationError(RuntimeError):
    """Screenshot generation failure with a safe user-facing message."""

    def __init__(self, safe_message: str) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message


class ProofTimeoutError(ScreenshotGenerationError):
    """Phase-specific timeout with a safe user-facing message."""

    def __init__(self, phase: str, timeout_ms: int, safe_message: str) -> None:
        super().__init__(safe_message)
        self.phase = phase
        self.timeout_ms = timeout_ms


class AdPageFallbackError(ScreenshotGenerationError):
    """All requested and predefined ad-page candidates failed to load."""

    def __init__(self, media_type: PlacementMediaType, failures: list[dict[str, str]]) -> None:
        super().__init__(AD_PAGE_FALLBACK_FAILED_MESSAGE)
        self.media_type = media_type
        self.failures = failures


@dataclass(frozen=True, slots=True)
class AdPageCandidate:
    url: str
    media_type: PlacementMediaType
    is_fallback: bool


async def generate_proof(request: ProofRequest) -> ProofResult:
    if async_playwright is None:  # pragma: no cover
        raise ScreenshotGenerationError(
            "Playwright не установлен или недоступен. Установите зависимости и выполните: python -m playwright install chromium."
        ) from _PLAYWRIGHT_IMPORT_ERROR

    started = time.monotonic()
    timings: dict[str, int] = {}
    output_path = Path(request.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    effective_click_url = merge_url_with_utm(
        request.landing_url,
        request.utm_params,
        allow_local=request.allow_local_urls,
    )

    ad_screenshot = output_path.with_suffix(".ad.png")
    landing_screenshot = output_path.with_suffix(".landing.png")
    landing_fallback_used = False
    log_context = _safe_log_context(request)
    _log_phase("proof_generation", "start", started, **log_context, timeout_ms=_overall_timeout_hint_ms(request))

    try:
        async with async_playwright() as playwright:
            browser_phase_started = time.monotonic()
            _log_phase("browser_launch", "start", browser_phase_started, **log_context)
            try:
                browser = await playwright.chromium.launch(
                    headless=request.headless,
                    args=["--disable-dev-shm-usage", "--no-sandbox"],
                )
            except Exception as exc:
                _log_phase("browser_launch", "failure", browser_phase_started, **log_context, exception=exc)
                raise
            _log_phase("browser_launch", "success", browser_phase_started, **log_context)
            try:
                context_phase_started = time.monotonic()
                _log_phase("browser_context", "start", context_phase_started, **log_context)
                context = await browser.new_context(
                    viewport={"width": request.viewport_width, "height": request.viewport_height},
                    locale=request.locale,
                    timezone_id=request.timezone_id,
                    device_scale_factor=1,
                    ignore_https_errors=True,
                )
                page = await context.new_page()
                _log_phase("browser_context", "success", context_phase_started, **log_context)

                phase_started = time.monotonic()
                _log_phase("ad_page_load", "start", phase_started, **log_context, timeout_ms=request.page_load_timeout_ms)
                try:
                    page, selected_candidate = await _load_ad_page_with_site_fallback(
                        context=context,
                        initial_page=page,
                        request=request,
                        log_context=log_context,
                    )
                except PlaywrightTimeoutError as exc:
                    _log_phase("ad_page_load", "failure", phase_started, **log_context, timeout_ms=request.page_load_timeout_ms, exception=exc)
                    if request.timeout_diagnostics:
                        await log_network_diagnostics(
                            [request.ad_page_url, request.landing_url],
                            timeout_ms=request.timeout_diagnostics_timeout_ms,
                            reason="ad_page_load_timeout",
                            request_id=request.request_id,
                        )
                    raise ProofTimeoutError("ad_page_load", request.page_load_timeout_ms, AD_PAGE_LOAD_TIMEOUT_MESSAGE) from exc
                active_placement = choose_placement_for_url(selected_candidate.url, request.spec)
                log_context = {
                    **log_context,
                    "placement": active_placement.label,
                    "resolved_ad_host": _safe_host(selected_candidate.url),
                    "ad_page_fallback_used": selected_candidate.is_fallback,
                    "fallback_media_type": selected_candidate.media_type,
                }
                _log_phase("ad_page_load", "success", phase_started, **log_context, timeout_ms=request.page_load_timeout_ms)
                timings["page_loaded"] = _elapsed_ms(started)

                phase_started = time.monotonic()
                _log_phase("cookie_banner", "start", phase_started, **log_context, timeout_ms=1_000)
                await _accept_known_cookie_banner(page)
                _log_phase("cookie_banner", "success", phase_started, **log_context, timeout_ms=1_000)

                phase_started = time.monotonic()
                _log_phase("creative_data_url", "start", phase_started, **log_context)
                creative_data_url = _creative_data_url(request.creative.path, request.creative.mime_type)
                _log_phase("creative_data_url", "success", phase_started, **log_context)

                phase_started = time.monotonic()
                _log_phase("creative_insert", "start", phase_started, **log_context, timeout_ms=request.page_load_timeout_ms)
                try:
                    await _insert_creative(
                        page=page,
                        placement=active_placement,
                        creative_data_url=creative_data_url,
                        click_url=effective_click_url,
                        banner_type=request.banner_type,
                        width=request.spec.width,
                        height=request.spec.height,
                        timeout_ms=request.page_load_timeout_ms,
                    )
                except Exception as exc:
                    _log_phase("creative_insert", "failure", phase_started, **log_context, timeout_ms=request.page_load_timeout_ms, exception=exc)
                    raise
                _log_phase("creative_insert", "success", phase_started, **log_context, timeout_ms=request.page_load_timeout_ms)
                timings["creative_inserted"] = _elapsed_ms(started)

                phase_started = time.monotonic()
                intermediate_screenshot_path = intermediate_creative_screenshot_path(
                    creative_path=request.creative.path,
                    ad_page_url=selected_candidate.url,
                )
                _log_phase(
                    "creative_insert_screenshot",
                    "start",
                    phase_started,
                    **log_context,
                    timeout_ms=request.screenshot_timeout_ms,
                    artifact_path=str(intermediate_screenshot_path),
                )
                try:
                    intermediate_screenshot_path.parent.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(intermediate_screenshot_path), timeout=request.screenshot_timeout_ms)
                except PlaywrightTimeoutError as exc:
                    _log_phase(
                        "creative_insert_screenshot",
                        "failure",
                        phase_started,
                        **log_context,
                        timeout_ms=request.screenshot_timeout_ms,
                        artifact_path=str(intermediate_screenshot_path),
                        exception=exc,
                    )
                    raise ProofTimeoutError("creative_insert_screenshot", request.screenshot_timeout_ms, "Истекло время сохранения промежуточного скриншота с креативом.") from exc
                except Exception as exc:
                    _log_phase(
                        "creative_insert_screenshot",
                        "failure",
                        phase_started,
                        **log_context,
                        timeout_ms=request.screenshot_timeout_ms,
                        artifact_path=str(intermediate_screenshot_path),
                        exception=exc,
                    )
                    raise ScreenshotGenerationError("Не удалось сохранить промежуточный скриншот с креативом.") from exc
                _log_phase(
                    "creative_insert_screenshot",
                    "success",
                    phase_started,
                    **log_context,
                    timeout_ms=request.screenshot_timeout_ms,
                    artifact_path=str(intermediate_screenshot_path),
                )
                timings["creative_insert_screenshot_saved"] = _elapsed_ms(started)

                phase_started = time.monotonic()
                _log_phase("landing_click", "start", phase_started, **log_context, timeout_ms=request.click_timeout_ms)
                try:
                    landing_page, landing_fallback_used = await _click_and_get_landing_page(
                        context=context,
                        ad_page=page,
                        effective_click_url=effective_click_url,
                        click_timeout_ms=request.click_timeout_ms,
                        page_load_timeout_ms=request.page_load_timeout_ms,
                        log_context=log_context,
                    )
                except PlaywrightTimeoutError as exc:
                    _log_phase("landing_click", "failure", phase_started, **log_context, timeout_ms=request.click_timeout_ms, exception=exc)
                    raise ProofTimeoutError("landing_click", request.click_timeout_ms, LANDING_CLICK_TIMEOUT_MESSAGE) from exc
                _log_phase("landing_click", "success", phase_started, **log_context, timeout_ms=request.click_timeout_ms, landing_fallback_used=landing_fallback_used)
                timings["click_done"] = _elapsed_ms(started)

                phase_started = time.monotonic()
                _log_phase("ad_screenshot", "start", phase_started, **log_context, timeout_ms=request.screenshot_timeout_ms)
                try:
                    await page.screenshot(path=str(ad_screenshot), timeout=request.screenshot_timeout_ms)
                except PlaywrightTimeoutError as exc:
                    _log_phase("ad_screenshot", "failure", phase_started, **log_context, timeout_ms=request.screenshot_timeout_ms, exception=exc)
                    raise ProofTimeoutError("ad_screenshot", request.screenshot_timeout_ms, "Истекло время создания скриншота рекламной страницы.") from exc
                _log_phase("ad_screenshot", "success", phase_started, **log_context, timeout_ms=request.screenshot_timeout_ms)

                phase_started = time.monotonic()
                _log_phase("landing_screenshot", "start", phase_started, **log_context, timeout_ms=request.screenshot_timeout_ms)
                try:
                    await landing_page.screenshot(path=str(landing_screenshot), timeout=request.screenshot_timeout_ms)
                except PlaywrightTimeoutError as exc:
                    _log_phase("landing_screenshot", "failure", phase_started, **log_context, timeout_ms=request.screenshot_timeout_ms, exception=exc)
                    raise ProofTimeoutError("landing_screenshot", request.screenshot_timeout_ms, "Истекло время создания скриншота landing page.") from exc
                _log_phase("landing_screenshot", "success", phase_started, **log_context, timeout_ms=request.screenshot_timeout_ms)
                timings["screenshots_captured"] = _elapsed_ms(started)
            finally:
                await browser.close()

        phase_started = time.monotonic()
        _log_phase("image_compose", "start", phase_started, **log_context)
        try:
            width, height = compose_final_image(
                ad_screenshot=ad_screenshot,
                landing_screenshot=landing_screenshot,
                output_path=output_path,
                final_width=request.viewport_width,
                final_height=request.viewport_height,
            )
        except Exception as exc:
            _log_phase("image_compose", "failure", phase_started, **log_context, exception=exc)
            raise
        _log_phase("image_compose", "success", phase_started, **log_context)
        timings["image_composed"] = _elapsed_ms(started)
        _log_phase("proof_generation", "success", started, **log_context, timeout_ms=_overall_timeout_hint_ms(request))
        return ProofResult(
            output_path=output_path,
            width=width,
            height=height,
            effective_click_url=effective_click_url,
            request_id=request.request_id,
            intermediate_screenshot_path=intermediate_screenshot_path,
            landing_fallback_used=landing_fallback_used,
            ad_page_fallback_used=selected_candidate.is_fallback,
            fallback_media_type=selected_candidate.media_type,
            resolved_ad_page_host=_safe_host(selected_candidate.url),
            timings_ms=timings,
        )
    except ValidationError:
        raise
    except ProofTimeoutError as exc:
        _log_phase("proof_generation", "failure", started, **log_context, timeout_ms=exc.timeout_ms, timeout_phase=exc.phase, exception=exc)
        raise
    except PlaywrightTimeoutError as exc:
        _log_phase("proof_generation", "failure", started, **log_context, timeout_ms=_overall_timeout_hint_ms(request), exception=exc)
        raise ProofTimeoutError("generation", _overall_timeout_hint_ms(request), GENERATION_TIMEOUT_MESSAGE) from exc
    except ScreenshotGenerationError as exc:
        _log_phase("proof_generation", "failure", started, **log_context, exception=exc)
        raise
    except Exception as exc:
        _log_phase("proof_generation", "failure", started, **log_context, exception=exc)
        raise ScreenshotGenerationError("Не удалось сформировать proof-скриншот.") from exc
    finally:
        for temp_file in (ad_screenshot, landing_screenshot):
            try:
                temp_file.unlink(missing_ok=True)
            except OSError:
                pass


def compose_final_image(
    *,
    ad_screenshot: Path,
    landing_screenshot: Path,
    output_path: Path,
    final_width: int,
    final_height: int,
    landing_ratio: float = 0.2,
) -> tuple[int, int]:
    if not (0.05 <= landing_ratio <= 0.5):
        raise ScreenshotGenerationError("Некорректная доля landing page в итоговом изображении.")

    top_height = int(final_height * (1 - landing_ratio))
    landing_height = final_height - top_height

    with Image.open(ad_screenshot) as ad_img_raw, Image.open(landing_screenshot) as landing_img_raw:
        ad_img = _cover_resize(ad_img_raw.convert("RGB"), final_width, final_height).crop((0, 0, final_width, top_height))
        landing_img = _cover_resize(landing_img_raw.convert("RGB"), final_width, landing_height)
        final_img = Image.new("RGB", (final_width, final_height), "white")
        final_img.paste(ad_img, (0, 0))
        final_img.paste(landing_img, (0, top_height))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        final_img.save(output_path, format="PNG", optimize=True)

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise ScreenshotGenerationError("Итоговое изображение не было создано.")
    with Image.open(output_path) as check_img:
        if check_img.size != (final_width, final_height):
            raise ScreenshotGenerationError("Итоговое изображение имеет неправильный размер.")
    return final_width, final_height


def _cover_resize(image: Image.Image, width: int, height: int) -> Image.Image:
    source_width, source_height = image.size
    scale = max(width / source_width, height / source_height)
    resized = image.resize((int(source_width * scale), int(source_height * scale)), Image.Resampling.LANCZOS)
    left = max((resized.width - width) // 2, 0)
    top = max((resized.height - height) // 2, 0)
    return resized.crop((left, top, left + width, top + height))


def _creative_data_url(path: Path, mime_type: str) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def intermediate_creative_screenshot_path(*, creative_path: Path, ad_page_url: str, created_at: datetime | None = None) -> Path:
    created = created_at or datetime.now(timezone.utc)
    timestamp = created.strftime("%Y%m%d-%H%M%S")
    creative_stem = _safe_filename_part(Path(creative_path).stem or "creative")
    host = _safe_filename_part(_safe_host(ad_page_url) or "unknown-host")
    return _project_tmp_dir() / f"{creative_stem}__{host}__{timestamp}.png"


def _project_tmp_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "tmp"


def _safe_filename_part(value: str, *, max_length: int = 80) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    normalized = normalized.strip("._-")
    return (normalized or "artifact")[:max_length]


async def _insert_creative(
    *,
    page: Any,
    placement: Placement,
    creative_data_url: str,
    click_url: str,
    banner_type: str,
    width: int | None,
    height: int | None,
    timeout_ms: int,
) -> None:
    if placement.mode == "selector":
        locator = page.locator(placement.value).first
        try:
            await locator.wait_for(state="visible", timeout=timeout_ms)
        except PlaywrightTimeoutError as exc:
            raise ScreenshotGenerationError("Не найден или не виден CSS-селектор для вставки рекламы.") from exc
        box = await locator.bounding_box()
        if not box or box["width"] < 16 or box["height"] < 16:
            raise ScreenshotGenerationError("Выбранный слот слишком мал или недоступен для вставки рекламы.")
        await locator.evaluate(_replace_slot_script(), _creative_payload(creative_data_url, click_url, banner_type, width, height))
    else:
        await _insert_into_template_slot(page, placement.value, creative_data_url, click_url, banner_type, width, height, timeout_ms)

    marker = page.locator("[data-reports-ad-proof='creative']").first
    try:
        await marker.wait_for(state="visible", timeout=timeout_ms)
    except PlaywrightTimeoutError as exc:
        raise ScreenshotGenerationError("Креатив не был вставлен в страницу.") from exc


async def _load_ad_page_with_fallback(*, page: Any, url: str, timeout_ms: int, log_context: dict[str, Any]) -> None:
    diagnostics = _new_chromium_diagnostics()
    _attach_chromium_diagnostics(page, diagnostics)
    try:
        response = await page.goto(url, wait_until="commit", timeout=timeout_ms)
        _log_phase(
            "ad_page_commit",
            "success",
            time.monotonic(),
            **log_context,
            timeout_ms=timeout_ms,
            response_status=getattr(response, "status", None) if response else None,
            response_host=_safe_host(getattr(response, "url", "")) if response else "",
        )
    except PlaywrightTimeoutError:
        _log_chromium_diagnostics("ad_page_load_chromium", diagnostics, log_context, page=page)
        raise

    dom_ready = False
    body_ready = False
    dom_timeout: BaseException | None = None
    body_timeout: BaseException | None = None
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 10_000))
        dom_ready = True
    except PlaywrightTimeoutError as exc:
        dom_timeout = exc

    try:
        await page.locator("body").first.wait_for(state="attached", timeout=min(timeout_ms, 5_000))
        body_ready = True
    except PlaywrightTimeoutError as exc:
        body_timeout = exc

    if dom_ready and body_ready:
        await page.wait_for_timeout(500)
        _log_chromium_diagnostics("ad_page_load_chromium", diagnostics, log_context, page=page)
        return

    probe = await _probe_document_state(page)
    if _can_continue_after_commit(probe):
        await _ensure_document_container(page, log_context=log_context, timeout_ms=min(timeout_ms, 3_000))
        _log_phase(
            "ad_page_partial_dom_fallback",
            "success",
            time.monotonic(),
            **log_context,
            timeout_ms=timeout_ms,
            domcontentloaded=dom_ready,
            body_attached=body_ready,
            document_ready_state=probe.get("readyState"),
            document_title=probe.get("title"),
            html_length=probe.get("htmlLength"),
            body_exists=probe.get("bodyExists"),
        )
        _log_chromium_diagnostics("ad_page_load_chromium", diagnostics, log_context, page=page, document_probe=probe)
        return

    _log_chromium_diagnostics("ad_page_load_chromium", diagnostics, log_context, page=page, document_probe=probe)
    raise body_timeout or dom_timeout or PlaywrightTimeoutError("Ad page did not expose an inspectable DOM after commit.")


async def _load_ad_page_with_site_fallback(
    *,
    context: Any,
    initial_page: Any,
    request: ProofRequest,
    log_context: dict[str, Any],
) -> tuple[Any, AdPageCandidate]:
    current_page = initial_page

    async def load_candidate(candidate: AdPageCandidate) -> Any:
        nonlocal current_page
        await _load_ad_page_with_fallback(
            page=current_page,
            url=candidate.url,
            timeout_ms=request.page_load_timeout_ms,
            log_context={**log_context, "candidate_host": _safe_host(candidate.url), "fallback_media_type": candidate.media_type},
        )
        return current_page

    async def reset_after_failure() -> None:
        nonlocal current_page
        try:
            await current_page.close()
        except Exception:
            pass
        current_page = await context.new_page()

    candidates = _ad_page_candidates(request)
    page, candidate = await _select_first_loadable_ad_page(
        candidates=candidates,
        load_candidate=load_candidate,
        reset_after_failure=reset_after_failure,
        timeout_ms=request.page_load_timeout_ms,
        log_context=log_context,
    )
    return page, candidate


def _ad_page_candidates(request: ProofRequest) -> list[AdPageCandidate]:
    media_type = infer_media_type_from_spec(request.spec)
    candidates = [AdPageCandidate(url=request.ad_page_url, media_type=media_type, is_fallback=False)]
    seen_hosts = {_safe_host(request.ad_page_url)}
    for fallback_url in placement_site_urls(media_type):
        host = _safe_host(fallback_url)
        if host and host not in seen_hosts:
            candidates.append(AdPageCandidate(url=fallback_url, media_type=media_type, is_fallback=True))
            seen_hosts.add(host)
    return candidates


async def _select_first_loadable_ad_page(
    *,
    candidates: list[AdPageCandidate],
    load_candidate: Callable[[AdPageCandidate], Awaitable[Any]],
    reset_after_failure: Callable[[], Awaitable[None]] | None,
    timeout_ms: int,
    log_context: dict[str, Any],
) -> tuple[Any, AdPageCandidate]:
    failures: list[dict[str, str]] = []
    count = len(candidates)
    media_type = candidates[0].media_type if candidates else "banner"
    for index, candidate in enumerate(candidates, start=1):
        started = time.monotonic()
        candidate_context = {
            **log_context,
            "fallback_media_type": candidate.media_type,
            "candidate_index": index,
            "candidate_count": count,
            "candidate_host": _safe_host(candidate.url),
            "candidate_kind": "fallback" if candidate.is_fallback else "requested",
            "timeout_ms": timeout_ms,
        }
        _log_phase("ad_page_fallback_candidate", "start", started, **candidate_context)
        try:
            page = await load_candidate(candidate)
        except Exception as exc:
            failures.append(
                {
                    "host": _safe_host(candidate.url),
                    "exception_class": type(exc).__name__,
                    "exception_message": _safe_exception_message(exc),
                }
            )
            _log_phase("ad_page_fallback_candidate", "failure", started, **candidate_context, exception=exc)
            if reset_after_failure is not None and index < count:
                await reset_after_failure()
            continue
        _log_phase("ad_page_fallback_candidate", "success", started, **candidate_context)
        return page, candidate
    raise AdPageFallbackError(media_type, failures)


def _new_chromium_diagnostics() -> dict[str, list[dict[str, Any]]]:
    return {"request_failures": [], "responses": [], "console": [], "pageerrors": [], "navigation_requests": []}


def _attach_chromium_diagnostics(page: Any, diagnostics: dict[str, list[dict[str, Any]]]) -> None:
    def append_limited(key: str, value: dict[str, Any], limit: int = _CHROMIUM_DIAGNOSTIC_LIMIT) -> None:
        if len(diagnostics[key]) < limit:
            diagnostics[key].append(value)

    page.on(
        "requestfailed",
        lambda request: append_limited(
            "request_failures",
            {
                "host": _safe_host(getattr(request, "url", "")),
                "resource_type": getattr(request, "resource_type", ""),
                "failure": _truncate_log_value(getattr(request, "failure", "") or ""),
            },
        ),
    )
    page.on(
        "response",
        lambda response: append_limited(
            "responses",
            {
                "host": _safe_host(getattr(response, "url", "")),
                "status": getattr(response, "status", None),
                "resource_type": getattr(getattr(response, "request", None), "resource_type", ""),
            },
        ),
    )
    page.on(
        "console",
        lambda message: append_limited(
            "console",
            {"type": getattr(message, "type", ""), "text": _truncate_log_value(getattr(message, "text", ""))},
            limit=6,
        ),
    )
    page.on("pageerror", lambda exc: append_limited("pageerrors", {"message": _safe_exception_message(exc)}, limit=6))
    page.on(
        "request",
        lambda request: append_limited(
            "navigation_requests",
            {"host": _safe_host(getattr(request, "url", "")), "resource_type": getattr(request, "resource_type", ""), "is_navigation": True},
        )
        if getattr(request, "is_navigation_request", lambda: False)()
        else None,
    )


async def _probe_document_state(page: Any) -> dict[str, Any]:
    try:
        probe = await page.evaluate(
            """
            () => ({
              readyState: document.readyState,
              title: (document.title || '').slice(0, 120),
              htmlExists: !!document.documentElement,
              bodyExists: !!document.body,
              htmlLength: document.documentElement ? document.documentElement.outerHTML.length : 0,
              bodyLength: document.body ? document.body.innerHTML.length : 0,
            })
            """
        )
        return probe if isinstance(probe, dict) else {"probeError": "unexpected_probe_type"}
    except Exception as exc:
        return {"probeError": type(exc).__name__, "probeMessage": _safe_exception_message(exc)}


def _can_continue_after_commit(probe: dict[str, Any]) -> bool:
    return bool(probe.get("htmlExists") and not probe.get("probeError"))


async def _ensure_document_container(page: Any, *, log_context: dict[str, Any], timeout_ms: int) -> None:
    try:
        await page.evaluate(
            """
            () => {
              if (!document.documentElement) return {ok: false, reason: 'missing_document_element'};
              if (!document.body) {
                const body = document.createElement('body');
                document.documentElement.appendChild(body);
                return {ok: true, createdBody: true};
              }
              return {ok: true, createdBody: false};
            }
            """
        )
    except Exception as exc:
        _log_phase("ad_page_partial_dom_container", "failure", time.monotonic(), **log_context, timeout_ms=timeout_ms, exception=exc)
        raise ScreenshotGenerationError("Страница открылась, но DOM недоступен для безопасной вставки креатива.") from exc


def _log_chromium_diagnostics(
    phase: str,
    diagnostics: dict[str, list[dict[str, Any]]],
    log_context: dict[str, Any],
    *,
    page: Any,
    document_probe: dict[str, Any] | None = None,
) -> None:
    fields: dict[str, Any] = {
        "page_url_host": _safe_host(getattr(page, "url", "")),
        "request_failures_count": len(diagnostics["request_failures"]),
        "responses_count": len(diagnostics["responses"]),
        "console_count": len(diagnostics["console"]),
        "pageerrors_count": len(diagnostics["pageerrors"]),
        "navigation_requests": diagnostics["navigation_requests"][:6],
        "sample_responses": diagnostics["responses"][:6],
        "sample_request_failures": diagnostics["request_failures"][:6],
        "sample_console": diagnostics["console"][:3],
        "sample_pageerrors": diagnostics["pageerrors"][:3],
    }
    if document_probe:
        fields.update(
            {
                "document_ready_state": document_probe.get("readyState"),
                "document_title": document_probe.get("title"),
                "document_html_exists": document_probe.get("htmlExists"),
                "document_body_exists": document_probe.get("bodyExists"),
                "document_html_length": document_probe.get("htmlLength"),
                "document_body_length": document_probe.get("bodyLength"),
                "document_probe_error": document_probe.get("probeError"),
                "document_probe_message": document_probe.get("probeMessage"),
            }
        )
    _log_phase(phase, "success", time.monotonic(), **log_context, **fields)


async def _insert_into_template_slot(
    page: Any,
    template_id: str,
    creative_data_url: str,
    click_url: str,
    banner_type: str,
    width: int | None,
    height: int | None,
    timeout_ms: int,
) -> None:
    selectors = selectors_for_template(template_id) or ("body",)
    payload = _creative_payload(creative_data_url, click_url, banner_type, width, height)
    for selector in selectors:
        locator = page.locator(selector).first
        if await locator.count() == 0:
            continue
        try:
            await locator.wait_for(state="visible", timeout=min(timeout_ms, 3_000))
            await locator.evaluate(_replace_slot_script(), payload)
            return
        except Exception:
            continue
    await page.evaluate(
        """
        (args) => {
          const parent = document.body || document.documentElement;
          if (!parent) throw new Error('missing_document_container');
          const slot = document.createElement('div');
          slot.setAttribute('data-reports-ad-proof-template-slot', args.templateId);
          const width = args.width || 300;
          const height = args.height || 250;
          slot.style.cssText = `width:${width}px;max-width:100%;min-height:${height}px;margin:12px auto;display:block;z-index:2147483647;background:#fff;`;
          const link = document.createElement('a');
          link.href = args.clickUrl;
          link.target = '_blank';
          link.rel = 'noopener noreferrer';
          link.setAttribute('data-reports-ad-proof', 'creative');
          link.setAttribute('aria-label', args.bannerType);
          link.style.cssText = `display:block;width:${width}px;height:${height}px;max-width:100%;text-decoration:none;`;
          const img = document.createElement('img');
          img.src = args.creativeDataUrl;
          img.alt = args.bannerType;
          img.style.cssText = `display:block;width:100%;height:100%;object-fit:contain;margin:0 auto;`;
          link.appendChild(img);
          slot.appendChild(link);
          if (parent.firstChild) {
            parent.insertBefore(slot, parent.firstChild);
          } else {
            parent.appendChild(slot);
          }
        }
        """,
        {**payload, "templateId": template_id},
    )


async def _click_and_get_landing_page(
    *,
    context: Any,
    ad_page: Any,
    effective_click_url: str,
    click_timeout_ms: int,
    page_load_timeout_ms: int,
    log_context: dict[str, Any] | None = None,
) -> tuple[Any, bool]:
    marker = ad_page.locator("[data-reports-ad-proof='creative']").first
    safe_context = log_context or {}
    try:
        async with context.expect_page(timeout=click_timeout_ms) as popup_info:
            await marker.click(timeout=click_timeout_ms)
        landing_page = await popup_info.value
        try:
            await landing_page.wait_for_load_state("domcontentloaded", timeout=page_load_timeout_ms)
            await landing_page.locator("body").first.wait_for(state="attached", timeout=min(page_load_timeout_ms, 5_000))
            await landing_page.wait_for_timeout(500)
        except PlaywrightTimeoutError as exc:
            raise ProofTimeoutError("landing_page_load", page_load_timeout_ms, LANDING_CLICK_TIMEOUT_MESSAGE) from exc
        return landing_page, False
    except ProofTimeoutError:
        raise
    except Exception as exc:
        _log_phase("landing_fallback_open", "start", time.monotonic(), **safe_context, timeout_ms=page_load_timeout_ms, fallback_reason=type(exc).__name__)
        landing_page = await context.new_page()
        try:
            await landing_page.goto(effective_click_url, wait_until="domcontentloaded", timeout=page_load_timeout_ms)
            await landing_page.locator("body").first.wait_for(state="attached", timeout=min(page_load_timeout_ms, 5_000))
            await landing_page.wait_for_timeout(500)
        except PlaywrightTimeoutError as fallback_exc:
            raise ProofTimeoutError("landing_page_load", page_load_timeout_ms, LANDING_CLICK_TIMEOUT_MESSAGE) from fallback_exc
        return landing_page, True


async def _accept_known_cookie_banner(page: Any) -> None:
    for text in ("Принять", "Согласен", "Accept", "I agree", "OK"):
        button = page.get_by_text(text, exact=True).first
        try:
            if await button.count() > 0 and await button.is_visible(timeout=500):
                await button.click(timeout=1_000)
                return
        except Exception:
            continue


def _replace_slot_script() -> str:
    return """
    (element, args) => {
      element.innerHTML = '';
      const width = args.width || element.getBoundingClientRect().width || 300;
      const height = args.height || element.getBoundingClientRect().height || 250;
      element.style.width = element.style.width || `${width}px`;
      element.style.maxWidth = element.style.maxWidth || '100%';
      element.style.minHeight = element.style.minHeight || `${height}px`;
      if (!element.isConnected) throw new Error('slot_not_connected');
      const link = document.createElement('a');
      link.href = args.clickUrl;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.setAttribute('data-reports-ad-proof', 'creative');
      link.setAttribute('aria-label', args.bannerType);
      link.style.cssText = `display:block;width:${width}px;height:${height}px;max-width:100%;text-decoration:none;`;
      const img = document.createElement('img');
      img.src = args.creativeDataUrl;
      img.alt = args.bannerType;
      img.style.cssText = 'display:block;width:100%;height:100%;object-fit:contain;margin:0 auto;';
      link.appendChild(img);
      element.appendChild(link);
    }
    """


def _creative_payload(creative_data_url: str, click_url: str, banner_type: str, width: int | None, height: int | None) -> dict[str, str | int | None]:
    return {"creativeDataUrl": creative_data_url, "clickUrl": click_url, "bannerType": banner_type, "width": width, "height": height}


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _overall_timeout_hint_ms(request: ProofRequest) -> int:
    return max(request.page_load_timeout_ms * 2 + request.click_timeout_ms + request.screenshot_timeout_ms * 2, 0)


def _safe_host(url: str) -> str:
    host = urlparse(url).hostname or ""
    return host.lower()[:120]


def _safe_exception_message(exc: BaseException) -> str:
    return str(exc).replace("\n", " ")[:_EXCEPTION_MESSAGE_LIMIT]


def _truncate_log_value(value: Any) -> str:
    return str(value).replace("\n", " ")[:_EXCEPTION_MESSAGE_LIMIT]


def _safe_log_context(request: ProofRequest) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "network": request.network_slug,
        "spec": request.spec.slug,
        "dimensions": request.spec.dimensions_label,
        "viewport": f"{request.viewport_width}x{request.viewport_height}",
        "placement": request.placement.label,
        "ad_host": _safe_host(request.ad_page_url),
        "landing_host": _safe_host(request.landing_url),
    }


def _log_phase(phase: str, status: str, started: float, **fields: Any) -> None:
    exception = fields.pop("exception", None)
    level = logging.WARNING if status == "failure" else logging.INFO
    payload: dict[str, Any] = {
        "event": "proof_phase",
        "phase": phase,
        "status": status,
        "elapsed_ms": _elapsed_ms(started),
        **{key: value for key, value in fields.items() if value is not None},
    }
    if exception is not None:
        payload["exception_class"] = type(exception).__name__
        payload["exception_message"] = _safe_exception_message(exception)
    logger.log(level, "proof_phase %s", " ".join(f"{key}={value!r}" for key, value in payload.items()))
