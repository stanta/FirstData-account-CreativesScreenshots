from __future__ import annotations

import logging
import json
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import BotConfig
from .gemma_client import GemmaBBoxClient, GemmaClientError, build_gemma_client_from_config
from .models import BoundingBox, CompositionValidationResult, CreativeSpec, GeneratedProofArtifacts, ProofRequest, ProofResult
from .proof import ScreenshotGenerationError
from .validation import ValidationError, merge_url_with_utm, validate_bbox_within_image

logger = logging.getLogger(__name__)
DEFAULT_BANNER_ASPECT_RATIO_DEVIATION_THRESHOLD = 0.35
MIN_BANNER_AREA_RATIO = 0.002
MIN_BANNER_DIMENSION_PIXELS = 16
MIN_BANNER_SPEC_DIMENSION_RATIO = 0.20
RIGHT_COLUMN_MIN_CENTER_X_RATIO = 2 / 3
LEFT_COLUMN_MAX_CENTER_X_RATIO = 1 / 3
TOP_SLOT_MAX_CENTER_Y_RATIO = 1 / 3
BOTTOM_SLOT_MIN_CENTER_Y_RATIO = 2 / 3
MAX_BANNER_SELECTION_ATTEMPTS = 5
MAX_COMPOSITION_VALIDATION_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class _BannerValidationFeedback:
    message: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _CompositionCorrection:
    bbox: BoundingBox
    source: str


