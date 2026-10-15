"""VK-only Telegram conversation steps; legacy network handlers stay untouched."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path
from typing import Any

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import ContextTypes, MessageHandler, filters

from .models import CreativeInfo
from .validation import ValidationError, validate_creative_file, validate_minimum_age
from .vk_card import VkCardContent, list_vk_ctas, validate_vk_body, validate_vk_header

VK_LOGO, VK_HEADER, VK_BODY, VK_CTA, VK_AGE, VK_REVIEW = range(10, 16)
VK_BACK = "◀ Назад"
VK_CANCEL = "Отмена"
VK_APPROVE = "✅ Подтвердить и получить PNG"
VK_REFRESH = "Показать предпросмотр"
VK_EXPIRY_SECONDS = 1800
VK_EDIT_ACTIONS = {
    "Изменить креатив": ("creative", 2),
    "Изменить логотип": ("vk_logo", VK_LOGO),
    "Изменить верхний текст": ("vk_header_text", VK_HEADER),
    "Изменить основной текст": ("vk_body_text", VK_BODY),
    "Изменить кнопку": ("vk_cta_id", VK_CTA),
    "Изменить возраст": ("minimum_age", VK_AGE),
}


def states() -> dict[int, list[MessageHandler]]:
    image_or_text = (filters.PHOTO | filters.Document.IMAGE | filters.TEXT) & ~filters.COMMAND
    text = filters.TEXT & ~filters.COMMAND
    return {
        VK_LOGO: [MessageHandler(image_or_text, collect_logo)],
        VK_HEADER: [MessageHandler(text, collect_header)],
        VK_BODY: [MessageHandler(text, collect_body)],
        VK_CTA: [MessageHandler(text, collect_cta)],
        VK_AGE: [MessageHandler(text, collect_age)],
        VK_REVIEW: [MessageHandler(text, review)],
    }


def _keyboard(*values: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[label] for label in values], resize_keyboard=True)


def _navigation() -> ReplyKeyboardMarkup:
    return _keyboard(VK_BACK, VK_CANCEL)


def review_keyboard() -> ReplyKeyboardMarkup:
    return _keyboard(VK_APPROVE, VK_REFRESH, *VK_EDIT_ACTIONS.keys(), VK_CANCEL)


def _prompt(stage: int) -> str:
    return {
        VK_LOGO: "Загрузите логотип PNG/JPEG отдельным файлом (до 2 МБ).",
        VK_HEADER: "Введите верхний текст объявления одной строкой.",
        VK_BODY: "Введите весь основной текст под кнопкой. Абзацы и переносы сохраняются.",
        VK_CTA: "Выберите надпись кнопки:",
        VK_AGE: "Подтвердите возрастную пометку: введите число от 0 до 99 без + (например, 18).",
    }[stage]


def _cta_keyboard() -> ReplyKeyboardMarkup:
    return _keyboard(*list_vk_ctas().values(), VK_BACK, VK_CANCEL)


async def _ask(update: Update, stage: int) -> int:
    from .bot import _reply_text_with_retry

    await _reply_text_with_retry(
        update.effective_message, _prompt(stage),
        operation=f"vk.prompt.{stage}",
        reply_markup=_cta_keyboard() if stage == VK_CTA else _navigation(),
    )
    return stage


async def after_creative(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _invalidate(context.user_data)
    if context.user_data.pop("vk_edit_field", None) == "creative":
        return await _preview(update, context)
    return await _ask(update, VK_LOGO)


async def back_from_creative(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from .bot import BANNER_TYPE, _reply_text_with_retry, spec_menu_keyboard, spec_menu_prompt

    if context.user_data.pop("vk_edit_field", None) == "creative":
        return await _preview(update, context)
    network = context.user_data.get("network_slug", "vk")
    await _reply_text_with_retry(update.effective_message, spec_menu_prompt(network), operation="vk.creative.back", reply_markup=spec_menu_keyboard(network))
    return BANNER_TYPE


def _invalidate(data: dict[str, Any]) -> None:
    task = data.pop("vk_expiry_task", None)
    if isinstance(task, asyncio.Task) and not task.done():
        task.cancel()
    old_preview = data.pop("vk_preview_path", None)
    data.pop("vk_preview_revision", None)
    data.pop("vk_preview_sha256", None)
    data["vk_draft_revision"] = int(data.get("vk_draft_revision", 0)) + 1
    if isinstance(old_preview, Path):
        old_preview.unlink(missing_ok=True)


async def _on_nav(update: Update, context: ContextTypes.DEFAULT_TYPE, stage: int) -> int | None:
    choice = (update.effective_message.text or "").strip()
    if choice == VK_CANCEL:
        from .bot import cancel
        return await cancel(update, context)
    if choice != VK_BACK:
        return None
    if context.user_data.pop("vk_edit_field", None):
        return await _preview(update, context)
    previous = {VK_LOGO: 2, VK_HEADER: VK_LOGO, VK_BODY: VK_HEADER, VK_CTA: VK_BODY, VK_AGE: VK_CTA}[stage]
    if previous == 2:
        from .bot import CREATIVE, _reply_text_with_retry, creative_upload_prompt
        size = context.user_data["banner_size"]
        await _reply_text_with_retry(update.effective_message, creative_upload_prompt(size, "vk"), operation="vk.back.creative", reply_markup=_navigation())
        return CREATIVE
    return await _ask(update, previous)


async def _image_to_file(message: Any, context: ContextTypes.DEFAULT_TYPE) -> CreativeInfo:
    from .bot import _config, _ensure_temp_dir

    config = _config(context)
    base = _ensure_temp_dir(context.user_data, config)
    limit = min(config.max_creative_bytes, 2 * 1024 * 1024)
    source: Any
    if message.document:
        source = message.document
        suffix = Path(source.file_name or "logo.png").suffix.lower()
        mime = source.mime_type
    elif message.photo:
        source = message.photo[-1]
        suffix = ".jpg"
        mime = "image/jpeg"
    elif message.text and config.allow_local_creative_path:
        return validate_creative_file(Path(message.text.strip()), declared_mime_type=None, max_bytes=limit, max_pixels=1_000_000)
    else:
        raise ValidationError("Отправьте логотип как PNG/JPEG-фото или документ.")
    if source.file_size and source.file_size > limit:
        raise ValidationError("Размер логотипа превышает 2 МБ.")
    target = base / f"vk-logo-{uuid.uuid4().hex[:12]}{suffix}"
    try:
        remote_file = await source.get_file()
        await remote_file.download_to_drive(custom_path=target)
        return validate_creative_file(target, declared_mime_type=mime, max_bytes=limit, max_pixels=1_000_000)
    except Exception:
        target.unlink(missing_ok=True)
        raise


async def _accept(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str, value: Any, next_state: int) -> int:
    edit_target = context.user_data.pop("vk_edit_field", None)
    context.user_data[key] = value
    _invalidate(context.user_data)
    if edit_target == key:
        return await _preview(update, context)
    if next_state == VK_REVIEW:
        return await _preview(update, context)
    return await _ask(update, next_state)


async def collect_logo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    nav = await _on_nav(update, context, VK_LOGO)
    if nav is not None:
        return nav
    try:
        info = await _image_to_file(update.effective_message, context)
    except ValidationError as exc:
        from .bot import _reply_text_with_retry
        await _reply_text_with_retry(update.effective_message, exc.safe_message, operation="vk.logo.invalid", reply_markup=_navigation())
        return VK_LOGO
    return await _accept(update, context, "vk_logo", info, VK_HEADER)


async def collect_header(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    nav = await _on_nav(update, context, VK_HEADER)
    if nav is not None:
        return nav
    try:
        value = validate_vk_header(update.effective_message.text or "")
    except ValidationError as exc:
        from .bot import _reply_text_with_retry
        await _reply_text_with_retry(update.effective_message, exc.safe_message, operation="vk.header.invalid")
        return VK_HEADER
    return await _accept(update, context, "vk_header_text", value, VK_BODY)


async def collect_body(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    nav = await _on_nav(update, context, VK_BODY)
    if nav is not None:
        return nav
    try:
        value = validate_vk_body(update.effective_message.text or "")
    except ValidationError as exc:
        from .bot import _reply_text_with_retry
        await _reply_text_with_retry(update.effective_message, exc.safe_message, operation="vk.body.invalid")
        return VK_BODY
    return await _accept(update, context, "vk_body_text", value, VK_CTA)


async def collect_cta(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    nav = await _on_nav(update, context, VK_CTA)
    if nav is not None:
        return nav
    entered = (update.effective_message.text or "").strip()
    choices = list_vk_ctas()
    cta = entered if entered in choices else next((key for key, label in choices.items() if entered == label), "")
    if not cta:
        from .bot import _reply_text_with_retry
        await _reply_text_with_retry(update.effective_message, "Выберите надпись кнопки из меню.", operation="vk.cta.invalid", reply_markup=_cta_keyboard())
        return VK_CTA
    return await _accept(update, context, "vk_cta_id", cta, VK_AGE)


async def collect_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    nav = await _on_nav(update, context, VK_AGE)
    if nav is not None:
        return nav
    try:
        age = validate_minimum_age(update.effective_message.text or "")
    except ValidationError as exc:
        from .bot import _reply_text_with_retry
        await _reply_text_with_retry(update.effective_message, exc.safe_message, operation="vk.age.invalid")
        return VK_AGE
    return await _accept(update, context, "minimum_age", age, VK_REVIEW)


def build_content(data: dict[str, Any]) -> VkCardContent:
    return VkCardContent(
        logo=data["vk_logo"],
        header_text=str(data["vk_header_text"]),
        body_text=str(data["vk_body_text"]),
        cta_id=str(data["vk_cta_id"]),
    )


async def _expire_if_idle(data: dict[str, Any], request_id: str, expected_revision: int) -> None:
    try:
        await asyncio.sleep(VK_EXPIRY_SECONDS)
        if data.get("request_id") == request_id and data.get("vk_preview_revision") == expected_revision:
            from .bot import _cleanup_state
            await _cleanup_state(data)
            data.clear()
    except asyncio.CancelledError:
        return


async def _preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from .bot import (
        _build_masked_banner_request, _config, _ensure_temp_dir, _reply_photo_file_with_retry,
        _reply_text_with_retry, _user_lock,
    )
    from .masked_banner import generate_masked_banner
    from .proof import ScreenshotGenerationError

    data = context.user_data
    revision = int(data.get("vk_draft_revision", 0))
    path = data.get("vk_preview_path")
    if not (isinstance(path, Path) and path.is_file() and data.get("vk_preview_revision") == revision):
        user_id = update.effective_user.id if update.effective_user else 0
        lock = _user_lock(context, user_id)
        if lock.locked():
            await _reply_text_with_retry(update.effective_message, "Предпросмотр уже создаётся. Дождитесь результата.", operation="vk.preview.busy")
            return VK_REVIEW
        async with lock:
            try:
                output = _ensure_temp_dir(data, _config(context)) / f"vk-preview-r{revision}.png"
                request = _build_masked_banner_request(data, output)
                generator = context.bot_data.get("banner_generator") or generate_masked_banner
                async with asyncio.timeout(_config(context).request_timeout_seconds):
                    result = await generator(request)
                path = Path(result.output_path)
                if not path.is_file():
                    raise ScreenshotGenerationError("Предпросмотр ВК не был создан.")
                data["vk_preview_path"] = path
                data["vk_preview_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                data["vk_preview_revision"] = revision
            except (ValidationError, ScreenshotGenerationError, asyncio.TimeoutError, KeyError) as exc:
                error = exc.safe_message if hasattr(exc, "safe_message") else "Не удалось подготовить предпросмотр. Измените данные или попробуйте ещё раз."
                await _reply_text_with_retry(update.effective_message, error, operation="vk.preview.failed", reply_markup=review_keyboard())
                return VK_REVIEW
    if path is None:
        return VK_REVIEW
    await _reply_photo_file_with_retry(
        update.effective_message,
        operation="vk.preview.photo",
        photo_path=path,
        caption="Предпросмотр макета ВК. Измените поле или подтвердите PNG. Это не скриншот опубликованного объявления.",
        reply_markup=review_keyboard(),
    )
    previous = data.pop("vk_expiry_task", None)
    if isinstance(previous, asyncio.Task) and not previous.done():
        previous.cancel()
    data["vk_expiry_task"] = context.application.create_task(_expire_if_idle(data, str(data["request_id"]), revision))
    return VK_REVIEW


async def review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from .bot import _reply_text_with_retry, _reply_document_file_with_retry, _cleanup_state, _user_lock

    label = (update.effective_message.text or "").strip()
    if label == VK_CANCEL:
        from .bot import cancel
        return await cancel(update, context)
    if label in VK_EDIT_ACTIONS:
        field, target = VK_EDIT_ACTIONS[label]
        context.user_data["vk_edit_field"] = field
        if field == "creative":
            from .bot import CREATIVE, creative_upload_prompt
            await _reply_text_with_retry(update.effective_message, creative_upload_prompt(context.user_data["banner_size"], "vk"), operation="vk.edit.creative", reply_markup=_navigation())
            return CREATIVE
        return await _ask(update, target)
    if label == VK_REFRESH:
        return await _preview(update, context)
    if label != VK_APPROVE:
        await _reply_text_with_retry(update.effective_message, "Выберите действие с карточкой из меню.", operation="vk.review.invalid", reply_markup=review_keyboard())
        return VK_REVIEW

    data = context.user_data
    path = data.get("vk_preview_path")
    if not (isinstance(path, Path) and path.is_file() and data.get("vk_preview_revision") == data.get("vk_draft_revision")):
        await _reply_text_with_retry(update.effective_message, "Предпросмотр устарел. Сначала обновите его.", operation="vk.review.stale", reply_markup=review_keyboard())
        return VK_REVIEW
    if hashlib.sha256(path.read_bytes()).hexdigest() != data.get("vk_preview_sha256"):
        await _reply_text_with_retry(update.effective_message, "Контрольная сумма предпросмотра изменилась. Обновите его.", operation="vk.review.checksum", reply_markup=review_keyboard())
        return VK_REVIEW
    user_id = update.effective_user.id if update.effective_user else 0
    lock = _user_lock(context, user_id)
    if lock.locked():
        await _reply_text_with_retry(update.effective_message, "Отправка уже выполняется.", operation="vk.review.busy")
        return VK_REVIEW
    async with lock:
        try:
            await _reply_document_file_with_retry(
                update.effective_message,
                operation="vk.final.document", document_path=path,
                filename="vk-card-v1-approved.png",
                caption="Утверждённый PNG-макет карточки ВК (не подтверждение публикации).",
                reply_markup=ReplyKeyboardRemove(),
            )
        except Exception:
            await _reply_text_with_retry(update.effective_message, "Не удалось отправить PNG. Предпросмотр сохранён; нажмите подтверждение ещё раз.", operation="vk.final.send_failed", reply_markup=review_keyboard())
            return VK_REVIEW
    task = data.pop("vk_expiry_task", None)
    if isinstance(task, asyncio.Task) and not task.done():
        task.cancel()
    await _cleanup_state(data)
    data.clear()
    from .bot import ConversationHandler
    return ConversationHandler.END
