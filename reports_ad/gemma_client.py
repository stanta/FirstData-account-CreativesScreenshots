from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
from PIL import Image

from .config import BotConfig
from .models import BBoxDetectionResult, BoundingBox, CompositionValidationResult, CreativeSpec, SourceScreenshotInfo
from .validation import ValidationError

logger = logging.getLogger(__name__)
DETAILED_OCR_LOGGING_ENV = "REPORTS_AD_OCR_LOG_TEXT"
DETAILED_OCR_LOGGING_MAX_CHARS_ENV = "REPORTS_AD_OCR_LOG_TEXT_MAX_CHARS"
DEFAULT_MAX_LOG_TEXT_CHARS = 0
DEFAULT_COMPOSITION_VALIDATION_CROP_MARGIN_PERCENT = 0.20
DEFAULT_COMPOSITION_VALIDATION_CROP_MARGIN_MIN_PIXELS = 60


@dataclass(frozen=True, slots=True)
class CompositionValidationCrop:
    x: int
    y: int
    width: int
    height: int
    margin_x: int
    margin_y: int
    margin_percent: float
    margin_min_pixels: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height


class GemmaClientError(RuntimeError):
    """Retryable or final OCR LLM client error."""


class GemmaBBoxClient(Protocol):
    async def detect_banner_bbox(
        self,
        *,
        screenshot: SourceScreenshotInfo,
        spec: CreativeSpec,
        request_id: str,
        validation_feedback: str | None = None,
        selection_attempt: int = 1,
    ) -> BBoxDetectionResult: ...

    async def detect_landing_bbox(self, *, screenshot: SourceScreenshotInfo, spec: CreativeSpec, request_id: str) -> BBoxDetectionResult: ...

    async def verify_composition_alignment(
        self,
        *,
        original_screenshot: SourceScreenshotInfo,
        composed_screenshot_path: Path,
        current_banner_bbox: BoundingBox,
        spec: CreativeSpec,
        request_id: str,
        validation_attempt: int = 1,
    ) -> CompositionValidationResult: ...


