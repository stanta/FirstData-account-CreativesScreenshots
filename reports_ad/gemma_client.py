from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Protocol

import httpx

from .config import BotConfig
from .models import BBoxDetectionResult, BoundingBox, CreativeSpec, SourceScreenshotInfo
from .validation import ValidationError

logger = logging.getLogger(__name__)
DETAILED_OCR_LOGGING_ENV = "REPORTS_AD_OCR_LOG_TEXT"
MAX_LOG_TEXT_CHARS = 700


class GemmaClientError(RuntimeError):
    """Retryable or final OCR LLM client error."""


class GemmaBBoxClient(Protocol):
    async def detect_banner_bbox(self, *, screenshot: SourceScreenshotInfo, spec: CreativeSpec, request_id: str) -> BBoxDetectionResult: ...

    async def detect_landing_bbox(self, *, screenshot: SourceScreenshotInfo, spec: CreativeSpec, request_id: str) -> BBoxDetectionResult: ...


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
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._authorization_header = authorization_header
        self._timeout_seconds = timeout_seconds
        self._max_retries = max(1, max_retries)
        self._confidence_threshold = confidence_threshold

    async def detect_banner_bbox(self, *, screenshot: SourceScreenshotInfo, spec: CreativeSpec, request_id: str) -> BBoxDetectionResult:
        prompt = _banner_prompt(screenshot=screenshot, spec=spec)
        return await self._detect_bbox(stage="banner_bbox", screenshot=screenshot, spec=spec, request_id=request_id, prompt=prompt)

    async def detect_landing_bbox(self, *, screenshot: SourceScreenshotInfo, spec: CreativeSpec, request_id: str) -> BBoxDetectionResult:
        prompt = _landing_prompt(screenshot=screenshot, spec=spec)
        return await self._detect_bbox(stage="landing_bbox", screenshot=screenshot, spec=spec, request_id=request_id, prompt=prompt)

    async def _detect_bbox(
        self,
        *,
        stage: str,
        screenshot: SourceScreenshotInfo,
        spec: CreativeSpec,
        request_id: str,
        prompt: str,
    ) -> BBoxDetectionResult:
        if not self._base_url:
            raise GemmaClientError("OCR LLM endpoint is not configured")

        last_error: BaseException | None = None
        for attempt in range(1, self._max_retries + 1):
            logger.info(
                "ocr_llm_request request_id=%s stage=%s attempt=%s model=%s spec_slug=%s spec_name=%s screenshot=%sx%s expected_dimensions=%s expected_aspect_ratio=%s placement_hint_present=%s placement_hint=%s prompt_length=%s prompt_sha256=%s prompt_text=%s",
                request_id,
                stage,
                attempt,
                self._model,
                spec.slug,
                _sanitize_log_text(spec.display_name, limit=120),
                screenshot.width,
                screenshot.height,
                spec.dimensions_label,
                _safe_aspect_ratio(spec),
                bool(spec.position_hint.strip()),
                _sanitize_log_text(spec.placement_hint, limit=180),
                len(prompt),
                _sha256_short(prompt),
                _safe_debug_text(prompt),
            )
            try:
                payload = await self._execute_request(prompt=prompt, image_path=screenshot.path, mime_type=screenshot.mime_type)
                raw_text = _extract_openai_compatible_text(payload)
                parsed = _loads_json_object(raw_text)
                result = _parse_bbox_result(
                    parsed,
                    stage=stage,
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
                    _safe_debug_text(raw_text),
                )
                return result
            except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError, GemmaClientError) as exc:
                last_error = exc
                logger.warning(
                    "ocr_llm_response request_id=%s stage=%s attempt=%s status=retryable_error parse_success=%s error_class=%s error_message=%s",
                    request_id,
                    stage,
                    attempt,
                    False,
                    type(exc).__name__,
                    _safe_exception_message(exc),
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(min(2 ** (attempt - 1), 8))

        raise GemmaClientError(f"OCR LLM failed after {self._max_retries} attempts: {type(last_error).__name__ if last_error else 'unknown'}") from last_error

    async def _execute_request(self, *, prompt: str, image_path: Path, mime_type: str) -> object:
        headers = {"Content-Type": "application/json"}
        if self._authorization_header:
            headers["Authorization"] = self._authorization_header
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.post(
                f"{self._base_url}/chat/completions",
                headers=headers,
                json={
                    "model": self._model,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": _data_url(image_path=image_path, mime_type=mime_type)}},
                            ],
                        }
                    ],
                    "response_format": {"type": "json_object"},
                },
            )
            response.raise_for_status()
            return response.json()


def build_gemma_client_from_config(config: BotConfig | None) -> HeaderAuthGemmaClient:
    runtime_config = _require_ocr_runtime_config(config)
    return HeaderAuthGemmaClient(
        base_url=runtime_config.ocr_llm_base_url,
        model=runtime_config.ocr_llm_model,
        authorization_header=runtime_config.ocr_llm_authorization_header,
        timeout_seconds=runtime_config.ocr_llm_timeout_seconds,
        max_retries=runtime_config.ocr_llm_max_retries,
        confidence_threshold=runtime_config.ocr_llm_confidence_threshold,
    )


def _require_ocr_runtime_config(config: BotConfig | None) -> BotConfig:
    if config is None:
        raise ValidationError("Конфигурация Stage 2 OCR не инициализирована. Перезапустите бота и повторите попытку.")

    if not config.ocr_llm_base_url.strip():
        raise ValidationError("Stage 2 OCR не настроен: заполните REPORTS_AD_OCR_LLM_BASE_URL и перезапустите бота.")

    if not config.ocr_llm_model.strip():
        raise ValidationError("Stage 2 OCR не настроен: заполните REPORTS_AD_OCR_LLM_MODEL и перезапустите бота.")

    return config


