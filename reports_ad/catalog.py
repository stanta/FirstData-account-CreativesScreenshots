from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from urllib.parse import urlparse

from .models import CreativeSpec, Placement, PlacementTemplate, SitePlacement

CATALOG_RESOURCE = "data/creative_catalog.json"


class CatalogError(RuntimeError):
    """Catalog loading error safe for startup/test diagnostics."""


@lru_cache(maxsize=1)
def load_catalog() -> dict:
    try:
        resource = files("reports_ad").joinpath(CATALOG_RESOURCE)
        return json.loads(resource.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive startup guard
        raise CatalogError("Не удалось загрузить каталог рекламных форматов.") from exc


def list_networks() -> list[dict[str, str]]:
    return list(load_catalog().get("networks", []))


def list_runtime_specs(network_slug: str | None = None) -> list[CreativeSpec]:
    specs = [_spec_from_raw(raw) for raw in load_catalog().get("specs", [])]
    supported = [spec for spec in specs if spec.runtime_upload_supported]
    if network_slug:
        supported = [spec for spec in supported if spec.network_slug == network_slug]
    return supported


def get_network(network_slug: str) -> dict[str, str] | None:
    for network in list_networks():
        if network.get("slug") == network_slug:
            return network
    return None


def get_spec(spec_slug: str) -> CreativeSpec | None:
    for raw in load_catalog().get("specs", []):
        if raw.get("slug") == spec_slug:
            return _spec_from_raw(raw)
    return None


def get_site_placement(host_or_url: str) -> SitePlacement | None:
    host = normalize_host(host_or_url)
    if not host:
        return None
    for raw in load_catalog().get("placements", {}).get("sites", []):
        candidate = normalize_host(raw.get("host", ""))
        if candidate and (host == candidate or host.endswith(f".{candidate}")):
            return SitePlacement(
                host=raw["host"],
                category=raw.get("category", ""),
                selector_confidence=raw.get("selector_confidence", "unknown"),
                observed_selectors=tuple(raw.get("observed_selectors", [])),
                fallback_templates=tuple(raw.get("fallback_templates", [])),
            )
    return None


def list_templates() -> dict[str, PlacementTemplate]:
    templates = {}
    for template_id, raw in load_catalog().get("placements", {}).get("templates", {}).items():
        templates[template_id] = PlacementTemplate(
            template_id=template_id,
            label=raw.get("label", template_id),
            selectors=tuple(raw.get("selectors", [])),
            preferred_dimensions=tuple(raw.get("preferred_dimensions", [])),
        )
    return templates


def selectors_for_template(template_id: str) -> tuple[str, ...]:
    template = list_templates().get(template_id)
    if not template:
        return ()
    return template.selectors


def choose_placement_for_url(ad_page_url: str, spec: CreativeSpec) -> Placement:
    site = get_site_placement(ad_page_url)
    templates = list_templates()
    dimension = f"{spec.width}x{spec.height}" if spec.width and spec.height else spec.format_slug

    if site:
        for selector in site.observed_selectors:
            if selector:
                return Placement(mode="selector", value=selector)
        for template_id in site.fallback_templates:
            template = templates.get(template_id)
            if template and (dimension in template.preferred_dimensions or not template.preferred_dimensions):
                return Placement(mode="template", value=template_id)
        for template_id in site.fallback_templates:
            if template_id in templates:
                return Placement(mode="template", value=template_id)

    for template_id, template in templates.items():
        if dimension in template.preferred_dimensions:
            return Placement(mode="template", value=template_id)
    return Placement(mode="template", value="in-content")


def catalog_summary(max_networks: int = 12) -> str:
    networks = list_networks()
    lines = []
    for index, network in enumerate(networks[:max_networks], start=1):
        count = len(list_runtime_specs(network.get("slug")))
        lines.append(f"{index}. {network.get('display_name')} — {network.get('slug')} ({count} PNG/JPEG specs)")
    if len(networks) > max_networks:
        lines.append(f"… еще {len(networks) - max_networks} сетей; отправьте slug из каталога.")
    return "\n".join(lines)


def specs_summary(network_slug: str, max_specs: int = 24) -> str:
    specs = list_runtime_specs(network_slug)
    lines = []
    for index, spec in enumerate(specs[:max_specs], start=1):
        lines.append(f"{index}. {spec.display_name} — {spec.slug}; {spec.dimensions_label}; до {spec.max_file_size_bytes} байт")
    if len(specs) > max_specs:
        lines.append(f"… еще {len(specs) - max_specs} форматов; отправьте slug из каталога.")
    return "\n".join(lines)


def normalize_host(host_or_url: str) -> str:
    value = (host_or_url or "").strip().lower()
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return (parsed.hostname or "").lower().removeprefix("www.")


def _spec_from_raw(raw: dict) -> CreativeSpec:
    return CreativeSpec(
        slug=raw["slug"],
        display_name=raw.get("display_name", raw["slug"]),
        network_slug=raw.get("network_slug") or raw.get("platform_slug", ""),
        network_display_name=raw.get("network_display_name") or raw.get("platform_slug", ""),
        platform_slug=raw.get("platform_slug", ""),
        format_slug=raw.get("format_slug", ""),
        creative_type_slug=raw.get("creative_type_slug", ""),
        width=raw.get("width"),
        height=raw.get("height"),
        allowed_extensions_source=tuple(raw.get("allowed_extensions_source", [])),
        allowed_mime_types_source=tuple(raw.get("allowed_mime_types_source", [])),
        runtime_allowed_extensions=tuple(raw.get("runtime_allowed_extensions", [])),
        runtime_allowed_mime_types=tuple(raw.get("runtime_allowed_mime_types", [])),
        max_file_size_bytes=int(raw.get("max_file_size_bytes") or 0),
        requires_ad_marking=bool(raw.get("requires_ad_marking", False)),
        runtime_upload_supported=bool(raw.get("runtime_upload_supported", False)),
        runtime_restriction_note=raw.get("runtime_restriction_note", ""),
    )
