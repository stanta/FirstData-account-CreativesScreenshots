"""Deterministic rendering for the VK card mockup (reference profile v1).

This is a *mockup*, not evidence that an advertisement was actually published.
All assets come from local, versioned mask files; there are no HTTP requests.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFont, UnidentifiedImageError

from .models import BoundingBox, CreativeInfo
from .validation import ValidationError, validate_minimum_age

_RESOURCE_DIR = "data/masks/VK"
_FALLBACK_CTA_COLOR = (55, 18, 155)
# Format describes the *uploaded creative*, not the final card width.
# 600x600 is displayed square, fitted to a 1080x1080 slot in the 1080 px card.
_VK_LAYOUT_FILES: dict[str, tuple[str, int]] = {
    "1080x607": ("layout-v2.json", 607),
    "600x600": ("layout-square-v2.json", 1080),
}


@dataclass(frozen=True, slots=True)
class VkCardContent:
    logo: CreativeInfo
    header_text: str
    body_text: str
    cta_id: str


@dataclass(frozen=True, slots=True)
class VkCardLayout:
    version: str
    width: int
    header_height: int
    creative_height: int
    cta_height: int
    min_footer_height: int
    max_height: int
    body_padding_x: int
    body_padding_y: int
    body_font_size: int
    body_line_gap: int
    header_font_size: int
    logo_size: int
    logo_x: int
    logo_y: int
    reference_width_px: float
    header_gap_reference_px: int
    header_content_bottom: int

    @property
    def creative_bbox(self) -> BoundingBox:
        return BoundingBox(0, self.header_height, self.width, self.creative_height)

    @property
    def cta_bbox(self) -> BoundingBox:
        return BoundingBox(0, self.header_height + self.creative_height, self.width, self.cta_height)

    @property
    def overlay_height(self) -> int:
        return self.header_height + self.creative_height + self.cta_height


@dataclass(frozen=True, slots=True)
class VkRenderResult:
    output_path: Path
    width: int
    height: int
    boxes: dict[str, BoundingBox]
    cta_rgb: tuple[int, int, int]


def _read_resource_json(file_name: str) -> dict[str, Any]:
    item = files("reports_ad").joinpath(_RESOURCE_DIR, file_name)
    return json.loads(item.read_text(encoding="utf-8"))


def load_vk_layout(size_slug: str = "1080x607") -> VkCardLayout:
    variant = _VK_LAYOUT_FILES.get(size_slug)
    if variant is None:
        raise ValidationError("Неизвестный размер карточки ВК.")
    filename, creative_height = variant
    raw = _read_resource_json(filename)
    layout = VkCardLayout(**raw)
    if layout.width != 1080 or layout.creative_height != creative_height:
        raise ValidationError("Некорректная геометрия профиля карточки ВК.")
    if layout.reference_width_px <= 0 or layout.header_gap_reference_px < 0:
        raise ValidationError("Некорректный эталонный отступ карточки ВК.")
    expected_gap = round(layout.header_gap_reference_px * layout.width / layout.reference_width_px)
    if (layout.header_height != layout.header_content_bottom + expected_gap
            or layout.logo_y + layout.logo_size > layout.header_content_bottom):
        raise ValidationError("Некорректная высота шапки карточки ВК.")
    return layout


def _mask_for_vk_layout(mask: Image.Image, profile: VkCardLayout) -> Image.Image:
    """Move the identical platform header and CTA mask to a new creative height.

    The versioned v2 PNG is 1080x848 and has a transparent creative region.
    Moving the lower overlay slice preserves both platform-only elements and
    leaves the square creative untouched. No resampling of the mask or input.
    """
    original = load_vk_layout("1080x607")
    if mask.size != (original.width, original.overlay_height):
        raise ValidationError("Размер файла маски ВК не совпадает с профилем v1.")
    if (
        profile.width != original.width
        or profile.header_height != original.header_height
        or profile.cta_height != original.cta_height
    ):
        raise ValidationError("Некорректная геометрия маски ВК.")

    if profile.creative_height == original.creative_height:
        return mask

    expanded = Image.new("RGBA", (profile.width, profile.overlay_height), (0, 0, 0, 0))
    expanded.alpha_composite(
        mask.crop((0, 0, original.width, original.header_height)),
        dest=(0, 0),
    )
    expanded.alpha_composite(
        mask.crop((0, original.cta_bbox.y, original.width, original.overlay_height)),
        dest=(0, profile.cta_bbox.y),
    )
    return expanded


def list_vk_ctas() -> dict[str, str]:
    data = _read_resource_json("cta-options-v1.json")
    return {str(key): str(value) for key, value in data.items()}


def validate_vk_card_content(content: VkCardContent, minimum_age: str | None) -> str:
    age = validate_minimum_age(minimum_age or "")
    if not isinstance(content.logo, CreativeInfo) or not content.logo.path.is_file():
        raise ValidationError("Для карточки ВК загрузите логотип PNG/JPEG.")
    validate_vk_header(content.header_text)
    validate_vk_body(content.body_text)
    if content.cta_id not in list_vk_ctas():
        raise ValidationError("Выберите надпись кнопки из предложенного списка.")
    return age


def validate_vk_header(text: str) -> str:
    value = text.strip()
    if not value or len(value) > 200 or "\n" in value or "\r" in value:
        raise ValidationError("Верхний текст должен быть одной непустой строкой до 200 символов.")
    return value


def validate_vk_body(text: str) -> str:
    value = text.strip()
    if not value or len(value) > 2000:
        raise ValidationError("Основной текст должен содержать от 1 до 2000 символов.")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in value):
        raise ValidationError("В основном тексте обнаружены недопустимые управляющие символы.")
    return value


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    candidates = (
        f"/usr/share/fonts/truetype/dejavu/{name}",
        f"/usr/local/share/fonts/{name}",
        name,
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    raise ValidationError("Не найден шрифт DejaVuSans для рендеринга карточки ВК.")


def _width(draw: ImageDraw.ImageDraw, value: str, font: ImageFont.FreeTypeFont) -> int:
    return math.ceil(draw.textlength(value, font=font))


def _wrap_text(draw: ImageDraw.ImageDraw, value: str, *, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    """Wrap text without discarding explicit blank lines or clipping long words."""
    lines: list[str] = []
    for paragraph in value.replace("\r\n", "\n").split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        current = ""
        for word in paragraph.split():
            proposal = f"{current} {word}" if current else word
            if _width(draw, proposal, font) <= width:
                current = proposal
                continue
            if current:
                lines.append(current)
                current = ""
            # Split one exceptionally long token along code points.
            for char in word:
                if current and _width(draw, current + char, font) > width:
                    lines.append(current)
                    current = char
                else:
                    current += char
        if current:
            lines.append(current)
    return lines


def choose_vk_cta_color(creative: Image.Image) -> tuple[int, int, int]:
    """Repeatable, conservative approximation of a platform-style accent color."""
    sample = creative.convert("RGB").resize((64, 36), Image.Resampling.BOX)
    counts: dict[tuple[int, int, int], int] = {}
    pixels = sample.get_flattened_data() if hasattr(sample, "get_flattened_data") else sample.getdata()
    for r, g, b in pixels:
        if max(r, g, b) - min(r, g, b) < 35 or max(r, g, b) > 245 or max(r, g, b) < 45:
            continue
        cell = (r // 32, g // 32, b // 32)
        counts[cell] = counts.get(cell, 0) + 1
    if not counts or max(counts.values()) < 20:
        return _FALLBACK_CTA_COLOR
    cell = sorted(counts, key=lambda key: (-counts[key], key))[0]
    rgb = tuple(min(255, component * 32 + 16) for component in cell)
    # White label must be readable (WCAG 4.5:1).
    while _white_contrast(rgb) < 4.5:
        rgb = tuple(int(channel * 0.87) for channel in rgb)
    return rgb


def _white_contrast(rgb: tuple[int, int, int]) -> float:
    def linear(n: int) -> float:
        s = n / 255.0
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4

    luminance = 0.2126 * linear(rgb[0]) + 0.7152 * linear(rgb[1]) + 0.0722 * linear(rgb[2])
    return 1.05 / (luminance + 0.05)


def render_vk_card(
    *,
    creative_path: Path,
    content: VkCardContent,
    minimum_age: str,
    output_path: Path,
    layout: VkCardLayout | None = None,
    mask_path: Path | None = None,
    size_slug: str = "1080x607",
) -> VkRenderResult:
    age = validate_vk_card_content(content, minimum_age)
    header = validate_vk_header(content.header_text)
    body = validate_vk_body(content.body_text)
    variant = _VK_LAYOUT_FILES.get(size_slug)
    if variant is None:
        raise ValidationError("Неизвестный размер карточки ВК.")
    profile = layout or load_vk_layout(size_slug)
    if profile.width != 1080 or profile.creative_height != variant[1]:
        raise ValidationError("Некорректная геометрия профиля карточки ВК.")
    overlay_path = mask_path or Path(str(files("reports_ad").joinpath(_RESOURCE_DIR, "card-v2.png")))
    try:
        with Image.open(creative_path) as source, Image.open(content.logo.path) as raw_logo, Image.open(overlay_path) as raw_mask:
            creative = source.convert("RGBA")
            logo = raw_logo.convert("RGBA")
            mask = raw_mask.convert("RGBA")
    except (OSError, UnidentifiedImageError) as exc:
        raise ValidationError("Невозможно прочитать креатив, логотип или маску ВК.") from exc
    mask = _mask_for_vk_layout(mask, profile)
    body_font = _font(profile.body_font_size)
    header_font = _font(profile.header_font_size, bold=True)
    subtitle_font = _font(22)
    cta_font = _font(28)
    measuring = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    lines = _wrap_text(measuring, body, font=body_font, width=profile.width - 2 * profile.body_padding_x)
    line_height = profile.body_font_size + profile.body_line_gap
    footer_height = max(profile.min_footer_height, profile.body_padding_y * 2 + line_height * len(lines))
    total_height = profile.overlay_height + footer_height
    if total_height > profile.max_height:
        raise ValidationError("Основной текст слишком длинный для карточки. Сократите его.")
    if _width(measuring, header, header_font) > profile.width - 190:
        raise ValidationError("Верхний текст не помещается в шапку без обрезания. Сократите его.")

    canvas = Image.new("RGBA", (profile.width, total_height), (255, 255, 255, 255))
    slot = profile.creative_bbox
    # Stretch to full VK slot. No cropping or renderer-created white margins.
    # The user-uploaded source remains unchanged.
    fitted_size = (slot.width, slot.height)
    fitted = creative.resize(fitted_size, Image.Resampling.LANCZOS)
    canvas.alpha_composite(fitted, dest=(slot.x, slot.y))
    canvas.alpha_composite(mask, dest=(0, 0))
    draw = ImageDraw.Draw(canvas)
    lx, ly, diameter = profile.logo_x, profile.logo_y, profile.logo_size
    draw.ellipse((lx, ly, lx + diameter - 1, ly + diameter - 1), fill="white", outline="#e8e8eb", width=2)
    logo_scale = min((diameter - 12) / logo.width, (diameter - 12) / logo.height)
    logo_fit = logo.resize((max(1, round(logo.width * logo_scale)), max(1, round(logo.height * logo_scale))), Image.Resampling.LANCZOS)
    alpha_circle = Image.new("L", logo_fit.size, 0)
    ImageDraw.Draw(alpha_circle).ellipse((0, 0, logo_fit.width - 1, logo_fit.height - 1), fill=255)
    combined_alpha = ImageChops.multiply(logo_fit.getchannel("A"), alpha_circle)
    logo_fit.putalpha(combined_alpha)
    canvas.alpha_composite(logo_fit, dest=(lx + (diameter - logo_fit.width) // 2, ly + (diameter - logo_fit.height) // 2))

    header_x, header_y = lx + diameter + 24, 43
    subtitle = f"Реклама · {age}+"
    # Match the VK reference: the second line starts immediately after the
    # visible title. A fixed baseline such as y=94 adds a large, variable gap.
    title_bounds = draw.textbbox((header_x, header_y), header, font=header_font)
    subtitle_local_bounds = draw.textbbox((0, 0), subtitle, font=subtitle_font)
    subtitle_y = title_bounds[3] - subtitle_local_bounds[1]
    if subtitle_y + subtitle_local_bounds[3] > profile.header_content_bottom:
        raise ValidationError("Текст шапки ВК не помещается без пересечения с отступом.")
    draw.text((header_x, header_y), header, font=header_font, fill=(28, 28, 32))
    draw.text((header_x, subtitle_y), subtitle, font=subtitle_font, fill=(135, 135, 140))

    cta_box = profile.cta_bbox
    color = choose_vk_cta_color(creative)
    draw.rectangle((0, cta_box.y, profile.width - 1, cta_box.bottom - 1), fill=color)
    draw.text((profile.body_padding_x, cta_box.y + 25), list_vk_ctas()[content.cta_id], font=cta_font, fill="white")
    ax, ay = profile.width - 34, cta_box.y + cta_box.height // 2
    draw.line([(ax - 11, ay - 12), (ax + 1, ay), (ax - 11, ay + 12)], fill="white", width=4, joint="curve")

    for idx, line in enumerate(lines):
        if line:
            draw.text((profile.body_padding_x, profile.overlay_height + profile.body_padding_y + idx * line_height), line, font=body_font, fill=(22, 22, 24))
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(output_path, format="PNG", optimize=True)
    with Image.open(output_path) as check:
        if check.size != (profile.width, total_height) or check.format != "PNG":
            raise ValidationError("Не удалось проверить итоговую карточку ВК.")
    boxes = {
        "header": BoundingBox(0, 0, profile.width, profile.header_height),
        "creative": slot,
        "creative_fitted": BoundingBox(slot.x, slot.y, *fitted_size),
        "cta": cta_box,
        "body": BoundingBox(0, profile.overlay_height, profile.width, footer_height),
        "logo": BoundingBox(lx, ly, diameter, diameter),
    }
    return VkRenderResult(output_path=output_path, width=profile.width, height=total_height, boxes=boxes, cta_rgb=color)
