from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .gemma_client import GemmaBBoxClient, build_gemma_client_from_config
from .models import BoundingBox, GeneratedProofArtifacts, ProofRequest, ProofResult
from .proof import ScreenshotGenerationError
from .validation import ValidationError, merge_url_with_utm, validate_bbox_within_image

logger = logging.getLogger(__name__)


async def generate_stage2_proof(request: ProofRequest, *, gemma_client: GemmaBBoxClient | None = None, config: object | None = None) -> ProofResult:
    """Generate a proof image from a user-uploaded screenshot without browser loading."""

    started = time.monotonic()
    timings: dict[str, int] = {}
    client = gemma_client or build_gemma_client_from_config(config)  # type: ignore[arg-type]
    output_path = Path(request.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    effective_click_url = merge_url_with_utm(request.landing_url, request.utm_params, allow_local=request.allow_local_urls)
    artifact_base = _artifact_base(request)
    source_copy_path = artifact_base.with_suffix(".source.png")
    banner_replaced_path = artifact_base.with_suffix(".banner.png")
    landing_area_path = artifact_base.with_suffix(".landing-area.png")

    logger.info(
        "stage2_generation_started request_id=%s network=%s spec=%s screenshot=%sx%s creative=%sx%s",
        request.request_id,
        request.network_slug,
        request.spec.slug,
        request.source_screenshot.width,
        request.source_screenshot.height,
        request.creative.width,
        request.creative.height,
    )

    try:
        banner_detection = await client.detect_banner_bbox(screenshot=request.source_screenshot, spec=request.spec, request_id=request.request_id)
        timings["banner_bbox_detected"] = _elapsed_ms(started)
        banner_bbox = validate_bbox_within_image(
            banner_detection.bbox,
            image_width=request.source_screenshot.width,
            image_height=request.source_screenshot.height,
            label="область баннера",
        )

        landing_detection = await client.detect_landing_bbox(screenshot=request.source_screenshot, spec=request.spec, request_id=request.request_id)
        timings["landing_bbox_detected"] = _elapsed_ms(started)
        landing_bbox = _valid_or_fallback_landing_bbox(
            landing_detection.bbox,
            image_width=request.source_screenshot.width,
            image_height=request.source_screenshot.height,
        )
        landing_fallback_used = landing_detection.bbox is None or landing_detection.bbox != landing_bbox

        width, height = compose_stage2_image(
            source_screenshot_path=request.source_screenshot.path,
            creative_path=request.creative.path,
            effective_click_url=effective_click_url,
            banner_bbox=banner_bbox,
            landing_bbox=landing_bbox,
            output_path=output_path,
            source_copy_path=source_copy_path,
            banner_replaced_path=banner_replaced_path,
            landing_area_path=landing_area_path,
        )
        timings["image_composed"] = _elapsed_ms(started)
        logger.info(
            "stage2_generation_done request_id=%s elapsed_ms=%s banner_bbox=%s landing_bbox=%s landing_fallback_used=%s",
            request.request_id,
            _elapsed_ms(started),
            _safe_bbox(banner_bbox),
            _safe_bbox(landing_bbox),
            landing_fallback_used,
        )
        artifacts = GeneratedProofArtifacts(
            source_copy_path=source_copy_path,
            banner_replaced_path=banner_replaced_path,
            landing_area_path=landing_area_path,
            final_path=output_path,
        )
        return ProofResult(
            output_path=output_path,
            width=width,
            height=height,
            effective_click_url=effective_click_url,
            request_id=request.request_id,
            intermediate_screenshot_path=banner_replaced_path,
            landing_fallback_used=landing_fallback_used,
            ad_page_fallback_used=False,
            fallback_media_type="screenshot-only",
            resolved_ad_page_host="uploaded-screenshot",
            banner_bbox=banner_bbox,
            landing_bbox=landing_bbox,
            artifacts=artifacts,
            timings_ms=timings,
        )
    except ValidationError:
        raise
    except ScreenshotGenerationError:
        raise
    except Exception as exc:
        logger.exception("stage2_generation_failed request_id=%s elapsed_ms=%s exception_class=%s", request.request_id, _elapsed_ms(started), type(exc).__name__)
        raise ScreenshotGenerationError("Не удалось сформировать proof-скриншот из загруженного скриншота.") from exc


def compose_stage2_image(
    *,
    source_screenshot_path: Path,
    creative_path: Path,
    effective_click_url: str,
    banner_bbox: BoundingBox,
    landing_bbox: BoundingBox,
    output_path: Path,
    source_copy_path: Path | None = None,
    banner_replaced_path: Path | None = None,
    landing_area_path: Path | None = None,
) -> tuple[int, int]:
    with Image.open(source_screenshot_path) as source_raw, Image.open(creative_path) as creative_raw:
        source = source_raw.convert("RGBA")
        if source_copy_path:
            source_copy_path.parent.mkdir(parents=True, exist_ok=True)
            source.convert("RGB").save(source_copy_path, format="PNG", optimize=True)

        _validate_bbox_for_compose(banner_bbox, image=source, label="область баннера")
        _validate_bbox_for_compose(landing_bbox, image=source, label="landing/link area")

        result = source.copy()
        creative = creative_raw.convert("RGBA").resize((banner_bbox.width, banner_bbox.height), Image.Resampling.LANCZOS)
        background_crop = result.crop((banner_bbox.x, banner_bbox.y, banner_bbox.right, banner_bbox.bottom))
        if "A" in creative.getbands():
            blended = Image.alpha_composite(background_crop, creative)
        else:
            blended = creative
        result.paste(blended, (banner_bbox.x, banner_bbox.y))
        _draw_ad_mark(result, banner_bbox)

        if banner_replaced_path:
            banner_replaced_path.parent.mkdir(parents=True, exist_ok=True)
            result.convert("RGB").save(banner_replaced_path, format="PNG", optimize=True)

        _draw_landing_area(result, landing_bbox, effective_click_url)
        if landing_area_path:
            landing_area_path.parent.mkdir(parents=True, exist_ok=True)
            result.crop((landing_bbox.x, landing_bbox.y, landing_bbox.right, landing_bbox.bottom)).convert("RGB").save(landing_area_path, format="PNG", optimize=True)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        result.convert("RGB").save(output_path, format="PNG", optimize=True)
        width, height = result.size

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise ScreenshotGenerationError("Итоговое изображение не было создано.")
    with Image.open(output_path) as check_img:
        if check_img.size != (width, height):
            raise ScreenshotGenerationError("Итоговое изображение имеет неправильный размер.")
    return width, height


def fallback_landing_bbox(*, image_width: int, image_height: int, ratio: float = 0.16) -> BoundingBox:
    ratio = min(max(ratio, 0.10), 0.20)
    height = max(80, int(image_height * ratio))
    height = min(height, image_height)
    return BoundingBox(x=0, y=image_height - height, width=image_width, height=height)


def _valid_or_fallback_landing_bbox(bbox: BoundingBox | None, *, image_width: int, image_height: int) -> BoundingBox:
    if bbox is None:
        return fallback_landing_bbox(image_width=image_width, image_height=image_height)
    try:
        return validate_bbox_within_image(bbox, image_width=image_width, image_height=image_height, label="landing/link area")
    except ValidationError:
        return fallback_landing_bbox(image_width=image_width, image_height=image_height)


def _draw_ad_mark(image: Image.Image, bbox: BoundingBox) -> None:
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    label = "Реклама"
    text_bbox = draw.textbbox((0, 0), label, font=font)
    padding_x = 6
    padding_y = 4
    width = text_bbox[2] - text_bbox[0] + padding_x * 2
    height = text_bbox[3] - text_bbox[1] + padding_y * 2
    x = bbox.x + max(4, min(12, bbox.width // 20))
    y = bbox.y + max(4, min(12, bbox.height // 20))
    draw.rounded_rectangle((x, y, x + width, y + height), radius=6, fill=(255, 255, 255, 220), outline=(170, 170, 170, 230))
    draw.text((x + padding_x, y + padding_y), label, fill=(45, 45, 45, 255), font=font)


def _draw_landing_area(image: Image.Image, bbox: BoundingBox, effective_click_url: str) -> None:
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    draw.rectangle((bbox.x, bbox.y, bbox.right, bbox.bottom), fill=(244, 246, 248, 255))
    draw.line((bbox.x, bbox.y, bbox.right, bbox.y), fill=(190, 190, 190, 255), width=2)
    toolbar_height = max(42, min(68, bbox.height // 3))
    toolbar_bottom = bbox.y + toolbar_height
    draw.rectangle((bbox.x, bbox.y, bbox.right, toolbar_bottom), fill=(231, 234, 238, 255))
    for idx, color in enumerate(((234, 82, 82, 255), (246, 190, 64, 255), (82, 180, 91, 255))):
        cx = bbox.x + 18 + idx * 18
        cy = bbox.y + toolbar_height // 2
        draw.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), fill=color)
    address_x = bbox.x + 80
    address_y = bbox.y + 10
    address_h = max(24, toolbar_height - 20)
    address_right = bbox.right - 18
    draw.rounded_rectangle((address_x, address_y, address_right, address_y + address_h), radius=10, fill=(255, 255, 255, 255), outline=(205, 210, 216, 255))
    safe_url = _truncate_middle(effective_click_url, max(24, (address_right - address_x) // 7))
    draw.text((address_x + 12, address_y + max(4, (address_h - 12) // 2)), safe_url, fill=(32, 70, 120, 255), font=font)
    caption = "Целевая ссылка с UTM-метками (сайт не открывался)"
    draw.text((bbox.x + 18, toolbar_bottom + 14), caption, fill=(60, 60, 60, 255), font=font)


def _validate_bbox_for_compose(bbox: BoundingBox, *, image: Image.Image, label: str) -> None:
    if bbox.x < 0 or bbox.y < 0 or bbox.width <= 0 or bbox.height <= 0 or bbox.right > image.width or bbox.bottom > image.height:
        raise ScreenshotGenerationError(f"Некорректная {label}: координаты выходят за границы изображения.")


def _artifact_base(request: ProofRequest) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    stem = _safe_filename_part(request.creative.path.stem or "creative")
    return _project_tmp_dir() / f"stage2-{request.request_id}-{stem}-{timestamp}"


def _project_tmp_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "tmp"


def _safe_filename_part(value: str, *, max_length: int = 80) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    normalized = normalized.strip("._-")
    return (normalized or "artifact")[:max_length]


def _safe_bbox(bbox: BoundingBox) -> str:
    return f"x={bbox.x},y={bbox.y},width={bbox.width},height={bbox.height}"


def _truncate_middle(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 8:
        return value[:limit]
    left = (limit - 1) // 2
    right = limit - left - 1
    return f"{value[:left]}…{value[-right:]}"


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
