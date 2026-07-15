from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw

from reports_ad.bot import build_application
from reports_ad.config import BotConfig
from reports_ad.gemma_client import _parse_bbox_result, build_gemma_client_from_config
from reports_ad.models import BBoxDetectionResult, BoundingBox, CreativeInfo, Placement, ProofRequest, ProofResult, SourceScreenshotInfo
from reports_ad.proof import compose_final_image
from reports_ad.stage2_proof import compose_stage2_image, fallback_landing_bbox, generate_stage2_proof
from reports_ad.catalog import get_spec
from reports_ad.validation import ValidationError


def test_compose_final_image_uses_landing_bottom_band(tmp_path: Path) -> None:
    ad_path = tmp_path / "ad.png"
    landing_path = tmp_path / "landing.png"
    output_path = tmp_path / "result.png"
    Image.new("RGB", (100, 100), "blue").save(ad_path)
    Image.new("RGB", (100, 100), "green").save(landing_path)

    width, height = compose_final_image(
        ad_screenshot=ad_path,
        landing_screenshot=landing_path,
        output_path=output_path,
        final_width=100,
        final_height=100,
        landing_ratio=0.2,
    )

    assert (width, height) == (100, 100)
    with Image.open(output_path) as result:
        assert result.size == (100, 100)
        assert result.getpixel((50, 10)) == (0, 0, 255)
        assert result.getpixel((50, 90)) == (0, 128, 0)


def test_stage2_compose_stretches_creative_draws_landing_and_ad_mark(tmp_path: Path) -> None:
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    output_path = tmp_path / "stage2.png"
    banner_path = tmp_path / "banner.png"
    landing_path = tmp_path / "landing.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    Image.new("RGBA", (20, 20), (255, 0, 0, 180)).save(creative_path)

    width, height = compose_stage2_image(
        source_screenshot_path=source_path,
        creative_path=creative_path,
        effective_click_url="https://example.com/landing?utm_source=test&utm_medium=cpc",
        banner_bbox=BoundingBox(x=50, y=40, width=120, height=80),
        landing_bbox=fallback_landing_bbox(image_width=400, image_height=300),
        output_path=output_path,
        banner_replaced_path=banner_path,
        landing_area_path=landing_path,
    )

    assert (width, height) == (400, 300)
    assert banner_path.exists()
    assert landing_path.exists()
    with Image.open(output_path) as result:
        assert result.getpixel((100, 70)) != (255, 255, 255)
        assert result.getpixel((62, 53)) != result.getpixel((200, 20))
        assert result.getpixel((200, 285)) != (255, 255, 255)


def test_stage2_compose_pastes_creative_into_exact_banner_region(tmp_path: Path) -> None:
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    output_path = tmp_path / "stage2.png"
    banner_path = tmp_path / "banner.png"
    Image.new("RGB", (240, 180), (255, 255, 255)).save(source_path)
    Image.new("RGBA", (10, 10), (20, 40, 220, 255)).save(creative_path)

    compose_stage2_image(
        source_screenshot_path=source_path,
        creative_path=creative_path,
        effective_click_url="https://example.com/landing?utm_source=test&utm_medium=cpc",
        banner_bbox=BoundingBox(x=80, y=30, width=60, height=90),
        landing_bbox=fallback_landing_bbox(image_width=240, image_height=180),
        output_path=output_path,
        banner_replaced_path=banner_path,
    )

    with Image.open(banner_path) as result:
        assert result.getpixel((130, 70)) == (20, 40, 220)
        assert result.getpixel((130, 105)) == (20, 40, 220)
        assert result.getpixel((79, 45)) == (255, 255, 255)
        assert result.getpixel((140, 45)) == (255, 255, 255)
        assert result.getpixel((90, 29)) == (255, 255, 255)
        assert result.getpixel((90, 120)) == (255, 255, 255)


def test_stage2_compose_trims_transparent_padding_before_stretching_to_slot(tmp_path: Path) -> None:
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    output_path = tmp_path / "stage2.png"
    banner_path = tmp_path / "banner.png"
    bbox = BoundingBox(x=60, y=40, width=160, height=100)
    source = Image.new("RGB", (300, 220), (255, 255, 255))
    source.save(source_path)
    creative = Image.new("RGBA", (120, 80), (0, 0, 0, 0))
    draw = ImageDraw.Draw(creative)
    draw.rectangle((30, 20, 89, 59), fill=(15, 35, 210, 255))
    creative.save(creative_path)

    compose_stage2_image(
        source_screenshot_path=source_path,
        creative_path=creative_path,
        effective_click_url="https://example.com/landing?utm_source=test&utm_medium=cpc",
        banner_bbox=bbox,
        landing_bbox=fallback_landing_bbox(image_width=300, image_height=220),
        output_path=output_path,
        banner_replaced_path=banner_path,
    )

    with Image.open(banner_path) as result:
        assert result.getpixel((bbox.x, bbox.y)) == (15, 35, 210)
        assert result.getpixel((bbox.right - 1, bbox.y)) == (15, 35, 210)
        assert result.getpixel((bbox.x, bbox.bottom - 1)) == (15, 35, 210)
        assert result.getpixel((bbox.right - 1, bbox.bottom - 1)) == (15, 35, 210)
        assert result.getpixel((bbox.x - 1, bbox.y + 10)) == (255, 255, 255)
        assert result.getpixel((bbox.right, bbox.y + 10)) == (255, 255, 255)


