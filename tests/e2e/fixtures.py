from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from .assertions import redact_sensitive

REPO_ROOT = Path(__file__).resolve().parents[3]
REPORTS_AD_ROOT = REPO_ROOT / "reports_ad"
DEFAULT_ARTIFACT_DIR = REPORTS_AD_ROOT / "tmp" / "telegram-e2e-artifacts"

CREATIVE_240_400 = REPO_ROOT / "docs" / "reports" / "examples" / "FD -- Примеры скриншотов" / "Медийка Ферст Дата" / "240-400.png"

REQUIRED_ENV_VARS = (
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TELEGRAM_SESSION_STRING",
    "TELEGRAM_BOT_USERNAME",
)


@dataclass(frozen=True, slots=True)
class TelegramE2EConfig:
    api_id: int
    api_hash: str
    session_string: str
    bot_username: str
    timeout_seconds: float
    poll_interval_seconds: float
    artifact_dir: Path
    cleanup_messages: bool

    @property
    def secrets(self) -> tuple[str, ...]:
        return (str(self.api_id), self.api_hash, self.session_string)


def live_e2e_enabled() -> bool:
    return os.getenv("TELEGRAM_E2E", "").strip().lower() in {"1", "true", "yes", "y", "on"}


def require_live_e2e_config() -> TelegramE2EConfig:
    if not live_e2e_enabled():
        pytest.skip("live Telegram E2E is disabled; set TELEGRAM_E2E=1 to enable")

    missing = [name for name in REQUIRED_ENV_VARS if not os.getenv(name, "").strip()]
    if missing:
        pytest.skip(f"live Telegram E2E credentials are incomplete: {', '.join(missing)}")

    try:
        api_id = int(os.environ["TELEGRAM_API_ID"].strip())
    except ValueError:
        pytest.fail("TELEGRAM_API_ID must be an integer")

    artifact_dir = Path(os.getenv("TELEGRAM_E2E_ARTIFACT_DIR", str(DEFAULT_ARTIFACT_DIR))).expanduser().resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)

    return TelegramE2EConfig(
        api_id=api_id,
        api_hash=os.environ["TELEGRAM_API_HASH"].strip(),
        session_string=os.environ["TELEGRAM_SESSION_STRING"].strip(),
        bot_username=_normalize_bot_username(os.environ["TELEGRAM_BOT_USERNAME"].strip()),
        timeout_seconds=float(os.getenv("TELEGRAM_E2E_TIMEOUT", "180")),
        poll_interval_seconds=float(os.getenv("TELEGRAM_E2E_POLL_INTERVAL", "1.0")),
        artifact_dir=artifact_dir,
        cleanup_messages=os.getenv("TELEGRAM_E2E_CLEANUP", "1").strip().lower() not in {"0", "false", "no", "off"},
    )


def assert_fixture_files_exist() -> None:
    missing = [path for path in (CREATIVE_240_400,) if not path.exists()]
    if missing:
        pytest.fail("Missing E2E fixture files: " + redact_sensitive(", ".join(str(path) for path in missing)))


def _normalize_bot_username(value: str) -> str:
    if not value:
        pytest.fail("TELEGRAM_BOT_USERNAME must not be empty")
    return value if value.startswith("@") else f"@{value}"
