from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw

from reports_ad.bot import build_application
from reports_ad.config import BotConfig
from reports_ad.gemma_client import GemmaClientError, HeaderAuthGemmaClient, _banner_prompt, _composition_validation_crop_rect, _composition_validation_prompt, _parse_bbox_result, _parse_composition_validation_result, build_gemma_client_from_config
from reports_ad.models import BBoxDetectionResult, BoundingBox, CompositionValidationResult, CreativeInfo, Placement, ProofRequest, ProofResult, SourceScreenshotInfo
from reports_ad.proof import compose_final_image
from reports_ad.stage2_proof import compose_stage2_image, fallback_landing_bbox, generate_stage2_proof, validate_banner_bbox_semantics, select_valid_banner_bbox_with_feedback
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


def test_validate_banner_bbox_semantics_rejects_right_sidebar_bbox_outside_right_third() -> None:
    spec = get_spec("first-data-rtb-banners-240x400-image")
    assert spec is not None

    with pytest.raises(ValidationError, match="неправдоподобную область"):
        validate_banner_bbox_semantics(
            BoundingBox(x=707, y=197, width=240, height=400),
            spec=spec,
            image_width=1280,
            image_height=719,
            request_id="req-off-target",
        )


def test_validate_banner_bbox_semantics_accepts_right_sidebar_bbox_in_right_third() -> None:
    spec = get_spec("first-data-rtb-banners-240x400-image")
    assert spec is not None

    bbox = BoundingBox(x=874, y=146, width=240, height=400)

    assert (
        validate_banner_bbox_semantics(
            bbox,
            spec=spec,
            image_width=1280,
            image_height=719,
            request_id="req-right-target",
        )
        == bbox
    )


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


def test_tgo_semantic_banner_bbox_rejects_wrong_internal_image_shape(caplog: pytest.LogCaptureFixture) -> None:
    spec = get_spec("yandex-tgo-600x600-text-image")
    assert spec is not None
    bbox = BoundingBox(x=922, y=215, width=217, height=373)

    with caplog.at_level("INFO", logger="reports_ad.stage2_proof"), pytest.raises(ValidationError, match="неправдоподобную область баннера"):
        validate_banner_bbox_semantics(
            bbox,
            spec=spec,
            image_width=1280,
            image_height=828,
            request_id="ee76e0a22f96",
        )

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "stage2_banner_bbox_semantic_validation request_id=ee76e0a22f96" in log_text
    assert "spec_slug=yandex-tgo-600x600-text-image" in log_text
    assert "decision=rejected" in log_text
    assert "expected_ratio=1.000" in log_text
    assert "reason_codes=aspect_ratio_deviation" in log_text


def test_tgo_semantic_banner_bbox_accepts_plausible_square_slot(caplog: pytest.LogCaptureFixture) -> None:
    spec = get_spec("yandex-tgo-600x600-text-image")
    assert spec is not None
    bbox = BoundingBox(x=680, y=140, width=360, height=350)

    with caplog.at_level("INFO", logger="reports_ad.stage2_proof"):
        result = validate_banner_bbox_semantics(
            bbox,
            spec=spec,
            image_width=1280,
            image_height=828,
            request_id="req-plausible-tgo",
        )

    assert result == bbox
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "stage2_banner_bbox_semantic_validation request_id=req-plausible-tgo" in log_text
    assert "decision=accepted" in log_text
    assert "reason_codes=none" in log_text


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
    assert client._ocr_image_max_width == 1280
    assert client._ocr_image_max_pixels == 2_000_000
    assert client._composition_validation_crop_margin_percent == 0.20
    assert client._composition_validation_crop_margin_min_pixels == 60


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


def test_parse_bbox_result_rejects_invalid_normalized_coordinate_space() -> None:
    with pytest.raises(GemmaClientError, match="invalid_coordinate_space"):
        _parse_bbox_result(
            {
                "bbox": {"x": 922, "y": 215, "width": 217, "height": 373},
                "confidence": 0.95,
                "coordinate_space": "normalized",
                "reason": "model mislabeled pixel coordinates as normalized",
            },
            stage="banner_bbox",
            confidence_threshold=0.45,
            source_image_width=1280,
            source_image_height=828,
        )


def test_banner_retry_prompt_strictly_forbids_mixed_coordinate_spaces() -> None:
    spec = get_spec("first-data-rtb-banners-240x400-image")
    assert spec is not None
    screenshot = SourceScreenshotInfo(path=Path("source.png"), mime_type="image/png", width=1280, height=719, size_bytes=123)

    prompt = _banner_prompt(
        screenshot=screenshot,
        spec=spec,
        ocr_image_width=1280,
        ocr_image_height=719,
        validation_feedback="previous bbox x=902,y=147,width=154,height=396 was rejected by aspect ratio",
        selection_attempt=2,
    )

    assert "Coordinate contract is strict and MUST NOT be mixed" in prompt
    assert "NEVER put pixel numbers with coordinate_space='normalized'" in prompt
    assert "values such as 707 or 116 are INVALID" in prompt
    assert "if bbox values are pixel-like numbers greater than 1" in prompt
    assert "coordinate_space MUST be 'source_pixels' or 'resized_pixels'" in prompt
    assert "x=0.70 not x=707" in prompt


