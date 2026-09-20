from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageFont

from reports_ad.bot import build_application
from reports_ad.config import BotConfig
from reports_ad.gemma_client import GemmaClientError, HeaderAuthGemmaClient, _banner_prompt, _composition_validation_crop_rect, _composition_validation_prompt, _parse_bbox_result, _parse_composition_validation_result, build_gemma_client_from_config
from reports_ad.mask_catalog import get_banner_size, resolve_mask_asset
from reports_ad import masked_banner as masked_banner_module
from reports_ad.masked_banner import GETINTENT_VIDEO_MASK_SIZE, GETINTENT_VIDEO_MINIMUM_AGE_BBOX, YANDEX_VIDEO_FAVICON_BBOX, YANDEX_VIDEO_TARGET_SITE_BBOX, MaskedBannerRequest, MaskedBannerResult, compose_masked_banner, generate_masked_banner
from reports_ad.models import BBoxDetectionResult, BoundingBox, CompositionValidationResult, CreativeInfo, Placement, ProofRequest, ProofResult, SourceScreenshotInfo
from reports_ad.proof import compose_final_image
from reports_ad.stage2_proof import AD_MARK_LABEL, compose_stage2_image, fallback_landing_bbox, generate_stage2_proof, validate_banner_bbox_semantics, select_valid_banner_bbox_with_feedback, compose_stage2_image_with_validation, _address_bar_only_height, _load_cyrillic_font, _render_ad_mark_label, _select_next_composition_bbox
from reports_ad.catalog import get_spec
from reports_ad.validation import ValidationError


def test_compose_final_image_uses_landing_bottom_band(tmp_path: Path) -> None:
    ad_path = tmp_path / "ad.png"
    landing_path = tmp_path / "landing.png"
    output_path = tmp_path / "result.png"
    Image.new("RGB", (100, 100), "blue").save(ad_path)
    Image.new("RGB", (100, 100), "green").save(landing_path)

    width, height = compose_final_image(
        ad_screenshot=ad_path,
        landing_screenshot=landing_path,
        output_path=output_path,
        final_width=100,
        final_height=100,
        landing_ratio=0.2,
    )

    assert (width, height) == (100, 100)
    with Image.open(output_path) as result:
        assert result.size == (100, 100)
        assert result.getpixel((50, 10)) == (0, 0, 255)
        assert result.getpixel((50, 90)) == (0, 128, 0)


def test_stage2_compose_fits_creative_proportionally_draws_landing_and_ad_mark(tmp_path: Path) -> None:
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    output_path = tmp_path / "stage2.png"
    banner_path = tmp_path / "banner.png"
    landing_path = tmp_path / "landing.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    Image.new("RGBA", (30, 20), (255, 0, 0, 180)).save(creative_path)

    width, height = compose_stage2_image(
        source_screenshot_path=source_path,
        creative_path=creative_path,
        effective_click_url="https://example.com/landing?utm_source=test&utm_medium=cpc",
        banner_bbox=BoundingBox(x=50, y=40, width=120, height=80),
        landing_bbox=fallback_landing_bbox(image_width=400, image_height=300),
        output_path=output_path,
        banner_replaced_path=banner_path,
        landing_area_path=landing_path,
    )

    assert (width, height) == (400, 300)
    assert banner_path.exists()
    assert landing_path.exists()
    with Image.open(output_path) as result:
        assert result.getpixel((100, 70)) != (255, 255, 255)
        assert result.getpixel((62, 53)) != result.getpixel((200, 20))
        assert result.getpixel((200, 285)) != (255, 255, 255)


def test_resolve_mask_asset_supports_required_networks_and_sizes() -> None:
    for network_slug in ("mediasniper", "hybrid"):
        for size_slug in ("240x400", "300x600", "300x500"):
            asset = resolve_mask_asset(network_slug, size_slug)

            assert asset.network.slug == network_slug
            assert asset.size.slug == size_slug
            assert asset.path.exists()

    yandex_video_asset = resolve_mask_asset("yandex_video", "1920x1080")
    assert yandex_video_asset.network.slug == "yandex_video"
    assert yandex_video_asset.size.slug == "1920x1080"
    assert yandex_video_asset.path.exists()

    mediasniper_video_asset = resolve_mask_asset("mediasniper_video", "1920_1080_all")
    assert mediasniper_video_asset.network.slug == "mediasniper_video"
    assert mediasniper_video_asset.size.slug == "1920_1080_all"
    assert mediasniper_video_asset.path.name == "1920_1080_all.png"

    mediasniper_video_player_strip_asset = resolve_mask_asset("mediasniper_video", "1920_1080_br")
    assert mediasniper_video_player_strip_asset.network.slug == "mediasniper_video"
    assert mediasniper_video_player_strip_asset.size.slug == "1920_1080_br"
    assert mediasniper_video_player_strip_asset.path.name == "1920_1080_br_only.png"

    getintent_video_asset = resolve_mask_asset("getintent_video", "1920x1080")
    assert getintent_video_asset.network.slug == "getintent_video"
    assert (getintent_video_asset.size.width, getintent_video_asset.size.height) == GETINTENT_VIDEO_MASK_SIZE
    assert getintent_video_asset.path.exists()


def test_compose_masked_banner_resizes_creative_and_overlays_mask(tmp_path: Path) -> None:
    creative_path = tmp_path / "creative.png"
    output_path = tmp_path / "masked.png"
    Image.new("RGB", (60, 100), (220, 0, 0)).save(creative_path)
    mask_asset = resolve_mask_asset("mediasniper", "240x400")

    width, height = compose_masked_banner(
        creative_path=creative_path,
        mask_path=mask_asset.path,
        output_path=output_path,
        width=240,
        height=400,
    )

    assert (width, height) == (240, 400)
    with Image.open(output_path) as result:
        assert result.size == (240, 400)
        assert result.getpixel((120, 200))[:3] != (255, 255, 255)
        assert result.getpixel((10, 10))[:3] != (220, 0, 0)


def test_compose_yandex_video_renders_target_site_after_mask_overlay_without_favicon(tmp_path: Path) -> None:
    creative_path = tmp_path / "creative.png"
    output_path = tmp_path / "yandex-video.png"
    Image.new("RGB", (640, 360), (20, 30, 40)).save(creative_path)
    mask_asset = resolve_mask_asset("yandex_video", "1920x1080")

    width, height = compose_masked_banner(
        creative_path=creative_path,
        mask_path=mask_asset.path,
        output_path=output_path,
        width=1536,
        height=1024,
        network_slug="yandex_video",
        target_site_display="sberbank.com",
    )

    assert (width, height) == (1536, 1024)
    x, y, box_width, box_height = YANDEX_VIDEO_TARGET_SITE_BBOX
    with Image.open(output_path) as result:
        assert result.size == (1536, 1024)
        target_crop = result.crop((x, y, x + box_width, y + box_height)).convert("RGB")
        blank = Image.new("RGB", target_crop.size, (20, 30, 40))
        assert ImageChops.difference(target_crop, blank).getbbox() is not None


@pytest.mark.parametrize("size_slug", ["1920_1080_all", "1920_1080_br"])
def test_compose_mediasniper_video_is_exact_native_mask_alpha_composite(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, size_slug: str) -> None:
    creative_path = tmp_path / f"creative-{size_slug}.png"
    output_path = tmp_path / f"mediasniper-video-{size_slug}.png"
    Image.new("RGB", (640, 360), (20, 30, 40)).save(creative_path)
    mask_asset = resolve_mask_asset("mediasniper_video", size_slug)

    monkeypatch.setattr(masked_banner_module, "_load_target_site_favicon", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("favicon loader must not run")))
    monkeypatch.setattr(masked_banner_module, "_clear_yandex_video_mask_region", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("mask blackening must not run")))

    width, height = compose_masked_banner(
        creative_path=creative_path,
        mask_path=mask_asset.path,
        output_path=output_path,
        width=mask_asset.size.width,
        height=mask_asset.size.height,
        network_slug="mediasniper_video",
    )

    with Image.open(creative_path) as creative_raw, Image.open(mask_asset.path) as mask_raw, Image.open(output_path) as actual_raw:
        creative = creative_raw.convert("RGBA").resize((width, height), Image.Resampling.LANCZOS)
        mask = mask_raw.convert("RGBA")
        mask_size = mask.size
        expected = Image.alpha_composite(creative, mask)
        actual = actual_raw.convert("RGBA")

    assert (width, height) == (mask_asset.size.width, mask_asset.size.height)
    assert actual.size == mask_size
    assert ImageChops.difference(actual, expected).getbbox() is None


