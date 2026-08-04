from pathlib import Path

import httpx
import pytest
from PIL import Image
from telegram.error import NetworkError, TimedOut

from reports_ad.bot import (
    CREATIVE,
    EDIT_TARGETS,
    REVIEW,
    REVIEW_MENU_ACTIONS,
    REVIEW_MENU_PROMPT,
    SCREENSHOT,
    SPEC_MENU_PROMPT,
    START_MENU_BUTTON,
    START_MENU_PROMPT,
    TARGET_SITE,
    TELEGRAM_KEYBOARD_BUTTON_TEXT_LIMIT,
    TELEGRAM_SEND_MAX_ATTEMPTS,
    _menu_label,
    _menu_value,
    _reply_document_with_retry,
    _reply_photo_with_retry,
    _reply_text_with_retry,
    _review_text,
    _telegram_send_with_retry,
    collect_banner_type,
    collect_creative,
    collect_network,
    collect_target_site,
    review_menu_action,
    review_menu_keyboard,
    review_or_generate,
    network_menu_keyboard,
    spec_menu_keyboard,
    spec_menu_prompt,
    start,
    start_menu_keyboard,
)
from reports_ad.catalog import choose_placement_for_url, get_site_placement, get_spec, infer_media_type_from_spec, list_placement_site_entries, list_placement_sites, list_runtime_specs, placement_site_urls
from reports_ad.config import BotConfig
from reports_ad.mask_catalog import get_banner_size, resolve_mask_asset
from reports_ad.masked_banner import MaskedBannerResult
from reports_ad.models import CreativeInfo, Placement
from reports_ad.validation import (
    ValidationError,
    merge_url_with_utm,
    normalize_placement,
    normalize_public_url,
    normalize_target_site_for_display,
    parse_utm_params,
    validate_creative_file,
    validate_creative_against_spec,
    validate_screenshot_file,
)


def test_normalize_public_url_rejects_localhost_by_default() -> None:
    with pytest.raises(ValidationError):
        normalize_public_url("http://localhost:8000/page")


def test_normalize_public_url_allows_localhost_in_test_mode() -> None:
    assert normalize_public_url("http://localhost:8000/page#fragment", allow_local=True) == "http://localhost:8000/page"


def test_normalize_public_url_rejects_file_scheme() -> None:
    with pytest.raises(ValidationError):
        normalize_public_url("file:///tmp/index.html", allow_local=True)


def test_parse_and_merge_utm_preserves_existing_query() -> None:
    utm = parse_utm_params("utm_source=telegram&utm_medium=cpc&utm_campaign=test")
    merged = merge_url_with_utm("https://example.com/landing?existing=1&utm_source=old", utm)
    assert merged == "https://example.com/landing?existing=1&utm_source=telegram&utm_medium=cpc&utm_campaign=test"


def test_normalize_target_site_for_display_accepts_domain_and_full_url() -> None:
    assert normalize_target_site_for_display("sberbank.com") == ("https://sberbank.com", "sberbank.com")

    normalized_url, display = normalize_target_site_for_display("https://www.sberbank.com/ru/person/avto?utm_source=x")

    assert normalized_url == "https://www.sberbank.com/ru/person/avto?utm_source=x"
    assert display == "sberbank.com"


def test_normalize_target_site_for_display_rejects_unsafe_hosts() -> None:
    with pytest.raises(ValidationError):
        normalize_target_site_for_display("file:///tmp/private.html")
    with pytest.raises(ValidationError):
        normalize_target_site_for_display("localhost:8000")


def test_parse_utm_rejects_duplicates() -> None:
    with pytest.raises(ValidationError):
        parse_utm_params("utm_source=a&utm_source=b")


def test_normalize_placement_selector_and_template() -> None:
    assert normalize_placement("selector: .ad-slot") == Placement(mode="selector", value=".ad-slot")
    assert normalize_placement("template: top-banner") == Placement(mode="template", value="top-banner")


def test_normalize_placement_rejects_unknown_template() -> None:
    with pytest.raises(ValidationError):
        normalize_placement("template: unknown")


def test_validate_creative_file_accepts_png(tmp_path: Path) -> None:
    image_path = tmp_path / "creative.png"
    Image.new("RGB", (320, 100), "red").save(image_path)
    info = validate_creative_file(image_path, declared_mime_type="image/png")
    assert info.mime_type == "image/png"
    assert info.width == 320
    assert info.height == 100


def test_validate_creative_file_rejects_wrong_declared_mime(tmp_path: Path) -> None:
    image_path = tmp_path / "creative.png"
    Image.new("RGB", (320, 100), "red").save(image_path)
    with pytest.raises(ValidationError):
        validate_creative_file(image_path, declared_mime_type="image/jpeg")