def test_parse_bbox_result_requires_resized_pixel_image_size() -> None:
    with pytest.raises(GemmaClientError, match="invalid_coordinate_space"):
        _parse_bbox_result(
            {
                "bbox": {"x": 400, "y": 100, "width": 200, "height": 300},
                "confidence": 0.95,
                "coordinate_space": "resized_pixels",
            },
            stage="banner_bbox",
            confidence_threshold=0.45,
            source_image_width=1600,
            source_image_height=1200,
        )


def test_parse_composition_validation_result_requires_structured_pass_flag() -> None:
    with pytest.raises(GemmaClientError, match="verification_passed"):
        _parse_composition_validation_result(
            {"confidence": 0.9, "reason": "missing pass flag"},
            source_image_width=400,
            source_image_height=300,
            confidence_threshold=0.45,
        )


def test_parse_composition_validation_result_detects_exact_pass_and_correction() -> None:
    passed = _parse_composition_validation_result(
        {
            "verification_passed": True,
            "original_banner_bbox": {"x": 20, "y": 30, "width": 100, "height": 70},
            "actual_inserted_creative_bbox": {"x": 20, "y": 30, "width": 100, "height": 70},
            "deltas": {"x": 0, "y": 0, "width": 0, "height": 0},
            "recommended_corrected_bbox": None,
            "confidence": 0.9,
            "reason": "exact match",
        },
        source_image_width=400,
        source_image_height=300,
        confidence_threshold=0.45,
    )
    failed = _parse_composition_validation_result(
        {
            "verification_passed": False,
            "original_banner_bbox": {"x": 40, "y": 50, "width": 120, "height": 80},
            "actual_inserted_creative_bbox": {"x": 20, "y": 30, "width": 100, "height": 70},
            "deltas": {"x": -20, "y": -20, "width": -20, "height": -10},
            "recommended_corrected_bbox": {"x": 40, "y": 50, "width": 120, "height": 80},
            "confidence": 0.95,
            "reason": "offset mismatch",
        },
        source_image_width=400,
        source_image_height=300,
        confidence_threshold=0.45,
    )

    assert passed.passed is True
    assert passed.corrected_bbox == BoundingBox(20, 30, 100, 70)
    assert failed.passed is False
    assert failed.corrected_bbox == BoundingBox(40, 50, 120, 80)
    assert failed.deltas == {"x": -20, "y": -20, "width": -20, "height": -10}


def test_composition_validation_crop_rect_expands_and_clamps_bbox() -> None:
    crop = _composition_validation_crop_rect(
        bbox=BoundingBox(20, 30, 100, 70),
        image_width=400,
        image_height=300,
        margin_percent=0.20,
        margin_min_pixels=60,
    )

    assert (crop.x, crop.y, crop.width, crop.height) == (0, 0, 180, 160)
    assert (crop.right, crop.bottom) == (180, 160)
    assert (crop.margin_x, crop.margin_y) == (60, 60)


def test_composition_validation_prompt_declares_crop_coordinate_contract() -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    screenshot = SourceScreenshotInfo(path=Path("source.png"), mime_type="image/png", width=400, height=300, size_bytes=123)
    bbox = BoundingBox(80, 70, 120, 80)
    crop = _composition_validation_crop_rect(
        bbox=bbox,
        image_width=400,
        image_height=300,
        margin_percent=0.20,
        margin_min_pixels=40,
    )

    prompt = _composition_validation_prompt(
        screenshot=screenshot,
        spec=spec,
        current_banner_bbox=bbox,
        ocr_image_width=crop.width,
        ocr_image_height=crop.height,
        validation_attempt=1,
        crop=crop,
    )

    assert "CROPPED images around the current banner candidate" in prompt
    assert "crop origin is" in prompt
    assert "coordinate_space='crop_pixels'" in prompt
    assert "Return full source screenshot pixel coordinates" in prompt


def test_parse_composition_validation_result_converts_crop_pixels_to_source_space() -> None:
    crop = _composition_validation_crop_rect(
        bbox=BoundingBox(80, 70, 120, 80),
        image_width=400,
        image_height=300,
        margin_percent=0.20,
        margin_min_pixels=40,
    )

    result = _parse_composition_validation_result(
        {
            "verification_passed": False,
            "original_banner_bbox": {"x": 30, "y": 40, "width": 130, "height": 90},
            "actual_inserted_creative_bbox": {"x": 40, "y": 40, "width": 120, "height": 80},
            "deltas": {"x": 10, "y": 0, "width": -10, "height": -10},
            "recommended_corrected_bbox": {"x": 30, "y": 40, "width": 130, "height": 90},
            "confidence": 0.95,
            "reason": "crop local correction",
            "coordinate_space": "crop_pixels",
            "image_width": crop.width,
            "image_height": crop.height,
        },
        source_image_width=400,
        source_image_height=300,
        confidence_threshold=0.45,
        crop=crop,
    )

    assert result.passed is False
    assert result.original_banner_bbox == BoundingBox(70, 70, 130, 90)
    assert result.inserted_creative_bbox == BoundingBox(80, 70, 120, 80)
    assert result.corrected_bbox == BoundingBox(70, 70, 130, 90)
    assert result.raw_metadata["crop_origin_x"] == 40
    assert result.raw_metadata["crop_origin_y"] == 30


