from pathlib import Path

import pytest

from reports_ad.bot import _safe_exception_message as bot_safe_exception_message
from reports_ad.bot import _safe_host as bot_safe_host
from reports_ad.catalog import get_spec
from reports_ad.diagnostics import NetworkDiagnosticResult, safe_exception_message as diagnostic_safe_exception_message, safe_host as diagnostic_safe_host
from reports_ad.models import CreativeInfo, Placement, ProofRequest
from reports_ad.proof import (
    AD_PAGE_LOAD_TIMEOUT_MESSAGE,
    GENERATION_TIMEOUT_MESSAGE,
    LANDING_CLICK_TIMEOUT_MESSAGE,
    AD_PAGE_FALLBACK_FAILED_MESSAGE,
    AdPageCandidate,
    AdPageFallbackError,
    ProofTimeoutError,
    _ad_page_candidates,
    _can_continue_after_commit,
    _new_chromium_diagnostics,
    _safe_exception_message,
    _safe_host,
    _safe_log_context,
    _select_first_loadable_ad_page,
)


def test_safe_host_logs_hostname_only_without_query_or_utm() -> None:
    url = "https://Example.COM/path?utm_source=telegram&utm_campaign=secret#fragment"

    assert _safe_host(url) == "example.com"
    assert bot_safe_host(url) == "example.com"
    assert diagnostic_safe_host(url) == "example.com"


def test_safe_exception_message_truncates_and_flattens_newlines() -> None:
    exc = RuntimeError("first line\n" + "x" * 400)

    proof_message = _safe_exception_message(exc)
    bot_message = bot_safe_exception_message(exc)

    assert "\n" not in proof_message
    assert "\n" not in bot_message
    assert len(proof_message) == 240
    assert len(bot_message) == 240
    assert "\n" not in diagnostic_safe_exception_message(exc)
    assert len(diagnostic_safe_exception_message(exc)) == 180


def test_safe_log_context_redacts_full_urls_and_paths(tmp_path: Path) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    creative_path = tmp_path / "sensitive-user-name" / "creative.png"
    request = ProofRequest(
        request_id="req-1",
        ad_page_url="https://ads.example/page?utm_source=secret",
        network_slug="yandex_graphic_banners",
        network_display_name="Яндекс",
        spec=spec,
        banner_type=spec.display_name,
        creative=CreativeInfo(path=creative_path, mime_type="image/png", width=300, height=250, size_bytes=123),
        landing_url="https://landing.example/path?utm_campaign=secret",
        utm_params={"utm_campaign": "secret"},
        placement=Placement(mode="template", value="top-banner"),
        output_path=tmp_path / "proof.png",
    )

    context = _safe_log_context(request)

    assert context["ad_host"] == "ads.example"
    assert context["landing_host"] == "landing.example"
    assert "utm" not in " ".join(str(value) for value in context.values())
    assert "sensitive-user-name" not in " ".join(str(value) for value in context.values())


@pytest.mark.parametrize(
    ("phase", "message"),
    [
        ("ad_page_load", AD_PAGE_LOAD_TIMEOUT_MESSAGE),
        ("landing_page_load", LANDING_CLICK_TIMEOUT_MESSAGE),
        ("generation", GENERATION_TIMEOUT_MESSAGE),
    ],
)
def test_proof_timeout_error_keeps_phase_timeout_and_safe_message(phase: str, message: str) -> None:
    error = ProofTimeoutError(phase, 1234, message)

    assert error.phase == phase
    assert error.timeout_ms == 1234
    assert error.safe_message == message


def test_network_diagnostic_result_keeps_only_safe_host_data() -> None:
    result = NetworkDiagnosticResult(
        check="tls_handshake",
        host=diagnostic_safe_host("https://Example.COM/path?utm_campaign=secret"),
        status="failure",
        elapsed_ms=5000,
        peer="203.0.113.10",
        exception_class="TimeoutError",
        exception_message="timed out",
    )

    assert result.host == "example.com"
    assert "utm" not in " ".join(str(value) for value in (result.host, result.peer, result.exception_message))