def test_validate_screenshot_file_accepts_png_and_jpeg(tmp_path: Path) -> None:
    png_path = tmp_path / "placement.png"
    jpeg_path = tmp_path / "placement.jpeg"
    Image.new("RGB", (640, 480), "white").save(png_path)
    Image.new("RGB", (640, 480), "white").save(jpeg_path)

    assert validate_screenshot_file(png_path, declared_mime_type="image/png").mime_type == "image/png"
    assert validate_screenshot_file(jpeg_path, declared_mime_type="image/jpeg").mime_type == "image/jpeg"


def test_validate_screenshot_file_rejects_oversized_or_unreadable(tmp_path: Path) -> None:
    image_path = tmp_path / "placement.png"
    Image.new("RGB", (20, 20), "white").save(image_path)
    with pytest.raises(ValidationError):
        validate_screenshot_file(image_path, max_bytes=1)

    broken_path = tmp_path / "broken.png"
    broken_path.write_bytes(b"not an image")
    with pytest.raises(ValidationError):
        validate_screenshot_file(broken_path)


def test_catalog_loads_runtime_png_jpeg_specs() -> None:
    specs = list_runtime_specs("yandex_graphic_banners")
    assert specs
    assert all(spec.runtime_upload_supported for spec in specs)
    assert {"image/png", "image/jpeg"}.issuperset(specs[0].runtime_allowed_mime_types)


def test_catalog_loads_spec_position_hint_from_json() -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")

    assert spec is not None
    assert "Medium rectangle display banner" in spec.position_hint
    assert spec.placement_hint == spec.position_hint


def test_placement_sites_json_loads_by_media_type() -> None:
    assert list_placement_sites("video") == [
        "yandex.ru",
        "kniga-online.com",
        "m.dzen.ru",
        "m.knigavuhe.org",
        "amd.online",
        "ya.ru",
        "ok.ru",
        "www.woman.ru",
    ]
    assert list_placement_sites("video", include_disabled=True) == [
        "dzen.ru",
        "yandex.ru",
        "deti-online.com",
        "kniga-online.com",
        "m.dzen.ru",
        "m.knigavuhe.org",
        "amd.online",
        "ya.ru",
        "ok.ru",
        "www.woman.ru",
    ]
    assert list_placement_sites("banner") == [
        "game.yandex.ru",
        "m.video.yandex.ru",
        "m.images.yandex.ru",
        "yandex.ru",
        "drug.vokrug",
        "pogoda.yandex.ru",
        "ok.ru",
        "images.yandex.ru",
        "m.pogoda.yandex.ru",
    ]
    assert list_placement_sites("banner", include_disabled=True) == [
        "dzen.ru",
        "deti-online.com",
        "game.yandex.ru",
        "m.video.yandex.ru",
        "m.images.yandex.ru",
        "yandex.ru",
        "drug.vokrug",
        "pogoda.yandex.ru",
        "ok.ru",
        "images.yandex.ru",
        "m.pogoda.yandex.ru",
    ]
    assert placement_site_urls("banner")[0] == "https://game.yandex.ru/"
    assert "https://dzen.ru/" not in placement_site_urls("banner")
    assert "https://deti-online.com/" not in placement_site_urls("banner")


def test_disabled_placement_site_entries_keep_traceable_metadata() -> None:
    entries = list_placement_site_entries("banner", include_disabled=True)
    disabled = {entry.host: entry for entry in entries if not entry.enabled}

    assert {"dzen.ru", "deti-online.com"}.issubset(disabled)
    assert "30 second" in disabled["dzen.ru"].disabled_reason.lower()
    assert "historical source entry retained" in disabled["deti-online.com"].disabled_reason.lower()


def test_bot_config_uses_extended_overall_timeout_and_shorter_candidate_timeout() -> None:
    config = BotConfig(telegram_token="test")

    assert config.request_timeout_seconds == 300
    assert config.page_load_timeout_ms == 12_000