def test_parse_composition_validation_result_preserves_source_pixel_crop_response() -> None:
    crop = _composition_validation_crop_rect(
        bbox=BoundingBox(80, 70, 120, 80),
        image_width=400,
        image_height=300,
        margin_percent=0.20,
        margin_min_pixels=40,
    )

    result = _parse_composition_validation_result(
        {
            "verification_passed": True,
            "original_banner_bbox": {"x": 80, "y": 70, "width": 120, "height": 80},
            "actual_inserted_creative_bbox": {"x": 80, "y": 70, "width": 120, "height": 80},
            "deltas": {"x": 0, "y": 0, "width": 0, "height": 0},
            "recommended_corrected_bbox": None,
            "confidence": 0.95,
            "reason": "exact source coords",
            "coordinate_space": "source_pixels",
        },
        source_image_width=400,
        source_image_height=300,
        confidence_threshold=0.45,
        crop=crop,
    )

    assert result.passed is True
    assert result.corrected_bbox == BoundingBox(80, 70, 120, 80)
    assert result.raw_metadata["crop_width"] == crop.width


def test_tgo_candidates_choose_full_ad_card_over_internal_image_asset() -> None:
    spec = get_spec("yandex-tgo-600x600-text-image")
    assert spec is not None

    result = _parse_bbox_result(
        {
            "candidates": [
                {
                    "role": "internal_image_asset",
                    "bbox": {"x": 820, "y": 190, "width": 225, "height": 400},
                    "confidence": 0.99,
                    "coordinate_space": "resized_pixels",
                    "image_width": 1280,
                    "image_height": 828,
                },
                {
                    "role": "full_ad_card",
                    "bbox": {"x": 680, "y": 140, "width": 360, "height": 350},
                    "confidence": 0.80,
                    "coordinate_space": "resized_pixels",
                    "image_width": 1280,
                    "image_height": 828,
                },
                {
                    "role": "browser_ui",
                    "bbox": {"x": 0, "y": 760, "width": 1280, "height": 68},
                    "confidence": 1.0,
                    "coordinate_space": "resized_pixels",
                    "image_width": 1280,
                    "image_height": 828,
                },
            ],
            "confidence": 0.1,
            "reason": "multiple candidates",
        },
        stage="banner_bbox",
        spec=spec,
        confidence_threshold=0.45,
        source_image_width=1280,
        source_image_height=828,
    )

    assert result.bbox == BoundingBox(680, 140, 360, 350)
    assert result.confidence == 0.80
    assert result.raw_metadata["candidate_role"] == "full_ad_card"


def test_banner_prompt_includes_selected_spec_dimensions_and_position_hint() -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    screenshot = SourceScreenshotInfo(path=Path("source.png"), mime_type="image/png", width=1024, height=768, size_bytes=123)

    prompt = _banner_prompt(screenshot=screenshot, spec=spec)

    assert spec.slug in prompt
    assert spec.dimensions_label in prompt
    assert spec.position_hint in prompt
    assert "original ad slot" in prompt
    assert "uploaded replacement creative" in prompt
    assert "landing/link/browser area" in prompt
    assert "coordinate_space='normalized'" in prompt
    assert "source screenshot" in prompt


def test_banner_prompt_includes_validation_feedback_for_retry() -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    screenshot = SourceScreenshotInfo(path=Path("source.png"), mime_type="image/png", width=1024, height=768, size_bytes=123)

    prompt = _banner_prompt(screenshot=screenshot, spec=spec, validation_feedback="reason_codes=out_of_bounds; previous_invalid_bbox=x=390,y=10,width=50,height=50", selection_attempt=2)

    assert "Base task instructions:" in prompt
    assert "Retry feedback:" in prompt
    assert "Previous validation feedback for this retry:" in prompt
    assert "selection attempt 2" in prompt
    assert "previous bbox candidate was rejected" in prompt
    assert "reason_codes=out_of_bounds" in prompt
    assert "Do NOT repeat the previous invalid bbox" in prompt
    assert "previous_invalid_bbox=x=390,y=10,width=50,height=50" in prompt
    assert "coordinate contract" in prompt


