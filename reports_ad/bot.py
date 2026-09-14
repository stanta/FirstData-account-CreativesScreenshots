from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any, Awaitable, BinaryIO, Callable

import httpx
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters
from telegram_menu import BaseMessage

from .config import BotConfig
from .mask_catalog import (
    BannerSize,
    get_banner_size_for_network,
    get_mask_network,
    list_banner_sizes_for_network,
    list_mask_networks,
    validate_banner_size_slug_for_network,
    validate_mask_network_slug,
)
from .masked_banner import MaskedBannerRequest, MaskedBannerResult, generate_masked_banner, _load_target_site_favicon
from .models import CreativeInfo
from .proof import ScreenshotGenerationError
from .validation import ValidationError, normalize_target_site_for_display, validate_creative_file, validate_minimum_age

logger = logging.getLogger(__name__)

NETWORK, BANNER_TYPE, CREATIVE, TARGET_SITE, FAVICON, MINIMUM_AGE = range(6)
BANNER_SIZE = BANNER_TYPE
SCREENSHOT, LANDING_URL, UTM, REVIEW = range(6, 10)
EDIT_TARGETS = {
    "network": NETWORK,
    "spec": BANNER_TYPE,
    "banner": BANNER_TYPE,
    "size": BANNER_TYPE,
    "creative": CREATIVE,
}
REVIEW_MENU_ACTIONS = {
    "Изменить рекламную сеть": "network",
    "Изменить размер баннера": "size",
    "Изменить креатив": "creative",
    "Запустить": "generate",
}
START_MENU_BUTTON = "Старт"

BannerGenerator = Callable[[MaskedBannerRequest], Awaitable[MaskedBannerResult]]
RawBannerGenerator = Callable[..., Awaitable[MaskedBannerResult]]

START_MENU_PROMPT = "Нажмите «Старт», чтобы запустить flow бота:"
NETWORK_MENU_PROMPT = "Выберите рекламную сеть кнопкой меню telegram_menu:"
SPEC_MENU_PROMPT = "Теперь выберите размер баннера кнопкой меню telegram_menu:"
REVIEW_MENU_PROMPT = "Выберите действие кнопкой меню telegram_menu:"
TELEGRAM_KEYBOARD_BUTTON_TEXT_LIMIT = 64
TELEGRAM_SEND_MAX_ATTEMPTS = 5
TELEGRAM_SEND_INITIAL_BACKOFF_SECONDS = 0.25
TELEGRAM_SEND_MAX_BACKOFF_SECONDS = 2.0
TELEGRAM_SAFE_MESSAGE_REDACTIONS = (
    (re.compile(r"bot[0-9]+:[A-Za-z0-9_-]+"), "bot<REDACTED>"),
    (re.compile(r"https?://[^/?\s\"']+[^\s\"']*"), "<url-redacted>"),
)
TELEGRAM_RETRYABLE_EXCEPTIONS = (
    TimedOut,
    NetworkError,
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.NetworkError,
)


class ReportsAdMenuMessage(BaseMessage):
    """Small telegram_menu message adapter used to build reply-keyboard menus."""

    def update(self, context: ContextTypes.DEFAULT_TYPE | None = None) -> str:
        return ""


