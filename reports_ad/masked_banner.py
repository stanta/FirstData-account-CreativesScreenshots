from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont, UnidentifiedImageError

from .mask_catalog import BannerSize, MaskAsset, resolve_mask_asset
from .models import CreativeInfo
from .proof import ScreenshotGenerationError
from .validation import ValidationError

logger = logging.getLogger(__name__)
OPAQUE_MASK_BACKGROUND_ALPHA = 80
YANDEX_VIDEO_NETWORK_SLUG = "yandex_video"
YANDEX_VIDEO_SIZE_SLUG = "1920x1080"
YANDEX_VIDEO_MASK_SIZE = (1536, 1024)
YANDEX_VIDEO_TARGET_SITE_BBOX = (68, 794, 360, 56)
YANDEX_VIDEO_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)


@dataclass(frozen=True, slots=True)
class MaskedBannerRequest:
    request_id: str
    network_slug: str
    network_display_name: str
    size_slug: str
    size: BannerSize
    creative: CreativeInfo
    output_path: Path
    target_site_url: str | None = None
    target_site_display: str | None = None


@dataclass(frozen=True, slots=True)
class MaskedBannerResult:
    output_path: Path
    width: int
    height: int
    request_id: str
    mask_asset: MaskAsset


async def generate_masked_banner(request: MaskedBannerRequest) -> MaskedBannerResult:
    """Resize a user creative to the selected banner size and overlay the matching PNG mask."""

    started = time.monotonic()
    _validate_masked_banner_request(request)
    mask_asset = resolve_mask_asset(request.network_slug, request.size_slug)
    output_path = Path(request.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info(
        "masked_banner_generation_started request_id=%s network=%s size=%s creative=%sx%s mask=%s",
        request.request_id,
        request.network_slug,
        request.size_slug,
        request.creative.width,
        request.creative.height,
        mask_asset.path.name,
    )

    try:
        width, height = compose_masked_banner(
            creative_path=request.creative.path,
            mask_path=mask_asset.path,
            output_path=output_path,
            width=request.size.width,
            height=request.size.height,
            network_slug=request.network_slug,
            target_site_display=request.target_site_display,
        )
    except ValidationError:
        raise
    except Exception as exc:
        logger.exception("masked_banner_generation_failed request_id=%s exception_class=%s", request.request_id, type(exc).__name__)
        raise ScreenshotGenerationError("Не удалось наложить маску на креатив.") from exc

    logger.info(
        "masked_banner_generation_done request_id=%s network=%s size=%s elapsed_ms=%s output=%sx%s",
        request.request_id,
        request.network_slug,
        request.size_slug,
        int((time.monotonic() - started) * 1000),
        width,
        height,
    )
    return MaskedBannerResult(output_path=output_path, width=width, height=height, request_id=request.request_id, mask_asset=mask_asset)


def compose_masked_banner(
    *,
    creative_path: Path,
    mask_path: Path,
    output_path: Path,
    width: int,
    height: int,
    network_slug: str | None = None,
    target_site_display: str | None = None,
) -> tuple[int, int]:
    if width <= 0 or height <= 0:
        raise ValidationError("Размер баннера должен быть положительным.")
    try:
        with Image.open(creative_path) as creative_raw, Image.open(mask_path) as mask_raw:
            creative = creative_raw.convert("RGBA").resize((width, height), Image.Resampling.LANCZOS)
            if network_slug == YANDEX_VIDEO_NETWORK_SLUG:
                if not target_site_display:
                    raise ValidationError("Для Яндекс Видео нужно указать адрес целевого сайта.")
                creative = render_yandex_video_target_site(creative, target_site_display)
            mask = _prepare_mask_overlay(mask_raw.convert("RGBA"))
            if mask.size != (width, height):
                raise ValidationError("Размер файла маски не совпадает с выбранным размером баннера.")
            pre_composite = creative.copy()
            result = Image.alpha_composite(creative, mask)
            _log_composition_diagnostics(
                network_slug=network_slug,
                size=(width, height),
                mask=mask,
                pre_composite=pre_composite,
                result=result,
            )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            result.save(output_path, format="PNG", optimize=True)
    except (UnidentifiedImageError, OSError) as exc:
        raise ValidationError("Креатив или маска не являются корректными PNG/JPEG-изображениями.") from exc

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise ScreenshotGenerationError("Итоговый баннер не был создан.")
    with Image.open(output_path) as check_img:
        if check_img.size != (width, height):
            raise ScreenshotGenerationError("Итоговый баннер имеет неправильный размер.")
    return width, height


def _log_composition_diagnostics(*, network_slug: str | None, size: tuple[int, int], mask: Image.Image, pre_composite: Image.Image, result: Image.Image) -> None:
    alpha = mask.getchannel("A")
    changed_bbox = ImageChops.difference(pre_composite.convert("RGB"), result.convert("RGB")).getbbox()
    target_region_changed = None
    if network_slug == YANDEX_VIDEO_NETWORK_SLUG:
        x, y, width, height = _scale_yandex_video_bbox(size)
        before_crop = pre_composite.crop((x, y, x + width, y + height)).convert("RGB")
        after_crop = result.crop((x, y, x + width, y + height)).convert("RGB")
        target_region_changed = ImageChops.difference(before_crop, after_crop).getbbox() is not None
    logger.info(
        "masked_banner_composition_diagnostics network=%s output_size=%sx%s mask_size=%sx%s mask_alpha_extrema=%s mask_alpha_bbox=%s changed_bbox=%s target_region_changed=%s",
        network_slug or "unknown",
        size[0],
        size[1],
        mask.width,
        mask.height,
        alpha.getextrema(),
        alpha.getbbox(),
        changed_bbox,
        target_region_changed,
    )


def render_yandex_video_target_site(base: Image.Image, display_text: str) -> Image.Image:
    text = (display_text or "").strip()
    if not text:
        raise ValidationError("Адрес целевого сайта не должен быть пустым.")
    result = base.copy().convert("RGBA")
    width, height = result.size
    scale_x = width / YANDEX_VIDEO_MASK_SIZE[0]
    scale_y = height / YANDEX_VIDEO_MASK_SIZE[1]
    x, y, box_width, box_height = YANDEX_VIDEO_TARGET_SITE_BBOX
    box = _scale_yandex_video_bbox((width, height))
    draw = ImageDraw.Draw(result)
    font = _fit_text_font(text=text, box_width=box[2], box_height=box[3])
    text_bbox = draw.textbbox((0, 0), text, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]
    text_x = box[0]
    text_y = box[1] + max(0, (box[3] - text_height) // 2) - text_bbox[1]
    draw.text((text_x, text_y), text, fill=(255, 255, 255, 255), font=font)
    if text_width > box[2]:
        logger.debug("yandex_video_target_site_text_overflow text_length=%s box_width=%s rendered_width=%s", len(text), box[2], text_width)
    return result


def _scale_yandex_video_bbox(size: tuple[int, int]) -> tuple[int, int, int, int]:
    width, height = size
    scale_x = width / YANDEX_VIDEO_MASK_SIZE[0]
    scale_y = height / YANDEX_VIDEO_MASK_SIZE[1]
    x, y, box_width, box_height = YANDEX_VIDEO_TARGET_SITE_BBOX
    return (
        int(round(x * scale_x)),
        int(round(y * scale_y)),
        int(round(box_width * scale_x)),
        int(round(box_height * scale_y)),
    )


def _fit_text_font(*, text: str, box_width: int, box_height: int) -> ImageFont.ImageFont:
    max_size = max(12, min(34, box_height - 8))
    min_size = 12
    for size in range(max_size, min_size - 1, -2):
        font = _load_font(size)
        bbox = ImageDraw.Draw(Image.new("RGBA", (1, 1))).textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= box_width and bbox[3] - bbox[1] <= box_height:
            return font
    return _load_font(min_size)


def _load_font(size: int) -> ImageFont.ImageFont:
    for candidate in YANDEX_VIDEO_FONT_CANDIDATES:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def _validate_masked_banner_request(request: MaskedBannerRequest) -> None:
    if request.network_slug == YANDEX_VIDEO_NETWORK_SLUG:
        if request.size_slug != YANDEX_VIDEO_SIZE_SLUG:
            raise ValidationError("Яндекс Видео поддерживает только формат 1920×1080.")
        if not request.target_site_display:
            raise ValidationError("Для Яндекс Видео нужно указать адрес целевого сайта.")


def _prepare_mask_overlay(mask: Image.Image) -> Image.Image:
    alpha = mask.getchannel("A")
    if alpha.getextrema() != (255, 255):
        return mask

    prepared = mask.copy()
    pixels = prepared.load()
    for y in range(prepared.height):
        for x in range(prepared.width):
            red, green, blue, current_alpha = pixels[x, y]
            if current_alpha == 255 and min(red, green, blue) >= 235 and max(red, green, blue) - min(red, green, blue) <= 12:
                pixels[x, y] = (red, green, blue, OPAQUE_MASK_BACKGROUND_ALPHA)
    return prepared
