from __future__ import annotations

import pytest

from .assertions import assert_png_image, assert_safe_telegram_text
from .fixtures import CREATIVE_300_250, SOURCE_YANDEX_BANNER, assert_fixture_files_exist, require_live_e2e_config
from .telegram_driver import TelegramUserDriver


pytestmark = [pytest.mark.telegram_e2e, pytest.mark.network, pytest.mark.slow]


@pytest.mark.asyncio
async def test_e2e_001_yandex_banner_happy_path() -> None:
    """E2E-001: live Telegram happy path with Yandex banner fixtures."""

    assert_fixture_files_exist()
    config = require_live_e2e_config()

    async with TelegramUserDriver(config) as driver:
        await driver.send_text("/start")
        start_reply = await driver.wait_for_text_contains("Я сформирую proof-скриншот", "Выберите рекламную сеть", action="E2E-001 start prompt")
        assert_safe_telegram_text(start_reply.text or "")

        await driver.send_text("Яндекс [yandex_graphic_banners] — 12 specs")
        network_reply = await driver.wait_for_text_contains("Сеть выбрана: Яндекс", "Теперь выберите тип баннера/spec", action="E2E-001 network selection")
        assert_safe_telegram_text(network_reply.text or "")

        await driver.send_text("[yandex-graphic-banners-300x250-image]")
        spec_reply = await driver.wait_for_text_contains("Тип баннера выбран", "Отправьте готовый скриншот площадки", action="E2E-001 spec selection")
        assert_safe_telegram_text(spec_reply.text or "")

        await driver.send_document(SOURCE_YANDEX_BANNER)
        screenshot_reply = await driver.wait_for_text_contains("Скриншот принят", "Отправьте PNG/JPEG-креатив", action="E2E-001 source screenshot upload")
        assert_safe_telegram_text(screenshot_reply.text or "")

        await driver.send_document(CREATIVE_300_250)
        creative_reply = await driver.wait_for_text_contains("Креатив принят", "Введите landing URL", action="E2E-001 creative upload")
        assert_safe_telegram_text(creative_reply.text or "")

        await driver.send_text("https://example.com/landing")
        landing_reply = await driver.wait_for_text_contains("Введите UTM-строку", action="E2E-001 landing URL")
        assert_safe_telegram_text(landing_reply.text or "")

        await driver.send_text("utm_source=telegram_e2e&utm_medium=test&utm_campaign=reports_ad_yandex_banner")
        review_reply = await driver.wait_for_text_contains(
            "Проверьте заявку перед запуском",
            "сеть:",
            "тип баннера:",
            "скриншот площадки:",
            "креатив:",
            "landing:",
            "UTM-ключи:",
            action="E2E-001 review screen",
        )
        assert_safe_telegram_text(review_reply.text or "")

        await driver.send_text("Запустить")
        generating_reply = await driver.wait_for_text_contains("Генерирую proof-скриншот", action="E2E-001 generation started")
        assert_safe_telegram_text(generating_reply.text or "")

        photo_reply = await driver.wait_for_photo(caption_contains="Proof-скриншот готов.", timeout=config.timeout_seconds)
        assert_safe_telegram_text(photo_reply.caption or "")

        document_reply = await driver.wait_for_document(caption_contains="Proof-скриншот как документ без сжатия.", timeout=config.timeout_seconds)
        assert_safe_telegram_text(document_reply.caption or "")

        output_path = await driver.download_media(document_reply, config.artifact_dir, filename="e2e-001-proof-result.png")
        assert_png_image(output_path)