def build_application(
    config: BotConfig | None = None,
    banner_generator: RawBannerGenerator = generate_masked_banner,
    **legacy_kwargs: RawBannerGenerator,
) -> Application:
    if "proof_generator" in legacy_kwargs:
        banner_generator = legacy_kwargs["proof_generator"]
    runtime_config = config or BotConfig.from_env()
    application = ApplicationBuilder().token(runtime_config.telegram_token).build()
    application.bot_data["config"] = runtime_config
    application.bot_data["banner_generator"] = _configure_banner_generator(banner_generator, runtime_config)
    application.bot_data["user_locks"] = {}

    conversation = ConversationHandler(
        entry_points=[CommandHandler("start", start), CommandHandler("help", start), _start_button_handler()],
        states={
            NETWORK: [_start_button_handler(), MessageHandler(filters.TEXT & ~filters.COMMAND, collect_network)],
            BANNER_TYPE: [_start_button_handler(), MessageHandler(filters.TEXT & ~filters.COMMAND, collect_banner_type)],
            CREATIVE: [_start_button_handler(), MessageHandler((filters.PHOTO | filters.Document.IMAGE | filters.TEXT) & ~filters.COMMAND, collect_creative)],
            TARGET_SITE: [_start_button_handler(), MessageHandler(filters.TEXT & ~filters.COMMAND, collect_target_site)],
            FAVICON: [_start_button_handler(), MessageHandler((filters.PHOTO | filters.Document.IMAGE | filters.TEXT) & ~filters.COMMAND, collect_favicon)],
            MINIMUM_AGE: [_start_button_handler(), MessageHandler(filters.TEXT & ~filters.COMMAND, collect_minimum_age)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start), _start_button_handler()],
        allow_reentry=True,
    )
    application.add_handler(conversation)
    return application


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _cleanup_state(context.user_data)
    context.user_data.clear()
    context.user_data["request_id"] = uuid.uuid4().hex[:12]
    await _reply_text_with_retry(
        update.effective_message,
        "Я сформирую баннер: выберите рекламную сеть → размер баннера → загрузите PNG/JPEG-креатив. "
        "Для видео-flow дополнительно попрошу адрес целевого сайта. Для GetIntent Video после адреса попрошу минимальный возраст от 0 до 99 без знака «+». "
        "Система проверит файл, изменит его до выбранного размера, наложит маску и вернет результат в Telegram.\n\n"
        f"{NETWORK_MENU_PROMPT}\n"
        f"{catalog_summary()}\n\n"
        "Команда /cancel отменяет текущую заявку.",
        operation="start.reply_text",
        reply_markup=network_menu_keyboard(),
    )
    return NETWORK


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _cleanup_state(context.user_data)
    context.user_data.clear()
    await _reply_text_with_retry(update.effective_message, "Заявка отменена, временные файлы удалены.", operation="cancel.reply_text", reply_markup=start_menu_keyboard())
    return ConversationHandler.END