def test_bot_config_reads_ocr_llm_env_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPORTS_AD_TELEGRAM_TOKEN", "test-token")
    monkeypatch.setenv("OCR_LLM_BASE_URL", "http://gemma.firstdata.io/v1/")
    monkeypatch.setenv("OCR_LLM_MODEL", "cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit")
    monkeypatch.setenv("OCR_LLM_HEADER_AUTHORISATION", "Basic placeholder")
    monkeypatch.setenv("OCR_LLM_TIMEOUT_SECONDS", "20")
    monkeypatch.setenv("OCR_LLM_MAX_RETRIES", "3")
    monkeypatch.setenv("OCR_LLM_CONFIDENCE_THRESHOLD", "0.6")
    monkeypatch.setenv("REPORTS_AD_OCR_IMAGE_MAX_WIDTH", "1024")
    monkeypatch.setenv("REPORTS_AD_OCR_IMAGE_MAX_PIXELS", "900000")
    monkeypatch.setenv("REPORTS_AD_COMPOSITION_VALIDATION_CROP_MARGIN_PERCENT", "0.25")
    monkeypatch.setenv("REPORTS_AD_COMPOSITION_VALIDATION_CROP_MARGIN_MIN_PIXELS", "80")
    monkeypatch.setenv("REPORTS_AD_OCR_LOG_TEXT", "1")
    monkeypatch.setenv("REPORTS_AD_OCR_LOG_TEXT_MAX_CHARS", "0")

    config = BotConfig.from_env()

    assert config.ocr_llm_base_url == "http://gemma.firstdata.io/v1"
    assert config.ocr_llm_model == "cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit"
    assert config.ocr_llm_authorization_header == "Basic placeholder"
    assert config.ocr_llm_timeout_seconds == 20
    assert config.ocr_llm_max_retries == 3
    assert config.ocr_llm_confidence_threshold == 0.6
    assert config.ocr_image_max_width == 1024
    assert config.ocr_image_max_pixels == 900_000
    assert config.composition_validation_crop_margin_percent == 0.25
    assert config.composition_validation_crop_margin_min_pixels == 80
    assert config.ocr_log_text is True
    assert config.ocr_log_text_max_chars == 0


def test_validate_creative_against_selected_spec_accepts_runtime_png(tmp_path: Path) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    image_path = tmp_path / "creative.png"
    Image.new("RGB", (300, 250), "red").save(image_path)
    info = validate_creative_file(image_path, declared_mime_type="image/png", spec=spec)
    validate_creative_against_spec(info, spec)


def test_media_type_inference_defaults_static_png_jpeg_specs_to_banner() -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None

    assert infer_media_type_from_spec(spec) == "banner"


def test_validate_creative_against_selected_spec_allows_non_matching_dimensions_for_stage2_proportional_fit(tmp_path: Path) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    image_path = tmp_path / "creative.png"
    Image.new("RGB", (320, 250), "red").save(image_path)
    info = validate_creative_file(image_path, declared_mime_type="image/png", spec=spec)
    assert info.width == 320
    assert info.height == 250


def test_site_placement_prefers_observed_selector_when_available() -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    assert get_site_placement("https://deti-online.com/story") is not None
    assert choose_placement_for_url("https://deti-online.com/story", spec) == Placement(mode="selector", value=".ads-wrap")


def test_site_placement_falls_back_by_dimension_for_unknown_host() -> None:
    spec = get_spec("yandex-graphic-banners-728x90-image")
    assert spec is not None
    assert choose_placement_for_url("https://example.com", spec) == Placement(mode="template", value="top-banner")


def test_telegram_menu_network_keyboard_contains_catalog_slug() -> None:
    keyboard = network_menu_keyboard()
    labels = [[button.text for button in row] for row in keyboard.keyboard]
    assert labels[0]
    assert all(row for row in labels)
    assert any("[mediasniper]" in label for row in labels for label in row)
    assert any("[hybrid]" in label for row in labels for label in row)
    assert any("[yandex_video]" in label for row in labels for label in row)
    assert any("[mediasniper_video]" in label for row in labels for label in row)
    assert _menu_value("Mediasniper [mediasniper] — 3 размера") == "mediasniper"


def test_telegram_menu_spec_keyboard_is_filtered_by_network() -> None:
    keyboard = spec_menu_keyboard("mediasniper")
    labels = [[button.text for button in row] for row in keyboard.keyboard]
    assert labels
    assert labels[0]
    assert all(row for row in labels)
    assert all(len(label) <= TELEGRAM_KEYBOARD_BUTTON_TEXT_LIMIT for row in labels for label in row)
    assert any("[240x400]" in label for row in labels for label in row)
    assert any("[300x600]" in label for row in labels for label in row)
    assert any("[300x500]" in label for row in labels for label in row)
    assert not any("[1920x1080]" in label for row in labels for label in row)
    assert keyboard.input_field_placeholder == SPEC_MENU_PROMPT