def test_banner_retry_prompt_keeps_full_base_instructions_with_feedback_section() -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    screenshot = SourceScreenshotInfo(path=Path("source.png"), mime_type="image/png", width=1024, height=768, size_bytes=123)
    feedback = (
        "validation_error=ИИ вернул область баннера за границами скриншота; "
        "selected_spec=yandex-graphic-banners-300x250-image; "
        "expected_dimensions=300×250; previous_invalid_bbox=x=390,y=10,width=50,height=50; "
        "previous_invalid_center=x=415.0,y=35.0; previous_invalid_zone=top_right; "
        "reason_codes=out_of_bounds"
    )

    base_prompt = _banner_prompt(screenshot=screenshot, spec=spec)
    retry_prompt = _banner_prompt(screenshot=screenshot, spec=spec, validation_feedback=feedback, selection_attempt=2)

    assert base_prompt in retry_prompt
    assert retry_prompt.index("Base task instructions:") < retry_prompt.index("Retry feedback:")
    assert retry_prompt.count("Base task instructions:") == 1
    assert retry_prompt.count("Retry feedback:") == 1
    assert "Find the visible original ad slot area" in retry_prompt
    assert "Selected spec dimensions: 300×250" in retry_prompt
    assert spec.position_hint in retry_prompt
    assert "The source screenshot is 1024x768 pixels" in retry_prompt
    assert "Previous validation feedback for this retry:" in retry_prompt
    assert "validation_error=" in retry_prompt
    assert "reason_codes=out_of_bounds" in retry_prompt
    assert "previous_invalid_bbox=x=390,y=10,width=50,height=50" in retry_prompt
    assert "previous_invalid_center=x=415.0,y=35.0" in retry_prompt
    assert "previous_invalid_zone=top_right" in retry_prompt
    assert "Do NOT repeat the previous invalid bbox" in retry_prompt
    assert "Base task instructions remain authoritative" in retry_prompt


def test_tgo_banner_prompt_selects_full_visible_ad_card_container() -> None:
    spec = get_spec("yandex-tgo-600x600-text-image")
    assert spec is not None
    screenshot = SourceScreenshotInfo(path=Path("source.png"), mime_type="image/png", width=1280, height=828, size_bytes=123)

    prompt = _banner_prompt(screenshot=screenshot, spec=spec)

    assert "full visible ad card/container including image, text and CTA" in prompt
    assert "not only the internal image asset" in prompt
    assert "candidates array" in prompt
    assert "full_ad_card" in prompt
    assert "original_ad_slot" in prompt
    assert "internal_image_asset" in prompt
    assert "landing_area" in prompt
    assert "browser_ui" in prompt
    assert "unknown" in prompt
    assert "coordinate_space='normalized'" in prompt
    assert spec.position_hint in prompt


@pytest.mark.asyncio
async def test_ocr_request_response_text_logs_are_disabled_by_default(tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REPORTS_AD_OCR_LOG_TEXT", raising=False)
    monkeypatch.delenv("REPORTS_AD_OCR_LOG_TEXT_MAX_CHARS", raising=False)
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    screenshot = SourceScreenshotInfo(path=source_path, mime_type="image/png", width=400, height=300, size_bytes=source_path.stat().st_size)
    client = HeaderAuthGemmaClient(
        base_url="http://ocr.example.test/v1",
        model="test-model",
        authorization_header="Basic super-secret-token",
        max_retries=1,
    )

    async def fake_execute_request(**kwargs):
        assert "Authorization" not in kwargs
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"bbox":{"x":0.1,"y":0.2,"width":0.3,"height":0.4},"confidence":0.91,"coordinate_space":"normalized","reason":"Authorization: Bearer raw-secret Password=hidden"}'
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "authorization": "Bearer should-not-log",
        }

    monkeypatch.setattr(client, "_execute_request", fake_execute_request)

    with caplog.at_level("INFO", logger="reports_ad.gemma_client"):
        result = await client.detect_banner_bbox(screenshot=screenshot, spec=spec, request_id="req-logs")

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert result.bbox == BoundingBox(40, 60, 120, 120)
    assert "ocr_llm_request request_id=req-logs stage=banner_bbox" in log_text
    assert "ocr_llm_response request_id=req-logs stage=banner_bbox" in log_text
    assert "ocr_llm_raw_response request_id=req-logs stage=banner_bbox" in log_text
    assert "prompt_sha256=" in log_text
    assert "response_text_sha256=" in log_text
    assert "prompt_text=disabled" in log_text
    assert "response_text=disabled" in log_text
    assert "Basic super-secret-token" not in log_text
    assert "raw-secret" not in log_text
    assert "Password=hidden" not in log_text
    assert "should-not-log" not in log_text
    assert "data:image" not in log_text


