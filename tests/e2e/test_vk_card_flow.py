"""Live Telegram VK-card E2E (opt-in, dedicated test bot credentials required)."""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from .assertions import assert_png_image
from .fixtures import require_live_e2e_config
from .telegram_driver import TelegramUserDriver


pytestmark = [pytest.mark.telegram_e2e, pytest.mark.network, pytest.mark.slow]


@pytest.mark.asyncio
async def test_vk_card_user_can_edit_and_approve(tmp_path: Path) -> None:
    config = require_live_e2e_config()
    creative = tmp_path / "vk-e2e-creative.png"
    logo = tmp_path / "vk-e2e-logo.png"
    Image.new("RGB", (1080, 607), "#3219a4").save(creative)
    Image.new("RGB", (160, 160), "#42b2cc").save(logo)
    async with TelegramUserDriver(config) as driver:
        await driver.send_text("/start")
        await driver.wait_for_text_contains("Выберите рекламную сеть")
        await driver.send_text("vk")
        await driver.wait_for_text_contains("Сеть выбрана", "ВК")
        await driver.send_text("1080x607")
        await driver.wait_for_text_contains("Отправьте PNG/JPEG-креатив")
        await driver.send_document(creative)
        await driver.wait_for_text_contains("Загрузите логотип")
        await driver.send_document(logo)
        await driver.wait_for_text_contains("Введите верхний текст")
        await driver.send_text("Рекламная карточка")
        await driver.wait_for_text_contains("Введите весь основной текст")
        await driver.send_text("Вопрос?\n\nОписание с абзацами.")
        await driver.wait_for_text_contains("Выберите надпись кнопки")
        await driver.send_text("Подробнее")
        await driver.wait_for_text_contains("Подтвердите возрастную пометку")
        await driver.send_text("18")
        await driver.wait_for_photo(caption_contains="Предпросмотр макета ВК")
        await driver.send_text("Изменить верхний текст")
        await driver.wait_for_text_contains("Введите верхний текст")
        await driver.send_text("Обновлённая карточка")
        await driver.wait_for_photo(caption_contains="Предпросмотр макета ВК")
        await driver.send_text("✅ Подтвердить и получить PNG")
        sent = await driver.wait_for_document(caption_contains="Утверждённый PNG-макет")
        png = await driver.download_media(sent, config.artifact_dir, filename="vk-card-v1-e2e.png")
        assert_png_image(png)
        with Image.open(png) as check:
            assert check.width == 1080
            assert check.height >= 1091