def test_stage2_compose_changed_region_matches_target_banner_bbox(tmp_path: Path) -> None:
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    banner_path = tmp_path / "banner.png"
    output_path = tmp_path / "stage2.png"
    bbox = BoundingBox(x=42, y=35, width=96, height=64)
    Image.new("RGB", (220, 180), (255, 255, 255)).save(source_path)
    Image.new("RGBA", (32, 32), (230, 30, 20, 255)).save(creative_path)

    compose_stage2_image(
        source_screenshot_path=source_path,
        creative_path=creative_path,
        effective_click_url="https://example.com/landing?utm_source=test&utm_medium=cpc",
        banner_bbox=bbox,
        landing_bbox=fallback_landing_bbox(image_width=220, image_height=180),
        output_path=output_path,
        banner_replaced_path=banner_path,
    )

    with Image.open(source_path) as source_raw, Image.open(banner_path) as result_raw:
        diff_bbox = ImageChops.difference(source_raw.convert("RGB"), result_raw.convert("RGB")).getbbox()

    assert diff_bbox == (bbox.x, bbox.y, bbox.right, bbox.bottom)


def test_fallback_landing_bbox_uses_lower_10_to_20_percent() -> None:
    bbox = fallback_landing_bbox(image_width=1000, image_height=800)

    assert bbox.x == 0
    assert bbox.width == 1000
    assert bbox.y == 672
    assert bbox.height == 128


def test_gemma_client_builder_uses_ocr_config_fields() -> None:
    config = BotConfig(
        telegram_token="test",
        ocr_llm_base_url="http://gemma.firstdata.io/v1/",
        ocr_llm_model="cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit",
        ocr_llm_authorization_header="Basic placeholder",
        ocr_llm_timeout_seconds=20,
        ocr_llm_max_retries=3,
        ocr_llm_confidence_threshold=0.6,
    )

    client = build_gemma_client_from_config(config)

    assert client._base_url == "http://gemma.firstdata.io/v1"
    assert client._model == "cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit"
    assert client._authorization_header == "Basic placeholder"
    assert client._timeout_seconds == 20
    assert client._max_retries == 3
    assert client._confidence_threshold == 0.6


def test_parse_bbox_result_rescales_from_resized_coordinate_space() -> None:
    result = _parse_bbox_result(
        {
            "bbox": {"x": 400, "y": 100, "width": 200, "height": 300},
            "confidence": 0.95,
            "reason": "bbox taken from resized preview",
            "coordinate_space": "resized_pixels",
            "image_width": 800,
            "image_height": 600,
        },
        stage="banner_bbox",
        confidence_threshold=0.45,
        source_image_width=1600,
        source_image_height=1200,
    )

    assert result.bbox == BoundingBox(800, 200, 400, 600)
    assert result.raw_metadata["coordinate_space"] == "resized_pixels"
    assert result.raw_metadata["coordinate_image_width"] == 800
    assert result.raw_metadata["coordinate_image_height"] == 600
    assert result.raw_metadata["scaled_to_source"] is True


def test_gemma_client_builder_rejects_missing_runtime_config() -> None:
    with pytest.raises(ValidationError, match="не инициализирована"):
        build_gemma_client_from_config(None)


def test_gemma_client_builder_requires_base_url() -> None:
    config = BotConfig(telegram_token="test", ocr_llm_base_url="", ocr_llm_model="cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit")

    with pytest.raises(ValidationError, match="REPORTS_AD_OCR_LLM_BASE_URL"):
        build_gemma_client_from_config(config)


@pytest.mark.asyncio
async def test_stage2_generate_uses_mocked_gemma_and_fallback_landing(tmp_path: Path) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    Image.new("RGB", (300, 250), "blue").save(creative_path)
    request = ProofRequest(
        request_id="req-stage2",
        network_display_name="Яндекс",
        network_slug="yandex_graphic_banners",
        spec=spec,
        banner_type=spec.display_name,
        source_screenshot=SourceScreenshotInfo(path=source_path, mime_type="image/png", width=400, height=300, size_bytes=source_path.stat().st_size),
        creative=CreativeInfo(path=creative_path, mime_type="image/png", width=300, height=250, size_bytes=creative_path.stat().st_size),
        landing_url="https://example.com/landing",
        utm_params={"utm_source": "test"},
        placement=Placement(mode="template", value="screenshot-detected"),
        output_path=tmp_path / "final.png",
    )

    class MockGemma:
        async def detect_banner_bbox(self, **kwargs):
            return BBoxDetectionResult(bbox=BoundingBox(20, 30, 100, 70), confidence=0.9, source="mock")

        async def detect_landing_bbox(self, **kwargs):
            return BBoxDetectionResult(bbox=None, confidence=0.0, source="mock")

    result = await generate_stage2_proof(request, gemma_client=MockGemma())

    assert result.output_path.exists()
    assert result.banner_bbox == BoundingBox(20, 30, 100, 70)
    assert result.landing_fallback_used is True
    assert result.landing_bbox == fallback_landing_bbox(image_width=400, image_height=300)