@pytest.mark.asyncio
async def test_ocr_request_response_logs_include_full_text_when_enabled(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    screenshot = SourceScreenshotInfo(path=source_path, mime_type="image/png", width=400, height=300, size_bytes=source_path.stat().st_size)
    raw_response = '{"bbox":{"x":0.1,"y":0.2,"width":0.3,"height":0.4},"confidence":0.91,"coordinate_space":"normalized","reason":"full-text-response-marker"}'
    client = HeaderAuthGemmaClient(
        base_url="http://ocr.example.test/v1",
        model="test-model",
        authorization_header="Basic super-secret-token",
        max_retries=1,
        ocr_log_text=True,
        ocr_log_text_max_chars=0,
    )

    async def fake_execute_request(**kwargs):
        return {"choices": [{"message": {"content": raw_response}}], "usage": {"prompt_tokens": 10}}

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(client, "_execute_request", fake_execute_request)
        with caplog.at_level("INFO", logger="reports_ad.gemma_client"):
            await client.detect_banner_bbox(screenshot=screenshot, spec=spec, request_id="req-full-logs")
    finally:
        monkeypatch.undo()

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "prompt_text=disabled" not in log_text
    assert "response_text=disabled" not in log_text
    assert "truncated:" not in log_text
    assert "Base task instructions: You are analyzing a placement screenshot" in log_text
    assert "full-text-response-marker" in log_text
    assert raw_response in log_text
    assert "Basic super-secret-token" not in log_text
    assert "data:image" not in log_text


@pytest.mark.asyncio
async def test_landing_bbox_logs_full_prompt_and_response_when_enabled(tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    screenshot = SourceScreenshotInfo(path=source_path, mime_type="image/png", width=400, height=300, size_bytes=source_path.stat().st_size)
    raw_response = '{"bbox":null,"confidence":0,"coordinate_space":"normalized","reason":"landing-full-response-marker"}'
    client = HeaderAuthGemmaClient(
        base_url="http://ocr.example.test/v1",
        model="test-model",
        authorization_header=None,
        max_retries=1,
        ocr_log_text=True,
        ocr_log_text_max_chars=0,
    )

    async def fake_execute_request(**kwargs):
        return {"choices": [{"message": {"content": raw_response}}]}

    monkeypatch.setattr(client, "_execute_request", fake_execute_request)

    with caplog.at_level("INFO", logger="reports_ad.gemma_client"):
        await client.detect_landing_bbox(screenshot=screenshot, spec=spec, request_id="req-landing-logs")

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "ocr_llm_request request_id=req-landing-logs stage=landing_bbox" in log_text
    assert "Find the browser/link/landing area template location" in log_text
    assert "ocr_llm_raw_response request_id=req-landing-logs stage=landing_bbox" in log_text
    assert raw_response in log_text
    assert "landing-full-response-marker" in log_text
    assert "disabled" not in log_text
    assert "data:image" not in log_text


@pytest.mark.asyncio
async def test_composition_validation_logs_full_prompt_and_raw_response_when_enabled(tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    composed_path = tmp_path / "composed.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    Image.new("RGB", (400, 300), "white").save(composed_path)
    screenshot = SourceScreenshotInfo(path=source_path, mime_type="image/png", width=400, height=300, size_bytes=source_path.stat().st_size)
    raw_response = '{"verification_passed":true,"original_banner_bbox":{"x":20,"y":30,"width":100,"height":70},"actual_inserted_creative_bbox":{"x":20,"y":30,"width":100,"height":70},"deltas":{"x":0,"y":0,"width":0,"height":0},"recommended_corrected_bbox":null,"confidence":0.99,"reason":"composition-full-response-marker","coordinate_space":"source_pixels"}'
    client = HeaderAuthGemmaClient(
        base_url="http://ocr.example.test/v1",
        model="test-model",
        authorization_header=None,
        max_retries=1,
        ocr_log_text=True,
        ocr_log_text_max_chars=0,
    )

    async def fake_execute_multimodal_request(**kwargs):
        return {"choices": [{"message": {"content": raw_response}}]}

    monkeypatch.setattr(client, "_execute_multimodal_request", fake_execute_multimodal_request)

    with caplog.at_level("INFO", logger="reports_ad.gemma_client"):
        await client.verify_composition_alignment(
            original_screenshot=screenshot,
            composed_screenshot_path=composed_path,
            current_banner_bbox=BoundingBox(20, 30, 100, 70),
            spec=spec,
            request_id="req-composition-logs",
        )

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "ocr_llm_request request_id=req-composition-logs stage=composition_validation" in log_text
    assert "CROPPED images around the current banner candidate" in log_text
    assert "ocr_llm_raw_response request_id=req-composition-logs stage=composition_validation" in log_text
    assert "ocr_llm_response request_id=req-composition-logs stage=composition_validation" in log_text
    assert raw_response in log_text
    assert "composition-full-response-marker" in log_text
    assert "disabled" not in log_text
    assert "data:image" not in log_text


@pytest.mark.asyncio
async def test_ocr_request_response_text_logs_can_be_bounded(tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    screenshot = SourceScreenshotInfo(path=source_path, mime_type="image/png", width=400, height=300, size_bytes=source_path.stat().st_size)
    raw_response = '{"bbox":{"x":0.1,"y":0.2,"width":0.3,"height":0.4},"confidence":0.91,"coordinate_space":"normalized","reason":"bounded-response-marker-with-long-tail"}'
    client = HeaderAuthGemmaClient(
        base_url="http://ocr.example.test/v1",
        model="test-model",
        authorization_header=None,
        max_retries=1,
        ocr_log_text=True,
        ocr_log_text_max_chars=80,
    )

    async def fake_execute_request(**kwargs):
        return {"choices": [{"message": {"content": raw_response}}]}

    monkeypatch.setattr(client, "_execute_request", fake_execute_request)

    with caplog.at_level("INFO", logger="reports_ad.gemma_client"):
        await client.detect_banner_bbox(screenshot=screenshot, spec=spec, request_id="req-bounded-logs")

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "prompt_text=Base task instructions: You are analyzing a placement screenshot for an advertis…[truncated:" in log_text
    assert "response_text={\"bbox\":{\"x\":0.1,\"y\":0.2,\"width\":0.3,\"height\":0.4},\"confidence\":0.91,\"coordinate…[truncated:" in log_text
    assert "bounded-response-marker-with-long-tail" not in log_text


@pytest.mark.asyncio
async def test_ocr_downscale_copy_is_used_and_resized_bbox_maps_to_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "large-source.png"
    Image.new("RGB", (2560, 1440), "white").save(source_path)
    screenshot = SourceScreenshotInfo(path=source_path, mime_type="image/png", width=2560, height=1440, size_bytes=source_path.stat().st_size)
    client = HeaderAuthGemmaClient(
        base_url="http://ocr.example.test/v1",
        model="test-model",
        authorization_header=None,
        max_retries=1,
        ocr_image_max_width=1280,
        ocr_image_max_pixels=2_000_000,
    )
    captured: dict[str, object] = {}

    async def fake_execute_request(**kwargs):
        image_path = kwargs["image_path"]
        captured["image_path"] = image_path
        captured["prompt"] = kwargs["prompt"]
        with Image.open(image_path) as sent:
            captured["sent_size"] = sent.size
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"bbox":{"x":100,"y":50,"width":300,"height":250},"confidence":0.9,"coordinate_space":"resized_pixels","image_width":1280,"image_height":720}'
                    }
                }
            ]
        }

    monkeypatch.setattr(client, "_execute_request", fake_execute_request)

    result = await client.detect_banner_bbox(screenshot=screenshot, spec=spec, request_id="req-downscale")

    assert captured["sent_size"] == (1280, 720)
    assert captured["image_path"] != source_path
    assert Path(captured["image_path"]).exists() is False
    assert "source screenshot is 2560x1440" in captured["prompt"]
    assert "OCR input image sent to you is 1280x720" in captured["prompt"]
    assert result.bbox == BoundingBox(200, 100, 600, 500)


