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
    flow_kind: str = "masked_banner"


@dataclass(frozen=True, slots=True)
class BannerSize:
    slug: str
    width: int
    height: int
    display_label: str | None = None

    @property
    def label(self) -> str:
        if self.display_label:
            return self.display_label
        return f"{self.width}×{self.height}"


@dataclass(frozen=True, slots=True)
class MaskAsset:
    network: MaskNetwork
    size: BannerSize
    path: Path


MASK_NETWORKS: tuple[MaskNetwork, ...] = (
    MaskNetwork(slug="mediasniper", display_name="Mediasniper", directory_name="Mediasniper"),
    MaskNetwork(slug="hybrid", display_name="Hybrid", directory_name="Hybrid"),
    MaskNetwork(slug="yandex_video", display_name="Яндекс Видео", directory_name="YandexVideo", flow_kind="yandex_video"),
    MaskNetwork(slug="mediasniper_video", display_name="Mediasniper Video", directory_name="MediasniperVideo", flow_kind="yandex_video"),
)

STANDARD_BANNER_SIZES: tuple[BannerSize, ...] = (
    BannerSize(slug="240x400", width=240, height=400),
    BannerSize(slug="300x600", width=300, height=600),
    BannerSize(slug="300x500", width=300, height=500),
)
YANDEX_VIDEO_BANNER_SIZES: tuple[BannerSize, ...] = (
    BannerSize(slug="1920x1080", width=1536, height=1024, display_label="1920×1080"),
)
MEDIASNIPER_VIDEO_BANNER_SIZES: tuple[BannerSize, ...] = (
    BannerSize(slug="1920x1080", width=1920, height=1080, display_label="1920×1080"),
)
SUPPORTED_BANNER_SIZES: tuple[BannerSize, ...] = STANDARD_BANNER_SIZES + YANDEX_VIDEO_BANNER_SIZES + MEDIASNIPER_VIDEO_BANNER_SIZES
NETWORK_BANNER_SIZES: dict[str, tuple[BannerSize, ...]] = {
    "mediasniper": STANDARD_BANNER_SIZES,
    "hybrid": STANDARD_BANNER_SIZES,
    "yandex_video": YANDEX_VIDEO_BANNER_SIZES,
    "mediasniper_video": MEDIASNIPER_VIDEO_BANNER_SIZES,
}


def list_mask_networks() -> list[MaskNetwork]:
    return list(MASK_NETWORKS)


def list_banner_sizes() -> list[BannerSize]:
    return list(SUPPORTED_BANNER_SIZES)


def list_banner_sizes_for_network(network_slug: str) -> list[BannerSize]:
    network = get_mask_network(network_slug)
    if network is None:
        return []
    return list(NETWORK_BANNER_SIZES.get(network.slug, ()))


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


def get_banner_size_for_network(network_slug: str, size_slug: str) -> BannerSize | None:
    normalized_size = normalize_banner_size_slug(size_slug)
    for size in list_banner_sizes_for_network(network_slug):
        if size.slug == normalized_size:
            return size
    return None


def normalize_mask_network_slug(value: str | None) -> str:
    normalized = (value or "").strip().lower().replace(" ", "_")
    aliases = {
        "media_sniper": "mediasniper",
        "media-sniper": "mediasniper",
        "mediasniper": "mediasniper",
        "media_sniper_video": "mediasniper_video",
        "media-sniper-video": "mediasniper_video",
        "mediasniper_video": "mediasniper_video",
        "mediasniper-video": "mediasniper_video",
        "mediasnipervideo": "mediasniper_video",
        "медиаснайпер_видео": "mediasniper_video",
        "медиаснайпер-видео": "mediasniper_video",
        "медиаснайпер видео": "mediasniper_video",
        "hybryd": "hybrid",
        "hybrid": "hybrid",
        "yandex_video": "yandex_video",
        "yandex-video": "yandex_video",
        "yandexvideo": "yandex_video",
        "яндекс_видео": "yandex_video",
        "яндекс-видео": "yandex_video",
        "яндекс видео": "yandex_video",
    }
    return aliases.get(normalized, normalized)


def normalize_banner_size_slug(value: str | None) -> str:
    return (value or "").strip().lower().replace("×", "x").replace("*", "x").replace(" ", "")


def validate_mask_network_slug(raw_value: str, allowed_slugs: set[str] | None = None) -> str:
    value = normalize_mask_network_slug(raw_value)
    allowed = allowed_slugs or {network.slug for network in MASK_NETWORKS}
    if value not in allowed:
        raise ValidationError("Неизвестная рекламная сеть. Выберите Mediasniper, Hybrid, Яндекс Видео или Mediasniper Video из меню.")
    return value


def validate_banner_size_slug(raw_value: str, allowed_slugs: set[str] | None = None) -> str:
    value = normalize_banner_size_slug(raw_value)
    allowed = allowed_slugs or {size.slug for size in SUPPORTED_BANNER_SIZES}
    if value not in allowed:
        raise ValidationError("Неизвестный размер баннера. Выберите размер из меню.")
    return value


def validate_banner_size_slug_for_network(raw_value: str, network_slug: str) -> str:
    value = normalize_banner_size_slug(raw_value)
    allowed = {size.slug for size in list_banner_sizes_for_network(network_slug)}
    if value not in allowed:
        supported = ", ".join(size.label for size in list_banner_sizes_for_network(network_slug)) or "нет доступных размеров"
        raise ValidationError(f"Неизвестный размер баннера для выбранной сети. Поддерживаются: {supported}.")
    return value


def resolve_mask_asset(network_slug: str, size_slug: str) -> MaskAsset:
    network = get_mask_network(network_slug)
    if network is None:
        raise ValidationError("Неизвестная рекламная сеть. Выберите Mediasniper, Hybrid, Яндекс Видео или Mediasniper Video из меню.")
    size = get_banner_size_for_network(network.slug, size_slug)
    if size is None:
        raise ValidationError("Неизвестный размер баннера для выбранной сети. Выберите размер из меню.")

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