def test_yandex_video_spec_keyboard_contains_only_1920x1080() -> None:
    keyboard = spec_menu_keyboard("yandex_video")
    labels = [button.text for row in keyboard.keyboard for button in row]

    assert labels == ["1920×1080 [1920x1080]"]
    assert resolve_mask_asset("yandex_video", "1920x1080").path.exists()
    with pytest.raises(ValidationError):
        resolve_mask_asset("mediasniper", "1920x1080")


def test_mediasniper_video_spec_keyboard_contains_only_1920x1080() -> None:
    keyboard = spec_menu_keyboard("mediasniper_video")
    labels = [button.text for row in keyboard.keyboard for button in row]

    assert labels == ["1920×1080 [1920x1080]"]
    mask_asset = resolve_mask_asset("mediasniper_video", "1920x1080")
    assert mask_asset.path.exists()
    assert mask_asset.size.width == 1920
    assert mask_asset.size.height == 1080


def test_spec_menu_prompt_uses_requested_telegram_menu_wording() -> None:
    prompt = spec_menu_prompt("mediasniper")
    assert prompt.startswith("Теперь выберите размер баннера кнопкой меню telegram_menu:\n")
    assert "240x400" in prompt


def test_review_text_uses_russian_menu_prompt_without_old_instruction(tmp_path: Path) -> None:
    user_data = _review_user_data(tmp_path)

    text = _review_text(user_data)

    assert text.startswith("Проверьте заявку перед запуском:")
    assert REVIEW_MENU_PROMPT in text
    assert "resize + mask overlay/Pillow" in text
    assert "размер баннера:" in text
    assert "скриншот площадки:" not in text
    assert "Ответьте `yes`" not in text
    assert "edit network" not in text


def test_review_menu_keyboard_contains_russian_labels_and_launch_is_last() -> None:
    keyboard = review_menu_keyboard()
    labels = [button.text for row in keyboard.keyboard for button in row]

    assert labels == list(REVIEW_MENU_ACTIONS)
    assert labels[-1] == "Запустить"
    assert keyboard.input_field_placeholder == REVIEW_MENU_PROMPT


def test_start_menu_keyboard_contains_exact_russian_start_button() -> None:
    keyboard = start_menu_keyboard()
    labels = [button.text for row in keyboard.keyboard for button in row]

    assert labels == [START_MENU_BUTTON]
    assert labels[0] == "Старт"
    assert keyboard.input_field_placeholder == START_MENU_PROMPT


def test_review_menu_action_maps_all_russian_labels_and_legacy_fallbacks() -> None:
    for label, action in REVIEW_MENU_ACTIONS.items():
        assert review_menu_action(label) == action

    assert review_menu_action("yes") == "generate"
    assert review_menu_action("edit network") == "network"
    assert review_menu_action("edit spec") == "spec"
    assert review_menu_action("edit size") == "size"
    assert review_menu_action("edit creative") == "creative"
    assert review_menu_action("edit landing") == "unknown_edit"
    assert review_menu_action("edit utm") == "unknown_edit"
    assert {action for action in REVIEW_MENU_ACTIONS.values() if action != "generate"} == {"network", "size", "creative"}


@pytest.mark.asyncio
async def test_telegram_send_retry_success_on_first_attempt() -> None:
    calls = 0
    sleeps: list[float] = []

    async def send_call() -> str:
        nonlocal calls
        calls += 1
        return "sent"

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    result = await _telegram_send_with_retry(send_call, operation="test.reply_text", sleep=fake_sleep)

    assert result == "sent"
    assert calls == 1
    assert sleeps == []


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [TimedOut("read timed out"), NetworkError("connect failed")])
async def test_telegram_send_retry_then_success_for_telegram_transient_errors(exc: Exception) -> None:
    calls = 0
    sleeps: list[float] = []

    async def send_call() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise exc
        return "sent-after-retry"

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    result = await _telegram_send_with_retry(send_call, operation="test.reply_text", sleep=fake_sleep)

    assert result == "sent-after-retry"
    assert calls == 2
    assert sleeps == [0.25]


@pytest.mark.asyncio
async def test_telegram_send_retry_then_success_for_httpx_connect_error() -> None:
    calls = 0
    sleeps: list[float] = []

    async def send_call() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("All connection attempts failed for https://api.telegram.org/bot123:SECRET/sendMessage")
        return "sent-after-connect-retry"

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    result = await _telegram_send_with_retry(send_call, operation="test.reply_text", sleep=fake_sleep)

    assert result == "sent-after-connect-retry"
    assert calls == 2
    assert sleeps == [0.25]