@pytest.mark.asyncio
async def test_composition_validation_sends_crop_sized_images_instead_of_full_screenshots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    composed_path = tmp_path / "composed.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    Image.new("RGB", (400, 300), "white").save(composed_path)
    screenshot = SourceScreenshotInfo(path=source_path, mime_type="image/png", width=400, height=300, size_bytes=source_path.stat().st_size)
    client = HeaderAuthGemmaClient(
        base_url="http://ocr.example.test/v1",
        model="test-model",
        authorization_header=None,
        max_retries=1,
        composition_validation_crop_margin_percent=0.20,
        composition_validation_crop_margin_min_pixels=60,
    )
    captured: dict[str, object] = {}

    async def fake_execute_multimodal_request(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        sizes = []
        for image_path, _mime_type in kwargs["images"]:
            with Image.open(image_path) as sent:
                sizes.append(sent.size)
        captured["sent_sizes"] = sizes
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"verification_passed":true,"original_banner_bbox":{"x":20,"y":30,"width":100,"height":70},"actual_inserted_creative_bbox":{"x":20,"y":30,"width":100,"height":70},"deltas":{"x":0,"y":0,"width":0,"height":0},"recommended_corrected_bbox":null,"confidence":0.99,"reason":"exact","coordinate_space":"source_pixels"}'
                    }
                }
            ]
        }

    monkeypatch.setattr(client, "_execute_multimodal_request", fake_execute_multimodal_request)

    result = await client.verify_composition_alignment(
        original_screenshot=screenshot,
        composed_screenshot_path=composed_path,
        current_banner_bbox=BoundingBox(20, 30, 100, 70),
        spec=spec,
        request_id="req-validation-crop",
    )

    assert result.passed is True
    assert captured["sent_sizes"] == [(180, 160), (180, 160)]
    assert "CROPPED images around the current banner candidate" in captured["prompt"]
    assert "crop rectangle in full source pixels is x=0, y=0, width=180, height=160" in captured["prompt"]


def test_gemma_client_builder_rejects_missing_runtime_config() -> None:
    with pytest.raises(ValidationError, match="не инициализирована"):
        build_gemma_client_from_config(None)


