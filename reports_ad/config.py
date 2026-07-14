from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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
    request_timeout_seconds: int = 300
    page_load_timeout_ms: int = 12_000
    click_timeout_ms: int = 7_000
    screenshot_timeout_ms: int = 15_000
    timeout_diagnostics: bool = True
    timeout_diagnostics_timeout_ms: int = 5_000
    locale: str = "ru-RU"
    timezone_id: str = "Europe/Moscow"
    temp_root: Path | None = None

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
            request_timeout_seconds=_int_from_env("REPORTS_AD_REQUEST_TIMEOUT_SECONDS", 300),
            page_load_timeout_ms=_int_from_env("REPORTS_AD_PAGE_LOAD_TIMEOUT_MS", 12_000),
            click_timeout_ms=_int_from_env("REPORTS_AD_CLICK_TIMEOUT_MS", 7_000),
            screenshot_timeout_ms=_int_from_env("REPORTS_AD_SCREENSHOT_TIMEOUT_MS", 15_000),
            timeout_diagnostics=_bool_from_env("REPORTS_AD_TIMEOUT_DIAGNOSTICS", True),
            timeout_diagnostics_timeout_ms=_int_from_env("REPORTS_AD_TIMEOUT_DIAGNOSTICS_TIMEOUT_MS", 5_000),
            locale=os.getenv("REPORTS_AD_LOCALE", "ru-RU"),
            timezone_id=os.getenv("REPORTS_AD_TIMEZONE", "Europe/Moscow"),
            temp_root=temp_root,
        )
