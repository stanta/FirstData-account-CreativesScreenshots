from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import base64


def _bool_from_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int_from_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True, slots=True)
class BotConfig:
    telegram_token: str
    headless: bool = True
    allow_local_urls: bool = False
    allow_local_creative_path: bool = False
    viewport_width: int = 1365
    viewport_height: int = 768
    max_creative_bytes: int = 10 * 1024 * 1024
    max_creative_pixels: int = 20_000_000
    max_screenshot_bytes: int = 10 * 1024 * 1024
    max_screenshot_pixels: int = 30_000_000
    request_timeout_seconds: int = 300
    page_load_timeout_ms: int = 12_000
    click_timeout_ms: int = 7_000
    screenshot_timeout_ms: int = 15_000
    timeout_diagnostics: bool = True
    timeout_diagnostics_timeout_ms: int = 5_000
    locale: str = "ru-RU"
    timezone_id: str = "Europe/Moscow"
    temp_root: Path | None = None
    ocr_llm_base_url: str = ""
    ocr_llm_model: str = "cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit"
    ocr_llm_authorization_header: str | None = None
    ocr_llm_timeout_seconds: float = 120.0
    ocr_llm_max_retries: int = 5
    ocr_llm_confidence_threshold: float = 0.45
    ocr_image_max_width: int = 1280
    ocr_image_max_pixels: int = 2_000_000
    composition_validation_crop_margin_percent: float = 0.20
    composition_validation_crop_margin_min_pixels: int = 60
    ocr_log_text: bool = False
    ocr_log_text_max_chars: int = 0

    @classmethod
    def from_env(cls) -> "BotConfig":
        token = os.getenv("REPORTS_AD_TELEGRAM_TOKEN", "").strip()
        if not token:
            raise RuntimeError("REPORTS_AD_TELEGRAM_TOKEN is required")

        temp_root_raw = os.getenv("REPORTS_AD_TEMP_ROOT", "").strip()
        temp_root = Path(temp_root_raw) if temp_root_raw else None

        return cls(
            telegram_token=token,
            headless=_bool_from_env("REPORTS_AD_HEADLESS", True),
            allow_local_urls=_bool_from_env("REPORTS_AD_ALLOW_LOCAL_URLS", False),
            allow_local_creative_path=_bool_from_env("REPORTS_AD_ALLOW_LOCAL_CREATIVE_PATH", False),
            viewport_width=_int_from_env("REPORTS_AD_VIEWPORT_WIDTH", 1365),
            viewport_height=_int_from_env("REPORTS_AD_VIEWPORT_HEIGHT", 768),
            max_creative_bytes=_int_from_env("REPORTS_AD_MAX_CREATIVE_BYTES", 10 * 1024 * 1024),
            max_creative_pixels=_int_from_env("REPORTS_AD_MAX_CREATIVE_PIXELS", 20_000_000),
            max_screenshot_bytes=_int_from_env("REPORTS_AD_MAX_SCREENSHOT_BYTES", 10 * 1024 * 1024),
            max_screenshot_pixels=_int_from_env("REPORTS_AD_MAX_SCREENSHOT_PIXELS", 30_000_000),
            request_timeout_seconds=_int_from_env("REPORTS_AD_REQUEST_TIMEOUT_SECONDS", 300),
            page_load_timeout_ms=_int_from_env("REPORTS_AD_PAGE_LOAD_TIMEOUT_MS", 12_000),
            click_timeout_ms=_int_from_env("REPORTS_AD_CLICK_TIMEOUT_MS", 7_000),
            screenshot_timeout_ms=_int_from_env("REPORTS_AD_SCREENSHOT_TIMEOUT_MS", 15_000),
            timeout_diagnostics=_bool_from_env("REPORTS_AD_TIMEOUT_DIAGNOSTICS", True),
            timeout_diagnostics_timeout_ms=_int_from_env("REPORTS_AD_TIMEOUT_DIAGNOSTICS_TIMEOUT_MS", 5_000),
            locale=os.getenv("REPORTS_AD_LOCALE", "ru-RU"),
            timezone_id=os.getenv("REPORTS_AD_TIMEZONE", "Europe/Moscow"),
            temp_root=temp_root,
            ocr_llm_base_url=os.getenv("OCR_LLM_BASE_URL", "").strip().rstrip("/"),
            ocr_llm_model=os.getenv("OCR_LLM_MODEL", "cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit").strip(),
            ocr_llm_authorization_header=_resolve_ocr_llm_authorization_header(),
            ocr_llm_timeout_seconds=float(os.getenv("OCR_LLM_TIMEOUT_SECONDS", "20")),
            ocr_llm_max_retries=_int_from_env("OCR_LLM_MAX_RETRIES", 5),
            ocr_llm_confidence_threshold=float(os.getenv("OCR_LLM_CONFIDENCE_THRESHOLD", "0.45")),
            ocr_image_max_width=_int_from_env("REPORTS_AD_OCR_IMAGE_MAX_WIDTH", 1280),
            ocr_image_max_pixels=_int_from_env("REPORTS_AD_OCR_IMAGE_MAX_PIXELS", 2_000_000),
            composition_validation_crop_margin_percent=float(os.getenv("REPORTS_AD_COMPOSITION_VALIDATION_CROP_MARGIN_PERCENT", "0.20")),
            composition_validation_crop_margin_min_pixels=_int_from_env("REPORTS_AD_COMPOSITION_VALIDATION_CROP_MARGIN_MIN_PIXELS", 60),
            ocr_log_text=_bool_from_env("REPORTS_AD_OCR_LOG_TEXT", False),
            ocr_log_text_max_chars=max(0, _int_from_env("REPORTS_AD_OCR_LOG_TEXT_MAX_CHARS", 0)),
        )


def _resolve_ocr_llm_authorization_header() -> str | None:
    explicit = os.getenv("OCR_LLM_HEADER_AUTHORISATION") or os.getenv("OCR_LLM_HEADER_AUTHORIZATION")
    if explicit and explicit.strip():
        return explicit.strip()
    user = os.getenv("OCR_LLM_USER")
    password = os.getenv("OCR_LLM_PASSWORD")
    if user and password:
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        return f"Basic {token}"
    return None