@pytest.mark.asyncio
async def test_build_application_injects_runtime_config_into_stage2_generator(tmp_path: Path) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    output_path = tmp_path / "final.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    Image.new("RGB", (300, 250), "blue").save(creative_path)
    config = BotConfig(
        telegram_token="test",
        temp_root=tmp_path,
        ocr_llm_base_url="http://gemma.firstdata.io/v1",
        ocr_llm_model="cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit",
    )
    request = ProofRequest(
        request_id="req-stage2-config",
        network_display_name="Яндекс",
        network_slug="yandex_graphic_banners",
        spec=spec,
        banner_type=spec.display_name,
        source_screenshot=SourceScreenshotInfo(path=source_path, mime_type="image/png", width=400, height=300, size_bytes=source_path.stat().st_size),
        creative=CreativeInfo(path=creative_path, mime_type="image/png", width=300, height=250, size_bytes=creative_path.stat().st_size),
        landing_url="https://example.com/landing",
        utm_params={"utm_source": "test"},
        placement=Placement(mode="template", value="screenshot-detected"),
        output_path=output_path,
    )
    captured: dict[str, object] = {}

    async def fake_stage2_generator(stage2_request: ProofRequest, *, config: BotConfig | None = None):
        captured["request"] = stage2_request
        captured["config"] = config
        output_path.write_bytes(b"fake-png")
        return ProofResult(
            output_path=output_path,
            width=400,
            height=300,
            effective_click_url="https://example.com/landing?utm_source=test",
            request_id=stage2_request.request_id,
        )

    application = build_application(config, proof_generator=fake_stage2_generator)
    configured_generator = application.bot_data["proof_generator"]

    await configured_generator(request)

    assert captured["request"] == request
    assert captured["config"] is config


@pytest.mark.asyncio
async def test_stage2_generate_invalid_banner_bbox_asks_for_better_screenshot(tmp_path: Path) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    Image.new("RGB", (300, 250), "blue").save(creative_path)
    request = ProofRequest(
        request_id="req-stage2",
        network_display_name="Яндекс",
        network_slug="yandex_graphic_banners",
        spec=spec,
        banner_type=spec.display_name,
        source_screenshot=SourceScreenshotInfo(path=source_path, mime_type="image/png", width=400, height=300, size_bytes=source_path.stat().st_size),
        creative=CreativeInfo(path=creative_path, mime_type="image/png", width=300, height=250, size_bytes=creative_path.stat().st_size),
        landing_url="https://example.com/landing",
        utm_params={"utm_source": "test"},
        placement=Placement(mode="template", value="screenshot-detected"),
        output_path=tmp_path / "final.png",
    )

    class MockGemma:
        async def detect_banner_bbox(self, **kwargs):
            return BBoxDetectionResult(bbox=BoundingBox(390, 10, 50, 50), confidence=0.9, source="mock")

        async def detect_landing_bbox(self, **kwargs):
            return BBoxDetectionResult(bbox=None, confidence=0.0, source="mock")

    try:
        await generate_stage2_proof(request, gemma_client=MockGemma())
    except ValidationError as exc:
        assert "Загрузите более качественный скриншот" in exc.safe_message
    else:
        raise AssertionError("Expected ValidationError")


@pytest.mark.asyncio
async def test_stage2_generate_requires_ocr_config_when_client_not_injected(tmp_path: Path) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    Image.new("RGB", (300, 250), "blue").save(creative_path)
    request = ProofRequest(
        request_id="req-stage2-misconfig",
        network_display_name="Яндекс",
        network_slug="yandex_graphic_banners",
        spec=spec,
        banner_type=spec.display_name,
        source_screenshot=SourceScreenshotInfo(path=source_path, mime_type="image/png", width=400, height=300, size_bytes=source_path.stat().st_size),
        creative=CreativeInfo(path=creative_path, mime_type="image/png", width=300, height=250, size_bytes=creative_path.stat().st_size),
        landing_url="https://example.com/landing",
        utm_params={"utm_source": "test"},
        placement=Placement(mode="template", value="screenshot-detected"),
        output_path=tmp_path / "final.png",
    )
    config = BotConfig(telegram_token="test", ocr_llm_base_url="")

    with pytest.raises(ValidationError, match="REPORTS_AD_OCR_LLM_BASE_URL"):
        await generate_stage2_proof(request, config=config)
