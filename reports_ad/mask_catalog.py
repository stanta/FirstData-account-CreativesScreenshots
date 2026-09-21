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
    menu_label: str | None = None
    mask_file_name: str | None = None
    # Expected decoded size of the source mask PNG. When set, it is independent
    # from width/height (the output canvas size) so strict mask validation stays
    # meaningful even when the mask asset and the canvas differ.
    mask_width: int | None = None
    mask_height: int | None = None

    @property
    def label(self) -> str:
        if self.display_label:
            return self.display_label
        return f"{self.width}×{self.height}"

    @property
    def expected_mask_size(self) -> tuple[int, int]:
        return (
            self.mask_width if self.mask_width is not None else self.width,
            self.mask_height if self.mask_height is not None else self.height,
        )

    @property
    def menu_text(self) -> str:
        return self.menu_label or self.label


@dataclass(frozen=True, slots=True)
class MaskAsset:
    network: MaskNetwork
    size: BannerSize
    path: Path


MASK_NETWORKS: tuple[MaskNetwork, ...] = (
    MaskNetwork(slug="mediasniper", display_name="Mediasniper", directory_name="Mediasniper"),
    MaskNetwork(slug="hybrid", display_name="Hybrid", directory_name="Hybrid"),
    MaskNetwork(slug="yandex_video", display_name="Яндекс Видео", directory_name="YandexVideo", flow_kind="yandex_video"),
    # MediaSniperVideo decouples from the Yandex video flow: mask-only composition,
    # no target-site prompt, no favicon, no extra text rendering.
    MaskNetwork(slug="mediasniper_video", display_name="Mediasniper Video", directory_name="MediasniperVideo", flow_kind="mediasniper_video"),
    MaskNetwork(slug="getintent_video", display_name="GetIntent Video", directory_name="GetIntentVideo", flow_kind="getintent_video"),
)

STANDARD_BANNER_SIZES: tuple[BannerSize, ...] = (
    BannerSize(slug="240x400", width=240, height=400),
    BannerSize(slug="300x600", width=300, height=600),
    BannerSize(slug="300x500", width=300, height=500),
)
YANDEX_VIDEO_BANNER_SIZES: tuple[BannerSize, ...] = (
    BannerSize(slug="1920x1080", width=1536, height=1024, display_label="1920×1080"),
)
# MediaSniperVideo outputs a fixed 2941x1080 canvas. The uploaded creative is
# proportionally scaled to the 1080 px canvas height and centered horizontally
# (a 16:9 creative becomes 1920x1080 and lands at x=510, y=0 with 510/511 px side
# margins). mask_width/mask_height pin the expected decoded source-mask size so
# validation stays strict while mask and canvas sizes are decoupled:
# 1920_1080_all.png decodes at 2941x1080 and overlays 1:1;
# 1920_1080_br_only.png decodes at 1920x1091 and is horizontally centered and
# bottom-aligned at native size (dest x=510, y=-11), clipping only the 11 px
# that overflow above the canvas.
MEDIASNIPER_VIDEO_BANNER_SIZES: tuple[BannerSize, ...] = (
    BannerSize(
        slug="1920_1080_all",
        width=2941,
        height=1080,
        display_label="1920×1080",
        menu_label="1920_1080 наложить все элементы",
        mask_file_name="1920_1080_all.png",
        mask_width=2941,
        mask_height=1080,
    ),
    BannerSize(
        slug="1920_1080_br",
        width=2941,
        height=1080,
        display_label="1920×1080",
        menu_label="1920_1080 наложить только линию видеоплеера внизу",
        mask_file_name="1920_1080_br_only.png",
        mask_width=1920,
        mask_height=1091,
    ),
)
GETINTENT_VIDEO_BANNER_SIZES: tuple[BannerSize, ...] = (
    BannerSize(slug="1920x1080", width=1920, height=1215, display_label="1920×1080"),
)
SUPPORTED_BANNER_SIZES: tuple[BannerSize, ...] = STANDARD_BANNER_SIZES + YANDEX_VIDEO_BANNER_SIZES + MEDIASNIPER_VIDEO_BANNER_SIZES + GETINTENT_VIDEO_BANNER_SIZES
NETWORK_BANNER_SIZES: dict[str, tuple[BannerSize, ...]] = {
    "mediasniper": STANDARD_BANNER_SIZES,
    "hybrid": STANDARD_BANNER_SIZES,
    "yandex_video": YANDEX_VIDEO_BANNER_SIZES,
    "mediasniper_video": MEDIASNIPER_VIDEO_BANNER_SIZES,
    "getintent_video": GETINTENT_VIDEO_BANNER_SIZES,
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
        "getintent_video": "getintent_video",
        "getintent-video": "getintent_video",
        "getintentvideo": "getintent_video",
        "гетинтент_видео": "getintent_video",
        "гетинтент-видео": "getintent_video",
        "гетинтент видео": "getintent_video",
        "гетинтентвидео": "getintent_video",
    }
    return aliases.get(normalized, normalized)


def normalize_banner_size_slug(value: str | None) -> str:
    normalized = (value or "").strip().lower().replace("×", "x").replace("*", "x").replace(" ", "")
    aliases = {
        "1920x1080_all": "1920_1080_all",
        "1920x1080_br_only": "1920_1080_br",
        "1920_1080_br_only": "1920_1080_br",
    }
    return aliases.get(normalized, normalized)


def validate_mask_network_slug(raw_value: str, allowed_slugs: set[str] | None = None) -> str:
    value = normalize_mask_network_slug(raw_value)
    allowed = allowed_slugs or {network.slug for network in MASK_NETWORKS}
    if value not in allowed:
        raise ValidationError("Неизвестная рекламная сеть. Выберите рекламную сеть из меню.")
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
        raise ValidationError("Неизвестная рекламная сеть. Выберите рекламную сеть из меню.")
    size = get_banner_size_for_network(network.slug, size_slug)
    if size is None:
        raise ValidationError("Неизвестный размер баннера для выбранной сети. Выберите размер из меню.")

    mask_path = _mask_path(network=network, size=size)
    if not mask_path.exists() or not mask_path.is_file():
        raise ValidationError("Маска для выбранной сети и размера не найдена. Выберите другой размер или сообщите оператору.")
    return MaskAsset(network=network, size=size, path=mask_path)


@lru_cache(maxsize=32)
def _mask_path(network: MaskNetwork, size: BannerSize) -> Path:
    mask_file_name = size.mask_file_name or f"{size.slug}.png"
    resource = files("reports_ad").joinpath(MASKS_RESOURCE_ROOT, network.directory_name, mask_file_name)
    try:
        if resource.is_file():
            return Path(str(resource))
    except TypeError:
        pass
    return Path(__file__).resolve().parent.joinpath(MASKS_RESOURCE_ROOT, network.directory_name, mask_file_name)
