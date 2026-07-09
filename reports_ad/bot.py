from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .config import BotConfig
from .models import CreativeInfo, ProofRequest, ProofResult
from .proof import ScreenshotGenerationError, generate_proof
from .validation import (
    ValidationError,
    merge_url_with_utm,
    normalize_placement,
    normalize_public_url,
    parse_utm_params,
    validate_banner_type,
    validate_creative_file,
)

logger = logging.getLogger(__name__)

AD_PAGE_URL, BANNER_TYPE, CREATIVE, LANDING_URL, UTM, PLACEMENT, CONFIRM = range(7)

ProofGenerator = Callable[[ProofRequest], Awaitable[ProofResult]]


def build_application(config: BotConfig | None = None, proof_generator: ProofGenerator = generate_proof) -> Application:
    runtime_config = config or BotConfig.from_env()
    application = ApplicationBuilder().token(runtime_config.telegram_token).build()
    application.bot_data["config"] = runtime_config
    application.bot_data["proof_generator"] = proof_generator
    application.bot_data["user_locks"] = {}

    conversation = ConversationHandler(
        entry_points=[CommandHandler("start", start), CommandHandler("help", start)],
        states={
            AD_PAGE_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_ad_page_url)],
            BANNER_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_banner_type)],
            CREATIVE: [MessageHandler((filters.PHOTO | filters.Document.IMAGE | filters.TEXT) & ~filters.COMMAND, collect_creative)],
            LANDING_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_landing_url)],
            UTM: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_utm)],
            PLACEMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_placement)],
            CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_and_generate)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
        allow_reentry=True,
    )
    application.add_handler(conversation)
    return application


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _cleanup_state(context.user_data)
    context.user_data.clear()
    context.user_data["request_id"] = uuid.uuid4().hex[:12]
    await update.effective_message.reply_text(
        "Я сформирую proof-скриншот рекламного размещения.\n\n"
        "Понадобятся: URL рекламной страницы, тип баннера, PNG/JPEG-креатив, landing URL, UTM-строка и CSS-селектор/template.\n"
        "Отправьте URL страницы, где нужно разместить рекламу.\n\n"
        "Команда /cancel отменяет текущую заявку."
    )
    return AD_PAGE_URL


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _cleanup_state(context.user_data)
    context.user_data.clear()
    await update.effective_message.reply_text("Заявка отменена, временные файлы удалены.")
    return ConversationHandler.END


async def collect_ad_page_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    config = _config(context)
    try:
        context.user_data["ad_page_url"] = normalize_public_url(update.effective_message.text, allow_local=config.allow_local_urls)
    except ValidationError as exc:
        await update.effective_message.reply_text(exc.safe_message)
        return AD_PAGE_URL

    await update.effective_message.reply_text("Введите тип/формат баннера, например: banner, sidebar, native, tgo.")
    return BANNER_TYPE


async def collect_banner_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["banner_type"] = validate_banner_type(update.effective_message.text)
    except ValidationError as exc:
        await update.effective_message.reply_text(exc.safe_message)
        return BANNER_TYPE

    await update.effective_message.reply_text("Загрузите PNG/JPEG-креатив как фото или документ.")
    return CREATIVE