@pytest.mark.asyncio
async def test_telegram_send_final_failure_after_five_attempts(caplog: pytest.LogCaptureFixture) -> None:
    calls = 0
    sleeps: list[float] = []

    async def send_call() -> None:
        nonlocal calls
        calls += 1
        raise TimedOut("read timeout at https://api.telegram.org/bot123:SECRET/sendMessage")

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    with caplog.at_level("WARNING", logger="reports_ad.bot"):
        with pytest.raises(TimedOut):
            await _telegram_send_with_retry(send_call, operation="test.reply_text", sleep=fake_sleep)

    assert calls == TELEGRAM_SEND_MAX_ATTEMPTS
    assert sleeps == [0.25, 0.5, 1.0, 2.0]
    assert "telegram_send_retry_exhausted" in caplog.text
    assert "attempt=5" in caplog.text
    assert "max_attempts=5" in caplog.text
    assert "<url-redacted>" in caplog.text
    assert "SECRET" not in caplog.text


@pytest.mark.asyncio
async def test_reply_photo_and_document_wrappers_use_retry() -> None:
    message = _FakeMessage("Запустить")
    message.photo_failures_remaining = 1
    message.document_failures_remaining = 1
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    photo_result = await _reply_photo_with_retry(message, operation="test.reply_photo", sleep=fake_sleep, photo=b"png", caption="photo")
    document_result = await _reply_document_with_retry(message, operation="test.reply_document", sleep=fake_sleep, document=b"png", filename="proof.png", caption="doc")

    assert photo_result == {"photo": b"png", "caption": "photo"}
    assert document_result == {"document": b"png", "filename": "proof.png", "caption": "doc", "reply_markup": None}
    assert len(message.photos) == 1
    assert len(message.documents) == 1
    assert sleeps == [0.25, 0.25]


@pytest.mark.asyncio
async def test_reply_text_wrapper_returns_underlying_result() -> None:
    message = _FakeMessage("/start")

    result = await _reply_text_with_retry(message, "hello", operation="test.reply_text")

    assert result == {"text": "hello", "reply_markup": None}
    assert message.replies == [{"text": "hello", "reply_markup": None}]


@pytest.mark.asyncio
async def test_review_launch_menu_label_starts_generation_like_yes(tmp_path: Path) -> None:
    context = _FakeContext(BotConfig(telegram_token="test", temp_root=tmp_path))
    context.user_data.update(_review_user_data(tmp_path))
    captured_requests = []

    async def fake_generator(request):
        captured_requests.append(request)
        request.output_path.write_bytes(b"fake-png")
        return MaskedBannerResult(output_path=request.output_path, width=1, height=1, request_id=request.request_id, mask_asset=resolve_mask_asset(request.network_slug, request.size_slug))

    context.bot_data["banner_generator"] = fake_generator
    context.bot_data["user_locks"] = {}
    message = _FakeMessage("Запустить")

    state = await review_or_generate(_FakeUpdate(message), context)

    assert state == -1
    assert captured_requests
    assert message.replies[0]["reply_markup"].__class__.__name__ == "ReplyKeyboardRemove"
    assert message.photos[-1]["caption"] == "Баннер с маской готов."
    assert message.documents[-1]["caption"] == "PNG-баннер с маской без сжатия."
    document_keyboard_labels = [button.text for row in message.documents[-1]["reply_markup"].keyboard for button in row]
    assert document_keyboard_labels == ["Старт"]
    assert context.user_data == {}


@pytest.mark.asyncio
async def test_start_button_invokes_existing_start_flow_and_resets_state(tmp_path: Path) -> None:
    context = _FakeContext(BotConfig(telegram_token="test", temp_root=tmp_path))
    context.user_data.update(_review_user_data(tmp_path))
    message = _FakeMessage("Старт")

    state = await start(_FakeUpdate(message), context)

    assert state == 0
    assert set(context.user_data) == {"request_id"}
    assert message.replies[-1]["text"].startswith("Я сформирую баннер:")
    labels = [[button.text for button in row] for row in message.replies[-1]["reply_markup"].keyboard]
    assert any("[mediasniper]" in label for row in labels for label in row)


@pytest.mark.asyncio
async def test_review_russian_edit_labels_transition_to_expected_states(tmp_path: Path) -> None:
    expected_states = {
        "Изменить рекламную сеть": 0,
        "Изменить тип баннера": 1,
        "Изменить размер баннера": 1,
        "Изменить креатив": CREATIVE,
    }
    for label, expected_state in expected_states.items():
        context = _FakeContext(BotConfig(telegram_token="test", temp_root=tmp_path))
        context.user_data.update(_review_user_data(tmp_path))
        message = _FakeMessage(label)

        state = await review_or_generate(_FakeUpdate(message), context)

        assert state == expected_state
        assert message.replies[-1]["reply_markup"] is not None

    context = _FakeContext(BotConfig(telegram_token="test", temp_root=tmp_path))
    context.user_data.update(_review_user_data(tmp_path))
    message = _FakeMessage("Изменить тип баннера")

    await review_or_generate(_FakeUpdate(message), context)

    assert "creative" not in context.user_data


