"""VK renderer unit tests: no Telegram API, browser or internet required."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from PIL import Image

from reports_ad.mask_catalog import get_mask_network, get_banner_size_for_network, resolve_mask_asset
from reports_ad.masked_banner import MaskedBannerRequest, generate_masked_banner
from reports_ad.models import CreativeInfo
from reports_ad.validation import ValidationError
from reports_ad.vk_card import (
    VkCardContent, choose_vk_cta_color, list_vk_ctas,
    render_vk_card, validate_vk_card_content,
)


def source(tmp_path: Path, name: str, dimensions: tuple[int, int], color: tuple[int, int, int]) -> CreativeInfo:
    path = tmp_path / name
    Image.new("RGB", dimensions, color).save(path, format="PNG")
    return CreativeInfo(path=path, mime_type="image/png", width=dimensions[0], height=dimensions[1], size_bytes=path.stat().st_size)


def content(tmp_path: Path, *, body: str = "Вопрос?\n\nОписание нового формата с заявками.") -> tuple[CreativeInfo, VkCardContent]:
    creative = source(tmp_path, "creative.png", (1080, 607), (30, 21, 133))
    logo = source(tmp_path, "logo.png", (256, 256), (33, 180, 170))
    return creative, VkCardContent(logo, "Больше продаж за те же деньги", body, "learn_more")


def test_vk_registered_in_catalog() -> None:
    network = get_mask_network("vk")
    assert network is not None and network.flow_kind == "vk_card"
    assert get_mask_network("вк") == network
    size = get_banner_size_for_network("vk", "1080x607")
    assert size is not None and size.width == 1080 and size.height == 607
    asset = resolve_mask_asset("vk", "1080x607")
    assert asset.size.expected_mask_size == (1080, 848)
    assert asset.path.is_file()


def test_vk_square_format_registered_and_uses_shared_platform_mask() -> None:
    from reports_ad.mask_catalog import list_banner_sizes_for_network, validate_banner_size_slug_for_network
    sizes = list_banner_sizes_for_network("vk")
    assert [item.slug for item in sizes] == ["1080x607", "600x600"]
    square = get_banner_size_for_network("vk", "600×600")
    assert square is not None
    assert (square.width, square.height) == (600, 600)
    assert validate_banner_size_slug_for_network("600x600", "vk") == "600x600"
    asset = resolve_mask_asset("vk", "600x600")
    assert asset.size.expected_mask_size == (1080, 848)
    assert asset.path.name == "card-v2.png"


def test_vk_mask_transparency() -> None:
    asset = resolve_mask_asset("vk", "1080x607")
    with Image.open(asset.path) as raw:
        overlay = raw.convert("RGBA")
        assert overlay.size == (1080, 848)
        assert overlay.getpixel((200, 40))[3] == 255
        assert overlay.getpixel((400, 400))[3] == 0
        assert overlay.getpixel((400, 800))[3] == 0


def test_vk_full_render_preserves_inputs_and_layout(tmp_path: Path) -> None:
    creative, item = content(tmp_path)
    hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in [creative.path, item.logo.path]}
    result = render_vk_card(creative_path=creative.path, content=item, minimum_age="18", output_path=tmp_path / "out.png")
    assert result.width == 1080 and result.height >= 1081
    assert result.boxes["creative"].height == 607 and result.boxes["creative"].y == 152
    assert result.boxes["creative_fitted"] == result.boxes["creative"]
    assert result.boxes["creative"].y - (39 + 82) == 31
    assert result.boxes["cta"].y == 759 and result.boxes["body"].y == 848
    assert result.boxes["cta"].y == result.boxes["creative"].bottom
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in hashes} == hashes
    with Image.open(result.output_path) as image:
        assert image.format == "PNG"
        assert image.getpixel((400, 400)) == (30, 21, 133)
        assert image.getpixel((100, 1060)) == (255, 255, 255)


def test_vk_square_creative_stretched_without_letterboxing(tmp_path: Path) -> None:
    _, item = content(tmp_path)
    square = source(tmp_path, "square.png", (300, 300), (14, 88, 255))
    result = render_vk_card(creative_path=square.path, content=item, minimum_age="0", output_path=tmp_path / "out.png")
    box = result.boxes["creative_fitted"]
    assert box == result.boxes["creative"]
    assert (box.width, box.height, box.x, box.y) == (1080, 607, 0, 152)
    with Image.open(result.output_path) as image:
        assert image.getpixel((10, 400)) == (14, 88, 255)


def test_vk_square_600_creative_renders_full_width_without_cropping(tmp_path: Path) -> None:
    _, item = content(tmp_path)
    square = source(tmp_path, "square600.png", (600, 600), (13, 91, 217))
    from reports_ad.vk_card import load_vk_layout
    profile = load_vk_layout("600x600")
    assert profile.width == 1080 and profile.creative_height == 1080
    result = render_vk_card(
        creative_path=square.path, content=item, minimum_age="18",
        output_path=tmp_path / "square-out.png", size_slug="600x600",
    )
    assert (result.width, result.height) == (1080, 1554)
    assert result.boxes["creative"] == result.boxes["creative_fitted"]
    assert result.boxes["creative"].y == 152
    assert result.boxes["cta"].y == 1232
    assert result.boxes["body"].y == 1321
    with Image.open(result.output_path) as image:
        assert image.size == (1080, 1554)
        assert image.getpixel((530, 700)) == (13, 91, 217)
    assert square.path.is_file()


def test_vk_square_format_stretches_landscape_input(tmp_path: Path) -> None:
    creative, item = content(tmp_path)
    result = render_vk_card(
        creative_path=creative.path, content=item, minimum_age="18",
        output_path=tmp_path / "landscape-in-square.png", size_slug="600x600",
    )
    box = result.boxes["creative_fitted"]
    assert (box.width, box.height) == (1080, 1080)
    assert box.x == 0
    assert box.y == 152
    with Image.open(result.output_path) as image:
        assert image.getpixel((500, box.y + 50)) == (30, 21, 133)
        assert image.getpixel((500, 175)) == (30, 21, 133)


def test_vk_square_600_profile_rejects_unrecognized_format(tmp_path: Path) -> None:
    creative, item = content(tmp_path)
    with pytest.raises(ValidationError):
        render_vk_card(
            creative_path=creative.path, content=item, minimum_age="18",
            output_path=tmp_path / "unknown.png", size_slug="42x42",
        )


def test_vk_long_body_expands_bottom_not_mask(tmp_path: Path) -> None:
    creative, item = content(tmp_path, body="Строка с подробным описанием. " * 40)
    result = render_vk_card(creative_path=creative.path, content=item, minimum_age="18", output_path=tmp_path / "out.png")
    assert 1081 < result.height <= 4096


def test_vk_header_does_not_clip(tmp_path: Path) -> None:
    creative, item = content(tmp_path)
    long = VkCardContent(item.logo, "Очень длинный заголовок " * 7, item.body_text, item.cta_id)
    with pytest.raises(ValidationError, match="не помещается"):
        render_vk_card(creative_path=creative.path, content=long, minimum_age="18", output_path=tmp_path / "out.png")


def test_vk_contract_rejects_unknown_cta_and_unapproved_age(tmp_path: Path) -> None:
    _, item = content(tmp_path)
    assert list_vk_ctas() == {"learn_more": "Подробнее"}
    with pytest.raises(ValidationError):
        validate_vk_card_content(VkCardContent(item.logo, item.header_text, item.body_text, "buy_now"), "18")
    with pytest.raises(ValidationError):
        validate_vk_card_content(item, "18+")


def test_vk_color_is_repeatable_and_has_fallback() -> None:
    image = Image.new("RGB", (1080, 607), (244, 233, 50))
    color = choose_vk_cta_color(image)
    assert color == choose_vk_cta_color(image)
    assert max(color) < 245
    assert choose_vk_cta_color(Image.new("RGB", (80, 80), (188, 188, 188))) == (55, 18, 155)


@pytest.mark.asyncio
async def test_vk_routes_through_existing_mask_generator(tmp_path: Path) -> None:
    creative, item = content(tmp_path)
    size = get_banner_size_for_network("vk", "1080x607")
    assert size is not None
    request = MaskedBannerRequest(
        request_id="vk-test", network_slug="vk", network_display_name="ВК", size_slug="1080x607",
        size=size, creative=creative, output_path=tmp_path / "generated.png",
        minimum_age="18", vk_card=item,
    )
    result = await generate_masked_banner(request)
    assert result.width == 1080 and result.height >= 1081
    with Image.open(result.output_path) as image:
        assert image.format == "PNG"


@pytest.mark.asyncio
async def test_vk_square_routes_through_existing_mask_generator(tmp_path: Path) -> None:
    _, item = content(tmp_path)
    square = source(tmp_path, "square600.png", (600, 600), (18, 55, 180))
    size = get_banner_size_for_network("vk", "600x600")
    assert size is not None
    request = MaskedBannerRequest(
        request_id="vk-square", network_slug="vk", network_display_name="ВК",
        size_slug="600x600", size=size, creative=square,
        output_path=tmp_path / "square-generated.png",
        minimum_age="18", vk_card=item,
    )
    result = await generate_masked_banner(request)
    assert result.width == 1080 and result.height == 1554
    with Image.open(result.output_path) as image:
        assert image.getpixel((500, 700)) == (18, 55, 180)


@pytest.mark.asyncio
async def test_vk_missing_content_rejected_server_side(tmp_path: Path) -> None:
    creative, _ = content(tmp_path)
    size = get_banner_size_for_network("vk", "1080x607")
    assert size is not None
    request = MaskedBannerRequest(
        request_id="vk-no-content", network_slug="vk", network_display_name="ВК",
        size_slug="1080x607", size=size, creative=creative, output_path=tmp_path / "bad.png",
    )
    with pytest.raises(ValidationError):
        await generate_masked_banner(request)
