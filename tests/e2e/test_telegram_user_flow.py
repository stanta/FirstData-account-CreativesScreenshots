from __future__ import annotations

import pytest

from .assertions import assert_png_image, assert_safe_telegram_text
from .fixtures import CREATIVE_240_400, assert_fixture_files_exist, require_live_e2e_config
from .telegram_driver import TelegramUserDriver


pytestmark = [pytest.mark.telegram_e2e, pytest.mark.network, pytest.mark.slow]


@pytest.mark.asyncio
async def test_e2e_001_mediasniper_mask_happy_path() -> None:
    """E2E-001: live Telegram happy path with Mediasniper mask overlay."""

    assert_fixture_files_exist()
    config = require_live_e2e_config()

    async with TelegramUserDriver(config) as driver:
        await driver.send_text("/start")
        start_reply = await driver.wait_for_text_contains("Я сформирую баннер", "Выберите рекламную сеть", action="E2E-001 start prompt")
        assert_safe_telegram_text(start_reply.text or "")

        await driver.send_text("Mediasniper [mediasniper] — 3 размера")
        network_reply = await driver.wait_for_text_contains("Сеть выбрана: Mediasniper", "Теперь выберите размер баннера", action="E2E-001 network selection")
        assert_safe_telegram_text(network_reply.text or "")

        await driver.send_text("240×400 [240x400]")
        spec_reply = await driver.wait_for_text_contains("Размер баннера выбран", "Отправьте PNG/JPEG-креатив", action="E2E-001 size selection")
        assert_safe_telegram_text(spec_reply.text or "")

        await driver.send_document(CREATIVE_240_400)
        creative_reply = await driver.wait_for_text_contains("Креатив принят", "Накладываю маску", action="E2E-001 creative upload and generation")
        assert_safe_telegram_text(creative_reply.text or "")

        photo_reply = await driver.wait_for_photo(caption_contains="Баннер с маской готов.", timeout=config.timeout_seconds)
        assert_safe_telegram_text(photo_reply.caption or "")

        document_reply = await driver.wait_for_document(caption_contains="PNG-баннер с маской без сжатия.", timeout=config.timeout_seconds)
        assert_safe_telegram_text(document_reply.caption or "")

        output_path = await driver.download_media(document_reply, config.artifact_dir, filename="e2e-001-masked-banner.png")
        assert_png_image(output_path)