@pytest.mark.asyncio
async def test_network_selection_immediately_sends_filtered_spec_menu() -> None:
    message = _FakeMessage("Mediasniper [mediasniper] — 3 размера")
    update = _FakeUpdate(message)
    context = _FakeContext()

    state = await collect_network(update, context)

    reply = message.replies[-1]
    labels = [[button.text for button in row] for row in reply["reply_markup"].keyboard]
    assert state == 1
    assert reply["text"].startswith("Сеть выбрана: Mediasniper.\nТеперь выберите размер баннера кнопкой меню telegram_menu:")
    assert any("[240x400]" in label for row in labels for label in row)


@pytest.mark.asyncio
async def test_yandex_video_network_selection_sends_1920x1080_spec_menu() -> None:
    message = _FakeMessage("Яндекс Видео [yandex_video] — 1 размер")
    context = _FakeContext()

    state = await collect_network(_FakeUpdate(message), context)

    labels = [button.text for row in message.replies[-1]["reply_markup"].keyboard for button in row]
    assert state == 1
    assert context.user_data["network_slug"] == "yandex_video"
    assert labels == ["1920×1080 [1920x1080]"]


@pytest.mark.asyncio
async def test_mediasniper_video_network_selection_sends_1920x1080_spec_menu() -> None:
    message = _FakeMessage("Mediasniper Video [mediasniper_video] — 1 размер")
    context = _FakeContext()

    state = await collect_network(_FakeUpdate(message), context)

    labels = [button.text for row in message.replies[-1]["reply_markup"].keyboard for button in row]
    assert state == 1
    assert context.user_data["network_slug"] == "mediasniper_video"
    assert labels == ["1920×1080 [1920x1080]"]


@pytest.mark.asyncio
async def test_network_menu_button_then_size_menu_button_advances_to_creative_upload_step() -> None:
    context = _FakeContext()
    network_message = _FakeMessage("Mediasniper [mediasniper] — 3 размера")

    network_state = await collect_network(_FakeUpdate(network_message), context)
    spec_keyboard = network_message.replies[-1]["reply_markup"]
    spec_label = next(
        button.text
        for row in spec_keyboard.keyboard
        for button in row
        if "[240x400]" in button.text
    )
    spec_message = _FakeMessage(spec_label)

    spec_state = await collect_banner_type(_FakeUpdate(spec_message), context)

    assert network_state == 1
    assert spec_state == CREATIVE
    assert context.user_data["network_slug"] == "mediasniper"
    assert context.user_data["banner_size_slug"] == "240x400"
    assert "Отправьте PNG/JPEG-креатив" in spec_message.replies[-1]["text"]
    assert "240×400" in spec_message.replies[-1]["text"]
    assert "Введите URL рекламной страницы" not in spec_message.replies[-1]["text"]


@pytest.mark.asyncio
async def test_invalid_creative_stays_in_creative_upload_state_and_asks_reupload(tmp_path: Path) -> None:
    image_path = tmp_path / "wrong-type.gif"
    Image.new("RGB", (320, 250), "red").save(image_path)
    message = _FakeMessage(str(image_path))
    context = _FakeContext(BotConfig(telegram_token="test", allow_local_creative_path=True, temp_root=tmp_path))
    context.user_data.update(_selected_mask_flow_data(tmp_path))

    state = await collect_creative(_FakeUpdate(message), context)

    assert state == CREATIVE
    assert "creative" not in context.user_data
    assert "Креатив не подходит" in message.replies[-1]["text"]
    assert "Отправьте PNG/JPEG-креатив" in message.replies[-1]["text"]


@pytest.mark.asyncio
async def test_valid_creative_generates_masked_banner_and_cleans_state(tmp_path: Path) -> None:
    image_path = tmp_path / "creative.png"
    Image.new("RGB", (300, 250), "red").save(image_path)
    message = _FakeMessage(str(image_path))
    context = _FakeContext(BotConfig(telegram_token="test", allow_local_creative_path=True, temp_root=tmp_path))
    context.user_data.update(_selected_mask_flow_data(tmp_path))

    state = await collect_creative(_FakeUpdate(message), context)

    assert state == -1
    assert context.user_data == {}
    assert message.photos[-1]["caption"] == "Баннер с маской готов."
    assert message.documents[-1]["filename"] == "mediasniper-240x400-masked-banner.png"