async def collect_network(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    allowed = {network.slug for network in list_mask_networks()}
    try:
        network_slug = validate_mask_network_slug(_menu_value(update.effective_message.text), allowed)
    except ValidationError as exc:
        await _reply_text_with_retry(
            update.effective_message,
            f"{exc.safe_message}\n\n{NETWORK_MENU_PROMPT}\n{catalog_summary()}",
            operation="collect_network.invalid.reply_text",
            reply_markup=network_menu_keyboard(),
        )
        return NETWORK

    network = get_mask_network(network_slug)
    context.user_data["network_slug"] = network_slug
    context.user_data["network_display_name"] = network.display_name if network else network_slug
    if "banner_size_slug" in context.user_data:
        _clear_from(context.user_data, "banner_size_slug")
    await _reply_text_with_retry(
        update.effective_message,
        f"Сеть выбрана: {context.user_data['network_display_name']}.\n{spec_menu_prompt(network_slug)}",
        operation="collect_network.accepted.reply_text",
        reply_markup=spec_menu_keyboard(network_slug),
    )
    return BANNER_TYPE


async def collect_banner_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    network_slug = context.user_data.get("network_slug")
    if not network_slug:
        await _reply_text_with_retry(
            update.effective_message,
            f"Сначала выберите рекламную сеть.\n{NETWORK_MENU_PROMPT}\n{catalog_summary()}",
            operation="collect_banner_type.missing_network.reply_text",
            reply_markup=network_menu_keyboard(),
        )
        return NETWORK

    try:
        size_slug = validate_banner_size_slug_for_network(_menu_value(update.effective_message.text), network_slug)
    except ValidationError as exc:
        await _reply_text_with_retry(
            update.effective_message,
            f"{exc.safe_message}\n\n{spec_menu_prompt(network_slug)}",
            operation="collect_banner_type.invalid.reply_text",
            reply_markup=spec_menu_keyboard(network_slug),
        )
        return BANNER_TYPE

    size = get_banner_size_for_network(network_slug, size_slug)
    if size is None:
        await _reply_text_with_retry(update.effective_message, "Выбранный размер не поддерживается.", operation="collect_banner_type.unsupported.reply_text", reply_markup=spec_menu_keyboard(network_slug))
        return BANNER_TYPE

    context.user_data["banner_size_slug"] = size.slug
    context.user_data["banner_size"] = size
    context.user_data["banner_type"] = size.label
    context.user_data.pop("creative", None)
    await _reply_text_with_retry(
        update.effective_message,
        f"Размер баннера выбран: {size.label}.\n{creative_upload_prompt(size)}",
        operation="collect_banner_type.accepted.reply_text",
        reply_markup=ReplyKeyboardRemove(),
    )
    return CREATIVE


async def collect_creative(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    config = _config(context)
    message = update.effective_message
    if "banner_size" not in context.user_data:
        await _reply_text_with_retry(message, f"Сначала выберите размер баннера.\n{spec_menu_prompt(context.user_data.get('network_slug', ''))}", operation="collect_creative.missing_size.reply_text", reply_markup=spec_menu_keyboard(context.user_data.get("network_slug", "")))
        return BANNER_TYPE
    try:
        creative_info = await _save_and_validate_creative(message, context, config)
    except ValidationError as exc:
        await _reply_text_with_retry(message, f"Креатив не подходит: {exc.safe_message}\nОтправьте PNG/JPEG-креатив еще раз.", operation="collect_creative.invalid.reply_text")
        return CREATIVE

    context.user_data["creative"] = creative_info
    if _selected_flow_kind(context.user_data) in {"yandex_video", "getintent_video"}:
        flow_kind = _selected_flow_kind(context.user_data)
        prompt = (
            "Креатив принят. Отправьте адрес целевого сайта. Для GetIntent Video адрес сохраняется в заявке, но пока не отрисовывается.\n"
            if flow_kind == "getintent_video"
            else "Креатив принят. Отправьте адрес целевого сайта, который нужно показать слева от кнопки «Перейти».\n"
        )
        await _reply_text_with_retry(
            message,
            prompt + "Например: sberbank.com или https://www.sberbank.com",
            operation="collect_creative.video_target_site_prompt.reply_text",
        )
        return TARGET_SITE
    return await _generate(update, context)


async def collect_target_site(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    try:
        target_site_url, target_site_display = normalize_target_site_for_display(message.text or "", allow_local=_config(context).allow_local_urls)
    except ValidationError as exc:
        await _reply_text_with_retry(
            message,
            f"Адрес целевого сайта не подходит: {exc.safe_message}\nВведите публичный домен или http/https URL, например sberbank.com или https://www.sberbank.com",
            operation="collect_target_site.invalid.reply_text",
        )
        return TARGET_SITE

    context.user_data["target_site_url"] = target_site_url
    context.user_data["target_site_display"] = target_site_display
    if _selected_flow_kind(context.user_data) == "getintent_video":
        await _reply_text_with_retry(
            message,
            "Адрес принят. Введите минимальный возраст цифрами от 0 до 99 без знака «+», например 12. На баннере будет показано 12+.",
            operation="collect_target_site.minimum_age_prompt.reply_text",
        )
        return MINIMUM_AGE
    if _selected_flow_kind(context.user_data) == "yandex_video" and _load_target_site_favicon(target_site_url) is None:
        await _reply_text_with_retry(
            message,
            "Не удалось автоматически скачать favicon целевого сайта. Отправьте PNG/JPEG-иконку сайта как фото или документ — я добавлю её в видео-баннер.",
            operation="collect_target_site.favicon_prompt.reply_text",
        )
        return FAVICON
    return await _generate(update, context)


async def collect_minimum_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    if _selected_flow_kind(context.user_data) != "getintent_video":
        return await _generate(update, context)
    try:
        minimum_age = validate_minimum_age(message.text or "")
    except ValidationError as exc:
        await _reply_text_with_retry(
            message,
            f"Минимальный возраст не подходит: {exc.safe_message}",
            operation="collect_minimum_age.invalid.reply_text",
        )
        return MINIMUM_AGE

    context.user_data["minimum_age"] = minimum_age
    return await _generate(update, context)


async def collect_favicon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    if _selected_flow_kind(context.user_data) != "yandex_video":
        return await _generate(update, context)
    try:
        favicon_info = await _save_and_validate_favicon(message, context, _config(context))
    except ValidationError as exc:
        await _reply_text_with_retry(message, f"Favicon не подходит: {exc.safe_message}\nОтправьте PNG/JPEG-иконку сайта еще раз.", operation="collect_favicon.invalid.reply_text")
        return FAVICON

    context.user_data["favicon"] = favicon_info
    return await _generate(update, context)


async def review_or_generate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    action = review_menu_action(update.effective_message.text)
    if action == "generate":
        return await _generate(update, context)
    if action in EDIT_TARGETS:
        _clear_for_edit(context.user_data, action)
        await _reply_text_with_retry(update.effective_message, _edit_prompt(action, context.user_data), operation=f"review.edit.{action}.reply_text", reply_markup=_edit_reply_markup(action, context.user_data))
        return EDIT_TARGETS[action]
    await _reply_text_with_retry(update.effective_message, "Review-экран в новом flow не используется. Загрузите креатив после выбора сети и размера.", operation="review.unsupported.reply_text")
    return CREATIVE


async def _generate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id if update.effective_user else 0
    effective_chat = getattr(update, "effective_chat", None)
    chat_id = effective_chat.id if effective_chat else 0
    lock = _user_lock(context, user_id)
    if lock.locked():
        await _reply_text_with_retry(update.effective_message, "У вас уже выполняется генерация. Дождитесь завершения или отмените текущую заявку.", operation="generate.busy.reply_text")
        return CREATIVE

    async with lock:
        await _reply_text_with_retry(update.effective_message, "Креатив принят. Накладываю маску и готовлю PNG-баннер.", operation="generate.started.reply_text", reply_markup=ReplyKeyboardRemove())
        request_id = context.user_data.get("request_id", uuid.uuid4().hex[:12])
        user_hash = _safe_user_hash(user_id)
        chat_hash = _safe_user_hash(chat_id)
        started = asyncio.get_running_loop().time()
        logger.info("masked_banner_request_started request_id=%s user=%s chat=%s", request_id, user_hash, chat_hash)
        try:
            config = _config(context)
            output_path = _ensure_temp_dir(context.user_data, config) / "masked-banner.png"
            request = _build_masked_banner_request(context.user_data, output_path)
            banner_generator: BannerGenerator = context.bot_data.get("banner_generator") or _configure_banner_generator(generate_masked_banner, config)
            async with asyncio.timeout(config.request_timeout_seconds):
                result = await banner_generator(request)
            await _reply_photo_file_with_retry(
                update.effective_message,
                operation="masked_banner.reply_photo",
                photo_path=result.output_path,
                caption="Баннер с маской готов.",
            )
            await _reply_document_file_with_retry(
                update.effective_message,
                operation="masked_banner.reply_document",
                document_path=result.output_path,
                filename=f"{request.network_slug}-{request.size_slug}-masked-banner.png",
                caption="PNG-баннер с маской без сжатия.",
                reply_markup=start_menu_keyboard(),
            )
            logger.info("masked_banner_request_done request_id=%s user=%s chat=%s elapsed_ms=%s", request_id, user_hash, chat_hash, _loop_elapsed_ms(started))
        except asyncio.TimeoutError as exc:
            logger.warning("masked_banner_timeout request_id=%s user=%s chat=%s elapsed_ms=%s exception_message=%s", request_id, user_hash, chat_hash, _loop_elapsed_ms(started), _safe_exception_message(exc))
            await _reply_text_with_retry(update.effective_message, "Истекло время генерации баннера. Попробуйте позже.", operation="generate.timeout.reply_text")
        except (ValidationError, ScreenshotGenerationError) as exc:
            logger.warning("masked_banner_failed request_id=%s user=%s chat=%s elapsed_ms=%s reason=%s exception_message=%s", request_id, user_hash, chat_hash, _loop_elapsed_ms(started), type(exc).__name__, _safe_exception_message(exc))
            await _reply_text_with_retry(update.effective_message, exc.safe_message, operation="generate.validation_or_composition_error.reply_text", reply_markup=start_menu_keyboard())
        except Exception as exc:
            logger.exception("masked_banner_unexpected request_id=%s user=%s chat=%s elapsed_ms=%s exception_class=%s", request_id, user_hash, chat_hash, _loop_elapsed_ms(started), type(exc).__name__)
            await _reply_text_with_retry(update.effective_message, "Не удалось сформировать баннер. Проверьте входные данные и попробуйте снова.", operation="generate.unexpected.reply_text", reply_markup=start_menu_keyboard())
        finally:
            await _cleanup_state(context.user_data)
            context.user_data.clear()

    return ConversationHandler.END


async def _save_and_validate_creative(message: Any, context: ContextTypes.DEFAULT_TYPE, config: BotConfig) -> CreativeInfo:
    temp_dir = _ensure_temp_dir(context.user_data, config)
    max_bytes = config.max_creative_bytes

    if message.document:
        document = message.document
        if document.file_size and document.file_size > max_bytes:
            raise ValidationError("Файл креатива превышает допустимый размер.")
        suffix = Path(document.file_name or "creative.jpg").suffix.lower() or ".jpg"
        destination = temp_dir / f"creative{suffix}"
        tg_file = await document.get_file()
        await tg_file.download_to_drive(custom_path=destination)
        return validate_creative_file(destination, declared_mime_type=document.mime_type, max_bytes=max_bytes, max_pixels=config.max_creative_pixels)

    if message.photo:
        photo = message.photo[-1]
        if photo.file_size and photo.file_size > max_bytes:
            raise ValidationError("Файл креатива превышает допустимый размер.")
        destination = temp_dir / "creative.jpg"
        tg_file = await photo.get_file()
        await tg_file.download_to_drive(custom_path=destination)
        return validate_creative_file(destination, declared_mime_type="image/jpeg", max_bytes=max_bytes, max_pixels=config.max_creative_pixels)

    if message.text and config.allow_local_creative_path:
        return validate_creative_file(Path(message.text.strip()), declared_mime_type=None, max_bytes=max_bytes, max_pixels=config.max_creative_pixels)

    raise ValidationError("Загрузите PNG/JPEG-файл как фото или документ.")


async def _save_and_validate_favicon(message: Any, context: ContextTypes.DEFAULT_TYPE, config: BotConfig) -> CreativeInfo:
    temp_dir = _ensure_temp_dir(context.user_data, config)
    max_bytes = min(config.max_creative_bytes, 2 * 1024 * 1024)

    if message.document:
        document = message.document
        if document.file_size and document.file_size > max_bytes:
            raise ValidationError("Файл favicon превышает допустимый размер 2 МБ.")
        suffix = Path(document.file_name or "favicon.png").suffix.lower() or ".png"
        destination = temp_dir / f"favicon{suffix}"
        tg_file = await document.get_file()
        await tg_file.download_to_drive(custom_path=destination)
        return validate_creative_file(destination, declared_mime_type=document.mime_type, max_bytes=max_bytes, max_pixels=1_000_000)

    if message.photo:
        photo = message.photo[-1]
        if photo.file_size and photo.file_size > max_bytes:
            raise ValidationError("Файл favicon превышает допустимый размер 2 МБ.")
        destination = temp_dir / "favicon.jpg"
        tg_file = await photo.get_file()
        await tg_file.download_to_drive(custom_path=destination)
        return validate_creative_file(destination, declared_mime_type="image/jpeg", max_bytes=max_bytes, max_pixels=1_000_000)

    if message.text and config.allow_local_creative_path:
        return validate_creative_file(Path(message.text.strip()), declared_mime_type=None, max_bytes=max_bytes, max_pixels=1_000_000)

    raise ValidationError("Загрузите PNG/JPEG-favicon как фото или документ.")


def _build_masked_banner_request(user_data: dict[str, Any], output_path: Path) -> MaskedBannerRequest:
    size: BannerSize = user_data["banner_size"]
    return MaskedBannerRequest(
        request_id=user_data["request_id"],
        network_slug=user_data["network_slug"],
        network_display_name=user_data["network_display_name"],
        size_slug=size.slug,
        size=size,
        creative=user_data["creative"],
        output_path=output_path,
        target_site_url=user_data.get("target_site_url"),
        target_site_display=user_data.get("target_site_display"),
        favicon_path=user_data["favicon"].path if isinstance(user_data.get("favicon"), CreativeInfo) else None,
        minimum_age=user_data.get("minimum_age"),
    )


def _ensure_temp_dir(user_data: dict[str, Any], config: BotConfig) -> Path:
    if "temp_dir_obj" not in user_data:
        user_data["temp_dir_obj"] = tempfile.TemporaryDirectory(prefix="reports-ad-", dir=config.temp_root)
    return Path(user_data["temp_dir_obj"].name)


def _review_text(user_data: dict[str, Any]) -> str:
    creative: CreativeInfo = user_data["creative"]
    size: BannerSize = user_data["banner_size"]
    return (
        "Проверьте заявку перед запуском:\n"
        f"• сеть: {user_data['network_display_name']} ({user_data['network_slug']})\n"
        f"• размер баннера: {size.label}\n"
        f"• креатив: {creative.mime_type}, {creative.width}×{creative.height}, {creative.size_bytes} байт\n"
        f"• целевой сайт: {user_data.get('target_site_display', 'не требуется')}\n"
        f"• минимальный возраст: {user_data.get('minimum_age', 'не требуется')}\n"
        "• режим: resize + mask overlay/Pillow\n\n"
        f"{REVIEW_MENU_PROMPT}"
    )


async def _reply_text_with_retry(message: Any, text: str, *, operation: str, **kwargs: Any) -> Any:
    return await _telegram_send_with_retry(lambda: message.reply_text(text, **kwargs), operation=operation)


async def _reply_photo_with_retry(message: Any, *, operation: str, sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep, **kwargs: Any) -> Any:
    return await _telegram_send_with_retry(lambda: message.reply_photo(**kwargs), operation=operation, sleep=sleep)


async def _reply_document_with_retry(message: Any, *, operation: str, sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep, **kwargs: Any) -> Any:
    return await _telegram_send_with_retry(lambda: message.reply_document(**kwargs), operation=operation, sleep=sleep)


async def _reply_photo_file_with_retry(message: Any, *, operation: str, photo_path: Path, sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep, **kwargs: Any) -> Any:
    return await _telegram_send_file_with_retry(lambda file_obj: message.reply_photo(photo=file_obj, **kwargs), operation=operation, file_path=photo_path, sleep=sleep)


async def _reply_document_file_with_retry(message: Any, *, operation: str, document_path: Path, sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep, **kwargs: Any) -> Any:
    return await _telegram_send_file_with_retry(lambda file_obj: message.reply_document(document=file_obj, **kwargs), operation=operation, file_path=document_path, sleep=sleep)


async def _telegram_send_file_with_retry(
    send_call: Callable[[BinaryIO], Awaitable[Any]],
    *,
    operation: str,
    file_path: Path,
    max_attempts: int = TELEGRAM_SEND_MAX_ATTEMPTS,
    initial_backoff_seconds: float = TELEGRAM_SEND_INITIAL_BACKOFF_SECONDS,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> Any:
    safe_file_name = file_path.name

    async def open_and_send() -> Any:
        file_size = file_path.stat().st_size if file_path.exists() else -1
        logger.info("telegram_file_send_attempt operation=%s file_name=%s file_size=%s", operation, safe_file_name, file_size)
        with file_path.open("rb") as file_obj:
            return await send_call(file_obj)

    return await _telegram_send_with_retry(
        open_and_send,
        operation=operation,
        max_attempts=max_attempts,
        initial_backoff_seconds=initial_backoff_seconds,
        sleep=sleep,
    )


async def _telegram_send_with_retry(
    send_call: Callable[[], Awaitable[Any]],
    *,
    operation: str,
    max_attempts: int = TELEGRAM_SEND_MAX_ATTEMPTS,
    initial_backoff_seconds: float = TELEGRAM_SEND_INITIAL_BACKOFF_SECONDS,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> Any:
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    for attempt in range(1, max_attempts + 1):
        try:
            return await send_call()
        except TELEGRAM_RETRYABLE_EXCEPTIONS as exc:
            if attempt >= max_attempts:
                logger.warning(
                    "telegram_send_retry_exhausted operation=%s attempt=%s max_attempts=%s exception_class=%s exception_message=%s",
                    operation,
                    attempt,
                    max_attempts,
                    type(exc).__name__,
                    _safe_telegram_exception_message(exc),
                )
                raise
            delay = min(initial_backoff_seconds * (2 ** (attempt - 1)), TELEGRAM_SEND_MAX_BACKOFF_SECONDS)
            logger.warning(
                "telegram_send_retry operation=%s attempt=%s max_attempts=%s retry_delay_seconds=%.2f exception_class=%s exception_message=%s",
                operation,
                attempt,
                max_attempts,
                delay,
                type(exc).__name__,
                _safe_telegram_exception_message(exc),
            )
            await sleep(delay)

    raise RuntimeError("unreachable telegram retry state")


def start_menu_keyboard() -> ReplyKeyboardMarkup:
    menu = ReportsAdMenuMessage(navigation=None, label="reports-ad-start", input_field=START_MENU_PROMPT)
    menu.keyboard = []
    _add_menu_row_button(menu, label=START_MENU_BUTTON, args=START_MENU_BUTTON)
    return menu.gen_keyboard_content()


def network_menu_keyboard() -> ReplyKeyboardMarkup:
    menu = ReportsAdMenuMessage(navigation=None, label="reports-ad-network", input_field="Выберите рекламную сеть")
    menu.keyboard = []
    for network in list_mask_networks():
        size_count = len(list_banner_sizes_for_network(network.slug))
        detail = f"{size_count} размер" if size_count == 1 else f"{size_count} размера"
        label = _menu_label(network.display_name, network.slug, detail)
        _add_menu_row_button(menu, label=label, args=network.slug)
    return menu.gen_keyboard_content()


def spec_menu_keyboard(network_slug: str = "") -> ReplyKeyboardMarkup:
    menu = ReportsAdMenuMessage(navigation=None, label=f"reports-ad-size-{network_slug or 'any'}", input_field=SPEC_MENU_PROMPT)
    menu.keyboard = []
    for size in _sizes_for_menu(network_slug):
        label = _size_menu_label(size)
        _add_menu_row_button(menu, label=label, args=size.slug)
    return menu.gen_keyboard_content()


def review_menu_keyboard() -> ReplyKeyboardMarkup:
    menu = ReportsAdMenuMessage(navigation=None, label="reports-ad-review", input_field=REVIEW_MENU_PROMPT)
    menu.keyboard = []
    for label in REVIEW_MENU_ACTIONS:
        _add_menu_row_button(menu, label=label, args=label)
    return menu.gen_keyboard_content()


def _start_button_handler() -> MessageHandler:
    return MessageHandler(filters.Regex(f"^{re.escape(START_MENU_BUTTON)}$") & ~filters.COMMAND, start)


def spec_menu_prompt(network_slug: str = "") -> str:
    return f"{SPEC_MENU_PROMPT}\n{specs_summary(network_slug)}"


def creative_upload_prompt(size: BannerSize) -> str:
    return f"Отправьте PNG/JPEG-креатив как фото или документ. Он будет изменен до размера {size.label} и наложен под выбранную маску."


def catalog_summary(max_networks: int = 12) -> str:
    return "\n".join(
        f"{index}. {network.display_name} — {network.slug} ({len(list_banner_sizes_for_network(network.slug))} размер(а))"
        for index, network in enumerate(list_mask_networks()[:max_networks], start=1)
    )


def specs_summary(network_slug: str = "", max_specs: int = 24) -> str:
    return "\n".join(f"{index}. {size.label} — {size.slug}" for index, size in enumerate(_sizes_for_menu(network_slug)[:max_specs], start=1))


def _sizes_for_menu(network_slug: str = "") -> list[BannerSize]:
    sizes = list_banner_sizes_for_network(network_slug) if network_slug else []
    if sizes:
        return sizes
    all_sizes: dict[str, BannerSize] = {}
    for network in list_mask_networks():
        for size in list_banner_sizes_for_network(network.slug):
            all_sizes.setdefault(size.slug, size)
    return list(all_sizes.values())


def _add_menu_row_button(menu: ReportsAdMenuMessage, *, label: str, args: str) -> None:
    new_row = bool(menu.keyboard and menu.keyboard[-1])
    menu.add_button(label=label, args=args, new_row=new_row)


def _menu_label(display_name: str, slug: str, detail: str = "") -> str:
    base = f"{display_name} [{slug}]"
    return f"{base} — {detail}" if detail else base


def _size_menu_label(size: BannerSize) -> str:
    label = _menu_label(size.label, size.slug)
    if len(label) <= TELEGRAM_KEYBOARD_BUTTON_TEXT_LIMIT:
        return label
    return size.slug


def _spec_menu_label(size: BannerSize) -> str:
    return _size_menu_label(size)


def _menu_value(text: str | None) -> str:
    value = (text or "").strip()
    if "[" in value and "]" in value:
        return value.split("[", 1)[1].split("]", 1)[0].strip()
    return value


def review_menu_action(text: str | None) -> str:
    raw_value = (text or "").strip()
    menu_action = REVIEW_MENU_ACTIONS.get(raw_value)
    if menu_action:
        return menu_action
    answer = raw_value.lower()
    if answer in {"yes", "y", "да", "д", "start", "go", "generate", "старт", "запуск", "запустить"}:
        return "generate"
    if answer.startswith("edit") or answer.startswith("изменить"):
        parts = answer.split(maxsplit=1)
        target = parts[1].strip() if len(parts) > 1 else ""
        if target in {"тип баннера", "размер баннера", "banner size"}:
            return "size"
        return target if target in EDIT_TARGETS else "unknown_edit"
    return ""


def _clear_for_edit(user_data: dict[str, Any], target: str) -> None:
    if target == "network":
        _clear_from(user_data, "network_slug")
    elif target in {"spec", "banner", "size"}:
        _clear_from(user_data, "banner_size_slug")
    elif target == "creative":
        user_data.pop("creative", None)


def _clear_from(user_data: dict[str, Any], key: str) -> None:
    order = ["network_slug", "network_display_name", "banner_size_slug", "banner_size", "banner_type", "creative", "target_site_url", "target_site_display", "minimum_age", "favicon"]
    if key not in order:
        return
    for candidate in order[order.index(key) :]:
        user_data.pop(candidate, None)


def _edit_prompt(target: str, user_data: dict[str, Any]) -> str:
    if target == "network":
        return f"{NETWORK_MENU_PROMPT}\n{catalog_summary()}"
    if target in {"spec", "banner", "size"}:
        return spec_menu_prompt(user_data.get("network_slug", ""))
    if target == "creative":
        size = user_data.get("banner_size")
        if isinstance(size, BannerSize):
            return creative_upload_prompt(size)
        return "Загрузите новый PNG/JPEG-креатив как фото или документ."
    return "Введите новое значение."


def _edit_reply_markup(target: str, user_data: dict[str, Any]) -> ReplyKeyboardMarkup | ReplyKeyboardRemove:
    if target == "network":
        return network_menu_keyboard()
    if target in {"spec", "banner", "size"}:
        return spec_menu_keyboard(user_data.get("network_slug", ""))
    return ReplyKeyboardRemove()


def _selected_flow_kind(user_data: dict[str, Any]) -> str:
    network = get_mask_network(str(user_data.get("network_slug", "")))
    return network.flow_kind if network else "masked_banner"


async def _cleanup_state(user_data: dict[str, Any]) -> None:
    temp_dir_obj = user_data.pop("temp_dir_obj", None)
    if temp_dir_obj is not None:
        temp_dir_obj.cleanup()


def _config(context: ContextTypes.DEFAULT_TYPE) -> BotConfig:
    return context.bot_data["config"]


def _configure_banner_generator(banner_generator: RawBannerGenerator, config: BotConfig) -> BannerGenerator:
    try:
        parameters = inspect.signature(banner_generator).parameters
    except (TypeError, ValueError):
        return banner_generator  # type: ignore[return-value]

    if "config" not in parameters:
        return banner_generator  # type: ignore[return-value]

    async def configured_banner_generator(request: MaskedBannerRequest) -> MaskedBannerResult:
        return await banner_generator(request, config=config)

    return configured_banner_generator


def _user_lock(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> asyncio.Lock:
    locks: dict[int, asyncio.Lock] = context.bot_data.setdefault("user_locks", {})
    lock = locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[user_id] = lock
    return lock


def _safe_user_hash(user_id: int) -> str:
    return hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:12]


def _safe_exception_message(exc: BaseException) -> str:
    return str(exc).replace("\n", " ")[:240]


def _safe_telegram_exception_message(exc: BaseException) -> str:
    message = _safe_exception_message(exc)
    for pattern, replacement in TELEGRAM_SAFE_MESSAGE_REDACTIONS:
        message = pattern.sub(replacement, message)
    return message


def _loop_elapsed_ms(started: float) -> int:
    return int((asyncio.get_running_loop().time() - started) * 1000)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)
