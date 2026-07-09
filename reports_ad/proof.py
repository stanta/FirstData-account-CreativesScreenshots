from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

from PIL import Image

from .models import Placement, ProofRequest, ProofResult
from .validation import ValidationError, merge_url_with_utm

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


TEMPLATE_SELECTORS: dict[str, tuple[str, ...]] = {
    "top-banner": ("[data-ad-slot='top']", ".ad-top", ".top-banner", "header", "body"),
    "sidebar-rect": ("[data-ad-slot='sidebar']", ".ad-sidebar", "aside", ".sidebar", "body"),
    "in-content": ("[data-ad-slot='content']", ".ad-content", "article", "main", "body"),
}


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

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=request.headless,
                args=["--disable-dev-shm-usage", "--no-sandbox"],
            )
            try:
                context = await browser.new_context(
                    viewport={"width": request.viewport_width, "height": request.viewport_height},
                    locale=request.locale,
                    timezone_id=request.timezone_id,
                    device_scale_factor=1,
                    ignore_https_errors=True,
                )
                page = await context.new_page()

                await page.goto(
                    request.ad_page_url,
                    wait_until="domcontentloaded",
                    timeout=request.page_load_timeout_ms,
                )
                timings["page_loaded"] = _elapsed_ms(started)
                await _accept_known_cookie_banner(page)

                creative_data_url = _creative_data_url(request.creative.path, request.creative.mime_type)
                await _insert_creative(
                    page=page,
                    placement=request.placement,
                    creative_data_url=creative_data_url,
                    click_url=effective_click_url,
                    banner_type=request.banner_type,
                    timeout_ms=request.page_load_timeout_ms,
                )
                timings["creative_inserted"] = _elapsed_ms(started)

                landing_page, landing_fallback_used = await _click_and_get_landing_page(
                    context=context,
                    ad_page=page,
                    effective_click_url=effective_click_url,
                    click_timeout_ms=request.click_timeout_ms,
                    page_load_timeout_ms=request.page_load_timeout_ms,
                )
                timings["click_done"] = _elapsed_ms(started)

                await page.screenshot(path=str(ad_screenshot), timeout=request.screenshot_timeout_ms)
                await landing_page.screenshot(path=str(landing_screenshot), timeout=request.screenshot_timeout_ms)
                timings["screenshots_captured"] = _elapsed_ms(started)
            finally:
                await browser.close()

        width, height = compose_final_image(
            ad_screenshot=ad_screenshot,
            landing_screenshot=landing_screenshot,
            output_path=output_path,
            final_width=request.viewport_width,
            final_height=request.viewport_height,
        )
        timings["image_composed"] = _elapsed_ms(started)
        return ProofResult(
            output_path=output_path,
            width=width,
            height=height,
            effective_click_url=effective_click_url,
            request_id=request.request_id,
            landing_fallback_used=landing_fallback_used,
            timings_ms=timings,
        )
    except ValidationError:
        raise
    except PlaywrightTimeoutError as exc:
        raise ScreenshotGenerationError("Истекло время загрузки страницы, клика или создания скриншота.") from exc
    except ScreenshotGenerationError:
        raise
    except Exception as exc:
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


async def _insert_creative(
    *,
    page: Any,
    placement: Placement,
    creative_data_url: str,
    click_url: str,
    banner_type: str,
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
        await locator.evaluate(_replace_slot_script(), _creative_payload(creative_data_url, click_url, banner_type))
    else:
        await _insert_into_template_slot(page, placement.value, creative_data_url, click_url, banner_type, timeout_ms)

    marker = page.locator("[data-reports-ad-proof='creative']").first
    try:
        await marker.wait_for(state="visible", timeout=timeout_ms)
    except PlaywrightTimeoutError as exc:
        raise ScreenshotGenerationError("Креатив не был вставлен в страницу.") from exc


async def _insert_into_template_slot(
    page: Any,
    template_id: str,
    creative_data_url: str,
    click_url: str,
    banner_type: str,
    timeout_ms: int,
) -> None:
    selectors = TEMPLATE_SELECTORS.get(template_id, ("body",))
    payload = _creative_payload(creative_data_url, click_url, banner_type)
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
          const slot = document.createElement('div');
          slot.setAttribute('data-reports-ad-proof-template-slot', args.templateId);
          slot.style.cssText = 'width:100%;max-width:100%;min-height:120px;margin:12px auto;display:block;z-index:2147483647;background:#fff;';
          document.body.prepend(slot);
          const link = document.createElement('a');
          link.href = args.clickUrl;
          link.target = '_blank';
          link.rel = 'noopener noreferrer';
          link.setAttribute('data-reports-ad-proof', 'creative');
          link.setAttribute('aria-label', args.bannerType);
          link.style.cssText = 'display:block;width:100%;text-decoration:none;';
          const img = document.createElement('img');
          img.src = args.creativeDataUrl;
          img.alt = args.bannerType;
          img.style.cssText = 'display:block;width:100%;height:auto;max-height:280px;object-fit:contain;margin:0 auto;';
          link.appendChild(img);
          slot.appendChild(link);
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
) -> tuple[Any, bool]:
    marker = ad_page.locator("[data-reports-ad-proof='creative']").first
    try:
        async with context.expect_page(timeout=click_timeout_ms) as popup_info:
            await marker.click(timeout=click_timeout_ms)
        landing_page = await popup_info.value
        await landing_page.wait_for_load_state("domcontentloaded", timeout=page_load_timeout_ms)
        return landing_page, False
    except Exception:
        landing_page = await context.new_page()
        await landing_page.goto(effective_click_url, wait_until="domcontentloaded", timeout=page_load_timeout_ms)
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
      element.style.minHeight = element.style.minHeight || '90px';
      const link = document.createElement('a');
      link.href = args.clickUrl;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.setAttribute('data-reports-ad-proof', 'creative');
      link.setAttribute('aria-label', args.bannerType);
      link.style.cssText = 'display:block;width:100%;height:100%;text-decoration:none;';
      const img = document.createElement('img');
      img.src = args.creativeDataUrl;
      img.alt = args.bannerType;
      img.style.cssText = 'display:block;width:100%;height:auto;max-height:100%;object-fit:contain;margin:0 auto;';
      link.appendChild(img);
      element.appendChild(link);
    }
    """


def _creative_payload(creative_data_url: str, click_url: str, banner_type: str) -> dict[str, str]:
    return {"creativeDataUrl": creative_data_url, "clickUrl": click_url, "bannerType": banner_type}


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)

