from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from PIL import Image, UnidentifiedImageError

SENSITIVE_PLACEHOLDER = "<REDACTED>"

_BOT_TOKEN_RE = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b")
_SESSION_RE = re.compile(r"\b[A-Za-z0-9_-]{80,}={0,2}\b")
_CHAT_USER_ID_RE = re.compile(r"\b(?:chat|user|sender|from|peer)_?id\s*[=:]\s*-?\d+\b", re.IGNORECASE)
_FILE_ID_RE = re.compile(r"\b(?:file_id|file_unique_id)\s*[=:]\s*[A-Za-z0-9_-]+\b", re.IGNORECASE)
_BARE_LARGE_ID_RE = re.compile(r"(?<![\w-])-?\d{7,}(?![\w-])")
_URL_RE = re.compile(r"https?://[^\s)\]}>\"']+", re.IGNORECASE)
_UTM_KEY_RE = re.compile(r"(?:^|[?&#])utm_[A-Za-z0-9_]+=", re.IGNORECASE)


def redact_sensitive(value: object) -> str:
    """Redact Telegram secrets, IDs, file IDs, and full UTM URLs from diagnostics."""

    text = str(value)
    text = _URL_RE.sub(_redact_url_match, text)
    text = _BOT_TOKEN_RE.sub(SENSITIVE_PLACEHOLDER, text)
    text = _CHAT_USER_ID_RE.sub(lambda match: re.sub(r"-?\d+", SENSITIVE_PLACEHOLDER, match.group(0)), text)
    text = _FILE_ID_RE.sub(lambda match: match.group(0).split("=", 1)[0].split(":", 1)[0] + f"={SENSITIVE_PLACEHOLDER}", text)
    text = _SESSION_RE.sub(SENSITIVE_PLACEHOLDER, text)
    text = _BARE_LARGE_ID_RE.sub(SENSITIVE_PLACEHOLDER, text)
    return text


def _redact_url_match(match: re.Match[str]) -> str:
    url = match.group(0)
    if not _UTM_KEY_RE.search(url):
        return url
    try:
        split = urlsplit(url)
        safe_pairs = [(key, SENSITIVE_PLACEHOLDER if key.lower().startswith("utm_") else value) for key, value in parse_qsl(split.query, keep_blank_values=True)]
        safe_query = urlencode(safe_pairs, doseq=False)
        return urlunsplit((split.scheme, split.netloc, split.path, safe_query, ""))
    except ValueError:
        return SENSITIVE_PLACEHOLDER


def assert_no_sensitive_diagnostics(text: str, secrets: Iterable[str] = ()) -> None:
    """Fail if unredacted diagnostics contain known secrets or secret-shaped values."""

    for secret in secrets:
        if secret and secret in text:
            raise AssertionError("diagnostic text contains an unredacted known secret")
    if _BOT_TOKEN_RE.search(text):
        raise AssertionError("diagnostic text contains an unredacted bot token")
    if _SESSION_RE.search(text):
        raise AssertionError("diagnostic text contains an unredacted session-like string")
    if _FILE_ID_RE.search(text):
        raise AssertionError("diagnostic text contains an unredacted Telegram file id")
    if _UTM_KEY_RE.search(text) and SENSITIVE_PLACEHOLDER not in text:
        raise AssertionError("diagnostic text contains an unredacted full UTM URL")


def assert_safe_telegram_text(text: str) -> None:
    """Assert a user-visible Telegram message does not expose internals."""

    lowered = text.lower()
    forbidden_fragments = ("traceback", "exception", "file \"", "token", "api_hash", "session", "/tmp/reports-ad")
    leaked = [fragment for fragment in forbidden_fragments if fragment in lowered]
    if leaked:
        raise AssertionError(f"Telegram response exposes internal diagnostics: {leaked}")
    if _BOT_TOKEN_RE.search(text) or _FILE_ID_RE.search(text):
        raise AssertionError("Telegram response exposes Telegram secret material")


def assert_png_image(path: Path) -> tuple[int, int]:
    """Open a downloaded result and verify it is a non-empty PNG image."""

    if not path.exists() or not path.is_file():
        raise AssertionError(f"Expected PNG artifact does not exist: {path}")
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            if image.format != "PNG":
                raise AssertionError(f"Expected PNG artifact, got {image.format}")
            width, height = image.size
    except (UnidentifiedImageError, OSError) as exc:
        raise AssertionError(f"Downloaded artifact is not a readable PNG: {path}") from exc
    if width <= 0 or height <= 0:
        raise AssertionError(f"PNG artifact has invalid dimensions: {width}x{height}")
    return width, height
