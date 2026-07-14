from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


SUPPORTED_CREATIVE_MIME_TYPES = frozenset({"image/png", "image/jpeg"})
SUPPORTED_CREATIVE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg"})
SUPPORTED_PLACEMENT_TEMPLATES = frozenset({"top-banner", "sidebar-rect", "in-content", "mobile-banner", "video-landscape"})

PlacementMode = Literal["selector", "template"]
PlacementMediaType = Literal["banner", "video"]


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
class CreativeSpec:
    slug: str
    display_name: str
    network_slug: str
    network_display_name: str
    platform_slug: str
    format_slug: str
    creative_type_slug: str
    width: int | None
    height: int | None
    allowed_extensions_source: tuple[str, ...]
    allowed_mime_types_source: tuple[str, ...]
    runtime_allowed_extensions: tuple[str, ...]
    runtime_allowed_mime_types: tuple[str, ...]
    max_file_size_bytes: int
    requires_ad_marking: bool = False
    runtime_upload_supported: bool = False
    runtime_restriction_note: str = ""

    @property
    def dimensions_label(self) -> str:
        if self.width and self.height:
            return f"{self.width}×{self.height}"
        return self.format_slug

    @property
    def label(self) -> str:
        return f"{self.display_name} ({self.dimensions_label})"


@dataclass(frozen=True, slots=True)
class PlacementTemplate:
    template_id: str
    label: str
    selectors: tuple[str, ...]
    preferred_dimensions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SitePlacement:
    host: str
    category: str
    selector_confidence: str
    observed_selectors: tuple[str, ...]
    fallback_templates: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlacementSite:
    host: str
    enabled: bool = True
    disabled_reason: str = ""


@dataclass(frozen=True, slots=True)
class ProofRequest:
    request_id: str
    ad_page_url: str
    network_slug: str
    network_display_name: str
    spec: CreativeSpec
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
    page_load_timeout_ms: int = 12_000
    click_timeout_ms: int = 7_000
    screenshot_timeout_ms: int = 15_000
    timeout_diagnostics: bool = True
    timeout_diagnostics_timeout_ms: int = 5_000
    locale: str = "ru-RU"
    timezone_id: str = "Europe/Moscow"


@dataclass(frozen=True, slots=True)
class ProofResult:
    output_path: Path
    width: int
    height: int
    effective_click_url: str
    request_id: str
    intermediate_screenshot_path: Path | None = None
    landing_fallback_used: bool = False
    ad_page_fallback_used: bool = False
    fallback_media_type: str = ""
    resolved_ad_page_host: str = ""
    timings_ms: dict[str, int] = field(default_factory=dict)
