"""Pixel and geometry regressions for the VK v2 layout."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from reports_ad.mask_catalog import resolve_mask_asset
from reports_ad.models import CreativeInfo
from reports_ad.vk_card import VkCardContent, load_vk_layout, render_vk_card


def _make_creative(path: Path, size: tuple[int, int], color=(18, 51, 160)) -> CreativeInfo:
    Image.new("RGB", size, color).save(path)
    return CreativeInfo(path=path, mime_type="image/png", width=size[0], height=size[1],
                        size_bytes=path.stat().st_size)


def _render(tmp_path: Path, slug: str, source: Path):
    logo = _make_creative(tmp_path / "logo.png", (64, 64))
    content = VkCardContent(logo, "Тестовый заголовок", "Основной текст", "learn_more")
    return render_vk_card(creative_path=source, content=content, minimum_age="18",
                          output_path=tmp_path / "result.png", size_slug=slug)


@pytest.mark.parametrize(("slug", "slot_height"), [("1080x607", 607), ("600x600", 1080)])
def test_vk_v2_reference_geometry_and_mask(slug: str, slot_height: int) -> None:
    profile = load_vk_layout(slug)
    assert profile.width == 1080
    assert profile.header_content_bottom == 121
    assert profile.header_height == 152
    assert profile.header_gap_reference_px == 16
    assert profile.reference_width_px == 551.02
    assert profile.creative_bbox.y - profile.header_content_bottom == 31
    assert profile.creative_bbox.height == slot_height
    assert profile.cta_bbox.y == profile.creative_bbox.bottom
    assert profile.overlay_height == profile.cta_bbox.bottom
    mask = resolve_mask_asset("vk", slug)
    assert mask.size.expected_mask_size == (1080, 848)
    assert mask.path.name == "card-v2.png"
    with Image.open(mask.path) as original:
        rgba = original.convert("RGBA")
        assert rgba.size == (1080, 848)
        assert rgba.crop((0, 152, 1080, 848)).getchannel("A").getextrema() == (0, 0)


@pytest.mark.parametrize(
    ("slug", "source_size", "slot_height"),
    [
        ("1080x607", (600, 600), 607),
        ("1080x607", (300, 700), 607),
        ("600x600", (1080, 607), 1080),
        ("600x600", (300, 700), 1080),
    ],
)
def test_v2_stretches_without_cropping_or_letterboxing(
    tmp_path: Path, slug: str, source_size: tuple[int, int], slot_height: int,
) -> None:
    source = tmp_path / "creative.png"
    im = Image.new("RGB", source_size)
    draw = ImageDraw.Draw(im)
    w, h = source_size
    draw.rectangle((0, 0, w // 2 - 1, h // 2 - 1), fill=(231, 17, 45))
    draw.rectangle((w // 2, 0, w - 1, h // 2 - 1), fill=(49, 73, 213))
    draw.rectangle((0, h // 2, w // 2 - 1, h - 1), fill=(31, 189, 65))
    draw.rectangle((w // 2, h // 2, w - 1, h - 1), fill=(211, 177, 28))
    im.save(source)
    input_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    result = _render(tmp_path, slug, source)
    assert result.boxes["creative_fitted"] == result.boxes["creative"]
    assert result.boxes["creative"].y == 152
    assert result.boxes["creative"].height == slot_height
    assert result.boxes["cta"].y == result.boxes["creative"].bottom
    assert hashlib.sha256(source.read_bytes()).hexdigest() == input_hash
    with Image.open(result.output_path) as output:
        assert output.getpixel((3, 155)) == (231, 17, 45)
        assert output.getpixel((1076, 155)) == (49, 73, 213)
        assert output.getpixel((3, 152 + slot_height - 3)) == (31, 189, 65)
        assert output.getpixel((1076, 152 + slot_height - 3)) == (211, 177, 28)
        assert output.getpixel((3, 152 + slot_height)) == result.cta_rgb


@pytest.mark.parametrize("slug", ["1080x607", "600x600"])
@pytest.mark.parametrize(
    ("headline", "age"), [
        ("Тест заголовка", "18"),
        ("Больше продаж за те же деньги", "0"),
        ("Новая шапка объявления", "99"),
    ],
)
def test_vk_header_subtitle_touches_title_ink_without_margin(
    tmp_path: Path, slug: str, headline: str, age: str,
) -> None:
    """The age line must start immediately after the title's visible bbox."""
    from reports_ad.vk_card import _font

    logo = _make_creative(tmp_path / "header-logo.png", (64, 64))
    creative = _make_creative(tmp_path / "header-creative.png", (600, 600))
    content = VkCardContent(logo, headline, "Основной текст", "learn_more")
    out = render_vk_card(
        creative_path=creative.path, content=content, minimum_age=age,
        output_path=tmp_path / "header-join.png", size_slug=slug,
    )
    header_x = load_vk_layout(slug).logo_x + load_vk_layout(slug).logo_size + 24
    title_font = _font(32, bold=True)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    title_bottom = probe.textbbox((header_x, 43), headline, font=title_font)[3]
    assert title_bottom < load_vk_layout(slug).header_content_bottom
    with Image.open(out.output_path) as image:
        # After the title's bottom boundary the next printed pixels must be
        # the muted age text, not a white band. Search first three rows.
        age_pixels = [
            image.getpixel((x, y))
            for y in range(title_bottom, title_bottom + 3)
            for x in range(header_x, header_x + 240)
        ]
    assert any(100 <= r <= 175 and 100 <= g <= 175 and 100 <= b <= 180
               for r, g, b in age_pixels)