def _banner_prompt(*, screenshot: SourceScreenshotInfo, spec: CreativeSpec) -> str:
    return (
        "You are analyzing a placement screenshot for an advertising proof generator. "
        "Find the visible original ad slot area that must be replaced by the uploaded creative. "
        "Use the selected network/spec context, expected dimensions, aspect ratio, and placement hint. "
        "You MUST identify the original ad slot matching this selected banner type and position hint; do NOT select the uploaded replacement creative, a floating preview, an editor overlay, or a landing/link/browser area. "
        "Return ONLY JSON with keys: bbox {x,y,width,height}, confidence 0..1, reason, coordinate_space, image_width, image_height. "
        "Preferred output: coordinate_space='normalized' with bbox values as 0..1 fractions of the full screenshot. "
        "If you return source screenshot pixel coordinates, set coordinate_space='source_pixels'. "
        "If you return pixel coordinates from any resized/preview image, set coordinate_space='resized_pixels' and include the exact image_width and image_height used for those coordinates. "
        f"The source screenshot is {screenshot.width}x{screenshot.height} pixels; bbox must map to this source screenshot and must never crop or reset the coordinate origin. "
        f"Selected spec slug: {spec.slug}. Selected spec name: {spec.display_name}. Network: {spec.network_display_name}. "
        f"Selected spec dimensions: {spec.dimensions_label}; expected aspect ratio: {_safe_aspect_ratio(spec)}. "
        f"Selected banner placement hint from catalog: {spec.placement_hint}"
    )


def _landing_prompt(*, screenshot: SourceScreenshotInfo, spec: CreativeSpec) -> str:
    return (
        "You are analyzing a placement proof screenshot. Find the browser/link/landing area template location if it is visible, "
        "usually a lower 10-20 percent area with browser header/address line. "
        "If it is not visible, return bbox null and confidence 0. "
        "Return ONLY JSON with keys: bbox {x,y,width,height} or null, confidence 0..1, reason, coordinate_space, image_width, image_height. "
        "Preferred output: coordinate_space='normalized' with bbox values as 0..1 fractions of the full screenshot. "
        "If you return pixel coordinates from any resized/preview image, set coordinate_space='resized_pixels' and include the exact image_width and image_height used for those coordinates. "
        f"The original screenshot is {screenshot.width}x{screenshot.height}; never crop the coordinate origin. "
        f"Selected spec: {spec.display_name}, dimensions {spec.dimensions_label}."
    )


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
) -> BBoxDetectionResult:
    confidence = _normalize_confidence(payload.get("confidence"))
    bbox_payload = payload.get("bbox")
    bbox = None
    raw_metadata: dict[str, object] = {"reason": str(payload.get("reason", ""))[:160]}
    if isinstance(bbox_payload, dict):
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


def _coerce_bbox_to_source_pixels(
    *,
    bbox_payload: dict[str, object],
    payload: dict[str, object],
    source_image_width: int,
    source_image_height: int,
) -> tuple[BoundingBox, dict[str, object]]:
    x = _require_number(bbox_payload, "x")
    y = _require_number(bbox_payload, "y")
    width = _require_number(bbox_payload, "width")
    height = _require_number(bbox_payload, "height")

    coordinate_space = _extract_coordinate_space(payload, bbox_payload)
    coordinate_image_width, coordinate_image_height = _extract_coordinate_image_size(payload, bbox_payload)

    metadata: dict[str, object] = {
        "coordinate_space": coordinate_space,
        "coordinate_image_width": coordinate_image_width,
        "coordinate_image_height": coordinate_image_height,
        "scaled_to_source": False,
    }

    if coordinate_space == "normalized" or _looks_normalized(x=x, y=y, width=width, height=height):
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

    if (
        coordinate_image_width
        and coordinate_image_height
        and (coordinate_image_width != source_image_width or coordinate_image_height != source_image_height)
    ):
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

    return BoundingBox(x=int(round(x)), y=int(round(y)), width=int(round(width)), height=int(round(height))), metadata


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
    return (
        f"coordinate_space={coordinate_space} "
        f"coordinate_image_width={coordinate_image_width} "
        f"coordinate_image_height={coordinate_image_height} "
        f"scaled_to_source={scaled_to_source}"
    )


def _safe_coordinate_space(raw_metadata: dict[str, object]) -> str:
    return _sanitize_log_text(str(raw_metadata.get("coordinate_space", "source_pixels")), limit=32)


def _safe_aspect_ratio(spec: CreativeSpec) -> str:
    if not spec.width or not spec.height:
        return "unknown"
    return f"{spec.width / spec.height:.4f}"


def _sha256_short(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def _safe_debug_text(value: str) -> str:
    if not _detailed_ocr_logging_enabled():
        return "disabled"
    return _sanitize_log_text(value, limit=MAX_LOG_TEXT_CHARS)


def _detailed_ocr_logging_enabled() -> bool:
    return os.getenv(DETAILED_OCR_LOGGING_ENV, "").strip().lower() in {"1", "true", "yes", "y", "on"}


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
    if len(redacted) <= limit:
        return redacted
    return f"{redacted[:limit]}…[truncated:{len(redacted)}]"


def _safe_exception_message(exc: BaseException) -> str:
    return _sanitize_log_text(str(exc).replace("\n", " "), limit=200)
