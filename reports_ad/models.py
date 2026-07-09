from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


SUPPORTED_CREATIVE_MIME_TYPES = frozenset({"image/png", "image/jpeg"})
SUPPORTED_CREATIVE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg"})
SUPPORTED_PLACEMENT_TEMPLATES = frozenset({"top-banner", "sidebar-rect", "in-content"})

PlacementMode = Literal["selector", "template"]


@dataclass(frozen=True, slots=True)
class Placement:
    mode: PlacementMode
    value: str

    @property
    def label(self) -> str:
        return f"{self.mode}:{self.value}"


@dataclass(frozen=True, slots=True)
class CreativeInfo:
    path: Path
    mime_type: str
    width: int
    height: int
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ProofRequest:
    request_id: str
    ad_page_url: str
    banner_type: str
    creative: CreativeInfo
    landing_url: str
    utm_params: dict[str, str]
    placement: Placement
    output_path: Path
    viewport_width: int = 1365
    viewport_height: int = 768
    headless: bool = True
    allow_local_urls: bool = False
    page_load_timeout_ms: int = 30_000
    click_timeout_ms: int = 7_000
    screenshot_timeout_ms: int = 15_000
    locale: str = "ru-RU"
    timezone_id: str = "Europe/Moscow"


@dataclass(frozen=True, slots=True)
class ProofResult:
    output_path: Path
    width: int
    height: int
    effective_click_url: str
    request_id: str
    landing_fallback_used: bool = False
    timings_ms: dict[str, int] = field(default_factory=dict)