@pytest.mark.asyncio
async def test_yandex_video_valid_creative_prompts_for_target_site_before_generation(tmp_path: Path) -> None:
    image_path = tmp_path / "creative.png"
    Image.new("RGB", (640, 360), "red").save(image_path)
    message = _FakeMessage(str(image_path))
    context = _FakeContext(BotConfig(telegram_token="test", allow_local_creative_path=True, temp_root=tmp_path))
    context.user_data.update(_selected_yandex_video_flow_data(tmp_path))

    state = await collect_creative(_FakeUpdate(message), context)

    assert state == TARGET_SITE
    assert context.user_data["creative"].path == image_path
    assert "адрес целевого сайта" in message.replies[-1]["text"]
    assert not message.photos


@pytest.mark.asyncio
async def test_mediasniper_video_valid_creative_prompts_for_target_site_before_generation(tmp_path: Path) -> None:
    image_path = tmp_path / "creative.png"
    Image.new("RGB", (640, 360), "red").save(image_path)
    message = _FakeMessage(str(image_path))
    context = _FakeContext(BotConfig(telegram_token="test", allow_local_creative_path=True, temp_root=tmp_path))
    context.user_data.update(_selected_mediasniper_video_flow_data(tmp_path))

    state = await collect_creative(_FakeUpdate(message), context)

    assert state == TARGET_SITE
    assert context.user_data["creative"].path == image_path
    assert "адрес целевого сайта" in message.replies[-1]["text"]
    assert not message.photos


@pytest.mark.asyncio
async def test_yandex_video_target_site_starts_generation_and_passes_display(tmp_path: Path) -> None:
    context = _FakeContext(BotConfig(telegram_token="test", temp_root=tmp_path))
    context.user_data.update(_selected_yandex_video_flow_data(tmp_path, with_creative=True))
    captured_requests = []

    async def fake_generator(request):
        captured_requests.append(request)
        request.output_path.write_bytes(b"fake-png")
        return MaskedBannerResult(output_path=request.output_path, width=1536, height=1024, request_id=request.request_id, mask_asset=resolve_mask_asset(request.network_slug, request.size_slug))

    context.bot_data["banner_generator"] = fake_generator
    context.bot_data["user_locks"] = {}
    message = _FakeMessage("https://www.sberbank.com/ru/person/avto?utm_source=x")

    state = await collect_target_site(_FakeUpdate(message), context)

    assert state == -1
    assert captured_requests[0].network_slug == "yandex_video"
    assert captured_requests[0].size_slug == "1920x1080"
    assert captured_requests[0].target_site_display == "sberbank.com"
    assert message.documents[-1]["filename"] == "yandex_video-1920x1080-masked-banner.png"


@pytest.mark.asyncio
async def test_mediasniper_video_target_site_starts_generation_and_passes_display(tmp_path: Path) -> None:
    context = _FakeContext(BotConfig(telegram_token="test", temp_root=tmp_path))
    context.user_data.update(_selected_mediasniper_video_flow_data(tmp_path, with_creative=True))
    captured_requests = []

    async def fake_generator(request):
        captured_requests.append(request)
        request.output_path.write_bytes(b"fake-png")
        return MaskedBannerResult(output_path=request.output_path, width=1920, height=1080, request_id=request.request_id, mask_asset=resolve_mask_asset(request.network_slug, request.size_slug))

    context.bot_data["banner_generator"] = fake_generator
    context.bot_data["user_locks"] = {}
    message = _FakeMessage("https://www.sberbank.com/ru/person/avto?utm_source=x")

    state = await collect_target_site(_FakeUpdate(message), context)

    assert state == -1
    assert captured_requests[0].network_slug == "mediasniper_video"
    assert captured_requests[0].size_slug == "1920x1080"
    assert captured_requests[0].target_site_display == "sberbank.com"
    assert message.documents[-1]["filename"] == "mediasniper_video-1920x1080-masked-banner.png"


@pytest.mark.asyncio
async def test_yandex_video_invalid_target_site_stays_in_target_site_state(tmp_path: Path) -> None:
    context = _FakeContext(BotConfig(telegram_token="test", temp_root=tmp_path))
    context.user_data.update(_selected_yandex_video_flow_data(tmp_path, with_creative=True))
    message = _FakeMessage("file:///tmp/private.html")

    state = await collect_target_site(_FakeUpdate(message), context)

    assert state == TARGET_SITE
    assert "Адрес целевого сайта не подходит" in message.replies[-1]["text"]


