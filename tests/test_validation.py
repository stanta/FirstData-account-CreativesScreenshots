from pathlib import Path

import pytest
from PIL import Image

from reports_ad.bot import (
    SPEC_MENU_PROMPT,
    TELEGRAM_KEYBOARD_BUTTON_TEXT_LIMIT,
    _menu_label,
    _menu_value,
    collect_banner_type,
    collect_network,
    network_menu_keyboard,
    spec_menu_keyboard,
    spec_menu_prompt,
)
from reports_ad.catalog import choose_placement_for_url, get_site_placement, get_spec, list_runtime_specs
from reports_ad.models import Placement
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


def test_validate_creative_against_selected_spec_accepts_exact_dimensions(tmp_path: Path) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    image_path = tmp_path / "creative.png"
    Image.new("RGB", (300, 250), "red").save(image_path)
    info = validate_creative_file(image_path, declared_mime_type="image/png", spec=spec)
    validate_creative_against_spec(info, spec)


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
async def test_network_menu_button_then_spec_menu_button_advances_to_ad_url_step() -> None:
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
    assert spec_state == 2
    assert context.user_data["network_slug"] == "yandex_graphic_banners"
    assert context.user_data["spec"].slug == "yandex-graphic-banners-300x250-image"
    assert spec_message.replies[-1]["text"].endswith("Введите URL рекламной страницы, где нужно разместить рекламу.")


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
        self.replies: list[dict] = []

    async def reply_text(self, text: str, reply_markup=None) -> None:
        self.replies.append({"text": text, "reply_markup": reply_markup})


class _FakeUpdate:
    def __init__(self, message: _FakeMessage) -> None:
        self.effective_message = message


class _FakeContext:
    def __init__(self) -> None:
        self.user_data: dict = {}
