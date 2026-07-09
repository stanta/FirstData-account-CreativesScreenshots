from pathlib import Path

import pytest
from PIL import Image

from reports_ad.models import Placement
from reports_ad.catalog import choose_placement_for_url, get_site_placement, get_spec, list_runtime_specs
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
