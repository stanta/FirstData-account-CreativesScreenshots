from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .mask_catalog import BannerSize, MaskAsset, resolve_mask_asset
from .models import CreativeInfo
from .proof import ScreenshotGenerationError
from .validation import ValidationError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MaskedBannerRequest:
    request_id: str
    network_slug: str
    network_display_name: str
    size_slug: str
    size: BannerSize
    creative: CreativeInfo
    output_path: Path


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


def compose_masked_banner(*, creative_path: Path, mask_path: Path, output_path: Path, width: int, height: int) -> tuple[int, int]:
    if width <= 0 or height <= 0:
        raise ValidationError("Размер баннера должен быть положительным.")
    try:
        with Image.open(creative_path) as creative_raw, Image.open(mask_path) as mask_raw:
            creative = creative_raw.convert("RGBA").resize((width, height), Image.Resampling.LANCZOS)
            mask = _prepare_mask_overlay(mask_raw.convert("RGBA"))
            if mask.size != (width, height):
                raise ValidationError("Размер файла маски не совпадает с выбранным размером баннера.")
            result = Image.alpha_composite(creative, mask)
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
                pixels[x, y] = (red, green, blue, 0)
    return prepared
