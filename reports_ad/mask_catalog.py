from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

from .validation import ValidationError

MASKS_RESOURCE_ROOT = "data/masks"


@dataclass(frozen=True, slots=True)
class MaskNetwork:
    slug: str
    display_name: str
    directory_name: str


@dataclass(frozen=True, slots=True)
class BannerSize:
    slug: str
    width: int
    height: int

    @property
    def label(self) -> str:
        return f"{self.width}×{self.height}"


@dataclass(frozen=True, slots=True)
class MaskAsset:
    network: MaskNetwork
    size: BannerSize
    path: Path


MASK_NETWORKS: tuple[MaskNetwork, ...] = (
    MaskNetwork(slug="mediasniper", display_name="Mediasniper", directory_name="Mediasniper"),
    MaskNetwork(slug="hybrid", display_name="Hybrid", directory_name="Hybrid"),
)

SUPPORTED_BANNER_SIZES: tuple[BannerSize, ...] = (
    BannerSize(slug="240x400", width=240, height=400),
    BannerSize(slug="300x600", width=300, height=600),
    BannerSize(slug="300x500", width=300, height=500),
)


def list_mask_networks() -> list[MaskNetwork]:
    return list(MASK_NETWORKS)


def list_banner_sizes() -> list[BannerSize]:
    return list(SUPPORTED_BANNER_SIZES)


def get_mask_network(slug: str) -> MaskNetwork | None:
    normalized = normalize_mask_network_slug(slug)
    for network in MASK_NETWORKS:
        if network.slug == normalized:
            return network
    return None


def get_banner_size(slug: str) -> BannerSize | None:
    normalized = normalize_banner_size_slug(slug)
    for size in SUPPORTED_BANNER_SIZES:
        if size.slug == normalized:
            return size
    return None


def normalize_mask_network_slug(value: str | None) -> str:
    normalized = (value or "").strip().lower().replace(" ", "_")
    aliases = {
        "media_sniper": "mediasniper",
        "media-sniper": "mediasniper",
        "mediasniper": "mediasniper",
        "hybryd": "hybrid",
        "hybrid": "hybrid",
    }
    return aliases.get(normalized, normalized)


def normalize_banner_size_slug(value: str | None) -> str:
    return (value or "").strip().lower().replace("×", "x").replace("*", "x").replace(" ", "")


def validate_mask_network_slug(raw_value: str, allowed_slugs: set[str] | None = None) -> str:
    value = normalize_mask_network_slug(raw_value)
    allowed = allowed_slugs or {network.slug for network in MASK_NETWORKS}
    if value not in allowed:
        raise ValidationError("Неизвестная рекламная сеть. Выберите Mediasniper или Hybrid из меню.")
    return value


def validate_banner_size_slug(raw_value: str, allowed_slugs: set[str] | None = None) -> str:
    value = normalize_banner_size_slug(raw_value)
    allowed = allowed_slugs or {size.slug for size in SUPPORTED_BANNER_SIZES}
    if value not in allowed:
        raise ValidationError("Неизвестный размер баннера. Выберите 240×400, 300×600 или 300×500 из меню.")
    return value


def resolve_mask_asset(network_slug: str, size_slug: str) -> MaskAsset:
    network = get_mask_network(network_slug)
    if network is None:
        raise ValidationError("Неизвестная рекламная сеть. Выберите Mediasniper или Hybrid из меню.")
    size = get_banner_size(size_slug)
    if size is None:
        raise ValidationError("Неизвестный размер баннера. Выберите 240×400, 300×600 или 300×500 из меню.")

    mask_path = _mask_path(network=network, size=size)
    if not mask_path.exists() or not mask_path.is_file():
        raise ValidationError("Маска для выбранной сети и размера не найдена. Выберите другой размер или сообщите оператору.")
    return MaskAsset(network=network, size=size, path=mask_path)


@lru_cache(maxsize=32)
def _mask_path(network: MaskNetwork, size: BannerSize) -> Path:
    resource = files("reports_ad").joinpath(MASKS_RESOURCE_ROOT, network.directory_name, f"{size.slug}.png")
    try:
        if resource.is_file():
            return Path(str(resource))
    except TypeError:
        pass
    return Path(__file__).resolve().parent.joinpath(MASKS_RESOURCE_ROOT, network.directory_name, f"{size.slug}.png")