class HeaderAuthGemmaClient:
    """OpenAI-compatible OCR vision client; the backing model may be Gemma."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        authorization_header: str | None,
        timeout_seconds: float = 120.0,
        max_retries: int = 5,
        confidence_threshold: float = 0.45,
        ocr_image_max_width: int = 1280,
        ocr_image_max_pixels: int = 2_000_000,
        composition_validation_crop_margin_percent: float = DEFAULT_COMPOSITION_VALIDATION_CROP_MARGIN_PERCENT,
        composition_validation_crop_margin_min_pixels: int = DEFAULT_COMPOSITION_VALIDATION_CROP_MARGIN_MIN_PIXELS,
        ocr_log_text: bool | None = None,
        ocr_log_text_max_chars: int | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._authorization_header = authorization_header
        self._timeout_seconds = timeout_seconds
        self._max_retries = max(1, max_retries)
        self._confidence_threshold = confidence_threshold
        self._ocr_image_max_width = max(0, ocr_image_max_width)
        self._ocr_image_max_pixels = max(0, ocr_image_max_pixels)
        self._composition_validation_crop_margin_percent = max(0.0, composition_validation_crop_margin_percent)
        self._composition_validation_crop_margin_min_pixels = max(0, composition_validation_crop_margin_min_pixels)
        self._ocr_log_text = _detailed_ocr_logging_enabled() if ocr_log_text is None else bool(ocr_log_text)
        self._ocr_log_text_max_chars = _ocr_log_text_max_chars() if ocr_log_text_max_chars is None else max(0, int(ocr_log_text_max_chars))

    async def detect_banner_bbox(
        self,
        *,
        screenshot: SourceScreenshotInfo,
        spec: CreativeSpec,
        request_id: str,
        validation_feedback: str | None = None,
        selection_attempt: int = 1,
    ) -> BBoxDetectionResult:
        return await self._detect_bbox(
            stage="banner_bbox",
            screenshot=screenshot,
            spec=spec,
            request_id=request_id,
            validation_feedback=validation_feedback,
            selection_attempt=selection_attempt,
        )

    async def detect_landing_bbox(self, *, screenshot: SourceScreenshotInfo, spec: CreativeSpec, request_id: str) -> BBoxDetectionResult:
        return await self._detect_bbox(stage="landing_bbox", screenshot=screenshot, spec=spec, request_id=request_id)

    async def verify_composition_alignment(
        self,
        *,
        original_screenshot: SourceScreenshotInfo,
        composed_screenshot_path: Path,
        current_banner_bbox: BoundingBox,
        spec: CreativeSpec,
        request_id: str,
        validation_attempt: int = 1,
    ) -> CompositionValidationResult:
        if not self._base_url:
            raise GemmaClientError("OCR LLM endpoint is not configured")

        last_error: BaseException | None = None
        original_ocr_path: Path | None = None
        composed_ocr_path: Path | None = None
        cleanup_original_ocr = False
        cleanup_composed_ocr = False
        stage = "composition_validation"
        try:
            crop = _full_image_composition_validation_crop(image_width=original_screenshot.width, image_height=original_screenshot.height)
            original_ocr_path, original_width, original_height, original_mime_type, cleanup_original_ocr = _crop_image_input_copy(
                image_path=original_screenshot.path,
                crop=crop,
            )
            composed_ocr_path, composed_width, composed_height, composed_mime_type, cleanup_composed_ocr = _crop_image_input_copy(
                image_path=composed_screenshot_path,
                crop=crop,
            )
            prompt = _composition_validation_prompt(
                screenshot=original_screenshot,
                spec=spec,
                current_banner_bbox=current_banner_bbox,
                ocr_image_width=original_width,
                ocr_image_height=original_height,
                validation_attempt=validation_attempt,
                crop=crop,
            )
            crop_mode = _composition_validation_crop_mode(crop=crop, source_image_width=original_screenshot.width, source_image_height=original_screenshot.height)
            for attempt in range(1, self._max_retries + 1):
                logger.info(
                    "ocr_llm_request request_id=%s stage=%s attempt=%s validation_attempt=%s model=%s spec_slug=%s crop_mode=%s source_screenshot=%sx%s original_ocr_image=%sx%s composed_ocr_image=%sx%s crop_rect=x=%s,y=%s,width=%s,height=%s current_banner_bbox=%s prompt_length=%s prompt_sha256=%s prompt_text=%s",
                    request_id,
                    stage,
                    attempt,
                    validation_attempt,
                    self._model,
                    spec.slug,
                    crop_mode,
                    original_screenshot.width,
                    original_screenshot.height,
                    original_width,
                    original_height,
                    composed_width,
                    composed_height,
                    crop.x,
                    crop.y,
                    crop.width,
                    crop.height,
                    _safe_bbox(current_banner_bbox),
                    len(prompt),
                    _sha256_short(prompt),
                    self._safe_debug_text(prompt),
                )
                try:
                    payload = await self._execute_multimodal_request(
                        prompt=prompt,
                        images=[
                            (original_ocr_path, original_mime_type),
                            (composed_ocr_path, composed_mime_type),
                        ],
                    )
                    raw_text = _extract_openai_compatible_text(payload)
                    logger.info(
                        "ocr_llm_raw_response request_id=%s stage=%s attempt=%s validation_attempt=%s response_text_length=%s response_text_sha256=%s response_text=%s",
                        request_id,
                        stage,
                        attempt,
                        validation_attempt,
                        len(raw_text),
                        _sha256_short(raw_text),
                        self._safe_debug_text(raw_text),
                    )
                    parsed = _loads_json_object(raw_text)
                    result = _parse_composition_validation_result(
                        parsed,
                        source_image_width=original_screenshot.width,
                        source_image_height=original_screenshot.height,
                        confidence_threshold=self._confidence_threshold,
                        crop=crop,
                    )
                    logger.info(
                        "ocr_llm_response request_id=%s stage=%s attempt=%s validation_attempt=%s status=ok parse_success=%s crop_mode=%s passed=%s confidence=%.3f original_banner_bbox=%s inserted_creative_bbox=%s corrected_bbox=%s deltas=%s corner_deltas=%s coordinate_space=%s reason=%s response_text_length=%s response_text_sha256=%s response_text=%s",
                        request_id,
                        stage,
                        attempt,
                        validation_attempt,
                        True,
                        crop_mode,
                        result.passed,
                        result.confidence,
                        _safe_bbox(result.original_banner_bbox),
                        _safe_bbox(result.inserted_creative_bbox),
                        _safe_bbox(result.corrected_bbox),
                        _safe_deltas(result.deltas),
                        _safe_corner_deltas(result.corner_deltas),
                        _safe_coordinate_space(result.raw_metadata),
                        _sanitize_log_text(result.reason, limit=180),
                        len(raw_text),
                        _sha256_short(raw_text),
                        self._safe_debug_text(raw_text),
                    )
                    return result
                except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError, GemmaClientError) as exc:
                    last_error = exc
                    logger.warning(
                        "ocr_llm_response request_id=%s stage=%s attempt=%s validation_attempt=%s status=retryable_error parse_success=%s error_class=%s reason_code=%s error_message=%s",
                        request_id,
                        stage,
                        attempt,
                        validation_attempt,
                        False,
                        type(exc).__name__,
                        _ocr_error_reason_code(exc),
                        _safe_exception_message(exc),
                    )
                    if attempt < self._max_retries:
                        await asyncio.sleep(min(2 ** (attempt - 1), 8))
        finally:
            for cleanup, path in (
                (cleanup_original_ocr, original_ocr_path),
                (cleanup_composed_ocr, composed_ocr_path),
            ):
                if cleanup and path is not None:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        logger.debug("ocr_input_cleanup_failed path=%s", path.name)

        raise GemmaClientError(f"OCR LLM composition validation failed after {self._max_retries} attempts: {type(last_error).__name__ if last_error else 'unknown'}") from last_error

    async def _detect_bbox(
        self,
        *,
        stage: str,
        screenshot: SourceScreenshotInfo,
        spec: CreativeSpec,
        request_id: str,
        validation_feedback: str | None = None,
        selection_attempt: int = 1,
    ) -> BBoxDetectionResult:
        if not self._base_url:
            raise GemmaClientError("OCR LLM endpoint is not configured")

        last_error: BaseException | None = None
        ocr_image_path: Path | None = None
        cleanup_ocr_image = False
        try:
            ocr_image_path, ocr_image_width, ocr_image_height, ocr_mime_type, cleanup_ocr_image = _prepare_ocr_input_image(
                screenshot=screenshot,
                max_width=self._ocr_image_max_width,
                max_pixels=self._ocr_image_max_pixels,
            )
            prompt = (
                _banner_prompt(
                    screenshot=screenshot,
                    spec=spec,
                    ocr_image_width=ocr_image_width,
                    ocr_image_height=ocr_image_height,
                    validation_feedback=validation_feedback,
                    selection_attempt=selection_attempt,
                )
                if stage == "banner_bbox"
                else _landing_prompt(screenshot=screenshot, spec=spec, ocr_image_width=ocr_image_width, ocr_image_height=ocr_image_height)
            )
            for attempt in range(1, self._max_retries + 1):
                logger.info(
                    "ocr_llm_request request_id=%s stage=%s attempt=%s model=%s spec_slug=%s spec_name=%s source_screenshot=%sx%s ocr_image=%sx%s ocr_downscaled=%s ocr_image_max_width=%s ocr_image_max_pixels=%s expected_dimensions=%s expected_aspect_ratio=%s placement_hint_present=%s placement_hint=%s prompt_length=%s prompt_sha256=%s prompt_text=%s",
                    request_id,
                    stage,
                    attempt,
                    self._model,
                    spec.slug,
                    _sanitize_log_text(spec.display_name, limit=120),
                    screenshot.width,
                    screenshot.height,
                    ocr_image_width,
                    ocr_image_height,
                    (ocr_image_width, ocr_image_height) != (screenshot.width, screenshot.height),
                    self._ocr_image_max_width,
                    self._ocr_image_max_pixels,
                    spec.dimensions_label,
                    _safe_aspect_ratio(spec),
                    bool(spec.position_hint.strip()),
                    _sanitize_log_text(spec.placement_hint, limit=180),
                    len(prompt),
                    _sha256_short(prompt),
                    self._safe_debug_text(prompt),
                )
                try:
                    payload = await self._execute_request(prompt=prompt, image_path=ocr_image_path, mime_type=ocr_mime_type)
                    raw_text = _extract_openai_compatible_text(payload)
                    logger.info(
                        "ocr_llm_raw_response request_id=%s stage=%s attempt=%s response_text_length=%s response_text_sha256=%s response_text=%s",
                        request_id,
                        stage,
                        attempt,
                        len(raw_text),
                        _sha256_short(raw_text),
                        self._safe_debug_text(raw_text),
                    )
                    parsed = _loads_json_object(raw_text)
                    logger.info(
                        "ocr_llm_bbox_payload request_id=%s stage=%s attempt=%s source_screenshot=%sx%s ocr_image=%sx%s raw_fields=%s",
                        request_id,
                        stage,
                        attempt,
                        screenshot.width,
                        screenshot.height,
                        ocr_image_width,
                        ocr_image_height,
                        _safe_raw_bbox_payload_fields(parsed),
                    )
                    result = _parse_bbox_result(
                        parsed,
                        stage=stage,
                        spec=spec,
                        confidence_threshold=self._confidence_threshold,
                        source_image_width=screenshot.width,
                        source_image_height=screenshot.height,
                    )
                    logger.info(
                        "ocr_llm_response request_id=%s stage=%s attempt=%s status=ok parse_success=%s confidence=%.3f bbox=%s coordinate_space=%s metadata=%s bbox_meta=%s response_text_length=%s response_text_sha256=%s response_text=%s",
                        request_id,
                        stage,
                        attempt,
                        True,
                        result.confidence,
                        _safe_bbox(result.bbox),
                        _safe_coordinate_space(result.raw_metadata),
                        _safe_response_metadata(payload),
                        _safe_bbox_metadata(result.raw_metadata),
                        len(raw_text),
                        _sha256_short(raw_text),
                        self._safe_debug_text(raw_text),
                    )
                    return result
                except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError, GemmaClientError) as exc:
                    last_error = exc
                    logger.warning(
                        "ocr_llm_response request_id=%s stage=%s attempt=%s status=retryable_error parse_success=%s error_class=%s reason_code=%s error_message=%s",
                        request_id,
                        stage,
                        attempt,
                        False,
                        type(exc).__name__,
                        _ocr_error_reason_code(exc),
                        _safe_exception_message(exc),
                    )
                    if attempt < self._max_retries:
                        await asyncio.sleep(min(2 ** (attempt - 1), 8))
        finally:
            if cleanup_ocr_image and ocr_image_path is not None:
                try:
                    ocr_image_path.unlink(missing_ok=True)
                except OSError:
                    logger.debug("ocr_input_cleanup_failed path=%s", ocr_image_path.name)

        raise GemmaClientError(f"OCR LLM failed after {self._max_retries} attempts: {type(last_error).__name__ if last_error else 'unknown'}") from last_error

    async def _execute_request(self, *, prompt: str, image_path: Path, mime_type: str) -> object:
        return await self._execute_multimodal_request(prompt=prompt, images=[(image_path, mime_type)])

    async def _execute_multimodal_request(self, *, prompt: str, images: list[tuple[Path, str]]) -> object:
        headers = {"Content-Type": "application/json"}
        if self._authorization_header:
            headers["Authorization"] = self._authorization_header
        content: list[dict[str, object]] = [{"type": "text", "text": prompt}]
        content.extend({"type": "image_url", "image_url": {"url": _data_url(image_path=image_path, mime_type=mime_type)}} for image_path, mime_type in images)
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.post(
                f"{self._base_url}/chat/completions",
                headers=headers,
                json={
                    "model": self._model,
                    "messages": [
                        {
                            "role": "user",
                            "content": content,
                        }
                    ],
                    "response_format": {"type": "json_object"},
                },
            )
            response.raise_for_status()
            return response.json()

    def _safe_debug_text(self, value: str) -> str:
        if not self._ocr_log_text:
            return "disabled"
        return _sanitize_log_text(value, limit=self._ocr_log_text_max_chars)


def build_gemma_client_from_config(config: BotConfig | None) -> HeaderAuthGemmaClient:
    runtime_config = _require_ocr_runtime_config(config)
    return HeaderAuthGemmaClient(
        base_url=runtime_config.ocr_llm_base_url,
        model=runtime_config.ocr_llm_model,
        authorization_header=runtime_config.ocr_llm_authorization_header,
        timeout_seconds=runtime_config.ocr_llm_timeout_seconds,
        max_retries=runtime_config.ocr_llm_max_retries,
        confidence_threshold=runtime_config.ocr_llm_confidence_threshold,
        ocr_image_max_width=runtime_config.ocr_image_max_width,
        ocr_image_max_pixels=runtime_config.ocr_image_max_pixels,
        composition_validation_crop_margin_percent=runtime_config.composition_validation_crop_margin_percent,
        composition_validation_crop_margin_min_pixels=runtime_config.composition_validation_crop_margin_min_pixels,
        ocr_log_text=runtime_config.ocr_log_text,
        ocr_log_text_max_chars=runtime_config.ocr_log_text_max_chars,
    )


def _require_ocr_runtime_config(config: BotConfig | None) -> BotConfig:
    if config is None:
        raise ValidationError("Конфигурация Stage 2 OCR не инициализирована. Перезапустите бота и повторите попытку.")

    if not config.ocr_llm_base_url.strip():
        raise ValidationError("Stage 2 OCR не настроен: заполните OCR_LLM_BASE_URL и перезапустите бота.")

    if not config.ocr_llm_model.strip():
        raise ValidationError("Stage 2 OCR не настроен: заполните OCR_LLM_MODEL и перезапустите бота.")

    return config


def _banner_prompt(
    *,
    screenshot: SourceScreenshotInfo,
    spec: CreativeSpec,
    ocr_image_width: int | None = None,
    ocr_image_height: int | None = None,
    validation_feedback: str | None = None,
    selection_attempt: int = 1,
) -> str:
    ocr_width = ocr_image_width or screenshot.width
    ocr_height = ocr_image_height or screenshot.height
    candidate_instruction = ""
    if _is_tgo_spec(spec):
        candidate_instruction = (
            "For text-image format, return multiple candidates where possible in a candidates array. "
            "Each candidate must include role, bbox, confidence, reason (with relative position on screen), coordinate_space, image_width and image_height. "
            "Allowed candidate role labels include: full_ad_card, original_ad_slot, internal_image_asset, landing_area, browser_ui, unknown. "
            "Prefer full_ad_card or original_ad_slot for replacement; do not select internal_image_asset when the full ad card/container is visible. "
        )
    base_task_instructions = (
        "Base task instructions: "
        "You are analyzing a placement screenshot for an advertising proof generator. "
        "Find the visible original ad slot area that must be replaced by the uploaded creative. "
        "Use the selected network/spec context, expected dimensions, aspect ratio, and placement hint. "
        f"Selected expected aspect ratio (width/height): {_safe_aspect_ratio(spec)}. "
        f"Selected banner usually placement: {spec.placement_hint}. "
        "The selected spec dimensions are the creative content (like picture, picture and text) size, not only empty white page area with the same geometry is an ad slot. "
        "You MUST identify the visible original ad content/card already present in the screenshot and return the bbox that with precision and accuracy overlaps that EXACTLY visible advertising content. "
        # "For right/sidebar/right-column placements, the bbox center must be in the right third of the screenshot unless the actual visible ad is clearly elsewhere. "
        "You MUST identify the original ad slot EXACTLY matching this selected banner type and position hint; do NOT select the uploaded replacement creative, a floating preview, an editor overlay, white/blank page area, article text area, or a landing/link/browser area. "
        "Select the full visible ad card/container including image, text and CTA, not only the internal image asset. "
        f"{candidate_instruction}"
        "Return ONLY JSON with either keys: bbox {x,y,width,height}, confidence 0..1, reason, coordinate_space, image_width, image_height; or candidates [...]. "
        "Coordinate contract is strict and MUST NOT be mixed: "
        "A) coordinate_space='normalized' means x,y,width,height are decimal fractions from 0.0 to 1.0 of the full OCR input image; NEVER put pixel numbers with coordinate_space='normalized'. "
        "B) coordinate_space='source_pixels' means x,y,width,height are integer pixel coordinates in the original full source screenshot. "
        "C) coordinate_space='resized_pixels' means x,y,width,height are integer pixel coordinates in the OCR input image and you MUST include image_width and image_height matching that coordinate image. "
        "Preferred output: coordinate_space='source_pixels' with integer pixel coordinates in the original full source screenshot. "
        "If you use normalized coordinates, every bbox value MUST be between 0 and 1 inclusive; values such as 707 or 116 are INVALID unless coordinate_space is source_pixels or resized_pixels. "
        "If uncertain about coordinate_space, use source_pixels with coordinates measured on the full source screenshot. "
        f"The source screenshot is {screenshot.width}x{screenshot.height} pixels; the OCR input image sent to you is {ocr_width}x{ocr_height} pixels. "
        "The source and OCR input may differ; do not crop or reset the coordinate origin. "
        f"Selected spec slug: {spec.slug}. Selected spec name: {spec.display_name}. Network: {spec.network_display_name}. "
    )
    feedback_instruction = ""
    if validation_feedback and validation_feedback.strip():
        feedback_instruction = (
            "\nRetry feedback: "
            f"This is selection attempt {max(1, selection_attempt)}. "
            "The previous bbox candidate was rejected by deterministic validation. "
            f"Previous validation feedback for this retry: {validation_feedback} "
            "Do NOT repeat the previous invalid bbox or substantially the same visual area. "
            "Use this feedback only as correction context; the Base task instructions remain authoritative. "
            "On this retry, re-check the coordinate contract before answering: if bbox values are pixel-like numbers greater than 1, coordinate_space MUST be 'source_pixels' or 'resized_pixels', never 'normalized'. "
            "If coordinate_space='normalized', all bbox values MUST be 0..1 fractions, for example x=0.70 not x=707. "
            "Choose a different original ad slot candidate that satisfies the coordinate contract, selected spec dimensions, aspect ratio, and minimum size."
        )
    return base_task_instructions + feedback_instruction


def _landing_prompt(*, screenshot: SourceScreenshotInfo, spec: CreativeSpec, ocr_image_width: int | None = None, ocr_image_height: int | None = None) -> str:
    ocr_width = ocr_image_width or screenshot.width
    ocr_height = ocr_image_height or screenshot.height
    return (
        "You are analyzing a placement proof screenshot. Find the browser/link/landing area template location if it is visible, "
        "usually a lower 10-20 percent area with browser header/address line. "
        "If it is not visible, return bbox null and confidence 0. "
        "Return ONLY JSON with keys: bbox {x,y,width,height} or null, confidence 0..1, reason, coordinate_space, image_width, image_height. "
        "Preferred output: coordinate_space='normalized' with bbox values as 0..1 fractions of the full OCR input image. "
        "If you return pixel coordinates from the OCR input/resized image, set coordinate_space='resized_pixels' and include the exact image_width and image_height used for those coordinates. "
        f"The original source screenshot is {screenshot.width}x{screenshot.height}; the OCR input image sent to you is {ocr_width}x{ocr_height}; never crop the coordinate origin. "
        f"Selected spec: {spec.display_name}, dimensions {spec.dimensions_label}."
    )


def _prepare_ocr_input_image(
    *,
    screenshot: SourceScreenshotInfo,
    max_width: int,
    max_pixels: int,
) -> tuple[Path, int, int, str, bool]:
    return _prepare_image_input_copy(image_path=screenshot.path, max_width=max_width, max_pixels=max_pixels)


def _prepare_image_input_copy(
    *,
    image_path: Path,
    max_width: int,
    max_pixels: int,
) -> tuple[Path, int, int, str, bool]:
    with Image.open(image_path) as raw_image:
        image = raw_image.convert("RGB")
        target_width, target_height = _bounded_ocr_image_size(width=image.width, height=image.height, max_width=max_width, max_pixels=max_pixels)
        if (target_width, target_height) != image.size:
            image = image.resize((target_width, target_height), Image.Resampling.LANCZOS)
        with tempfile.NamedTemporaryFile(prefix="reports-ad-ocr-", suffix=".png", dir=image_path.parent, delete=False) as temp_file:
            temp_path = Path(temp_file.name)
        image.save(temp_path, format="PNG", optimize=True)
        return temp_path, target_width, target_height, "image/png", True


def _composition_validation_crop_rect(
    *,
    bbox: BoundingBox,
    image_width: int,
    image_height: int,
    margin_percent: float = DEFAULT_COMPOSITION_VALIDATION_CROP_MARGIN_PERCENT,
    margin_min_pixels: int = DEFAULT_COMPOSITION_VALIDATION_CROP_MARGIN_MIN_PIXELS,
) -> CompositionValidationCrop:
    if image_width <= 0 or image_height <= 0:
        raise GemmaClientError("composition validation source image size is invalid")
    if bbox.width <= 0 or bbox.height <= 0:
        raise GemmaClientError("composition validation bbox size is invalid")
    safe_percent = max(0.0, margin_percent)
    safe_min_pixels = max(0, margin_min_pixels)
    margin_x = max(safe_min_pixels, int(round(bbox.width * safe_percent)))
    margin_y = max(safe_min_pixels, int(round(bbox.height * safe_percent)))
    left = max(0, bbox.x - margin_x)
    top = max(0, bbox.y - margin_y)
    right = min(image_width, bbox.right + margin_x)
    bottom = min(image_height, bbox.bottom + margin_y)
    if right <= left or bottom <= top:
        raise GemmaClientError("composition validation crop rect is empty")
    return CompositionValidationCrop(
        x=left,
        y=top,
        width=right - left,
        height=bottom - top,
        margin_x=margin_x,
        margin_y=margin_y,
        margin_percent=safe_percent,
        margin_min_pixels=safe_min_pixels,
    )


def _full_image_composition_validation_crop(*, image_width: int, image_height: int) -> CompositionValidationCrop:
    if image_width <= 0 or image_height <= 0:
        raise GemmaClientError("composition validation source image size is invalid")
    return CompositionValidationCrop(
        x=0,
        y=0,
        width=image_width,
        height=image_height,
        margin_x=0,
        margin_y=0,
        margin_percent=1.0,
        margin_min_pixels=0,
    )


def _composition_validation_crop_mode(*, crop: CompositionValidationCrop, source_image_width: int, source_image_height: int) -> str:
    if crop.x == 0 and crop.y == 0 and crop.width == source_image_width and crop.height == source_image_height:
        return "full_image_100_percent"
    return "banner_bbox_margin"


def _crop_image_input_copy(*, image_path: Path, crop: CompositionValidationCrop) -> tuple[Path, int, int, str, bool]:
    with Image.open(image_path) as raw_image:
        image = raw_image.convert("RGB")
        if crop.x < 0 or crop.y < 0 or crop.right > image.width or crop.bottom > image.height:
            raise GemmaClientError("composition validation crop rect is out of image bounds")
        cropped = image.crop((crop.x, crop.y, crop.right, crop.bottom))
        with tempfile.NamedTemporaryFile(prefix="reports-ad-composition-crop-", suffix=".png", dir=image_path.parent, delete=False) as temp_file:
            temp_path = Path(temp_file.name)
        cropped.save(temp_path, format="PNG", optimize=True)
        return temp_path, cropped.width, cropped.height, "image/png", True


def _bounded_ocr_image_size(*, width: int, height: int, max_width: int, max_pixels: int) -> tuple[int, int]:
    scale = 1.0
    if max_width > 0 and width > max_width:
        scale = min(scale, max_width / width)
    if max_pixels > 0 and width * height * scale * scale > max_pixels:
        scale = min(scale, math.sqrt(max_pixels / (width * height)))
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def _data_url(*, image_path: Path, mime_type: str) -> str:
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _extract_openai_compatible_text(payload: object) -> str:
    if not isinstance(payload, dict):
        raise GemmaClientError("OpenAI-compatible response JSON is not an object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise GemmaClientError("OpenAI-compatible response does not contain choices")
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise GemmaClientError("OpenAI-compatible response choice is invalid")
    message = first_choice.get("message")
    if not isinstance(message, dict):
        raise GemmaClientError("OpenAI-compatible response message is invalid")
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        text_parts = [item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"]
        merged = "\n".join(part for part in text_parts if isinstance(part, str) and part.strip()).strip()
        if merged:
            return merged
    raise GemmaClientError("OpenAI-compatible response does not contain text output")


def _loads_json_object(raw_text: str) -> dict[str, object]:
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise GemmaClientError("OCR LLM JSON output is not an object")
    return payload


def _parse_bbox_result(
    payload: dict[str, object],
    *,
    stage: str,
    confidence_threshold: float,
    source_image_width: int,
    source_image_height: int,
    spec: CreativeSpec | None = None,
) -> BBoxDetectionResult:
    confidence = _normalize_confidence(payload.get("confidence"))
    bbox_payload = payload.get("bbox")
    bbox = None
    raw_metadata: dict[str, object] = {"reason": str(payload.get("reason", ""))[:160]}
    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        selected = _select_bbox_candidate(
            payload=payload,
            candidates=candidates,
            stage=stage,
            spec=spec,
            source_image_width=source_image_width,
            source_image_height=source_image_height,
        )
        raw_metadata.update(selected[3])
        bbox = selected[0]
        confidence = selected[1]
        raw_metadata.update(selected[2])
    elif isinstance(bbox_payload, dict):
        bbox, bbox_metadata = _coerce_bbox_to_source_pixels(
            bbox_payload=bbox_payload,
            payload=payload,
            source_image_width=source_image_width,
            source_image_height=source_image_height,
        )
        raw_metadata.update(bbox_metadata)
    if stage == "banner_bbox" and (bbox is None or confidence < confidence_threshold):
        return BBoxDetectionResult(bbox=None, confidence=confidence, source="gemma", raw_metadata=raw_metadata)
    if bbox is not None and confidence < confidence_threshold:
        bbox = None
    return BBoxDetectionResult(bbox=bbox, confidence=confidence, source="gemma", raw_metadata=raw_metadata)


def _parse_composition_validation_result(
    payload: dict[str, object],
    *,
    source_image_width: int,
    source_image_height: int,
    confidence_threshold: float,
    crop: CompositionValidationCrop | None = None,
) -> CompositionValidationResult:
    confidence = _normalize_confidence(payload.get("confidence"))
    raw_passed = _first_present(payload.get("verification_passed"), payload.get("passed"), payload.get("pass"), payload.get("is_exact_match"))
    if not isinstance(raw_passed, bool):
        raise GemmaClientError("composition validation JSON must include boolean verification_passed")
    original_bbox = _optional_bbox_from_payload(
        payload,
        keys=("original_banner_bbox", "original_bbox", "banner_bbox"),
        source_image_width=source_image_width,
        source_image_height=source_image_height,
        crop=crop,
    )
    inserted_bbox = _optional_bbox_from_payload(
        payload,
        keys=("actual_inserted_creative_bbox", "inserted_creative_bbox", "actual_bbox", "inserted_bbox"),
        source_image_width=source_image_width,
        source_image_height=source_image_height,
        crop=crop,
    )
    corrected_bbox = _optional_bbox_from_payload(
        payload,
        keys=("recommended_corrected_bbox", "corrected_bbox", "recommended_insert_bbox", "corrected_insert_parameters"),
        source_image_width=source_image_width,
        source_image_height=source_image_height,
        crop=crop,
    )
    if original_bbox is None:
        raise GemmaClientError("composition validation JSON must include original_banner_bbox")
    if inserted_bbox is None:
        raise GemmaClientError("composition validation JSON must include actual_inserted_creative_bbox")
    deltas = _parse_bbox_deltas(payload.get("deltas") or payload.get("mismatches") or payload.get("delta"), original_bbox=original_bbox, inserted_bbox=inserted_bbox)
    corner_deltas = _parse_corner_deltas(
        _first_present(payload.get("corner_deltas"), payload.get("creative_corner_deltas"), payload.get("per_corner_deltas"), payload.get("corner_displacement_deltas")),
        original_bbox=original_bbox,
        inserted_bbox=inserted_bbox,
    )
    bbox_exact_match = original_bbox == inserted_bbox
    exact_match = bbox_exact_match and all(deltas.get(key, 0) == 0 for key in ("x", "y", "width", "height")) and _corner_deltas_exact_match(corner_deltas)
    model_pass_flag_contradicted_exact_match = not bool(raw_passed) and exact_match and confidence >= confidence_threshold
    passed = (bool(raw_passed) or model_pass_flag_contradicted_exact_match) and exact_match and confidence >= confidence_threshold
    if bool(raw_passed) and not exact_match:
        corrected_bbox = corrected_bbox or original_bbox
    if passed and corrected_bbox is None:
        corrected_bbox = original_bbox
    if not passed and corrected_bbox is None:
        raise GemmaClientError("composition validation mismatch must include recommended_corrected_bbox")
    if corrected_bbox is not None:
        _validate_source_bbox_bounds(bbox=corrected_bbox, source_image_width=source_image_width, source_image_height=source_image_height, label="recommended_corrected_bbox")
    reason = str(payload.get("reason") or payload.get("explanation") or "")[:300]
    if bool(raw_passed) and confidence < confidence_threshold:
        reason = (reason + "; " if reason else "") + "confidence below threshold"
    return CompositionValidationResult(
        passed=passed,
        original_banner_bbox=original_bbox,
        inserted_creative_bbox=inserted_bbox,
        deltas=deltas,
        corner_deltas=corner_deltas,
        corrected_bbox=corrected_bbox,
        confidence=confidence,
        reason=reason,
        source="gemma",
        raw_metadata={
            "raw_passed": raw_passed,
            "bbox_exact_match": bbox_exact_match,
            "exact_match": exact_match,
            "model_pass_flag_contradicted_exact_match": model_pass_flag_contradicted_exact_match,
            "confidence_threshold": confidence_threshold,
            "coordinate_space": _sanitize_log_text(str(payload.get("coordinate_space", "source_pixels")), limit=32),
            "crop_mode": _composition_validation_crop_mode(crop=crop, source_image_width=source_image_width, source_image_height=source_image_height) if crop is not None else "none",
            "crop_origin_x": crop.x if crop is not None else None,
            "crop_origin_y": crop.y if crop is not None else None,
            "crop_width": crop.width if crop is not None else None,
            "crop_height": crop.height if crop is not None else None,
        },
    )


def _optional_bbox_from_payload(
    payload: dict[str, object],
    *,
    keys: tuple[str, ...],
    source_image_width: int,
    source_image_height: int,
    crop: CompositionValidationCrop | None = None,
) -> BoundingBox | None:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise GemmaClientError(f"composition validation field {key} must be bbox object or null")
        bbox, _ = _coerce_bbox_to_source_pixels(
            bbox_payload=value,
            payload={**payload, **value},
            source_image_width=source_image_width,
            source_image_height=source_image_height,
            crop=crop,
        )
        _validate_source_bbox_bounds(bbox=bbox, source_image_width=source_image_width, source_image_height=source_image_height, label=key)
        return bbox
    return None


def _parse_bbox_deltas(value: object, *, original_bbox: BoundingBox, inserted_bbox: BoundingBox) -> dict[str, int]:
    computed = {
        "x": inserted_bbox.x - original_bbox.x,
        "y": inserted_bbox.y - original_bbox.y,
        "width": inserted_bbox.width - original_bbox.width,
        "height": inserted_bbox.height - original_bbox.height,
    }
    if not isinstance(value, dict):
        return computed
    deltas: dict[str, int] = {}
    for key in ("x", "y", "width", "height"):
        raw = value.get(key, computed[key])
        if isinstance(raw, bool):
            raw = computed[key]
        try:
            deltas[key] = int(round(float(raw)))
        except (TypeError, ValueError):
            deltas[key] = computed[key]
    return deltas


def _parse_corner_deltas(value: object, *, original_bbox: BoundingBox, inserted_bbox: BoundingBox) -> dict[str, dict[str, int]]:
    computed = _computed_corner_deltas(original_bbox=original_bbox, inserted_bbox=inserted_bbox)
    if not isinstance(value, dict):
        return computed
    parsed: dict[str, dict[str, int]] = {}
    for corner in ("top_left", "top_right", "bottom_right", "bottom_left"):
        raw_corner = _first_present(value.get(corner), value.get(corner.replace("_", "-")), value.get(corner.replace("_", "")))
        parsed[corner] = _parse_corner_delta(raw_corner, fallback=computed[corner])
    return parsed


def _parse_corner_delta(value: object, *, fallback: dict[str, int]) -> dict[str, int]:
    if not isinstance(value, dict):
        return dict(fallback)
    return {
        "dx": _coerce_delta_int(_first_present(value.get("dx"), value.get("x"), value.get("delta_x")), fallback=fallback["dx"]),
        "dy": _coerce_delta_int(_first_present(value.get("dy"), value.get("y"), value.get("delta_y")), fallback=fallback["dy"]),
    }


def _coerce_delta_int(value: object, *, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return fallback


def _computed_corner_deltas(*, original_bbox: BoundingBox, inserted_bbox: BoundingBox) -> dict[str, dict[str, int]]:
    original_corners = _bbox_corners(original_bbox)
    inserted_corners = _bbox_corners(inserted_bbox)
    return {
        corner: {
            "dx": original_corners[corner][0] - inserted_corners[corner][0],
            "dy": original_corners[corner][1] - inserted_corners[corner][1],
        }
        for corner in ("top_left", "top_right", "bottom_right", "bottom_left")
    }


def _bbox_corners(bbox: BoundingBox) -> dict[str, tuple[int, int]]:
    return {
        "top_left": (bbox.x, bbox.y),
        "top_right": (bbox.right, bbox.y),
        "bottom_right": (bbox.right, bbox.bottom),
        "bottom_left": (bbox.x, bbox.bottom),
    }


def _corner_deltas_exact_match(corner_deltas: dict[str, dict[str, int]]) -> bool:
    return all(corner_deltas.get(corner, {}).get(axis, 0) == 0 for corner in ("top_left", "top_right", "bottom_right", "bottom_left") for axis in ("dx", "dy"))


def _coerce_bbox_to_source_pixels(
    *,
    bbox_payload: dict[str, object],
    payload: dict[str, object],
    source_image_width: int,
    source_image_height: int,
    crop: CompositionValidationCrop | None = None,
) -> tuple[BoundingBox, dict[str, object]]:
    x = _require_number(bbox_payload, "x")
    y = _require_number(bbox_payload, "y")
    width = _require_number(bbox_payload, "width")
    height = _require_number(bbox_payload, "height")

    coordinate_space = _extract_coordinate_space(payload, bbox_payload)
    coordinate_space_explicit = _has_explicit_coordinate_space(payload, bbox_payload)
    coordinate_image_width, coordinate_image_height = _extract_coordinate_image_size(payload, bbox_payload)

    metadata: dict[str, object] = {
        "coordinate_space": coordinate_space,
        "raw_coordinate_space": coordinate_space,
        "coordinate_image_width": coordinate_image_width,
        "coordinate_image_height": coordinate_image_height,
        "raw_image_width": coordinate_image_width,
        "raw_image_height": coordinate_image_height,
        "raw_bbox": _safe_raw_bbox_values(x=x, y=y, width=width, height=height),
        "scaled_to_source": False,
        "crop_origin_x": crop.x if crop is not None else None,
        "crop_origin_y": crop.y if crop is not None else None,
    }

    if coordinate_space in {"crop_pixels", "cropped_pixels", "crop"}:
        if crop is None:
            raise GemmaClientError("invalid_coordinate_space: coordinate_space=crop_pixels requires crop metadata")
        bbox = _bbox_from_crop_pixels(x=x, y=y, width=width, height=height, crop=crop)
        metadata["coordinate_space"] = "crop_pixels"
        metadata["scaled_to_source"] = True
        return bbox, metadata

    if coordinate_space == "normalized":
        if not _looks_normalized(x=x, y=y, width=width, height=height):
            raise GemmaClientError("invalid_coordinate_space: coordinate_space=normalized but bbox values are not 0..1 fractions")
        bbox = _bbox_from_normalized(
            x=x,
            y=y,
            width=width,
            height=height,
            source_image_width=source_image_width,
            source_image_height=source_image_height,
        )
        metadata["coordinate_space"] = "normalized"
        metadata["scaled_to_source"] = True
        return bbox, metadata

    if coordinate_space == "resized_pixels":
        if not coordinate_image_width or not coordinate_image_height:
            raise GemmaClientError("invalid_coordinate_space: coordinate_space=resized_pixels requires image_width and image_height")
        bbox = _bbox_from_resized_pixels(
            x=x,
            y=y,
            width=width,
            height=height,
            coordinate_image_width=coordinate_image_width,
            coordinate_image_height=coordinate_image_height,
            source_image_width=source_image_width,
            source_image_height=source_image_height,
        )
        metadata["scaled_to_source"] = True
        return bbox, metadata

    if not coordinate_space_explicit and _looks_normalized(x=x, y=y, width=width, height=height):
        bbox = _bbox_from_normalized(
            x=x,
            y=y,
            width=width,
            height=height,
            source_image_width=source_image_width,
            source_image_height=source_image_height,
        )
        metadata["coordinate_space"] = "normalized"
        metadata["scaled_to_source"] = True
        return bbox, metadata

    return BoundingBox(x=int(round(x)), y=int(round(y)), width=int(round(width)), height=int(round(height))), metadata


def _validate_source_bbox_bounds(*, bbox: BoundingBox, source_image_width: int, source_image_height: int, label: str) -> None:
    if bbox.x < 0 or bbox.y < 0 or bbox.width <= 0 or bbox.height <= 0 or bbox.right > source_image_width or bbox.bottom > source_image_height:
        raise GemmaClientError(f"composition validation {label} is outside source image bounds")


def _select_bbox_candidate(
    *,
    payload: dict[str, object],
    candidates: list[object],
    stage: str,
    spec: CreativeSpec | None,
    source_image_width: int,
    source_image_height: int,
) -> tuple[BoundingBox | None, float, dict[str, object], dict[str, object]]:
    best: tuple[tuple[float, ...], BoundingBox, float, dict[str, object], dict[str, object]] | None = None
    tgo = stage == "banner_bbox" and spec is not None and _is_tgo_spec(spec)
    allowed_tgo_roles = {"full_ad_card", "original_ad_slot"}
    rejected_banner_roles = {"landing_area", "browser_ui"}
    rejected_roles = rejected_banner_roles | ({"internal_image_asset"} if tgo else set())
    considered = 0
    rejected_by_role = 0

    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            continue
        role = _candidate_role(candidate)
        if role in rejected_roles or (tgo and role not in allowed_tgo_roles):
            rejected_by_role += 1
            continue
        bbox_payload = candidate.get("bbox")
        if not isinstance(bbox_payload, dict):
            continue
        considered += 1
        candidate_payload = _candidate_payload_context(parent=payload, candidate=candidate)
        bbox, bbox_metadata = _coerce_bbox_to_source_pixels(
            bbox_payload=bbox_payload,
            payload=candidate_payload,
            source_image_width=source_image_width,
            source_image_height=source_image_height,
        )
        confidence = _normalize_confidence(_first_present(candidate.get("confidence"), payload.get("confidence")))
        role_rank = _candidate_role_rank(role, tgo=tgo)
        aspect_penalty = _candidate_aspect_penalty(bbox=bbox, spec=spec)
        size_penalty = _candidate_size_penalty(bbox=bbox, source_image_width=source_image_width, source_image_height=source_image_height)
        position_penalty = _candidate_position_penalty(bbox=bbox, spec=spec, source_image_width=source_image_width, source_image_height=source_image_height)
        sort_key = (role_rank, -confidence, aspect_penalty, size_penalty, position_penalty, float(index))
        selection_metadata = {
            "candidates_count": len(candidates),
            "candidate_index": index,
            "candidate_role": role,
            "candidate_selection": "selected",
            "candidate_rejected_by_role_count": rejected_by_role,
            "raw_confidence": confidence,
        }
        if best is None or sort_key < best[0]:
            best = (sort_key, bbox, confidence, bbox_metadata, selection_metadata)

    if best is None:
        return None, 0.0, {}, {"candidates_count": len(candidates), "candidate_selection": "none", "candidate_considered_count": considered, "candidate_rejected_by_role_count": rejected_by_role}
    return best[1], best[2], best[3], best[4]


def _candidate_payload_context(*, parent: dict[str, object], candidate: dict[str, object]) -> dict[str, object]:
    context: dict[str, object] = {}
    for key in ("coordinate_space", "image_width", "image_height", "image_size"):
        if key in parent:
            context[key] = parent[key]
    context.update(candidate)
    return context


def _candidate_role(candidate: dict[str, object]) -> str:
    for key in ("role", "label", "type"):
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            return re.sub(r"[^a-z0-9_]+", "_", value.strip().lower()).strip("_") or "unknown"
    return "unknown"


def _candidate_role_rank(role: str, *, tgo: bool) -> float:
    if tgo:
        return {"full_ad_card": 0.0, "original_ad_slot": 0.2}.get(role, 10.0)
    return {"full_ad_card": 0.0, "original_ad_slot": 0.1, "unknown": 1.0, "internal_image_asset": 2.0}.get(role, 3.0)


def _candidate_aspect_penalty(*, bbox: BoundingBox, spec: CreativeSpec | None) -> float:
    if spec is None or not spec.width or not spec.height or bbox.height <= 0:
        return 0.0
    expected = spec.width / spec.height
    actual = bbox.width / bbox.height
    return abs(actual - expected) / expected if expected > 0 else 0.0


def _candidate_size_penalty(*, bbox: BoundingBox, source_image_width: int, source_image_height: int) -> float:
    if source_image_width <= 0 or source_image_height <= 0:
        return 0.0
    area_ratio = bbox.width * bbox.height / (source_image_width * source_image_height)
    if bbox.width < 16 or bbox.height < 16:
        return 1.0
    if area_ratio < 0.001:
        return 0.5
    return 0.0


def _candidate_position_penalty(*, bbox: BoundingBox, spec: CreativeSpec | None, source_image_width: int, source_image_height: int) -> float:
    if spec is None or source_image_width <= 0 or source_image_height <= 0:
        return 0.0
    hint = spec.placement_hint.lower()
    center_x = (bbox.x + bbox.width / 2) / source_image_width
    center_y = (bbox.y + bbox.height / 2) / source_image_height
    if "right" in hint or "sidebar" in hint:
        return abs(center_x - 0.80)
    if "left" in hint:
        return abs(center_x - 0.20)
    if "top" in hint or "header" in hint:
        return center_y
    if "bottom" in hint or "footer" in hint:
        return abs((bbox.bottom / source_image_height) - 1.0)
    return 0.0


def _composition_validation_prompt(
    *,
    screenshot: SourceScreenshotInfo,
    spec: CreativeSpec,
    current_banner_bbox: BoundingBox,
    ocr_image_width: int,
    ocr_image_height: int,
    validation_attempt: int,
    crop: CompositionValidationCrop | None = None,
) -> str:
    crop_instruction = ""
    if crop is not None:
        if crop.x == 0 and crop.y == 0 and crop.width == screenshot.width and crop.height == screenshot.height:
            crop_instruction = (
                "Important crop context: the composition-validation crop/preparation pipeline is active, but this crop is 100% of the image. "
                "You receive the FULL original source screenshot and the FULL composed screenshot with the inserted creative; no visual content has been removed. "
                f"The crop rectangle in full source pixels is x={crop.x}, y={crop.y}, width={crop.width}, height={crop.height}, right={crop.right}, bottom={crop.bottom}; crop origin is ({crop.x},{crop.y}). "
                f"The current insertion bbox in crop-local pixels is x={current_banner_bbox.x - crop.x}, y={current_banner_bbox.y - crop.y}, width={current_banner_bbox.width}, height={current_banner_bbox.height}. "
                "Return full source screenshot pixel coordinates whenever possible and set coordinate_space='source_pixels'. "
                "If you return crop-local pixel coordinates, set coordinate_space='crop_pixels'; because this is a 100% crop with origin (0,0), crop-local pixels equal full source pixels. "
            )
        else:
            crop_instruction = (
                "Important crop context: you receive CROPPED images around the current banner candidate, not full screenshots. "
                f"The crop rectangle in full source pixels is x={crop.x}, y={crop.y}, width={crop.width}, height={crop.height}, right={crop.right}, bottom={crop.bottom}; crop origin is ({crop.x},{crop.y}). "
                f"The current insertion bbox in crop-local pixels is x={current_banner_bbox.x - crop.x}, y={current_banner_bbox.y - crop.y}, width={current_banner_bbox.width}, height={current_banner_bbox.height}. "
                "Return full source screenshot pixel coordinates whenever possible and set coordinate_space='source_pixels'. "
                "If you can only return crop-local pixel coordinates, set coordinate_space='crop_pixels' and keep image_width/image_height equal to the crop image size so the caller can convert using the crop origin. "
            )
    else:
        crop_instruction = "Important image context: you receive the FULL original source screenshot and the FULL composed screenshot with the inserted creative; no composition-validation crop has been applied. "
    return (
        "You are validating an advertising proof image after creative insertion. "
        "You receive two images in this order: Image 1 is the original source screenshot BEFORE insertion; Image 2 is the composed screenshot AFTER the uploaded creative was inserted. "
        f"{crop_instruction}"
        "Compare the two images and verify whether the inserted creative fully and exactly covers the original ad banner/card area with pixel-level alignment: no x/y offset, incomplete coverage, overflow, extra margins, or width/height mismatch. "
        f"Current insertion bbox in source screenshot pixels: x={current_banner_bbox.x}, y={current_banner_bbox.y}, width={current_banner_bbox.width}, height={current_banner_bbox.height}. "
        f"Selected spec dimensions: {spec.dimensions_label}; expected aspect ratio: {_safe_aspect_ratio(spec)}; selected spec slug: {spec.slug}; network: {spec.network_display_name}. "
        f"Validation cycle: {max(1, validation_attempt)}. Source screenshot is {screenshot.width}x{screenshot.height} pixels; OCR input images sent to you are {ocr_image_width}x{ocr_image_height} pixels. "
        "Return ONLY strict JSON with keys: verification_passed boolean, original_banner_bbox {x,y,width,height}, actual_inserted_creative_bbox {x,y,width,height}, deltas {x,y,width,height}, corner_deltas, recommended_corrected_bbox {x,y,width,height} or null, confidence 0..1, reason string, coordinate_space, image_width, image_height. "
        "corner_deltas MUST be an object with exactly these corner keys: top_left, top_right, bottom_right, bottom_left. Each corner value MUST be an object with deterministic numeric dx and dy fields. "
        "For each corner, dx and dy describe how to move that INSERTED CREATIVE corner so it glues precisely to the matching ORIGINAL SCREENSHOT ad-slot corner: dx = original_corner_x - inserted_creative_corner_x; dy = original_corner_y - inserted_creative_corner_y. "
        "Example zero-alignment corner_deltas: {\"top_left\":{\"dx\":0,\"dy\":0},\"top_right\":{\"dx\":0,\"dy\":0},\"bottom_right\":{\"dx\":0,\"dy\":0},\"bottom_left\":{\"dx\":0,\"dy\":0}}. "
        "Use source screenshot pixel coordinates whenever possible and set coordinate_space='source_pixels'. If you return crop-local coordinates, set coordinate_space='crop_pixels'. If you return resized OCR-image pixels, set coordinate_space='resized_pixels' and include image_width and image_height. "
        "If verification_passed is false, recommended_corrected_bbox must be the exact pixel bbox to use for the next insertion attempt."
    )


def _first_present(*values: object) -> object:
    for value in values:
        if value is not None:
            return value
    return None


def _require_number(container: dict[str, object], key: str) -> float:
    value = container[key]
    if isinstance(value, bool):
        raise GemmaClientError(f"OCR LLM bbox field {key} must be numeric")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise GemmaClientError(f"OCR LLM bbox field {key} must be numeric") from exc


def _extract_coordinate_space(payload: dict[str, object], bbox_payload: dict[str, object]) -> str:
    for container in (payload, bbox_payload):
        value = container.get("coordinate_space")
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return "source_pixels"


def _has_explicit_coordinate_space(payload: dict[str, object], bbox_payload: dict[str, object]) -> bool:
    for container in (payload, bbox_payload):
        value = container.get("coordinate_space")
        if isinstance(value, str) and value.strip():
            return True
    return False


def _extract_coordinate_image_size(payload: dict[str, object], bbox_payload: dict[str, object]) -> tuple[int | None, int | None]:
    for container in (payload, bbox_payload):
        width = _optional_positive_int(container.get("image_width"))
        height = _optional_positive_int(container.get("image_height"))
        if width and height:
            return width, height
        image_size = container.get("image_size")
        if isinstance(image_size, dict):
            width = _optional_positive_int(image_size.get("width"))
            height = _optional_positive_int(image_size.get("height"))
            if width and height:
                return width, height
    return None, None


def _optional_positive_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _looks_normalized(*, x: float, y: float, width: float, height: float) -> bool:
    return 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 and 0.0 < width <= 1.0 and 0.0 < height <= 1.0


def _bbox_from_normalized(
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    source_image_width: int,
    source_image_height: int,
) -> BoundingBox:
    return _bbox_from_edges(
        left=x * source_image_width,
        top=y * source_image_height,
        right=(x + width) * source_image_width,
        bottom=(y + height) * source_image_height,
    )


def _bbox_from_resized_pixels(
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    coordinate_image_width: int,
    coordinate_image_height: int,
    source_image_width: int,
    source_image_height: int,
) -> BoundingBox:
    scale_x = source_image_width / coordinate_image_width
    scale_y = source_image_height / coordinate_image_height
    return _bbox_from_edges(
        left=x * scale_x,
        top=y * scale_y,
        right=(x + width) * scale_x,
        bottom=(y + height) * scale_y,
    )


def _bbox_from_crop_pixels(*, x: float, y: float, width: float, height: float, crop: CompositionValidationCrop) -> BoundingBox:
    return _bbox_from_edges(
        left=crop.x + x,
        top=crop.y + y,
        right=crop.x + x + width,
        bottom=crop.y + y + height,
    )


def _bbox_from_edges(*, left: float, top: float, right: float, bottom: float) -> BoundingBox:
    x = int(round(left))
    y = int(round(top))
    right_px = int(round(right))
    bottom_px = int(round(bottom))
    return BoundingBox(
        x=x,
        y=y,
        width=max(1, right_px - x),
        height=max(1, bottom_px - y),
    )


def _normalize_confidence(value: object) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    if 1.0 < confidence <= 100.0:
        confidence /= 100.0
    return max(0.0, min(confidence, 1.0))


def _safe_bbox(bbox: BoundingBox | None) -> str:
    if bbox is None:
        return "none"
    return f"x={bbox.x},y={bbox.y},width={bbox.width},height={bbox.height}"


def _safe_deltas(deltas: dict[str, int]) -> str:
    return ",".join(f"{key}={int(deltas.get(key, 0))}" for key in ("x", "y", "width", "height"))


def _safe_corner_deltas(corner_deltas: dict[str, dict[str, int]]) -> str:
    parts = []
    for corner in ("top_left", "top_right", "bottom_right", "bottom_left"):
        delta = corner_deltas.get(corner, {})
        parts.append(f"{corner}.dx={int(delta.get('dx', 0))},{corner}.dy={int(delta.get('dy', 0))}")
    return ";".join(parts)


def _safe_response_metadata(payload: object) -> str:
    if not isinstance(payload, dict):
        return "payload_type=non_object"
    choices = payload.get("choices")
    usage = payload.get("usage")
    choice_count = len(choices) if isinstance(choices, list) else 0
    usage_keys = sorted(usage) if isinstance(usage, dict) else []
    return f"choice_count={choice_count} usage_keys={','.join(str(key) for key in usage_keys)[:80]}"


def _safe_bbox_metadata(raw_metadata: dict[str, object]) -> str:
    coordinate_space = _safe_coordinate_space(raw_metadata)
    coordinate_image_width = raw_metadata.get("coordinate_image_width")
    coordinate_image_height = raw_metadata.get("coordinate_image_height")
    scaled_to_source = bool(raw_metadata.get("scaled_to_source", False))
    candidate_role = _sanitize_log_text(str(raw_metadata.get("candidate_role", "none")), limit=48)
    raw_bbox = _sanitize_log_text(str(raw_metadata.get("raw_bbox", "none")), limit=120)
    return (
        f"coordinate_space={coordinate_space} "
        f"coordinate_image_width={coordinate_image_width} "
        f"coordinate_image_height={coordinate_image_height} "
        f"candidate_role={candidate_role} "
        f"raw_bbox={raw_bbox} "
        f"scaled_to_source={scaled_to_source}"
    )


def _safe_raw_bbox_payload_fields(payload: dict[str, object]) -> str:
    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        parts = []
        for index, candidate in enumerate(candidates[:6]):
            if not isinstance(candidate, dict):
                continue
            bbox_payload = candidate.get("bbox")
            parts.append(
                "index={index},role={role},coordinate_space={space},image_width={width},image_height={height},confidence={confidence},bbox={bbox}".format(
                    index=index,
                    role=_sanitize_log_text(_candidate_role(candidate), limit=48),
                    space=_sanitize_log_text(_extract_coordinate_space(_candidate_payload_context(parent=payload, candidate=candidate), bbox_payload if isinstance(bbox_payload, dict) else {}), limit=32),
                    width=_optional_positive_int(candidate.get("image_width")) or _optional_positive_int(payload.get("image_width")),
                    height=_optional_positive_int(candidate.get("image_height")) or _optional_positive_int(payload.get("image_height")),
                    confidence=_normalize_confidence(_first_present(candidate.get("confidence"), payload.get("confidence"))),
                    bbox=_sanitize_log_text(_safe_raw_bbox_container(bbox_payload), limit=100),
                )
            )
        return ";".join(parts) if parts else "candidates=empty_or_invalid"
    bbox_payload = payload.get("bbox")
    return "role=single,coordinate_space={space},image_width={width},image_height={height},confidence={confidence},bbox={bbox}".format(
        space=_sanitize_log_text(_extract_coordinate_space(payload, bbox_payload if isinstance(bbox_payload, dict) else {}), limit=32),
        width=_optional_positive_int(payload.get("image_width")),
        height=_optional_positive_int(payload.get("image_height")),
        confidence=_normalize_confidence(payload.get("confidence")),
        bbox=_sanitize_log_text(_safe_raw_bbox_container(bbox_payload), limit=100),
    )


def _safe_raw_bbox_container(value: object) -> str:
    if not isinstance(value, dict):
        return "none"
    keys = ("x", "y", "width", "height")
    return ",".join(f"{key}={_sanitize_log_text(str(value.get(key, '')), limit=24)}" for key in keys)


def _safe_raw_bbox_values(*, x: float, y: float, width: float, height: float) -> str:
    return f"x={x:.6g},y={y:.6g},width={width:.6g},height={height:.6g}"


def _ocr_error_reason_code(exc: BaseException) -> str:
    message = str(exc).lower()
    if "invalid_coordinate_space" in message:
        return "invalid_coordinate_space"
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 413:
        return "ocr_payload_too_large"
    if isinstance(exc, (ValueError, TypeError, KeyError, GemmaClientError)):
        return "invalid_ocr_response"
    if isinstance(exc, httpx.TimeoutException):
        return "ocr_timeout"
    return "ocr_request_failed"


def _safe_coordinate_space(raw_metadata: dict[str, object]) -> str:
    return _sanitize_log_text(str(raw_metadata.get("coordinate_space", "source_pixels")), limit=32)


def _safe_aspect_ratio(spec: CreativeSpec) -> str:
    if not spec.width or not spec.height:
        return "unknown"
    return f"{spec.width / spec.height:.4f}"


def _is_tgo_spec(spec: CreativeSpec) -> bool:
    text = " ".join((spec.slug, spec.format_slug, spec.creative_type_slug, spec.display_name)).lower()
    return "tgo" in text or "text-image" in text or "тго" in text


def _sha256_short(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def _detailed_ocr_logging_enabled() -> bool:
    return os.getenv(DETAILED_OCR_LOGGING_ENV, "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _ocr_log_text_max_chars() -> int:
    raw = os.getenv(DETAILED_OCR_LOGGING_MAX_CHARS_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_LOG_TEXT_CHARS
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_MAX_LOG_TEXT_CHARS


def _sanitize_log_text(value: str, *, limit: int) -> str:
    compact = re.sub(r"\s+", " ", value or "").strip()
    redacted = re.sub(
        r"(?i)\b(authorization|authorisation|password|passwd|secret|token|api[_-]?key|cookie)\b\s*[:=]\s*(?:Basic|Bearer)\s+[^\s,;\"'}]+",
        r"\1=<redacted>",
        compact,
    )
    redacted = re.sub(
        r"(?i)\b(authorization|authorisation|password|passwd|secret|token|api[_-]?key|cookie)\b\s*[:=]\s*[^\s,;\"'}]+",
        r"\1=<redacted>",
        redacted,
    )
    redacted = re.sub(r"(?i)\b(Basic|Bearer)\s+[A-Za-z0-9._~+/=-]+", r"\1 <redacted>", redacted)
    if limit <= 0:
        return redacted
    if len(redacted) <= limit:
        return redacted
    return f"{redacted[:limit]}…[truncated:{len(redacted)}]"


def _safe_exception_message(exc: BaseException) -> str:
    return _sanitize_log_text(str(exc).replace("\n", " "), limit=200)