def test_compose_yandex_video_renders_target_site_favicon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    creative_path = tmp_path / "creative.png"
    output_path = tmp_path / "yandex-video-favicon.png"
    Image.new("RGB", (640, 360), (20, 30, 40)).save(creative_path)
    mask_asset = resolve_mask_asset("yandex_video", "1920x1080")

    def fake_load_favicon(target_site_url: str | None) -> Image.Image:
        assert target_site_url == "https://sberbank.com"
        return Image.new("RGBA", (16, 16), (230, 20, 20, 255))

    monkeypatch.setattr(masked_banner_module, "_load_target_site_favicon", fake_load_favicon)

    width, height = compose_masked_banner(
        creative_path=creative_path,
        mask_path=mask_asset.path,
        output_path=output_path,
        width=1536,
        height=1024,
        network_slug="yandex_video",
        target_site_url="https://sberbank.com",
        target_site_display="sberbank.com",
    )

    assert (width, height) == (1536, 1024)
    x, y, box_width, box_height = YANDEX_VIDEO_FAVICON_BBOX
    with Image.open(output_path) as result:
        assert result.size == (1536, 1024)
        center_pixel = result.convert("RGBA").getpixel((x + box_width // 2, y + box_height // 2))
        assert center_pixel[:3] == (230, 20, 20)


def test_compose_yandex_video_uses_uploaded_favicon_when_provided(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    creative_path = tmp_path / "creative.png"
    favicon_path = tmp_path / "favicon.png"
    output_path = tmp_path / "yandex-video-uploaded-favicon.png"
    Image.new("RGB", (640, 360), (20, 30, 40)).save(creative_path)
    Image.new("RGBA", (24, 24), (20, 230, 40, 255)).save(favicon_path)
    mask_asset = resolve_mask_asset("yandex_video", "1920x1080")
    monkeypatch.setattr(masked_banner_module, "_load_target_site_favicon", lambda target_site_url: None)

    width, height = compose_masked_banner(
        creative_path=creative_path,
        mask_path=mask_asset.path,
        output_path=output_path,
        width=1536,
        height=1024,
        network_slug="yandex_video",
        target_site_url="https://missing-icon.example.com",
        target_site_display="missing-icon.example.com",
        favicon_path=favicon_path,
    )

    assert (width, height) == (1536, 1024)
    x, y, box_width, box_height = YANDEX_VIDEO_FAVICON_BBOX
    with Image.open(output_path) as result:
        center_pixel = result.convert("RGBA").getpixel((x + box_width // 2, y + box_height // 2))
        assert center_pixel[:3] == (20, 230, 40)


def test_compose_yandex_video_ignores_missing_favicon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    creative_path = tmp_path / "creative.png"
    output_path = tmp_path / "yandex-video-no-favicon.png"
    Image.new("RGB", (640, 360), (20, 30, 40)).save(creative_path)
    mask_asset = resolve_mask_asset("yandex_video", "1920x1080")

    monkeypatch.setattr(masked_banner_module, "_load_target_site_favicon", lambda target_site_url: None)

    width, height = compose_masked_banner(
        creative_path=creative_path,
        mask_path=mask_asset.path,
        output_path=output_path,
        width=1536,
        height=1024,
        network_slug="yandex_video",
        target_site_url="https://missing-icon.example.com",
        target_site_display="missing-icon.example.com",
    )

    assert (width, height) == (1536, 1024)
    assert output_path.exists()


def test_favicon_link_parser_discovers_svg_icon_from_single_line_html() -> None:
    html = '<!doctype html><html><head><link rel="shortcut icon" href="https://static.example.com/icon.svg" type="image/x-icon"></head></html>'
    parser = masked_banner_module._FaviconLinkParser(base_url="https://firstdata.io")

    parser.feed(html)

    assert parser.icon_urls == ["https://static.example.com/icon.svg"]


def test_target_site_origin_urls_adds_apex_fallback_for_www_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(masked_banner_module, "_hostname_resolves_to_private_address", lambda hostname: False)

    origins = masked_banner_module._target_site_origin_urls(masked_banner_module.urlparse("https://www.firstdata.io/path?q=1"))

    assert origins == ("https://www.firstdata.io", "https://firstdata.io")


def test_render_svg_favicon_converts_svg_to_rgba_image() -> None:
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"><rect width="16" height="16" fill="#18a058"/></svg>'

    icon = masked_banner_module._render_svg_favicon(svg)

    assert icon.mode == "RGBA"
    assert icon.size == (masked_banner_module.YANDEX_VIDEO_FAVICON_SVG_RENDER_SIZE, masked_banner_module.YANDEX_VIDEO_FAVICON_SVG_RENDER_SIZE)
    assert icon.getpixel((icon.width // 2, icon.height // 2))[:3] == (24, 160, 88)


def test_compose_yandex_video_requires_target_site_display(tmp_path: Path) -> None:
    creative_path = tmp_path / "creative.png"
    output_path = tmp_path / "yandex-video.png"
    Image.new("RGB", (640, 360), (20, 30, 40)).save(creative_path)
    mask_asset = resolve_mask_asset("yandex_video", "1920x1080")

    with pytest.raises(ValidationError):
        compose_masked_banner(
            creative_path=creative_path,
            mask_path=mask_asset.path,
            output_path=output_path,
            width=1536,
            height=1024,
            network_slug="yandex_video",
        )


def test_compose_mediasniper_video_rejects_non_native_mask_dimensions(tmp_path: Path) -> None:
    creative_path = tmp_path / "creative.png"
    output_path = tmp_path / "mediasniper-video.png"
    Image.new("RGB", (640, 360), (20, 30, 40)).save(creative_path)
    mask_asset = resolve_mask_asset("mediasniper_video", "1920_1080_all")

    with pytest.raises(ValidationError, match="Размер файла маски"):
        compose_masked_banner(
            creative_path=creative_path,
            mask_path=mask_asset.path,
            output_path=output_path,
            width=1920,
            height=1080,
            network_slug="mediasniper_video",
        )


@pytest.mark.parametrize("minimum_age", ["0", "12", "99"])
def test_compose_getintent_video_renders_centered_age_without_background_panel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, minimum_age: str) -> None:
    creative_path = tmp_path / f"creative-{minimum_age}.png"
    pre_age_path = tmp_path / f"pre-age-{minimum_age}.png"
    output_path = tmp_path / f"getintent-{minimum_age}.png"
    Image.new("RGB", GETINTENT_VIDEO_MASK_SIZE, (24, 36, 48)).save(creative_path)
    mask_asset = resolve_mask_asset("getintent_video", "1920x1080")

    original_renderer = masked_banner_module.render_getintent_video_minimum_age

    def identity_renderer(base: Image.Image, age: str) -> Image.Image:
        assert age == minimum_age
        return base.copy()

    monkeypatch.setattr(masked_banner_module, "render_getintent_video_minimum_age", identity_renderer)
    compose_masked_banner(
        creative_path=creative_path,
        mask_path=mask_asset.path,
        output_path=pre_age_path,
        width=GETINTENT_VIDEO_MASK_SIZE[0],
        height=GETINTENT_VIDEO_MASK_SIZE[1],
        network_slug="getintent_video",
        target_site_url="https://example.com",
        target_site_display="example.com",
        minimum_age=minimum_age,
    )
    monkeypatch.setattr(masked_banner_module, "render_getintent_video_minimum_age", original_renderer)

    favicon_calls = 0

    def fail_favicon_fetch(target_site_url: str | None):
        nonlocal favicon_calls
        favicon_calls += 1
        raise AssertionError("GetIntent Video must not fetch a favicon")

    monkeypatch.setattr(masked_banner_module, "_load_target_site_favicon", fail_favicon_fetch)
    compose_masked_banner(
        creative_path=creative_path,
        mask_path=mask_asset.path,
        output_path=output_path,
        width=GETINTENT_VIDEO_MASK_SIZE[0],
        height=GETINTENT_VIDEO_MASK_SIZE[1],
        network_slug="getintent_video",
        target_site_url="https://example.com",
        target_site_display="example.com",
        minimum_age=minimum_age,
    )

    assert favicon_calls == 0
    assert GETINTENT_VIDEO_MINIMUM_AGE_BBOX == (1670, 180, 224, 110)
    with Image.open(pre_age_path) as before_raw, Image.open(output_path) as after_raw:
        before = before_raw.convert("RGB")
        after = after_raw.convert("RGB")
        diff_bbox = ImageChops.difference(before, after).getbbox()
        assert diff_bbox is not None
        x, y, box_width, box_height = GETINTENT_VIDEO_MINIMUM_AGE_BBOX
        assert x <= diff_bbox[0] < diff_bbox[2] <= x + box_width
        assert y <= diff_bbox[1] < diff_bbox[3] <= y + box_height
        glyph_center = ((diff_bbox[0] + diff_bbox[2]) / 2, (diff_bbox[1] + diff_bbox[3]) / 2)
        box_center = (x + box_width / 2, y + box_height / 2)
        assert abs(glyph_center[0] - box_center[0]) <= 2
        assert abs(glyph_center[1] - box_center[1]) <= 2
        changed_pixels = sum(1 for pixel in ImageChops.difference(before, after).crop((x, y, x + box_width, y + box_height)).get_flattened_data() if pixel != (0, 0, 0))
        assert changed_pixels < box_width * box_height * 0.45


@pytest.mark.parametrize(
    ("target_site_url", "target_site_display", "minimum_age"),
    [(None, "example.com", "12"), ("https://example.com", None, "12"), ("https://example.com", "example.com", None), ("https://example.com", "example.com", "100")],
)
def test_compose_getintent_video_requires_address_and_valid_age(tmp_path: Path, target_site_url: str | None, target_site_display: str | None, minimum_age: str | None) -> None:
    creative_path = tmp_path / "creative.png"
    output_path = tmp_path / "getintent-invalid.png"
    Image.new("RGB", GETINTENT_VIDEO_MASK_SIZE, (20, 30, 40)).save(creative_path)
    mask_asset = resolve_mask_asset("getintent_video", "1920x1080")

    with pytest.raises(ValidationError):
        compose_masked_banner(
            creative_path=creative_path,
            mask_path=mask_asset.path,
            output_path=output_path,
            width=GETINTENT_VIDEO_MASK_SIZE[0],
            height=GETINTENT_VIDEO_MASK_SIZE[1],
            network_slug="getintent_video",
            target_site_url=target_site_url,
            target_site_display=target_site_display,
            minimum_age=minimum_age,
        )


@pytest.mark.asyncio
async def test_generate_getintent_video_passes_minimum_age(tmp_path: Path) -> None:
    mask_asset = resolve_mask_asset("getintent_video", "1920x1080")
    creative_path = tmp_path / "creative.png"
    Image.new("RGB", (640, 360), (10, 120, 220)).save(creative_path)
    creative = CreativeInfo(path=creative_path, mime_type="image/png", width=640, height=360, size_bytes=creative_path.stat().st_size)
    request = MaskedBannerRequest(
        request_id="req-getintent",
        network_slug="getintent_video",
        network_display_name="GetIntent Video",
        size_slug="1920x1080",
        size=mask_asset.size,
        creative=creative,
        output_path=tmp_path / "getintent.png",
        target_site_url="https://example.com",
        target_site_display="example.com",
        minimum_age="12",
    )

    result = await generate_masked_banner(request)

    assert (result.width, result.height) == GETINTENT_VIDEO_MASK_SIZE
    assert result.output_path.exists()


@pytest.mark.asyncio
async def test_generate_masked_banner_uses_selected_hybrid_mask(tmp_path: Path) -> None:
    size = get_banner_size("300x500")
    assert size is not None
    creative_path = tmp_path / "creative.jpg"
    output_path = tmp_path / "masked.png"
    Image.new("RGB", (150, 250), (10, 120, 220)).save(creative_path)
    request = MaskedBannerRequest(
        request_id="req-mask-hybrid",
        network_slug="hybrid",
        network_display_name="Hybrid",
        size_slug=size.slug,
        size=size,
        creative=CreativeInfo(path=creative_path, mime_type="image/jpeg", width=150, height=250, size_bytes=creative_path.stat().st_size),
        output_path=output_path,
    )

    result = await generate_masked_banner(request)

    assert result.output_path == output_path
    assert result.width == 300
    assert result.height == 500
    assert result.mask_asset.network.slug == "hybrid"
    assert result.mask_asset.size.slug == "300x500"


def test_stage2_compose_centers_proportionally_fitted_creative_in_banner_region(tmp_path: Path) -> None:
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    output_path = tmp_path / "stage2.png"
    banner_path = tmp_path / "banner.png"
    Image.new("RGB", (240, 180), (255, 255, 255)).save(source_path)
    Image.new("RGBA", (10, 10), (20, 40, 220, 255)).save(creative_path)

    compose_stage2_image(
        source_screenshot_path=source_path,
        creative_path=creative_path,
        effective_click_url="https://example.com/landing?utm_source=test&utm_medium=cpc",
        banner_bbox=BoundingBox(x=80, y=30, width=60, height=90),
        landing_bbox=fallback_landing_bbox(image_width=240, image_height=180),
        output_path=output_path,
        banner_replaced_path=banner_path,
    )

    with Image.open(banner_path) as result:
        assert result.getpixel((110, 60)) == (20, 40, 220)
        assert result.getpixel((110, 104)) == (20, 40, 220)
        assert result.getpixel((110, 44)) == (255, 255, 255)
        assert result.getpixel((110, 105)) == (255, 255, 255)
        assert result.getpixel((79, 45)) == (255, 255, 255)
        assert result.getpixel((142, 75)) == (255, 255, 255)
        assert result.getpixel((90, 29)) == (255, 255, 255)
        assert result.getpixel((90, 120)) == (255, 255, 255)


def test_stage2_compose_preserves_original_creative_aspect_ratio_when_slot_ratio_differs(tmp_path: Path) -> None:
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    banner_path = tmp_path / "banner.png"
    output_path = tmp_path / "stage2.png"
    source_size = (240, 180)
    bbox = BoundingBox(x=40, y=30, width=120, height=80)
    Image.new("RGB", source_size, (255, 255, 255)).save(source_path)
    Image.new("RGBA", (20, 10), (10, 120, 220, 255)).save(creative_path)

    compose_stage2_image(
        source_screenshot_path=source_path,
        creative_path=creative_path,
        effective_click_url="https://example.com/landing?utm_source=test&utm_medium=cpc",
        banner_bbox=bbox,
        landing_bbox=fallback_landing_bbox(image_width=source_size[0], image_height=source_size[1]),
        output_path=output_path,
        banner_replaced_path=banner_path,
    )

    with Image.open(source_path) as source_raw, Image.open(banner_path) as result_raw:
        diff_bbox = ImageChops.difference(source_raw.convert("RGB"), result_raw.convert("RGB")).getbbox()

    assert diff_bbox is not None
    inserted_width = diff_bbox[2] - diff_bbox[0]
    inserted_height = diff_bbox[3] - diff_bbox[1]
    source_ratio = 20 / 10
    inserted_ratio = inserted_width / inserted_height
    assert abs(inserted_ratio - source_ratio) <= 0.065
    assert diff_bbox[0] == bbox.x
    assert diff_bbox[2] == bbox.right
    assert bbox.y <= diff_bbox[1] <= bbox.y + 10
    assert bbox.y + 70 <= diff_bbox[3] <= bbox.bottom


def test_stage2_compose_full_cover_replacement_overwrites_old_bbox_pixels(tmp_path: Path) -> None:
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    output_path = tmp_path / "stage2.png"
    banner_path = tmp_path / "banner.png"
    bbox = BoundingBox(x=30, y=20, width=80, height=60)
    Image.new("RGB", (160, 120), (0, 255, 0)).save(source_path)
    Image.new("RGBA", (20, 20), (255, 0, 0, 0)).save(creative_path)

    compose_stage2_image(
        source_screenshot_path=source_path,
        creative_path=creative_path,
        effective_click_url="https://example.com/landing?utm_source=test&utm_medium=cpc",
        banner_bbox=bbox,
        landing_bbox=fallback_landing_bbox(image_width=160, image_height=120),
        output_path=output_path,
        banner_replaced_path=banner_path,
        full_cover_replacement=True,
    )

    with Image.open(banner_path) as result:
        assert result.getpixel((bbox.x + bbox.width // 2, bbox.y + bbox.height // 2)) == (255, 255, 255)
        assert result.getpixel((bbox.x + 2, bbox.y + bbox.height // 2)) == (255, 255, 255)
        assert result.getpixel((bbox.x + bbox.width - 3, bbox.y + bbox.height // 2)) == (255, 255, 255)
        assert result.getpixel((bbox.x - 1, bbox.y + bbox.height // 2)) == (0, 255, 0)


def test_ad_mark_label_uses_cyrillic_capable_font_and_transparent_background() -> None:
    font = _load_cyrillic_font(size=13)
    label = _render_ad_mark_label(font=font)
    alpha = label.getchannel("A")

    assert font.getmask(AD_MARK_LABEL).getbbox() != ImageFont.load_default().getmask(AD_MARK_LABEL).getbbox()
    assert alpha.getbbox() is not None
    assert label.getpixel((0, 0))[3] == 0
    assert label.getpixel((label.width - 1, label.height - 1))[3] == 0


def test_stage2_compose_ad_mark_keeps_background_transparent_over_creative(tmp_path: Path) -> None:
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    banner_path = tmp_path / "banner.png"
    output_path = tmp_path / "stage2.png"
    bbox = BoundingBox(x=20, y=20, width=180, height=100)
    Image.new("RGB", (240, 180), (255, 255, 255)).save(source_path)
    Image.new("RGBA", (10, 10), (20, 40, 220, 255)).save(creative_path)

    compose_stage2_image(
        source_screenshot_path=source_path,
        creative_path=creative_path,
        effective_click_url="https://example.com/landing?utm_source=test&utm_medium=cpc",
        banner_bbox=bbox,
        landing_bbox=fallback_landing_bbox(image_width=240, image_height=180),
        output_path=output_path,
        banner_replaced_path=banner_path,
    )

    fitted_left = bbox.x + (bbox.width - bbox.height) // 2
    with Image.open(banner_path) as result:
        assert result.getpixel((fitted_left + 6, bbox.y + 6)) == (20, 40, 220)
        assert result.getpixel((fitted_left + 18, bbox.y + 12)) != (20, 40, 220)


def test_validate_banner_bbox_semantics_rejects_right_sidebar_bbox_outside_right_third() -> None:
    spec = get_spec("first-data-rtb-banners-240x400-image")
    assert spec is not None

    with pytest.raises(ValidationError, match="неправдоподобную область"):
        validate_banner_bbox_semantics(
            BoundingBox(x=707, y=197, width=240, height=400),
            spec=spec,
            image_width=1280,
            image_height=719,
            request_id="req-off-target",
        )


def test_validate_banner_bbox_semantics_accepts_right_sidebar_bbox_in_right_third() -> None:
    spec = get_spec("first-data-rtb-banners-240x400-image")
    assert spec is not None

    bbox = BoundingBox(x=874, y=146, width=240, height=400)

    assert (
        validate_banner_bbox_semantics(
            bbox,
            spec=spec,
            image_width=1280,
            image_height=719,
            request_id="req-right-target",
        )
        == bbox
    )


def test_stage2_compose_trims_transparent_padding_before_proportional_fit(tmp_path: Path) -> None:
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    output_path = tmp_path / "stage2.png"
    banner_path = tmp_path / "banner.png"
    bbox = BoundingBox(x=60, y=40, width=160, height=100)
    source = Image.new("RGB", (300, 220), (255, 255, 255))
    source.save(source_path)
    creative = Image.new("RGBA", (120, 80), (0, 0, 0, 0))
    draw = ImageDraw.Draw(creative)
    draw.rectangle((30, 20, 89, 59), fill=(15, 35, 210, 255))
    creative.save(creative_path)

    compose_stage2_image(
        source_screenshot_path=source_path,
        creative_path=creative_path,
        effective_click_url="https://example.com/landing?utm_source=test&utm_medium=cpc",
        banner_bbox=bbox,
        landing_bbox=fallback_landing_bbox(image_width=300, image_height=220),
        output_path=output_path,
        banner_replaced_path=banner_path,
    )

    with Image.open(banner_path) as result:
        assert result.getpixel((bbox.x + 5, bbox.y)) == (15, 35, 210)
        assert result.getpixel((bbox.right - 6, bbox.y)) == (15, 35, 210)
        assert result.getpixel((bbox.x + 5, bbox.bottom - 1)) == (15, 35, 210)
        assert result.getpixel((bbox.right - 6, bbox.bottom - 1)) == (15, 35, 210)
        assert result.getpixel((bbox.x, bbox.y + 10)) == (255, 255, 255)
        assert result.getpixel((bbox.right - 1, bbox.y + 10)) == (255, 255, 255)
        assert result.getpixel((bbox.x - 1, bbox.y + 10)) == (255, 255, 255)
        assert result.getpixel((bbox.right, bbox.y + 10)) == (255, 255, 255)


def test_stage2_compose_changed_region_matches_proportionally_fitted_creative_and_ad_mark(tmp_path: Path) -> None:
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    banner_path = tmp_path / "banner.png"
    output_path = tmp_path / "stage2.png"
    bbox = BoundingBox(x=42, y=35, width=96, height=64)
    Image.new("RGB", (220, 180), (255, 255, 255)).save(source_path)
    Image.new("RGBA", (32, 32), (230, 30, 20, 255)).save(creative_path)

    compose_stage2_image(
        source_screenshot_path=source_path,
        creative_path=creative_path,
        effective_click_url="https://example.com/landing?utm_source=test&utm_medium=cpc",
        banner_bbox=bbox,
        landing_bbox=fallback_landing_bbox(image_width=220, image_height=180),
        output_path=output_path,
        banner_replaced_path=banner_path,
    )

    with Image.open(source_path) as source_raw, Image.open(banner_path) as result_raw:
        diff_bbox = ImageChops.difference(source_raw.convert("RGB"), result_raw.convert("RGB")).getbbox()

    assert diff_bbox is not None
    expected_fit_height = bbox.height
    expected_fit_width = bbox.height
    expected_fit_left = bbox.x + (bbox.width - expected_fit_width) // 2
    inserted_width = diff_bbox[2] - expected_fit_left
    inserted_height = diff_bbox[3] - diff_bbox[1]
    assert diff_bbox[0] <= expected_fit_left
    assert diff_bbox[1:] == (bbox.y, expected_fit_left + expected_fit_width, bbox.y + expected_fit_height)
    assert abs((inserted_width / inserted_height) - 1.0) <= 0.08


def test_fallback_landing_bbox_uses_lower_10_to_20_percent() -> None:
    bbox = fallback_landing_bbox(image_width=1000, image_height=800)

    assert bbox.x == 0
    assert bbox.width == 1000
    assert bbox.y == 672
    assert bbox.height == 128


def test_landing_area_artifact_contains_only_address_bar_no_body_text(tmp_path: Path) -> None:
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    output_path = tmp_path / "stage2.png"
    landing_path = tmp_path / "landing.png"
    image_width = 500
    image_height = 400
    landing_bbox = fallback_landing_bbox(image_width=image_width, image_height=image_height)
    Image.new("RGB", (image_width, image_height), "white").save(source_path)
    Image.new("RGBA", (20, 20), (255, 0, 0, 255)).save(creative_path)

    compose_stage2_image(
        source_screenshot_path=source_path,
        creative_path=creative_path,
        effective_click_url="https://example.com/landing?utm_source=test&utm_medium=cpc",
        banner_bbox=BoundingBox(x=50, y=40, width=120, height=80),
        landing_bbox=landing_bbox,
        output_path=output_path,
        landing_area_path=landing_path,
    )

    address_bar_height = _address_bar_only_height(BoundingBox(0, 0, landing_bbox.width, landing_bbox.height))
    with Image.open(landing_path) as landing:
        assert landing.size == (landing_bbox.width, landing_bbox.height)
        assert 42 <= address_bar_height <= 68
        assert landing.getbbox() is not None
        assert landing.getpixel((landing_bbox.width // 2, address_bar_height + 8)) == (244, 246, 248)
        assert landing.getpixel((landing_bbox.width // 2, landing_bbox.height - 5)) == (244, 246, 248)


def test_tgo_semantic_banner_bbox_rejects_wrong_internal_image_shape(caplog: pytest.LogCaptureFixture) -> None:
    spec = get_spec("yandex-tgo-600x600-text-image")
    assert spec is not None
    bbox = BoundingBox(x=922, y=215, width=217, height=373)

    with caplog.at_level("INFO", logger="reports_ad.stage2_proof"), pytest.raises(ValidationError, match="неправдоподобную область баннера"):
        validate_banner_bbox_semantics(
            bbox,
            spec=spec,
            image_width=1280,
            image_height=828,
            request_id="ee76e0a22f96",
        )

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "stage2_banner_bbox_semantic_validation request_id=ee76e0a22f96" in log_text
    assert "spec_slug=yandex-tgo-600x600-text-image" in log_text
    assert "decision=rejected" in log_text
    assert "expected_ratio=1.000" in log_text
    assert "reason_codes=aspect_ratio_deviation" in log_text


def test_tgo_semantic_banner_bbox_accepts_plausible_square_slot(caplog: pytest.LogCaptureFixture) -> None:
    spec = get_spec("yandex-tgo-600x600-text-image")
    assert spec is not None
    bbox = BoundingBox(x=680, y=140, width=360, height=350)

    with caplog.at_level("INFO", logger="reports_ad.stage2_proof"):
        result = validate_banner_bbox_semantics(
            bbox,
            spec=spec,
            image_width=1280,
            image_height=828,
            request_id="req-plausible-tgo",
        )

    assert result == bbox
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "stage2_banner_bbox_semantic_validation request_id=req-plausible-tgo" in log_text
    assert "decision=accepted" in log_text
    assert "reason_codes=none" in log_text


def test_gemma_client_builder_uses_ocr_config_fields() -> None:
    config = BotConfig(
        telegram_token="test",
        ocr_llm_base_url="http://gemma.firstdata.io/v1/",
        ocr_llm_model="cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit",
        ocr_llm_authorization_header="Basic placeholder",
        ocr_llm_timeout_seconds=20,
        ocr_llm_max_retries=3,
        ocr_llm_confidence_threshold=0.6,
    )

    client = build_gemma_client_from_config(config)

    assert client._base_url == "http://gemma.firstdata.io/v1"
    assert client._model == "cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit"
    assert client._authorization_header == "Basic placeholder"
    assert client._timeout_seconds == 20
    assert client._max_retries == 3
    assert client._confidence_threshold == 0.6
    assert client._ocr_image_max_width == 1280
    assert client._ocr_image_max_pixels == 2_000_000
    assert client._composition_validation_crop_margin_percent == 0.20
    assert client._composition_validation_crop_margin_min_pixels == 60


def test_parse_bbox_result_rescales_from_resized_coordinate_space() -> None:
    result = _parse_bbox_result(
        {
            "bbox": {"x": 400, "y": 100, "width": 200, "height": 300},
            "confidence": 0.95,
            "reason": "bbox taken from resized preview",
            "coordinate_space": "resized_pixels",
            "image_width": 800,
            "image_height": 600,
        },
        stage="banner_bbox",
        confidence_threshold=0.45,
        source_image_width=1600,
        source_image_height=1200,
    )

    assert result.bbox == BoundingBox(800, 200, 400, 600)
    assert result.raw_metadata["coordinate_space"] == "resized_pixels"
    assert result.raw_metadata["coordinate_image_width"] == 800
    assert result.raw_metadata["coordinate_image_height"] == 600
    assert result.raw_metadata["scaled_to_source"] is True


def test_parse_bbox_result_rejects_invalid_normalized_coordinate_space() -> None:
    with pytest.raises(GemmaClientError, match="invalid_coordinate_space"):
        _parse_bbox_result(
            {
                "bbox": {"x": 922, "y": 215, "width": 217, "height": 373},
                "confidence": 0.95,
                "coordinate_space": "normalized",
                "reason": "model mislabeled pixel coordinates as normalized",
            },
            stage="banner_bbox",
            confidence_threshold=0.45,
            source_image_width=1280,
            source_image_height=828,
        )


def test_banner_retry_prompt_strictly_forbids_mixed_coordinate_spaces() -> None:
    spec = get_spec("first-data-rtb-banners-240x400-image")
    assert spec is not None
    screenshot = SourceScreenshotInfo(path=Path("source.png"), mime_type="image/png", width=1280, height=719, size_bytes=123)

    prompt = _banner_prompt(
        screenshot=screenshot,
        spec=spec,
        ocr_image_width=1280,
        ocr_image_height=719,
        validation_feedback="previous bbox x=902,y=147,width=154,height=396 was rejected by aspect ratio",
        selection_attempt=2,
    )

    assert "Coordinate contract is strict and MUST NOT be mixed" in prompt
    assert "NEVER put pixel numbers with coordinate_space='normalized'" in prompt
    assert "values such as 707 or 116 are INVALID" in prompt
    assert "if bbox values are pixel-like numbers greater than 1" in prompt
    assert "coordinate_space MUST be 'source_pixels' or 'resized_pixels'" in prompt
    assert "x=0.70 not x=707" in prompt


def test_parse_bbox_result_requires_resized_pixel_image_size() -> None:
    with pytest.raises(GemmaClientError, match="invalid_coordinate_space"):
        _parse_bbox_result(
            {
                "bbox": {"x": 400, "y": 100, "width": 200, "height": 300},
                "confidence": 0.95,
                "coordinate_space": "resized_pixels",
            },
            stage="banner_bbox",
            confidence_threshold=0.45,
            source_image_width=1600,
            source_image_height=1200,
        )


def test_parse_composition_validation_result_requires_structured_pass_flag() -> None:
    with pytest.raises(GemmaClientError, match="verification_passed"):
        _parse_composition_validation_result(
            {"confidence": 0.9, "reason": "missing pass flag"},
            source_image_width=400,
            source_image_height=300,
            confidence_threshold=0.45,
        )


def test_parse_composition_validation_result_detects_exact_pass_and_correction() -> None:
    passed = _parse_composition_validation_result(
        {
            "verification_passed": True,
            "original_banner_bbox": {"x": 20, "y": 30, "width": 100, "height": 70},
            "actual_inserted_creative_bbox": {"x": 20, "y": 30, "width": 100, "height": 70},
            "deltas": {"x": 0, "y": 0, "width": 0, "height": 0},
            "recommended_corrected_bbox": None,
            "confidence": 0.9,
            "reason": "exact match",
        },
        source_image_width=400,
        source_image_height=300,
        confidence_threshold=0.45,
    )
    failed = _parse_composition_validation_result(
        {
            "verification_passed": False,
            "original_banner_bbox": {"x": 40, "y": 50, "width": 120, "height": 80},
            "actual_inserted_creative_bbox": {"x": 20, "y": 30, "width": 100, "height": 70},
            "deltas": {"x": -20, "y": -20, "width": -20, "height": -10},
            "recommended_corrected_bbox": {"x": 40, "y": 50, "width": 120, "height": 80},
            "confidence": 0.95,
            "reason": "offset mismatch",
        },
        source_image_width=400,
        source_image_height=300,
        confidence_threshold=0.45,
    )

    assert passed.passed is True
    assert passed.corrected_bbox == BoundingBox(20, 30, 100, 70)
    assert failed.passed is False
    assert failed.corrected_bbox == BoundingBox(40, 50, 120, 80)
    assert failed.deltas == {"x": -20, "y": -20, "width": -20, "height": -10}


def test_parse_composition_validation_result_overrides_false_pass_flag_for_safe_exact_match() -> None:
    result = _parse_composition_validation_result(
        {
            "verification_passed": False,
            "original_banner_bbox": {"x": 20, "y": 30, "width": 100, "height": 70},
            "actual_inserted_creative_bbox": {"x": 20, "y": 30, "width": 100, "height": 70},
            "deltas": {"x": 0, "y": 0, "width": 0, "height": 0},
            "corner_deltas": {
                "top_left": {"dx": 0, "dy": 0},
                "top_right": {"dx": 0, "dy": 0},
                "bottom_right": {"dx": 0, "dy": 0},
                "bottom_left": {"dx": 0, "dy": 0},
            },
            "recommended_corrected_bbox": {"x": 20, "y": 30, "width": 100, "height": 70},
            "confidence": 1.0,
            "reason": "the creative exactly covered the original banner",
        },
        source_image_width=400,
        source_image_height=300,
        confidence_threshold=0.45,
    )

    assert result.passed is True
    assert result.original_banner_bbox == BoundingBox(20, 30, 100, 70)
    assert result.inserted_creative_bbox == BoundingBox(20, 30, 100, 70)
    assert result.corrected_bbox == BoundingBox(20, 30, 100, 70)
    assert result.deltas == {"x": 0, "y": 0, "width": 0, "height": 0}
    assert result.corner_deltas == {
        "top_left": {"dx": 0, "dy": 0},
        "top_right": {"dx": 0, "dy": 0},
        "bottom_right": {"dx": 0, "dy": 0},
        "bottom_left": {"dx": 0, "dy": 0},
    }
    assert result.raw_metadata["raw_passed"] is False
    assert result.raw_metadata["model_pass_flag_contradicted_exact_match"] is True


def test_composition_validation_crop_rect_expands_and_clamps_bbox() -> None:
    crop = _composition_validation_crop_rect(
        bbox=BoundingBox(20, 30, 100, 70),
        image_width=400,
        image_height=300,
        margin_percent=0.20,
        margin_min_pixels=60,
    )

    assert (crop.x, crop.y, crop.width, crop.height) == (0, 0, 180, 160)
    assert (crop.right, crop.bottom) == (180, 160)
    assert (crop.margin_x, crop.margin_y) == (60, 60)


def test_composition_validation_prompt_declares_full_screenshot_corner_delta_contract() -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    screenshot = SourceScreenshotInfo(path=Path("source.png"), mime_type="image/png", width=400, height=300, size_bytes=123)
    bbox = BoundingBox(80, 70, 120, 80)

    prompt = _composition_validation_prompt(
        screenshot=screenshot,
        spec=spec,
        current_banner_bbox=bbox,
        ocr_image_width=400,
        ocr_image_height=300,
        validation_attempt=1,
        crop=None,
    )

    assert "FULL original source screenshot" in prompt
    assert "FULL composed screenshot" in prompt
    assert "corner_deltas" in prompt
    assert "top_left, top_right, bottom_right, bottom_left" in prompt
    assert "dx = original_corner_x - inserted_creative_corner_x" in prompt
    assert "dy = original_corner_y - inserted_creative_corner_y" in prompt


def test_parse_composition_validation_result_converts_crop_pixels_to_source_space() -> None:
    crop = _composition_validation_crop_rect(
        bbox=BoundingBox(80, 70, 120, 80),
        image_width=400,
        image_height=300,
        margin_percent=0.20,
        margin_min_pixels=40,
    )

    result = _parse_composition_validation_result(
        {
            "verification_passed": False,
            "original_banner_bbox": {"x": 30, "y": 40, "width": 130, "height": 90},
            "actual_inserted_creative_bbox": {"x": 40, "y": 40, "width": 120, "height": 80},
            "deltas": {"x": 10, "y": 0, "width": -10, "height": -10},
            "recommended_corrected_bbox": {"x": 30, "y": 40, "width": 130, "height": 90},
            "confidence": 0.95,
            "reason": "crop local correction",
            "coordinate_space": "crop_pixels",
            "image_width": crop.width,
            "image_height": crop.height,
        },
        source_image_width=400,
        source_image_height=300,
        confidence_threshold=0.45,
        crop=crop,
    )

    assert result.passed is False
    assert result.original_banner_bbox == BoundingBox(70, 70, 130, 90)
    assert result.inserted_creative_bbox == BoundingBox(80, 70, 120, 80)
    assert result.corrected_bbox == BoundingBox(70, 70, 130, 90)
    assert result.raw_metadata["crop_origin_x"] == 40
    assert result.raw_metadata["crop_origin_y"] == 30


def test_parse_composition_validation_result_preserves_source_pixel_crop_response() -> None:
    crop = _composition_validation_crop_rect(
        bbox=BoundingBox(80, 70, 120, 80),
        image_width=400,
        image_height=300,
        margin_percent=0.20,
        margin_min_pixels=40,
    )

    result = _parse_composition_validation_result(
        {
            "verification_passed": True,
            "original_banner_bbox": {"x": 80, "y": 70, "width": 120, "height": 80},
            "actual_inserted_creative_bbox": {"x": 80, "y": 70, "width": 120, "height": 80},
            "deltas": {"x": 0, "y": 0, "width": 0, "height": 0},
            "recommended_corrected_bbox": None,
            "confidence": 0.95,
            "reason": "exact source coords",
            "coordinate_space": "source_pixels",
        },
        source_image_width=400,
        source_image_height=300,
        confidence_threshold=0.45,
        crop=crop,
    )

    assert result.passed is True
    assert result.corrected_bbox == BoundingBox(80, 70, 120, 80)
    assert result.raw_metadata["crop_width"] == crop.width


def test_parse_composition_validation_result_normalizes_corner_deltas() -> None:
    result = _parse_composition_validation_result(
        {
            "verification_passed": False,
            "original_banner_bbox": {"x": 40, "y": 50, "width": 120, "height": 80},
            "actual_inserted_creative_bbox": {"x": 20, "y": 30, "width": 100, "height": 70},
            "corner_deltas": {
                "top_left": {"dx": 20.2, "dy": 19.8},
                "top_right": {"dx": 40, "dy": 20},
                "bottom_right": {"dx": 40, "dy": 30},
                "bottom_left": {"dx": 20, "dy": 30},
            },
            "recommended_corrected_bbox": {"x": 40, "y": 50, "width": 120, "height": 80},
            "confidence": 0.95,
            "reason": "corner displacement mismatch",
            "coordinate_space": "source_pixels",
        },
        source_image_width=400,
        source_image_height=300,
        confidence_threshold=0.45,
    )

    assert result.passed is False
    assert result.corner_deltas == {
        "top_left": {"dx": 20, "dy": 20},
        "top_right": {"dx": 40, "dy": 20},
        "bottom_right": {"dx": 40, "dy": 30},
        "bottom_left": {"dx": 20, "dy": 30},
    }


def test_parse_composition_validation_result_defaults_corner_deltas_from_bboxes() -> None:
    result = _parse_composition_validation_result(
        {
            "verification_passed": False,
            "original_banner_bbox": {"x": 40, "y": 50, "width": 120, "height": 80},
            "actual_inserted_creative_bbox": {"x": 20, "y": 30, "width": 100, "height": 70},
            "recommended_corrected_bbox": {"x": 40, "y": 50, "width": 120, "height": 80},
            "confidence": 0.95,
            "reason": "legacy response without corner deltas",
            "coordinate_space": "source_pixels",
        },
        source_image_width=400,
        source_image_height=300,
        confidence_threshold=0.45,
    )

    assert result.corner_deltas == {
        "top_left": {"dx": 20, "dy": 20},
        "top_right": {"dx": 40, "dy": 20},
        "bottom_right": {"dx": 40, "dy": 30},
        "bottom_left": {"dx": 20, "dy": 30},
    }


def test_stage2_composition_correction_prefers_new_recommended_bbox_retry() -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    request = ProofRequest(
        request_id="req-correction-new-bbox",
        network_display_name="Яндекс",
        network_slug="yandex_graphic_banners",
        spec=spec,
        banner_type=spec.display_name,
        source_screenshot=SourceScreenshotInfo(path=Path("source.png"), mime_type="image/png", width=400, height=300, size_bytes=123),
        creative=CreativeInfo(path=Path("creative.png"), mime_type="image/png", width=300, height=250, size_bytes=123),
        landing_url="https://example.com/landing",
        utm_params={},
        placement=Placement(mode="template", value="screenshot-detected"),
        output_path=Path("final.png"),
    )
    current_bbox = BoundingBox(20, 30, 100, 70)
    corrected_bbox = BoundingBox(40, 50, 120, 80)

    correction = _select_next_composition_bbox(
        validation=CompositionValidationResult(
            passed=False,
            original_banner_bbox=corrected_bbox,
            inserted_creative_bbox=current_bbox,
            corrected_bbox=corrected_bbox,
            deltas={"x": -20, "y": -20, "width": -20, "height": -10},
            corner_deltas={
                "top_left": {"dx": 999, "dy": 999},
                "top_right": {"dx": 999, "dy": 999},
                "bottom_right": {"dx": 999, "dy": 999},
                "bottom_left": {"dx": 999, "dy": 999},
            },
            confidence=0.95,
            reason="recommended bbox differs from current slot",
        ),
        current_bbox=current_bbox,
        request=request,
        attempt=1,
    )

    assert correction is not None
    assert correction.source == "corrected_bbox"
    assert correction.bbox == corrected_bbox
    assert correction.full_cover_replacement is False


def test_stage2_composition_correction_uses_full_cover_for_render_coverage_mismatch(caplog: pytest.LogCaptureFixture) -> None:
    spec = get_spec("first-data-rtb-banners-240x400-image")
    assert spec is not None
    request = ProofRequest(
        request_id="req-render-cover-retry",
        network_display_name="First Data",
        network_slug="first_data_rtb_banners",
        spec=spec,
        banner_type=spec.display_name,
        source_screenshot=SourceScreenshotInfo(path=Path("source.png"), mime_type="image/png", width=1280, height=719, size_bytes=123),
        creative=CreativeInfo(path=Path("creative.png"), mime_type="image/png", width=240, height=400, size_bytes=123),
        landing_url="https://example.com/landing",
        utm_params={},
        placement=Placement(mode="template", value="screenshot-detected"),
        output_path=Path("final.png"),
    )
    current_bbox = BoundingBox(896, 201, 177, 410)

    with caplog.at_level("INFO", logger="reports_ad.stage2_proof"):
        correction = _select_next_composition_bbox(
            validation=CompositionValidationResult(
                passed=False,
                original_banner_bbox=current_bbox,
                inserted_creative_bbox=BoundingBox(896, 281, 177, 330),
                corrected_bbox=current_bbox,
                deltas={"x": 0, "y": 80, "width": 0, "height": -80},
                corner_deltas={
                    "top_left": {"dx": 0, "dy": -80},
                    "top_right": {"dx": 0, "dy": -80},
                    "bottom_right": {"dx": 0, "dy": 0},
                    "bottom_left": {"dx": 0, "dy": 0},
                },
                confidence=0.95,
                reason="slot correct but rendered creative covers only lower part",
            ),
            current_bbox=current_bbox,
            request=request,
            attempt=1,
        )

    assert correction is not None
    assert correction.source == "render_full_cover"
    assert correction.bbox == current_bbox
    assert correction.full_cover_replacement is True
    assert "stage2_composition_validation_render_cover_retry" in caplog.text
    assert "correction_source=corner_deltas" not in caplog.text


def test_tgo_candidates_choose_full_ad_card_over_internal_image_asset() -> None:
    spec = get_spec("yandex-tgo-600x600-text-image")
    assert spec is not None

    result = _parse_bbox_result(
        {
            "candidates": [
                {
                    "role": "internal_image_asset",
                    "bbox": {"x": 820, "y": 190, "width": 225, "height": 400},
                    "confidence": 0.99,
                    "coordinate_space": "resized_pixels",
                    "image_width": 1280,
                    "image_height": 828,
                },
                {
                    "role": "full_ad_card",
                    "bbox": {"x": 680, "y": 140, "width": 360, "height": 350},
                    "confidence": 0.80,
                    "coordinate_space": "resized_pixels",
                    "image_width": 1280,
                    "image_height": 828,
                },
                {
                    "role": "browser_ui",
                    "bbox": {"x": 0, "y": 760, "width": 1280, "height": 68},
                    "confidence": 1.0,
                    "coordinate_space": "resized_pixels",
                    "image_width": 1280,
                    "image_height": 828,
                },
            ],
            "confidence": 0.1,
            "reason": "multiple candidates",
        },
        stage="banner_bbox",
        spec=spec,
        confidence_threshold=0.45,
        source_image_width=1280,
        source_image_height=828,
    )

    assert result.bbox == BoundingBox(680, 140, 360, 350)
    assert result.confidence == 0.80
    assert result.raw_metadata["candidate_role"] == "full_ad_card"


def test_banner_prompt_includes_selected_spec_dimensions_and_position_hint() -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    screenshot = SourceScreenshotInfo(path=Path("source.png"), mime_type="image/png", width=1024, height=768, size_bytes=123)

    prompt = _banner_prompt(screenshot=screenshot, spec=spec)

    assert spec.slug in prompt
    assert spec.dimensions_label in prompt
    assert spec.position_hint in prompt
    assert "original ad slot" in prompt
    assert "uploaded replacement creative" in prompt
    assert "landing/link/browser area" in prompt
    assert "coordinate_space='normalized'" in prompt
    assert "source screenshot" in prompt


def test_banner_prompt_includes_validation_feedback_for_retry() -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    screenshot = SourceScreenshotInfo(path=Path("source.png"), mime_type="image/png", width=1024, height=768, size_bytes=123)

    prompt = _banner_prompt(screenshot=screenshot, spec=spec, validation_feedback="reason_codes=out_of_bounds; previous_invalid_bbox=x=390,y=10,width=50,height=50", selection_attempt=2)

    assert "Base task instructions:" in prompt
    assert "Retry feedback:" in prompt
    assert "Previous validation feedback for this retry:" in prompt
    assert "selection attempt 2" in prompt
    assert "previous bbox candidate was rejected" in prompt
    assert "reason_codes=out_of_bounds" in prompt
    assert "Do NOT repeat the previous invalid bbox" in prompt
    assert "previous_invalid_bbox=x=390,y=10,width=50,height=50" in prompt
    assert "coordinate contract" in prompt


def test_banner_retry_prompt_keeps_full_base_instructions_with_feedback_section() -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    screenshot = SourceScreenshotInfo(path=Path("source.png"), mime_type="image/png", width=1024, height=768, size_bytes=123)
    feedback = (
        "validation_error=ИИ вернул область баннера за границами скриншота; "
        "selected_spec=yandex-graphic-banners-300x250-image; "
        "expected_dimensions=300×250; previous_invalid_bbox=x=390,y=10,width=50,height=50; "
        "previous_invalid_center=x=415.0,y=35.0; previous_invalid_zone=top_right; "
        "reason_codes=out_of_bounds"
    )

    base_prompt = _banner_prompt(screenshot=screenshot, spec=spec)
    retry_prompt = _banner_prompt(screenshot=screenshot, spec=spec, validation_feedback=feedback, selection_attempt=2)

    assert base_prompt in retry_prompt
    assert retry_prompt.index("Base task instructions:") < retry_prompt.index("Retry feedback:")
    assert retry_prompt.count("Base task instructions:") == 1
    assert retry_prompt.count("Retry feedback:") == 1
    assert "Find the visible original ad slot area" in retry_prompt
    assert "Selected spec dimensions: 300×250" in retry_prompt
    assert spec.position_hint in retry_prompt
    assert "The source screenshot is 1024x768 pixels" in retry_prompt
    assert "Previous validation feedback for this retry:" in retry_prompt
    assert "validation_error=" in retry_prompt
    assert "reason_codes=out_of_bounds" in retry_prompt
    assert "previous_invalid_bbox=x=390,y=10,width=50,height=50" in retry_prompt
    assert "previous_invalid_center=x=415.0,y=35.0" in retry_prompt
    assert "previous_invalid_zone=top_right" in retry_prompt
    assert "Do NOT repeat the previous invalid bbox" in retry_prompt
    assert "Base task instructions remain authoritative" in retry_prompt


def test_tgo_banner_prompt_selects_full_visible_ad_card_container() -> None:
    spec = get_spec("yandex-tgo-600x600-text-image")
    assert spec is not None
    screenshot = SourceScreenshotInfo(path=Path("source.png"), mime_type="image/png", width=1280, height=828, size_bytes=123)

    prompt = _banner_prompt(screenshot=screenshot, spec=spec)

    assert "full visible ad card/container including image, text and CTA" in prompt
    assert "not only the internal image asset" in prompt
    assert "candidates array" in prompt
    assert "full_ad_card" in prompt
    assert "original_ad_slot" in prompt
    assert "internal_image_asset" in prompt
    assert "landing_area" in prompt
    assert "browser_ui" in prompt
    assert "unknown" in prompt
    assert "coordinate_space='normalized'" in prompt
    assert spec.position_hint in prompt


@pytest.mark.asyncio
async def test_ocr_request_response_text_logs_are_disabled_by_default(tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REPORTS_AD_OCR_LOG_TEXT", raising=False)
    monkeypatch.delenv("REPORTS_AD_OCR_LOG_TEXT_MAX_CHARS", raising=False)
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    screenshot = SourceScreenshotInfo(path=source_path, mime_type="image/png", width=400, height=300, size_bytes=source_path.stat().st_size)
    client = HeaderAuthGemmaClient(
        base_url="http://ocr.example.test/v1",
        model="test-model",
        authorization_header="Basic super-secret-token",
        max_retries=1,
    )

    async def fake_execute_request(**kwargs):
        assert "Authorization" not in kwargs
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"bbox":{"x":0.1,"y":0.2,"width":0.3,"height":0.4},"confidence":0.91,"coordinate_space":"normalized","reason":"Authorization: Bearer raw-secret Password=hidden"}'
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "authorization": "Bearer should-not-log",
        }

    monkeypatch.setattr(client, "_execute_request", fake_execute_request)

    with caplog.at_level("INFO", logger="reports_ad.gemma_client"):
        result = await client.detect_banner_bbox(screenshot=screenshot, spec=spec, request_id="req-logs")

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert result.bbox == BoundingBox(40, 60, 120, 120)
    assert "ocr_llm_request request_id=req-logs stage=banner_bbox" in log_text
    assert "ocr_llm_response request_id=req-logs stage=banner_bbox" in log_text
    assert "ocr_llm_raw_response request_id=req-logs stage=banner_bbox" in log_text
    assert "prompt_sha256=" in log_text
    assert "response_text_sha256=" in log_text
    assert "prompt_text=disabled" in log_text
    assert "response_text=disabled" in log_text
    assert "Basic super-secret-token" not in log_text
    assert "raw-secret" not in log_text
    assert "Password=hidden" not in log_text
    assert "should-not-log" not in log_text
    assert "data:image" not in log_text


@pytest.mark.asyncio
async def test_ocr_request_response_logs_include_full_text_when_enabled(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    screenshot = SourceScreenshotInfo(path=source_path, mime_type="image/png", width=400, height=300, size_bytes=source_path.stat().st_size)
    raw_response = '{"bbox":{"x":0.1,"y":0.2,"width":0.3,"height":0.4},"confidence":0.91,"coordinate_space":"normalized","reason":"full-text-response-marker"}'
    client = HeaderAuthGemmaClient(
        base_url="http://ocr.example.test/v1",
        model="test-model",
        authorization_header="Basic super-secret-token",
        max_retries=1,
        ocr_log_text=True,
        ocr_log_text_max_chars=0,
    )

    async def fake_execute_request(**kwargs):
        return {"choices": [{"message": {"content": raw_response}}], "usage": {"prompt_tokens": 10}}

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(client, "_execute_request", fake_execute_request)
        with caplog.at_level("INFO", logger="reports_ad.gemma_client"):
            await client.detect_banner_bbox(screenshot=screenshot, spec=spec, request_id="req-full-logs")
    finally:
        monkeypatch.undo()

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "prompt_text=disabled" not in log_text
    assert "response_text=disabled" not in log_text
    assert "truncated:" not in log_text
    assert "Base task instructions: You are analyzing a placement screenshot" in log_text
    assert "full-text-response-marker" in log_text
    assert raw_response in log_text
    assert "Basic super-secret-token" not in log_text
    assert "data:image" not in log_text


@pytest.mark.asyncio
async def test_landing_bbox_logs_full_prompt_and_response_when_enabled(tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    screenshot = SourceScreenshotInfo(path=source_path, mime_type="image/png", width=400, height=300, size_bytes=source_path.stat().st_size)
    raw_response = '{"bbox":null,"confidence":0,"coordinate_space":"normalized","reason":"landing-full-response-marker"}'
    client = HeaderAuthGemmaClient(
        base_url="http://ocr.example.test/v1",
        model="test-model",
        authorization_header=None,
        max_retries=1,
        ocr_log_text=True,
        ocr_log_text_max_chars=0,
    )

    async def fake_execute_request(**kwargs):
        return {"choices": [{"message": {"content": raw_response}}]}

    monkeypatch.setattr(client, "_execute_request", fake_execute_request)

    with caplog.at_level("INFO", logger="reports_ad.gemma_client"):
        await client.detect_landing_bbox(screenshot=screenshot, spec=spec, request_id="req-landing-logs")

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "ocr_llm_request request_id=req-landing-logs stage=landing_bbox" in log_text
    assert "Find the browser/link/landing area template location" in log_text
    assert "ocr_llm_raw_response request_id=req-landing-logs stage=landing_bbox" in log_text
    assert raw_response in log_text
    assert "landing-full-response-marker" in log_text
    assert "disabled" not in log_text
    assert "data:image" not in log_text


@pytest.mark.asyncio
async def test_composition_validation_logs_full_prompt_and_raw_response_when_enabled(tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    composed_path = tmp_path / "composed.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    Image.new("RGB", (400, 300), "white").save(composed_path)
    screenshot = SourceScreenshotInfo(path=source_path, mime_type="image/png", width=400, height=300, size_bytes=source_path.stat().st_size)
    raw_response = '{"verification_passed":true,"original_banner_bbox":{"x":20,"y":30,"width":100,"height":70},"actual_inserted_creative_bbox":{"x":20,"y":30,"width":100,"height":70},"deltas":{"x":0,"y":0,"width":0,"height":0},"corner_deltas":{"top_left":{"dx":0,"dy":0},"top_right":{"dx":0,"dy":0},"bottom_right":{"dx":0,"dy":0},"bottom_left":{"dx":0,"dy":0}},"recommended_corrected_bbox":null,"confidence":0.99,"reason":"composition-full-response-marker","coordinate_space":"source_pixels"}'
    client = HeaderAuthGemmaClient(
        base_url="http://ocr.example.test/v1",
        model="test-model",
        authorization_header=None,
        max_retries=1,
        ocr_log_text=True,
        ocr_log_text_max_chars=0,
    )

    async def fake_execute_multimodal_request(**kwargs):
        return {"choices": [{"message": {"content": raw_response}}]}

    monkeypatch.setattr(client, "_execute_multimodal_request", fake_execute_multimodal_request)

    with caplog.at_level("INFO", logger="reports_ad.gemma_client"):
        await client.verify_composition_alignment(
            original_screenshot=screenshot,
            composed_screenshot_path=composed_path,
            current_banner_bbox=BoundingBox(20, 30, 100, 70),
            spec=spec,
            request_id="req-composition-logs",
        )

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "ocr_llm_request request_id=req-composition-logs stage=composition_validation" in log_text
    assert "FULL original source screenshot" in log_text
    assert "corner_deltas" in log_text
    assert "ocr_llm_raw_response request_id=req-composition-logs stage=composition_validation" in log_text
    assert "ocr_llm_response request_id=req-composition-logs stage=composition_validation" in log_text
    assert raw_response in log_text
    assert "composition-full-response-marker" in log_text
    assert "prompt_text=disabled" not in log_text
    assert "response_text=disabled" not in log_text
    assert "data:image" not in log_text


@pytest.mark.asyncio
async def test_ocr_request_response_text_logs_can_be_bounded(tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    screenshot = SourceScreenshotInfo(path=source_path, mime_type="image/png", width=400, height=300, size_bytes=source_path.stat().st_size)
    raw_response = '{"bbox":{"x":0.1,"y":0.2,"width":0.3,"height":0.4},"confidence":0.91,"coordinate_space":"normalized","reason":"bounded-response-marker-with-long-tail"}'
    client = HeaderAuthGemmaClient(
        base_url="http://ocr.example.test/v1",
        model="test-model",
        authorization_header=None,
        max_retries=1,
        ocr_log_text=True,
        ocr_log_text_max_chars=80,
    )

    async def fake_execute_request(**kwargs):
        return {"choices": [{"message": {"content": raw_response}}]}

    monkeypatch.setattr(client, "_execute_request", fake_execute_request)

    with caplog.at_level("INFO", logger="reports_ad.gemma_client"):
        await client.detect_banner_bbox(screenshot=screenshot, spec=spec, request_id="req-bounded-logs")

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "prompt_text=Base task instructions: You are analyzing a placement screenshot for an advertis…[truncated:" in log_text
    assert "response_text={\"bbox\":{\"x\":0.1,\"y\":0.2,\"width\":0.3,\"height\":0.4},\"confidence\":0.91,\"coordinate…[truncated:" in log_text
    assert "bounded-response-marker-with-long-tail" not in log_text


@pytest.mark.asyncio
async def test_ocr_downscale_copy_is_used_and_resized_bbox_maps_to_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "large-source.png"
    Image.new("RGB", (2560, 1440), "white").save(source_path)
    screenshot = SourceScreenshotInfo(path=source_path, mime_type="image/png", width=2560, height=1440, size_bytes=source_path.stat().st_size)
    client = HeaderAuthGemmaClient(
        base_url="http://ocr.example.test/v1",
        model="test-model",
        authorization_header=None,
        max_retries=1,
        ocr_image_max_width=1280,
        ocr_image_max_pixels=2_000_000,
    )
    captured: dict[str, object] = {}

    async def fake_execute_request(**kwargs):
        image_path = kwargs["image_path"]
        captured["image_path"] = image_path
        captured["prompt"] = kwargs["prompt"]
        with Image.open(image_path) as sent:
            captured["sent_size"] = sent.size
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"bbox":{"x":100,"y":50,"width":300,"height":250},"confidence":0.9,"coordinate_space":"resized_pixels","image_width":1280,"image_height":720}'
                    }
                }
            ]
        }

    monkeypatch.setattr(client, "_execute_request", fake_execute_request)

    result = await client.detect_banner_bbox(screenshot=screenshot, spec=spec, request_id="req-downscale")

    assert captured["sent_size"] == (1280, 720)
    assert captured["image_path"] != source_path
    assert Path(captured["image_path"]).exists() is False
    assert "source screenshot is 2560x1440" in captured["prompt"]
    assert "OCR input image sent to you is 1280x720" in captured["prompt"]
    assert result.bbox == BoundingBox(200, 100, 600, 500)


@pytest.mark.asyncio
async def test_composition_validation_sends_full_screenshots_through_100_percent_crop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    composed_path = tmp_path / "composed.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    Image.new("RGB", (400, 300), "white").save(composed_path)
    screenshot = SourceScreenshotInfo(path=source_path, mime_type="image/png", width=400, height=300, size_bytes=source_path.stat().st_size)
    client = HeaderAuthGemmaClient(
        base_url="http://ocr.example.test/v1",
        model="test-model",
        authorization_header=None,
        max_retries=1,
        composition_validation_crop_margin_percent=0.20,
        composition_validation_crop_margin_min_pixels=60,
    )
    captured: dict[str, object] = {}

    async def fake_execute_multimodal_request(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        sizes = []
        paths = []
        for image_path, _mime_type in kwargs["images"]:
            paths.append(image_path)
            with Image.open(image_path) as sent:
                sizes.append(sent.size)
        captured["sent_sizes"] = sizes
        captured["sent_paths"] = paths
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"verification_passed":true,"original_banner_bbox":{"x":20,"y":30,"width":100,"height":70},"actual_inserted_creative_bbox":{"x":20,"y":30,"width":100,"height":70},"deltas":{"x":0,"y":0,"width":0,"height":0},"corner_deltas":{"top_left":{"dx":0,"dy":0},"top_right":{"dx":0,"dy":0},"bottom_right":{"dx":0,"dy":0},"bottom_left":{"dx":0,"dy":0}},"recommended_corrected_bbox":null,"confidence":0.99,"reason":"exact","coordinate_space":"crop_pixels","image_width":400,"image_height":300}'
                    }
                }
            ]
        }

    monkeypatch.setattr(client, "_execute_multimodal_request", fake_execute_multimodal_request)

    result = await client.verify_composition_alignment(
        original_screenshot=screenshot,
        composed_screenshot_path=composed_path,
        current_banner_bbox=BoundingBox(20, 30, 100, 70),
        spec=spec,
        request_id="req-validation-crop",
    )

    assert result.passed is True
    assert captured["sent_sizes"] == [(400, 300), (400, 300)]
    assert captured["sent_paths"] != [source_path, composed_path]
    assert all(Path(sent_path).exists() is False for sent_path in captured["sent_paths"])
    assert result.raw_metadata["crop_mode"] == "full_image_100_percent"
    assert result.raw_metadata["crop_origin_x"] == 0
    assert result.raw_metadata["crop_origin_y"] == 0
    assert result.raw_metadata["crop_width"] == 400
    assert result.raw_metadata["crop_height"] == 300
    assert "composition-validation crop/preparation pipeline is active" in captured["prompt"]
    assert "crop is 100% of the image" in captured["prompt"]
    assert "FULL original source screenshot" in captured["prompt"]
    assert "FULL composed screenshot" in captured["prompt"]
    assert "CROPPED images around the current banner candidate" not in captured["prompt"]
    assert "crop rectangle in full source pixels is x=0, y=0, width=400, height=300" in captured["prompt"]


def test_gemma_client_builder_rejects_missing_runtime_config() -> None:
    with pytest.raises(ValidationError, match="не инициализирована"):
        build_gemma_client_from_config(None)


def test_gemma_client_builder_requires_base_url() -> None:
    config = BotConfig(telegram_token="test", ocr_llm_base_url="", ocr_llm_model="cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit")

    with pytest.raises(ValidationError, match="OCR_LLM_BASE_URL"):
        build_gemma_client_from_config(config)


@pytest.mark.asyncio
async def test_stage2_generate_uses_mocked_gemma_and_fallback_landing(tmp_path: Path) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    Image.new("RGB", (300, 250), "blue").save(creative_path)
    request = ProofRequest(
        request_id="req-stage2",
        network_display_name="Яндекс",
        network_slug="yandex_graphic_banners",
        spec=spec,
        banner_type=spec.display_name,
        source_screenshot=SourceScreenshotInfo(path=source_path, mime_type="image/png", width=400, height=300, size_bytes=source_path.stat().st_size),
        creative=CreativeInfo(path=creative_path, mime_type="image/png", width=300, height=250, size_bytes=creative_path.stat().st_size),
        landing_url="https://example.com/landing",
        utm_params={"utm_source": "test"},
        placement=Placement(mode="template", value="screenshot-detected"),
        output_path=tmp_path / "final.png",
    )

    class MockGemma:
        def __init__(self) -> None:
            self.banner_calls = 0
            self.landing_calls = 0
            self.validation_calls = 0

        async def detect_banner_bbox(self, **kwargs):
            self.banner_calls += 1
            assert kwargs["selection_attempt"] == 1
            assert kwargs["validation_feedback"] is None
            return BBoxDetectionResult(bbox=BoundingBox(20, 30, 100, 70), confidence=0.9, source="mock")

        async def detect_landing_bbox(self, **kwargs):
            self.landing_calls += 1
            return BBoxDetectionResult(bbox=None, confidence=0.0, source="mock")

        async def verify_composition_alignment(self, **kwargs):
            self.validation_calls += 1
            return CompositionValidationResult(
                passed=True,
                original_banner_bbox=kwargs["current_banner_bbox"],
                inserted_creative_bbox=kwargs["current_banner_bbox"],
                corrected_bbox=kwargs["current_banner_bbox"],
                confidence=0.99,
                reason="exact match",
                source="mock",
            )

    client = MockGemma()
    result = await generate_stage2_proof(request, gemma_client=client)

    assert result.output_path.exists()
    assert result.banner_bbox == BoundingBox(20, 30, 100, 70)
    assert result.landing_fallback_used is True
    assert result.landing_bbox == fallback_landing_bbox(image_width=400, image_height=300)
    assert client.banner_calls == 1
    assert client.landing_calls == 1
    assert client.validation_calls == 1


@pytest.mark.asyncio
async def test_stage2_generate_corrects_composition_after_validation_offset(tmp_path: Path) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    Image.new("RGB", (300, 250), "blue").save(creative_path)
    request = ProofRequest(
        request_id="req-stage2-correct",
        network_display_name="Яндекс",
        network_slug="yandex_graphic_banners",
        spec=spec,
        banner_type=spec.display_name,
        source_screenshot=SourceScreenshotInfo(path=source_path, mime_type="image/png", width=400, height=300, size_bytes=source_path.stat().st_size),
        creative=CreativeInfo(path=creative_path, mime_type="image/png", width=300, height=250, size_bytes=creative_path.stat().st_size),
        landing_url="https://example.com/landing",
        utm_params={"utm_source": "test"},
        placement=Placement(mode="template", value="screenshot-detected"),
        output_path=tmp_path / "final.png",
    )

    class MockGemma:
        def __init__(self) -> None:
            self.validation_bboxes: list[BoundingBox] = []

        async def detect_banner_bbox(self, **kwargs):
            return BBoxDetectionResult(bbox=BoundingBox(20, 30, 100, 70), confidence=0.9, source="mock")

        async def detect_landing_bbox(self, **kwargs):
            return BBoxDetectionResult(bbox=None, confidence=0.0, source="mock")

        async def verify_composition_alignment(self, **kwargs):
            bbox = kwargs["current_banner_bbox"]
            self.validation_bboxes.append(bbox)
            if len(self.validation_bboxes) == 1:
                return CompositionValidationResult(
                    passed=False,
                    original_banner_bbox=BoundingBox(40, 50, 120, 80),
                    inserted_creative_bbox=bbox,
                    corrected_bbox=BoundingBox(40, 50, 120, 80),
                    deltas={"x": -20, "y": -20, "width": -20, "height": -10},
                    confidence=0.95,
                    reason="offset and size mismatch",
                    source="mock",
                )
            return CompositionValidationResult(
                passed=True,
                original_banner_bbox=bbox,
                inserted_creative_bbox=bbox,
                corrected_bbox=bbox,
                confidence=0.98,
                reason="exact match after correction",
                source="mock",
            )

    client = MockGemma()
    result = await generate_stage2_proof(request, gemma_client=client)

    assert result.banner_bbox == BoundingBox(40, 50, 120, 80)
    assert client.validation_bboxes == [BoundingBox(20, 30, 100, 70), BoundingBox(40, 50, 120, 80)]
    with Image.open(result.intermediate_screenshot_path) as result_image:
        assert result_image.getpixel((60, 60)) != (255, 255, 255)
        assert result_image.getpixel((45, 55)) == (255, 255, 255)
        assert result_image.getpixel((25, 35)) == (255, 255, 255)


@pytest.mark.asyncio
async def test_stage2_generate_corrects_composition_from_corner_deltas_when_corrected_bbox_unchanged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    Image.new("RGB", (300, 250), "blue").save(creative_path)
    request = ProofRequest(
        request_id="req-stage2-corner-deltas",
        network_display_name="Яндекс",
        network_slug="yandex_graphic_banners",
        spec=spec,
        banner_type=spec.display_name,
        source_screenshot=SourceScreenshotInfo(path=source_path, mime_type="image/png", width=400, height=300, size_bytes=source_path.stat().st_size),
        creative=CreativeInfo(path=creative_path, mime_type="image/png", width=300, height=250, size_bytes=creative_path.stat().st_size),
        landing_url="https://example.com/landing",
        utm_params={"utm_source": "test"},
        placement=Placement(mode="template", value="screenshot-detected"),
        output_path=tmp_path / "final.png",
    )

    class MockGemma:
        def __init__(self) -> None:
            self.validation_bboxes: list[BoundingBox] = []

        async def detect_banner_bbox(self, **kwargs):
            return BBoxDetectionResult(bbox=BoundingBox(20, 110, 100, 70), confidence=0.9, source="mock")

        async def detect_landing_bbox(self, **kwargs):
            return BBoxDetectionResult(bbox=None, confidence=0.0, source="mock")

        async def verify_composition_alignment(self, **kwargs):
            bbox = kwargs["current_banner_bbox"]
            self.validation_bboxes.append(bbox)
            if len(self.validation_bboxes) == 1:
                return CompositionValidationResult(
                    passed=False,
                    original_banner_bbox=BoundingBox(20, 30, 100, 70),
                    inserted_creative_bbox=bbox,
                    corrected_bbox=bbox,
                    deltas={"x": 0, "y": 80, "width": 0, "height": 0},
                    corner_deltas={
                        "top_left": {"dx": 0, "dy": -80},
                        "top_right": {"dx": 0, "dy": -80},
                        "bottom_right": {"dx": 0, "dy": -80},
                        "bottom_left": {"dx": 0, "dy": -80},
                    },
                    confidence=0.95,
                    reason="uniform vertical offset",
                    source="mock",
                )
            return CompositionValidationResult(
                passed=True,
                original_banner_bbox=bbox,
                inserted_creative_bbox=bbox,
                corrected_bbox=bbox,
                confidence=0.98,
                reason="exact match after corner correction",
                source="mock",
            )

    client = MockGemma()
    with caplog.at_level("INFO", logger="reports_ad.stage2_proof"):
        result = await generate_stage2_proof(request, gemma_client=client)

    assert result.banner_bbox == BoundingBox(20, 30, 100, 70)
    assert client.validation_bboxes == [BoundingBox(20, 110, 100, 70), BoundingBox(20, 30, 100, 70)]
    assert "correction_source=corner_deltas" in caplog.text


@pytest.mark.asyncio
async def test_stage2_generate_fails_after_default_unsuccessful_composition_validations(tmp_path: Path) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    Image.new("RGB", (300, 250), "blue").save(creative_path)
    request = ProofRequest(
        request_id="req-stage2-uncorrected",
        network_display_name="Яндекс",
        network_slug="yandex_graphic_banners",
        spec=spec,
        banner_type=spec.display_name,
        source_screenshot=SourceScreenshotInfo(path=source_path, mime_type="image/png", width=400, height=300, size_bytes=source_path.stat().st_size),
        creative=CreativeInfo(path=creative_path, mime_type="image/png", width=300, height=250, size_bytes=creative_path.stat().st_size),
        landing_url="https://example.com/landing",
        utm_params={"utm_source": "test"},
        placement=Placement(mode="template", value="screenshot-detected"),
        output_path=tmp_path / "final.png",
    )

    class MockGemma:
        def __init__(self) -> None:
            self.validation_calls = 0

        async def detect_banner_bbox(self, **kwargs):
            return BBoxDetectionResult(bbox=BoundingBox(20, 30, 100, 70), confidence=0.9, source="mock")

        async def detect_landing_bbox(self, **kwargs):
            return BBoxDetectionResult(bbox=None, confidence=0.0, source="mock")

        async def verify_composition_alignment(self, **kwargs):
            self.validation_calls += 1
            return CompositionValidationResult(
                passed=False,
                original_banner_bbox=BoundingBox(60, 50, 120, 80),
                inserted_creative_bbox=kwargs["current_banner_bbox"],
                corrected_bbox=BoundingBox(20 + self.validation_calls * 10, 30, 100, 70),
                deltas={"x": -10, "y": 0, "width": 0, "height": 0},
                confidence=0.9,
                reason=f"still offset {self.validation_calls}",
                source="mock",
            )

    client = MockGemma()
    with pytest.raises(ValidationError, match="точно совместить"):
        await generate_stage2_proof(request, gemma_client=client)

    assert client.validation_calls == 5
    diagnostics = list(Path("tmp").glob("stage2-req-stage2-uncorrected-*.composition-validation-failed.json"))
    if not diagnostics:
        diagnostics = list(Path("reports_ad/tmp").glob("stage2-req-stage2-uncorrected-*.composition-validation-failed.json"))
    assert diagnostics


@pytest.mark.asyncio
async def test_stage2_composition_validation_fails_when_all_correction_sources_unchanged_or_invalid(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    output_path = tmp_path / "final.png"
    artifact_base = tmp_path / "stage2-unchanged"
    source_copy_path = tmp_path / "source-copy.png"
    banner_replaced_path = tmp_path / "banner.png"
    landing_area_path = tmp_path / "landing.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    Image.new("RGB", (300, 250), "blue").save(creative_path)
    request = ProofRequest(
        request_id="req-stage2-unchanged-correction",
        network_display_name="Яндекс",
        network_slug="yandex_graphic_banners",
        spec=spec,
        banner_type=spec.display_name,
        source_screenshot=SourceScreenshotInfo(path=source_path, mime_type="image/png", width=400, height=300, size_bytes=source_path.stat().st_size),
        creative=CreativeInfo(path=creative_path, mime_type="image/png", width=300, height=250, size_bytes=creative_path.stat().st_size),
        landing_url="https://example.com/landing",
        utm_params={"utm_source": "test"},
        placement=Placement(mode="template", value="screenshot-detected"),
        output_path=output_path,
    )

    class MockGemma:
        def __init__(self) -> None:
            self.validation_bboxes: list[BoundingBox] = []

        async def verify_composition_alignment(self, **kwargs):
            bbox = kwargs["current_banner_bbox"]
            self.validation_bboxes.append(bbox)
            return CompositionValidationResult(
                passed=False,
                original_banner_bbox=bbox,
                inserted_creative_bbox=bbox,
                corrected_bbox=bbox,
                deltas={"x": 0, "y": 0, "width": 0, "height": 0},
                corner_deltas={
                    "top_left": {"dx": 0, "dy": 0},
                    "top_right": {"dx": 0, "dy": 0},
                    "bottom_right": {"dx": 0, "dy": 0},
                    "bottom_left": {"dx": 0, "dy": 0},
                },
                confidence=0.8,
                reason="no usable correction",
                source="mock",
            )

    client = MockGemma()
    with caplog.at_level("INFO", logger="reports_ad.stage2_proof"), pytest.raises(ValidationError, match="точно совместить"):
        await compose_stage2_image_with_validation(
            client=client,
            request=request,
            effective_click_url="https://example.com/landing?utm_source=test",
            initial_banner_bbox=BoundingBox(20, 30, 100, 70),
            landing_bbox=fallback_landing_bbox(image_width=400, image_height=300),
            output_path=output_path,
            artifact_base=artifact_base,
            source_copy_path=source_copy_path,
            banner_replaced_path=banner_replaced_path,
            landing_area_path=landing_area_path,
            max_attempts=3,
        )

    assert client.validation_bboxes == [BoundingBox(20, 30, 100, 70)]
    assert "rejection=unchanged_bbox" in caplog.text
    assert (tmp_path / "stage2-unchanged.composition-validation-failed.json").exists()


@pytest.mark.asyncio
async def test_stage2_generate_handles_invalid_composition_validation_response(tmp_path: Path) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    Image.new("RGB", (300, 250), "blue").save(creative_path)
    request = ProofRequest(
        request_id="req-stage2-invalid-validation",
        network_display_name="Яндекс",
        network_slug="yandex_graphic_banners",
        spec=spec,
        banner_type=spec.display_name,
        source_screenshot=SourceScreenshotInfo(path=source_path, mime_type="image/png", width=400, height=300, size_bytes=source_path.stat().st_size),
        creative=CreativeInfo(path=creative_path, mime_type="image/png", width=300, height=250, size_bytes=creative_path.stat().st_size),
        landing_url="https://example.com/landing",
        utm_params={"utm_source": "test"},
        placement=Placement(mode="template", value="screenshot-detected"),
        output_path=tmp_path / "final.png",
    )

    class MockGemma:
        async def detect_banner_bbox(self, **kwargs):
            return BBoxDetectionResult(bbox=BoundingBox(20, 30, 100, 70), confidence=0.9, source="mock")

        async def detect_landing_bbox(self, **kwargs):
            return BBoxDetectionResult(bbox=None, confidence=0.0, source="mock")

        async def verify_composition_alignment(self, **kwargs):
            raise GemmaClientError("invalid JSON")

    with pytest.raises(ValidationError, match="точно совместить"):
        await generate_stage2_proof(request, gemma_client=MockGemma())


@pytest.mark.asyncio
async def test_build_application_injects_runtime_config_into_masked_banner_generator(tmp_path: Path) -> None:
    size = get_banner_size("240x400")
    assert size is not None
    creative_path = tmp_path / "creative.png"
    output_path = tmp_path / "final.png"
    Image.new("RGB", (240, 400), "blue").save(creative_path)
    config = BotConfig(
        telegram_token="test",
        temp_root=tmp_path,
        ocr_llm_base_url="http://gemma.firstdata.io/v1",
        ocr_llm_model="cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit",
    )
    request = MaskedBannerRequest(
        request_id="req-mask-config",
        network_display_name="Mediasniper",
        network_slug="mediasniper",
        size_slug=size.slug,
        size=size,
        creative=CreativeInfo(path=creative_path, mime_type="image/png", width=240, height=400, size_bytes=creative_path.stat().st_size),
        output_path=output_path,
    )
    captured: dict[str, object] = {}

    async def fake_masked_banner_generator(mask_request: MaskedBannerRequest, *, config: BotConfig | None = None):
        captured["request"] = mask_request
        captured["config"] = config
        output_path.write_bytes(b"fake-png")
        return MaskedBannerResult(
            output_path=output_path,
            width=240,
            height=400,
            request_id=mask_request.request_id,
            mask_asset=resolve_mask_asset(mask_request.network_slug, mask_request.size_slug),
        )

    application = build_application(config, banner_generator=fake_masked_banner_generator)
    configured_generator = application.bot_data["banner_generator"]

    await configured_generator(request)

    assert captured["request"] == request
    assert captured["config"] is config


@pytest.mark.asyncio
async def test_stage2_generate_invalid_banner_bbox_asks_for_better_screenshot(tmp_path: Path) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    Image.new("RGB", (300, 250), "blue").save(creative_path)
    request = ProofRequest(
        request_id="req-stage2",
        network_display_name="Яндекс",
        network_slug="yandex_graphic_banners",
        spec=spec,
        banner_type=spec.display_name,
        source_screenshot=SourceScreenshotInfo(path=source_path, mime_type="image/png", width=400, height=300, size_bytes=source_path.stat().st_size),
        creative=CreativeInfo(path=creative_path, mime_type="image/png", width=300, height=250, size_bytes=creative_path.stat().st_size),
        landing_url="https://example.com/landing",
        utm_params={"utm_source": "test"},
        placement=Placement(mode="template", value="screenshot-detected"),
        output_path=tmp_path / "final.png",
    )

    class MockGemma:
        async def detect_banner_bbox(self, **kwargs):
            return BBoxDetectionResult(bbox=BoundingBox(390, 10, 50, 50), confidence=0.9, source="mock")

        async def detect_landing_bbox(self, **kwargs):
            return BBoxDetectionResult(bbox=None, confidence=0.0, source="mock")

    try:
        await generate_stage2_proof(request, gemma_client=MockGemma())
    except ValidationError as exc:
        assert "после 5 попыток" in exc.safe_message
        assert "Последняя ошибка" in exc.safe_message
    else:
        raise AssertionError("Expected ValidationError")


@pytest.mark.asyncio
async def test_stage2_generate_retries_with_validation_feedback_then_uses_valid_bbox(tmp_path: Path) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    output_path = tmp_path / "final.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    Image.new("RGB", (300, 250), "blue").save(creative_path)
    original_source_bytes = source_path.read_bytes()
    request = ProofRequest(
        request_id="req-stage2-retry",
        network_display_name="Яндекс",
        network_slug="yandex_graphic_banners",
        spec=spec,
        banner_type=spec.display_name,
        source_screenshot=SourceScreenshotInfo(path=source_path, mime_type="image/png", width=400, height=300, size_bytes=source_path.stat().st_size),
        creative=CreativeInfo(path=creative_path, mime_type="image/png", width=300, height=250, size_bytes=creative_path.stat().st_size),
        landing_url="https://example.com/landing",
        utm_params={"utm_source": "test"},
        placement=Placement(mode="template", value="screenshot-detected"),
        output_path=output_path,
    )

    class MockGemma:
        def __init__(self) -> None:
            self.banner_calls: list[dict[str, object]] = []

        async def detect_banner_bbox(self, **kwargs):
            self.banner_calls.append(kwargs)
            assert source_path.read_bytes() == original_source_bytes
            if len(self.banner_calls) == 1:
                assert output_path.exists() is False
                return BBoxDetectionResult(bbox=BoundingBox(390, 10, 50, 50), confidence=0.9, source="mock")
            return BBoxDetectionResult(bbox=BoundingBox(20, 30, 100, 70), confidence=0.9, source="mock")

        async def detect_landing_bbox(self, **kwargs):
            return BBoxDetectionResult(bbox=None, confidence=0.0, source="mock")

    client = MockGemma()
    result = await generate_stage2_proof(request, gemma_client=client)

    assert result.output_path.exists()
    assert result.banner_bbox == BoundingBox(20, 30, 100, 70)
    assert source_path.read_bytes() == original_source_bytes
    assert len(client.banner_calls) == 2
    assert client.banner_calls[0]["selection_attempt"] == 1
    assert client.banner_calls[0]["validation_feedback"] is None
    assert client.banner_calls[1]["selection_attempt"] == 2
    retry_feedback = str(client.banner_calls[1]["validation_feedback"])
    assert "out_of_bounds" in retry_feedback
    assert "previous_invalid_bbox=x=390,y=10,width=50,height=50" in retry_feedback
    assert "previous_invalid_center=x=415.0,y=35.0" in retry_feedback
    assert "previous_invalid_zone=top_right" in retry_feedback
    assert "do_not_repeat_previous_invalid_bbox=true" in retry_feedback
    with Image.open(output_path) as result_image:
        assert result_image.getpixel((45, 55)) != (255, 255, 255)
        assert result_image.getpixel((390, 10)) == (255, 255, 255)


@pytest.mark.asyncio
async def test_stage2_generate_exhausts_five_invalid_bbox_attempts_without_composition(tmp_path: Path) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    output_path = tmp_path / "final.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    Image.new("RGB", (300, 250), "blue").save(creative_path)
    original_source_bytes = source_path.read_bytes()
    request = ProofRequest(
        request_id="req-stage2-exhaust",
        network_display_name="Яндекс",
        network_slug="yandex_graphic_banners",
        spec=spec,
        banner_type=spec.display_name,
        source_screenshot=SourceScreenshotInfo(path=source_path, mime_type="image/png", width=400, height=300, size_bytes=source_path.stat().st_size),
        creative=CreativeInfo(path=creative_path, mime_type="image/png", width=300, height=250, size_bytes=creative_path.stat().st_size),
        landing_url="https://example.com/landing",
        utm_params={"utm_source": "test"},
        placement=Placement(mode="template", value="screenshot-detected"),
        output_path=output_path,
    )

    class MockGemma:
        def __init__(self) -> None:
            self.banner_calls: list[dict[str, object]] = []
            self.landing_calls = 0

        async def detect_banner_bbox(self, **kwargs):
            self.banner_calls.append(kwargs)
            assert source_path.read_bytes() == original_source_bytes
            return BBoxDetectionResult(bbox=BoundingBox(390, 10, 50, 50), confidence=0.9, source="mock")

        async def detect_landing_bbox(self, **kwargs):
            self.landing_calls += 1
            return BBoxDetectionResult(bbox=None, confidence=0.0, source="mock")

    client = MockGemma()
    with pytest.raises(ValidationError) as exc_info:
        await generate_stage2_proof(request, gemma_client=client)

    assert "после 5 попыток" in exc_info.value.safe_message
    assert "Последняя ошибка" in exc_info.value.safe_message
    assert "out_of_bounds" in exc_info.value.safe_message
    assert len(client.banner_calls) == 5
    assert [call["selection_attempt"] for call in client.banner_calls] == [1, 2, 3, 4, 5]
    assert client.banner_calls[0]["validation_feedback"] is None
    assert all(call["validation_feedback"] for call in client.banner_calls[1:])
    assert client.landing_calls == 0
    assert output_path.exists() is False
    assert source_path.read_bytes() == original_source_bytes


@pytest.mark.asyncio
async def test_stage2_generate_requires_ocr_config_when_client_not_injected(tmp_path: Path) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    Image.new("RGB", (300, 250), "blue").save(creative_path)
    request = ProofRequest(
        request_id="req-stage2-misconfig",
        network_display_name="Яндекс",
        network_slug="yandex_graphic_banners",
        spec=spec,
        banner_type=spec.display_name,
        source_screenshot=SourceScreenshotInfo(path=source_path, mime_type="image/png", width=400, height=300, size_bytes=source_path.stat().st_size),
        creative=CreativeInfo(path=creative_path, mime_type="image/png", width=300, height=250, size_bytes=creative_path.stat().st_size),
        landing_url="https://example.com/landing",
        utm_params={"utm_source": "test"},
        placement=Placement(mode="template", value="screenshot-detected"),
        output_path=tmp_path / "final.png",
    )
    config = BotConfig(telegram_token="test", ocr_llm_base_url="")

    with pytest.raises(ValidationError, match="OCR_LLM_BASE_URL"):
        await generate_stage2_proof(request, config=config)