def test_partial_dom_fallback_allows_inspectable_document_element() -> None:
    probe = {
        "readyState": "loading",
        "title": "",
        "htmlExists": True,
        "bodyExists": False,
        "htmlLength": 77,
        "bodyLength": 0,
    }

    assert _can_continue_after_commit(probe) is True


@pytest.mark.parametrize(
    "probe",
    [
        {"htmlExists": False, "bodyExists": False, "htmlLength": 0},
        {"probeError": "Error", "probeMessage": "Execution context was destroyed"},
    ],
)
def test_partial_dom_fallback_rejects_uninspectable_document(probe: dict[str, object]) -> None:
    assert _can_continue_after_commit(probe) is False


def test_chromium_diagnostics_shape_contains_only_safe_buckets() -> None:
    diagnostics = _new_chromium_diagnostics()

    assert set(diagnostics) == {"request_failures", "responses", "console", "pageerrors", "navigation_requests"}
    assert all(value == [] for value in diagnostics.values())


def test_ad_page_candidates_try_requested_url_then_banner_fallbacks(tmp_path: Path) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    request = ProofRequest(
        request_id="req-1",
        ad_page_url="https://example.com/ad?secret=1",
        network_slug="yandex_graphic_banners",
        network_display_name="Яндекс",
        spec=spec,
        banner_type=spec.display_name,
        creative=CreativeInfo(path=tmp_path / "creative.png", mime_type="image/png", width=300, height=250, size_bytes=123),
        landing_url="https://landing.example/path",
        utm_params={},
        placement=Placement(mode="template", value="top-banner"),
        output_path=tmp_path / "proof.png",
    )

    candidates = _ad_page_candidates(request)

    assert candidates[0] == AdPageCandidate(url="https://example.com/ad?secret=1", media_type="banner", is_fallback=False)
    assert candidates[1] == AdPageCandidate(url="https://dzen.ru/", media_type="banner", is_fallback=True)
    assert all("secret" not in _safe_host(candidate.url) for candidate in candidates)


@pytest.mark.asyncio
async def test_fallback_iterator_chooses_first_successful_candidate_after_failures() -> None:
    candidates = [
        AdPageCandidate(url="https://requested.example/path", media_type="banner", is_fallback=False),
        AdPageCandidate(url="https://bad.example/", media_type="banner", is_fallback=True),
        AdPageCandidate(url="https://ok.example/", media_type="banner", is_fallback=True),
    ]
    attempted: list[str] = []
    resets = 0

    async def load_candidate(candidate: AdPageCandidate) -> str:
        attempted.append(_safe_host(candidate.url))
        if _safe_host(candidate.url) != "ok.example":
            raise TimeoutError("download failed")
        return "page-ok"

    async def reset_after_failure() -> None:
        nonlocal resets
        resets += 1

    page, candidate = await _select_first_loadable_ad_page(
        candidates=candidates,
        load_candidate=load_candidate,
        reset_after_failure=reset_after_failure,
        timeout_ms=100,
        log_context={"request_id": "req-1"},
    )

    assert page == "page-ok"
    assert candidate.url == "https://ok.example/"
    assert attempted == ["requested.example", "bad.example", "ok.example"]
    assert resets == 2


@pytest.mark.asyncio
async def test_fallback_iterator_all_failed_raises_classified_safe_error() -> None:
    candidates = [
        AdPageCandidate(url="https://requested.example/path?utm_secret=1", media_type="banner", is_fallback=False),
        AdPageCandidate(url="https://bad.example/", media_type="banner", is_fallback=True),
    ]

    async def load_candidate(candidate: AdPageCandidate) -> str:
        raise TimeoutError(f"failed host {_safe_host(candidate.url)}")

    with pytest.raises(AdPageFallbackError) as error:
        await _select_first_loadable_ad_page(
            candidates=candidates,
            load_candidate=load_candidate,
            reset_after_failure=None,
            timeout_ms=100,
            log_context={"request_id": "req-1"},
        )

    assert error.value.safe_message == AD_PAGE_FALLBACK_FAILED_MESSAGE
    assert error.value.media_type == "banner"
    assert [failure["host"] for failure in error.value.failures] == ["requested.example", "bad.example"]
    assert "utm_secret" not in str(error.value.failures)