def test_gemma_client_builder_requires_base_url() -> None:
    config = BotConfig(telegram_token="test", ocr_llm_base_url="", ocr_llm_model="cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit")

    with pytest.raises(ValidationError, match="OCR_LLM_BASE_URL"):
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
        def __init__(self) -> None:
            self.banner_calls = 0
            self.landing_calls = 0
            self.validation_calls = 0

        async def detect_banner_bbox(self, **kwargs):
            self.banner_calls += 1
            assert kwargs["selection_attempt"] == 1
            assert kwargs["validation_feedback"] is None
            return BBoxDetectionResult(bbox=BoundingBox(20, 30, 100, 70), confidence=0.9, source="mock")

        async def detect_landing_bbox(self, **kwargs):
            self.landing_calls += 1
            return BBoxDetectionResult(bbox=None, confidence=0.0, source="mock")

        async def verify_composition_alignment(self, **kwargs):
            self.validation_calls += 1
            return CompositionValidationResult(
                passed=True,
                original_banner_bbox=kwargs["current_banner_bbox"],
                inserted_creative_bbox=kwargs["current_banner_bbox"],
                corrected_bbox=kwargs["current_banner_bbox"],
                confidence=0.99,
                reason="exact match",
                source="mock",
            )

    client = MockGemma()
    result = await generate_stage2_proof(request, gemma_client=client)

    assert result.output_path.exists()
    assert result.banner_bbox == BoundingBox(20, 30, 100, 70)
    assert result.landing_fallback_used is True
    assert result.landing_bbox == fallback_landing_bbox(image_width=400, image_height=300)
    assert client.banner_calls == 1
    assert client.landing_calls == 1
    assert client.validation_calls == 1


