from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram_menu import BaseMessage

from .catalog import (
    catalog_summary,
    choose_placement_for_url,
    get_network,
    get_site_placement,
    get_spec,
    list_networks,
    list_runtime_specs,
    specs_summary,
)
from .config import BotConfig
from .models import CreativeInfo, CreativeSpec, ProofRequest, ProofResult
from .proof import ScreenshotGenerationError, generate_proof
from .validation import (
    ValidationError,
    merge_url_with_utm,
    normalize_public_url,
    parse_utm_params,
    validate_creative_file,
    validate_network_slug,
    validate_spec_slug,
)

logger = logging.getLogger(__name__)

NETWORK, BANNER_TYPE, CREATIVE, AD_PAGE_URL, LANDING_URL, UTM, REVIEW = range(7)
EDIT_TARGETS = {
    "network": NETWORK,
    "spec": BANNER_TYPE,
    "banner": BANNER_TYPE,
    "ad_url": AD_PAGE_URL,
    "page": AD_PAGE_URL,
    "creative": CREATIVE,
    "landing": LANDING_URL,
    "utm": UTM,
}

ProofGenerator = Callable[[ProofRequest], Awaitable[ProofResult]]

NETWORK_MENU_PROMPT = "Выберите рекламную сеть кнопкой меню telegram_menu:"
SPEC_MENU_PROMPT = "Теперь выберите тип баннера/spec кнопкой меню telegram_menu:"
TELEGRAM_KEYBOARD_BUTTON_TEXT_LIMIT = 64


class ReportsAdMenuMessage(BaseMessage):
    """Small telegram_menu message adapter used to build reply-keyboard menus."""

    def update(self, context: ContextTypes.DEFAULT_TYPE | None = None) -> str:
        return ""


