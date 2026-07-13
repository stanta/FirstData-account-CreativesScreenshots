from pathlib import Path

import pytest
from PIL import Image

from reports_ad.bot import (
    AD_PAGE_URL,
    CREATIVE,
    EDIT_TARGETS,
    REVIEW,
    REVIEW_MENU_ACTIONS,
    REVIEW_MENU_PROMPT,
    SPEC_MENU_PROMPT,
    TELEGRAM_KEYBOARD_BUTTON_TEXT_LIMIT,
    _menu_label,
    _menu_value,
    _review_text,
    collect_banner_type,
    collect_creative,
    collect_network,
    review_menu_action,
    review_menu_keyboard,
    review_or_generate,
    network_menu_keyboard,
    spec_menu_keyboard,
    spec_menu_prompt,
)
from reports_ad.catalog import choose_placement_for_url, get_site_placement, get_spec, infer_media_type_from_spec, list_placement_sites, list_runtime_specs, placement_site_urls
from reports_ad.config import BotConfig
from reports_ad.models import CreativeInfo, Placement, ProofResult
from reports_ad.validation import (
    ValidationError,
    merge_url_with_utm,
    normalize_placement,
    normalize_public_url,
    parse_utm_params,
    validate_creative_file,
    validate_creative_against_spec,
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


def test_catalog_loads_runtime_png_jpeg_specs() -> None:
    specs = list_runtime_specs("yandex_graphic_banners")
    assert specs
    assert all(spec.runtime_upload_supported for spec in specs)
    assert {"image/png", "image/jpeg"}.issuperset(specs[0].runtime_allowed_mime_types)


def test_placement_sites_json_loads_by_media_type() -> None:
    assert list_placement_sites("video") == [
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
        "dzen.ru",
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
    assert placement_site_urls("banner")[0] == "https://dzen.ru/"


def test_validate_creative_against_selected_spec_accepts_exact_dimensions(tmp_path: Path) -> None:
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


def test_validate_creative_against_selected_spec_rejects_wrong_dimensions(tmp_path: Path) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    image_path = tmp_path / "creative.png"
    Image.new("RGB", (320, 250), "red").save(image_path)
    with pytest.raises(ValidationError):
        validate_creative_file(image_path, declared_mime_type="image/png", spec=spec)


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
    assert any("[yandex_graphic_banners]" in label for row in labels for label in row)
    assert _menu_value("Яндекс [yandex_graphic_banners] — 12 specs") == "yandex_graphic_banners"


def test_telegram_menu_spec_keyboard_is_filtered_by_network() -> None:
    keyboard = spec_menu_keyboard("yandex_graphic_banners")
    labels = [[button.text for button in row] for row in keyboard.keyboard]
    assert labels
    assert labels[0]
    assert all(row for row in labels)
    assert all(len(label) <= TELEGRAM_KEYBOARD_BUTTON_TEXT_LIMIT for row in labels for label in row)
    assert any("[yandex-graphic-banners-300x250-image]" in label for row in labels for label in row)
    assert all("yandex-graphic-banners" in label for row in labels for label in row)
    assert keyboard.input_field_placeholder == SPEC_MENU_PROMPT


def test_spec_menu_prompt_uses_requested_telegram_menu_wording() -> None:
    prompt = spec_menu_prompt("yandex_graphic_banners")
    assert prompt.startswith("Теперь выберите тип баннера/spec кнопкой меню telegram_menu:\n")
    assert "yandex-graphic-banners-300x250-image" in prompt


def test_review_text_uses_russian_menu_prompt_without_old_instruction(tmp_path: Path) -> None:
    user_data = _review_user_data(tmp_path)

    text = _review_text(user_data)

    assert text.startswith("Проверьте заявку перед запуском:")
    assert REVIEW_MENU_PROMPT in text
    assert "fallback-площадки: banner" in text
    assert "Ответьте `yes`" not in text
    assert "edit network" not in text


def test_review_menu_keyboard_contains_russian_labels_and_launch_is_last() -> None:
    keyboard = review_menu_keyboard()
    labels = [button.text for row in keyboard.keyboard for button in row]

    assert labels == list(REVIEW_MENU_ACTIONS)
    assert labels[-1] == "Запустить"
    assert keyboard.input_field_placeholder == REVIEW_MENU_PROMPT


def test_review_menu_action_maps_all_russian_labels_and_legacy_fallbacks() -> None:
    for label, action in REVIEW_MENU_ACTIONS.items():
        assert review_menu_action(label) == action

    assert review_menu_action("yes") == "generate"
    assert review_menu_action("edit network") == "network"
    assert review_menu_action("edit spec") == "spec"
    assert review_menu_action("edit creative") == "creative"
    assert review_menu_action("edit ad_url") == "ad_url"
    assert review_menu_action("edit landing") == "landing"
    assert review_menu_action("edit utm") == "utm"
    assert {action for action in REVIEW_MENU_ACTIONS.values() if action != "generate"} == set(EDIT_TARGETS) - {"banner", "page"}


@pytest.mark.asyncio
async def test_review_launch_menu_label_starts_generation_like_yes(tmp_path: Path) -> None:
    context = _FakeContext(BotConfig(telegram_token="test", temp_root=tmp_path))
    context.user_data.update(_review_user_data(tmp_path))
    captured_requests = []

    async def fake_generator(request):
        captured_requests.append(request)
        request.output_path.write_bytes(b"fake-png")
        return ProofResult(output_path=request.output_path, width=1, height=1, effective_click_url="https://example.com", request_id=request.request_id)

    context.bot_data["proof_generator"] = fake_generator
    context.bot_data["user_locks"] = {}
    message = _FakeMessage("Запустить")

    state = await review_or_generate(_FakeUpdate(message), context)

    assert state == -1
    assert captured_requests
    assert message.replies[0]["reply_markup"].__class__.__name__ == "ReplyKeyboardRemove"
    assert message.photos[-1]["caption"] == "Proof-скриншот готов."
    assert context.user_data == {}


@pytest.mark.asyncio
async def test_review_russian_edit_labels_transition_to_expected_states(tmp_path: Path) -> None:
    expected_states = {
        "Изменить рекламную сеть": 0,
        "Изменить тип баннера": 1,
        "Изменить креатив": CREATIVE,
        "Изменить URL рекламной страницы": AD_PAGE_URL,
        "Изменить landing URL": 4,
        "Изменить UTM-строку": 5,
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
    message = _FakeMessage("Яндекс [yandex_graphic_banners] — 12 specs")
    update = _FakeUpdate(message)
    context = _FakeContext()

    state = await collect_network(update, context)

    reply = message.replies[-1]
    labels = [[button.text for button in row] for row in reply["reply_markup"].keyboard]
    assert state == 1
    assert reply["text"].startswith("Сеть выбрана: Яндекс.\nТеперь выберите тип баннера/spec кнопкой меню telegram_menu:")
    assert any("[yandex-graphic-banners-300x250-image]" in label for row in labels for label in row)
    assert all("yandex-graphic-banners" in label for row in labels for label in row)


@pytest.mark.asyncio
async def test_network_menu_button_then_spec_menu_button_advances_to_creative_upload_step() -> None:
    context = _FakeContext()
    network_message = _FakeMessage("Яндекс [yandex_graphic_banners] — 12 specs")

    network_state = await collect_network(_FakeUpdate(network_message), context)
    spec_keyboard = network_message.replies[-1]["reply_markup"]
    spec_label = next(
        button.text
        for row in spec_keyboard.keyboard
        for button in row
        if "[yandex-graphic-banners-300x250-image]" in button.text
    )
    spec_message = _FakeMessage(spec_label)

    spec_state = await collect_banner_type(_FakeUpdate(spec_message), context)

    assert network_state == 1
    assert spec_state == CREATIVE
    assert context.user_data["network_slug"] == "yandex_graphic_banners"
    assert context.user_data["spec"].slug == "yandex-graphic-banners-300x250-image"
    assert "Отправьте PNG/JPEG-креатив" in spec_message.replies[-1]["text"]
    assert "Ожидаемые размеры: 300×250" in spec_message.replies[-1]["text"]
    assert "Введите URL рекламной страницы" not in spec_message.replies[-1]["text"]


@pytest.mark.asyncio
async def test_invalid_creative_stays_in_creative_upload_state_and_asks_reupload(tmp_path: Path) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    image_path = tmp_path / "wrong-size.png"
    Image.new("RGB", (320, 250), "red").save(image_path)
    message = _FakeMessage(str(image_path))
    context = _FakeContext(BotConfig(telegram_token="test", allow_local_creative_path=True, temp_root=tmp_path))
    context.user_data["spec"] = spec

    state = await collect_creative(_FakeUpdate(message), context)

    assert state == CREATIVE
    assert "creative" not in context.user_data
    assert "Креатив не подходит" in message.replies[-1]["text"]
    assert "Отправьте PNG/JPEG-креатив" in message.replies[-1]["text"]


@pytest.mark.asyncio
async def test_valid_creative_transitions_to_ad_url_prompt_state(tmp_path: Path) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    image_path = tmp_path / "creative.png"
    Image.new("RGB", (300, 250), "red").save(image_path)
    message = _FakeMessage(str(image_path))
    context = _FakeContext(BotConfig(telegram_token="test", allow_local_creative_path=True, temp_root=tmp_path))
    context.user_data["spec"] = spec

    state = await collect_creative(_FakeUpdate(message), context)

    assert state == AD_PAGE_URL
    assert context.user_data["creative"].width == 300
    assert message.replies[-1]["text"] == "Креатив принят. Введите URL рекламной страницы, где нужно разместить рекламу."


def test_menu_label_roundtrip_preserves_slug_for_validation() -> None:
    label = _menu_label("First Data RTB", "first_data_rtb_banners", "17 specs")
    assert label == "First Data RTB [first_data_rtb_banners] — 17 specs"
    assert _menu_value(label) == "first_data_rtb_banners"


def test_spec_menu_label_roundtrip_preserves_slug_for_validation() -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    label = _menu_label(spec.display_name, spec.slug, spec.dimensions_label)
    assert _menu_value(label) == "yandex-graphic-banners-300x250-image"


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.document = None
        self.photo = []
        self.replies: list[dict] = []
        self.photos: list[dict] = []

    async def reply_text(self, text: str, reply_markup=None) -> None:
        self.replies.append({"text": text, "reply_markup": reply_markup})

    async def reply_photo(self, photo, caption: str | None = None) -> None:
        self.photos.append({"photo": photo, "caption": caption})


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
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    creative_path = tmp_path / "creative.png"
    if not creative_path.exists():
        Image.new("RGB", (300, 250), "red").save(creative_path)
    return {
        "request_id": "test-request",
        "network_slug": "yandex_graphic_banners",
        "network_display_name": "Яндекс",
        "spec": spec,
        "banner_type": spec.display_name,
        "ad_page_url": "https://example.com/ad",
        "placement": Placement(mode="template", value="top-banner"),
        "creative": CreativeInfo(path=creative_path, mime_type="image/png", width=300, height=250, size_bytes=creative_path.stat().st_size),
        "landing_url": "https://example.com/landing",
        "utm_params": {"utm_source": "telegram", "utm_medium": "cpc"},
    }
