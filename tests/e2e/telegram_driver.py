from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from .assertions import assert_no_sensitive_diagnostics, redact_sensitive
from .fixtures import TelegramE2EConfig

try:  # pragma: no cover - exercised only when live E2E dependencies are installed.
    from pyrogram import Client
    from pyrogram.enums import ChatType
    from pyrogram.types import Message
except ImportError:  # pragma: no cover - validated by skipped default collection.
    Client = None  # type: ignore[assignment]
    ChatType = None  # type: ignore[assignment]
    Message = Any  # type: ignore[misc,assignment]


MessagePredicate = Callable[[Any], bool]


@dataclass(slots=True)
class TelegramE2EDiagnostics:
    action: str
    bot_username: str
    timeout_seconds: float
    seen_messages: list[str]
    last_error: str = ""

    def safe_text(self) -> str:
        joined = "\n".join(self.seen_messages[-12:])
        return redact_sensitive(
            f"action={self.action} bot={self.bot_username} timeout={self.timeout_seconds}s "
            f"last_error={self.last_error}\nrecent_messages:\n{joined}"
        )


class TelegramE2ETimeout(AssertionError):
    """Timeout with already-redacted Telegram E2E diagnostic context."""


class TelegramUserDriver:
    """Kurigram-backed external user driver for live Telegram E2E tests."""

    def __init__(self, config: TelegramE2EConfig) -> None:
        if Client is None:
            pytest.skip("Kurigram is not installed; install test extras with pip install -e '.[telegram-e2e]'")
        self.config = config
        self._client = Client(
            name="reports-ad-e2e",
            api_id=config.api_id,
            api_hash=config.api_hash,
            session_string=config.session_string,
            in_memory=True,
        )
        self._peer: Any | None = None
        self._started = False
        self._owned_message_ids: list[int] = []
        self._last_seen_message_id = 0

    async def __aenter__(self) -> "TelegramUserDriver":
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            await self.cleanup_messages()
        finally:
            await self.stop()

    async def start(self) -> None:
        if self._started:
            return
        await self._client.start()
        self._peer = await self._client.get_chat(self.config.bot_username)
        history = [message async for message in self._client.get_chat_history(self._peer.id, limit=1)]
        if history:
            self._last_seen_message_id = history[0].id
        self._started = True

    async def stop(self) -> None:
        if self._started:
            await self._client.stop()
            self._started = False

    async def send_text(self, text: str) -> Any:
        message = await self._client.send_message(self._peer.id, text)
        self._remember_message(message)
        return message

    async def send_document(self, path: Path, *, caption: str | None = None) -> Any:
        message = await self._client.send_document(self._peer.id, str(path), caption=caption)
        self._remember_message(message)
        return message

    async def wait_for_response(self, predicate: MessagePredicate, *, timeout: float | None = None, action: str = "wait_for_response") -> Any:
        deadline = time.monotonic() + (timeout or self.config.timeout_seconds)
        seen: list[str] = []
        last_error = ""
        while time.monotonic() < deadline:
            try:
                messages = await self._recent_bot_messages(limit=20)
                for message in messages:
                    summary = self._message_summary(message)
                    seen.append(summary)
                    if message.id > self._last_seen_message_id:
                        self._last_seen_message_id = message.id
                    if predicate(message):
                        self._remember_message(message)
                        return message
            except Exception as exc:  # pragma: no cover - live diagnostic path.
                last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(self.config.poll_interval_seconds)

        diagnostics = TelegramE2EDiagnostics(
            action=action,
            bot_username=self.config.bot_username,
            timeout_seconds=timeout or self.config.timeout_seconds,
            seen_messages=seen,
            last_error=last_error,
        ).safe_text()
        assert_no_sensitive_diagnostics(diagnostics, self.config.secrets)
        raise TelegramE2ETimeout(diagnostics)

    async def wait_for_text_contains(self, *needles: str, timeout: float | None = None, action: str | None = None) -> Any:
        lowered = tuple(needle.lower() for needle in needles)
        return await self.wait_for_response(
            lambda message: bool(message.text or message.caption) and all(needle in (message.text or message.caption or "").lower() for needle in lowered),
            timeout=timeout,
            action=action or f"wait_for_text_contains:{','.join(needles)}",
        )

    async def wait_for_photo(self, *, caption_contains: str | None = None, timeout: float | None = None) -> Any:
        return await self.wait_for_response(
            lambda message: bool(message.photo) and (caption_contains is None or caption_contains.lower() in (message.caption or "").lower()),
            timeout=timeout,
            action="wait_for_photo",
        )

    async def wait_for_document(self, *, caption_contains: str | None = None, timeout: float | None = None) -> Any:
        return await self.wait_for_response(
            lambda message: bool(message.document) and (caption_contains is None or caption_contains.lower() in (message.caption or "").lower()),
            timeout=timeout,
            action="wait_for_document",
        )

    async def download_media(self, message: Any, destination_dir: Path, *, filename: str | None = None) -> Path:
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / (filename or self._download_filename(message))
        downloaded = await self._client.download_media(message, file_name=str(destination))
        if not downloaded:
            raise AssertionError("Telegram media download returned no path")
        return Path(downloaded)

    async def cleanup_messages(self) -> None:
        if not self.config.cleanup_messages or not self._owned_message_ids:
            return
        unique_ids = sorted(set(self._owned_message_ids))
        try:
            await self._client.delete_messages(self._peer.id, unique_ids, revoke=True)
        except Exception:
            for message_id in unique_ids:
                try:
                    await self._client.delete_messages(self._peer.id, message_id, revoke=True)
                except Exception:
                    continue

    async def _recent_bot_messages(self, *, limit: int) -> list[Any]:
        messages = []
        async for message in self._client.get_chat_history(self._peer.id, limit=limit):
            if message.id <= self._last_seen_message_id:
                break
            if self._is_bot_message(message):
                messages.append(message)
        return list(reversed(messages))

    def _is_bot_message(self, message: Any) -> bool:
        sender = getattr(message, "from_user", None)
        if sender is not None and getattr(sender, "is_bot", False):
            return True
        chat = getattr(message, "chat", None)
        if ChatType is not None and chat is not None and getattr(chat, "type", None) == ChatType.BOT:
            return True
        return False

    def _remember_message(self, message: Any) -> None:
        message_id = getattr(message, "id", None)
        if isinstance(message_id, int):
            self._owned_message_ids.append(message_id)

    def _message_summary(self, message: Any) -> str:
        text = message.text or message.caption or ""
        media = "photo" if message.photo else "document" if message.document else "text"
        return redact_sensitive(f"id={message.id} media={media} text={text[:240]}")

    def _download_filename(self, message: Any) -> str:
        if message.document and getattr(message.document, "file_name", None):
            return str(message.document.file_name)
        if message.photo:
            return f"telegram-photo-{message.id}.jpg"
        return f"telegram-media-{message.id}"