def build_application(config: BotConfig | None = None, proof_generator: ProofGenerator = generate_proof) -> Application:
    runtime_config = config or BotConfig.from_env()
    application = ApplicationBuilder().token(runtime_config.telegram_token).build()
    application.bot_data["config"] = runtime_config
    application.bot_data["proof_generator"] = proof_generator
    application.bot_data["user_locks"] = {}

    conversation = ConversationHandler(
        entry_points=[CommandHandler("start", start), CommandHandler("help", start)],
        states={
            NETWORK: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_network)],
            BANNER_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_banner_type)],
            AD_PAGE_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_ad_page_url)],
            CREATIVE: [MessageHandler((filters.PHOTO | filters.Document.IMAGE | filters.TEXT) & ~filters.COMMAND, collect_creative)],
            LANDING_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_landing_url)],
            UTM: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_utm)],
            REVIEW: [MessageHandler(filters.TEXT & ~filters.COMMAND, review_or_generate)],
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
        "Выберите: рекламную сеть → тип баннера → PNG/JPEG-креатив → URL рекламной страницы → landing URL → UTM → review/edit → старт.\n"
        "MVP принимает только PNG/JPEG, даже если исходный каталог содержит GIF/video/HTML5.\n\n"
        f"{NETWORK_MENU_PROMPT}\n"
        f"{catalog_summary()}\n\n"
        "Команда /cancel отменяет текущую заявку.",
        reply_markup=network_menu_keyboard(),
    )
    return NETWORK


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _cleanup_state(context.user_data)
    context.user_data.clear()
    await update.effective_message.reply_text("Заявка отменена, временные файлы удалены.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def collect_network(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    allowed = {network["slug"] for network in list_networks()}
    try:
        network_slug = validate_network_slug(_menu_value(update.effective_message.text), allowed)
    except ValidationError as exc:
        await update.effective_message.reply_text(
            f"{exc.safe_message}\n\n{NETWORK_MENU_PROMPT}\n{catalog_summary()}",
            reply_markup=network_menu_keyboard(),
        )
        return NETWORK

    network = get_network(network_slug) or {"slug": network_slug, "display_name": network_slug}
    context.user_data["network_slug"] = network_slug
    context.user_data["network_display_name"] = network.get("display_name", network_slug)
    if "spec" in context.user_data and context.user_data["spec"].network_slug != network_slug:
        _clear_from(context.user_data, "spec")
    await update.effective_message.reply_text(
        f"Сеть выбрана: {context.user_data['network_display_name']}.\n"
        f"{spec_menu_prompt(network_slug)}",
        reply_markup=spec_menu_keyboard(network_slug),
    )
    return BANNER_TYPE


async def collect_banner_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    network_slug = context.user_data.get("network_slug")
    if not network_slug:
        await update.effective_message.reply_text(
            f"Сначала выберите рекламную сеть.\n{NETWORK_MENU_PROMPT}\n{catalog_summary()}",
            reply_markup=network_menu_keyboard(),
        )
        return NETWORK

    specs = list_runtime_specs(network_slug)
    allowed = {spec.slug for spec in specs}
    try:
        spec_slug = validate_spec_slug(_menu_value(update.effective_message.text), allowed)
    except ValidationError as exc:
        await update.effective_message.reply_text(
            f"{exc.safe_message}\n\n{spec_menu_prompt(network_slug)}",
            reply_markup=spec_menu_keyboard(network_slug),
        )
        return BANNER_TYPE

    spec = get_spec(spec_slug)
    if spec is None or not spec.runtime_upload_supported:
        await update.effective_message.reply_text(
            "Выбранный формат не поддерживается runtime. Выберите PNG/JPEG spec из меню.",
            reply_markup=spec_menu_keyboard(network_slug),
        )
        return BANNER_TYPE

    context.user_data["spec"] = spec
    context.user_data["banner_type"] = spec.display_name
    context.user_data.pop("creative", None)
    if "ad_page_url" in context.user_data:
        context.user_data["placement"] = choose_placement_for_url(context.user_data["ad_page_url"], spec)
    if "landing_url" in context.user_data or "utm_params" in context.user_data:
        _clear_from(context.user_data, "landing_url")
    await update.effective_message.reply_text(
        f"Тип баннера выбран: {spec.display_name}\n"
        f"{creative_upload_prompt(spec)}",
        reply_markup=ReplyKeyboardRemove(),
    )
    return CREATIVE


async def collect_ad_page_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    config = _config(context)
    try:
        ad_page_url = normalize_public_url(update.effective_message.text, allow_local=config.allow_local_urls)
    except ValidationError as exc:
        await update.effective_message.reply_text(exc.safe_message)
        return AD_PAGE_URL

    spec: CreativeSpec = context.user_data["spec"]
    placement = choose_placement_for_url(ad_page_url, spec)
    context.user_data["ad_page_url"] = ad_page_url
    context.user_data["placement"] = placement
    site = get_site_placement(ad_page_url)
    placement_note = f"Автоматически выбран placement: {placement.label}."
    if site:
        placement_note += f" Площадка: {site.host}; статус селектора: {site.selector_confidence}."
    else:
        placement_note += " Площадка не найдена в списке; использован dimension-based fallback."

    if "landing_url" in context.user_data and "utm_params" in context.user_data:
        await update.effective_message.reply_text(f"{placement_note}\n\n" + _review_text(context.user_data))
        return REVIEW
    await update.effective_message.reply_text(f"{placement_note}\nВведите landing URL, куда должен вести клик по баннеру.")
    return LANDING_URL


async def collect_creative(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    config = _config(context)
    message = update.effective_message
    try:
        creative_info = await _save_and_validate_creative(message, context, config)
    except ValidationError as exc:
        await message.reply_text(f"Креатив не подходит: {exc.safe_message}\nОтправьте PNG/JPEG-креатив для выбранного spec еще раз.")
        return CREATIVE

    context.user_data["creative"] = creative_info
    if "ad_page_url" in context.user_data and "landing_url" in context.user_data and "utm_params" in context.user_data:
        await message.reply_text(_review_text(context.user_data))
        return REVIEW
    if "ad_page_url" in context.user_data:
        await message.reply_text("Креатив принят. Введите landing URL, куда должен вести клик по баннеру.")
        return LANDING_URL
    await message.reply_text("Креатив принят. Введите URL рекламной страницы, где нужно разместить рекламу.")
    return AD_PAGE_URL


async def collect_landing_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    config = _config(context)
    try:
        context.user_data["landing_url"] = normalize_public_url(update.effective_message.text, allow_local=config.allow_local_urls)
    except ValidationError as exc:
        await update.effective_message.reply_text(exc.safe_message)
        return LANDING_URL

    if "utm_params" in context.user_data:
        await update.effective_message.reply_text(_review_text(context.user_data))
        return REVIEW
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
    await update.effective_message.reply_text(_review_text(context.user_data))
    return REVIEW


async def review_or_generate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    answer = (update.effective_message.text or "").strip().lower()
    if answer in {"yes", "y", "да", "д", "start", "go", "generate", "старт", "запуск"}:
        return await _generate(update, context)

    if answer.startswith("edit") or answer.startswith("изменить"):
        parts = answer.split(maxsplit=1)
        target = parts[1].strip() if len(parts) > 1 else ""
        next_state = EDIT_TARGETS.get(target)
        if next_state is None:
            await update.effective_message.reply_text(
                "Неизвестное поле для редактирования. Используйте: edit network, edit spec, edit ad_url, edit creative, edit landing, edit utm."
            )
            return REVIEW
        _clear_for_edit(context.user_data, target)
        await update.effective_message.reply_text(_edit_prompt(target, context.user_data), reply_markup=_edit_reply_markup(target, context.user_data))
        return next_state

    await update.effective_message.reply_text(
        "Генерация не запущена. Ответьте `yes` для старта или `edit <field>` для правки.\n\n"
        + _review_text(context.user_data)
    )
    return REVIEW


async def _generate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id if update.effective_user else 0
    lock = _user_lock(context, user_id)
    if lock.locked():
        await update.effective_message.reply_text("У вас уже выполняется генерация. Дождитесь завершения или отмените текущую заявку.")
        return REVIEW

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
    spec: CreativeSpec = context.user_data["spec"]
    max_bytes = min(config.max_creative_bytes, spec.max_file_size_bytes or config.max_creative_bytes)

    if message.document:
        document = message.document
        if document.file_size and document.file_size > max_bytes:
            raise ValidationError("Файл креатива превышает допустимый размер.")
        suffix = Path(document.file_name or "creative.jpg").suffix.lower() or ".jpg"
        destination = temp_dir / f"creative{suffix}"
        tg_file = await document.get_file()
        await tg_file.download_to_drive(custom_path=destination)
        return validate_creative_file(
            destination,
            declared_mime_type=document.mime_type,
            max_bytes=max_bytes,
            max_pixels=config.max_creative_pixels,
            spec=spec,
        )

    if message.photo:
        photo = message.photo[-1]
        if photo.file_size and photo.file_size > max_bytes:
            raise ValidationError("Файл креатива превышает допустимый размер.")
        destination = temp_dir / "creative.jpg"
        tg_file = await photo.get_file()
        await tg_file.download_to_drive(custom_path=destination)
        return validate_creative_file(
            destination,
            declared_mime_type="image/jpeg",
            max_bytes=max_bytes,
            max_pixels=config.max_creative_pixels,
            spec=spec,
        )

    if message.text and config.allow_local_creative_path:
        return validate_creative_file(
            Path(message.text.strip()),
            declared_mime_type=None,
            max_bytes=max_bytes,
            max_pixels=config.max_creative_pixels,
            spec=spec,
        )

    raise ValidationError("Загрузите PNG/JPEG-файл как фото или документ.")


def _build_proof_request(user_data: dict[str, Any], config: BotConfig, output_path: Path) -> ProofRequest:
    return ProofRequest(
        request_id=user_data["request_id"],
        ad_page_url=user_data["ad_page_url"],
        network_slug=user_data["network_slug"],
        network_display_name=user_data["network_display_name"],
        spec=user_data["spec"],
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


def _review_text(user_data: dict[str, Any]) -> str:
    creative: CreativeInfo = user_data["creative"]
    spec: CreativeSpec = user_data["spec"]
    utm_keys = ", ".join(sorted(user_data["utm_params"].keys()))
    return (
        "Проверьте заявку перед запуском:\n"
        f"• сеть: {user_data['network_display_name']} ({user_data['network_slug']})\n"
        f"• тип баннера: {spec.display_name}\n"
        f"• размер spec: {spec.dimensions_label}; лимит: {spec.max_file_size_bytes} байт\n"
        f"• страница размещения: {user_data['ad_page_url']}\n"
        f"• placement: {user_data['placement'].label}\n"
        f"• креатив: {creative.mime_type}, {creative.width}×{creative.height}, {creative.size_bytes} байт\n"
        f"• landing: {user_data['landing_url']}\n"
        f"• UTM-ключи: {utm_keys}\n\n"
        "Ответьте `yes`, чтобы запустить генерацию. Для правки используйте: edit network, edit spec, edit ad_url, edit creative, edit landing, edit utm."
    )


def network_menu_keyboard() -> ReplyKeyboardMarkup:
    menu = ReportsAdMenuMessage(navigation=None, label="reports-ad-network", input_field="Выберите рекламную сеть")
    menu.keyboard = []
    for network in list_networks():
        slug = network.get("slug", "")
        count = len(list_runtime_specs(slug))
        label = _menu_label(network.get("display_name", slug), slug, f"{count} specs")
        _add_menu_row_button(menu, label=label, args=slug)
    return menu.gen_keyboard_content()


def spec_menu_keyboard(network_slug: str) -> ReplyKeyboardMarkup:
    menu = ReportsAdMenuMessage(navigation=None, label=f"reports-ad-spec-{network_slug}", input_field=SPEC_MENU_PROMPT)
    menu.keyboard = []
    for spec in list_runtime_specs(network_slug):
        label = _spec_menu_label(spec)
        _add_menu_row_button(menu, label=label, args=spec.slug)
    return menu.gen_keyboard_content()


def spec_menu_prompt(network_slug: str) -> str:
    return f"{SPEC_MENU_PROMPT}\n{specs_summary(network_slug)}"


def creative_upload_prompt(spec: CreativeSpec) -> str:
    limit = f"; максимальный размер файла: {spec.max_file_size_bytes} байт" if spec.max_file_size_bytes else ""
    return (
        f"Отправьте PNG/JPEG-креатив как фото или документ для выбранного spec. "
        f"Ожидаемые размеры: {spec.dimensions_label}{limit}."
    )


def _add_menu_row_button(menu: ReportsAdMenuMessage, *, label: str, args: str) -> None:
    new_row = bool(menu.keyboard and menu.keyboard[-1])
    menu.add_button(label=label, args=args, new_row=new_row)


def _menu_label(display_name: str, slug: str, detail: str = "") -> str:
    base = f"{display_name} [{slug}]"
    return f"{base} — {detail}" if detail else base


def _spec_menu_label(spec: CreativeSpec) -> str:
    label = _menu_label(spec.dimensions_label, spec.slug)
    if len(label) <= TELEGRAM_KEYBOARD_BUTTON_TEXT_LIMIT:
        return label

    label = f"[{spec.slug}]"
    if len(label) <= TELEGRAM_KEYBOARD_BUTTON_TEXT_LIMIT:
        return label

    return spec.slug


def _menu_value(text: str | None) -> str:
    value = (text or "").strip()
    if "[" in value and "]" in value:
        return value.split("[", 1)[1].split("]", 1)[0].strip()
    return value


def _clear_for_edit(user_data: dict[str, Any], target: str) -> None:
    if target == "network":
        _clear_from(user_data, "network_slug")
    elif target in {"spec", "banner"}:
        _clear_from(user_data, "spec")
    elif target in {"ad_url", "page"}:
        _clear_from(user_data, "ad_page_url", keep_creative=True)
    elif target == "creative":
        user_data.pop("creative", None)
    elif target == "landing":
        _clear_from(user_data, "landing_url")
    elif target == "utm":
        user_data.pop("utm_params", None)


def _clear_from(user_data: dict[str, Any], key: str, *, keep_creative: bool = False) -> None:
    order = ["network_slug", "network_display_name", "spec", "banner_type", "ad_page_url", "placement", "creative", "landing_url", "utm_params"]
    if key not in order:
        return
    for candidate in order[order.index(key) :]:
        if candidate == "creative" and keep_creative:
            continue
        user_data.pop(candidate, None)


def _edit_prompt(target: str, user_data: dict[str, Any]) -> str:
    if target == "network":
        return f"{NETWORK_MENU_PROMPT}\n{catalog_summary()}"
    if target in {"spec", "banner"}:
        network_slug = user_data.get("network_slug")
        if not network_slug:
            return f"Сначала выберите рекламную сеть.\n{NETWORK_MENU_PROMPT}\n{catalog_summary()}"
        return spec_menu_prompt(network_slug)
    if target in {"ad_url", "page"}:
        return "Введите URL рекламной страницы. Placement будет выбран заново по площадке и размеру spec."
    if target == "creative":
        spec = user_data.get("spec")
        if isinstance(spec, CreativeSpec):
            return creative_upload_prompt(spec)
        return "Загрузите новый PNG/JPEG-креатив как фото или документ."
    if target == "landing":
        return "Введите новый landing URL."
    if target == "utm":
        return "Введите новую UTM-строку, например: utm_source=telegram&utm_medium=cpc&utm_campaign=test"
    return "Введите новое значение."


def _edit_reply_markup(target: str, user_data: dict[str, Any]) -> ReplyKeyboardMarkup | ReplyKeyboardRemove:
    if target == "network":
        return network_menu_keyboard()
    if target in {"spec", "banner"}:
        network_slug = user_data.get("network_slug")
        if network_slug:
            return spec_menu_keyboard(network_slug)
        return network_menu_keyboard()
    return ReplyKeyboardRemove()


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