@pytest.mark.asyncio
async def test_stage2_generate_corrects_composition_after_validation_offset(tmp_path: Path) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    Image.new("RGB", (300, 250), "blue").save(creative_path)
    request = ProofRequest(
        request_id="req-stage2-correct",
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
        def __init__(self) -> None:
            self.validation_bboxes: list[BoundingBox] = []

        async def detect_banner_bbox(self, **kwargs):
            return BBoxDetectionResult(bbox=BoundingBox(20, 30, 100, 70), confidence=0.9, source="mock")

        async def detect_landing_bbox(self, **kwargs):
            return BBoxDetectionResult(bbox=None, confidence=0.0, source="mock")

        async def verify_composition_alignment(self, **kwargs):
            bbox = kwargs["current_banner_bbox"]
            self.validation_bboxes.append(bbox)
            if len(self.validation_bboxes) == 1:
                return CompositionValidationResult(
                    passed=False,
                    original_banner_bbox=BoundingBox(40, 50, 120, 80),
                    inserted_creative_bbox=bbox,
                    corrected_bbox=BoundingBox(40, 50, 120, 80),
                    deltas={"x": -20, "y": -20, "width": -20, "height": -10},
                    confidence=0.95,
                    reason="offset and size mismatch",
                    source="mock",
                )
            return CompositionValidationResult(
                passed=True,
                original_banner_bbox=bbox,
                inserted_creative_bbox=bbox,
                corrected_bbox=bbox,
                confidence=0.98,
                reason="exact match after correction",
                source="mock",
            )

    client = MockGemma()
    result = await generate_stage2_proof(request, gemma_client=client)

    assert result.banner_bbox == BoundingBox(40, 50, 120, 80)
    assert client.validation_bboxes == [BoundingBox(20, 30, 100, 70), BoundingBox(40, 50, 120, 80)]
    with Image.open(result.intermediate_screenshot_path) as result_image:
        assert result_image.getpixel((45, 55)) != (255, 255, 255)
        assert result_image.getpixel((25, 35)) == (255, 255, 255)


@pytest.mark.asyncio
async def test_stage2_generate_fails_after_three_unsuccessful_composition_validations(tmp_path: Path) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    Image.new("RGB", (300, 250), "blue").save(creative_path)
    request = ProofRequest(
        request_id="req-stage2-uncorrected",
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
        def __init__(self) -> None:
            self.validation_calls = 0

        async def detect_banner_bbox(self, **kwargs):
            return BBoxDetectionResult(bbox=BoundingBox(20, 30, 100, 70), confidence=0.9, source="mock")

        async def detect_landing_bbox(self, **kwargs):
            return BBoxDetectionResult(bbox=None, confidence=0.0, source="mock")

        async def verify_composition_alignment(self, **kwargs):
            self.validation_calls += 1
            return CompositionValidationResult(
                passed=False,
                original_banner_bbox=BoundingBox(60, 50, 120, 80),
                inserted_creative_bbox=kwargs["current_banner_bbox"],
                corrected_bbox=BoundingBox(20 + self.validation_calls * 10, 30, 100, 70),
                deltas={"x": -10, "y": 0, "width": 0, "height": 0},
                confidence=0.9,
                reason=f"still offset {self.validation_calls}",
                source="mock",
            )

    client = MockGemma()
    with pytest.raises(ValidationError, match="точно совместить"):
        await generate_stage2_proof(request, gemma_client=client)

    assert client.validation_calls == 3
    diagnostics = list(Path("tmp").glob("stage2-req-stage2-uncorrected-*.composition-validation-failed.json"))
    if not diagnostics:
        diagnostics = list(Path("reports_ad/tmp").glob("stage2-req-stage2-uncorrected-*.composition-validation-failed.json"))
    assert diagnostics


@pytest.mark.asyncio
async def test_stage2_generate_handles_invalid_composition_validation_response(tmp_path: Path) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    Image.new("RGB", (300, 250), "blue").save(creative_path)
    request = ProofRequest(
        request_id="req-stage2-invalid-validation",
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

        async def verify_composition_alignment(self, **kwargs):
            raise GemmaClientError("invalid JSON")

    with pytest.raises(ValidationError, match="точно совместить"):
        await generate_stage2_proof(request, gemma_client=MockGemma())


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
        assert "после 5 попыток" in exc.safe_message
        assert "Последняя ошибка" in exc.safe_message
    else:
        raise AssertionError("Expected ValidationError")


@pytest.mark.asyncio
async def test_stage2_generate_retries_with_validation_feedback_then_uses_valid_bbox(tmp_path: Path) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    output_path = tmp_path / "final.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    Image.new("RGB", (300, 250), "blue").save(creative_path)
    original_source_bytes = source_path.read_bytes()
    request = ProofRequest(
        request_id="req-stage2-retry",
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

    class MockGemma:
        def __init__(self) -> None:
            self.banner_calls: list[dict[str, object]] = []

        async def detect_banner_bbox(self, **kwargs):
            self.banner_calls.append(kwargs)
            assert source_path.read_bytes() == original_source_bytes
            if len(self.banner_calls) == 1:
                assert output_path.exists() is False
                return BBoxDetectionResult(bbox=BoundingBox(390, 10, 50, 50), confidence=0.9, source="mock")
            return BBoxDetectionResult(bbox=BoundingBox(20, 30, 100, 70), confidence=0.9, source="mock")

        async def detect_landing_bbox(self, **kwargs):
            return BBoxDetectionResult(bbox=None, confidence=0.0, source="mock")

    client = MockGemma()
    result = await generate_stage2_proof(request, gemma_client=client)

    assert result.output_path.exists()
    assert result.banner_bbox == BoundingBox(20, 30, 100, 70)
    assert source_path.read_bytes() == original_source_bytes
    assert len(client.banner_calls) == 2
    assert client.banner_calls[0]["selection_attempt"] == 1
    assert client.banner_calls[0]["validation_feedback"] is None
    assert client.banner_calls[1]["selection_attempt"] == 2
    retry_feedback = str(client.banner_calls[1]["validation_feedback"])
    assert "out_of_bounds" in retry_feedback
    assert "previous_invalid_bbox=x=390,y=10,width=50,height=50" in retry_feedback
    assert "previous_invalid_center=x=415.0,y=35.0" in retry_feedback
    assert "previous_invalid_zone=top_right" in retry_feedback
    assert "do_not_repeat_previous_invalid_bbox=true" in retry_feedback
    with Image.open(output_path) as result_image:
        assert result_image.getpixel((45, 55)) != (255, 255, 255)
        assert result_image.getpixel((390, 10)) == (255, 255, 255)


@pytest.mark.asyncio
async def test_stage2_generate_exhausts_five_invalid_bbox_attempts_without_composition(tmp_path: Path) -> None:
    spec = get_spec("yandex-graphic-banners-300x250-image")
    assert spec is not None
    source_path = tmp_path / "source.png"
    creative_path = tmp_path / "creative.png"
    output_path = tmp_path / "final.png"
    Image.new("RGB", (400, 300), "white").save(source_path)
    Image.new("RGB", (300, 250), "blue").save(creative_path)
    original_source_bytes = source_path.read_bytes()
    request = ProofRequest(
        request_id="req-stage2-exhaust",
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

    class MockGemma:
        def __init__(self) -> None:
            self.banner_calls: list[dict[str, object]] = []
            self.landing_calls = 0

        async def detect_banner_bbox(self, **kwargs):
            self.banner_calls.append(kwargs)
            assert source_path.read_bytes() == original_source_bytes
            return BBoxDetectionResult(bbox=BoundingBox(390, 10, 50, 50), confidence=0.9, source="mock")

        async def detect_landing_bbox(self, **kwargs):
            self.landing_calls += 1
            return BBoxDetectionResult(bbox=None, confidence=0.0, source="mock")

    client = MockGemma()
    with pytest.raises(ValidationError) as exc_info:
        await generate_stage2_proof(request, gemma_client=client)

    assert "после 5 попыток" in exc_info.value.safe_message
    assert "Последняя ошибка" in exc_info.value.safe_message
    assert "out_of_bounds" in exc_info.value.safe_message
    assert len(client.banner_calls) == 5
    assert [call["selection_attempt"] for call in client.banner_calls] == [1, 2, 3, 4, 5]
    assert client.banner_calls[0]["validation_feedback"] is None
    assert all(call["validation_feedback"] for call in client.banner_calls[1:])
    assert client.landing_calls == 0
    assert output_path.exists() is False
    assert source_path.read_bytes() == original_source_bytes


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

    with pytest.raises(ValidationError, match="OCR_LLM_BASE_URL"):
        await generate_stage2_proof(request, config=config)