async def generate_stage2_proof(request: ProofRequest, *, gemma_client: GemmaBBoxClient | None = None, config: BotConfig | None = None) -> ProofResult:
    """Generate a proof image from a user-uploaded screenshot without browser loading."""

    started = time.monotonic()
    timings: dict[str, int] = {}
    client = gemma_client or build_gemma_client_from_config(config)
    output_path = Path(request.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    effective_click_url = merge_url_with_utm(request.landing_url, request.utm_params, allow_local=request.allow_local_urls)
    artifact_base = _artifact_base(request)
    source_copy_path = artifact_base.with_suffix(".source.png")
    banner_replaced_path = artifact_base.with_suffix(".banner.png")
    landing_area_path = artifact_base.with_suffix(".landing-area.png")

    logger.info(
        "stage2_generation_started request_id=%s network=%s spec=%s screenshot=%sx%s creative=%sx%s placement_hint_present=%s",
        request.request_id,
        request.network_slug,
        request.spec.slug,
        request.source_screenshot.width,
        request.source_screenshot.height,
        request.creative.width,
        request.creative.height,
        request.spec.position_hint,
    )

    try:
        banner_bbox = await select_valid_banner_bbox_with_feedback(client=client, request=request)
        timings["banner_bbox_detected"] = _elapsed_ms(started)

        landing_detection = await client.detect_landing_bbox(screenshot=request.source_screenshot, spec=request.spec, request_id=request.request_id)
        timings["landing_bbox_detected"] = _elapsed_ms(started)
        landing_bbox = _valid_or_fallback_landing_bbox(
            landing_detection.bbox,
            image_width=request.source_screenshot.width,
            image_height=request.source_screenshot.height,
        )
        landing_fallback_used = landing_detection.bbox is None or landing_detection.bbox != landing_bbox

        width, height, final_banner_bbox, final_banner_replaced_path = await compose_stage2_image_with_validation(
            client=client,
            request=request,
            effective_click_url=effective_click_url,
            initial_banner_bbox=banner_bbox,
            landing_bbox=landing_bbox,
            output_path=output_path,
            artifact_base=artifact_base,
            source_copy_path=source_copy_path,
            banner_replaced_path=banner_replaced_path,
            landing_area_path=landing_area_path,
        )
        timings["image_composed"] = _elapsed_ms(started)
        logger.info(
            "stage2_generation_done request_id=%s elapsed_ms=%s banner_bbox=%s landing_bbox=%s landing_fallback_used=%s",
            request.request_id,
            _elapsed_ms(started),
            _safe_bbox(final_banner_bbox),
            _safe_bbox(landing_bbox),
            landing_fallback_used,
        )
        artifacts = GeneratedProofArtifacts(
            source_copy_path=source_copy_path,
            banner_replaced_path=final_banner_replaced_path,
            landing_area_path=landing_area_path,
            final_path=output_path,
        )
        return ProofResult(
            output_path=output_path,
            width=width,
            height=height,
            effective_click_url=effective_click_url,
            request_id=request.request_id,
            intermediate_screenshot_path=final_banner_replaced_path,
            landing_fallback_used=landing_fallback_used,
            ad_page_fallback_used=False,
            fallback_media_type="screenshot-only",
            resolved_ad_page_host="uploaded-screenshot",
            banner_bbox=final_banner_bbox,
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


async def compose_stage2_image_with_validation(
    *,
    client: GemmaBBoxClient,
    request: ProofRequest,
    effective_click_url: str,
    initial_banner_bbox: BoundingBox,
    landing_bbox: BoundingBox,
    output_path: Path,
    artifact_base: Path,
    source_copy_path: Path,
    banner_replaced_path: Path,
    landing_area_path: Path,
    max_attempts: int = MAX_COMPOSITION_VALIDATION_ATTEMPTS,
) -> tuple[int, int, BoundingBox, Path]:
    current_bbox = initial_banner_bbox
    bounded_attempts = max(1, max_attempts)
    attempts: list[dict[str, object]] = []
    last_validation: CompositionValidationResult | None = None
    last_banner_attempt_path = banner_replaced_path

    for attempt in range(1, bounded_attempts + 1):
        banner_attempt_path = artifact_base.with_suffix(f".banner-attempt-{attempt}.png")
        last_banner_attempt_path = banner_attempt_path
        width, height = compose_stage2_image(
            source_screenshot_path=request.source_screenshot.path,
            creative_path=request.creative.path,
            effective_click_url=effective_click_url,
            banner_bbox=current_bbox,
            landing_bbox=landing_bbox,
            output_path=output_path,
            source_copy_path=source_copy_path if attempt == 1 else None,
            banner_replaced_path=banner_attempt_path,
            landing_area_path=landing_area_path,
        )
        validation = await _verify_stage2_composition(
            client=client,
            request=request,
            composed_screenshot_path=banner_attempt_path,
            current_banner_bbox=current_bbox,
            validation_attempt=attempt,
        )
        last_validation = validation
        attempts.append(
            {
                "attempt": attempt,
                "bbox": _bbox_dict(current_bbox),
                "banner_attempt_path": banner_attempt_path.name,
                "passed": validation.passed,
                "confidence": validation.confidence,
                "reason": _safe_feedback_text(validation.reason, max_length=240),
                "corrected_bbox": _bbox_dict(validation.corrected_bbox),
                "deltas": dict(validation.deltas),
                "corner_deltas": dict(validation.corner_deltas),
                "validation_metadata": _safe_validation_metadata(validation.raw_metadata),
            }
        )
        logger.info(
            "stage2_composition_validation_attempt request_id=%s attempt=%s max_attempts=%s current_bbox=%s passed=%s confidence=%.3f original_banner_bbox=%s inserted_creative_bbox=%s corrected_bbox=%s deltas=%s corner_deltas=%s reason=%s",
            request.request_id,
            attempt,
            bounded_attempts,
            _safe_bbox(current_bbox),
            validation.passed,
            validation.confidence,
            _safe_bbox(validation.original_banner_bbox),
            _safe_bbox(validation.inserted_creative_bbox),
            _safe_bbox(validation.corrected_bbox),
            _safe_deltas(validation.deltas),
            _safe_corner_deltas(validation.corner_deltas),
            _safe_feedback_text(validation.reason, max_length=240),
        )
        if validation.passed:
            if banner_attempt_path != banner_replaced_path:
                shutil.copyfile(banner_attempt_path, banner_replaced_path)
            return width, height, current_bbox, banner_replaced_path

        correction = _select_next_composition_bbox(
            validation=validation,
            current_bbox=current_bbox,
            request=request,
            attempt=attempt,
        )
        if correction is None:
            logger.info(
                "stage2_composition_validation_no_correction request_id=%s attempt=%s reason=%s",
                request.request_id,
                attempt,
                _safe_feedback_text(validation.reason, max_length=240),
            )
            break
        logger.info(
            "stage2_composition_validation_next_bbox request_id=%s attempt=%s correction_source=%s previous_bbox=%s next_bbox=%s deltas=%s corner_deltas=%s",
            request.request_id,
            attempt,
            correction.source,
            _safe_bbox(current_bbox),
            _safe_bbox(correction.bbox),
            _safe_deltas(validation.deltas),
            _safe_corner_deltas(validation.corner_deltas),
        )
        current_bbox = correction.bbox

    diagnostics_path = _save_composition_validation_diagnostics(
        request=request,
        artifact_base=artifact_base,
        source_copy_path=source_copy_path,
        output_path=output_path,
        last_banner_attempt_path=last_banner_attempt_path,
        attempts=attempts,
        last_validation=last_validation,
    )
    logger.warning(
        "stage2_composition_validation_failed request_id=%s attempts=%s last_reason=%s last_corrected_bbox=%s diagnostics=%s",
        request.request_id,
        len(attempts),
        _safe_feedback_text(last_validation.reason if last_validation else "validation unavailable", max_length=240),
        _safe_bbox(last_validation.corrected_bbox if last_validation else None),
        diagnostics_path.name,
    )
    raise ValidationError("ИИ не смог точно совместить креатив с исходным рекламным блоком после проверки. Загрузите более четкий скриншот, где весь рекламный блок виден полностью.")


async def select_valid_banner_bbox_with_feedback(
    *,
    client: GemmaBBoxClient,
    request: ProofRequest,
    max_attempts: int = MAX_BANNER_SELECTION_ATTEMPTS,
) -> BoundingBox:
    """Select and validate a banner bbox before any creative composition."""

    validation_feedback: str | None = None
    last_feedback: _BannerValidationFeedback | None = None
    bounded_attempts = max(1, max_attempts)

    for attempt in range(1, bounded_attempts + 1):
        banner_detection = await client.detect_banner_bbox(
            screenshot=request.source_screenshot,
            spec=request.spec,
            request_id=request.request_id,
            validation_feedback=validation_feedback,
            selection_attempt=attempt,
        )
        try:
            banner_bbox = validate_bbox_within_image(
                banner_detection.bbox,
                image_width=request.source_screenshot.width,
                image_height=request.source_screenshot.height,
                label="область баннера",
            )
            banner_bbox = validate_banner_bbox_semantics(
                banner_bbox,
                spec=request.spec,
                image_width=request.source_screenshot.width,
                image_height=request.source_screenshot.height,
                request_id=request.request_id,
            )
        except ValidationError as exc:
            last_feedback = _banner_validation_feedback(
                banner_detection.bbox,
                spec=request.spec,
                image_width=request.source_screenshot.width,
                image_height=request.source_screenshot.height,
                error=exc,
            )
            logger.info(
                "stage2_banner_bbox_selection_attempt request_id=%s attempt=%s max_attempts=%s spec_slug=%s bbox=%s validation_status=rejected reason_codes=%s validation_error=%s",
                request.request_id,
                attempt,
                bounded_attempts,
                request.spec.slug,
                _safe_bbox(banner_detection.bbox),
                ",".join(last_feedback.reason_codes) if last_feedback.reason_codes else "unknown",
                last_feedback.message,
            )
            validation_feedback = last_feedback.message
            continue

        logger.info(
            "stage2_banner_bbox_selection_attempt request_id=%s attempt=%s max_attempts=%s spec_slug=%s bbox=%s validation_status=accepted reason_codes=none validation_error=none",
            request.request_id,
            attempt,
            bounded_attempts,
            request.spec.slug,
            _safe_bbox(banner_bbox),
        )
        return banner_bbox

    last_message = last_feedback.message if last_feedback else "unknown validation error"
    raise ValidationError(f"ИИ не смог подобрать валидную область баннера после {bounded_attempts} попыток. Последняя ошибка: {last_message}")


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
        creative_source = _trim_transparent_padding(creative_raw.convert("RGBA"))
        creative = creative_source.resize((banner_bbox.width, banner_bbox.height), Image.Resampling.LANCZOS)
        background_crop = result.crop((banner_bbox.x, banner_bbox.y, banner_bbox.right, banner_bbox.bottom))
        blended = Image.alpha_composite(background_crop, creative)
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


def validate_banner_bbox_semantics(
    bbox: BoundingBox,
    *,
    spec: CreativeSpec,
    image_width: int,
    image_height: int,
    request_id: str,
    max_aspect_ratio_deviation: float = DEFAULT_BANNER_ASPECT_RATIO_DEVIATION_THRESHOLD,
) -> BoundingBox:
    """Validate OCR banner bbox against selected creative spec geometry."""

    expected_ratio = _spec_aspect_ratio(spec)
    bbox_ratio = bbox.width / bbox.height if bbox.height > 0 else 0.0
    ratio_deviation = _relative_ratio_deviation(bbox_ratio=bbox_ratio, expected_ratio=expected_ratio)
    area_ratio = bbox.width * bbox.height / (image_width * image_height) if image_width > 0 and image_height > 0 else 0.0
    min_width, min_height = _minimum_banner_dimensions(spec=spec)
    too_small = bbox.width < min_width or bbox.height < min_height or area_ratio < MIN_BANNER_AREA_RATIO
    ratio_invalid = ratio_deviation > max_aspect_ratio_deviation
    position_feedback = _banner_position_feedback(bbox=bbox, spec=spec, image_width=image_width, image_height=image_height)
    position_invalid = not position_feedback.accepted
    accepted = not (ratio_invalid or too_small or position_invalid)
    reason_codes = _semantic_reject_reason_codes(
        bbox=bbox,
        ratio_invalid=ratio_invalid,
        ratio_deviation=ratio_deviation,
        min_width=min_width,
        min_height=min_height,
        area_ratio=area_ratio,
        image_width=image_width,
        image_height=image_height,
        position_invalid=position_invalid,
    )

    logger.info(
        "stage2_banner_bbox_semantic_validation request_id=%s spec_slug=%s bbox=%s bbox_ratio=%.3f expected_ratio=%.3f ratio_deviation=%.3f area_ratio=%.5f min_width=%s min_height=%s center_ratio=x=%.3f,y=%.3f zone=%s placement_guard=%s placement_hint=%s decision=%s reason_codes=%s",
        request_id,
        spec.slug,
        _safe_bbox(bbox),
        bbox_ratio,
        expected_ratio,
        ratio_deviation,
        area_ratio,
        min_width,
        min_height,
        position_feedback.center_x_ratio,
        position_feedback.center_y_ratio,
        position_feedback.zone,
        position_feedback.reason,
        _safe_feedback_text(spec.placement_hint, max_length=120),
        "accepted" if accepted else "rejected",
        ",".join(reason_codes) if reason_codes else "none",
    )

    if not accepted:
        raise ValidationError("ИИ нашел неправдоподобную область баннера на скриншоте. Загрузите более четкий скриншот, где весь рекламный блок виден полностью.")
    return bbox


def _select_next_composition_bbox(
    *,
    validation: CompositionValidationResult,
    current_bbox: BoundingBox,
    request: ProofRequest,
    attempt: int,
) -> _CompositionCorrection | None:
    mismatch_present = _has_non_zero_deltas(validation.deltas) or _has_non_zero_corner_deltas(validation.corner_deltas)
    candidates: list[tuple[str, BoundingBox | None]] = []

    if validation.corrected_bbox is not None:
        if validation.corrected_bbox != current_bbox or not mismatch_present:
            candidates.append(("corrected_bbox", validation.corrected_bbox))
        else:
            logger.info(
                "stage2_composition_validation_correction_unchanged request_id=%s attempt=%s correction_source=corrected_bbox current_bbox=%s mismatch_present=%s",
                request.request_id,
                attempt,
                _safe_bbox(current_bbox),
                mismatch_present,
            )

    candidates.extend(
        (
            ("corner_deltas", _bbox_from_corner_deltas(current_bbox=current_bbox, corner_deltas=validation.corner_deltas)),
            ("deltas", _bbox_from_deltas(current_bbox=current_bbox, deltas=validation.deltas)),
            ("original_banner_bbox", validation.original_banner_bbox),
        )
    )

    for source, candidate in candidates:
        if candidate is None:
            continue
        if candidate == current_bbox:
            logger.info(
                "stage2_composition_validation_candidate_rejected request_id=%s attempt=%s correction_source=%s candidate_bbox=%s rejection=unchanged_bbox mismatch_present=%s",
                request.request_id,
                attempt,
                source,
                _safe_bbox(candidate),
                mismatch_present,
            )
            continue
        try:
            validated = validate_bbox_within_image(
                candidate,
                image_width=request.source_screenshot.width,
                image_height=request.source_screenshot.height,
                label="исправленная область баннера",
            )
            validated = validate_banner_bbox_semantics(
                validated,
                spec=request.spec,
                image_width=request.source_screenshot.width,
                image_height=request.source_screenshot.height,
                request_id=request.request_id,
            )
        except ValidationError as exc:
            logger.info(
                "stage2_composition_validation_candidate_rejected request_id=%s attempt=%s correction_source=%s candidate_bbox=%s rejection=validation_error validation_error=%s",
                request.request_id,
                attempt,
                source,
                _safe_bbox(candidate),
                _safe_feedback_text(exc.safe_message, max_length=240),
            )
            continue
        return _CompositionCorrection(bbox=validated, source=source)

    return None


def _bbox_from_corner_deltas(*, current_bbox: BoundingBox, corner_deltas: dict[str, dict[str, int]]) -> BoundingBox | None:
    if not _has_non_zero_corner_deltas(corner_deltas):
        return None

    top_left = corner_deltas.get("top_left", {})
    top_right = corner_deltas.get("top_right", {})
    bottom_right = corner_deltas.get("bottom_right", {})
    bottom_left = corner_deltas.get("bottom_left", {})

    left = current_bbox.x + _average_present_delta((top_left, bottom_left), "dx")
    top = current_bbox.y + _average_present_delta((top_left, top_right), "dy")
    right = current_bbox.right + _average_present_delta((top_right, bottom_right), "dx")
    bottom = current_bbox.bottom + _average_present_delta((bottom_left, bottom_right), "dy")

    rounded_left = int(round(left))
    rounded_top = int(round(top))
    rounded_right = int(round(right))
    rounded_bottom = int(round(bottom))
    return BoundingBox(
        x=rounded_left,
        y=rounded_top,
        width=rounded_right - rounded_left,
        height=rounded_bottom - rounded_top,
    )


def _bbox_from_deltas(*, current_bbox: BoundingBox, deltas: dict[str, int]) -> BoundingBox | None:
    if not _has_non_zero_deltas(deltas):
        return None

    delta_x = int(deltas.get("x", 0))
    delta_y = int(deltas.get("y", 0))
    delta_width = int(deltas.get("width", 0))
    delta_height = int(deltas.get("height", 0))
    return BoundingBox(
        x=current_bbox.x - delta_x,
        y=current_bbox.y - delta_y,
        width=current_bbox.width - delta_width,
        height=current_bbox.height - delta_height,
    )


def _average_present_delta(corners: tuple[dict[str, int], ...], axis: str) -> float:
    values = [float(corner[axis]) for corner in corners if axis in corner]
    if not values:
        return 0.0
    return sum(values) / len(values)


def _has_non_zero_deltas(deltas: dict[str, int]) -> bool:
    return any(int(deltas.get(key, 0)) != 0 for key in ("x", "y", "width", "height"))


def _has_non_zero_corner_deltas(corner_deltas: dict[str, dict[str, int]]) -> bool:
    return any(
        int(corner_deltas.get(corner, {}).get(axis, 0)) != 0
        for corner in ("top_left", "top_right", "bottom_right", "bottom_left")
        for axis in ("dx", "dy")
    )


@dataclass(frozen=True, slots=True)
class _BannerPositionFeedback:
    accepted: bool
    reason: str
    center_x_ratio: float
    center_y_ratio: float
    zone: str


def _banner_position_feedback(*, bbox: BoundingBox, spec: CreativeSpec, image_width: int, image_height: int) -> _BannerPositionFeedback:
    if image_width <= 0 or image_height <= 0:
        return _BannerPositionFeedback(accepted=True, reason="image_size_unavailable", center_x_ratio=0.0, center_y_ratio=0.0, zone="unknown")

    center_x_ratio = (bbox.x + bbox.width / 2) / image_width
    center_y_ratio = (bbox.y + bbox.height / 2) / image_height
    hint = spec.placement_hint.lower()
    zone = _bbox_zone(bbox=bbox, image_width=image_width, image_height=image_height)
    checks: list[tuple[str, bool]] = []

    if "right" in hint or "right-column" in hint:
        checks.append((f"right_column_center_x>={RIGHT_COLUMN_MIN_CENTER_X_RATIO:.3f}", center_x_ratio >= RIGHT_COLUMN_MIN_CENTER_X_RATIO))
    elif "left" in hint:
        checks.append((f"left_column_center_x<={LEFT_COLUMN_MAX_CENTER_X_RATIO:.3f}", center_x_ratio <= LEFT_COLUMN_MAX_CENTER_X_RATIO))

    if _has_top_slot_hint(hint):
        checks.append((f"top_slot_center_y<={TOP_SLOT_MAX_CENTER_Y_RATIO:.3f}", center_y_ratio <= TOP_SLOT_MAX_CENTER_Y_RATIO))
    elif "bottom" in hint or "footer" in hint:
        checks.append((f"bottom_slot_center_y>={BOTTOM_SLOT_MIN_CENTER_Y_RATIO:.3f}", center_y_ratio >= BOTTOM_SLOT_MIN_CENTER_Y_RATIO))

    if not checks:
        return _BannerPositionFeedback(accepted=True, reason="no_position_guard", center_x_ratio=center_x_ratio, center_y_ratio=center_y_ratio, zone=zone)

    failed = [name for name, passed in checks if not passed]
    reason = "passed:" + ",".join(name for name, _ in checks) if not failed else "failed:" + ",".join(failed)
    return _BannerPositionFeedback(accepted=not failed, reason=reason, center_x_ratio=center_x_ratio, center_y_ratio=center_y_ratio, zone=zone)


def _has_top_slot_hint(hint: str) -> bool:
    if "bottom" in hint:
        return False
    return "header" in hint or "top-of-page" in hint or "top of page" in hint or "above main content" in hint


def _banner_validation_feedback(
    bbox: BoundingBox | None,
    *,
    spec: CreativeSpec,
    image_width: int,
    image_height: int,
    error: ValidationError,
) -> _BannerValidationFeedback:
    reason_codes: list[str] = []
    parts = [f"validation_error={_safe_feedback_text(error.safe_message)}"]
    expected_ratio = _spec_aspect_ratio(spec)
    min_width, min_height = _minimum_banner_dimensions(spec=spec)
    parts.extend(
        [
            f"selected_spec={spec.slug}",
            f"expected_dimensions={spec.dimensions_label}",
            f"expected_ratio={expected_ratio:.3f}",
            f"min_width={min_width}",
            f"min_height={min_height}",
            f"source_image={image_width}x{image_height}",
            "coordinate_contract=source screenshot pixel bbox must stay within image bounds",
            "tgo_full_card_policy=select full ad card/original slot, not internal image asset",
        ]
    )

    if bbox is None:
        reason_codes.append("bbox_missing")
        parts.extend(["previous_invalid_bbox=none", "avoid_previous_invalid_area=true"])
    else:
        bbox_ratio = bbox.width / bbox.height if bbox.height > 0 else 0.0
        ratio_deviation = _relative_ratio_deviation(bbox_ratio=bbox_ratio, expected_ratio=expected_ratio)
        area_ratio = bbox.width * bbox.height / (image_width * image_height) if image_width > 0 and image_height > 0 else 0.0
        center_x = bbox.x + bbox.width / 2
        center_y = bbox.y + bbox.height / 2
        position_feedback = _banner_position_feedback(bbox=bbox, spec=spec, image_width=image_width, image_height=image_height)
        zone = position_feedback.zone
        reason_codes.extend(
            _semantic_reject_reason_codes(
                bbox=bbox,
                ratio_invalid=ratio_deviation > DEFAULT_BANNER_ASPECT_RATIO_DEVIATION_THRESHOLD,
                ratio_deviation=ratio_deviation,
                min_width=min_width,
                min_height=min_height,
                area_ratio=area_ratio,
                image_width=image_width,
                image_height=image_height,
                position_invalid=not position_feedback.accepted,
            )
        )
        if bbox.x < 0 or bbox.y < 0 or bbox.width <= 0 or bbox.height <= 0:
            reason_codes.append("invalid_dimensions")
        if bbox.right > image_width or bbox.bottom > image_height:
            reason_codes.append("out_of_bounds")
        parts.extend(
            [
                f"previous_invalid_bbox={_safe_bbox(bbox)}",
                f"previous_invalid_center=x={center_x:.1f},y={center_y:.1f}",
                f"previous_invalid_center_ratio=x={position_feedback.center_x_ratio:.3f},y={position_feedback.center_y_ratio:.3f}",
                f"previous_invalid_zone={zone}",
                f"placement_guard={position_feedback.reason}",
                "avoid_previous_invalid_area=true",
                "do_not_repeat_previous_invalid_bbox=true",
                f"bbox_ratio={bbox_ratio:.3f}",
                f"ratio_deviation={ratio_deviation:.3f}",
                f"area_ratio={area_ratio:.5f}",
                f"out_of_bounds={bbox.x < 0 or bbox.y < 0 or bbox.width <= 0 or bbox.height <= 0 or bbox.right > image_width or bbox.bottom > image_height}",
            ]
        )

    unique_reason_codes = tuple(dict.fromkeys(reason_codes or ["validation_failed"]))
    parts.append(f"reason_codes={','.join(unique_reason_codes)}")
    return _BannerValidationFeedback(message="; ".join(parts), reason_codes=unique_reason_codes)


def _semantic_reject_reason_codes(
    *,
    bbox: BoundingBox,
    ratio_invalid: bool,
    ratio_deviation: float,
    min_width: int,
    min_height: int,
    area_ratio: float,
    image_width: int,
    image_height: int,
    position_invalid: bool = False,
) -> list[str]:
    reasons: list[str] = []
    if bbox.x < 0 or bbox.y < 0 or bbox.width <= 0 or bbox.height <= 0 or bbox.right > image_width or bbox.bottom > image_height:
        reasons.append("out_of_bounds")
    if ratio_invalid:
        reasons.append("aspect_ratio_deviation")
    if position_invalid:
        reasons.append("placement_position_mismatch")
    if bbox.width < min_width:
        reasons.append("min_width")
    if bbox.height < min_height:
        reasons.append("min_height")
    if area_ratio < MIN_BANNER_AREA_RATIO:
        reasons.append("min_area_ratio")
    return reasons


def _bbox_zone(*, bbox: BoundingBox, image_width: int, image_height: int) -> str:
    if image_width <= 0 or image_height <= 0:
        return "unknown"
    center_x = (bbox.x + bbox.width / 2) / image_width
    center_y = (bbox.y + bbox.height / 2) / image_height
    horizontal = "left" if center_x < 1 / 3 else "right" if center_x > 2 / 3 else "center"
    vertical = "top" if center_y < 1 / 3 else "bottom" if center_y > 2 / 3 else "middle"
    return f"{vertical}_{horizontal}"


def _valid_or_fallback_landing_bbox(bbox: BoundingBox | None, *, image_width: int, image_height: int) -> BoundingBox:
    if bbox is None:
        return fallback_landing_bbox(image_width=image_width, image_height=image_height)
    try:
        return validate_bbox_within_image(bbox, image_width=image_width, image_height=image_height, label="landing/link area")
    except ValidationError:
        return fallback_landing_bbox(image_width=image_width, image_height=image_height)


async def _verify_stage2_composition(
    *,
    client: GemmaBBoxClient,
    request: ProofRequest,
    composed_screenshot_path: Path,
    current_banner_bbox: BoundingBox,
    validation_attempt: int,
) -> CompositionValidationResult:
    verifier = getattr(client, "verify_composition_alignment", None)
    if verifier is None:
        logger.info(
            "stage2_composition_validation_skipped request_id=%s attempt=%s reason=client_method_missing",
            request.request_id,
            validation_attempt,
        )
        return CompositionValidationResult(
            passed=True,
            original_banner_bbox=current_banner_bbox,
            inserted_creative_bbox=current_banner_bbox,
            corrected_bbox=current_banner_bbox,
            confidence=1.0,
            reason="composition validation skipped because injected client has no verifier",
            source="local",
            raw_metadata={"skipped": True},
        )
    try:
        return await verifier(
            original_screenshot=request.source_screenshot,
            composed_screenshot_path=composed_screenshot_path,
            current_banner_bbox=current_banner_bbox,
            spec=request.spec,
            request_id=request.request_id,
            validation_attempt=validation_attempt,
        )
    except (GemmaClientError, ValueError, TypeError, KeyError) as exc:
        logger.warning(
            "stage2_composition_validation_model_error request_id=%s attempt=%s error_class=%s error_message=%s",
            request.request_id,
            validation_attempt,
            type(exc).__name__,
            _safe_feedback_text(str(exc), max_length=240),
        )
        return CompositionValidationResult(
            passed=False,
            original_banner_bbox=None,
            inserted_creative_bbox=current_banner_bbox,
            corrected_bbox=None,
            confidence=0.0,
            reason="invalid composition validation model response",
            source="gemma",
            raw_metadata={"error_class": type(exc).__name__},
        )


def _trim_transparent_padding(image: Image.Image) -> Image.Image:
    alpha_bbox = image.getchannel("A").getbbox()
    if alpha_bbox is None:
        return image
    if alpha_bbox == (0, 0, image.width, image.height):
        return image
    return image.crop(alpha_bbox)


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


def _spec_aspect_ratio(spec: CreativeSpec) -> float:
    if spec.width and spec.height and spec.width > 0 and spec.height > 0:
        return spec.width / spec.height
    return 0.0


def _relative_ratio_deviation(*, bbox_ratio: float, expected_ratio: float) -> float:
    if bbox_ratio <= 0 or expected_ratio <= 0:
        return 0.0
    return abs(bbox_ratio - expected_ratio) / expected_ratio


def _minimum_banner_dimensions(*, spec: CreativeSpec) -> tuple[int, int]:
    min_width = MIN_BANNER_DIMENSION_PIXELS
    min_height = MIN_BANNER_DIMENSION_PIXELS
    if spec.width and spec.height:
        min_width = max(min_width, int(round(spec.width * MIN_BANNER_SPEC_DIMENSION_RATIO)))
        min_height = max(min_height, int(round(spec.height * MIN_BANNER_SPEC_DIMENSION_RATIO)))
    return min_width, min_height


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


def _safe_bbox(bbox: BoundingBox | None) -> str:
    if bbox is None:
        return "none"
    return f"x={bbox.x},y={bbox.y},width={bbox.width},height={bbox.height}"


def _bbox_dict(bbox: BoundingBox | None) -> dict[str, int] | None:
    if bbox is None:
        return None
    return {"x": bbox.x, "y": bbox.y, "width": bbox.width, "height": bbox.height}


def _safe_validation_metadata(raw_metadata: dict[str, object]) -> dict[str, object]:
    allowed_keys = (
        "coordinate_space",
        "crop_mode",
        "crop_origin_x",
        "crop_origin_y",
        "crop_width",
        "crop_height",
        "confidence_threshold",
        "exact_match",
    )
    return {key: raw_metadata.get(key) for key in allowed_keys if key in raw_metadata}


def _safe_deltas(deltas: dict[str, int]) -> str:
    return ",".join(f"{key}={int(deltas.get(key, 0))}" for key in ("x", "y", "width", "height"))


def _safe_corner_deltas(corner_deltas: dict[str, dict[str, int]]) -> str:
    parts = []
    for corner in ("top_left", "top_right", "bottom_right", "bottom_left"):
        delta = corner_deltas.get(corner, {})
        parts.append(f"{corner}.dx={int(delta.get('dx', 0))},{corner}.dy={int(delta.get('dy', 0))}")
    return ";".join(parts)


def _save_composition_validation_diagnostics(
    *,
    request: ProofRequest,
    artifact_base: Path,
    source_copy_path: Path,
    output_path: Path,
    last_banner_attempt_path: Path,
    attempts: list[dict[str, object]],
    last_validation: CompositionValidationResult | None,
) -> Path:
    diagnostics_path = artifact_base.with_suffix(".composition-validation-failed.json")
    diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "request_id": request.request_id,
        "spec_slug": request.spec.slug,
        "source_screenshot": {"width": request.source_screenshot.width, "height": request.source_screenshot.height, "artifact": source_copy_path.name},
        "output_artifact": output_path.name,
        "last_composed_attempt_artifact": last_banner_attempt_path.name,
        "attempts": attempts,
        "last_reason": _safe_feedback_text(last_validation.reason if last_validation else "validation unavailable", max_length=240),
        "last_corrected_bbox": _bbox_dict(last_validation.corrected_bbox if last_validation else None),
    }
    diagnostics_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return diagnostics_path


def _safe_feedback_text(value: str, *, max_length: int = 300) -> str:
    safe = re.sub(r"[\r\n\t]+", " ", value.strip())
    safe = re.sub(r"[^0-9A-Za-zА-Яа-яЁё .,;:()_\-=+/%×]+", "", safe)
    return safe[:max_length]


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