async def collect_creative(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    config = _config(context)
    message = update.effective_message
    try:
        creative_info = await _save_and_validate_creative(message, context, config)
    except ValidationError as exc:
        await message.reply_text(exc.safe_message)
        return CREATIVE

    context.user_data["creative"] = creative_info
    await message.reply_text("Введите landing URL, куда должен вести клик по баннеру.")
    return LANDING_URL


async def collect_landing_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    config = _config(context)
    try:
        context.user_data["landing_url"] = normalize_public_url(update.effective_message.text, allow_local=config.allow_local_urls)
    except ValidationError as exc:
        await update.effective_message.reply_text(exc.safe_message)
        return LANDING_URL

    await update.effective_message.reply_text("Введите UTM-строку, например: utm_source=telegram&utm_medium=cpc&utm_campaign=test")
    return UTM


async def collect_utm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    config = _config(context)
    try:
        utm_params = parse_utm_params(update.effective_message.text)
        merge_url_with_utm(context.user_data["landing_url"], utm_params, allow_local=config.allow_local_urls)
    except ValidationError as exc:
        await update.effective_message.reply_text(exc.safe_message)
        return UTM

    context.user_data["utm_params"] = utm_params
    await update.effective_message.reply_text(
        "Введите место вставки: CSS-селектор (`selector: .ad-slot`) или template: top-banner / sidebar-rect / in-content."
    )
    return PLACEMENT


async def collect_placement(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        placement = normalize_placement(update.effective_message.text)
    except ValidationError as exc:
        await update.effective_message.reply_text(exc.safe_message)
        return PLACEMENT

    context.user_data["placement"] = placement
    creative: CreativeInfo = context.user_data["creative"]
    utm_keys = ", ".join(sorted(context.user_data["utm_params"].keys()))
    await update.effective_message.reply_text(
        "Проверьте заявку:\n"
        f"• страница: {context.user_data['ad_page_url']}\n"
        f"• формат: {context.user_data['banner_type']}\n"
        f"• креатив: {creative.mime_type}, {creative.width}×{creative.height}, {creative.size_bytes} байт\n"
        f"• landing: {context.user_data['landing_url']}\n"
        f"• UTM-ключи: {utm_keys}\n"
        f"• placement: {placement.label}\n\n"
        "Ответьте `yes`, чтобы запустить генерацию, или /cancel для отмены."
    )
    return CONFIRM


async def confirm_and_generate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    answer = (update.effective_message.text or "").strip().lower()
    if answer not in {"yes", "y", "да", "д"}:
        await update.effective_message.reply_text("Генерация не запущена. Ответьте `yes` или отмените заявку через /cancel.")
        return CONFIRM

    user_id = update.effective_user.id if update.effective_user else 0
    lock = _user_lock(context, user_id)
    if lock.locked():
        await update.effective_message.reply_text("У вас уже выполняется генерация. Дождитесь завершения или отмените текущую заявку.")
        return CONFIRM

    async with lock:
        await update.effective_message.reply_text("Генерирую proof-скриншот. Это может занять до 1–2 минут.")
        request_id = context.user_data.get("request_id", uuid.uuid4().hex[:12])
        user_hash = _safe_user_hash(user_id)
        logger.info("proof_generation_started request_id=%s user=%s", request_id, user_hash)
        try:
            config = _config(context)
            output_path = _ensure_temp_dir(context.user_data, config) / "proof-result.png"
            request = _build_proof_request(context.user_data, config, output_path)
            proof_generator: ProofGenerator = context.bot_data["proof_generator"]
            async with asyncio.timeout(config.request_timeout_seconds):
                result = await proof_generator(request)
            with result.output_path.open("rb") as image_file:
                await update.effective_message.reply_photo(photo=image_file, caption="Proof-скриншот готов.")
            logger.info("proof_generation_done request_id=%s user=%s", request_id, user_hash)
        except asyncio.TimeoutError:
            logger.warning("proof_generation_timeout request_id=%s user=%s", request_id, user_hash)
            await update.effective_message.reply_text("Истекло время генерации proof-скриншота. Попробуйте позже или упростите страницу/селектор.")
        except (ValidationError, ScreenshotGenerationError) as exc:
            logger.warning("proof_generation_failed request_id=%s user=%s reason=%s", request_id, user_hash, type(exc).__name__)
            await update.effective_message.reply_text(exc.safe_message)
        except Exception:
            logger.exception("proof_generation_unexpected request_id=%s user=%s", request_id, user_hash)
            await update.effective_message.reply_text("Не удалось сформировать proof-скриншот. Проверьте входные данные и попробуйте снова.")
        finally:
            await _cleanup_state(context.user_data)
            context.user_data.clear()

    return ConversationHandler.END


async def _save_and_validate_creative(message: Any, context: ContextTypes.DEFAULT_TYPE, config: BotConfig) -> CreativeInfo:
    temp_dir = _ensure_temp_dir(context.user_data, config)

    if message.document:
        document = message.document
        if document.file_size and document.file_size > config.max_creative_bytes:
            raise ValidationError("Файл креатива превышает допустимый размер.")
        suffix = Path(document.file_name or "creative.jpg").suffix.lower() or ".jpg"
        destination = temp_dir / f"creative{suffix}"
        tg_file = await document.get_file()
        await tg_file.download_to_drive(custom_path=destination)
        return validate_creative_file(
            destination,
            declared_mime_type=document.mime_type,
            max_bytes=config.max_creative_bytes,
            max_pixels=config.max_creative_pixels,
        )

    if message.photo:
        photo = message.photo[-1]
        if photo.file_size and photo.file_size > config.max_creative_bytes:
            raise ValidationError("Файл креатива превышает допустимый размер.")
        destination = temp_dir / "creative.jpg"
        tg_file = await photo.get_file()
        await tg_file.download_to_drive(custom_path=destination)
        return validate_creative_file(
            destination,
            declared_mime_type="image/jpeg",
            max_bytes=config.max_creative_bytes,
            max_pixels=config.max_creative_pixels,
        )

    if message.text and config.allow_local_creative_path:
        return validate_creative_file(
            Path(message.text.strip()),
            declared_mime_type=None,
            max_bytes=config.max_creative_bytes,
            max_pixels=config.max_creative_pixels,
        )

    raise ValidationError("Загрузите PNG/JPEG-файл как фото или документ.")


def _build_proof_request(user_data: dict[str, Any], config: BotConfig, output_path: Path) -> ProofRequest:
    return ProofRequest(
        request_id=user_data["request_id"],
        ad_page_url=user_data["ad_page_url"],
        banner_type=user_data["banner_type"],
        creative=user_data["creative"],
        landing_url=user_data["landing_url"],
        utm_params=user_data["utm_params"],
        placement=user_data["placement"],
        output_path=output_path,
        viewport_width=config.viewport_width,
        viewport_height=config.viewport_height,
        headless=config.headless,
        allow_local_urls=config.allow_local_urls,
        page_load_timeout_ms=config.page_load_timeout_ms,
        click_timeout_ms=config.click_timeout_ms,
        screenshot_timeout_ms=config.screenshot_timeout_ms,
        locale=config.locale,
        timezone_id=config.timezone_id,
    )


def _ensure_temp_dir(user_data: dict[str, Any], config: BotConfig) -> Path:
    if "temp_dir_obj" not in user_data:
        user_data["temp_dir_obj"] = tempfile.TemporaryDirectory(prefix="reports-ad-", dir=config.temp_root)
    return Path(user_data["temp_dir_obj"].name)


async def _cleanup_state(user_data: dict[str, Any]) -> None:
    temp_dir_obj = user_data.pop("temp_dir_obj", None)
    if temp_dir_obj is not None:
        temp_dir_obj.cleanup()


def _config(context: ContextTypes.DEFAULT_TYPE) -> BotConfig:
    return context.bot_data["config"]


def _user_lock(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> asyncio.Lock:
    locks: dict[int, asyncio.Lock] = context.bot_data["user_locks"]
    lock = locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[user_id] = lock
    return lock


def _safe_user_hash(user_id: int) -> str:
    return hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:12]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)