def test_menu_label_roundtrip_preserves_slug_for_validation() -> None:
    label = _menu_label("First Data RTB", "first_data_rtb_banners", "17 specs")
    assert label == "First Data RTB [first_data_rtb_banners] — 17 specs"
    assert _menu_value(label) == "first_data_rtb_banners"


def test_spec_menu_label_roundtrip_preserves_slug_for_validation() -> None:
    label = _menu_label("240×400", "240x400")
    assert _menu_value(label) == "240x400"


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.document = None
        self.photo = []
        self.replies: list[dict] = []
        self.photos: list[dict] = []
        self.documents: list[dict] = []
        self.text_failures_remaining = 0
        self.photo_failures_remaining = 0
        self.document_failures_remaining = 0

    async def reply_text(self, text: str, reply_markup=None) -> dict:
        if self.text_failures_remaining:
            self.text_failures_remaining -= 1
            raise TimedOut("reply_text timed out")
        reply = {"text": text, "reply_markup": reply_markup}
        self.replies.append(reply)
        return reply

    async def reply_photo(self, photo, caption: str | None = None) -> dict:
        if self.photo_failures_remaining:
            self.photo_failures_remaining -= 1
            raise NetworkError("reply_photo network error")
        reply = {"photo": photo, "caption": caption}
        self.photos.append(reply)
        return reply

    async def reply_document(self, document, filename: str | None = None, caption: str | None = None, reply_markup=None) -> dict:
        if self.document_failures_remaining:
            self.document_failures_remaining -= 1
            raise TimedOut("reply_document timed out")
        reply = {"document": document, "filename": filename, "caption": caption, "reply_markup": reply_markup}
        self.documents.append(reply)
        return reply


class _FakeUser:
    id = 123


class _FakeUpdate:
    def __init__(self, message: _FakeMessage) -> None:
        self.effective_message = message
        self.effective_user = _FakeUser()


class _FakeContext:
    def __init__(self, config: BotConfig | None = None) -> None:
        self.user_data: dict = {}
        self.bot_data: dict = {"config": config or BotConfig(telegram_token="test")}


def _review_user_data(tmp_path: Path) -> dict:
    creative_path = tmp_path / "creative.png"
    if not creative_path.exists():
        Image.new("RGB", (240, 400), "red").save(creative_path)
    size = get_banner_size("240x400")
    assert size is not None
    return {
        "request_id": "test-request",
        "network_slug": "mediasniper",
        "network_display_name": "Mediasniper",
        "banner_size_slug": "240x400",
        "banner_size": size,
        "banner_type": size.label,
        "creative": CreativeInfo(path=creative_path, mime_type="image/png", width=240, height=400, size_bytes=creative_path.stat().st_size),
    }


def _selected_mask_flow_data(tmp_path: Path) -> dict:
    size = get_banner_size("240x400")
    assert size is not None
    return {
        "request_id": "test-request",
        "network_slug": "mediasniper",
        "network_display_name": "Mediasniper",
        "banner_size_slug": "240x400",
        "banner_size": size,
        "banner_type": size.label,
    }


def _selected_yandex_video_flow_data(tmp_path: Path, *, with_creative: bool = False) -> dict:
    size = get_banner_size("1920x1080")
    assert size is not None
    data = {
        "request_id": "test-request-yandex-video",
        "network_slug": "yandex_video",
        "network_display_name": "Яндекс Видео",
        "banner_size_slug": "1920x1080",
        "banner_size": size,
        "banner_type": size.label,
    }
    if with_creative:
        creative_path = tmp_path / "creative-yandex-video.png"
        Image.new("RGB", (640, 360), "red").save(creative_path)
        data["creative"] = CreativeInfo(path=creative_path, mime_type="image/png", width=640, height=360, size_bytes=creative_path.stat().st_size)
    return data


def _selected_mediasniper_video_flow_data(tmp_path: Path, *, with_creative: bool = False) -> dict:
    size = resolve_mask_asset("mediasniper_video", "1920x1080").size
    data = {
        "request_id": "test-request-mediasniper-video",
        "network_slug": "mediasniper_video",
        "network_display_name": "Mediasniper Video",
        "banner_size_slug": "1920x1080",
        "banner_size": size,
        "banner_type": size.label,
    }
    if with_creative:
        creative_path = tmp_path / "creative-mediasniper-video.png"
        Image.new("RGB", (640, 360), "red").save(creative_path)
        data["creative"] = CreativeInfo(path=creative_path, mime_type="image/png", width=640, height=360, size_bytes=creative_path.stat().st_size)
    return data
