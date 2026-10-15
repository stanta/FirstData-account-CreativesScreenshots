"""VK Telegram FSM tests using real handlers with fake Telegram updates."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from reports_ad import bot, vk_flow
from reports_ad.config import BotConfig
from reports_ad.mask_catalog import get_banner_size_for_network
from reports_ad.masked_banner import generate_masked_banner
from reports_ad.models import CreativeInfo


class FakeFile:
    def __init__(self, source: Path):
        self.source = source

    async def download_to_drive(self, *, custom_path: Path) -> None:
        Path(custom_path).write_bytes(self.source.read_bytes())


class FakeDocument:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.file_name = path.name
        self.file_size = path.stat().st_size
        self.mime_type = "image/png"

    async def get_file(self) -> FakeFile:
        return FakeFile(self.path)


class FakeApp:
    def create_task(self, coro):
        return asyncio.create_task(coro)


def message(text: str | None = None, document=None):
    return SimpleNamespace(text=text, photo=None, document=document)


def update(text: str | None = None, *, document=None, uid: int = 123):
    return SimpleNamespace(
        effective_message=message(text, document),
        effective_user=SimpleNamespace(id=uid),
        effective_chat=SimpleNamespace(id=uid),
    )


def image_info(tmp_path: Path, name: str, size: tuple[int, int], color: tuple[int, int, int]) -> CreativeInfo:
    p = tmp_path / name
    Image.new("RGB", size, color).save(p, "PNG")
    return CreativeInfo(p, "image/png", *size, p.stat().st_size)


@pytest.fixture
def harness(tmp_path: Path, monkeypatch):
    creative = image_info(tmp_path, "creative.png", (1080, 607), (50, 10, 130))
    logo = image_info(tmp_path, "brand.png", (64, 64), (17, 185, 146))
    size = get_banner_size_for_network("vk", "1080x607")
    assert size is not None
    data = {
        "request_id": "vk-unit",
        "network_slug": "vk", "network_display_name": "ВК",
        "banner_size": size, "banner_size_slug": size.slug, "creative": creative,
    }
    observations: dict[str, list] = {"texts": [], "photos": [], "documents": []}
    async def text_reply(_message, text, *, operation, **kw):
        observations["texts"].append((text, operation))
    async def photo_reply(_message, *, photo_path, operation, **kw):
        observations["photos"].append(hashlib.sha256(Path(photo_path).read_bytes()).hexdigest())
    async def doc_reply(_message, *, document_path, operation, **kw):
        observations["documents"].append(hashlib.sha256(Path(document_path).read_bytes()).hexdigest())
    monkeypatch.setattr(bot, "_reply_text_with_retry", text_reply)
    monkeypatch.setattr(bot, "_reply_photo_file_with_retry", photo_reply)
    monkeypatch.setattr(bot, "_reply_document_file_with_retry", doc_reply)
    context = SimpleNamespace(
        user_data=data,
        bot_data={"config": BotConfig(telegram_token="123456:TEST", temp_root=tmp_path),
                  "banner_generator": generate_masked_banner, "user_locks": {}},
        application=FakeApp(),
    )
    return context, observations, creative, logo


@pytest.mark.asyncio
async def test_vk_full_flow_edit_header_and_approve_same_png(harness):
    ctx, observed, creative, logo = harness
    assert await vk_flow.after_creative(update(), ctx) == vk_flow.VK_LOGO
    assert await vk_flow.collect_logo(update(document=FakeDocument(logo.path)), ctx) == vk_flow.VK_HEADER
    assert ctx.user_data["vk_logo"].path != creative.path
    assert await vk_flow.collect_header(update("Больше продаж за те же деньги"), ctx) == vk_flow.VK_BODY
    assert await vk_flow.collect_body(update("Вопрос?\n\nТекст с разными абзацами."), ctx) == vk_flow.VK_CTA
    assert await vk_flow.collect_cta(update("Подробнее"), ctx) == vk_flow.VK_AGE
    assert await vk_flow.collect_age(update("18"), ctx) == vk_flow.VK_REVIEW
    first_preview = ctx.user_data["vk_preview_path"]
    old_digest = ctx.user_data["vk_preview_sha256"]
    assert observed["photos"][-1] == old_digest
    assert await vk_flow.review(update("Изменить верхний текст"), ctx) == vk_flow.VK_HEADER
    assert await vk_flow.collect_header(update("Новая шапка объявления"), ctx) == vk_flow.VK_REVIEW
    approved_digest = ctx.user_data["vk_preview_sha256"]
    assert old_digest != approved_digest
    assert not first_preview.exists()
    assert observed["photos"][-1] == approved_digest
    assert await vk_flow.review(update(vk_flow.VK_APPROVE), ctx) == bot.ConversationHandler.END
    assert observed["documents"] == [approved_digest]
    assert not ctx.user_data


@pytest.mark.asyncio
async def test_vk_edit_body_uses_review_not_remaining_steps(harness, monkeypatch):
    ctx, _, _, logo = harness
    ctx.user_data.update(vk_logo=logo, vk_header_text="Заголовок", vk_body_text="Исходный текст",
                         vk_cta_id="learn_more", minimum_age="18", vk_draft_revision=4)
    assert await vk_flow._preview(update(), ctx) == vk_flow.VK_REVIEW
    original_logo_path = ctx.user_data["vk_logo"].path
    assert await vk_flow.review(update("Изменить основной текст"), ctx) == vk_flow.VK_BODY
    assert await vk_flow.collect_body(update("Изменённый текст"), ctx) == vk_flow.VK_REVIEW
    assert ctx.user_data["vk_logo"].path == original_logo_path
    assert ctx.user_data["vk_body_text"] == "Изменённый текст"
    await bot._cleanup_state(ctx.user_data)
    ctx.user_data.clear()


@pytest.mark.asyncio
async def test_vk_stale_preview_never_sent(harness):
    ctx, observed, _, logo = harness
    ctx.user_data.update(vk_logo=logo, vk_header_text="Заголовок", vk_body_text="Основной текст",
                         vk_cta_id="learn_more", minimum_age="18", vk_draft_revision=1)
    assert await vk_flow._preview(update(), ctx) == vk_flow.VK_REVIEW
    ctx.user_data["vk_draft_revision"] += 1
    assert await vk_flow.review(update(vk_flow.VK_APPROVE), ctx) == vk_flow.VK_REVIEW
    assert not observed["documents"]
    await bot._cleanup_state(ctx.user_data)
    ctx.user_data.clear()


@pytest.mark.asyncio
async def test_vk_invalid_logo_keeps_old_input_and_separate_files(harness, tmp_path):
    ctx, observed, creative, logo = harness
    ctx.user_data["vk_logo"] = logo
    invalid = tmp_path / "bad.png"
    invalid.write_bytes(b"not an image")
    assert await vk_flow.collect_logo(update(document=FakeDocument(invalid)), ctx) == vk_flow.VK_LOGO
    assert ctx.user_data["vk_logo"].path == logo.path
    assert creative.path.read_bytes() != b"not an image"
    assert logo.path.is_file()
    assert observed["texts"][-1][1] == "vk.logo.invalid"
    await bot._cleanup_state(ctx.user_data)
    ctx.user_data.clear()


@pytest.mark.asyncio
async def test_vk_send_failure_keeps_preview_for_retry(harness, monkeypatch):
    ctx, observed, _, logo = harness
    ctx.user_data.update(vk_logo=logo, vk_header_text="Заголовок", vk_body_text="Основной текст",
                         vk_cta_id="learn_more", minimum_age="18", vk_draft_revision=1)
    await vk_flow._preview(update(), ctx)
    approved_sha = ctx.user_data["vk_preview_sha256"]
    first = True
    async def flaky_send(_message, *, document_path, **kwargs):
        nonlocal first
        if first:
            first = False
            raise OSError("simulated delivery failure")
        observed["documents"].append(hashlib.sha256(Path(document_path).read_bytes()).hexdigest())
    monkeypatch.setattr(bot, "_reply_document_file_with_retry", flaky_send)
    assert await vk_flow.review(update(vk_flow.VK_APPROVE), ctx) == vk_flow.VK_REVIEW
    assert ctx.user_data["vk_preview_path"].exists()
    assert await vk_flow.review(update(vk_flow.VK_APPROVE), ctx) == bot.ConversationHandler.END
    assert observed["documents"] == [approved_sha]


@pytest.mark.asyncio
async def test_vk_idle_expiration_applies_before_preview_and_rejects_stale_reply(harness, monkeypatch):
    ctx, observed, _, _ = harness
    monkeypatch.setattr(vk_flow, "VK_EXPIRY_SECONDS", 0.01)
    temporary = bot._ensure_temp_dir(ctx.user_data, ctx.bot_data["config"])
    marker = temporary / "pending-logo-upload.png"
    marker.write_bytes(b"temporary")
    assert await vk_flow.after_creative(update(), ctx) == vk_flow.VK_LOGO
    assert "vk_expiry_task" in ctx.user_data
    await asyncio.sleep(0.05)
    assert not marker.exists()
    assert not ctx.user_data
    handler = vk_flow.states()[vk_flow.VK_LOGO][0].callback
    assert await handler(update("obsolete answer"), ctx) == bot.ConversationHandler.END
    assert observed["texts"][-1][1] == "vk.session.expired"


def test_vk_banner_size_menu_offers_both_creative_dimensions() -> None:
    buttons = bot.spec_menu_keyboard("vk").keyboard
    labels = [button.text for row in buttons for button in row]
    assert len(labels) == 2
    assert any("1080×607" in label for label in labels)
    assert any("600×600" in label for label in labels)
    assert bot._menu_value(next(label for label in labels if "600×600" in label)) == "600x600"


@pytest.mark.asyncio
async def test_vk_select_600x600_sets_square_upload_prompt(harness):
    ctx, observed, _, _ = harness
    label = next(
        button.text
        for row in bot.spec_menu_keyboard("vk").keyboard
        for button in row
        if "600×600" in button.text
    )
    assert await bot.collect_banner_type(update(label), ctx) == bot.CREATIVE
    size = ctx.user_data["banner_size"]
    assert size.slug == "600x600"
    assert size.width == size.height == 600
    assert "600×600" in observed["texts"][-1][0]
    assert "логотип" in observed["texts"][-1][0]


@pytest.mark.asyncio
async def test_vk_600x600_wizard_preview_edit_and_approve_unchanged(harness, tmp_path):
    ctx, observed, _, logo = harness
    square = image_info(tmp_path, "square600.png", (600, 600), (21, 63, 174))
    size = get_banner_size_for_network("vk", "600x600")
    assert size is not None
    ctx.user_data.update(
        banner_size=size, banner_size_slug=size.slug, creative=square,
    )
    assert await vk_flow.after_creative(update(), ctx) == vk_flow.VK_LOGO
    assert await vk_flow.collect_logo(update(document=FakeDocument(logo.path)), ctx) == vk_flow.VK_HEADER
    assert await vk_flow.collect_header(update("Шапка для квадрата"), ctx) == vk_flow.VK_BODY
    assert await vk_flow.collect_body(update("Текст объявления"), ctx) == vk_flow.VK_CTA
    assert await vk_flow.collect_cta(update("Подробнее"), ctx) == vk_flow.VK_AGE
    assert await vk_flow.collect_age(update("18"), ctx) == vk_flow.VK_REVIEW
    path = ctx.user_data["vk_preview_path"]
    with Image.open(path) as generated:
        assert generated.size == (1080, 1564)
    assert await vk_flow.review(update("Изменить основной текст"), ctx) == vk_flow.VK_BODY
    assert await vk_flow.collect_body(update("Исправленный текст"), ctx) == vk_flow.VK_REVIEW
    digest = ctx.user_data["vk_preview_sha256"]
    assert await vk_flow.review(update(vk_flow.VK_APPROVE), ctx) == bot.ConversationHandler.END
    assert observed["documents"] == [digest]
    assert not ctx.user_data
