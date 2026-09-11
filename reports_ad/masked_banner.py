from __future__ import annotations

import io
import logging
import socket
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

import cairosvg
from PIL import Image, ImageChops, ImageDraw, ImageFont, UnidentifiedImageError

from .mask_catalog import BannerSize, MaskAsset, resolve_mask_asset
from .models import CreativeInfo
from .proof import ScreenshotGenerationError
from .validation import ValidationError, validate_minimum_age

logger = logging.getLogger(__name__)
OPAQUE_MASK_BACKGROUND_ALPHA = 80
VIDEO_FLOW_NETWORK_SLUGS = frozenset({"yandex_video", "mediasniper_video"})
VIDEO_FLOW_SIZE_SLUG = "1920x1080"
VIDEO_FLOW_BASE_SIZE = (1536, 1024)
YANDEX_VIDEO_NETWORK_SLUG = "yandex_video"
MEDIASNIPER_VIDEO_NETWORK_SLUG = "mediasniper_video"
GETINTENT_VIDEO_NETWORK_SLUG = "getintent_video"
YANDEX_VIDEO_SIZE_SLUG = VIDEO_FLOW_SIZE_SLUG
YANDEX_VIDEO_MASK_SIZE = VIDEO_FLOW_BASE_SIZE
YANDEX_VIDEO_FAVICON_BBOX = (56, 799, 32, 32)
YANDEX_VIDEO_TARGET_SITE_BBOX = (100, 795, 250, 40)
YANDEX_VIDEO_TARGET_SITE_FONT_SIZE = 45
YANDEX_VIDEO_TARGET_SITE_FILL = (255, 255, 255, 255)
YANDEX_VIDEO_FAVICON_TIMEOUT_SECONDS = 1.5
YANDEX_VIDEO_FAVICON_MAX_BYTES = 512 * 1024
YANDEX_VIDEO_FAVICON_HTML_MAX_BYTES = 256 * 1024
YANDEX_VIDEO_FAVICON_SVG_RENDER_SIZE = 96
YANDEX_VIDEO_FAVICON_USER_AGENT = "reports-ad-bot/1.0"
YANDEX_VIDEO_FAVICON_PATHS = ("/favicon.ico", "/favicon.png", "/apple-touch-icon.png")
MEDIASNIPER_VIDEO_SIZE = (1920, 1080)
MEDIASNIPER_VIDEO_FAVICON_BBOX = (90, 765, 128, 128)
MEDIASNIPER_VIDEO_TARGET_SITE_BBOX = (270, 765, 465, 75)
MEDIASNIPER_VIDEO_CTA_BBOX = (780, 780, 370, 70)
MEDIASNIPER_VIDEO_SKIP_BBOX = (1355, 800, 460, 60)
MEDIASNIPER_VIDEO_AD_LABEL_BBOX = (90, 945, 270, 65)
MEDIASNIPER_VIDEO_TEXT_FILL = (255, 255, 255, 255)
GETINTENT_VIDEO_SIZE_SLUG = "1920x1080"
GETINTENT_VIDEO_MASK_SIZE = (1920, 1215)
GETINTENT_VIDEO_MINIMUM_AGE_BBOX = (1670, 130, 224, 60)
GETINTENT_VIDEO_MINIMUM_AGE_FONT_SIZE = 90
GETINTENT_VIDEO_TEXT_FILL = (255, 255, 255, 255)
YANDEX_VIDEO_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/msttcorefonts/arialbd.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/arial.ttf",
    "/usr/share/fonts/truetype/helvetica/Helvetica-Bold.ttf",
    "/usr/share/fonts/truetype/helvetica/Helvetica.ttf",
    "/usr/share/fonts/opentype/urw-base35/NimbusSans-Bold.otf",
    "/usr/share/fonts/opentype/urw-base35/NimbusSans-Regular.otf",
    "/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/arialbd.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    "/usr/local/share/fonts/DejaVuSans.ttf",
    "/usr/local/lib/python3.14/site-packages/PIL/fonts/DejaVuSans.ttf",
    "/usr/local/lib/python3.13/site-packages/PIL/fonts/DejaVuSans.ttf",
    "/usr/local/lib/python3.12/site-packages/PIL/fonts/DejaVuSans.ttf",
    "/usr/local/lib/python3.11/site-packages/PIL/fonts/DejaVuSans.ttf",
)


@dataclass(frozen=True, slots=True)
class MaskedBannerRequest:
    request_id: str
    network_slug: str
    network_display_name: str
    size_slug: str
    size: BannerSize
    creative: CreativeInfo
    output_path: Path
    target_site_url: str | None = None
    target_site_display: str | None = None
    favicon_path: Path | None = None
    minimum_age: str | None = None


@dataclass(frozen=True, slots=True)
class MaskedBannerResult:
    output_path: Path
    width: int
    height: int
    request_id: str
    mask_asset: MaskAsset


async def generate_masked_banner(request: MaskedBannerRequest) -> MaskedBannerResult:
    """Resize a user creative to the selected banner size and overlay the matching PNG mask."""

    started = time.monotonic()
    _validate_masked_banner_request(request)
    mask_asset = resolve_mask_asset(request.network_slug, request.size_slug)
    output_path = Path(request.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info(
        "masked_banner_generation_started request_id=%s network=%s size=%s creative=%sx%s mask=%s",
        request.request_id,
        request.network_slug,
        request.size_slug,
        request.creative.width,
        request.creative.height,
        mask_asset.path.name,
    )

    try:
        width, height = compose_masked_banner(
            creative_path=request.creative.path,
            mask_path=mask_asset.path,
            output_path=output_path,
            width=request.size.width,
            height=request.size.height,
            network_slug=request.network_slug,
            target_site_url=request.target_site_url,
            target_site_display=request.target_site_display,
            favicon_path=request.favicon_path,
            minimum_age=request.minimum_age,
        )
    except ValidationError:
        raise
    except Exception as exc:
        logger.exception("masked_banner_generation_failed request_id=%s exception_class=%s", request.request_id, type(exc).__name__)
        raise ScreenshotGenerationError("Не удалось наложить маску на креатив.") from exc

    logger.info(
        "masked_banner_generation_done request_id=%s network=%s size=%s elapsed_ms=%s output=%sx%s",
        request.request_id,
        request.network_slug,
        request.size_slug,
        int((time.monotonic() - started) * 1000),
        width,
        height,
    )
    return MaskedBannerResult(output_path=output_path, width=width, height=height, request_id=request.request_id, mask_asset=mask_asset)


def compose_masked_banner(
    *,
    creative_path: Path,
    mask_path: Path,
    output_path: Path,
    width: int,
    height: int,
    network_slug: str | None = None,
    target_site_url: str | None = None,
    target_site_display: str | None = None,
    favicon_path: Path | None = None,
    minimum_age: str | None = None,
) -> tuple[int, int]:
    if width <= 0 or height <= 0:
        raise ValidationError("Размер баннера должен быть положительным.")
    try:
        with Image.open(creative_path) as creative_raw, Image.open(mask_path) as mask_raw:
            creative = creative_raw.convert("RGBA").resize((width, height), Image.Resampling.LANCZOS)
            if _is_video_flow(network_slug):
                if not target_site_display:
                    raise ValidationError("Для видео-flow нужно указать адрес целевого сайта.")
                raw_mask = mask_raw.convert("RGBA")
                creative = _clear_yandex_video_mask_region(creative, raw_mask)
            if network_slug == GETINTENT_VIDEO_NETWORK_SLUG:
                if not target_site_url or not target_site_display:
                    raise ValidationError("Для GetIntent Video нужно указать адрес целевого сайта.")
                minimum_age = validate_minimum_age(minimum_age or "")
                if (width, height) != GETINTENT_VIDEO_MASK_SIZE:
                    raise ValidationError("Маска GetIntent Video должна использовать исходный размер 1920×1215.")
            mask = _prepare_mask_overlay(mask_raw.convert("RGBA"))
            if mask.size != (width, height):
                raise ValidationError("Размер файла маски не совпадает с выбранным размером баннера.")
            pre_composite = creative.copy()
            result = Image.alpha_composite(creative, mask)
            if network_slug == YANDEX_VIDEO_NETWORK_SLUG:
                result = render_yandex_video_target_site(result, target_site_display or "", target_site_url=target_site_url, favicon_path=favicon_path)
            elif network_slug == MEDIASNIPER_VIDEO_NETWORK_SLUG:
                result = render_mediasniper_video_overlay(result, target_site_display or "", target_site_url=target_site_url, favicon_path=favicon_path)
            elif network_slug == GETINTENT_VIDEO_NETWORK_SLUG:
                # TODO: Render target_site_display when approved lower-block coordinates are supplied.
                result = render_getintent_video_minimum_age(result, minimum_age or "")
            _log_composition_diagnostics(
                network_slug=network_slug,
                size=(width, height),
                mask=mask,
                pre_composite=pre_composite,
                result=result,
            )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            result.save(output_path, format="PNG", optimize=True)
    except (UnidentifiedImageError, OSError) as exc:
        raise ValidationError("Креатив или маска не являются корректными PNG/JPEG-изображениями.") from exc

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise ScreenshotGenerationError("Итоговый баннер не был создан.")
    with Image.open(output_path) as check_img:
        if check_img.size != (width, height):
            raise ScreenshotGenerationError("Итоговый баннер имеет неправильный размер.")
    return width, height


def _log_composition_diagnostics(*, network_slug: str | None, size: tuple[int, int], mask: Image.Image, pre_composite: Image.Image, result: Image.Image) -> None:
    alpha = mask.getchannel("A")
    changed_bbox = ImageChops.difference(pre_composite.convert("RGB"), result.convert("RGB")).getbbox()
    target_region_changed = None
    if _is_video_flow(network_slug):
        x, y, width, height = _scale_yandex_video_bbox(size)
        before_crop = pre_composite.crop((x, y, x + width, y + height)).convert("RGB")
        after_crop = result.crop((x, y, x + width, y + height)).convert("RGB")
        target_region_changed = ImageChops.difference(before_crop, after_crop).getbbox() is not None
    logger.info(
        "masked_banner_composition_diagnostics network=%s output_size=%sx%s mask_size=%sx%s mask_alpha_extrema=%s mask_alpha_bbox=%s changed_bbox=%s target_region_changed=%s",
        network_slug or "unknown",
        size[0],
        size[1],
        mask.width,
        mask.height,
        alpha.getextrema(),
        alpha.getbbox(),
        changed_bbox,
        target_region_changed,
    )


def render_yandex_video_target_site(base: Image.Image, display_text: str, *, target_site_url: str | None = None, favicon_path: Path | None = None) -> Image.Image:
    text = (display_text or "").strip()
    if not text:
        raise ValidationError("Адрес целевого сайта не должен быть пустым.")
    result = base.copy().convert("RGBA")
    width, height = result.size
    favicon_box = _scale_yandex_video_bbox((width, height), YANDEX_VIDEO_FAVICON_BBOX)
    text_box = _scale_yandex_video_bbox((width, height), YANDEX_VIDEO_TARGET_SITE_BBOX)
    favicon_present = _paste_target_site_favicon(result, target_site_url=target_site_url, favicon_path=favicon_path, box=favicon_box)
    draw = ImageDraw.Draw(result)
    font = _fit_text_font(text=text, box_width=text_box[2], box_height=text_box[3], preferred_size=YANDEX_VIDEO_TARGET_SITE_FONT_SIZE)
    text_bbox = draw.textbbox((0, 0), text, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]
    text_x = text_box[0]
    text_y = text_box[1] + max(0, (text_box[3] - text_height) // 2) - text_bbox[1]
    draw.text((text_x, text_y), text, fill=YANDEX_VIDEO_TARGET_SITE_FILL, font=font)
    logger.info(
        "yandex_video_target_site_rendered text_length=%s text_box=%s favicon_box=%s favicon_present=%s font_class=%s font_size=%s fill=%s rendered_size=%sx%s",
        len(text),
        text_box,
        favicon_box,
        favicon_present,
        type(font).__name__,
        getattr(font, "size", "unknown"),
        YANDEX_VIDEO_TARGET_SITE_FILL,
        text_width,
        text_height,
    )
    if text_width > text_box[2]:
        logger.debug("yandex_video_target_site_text_overflow text_length=%s box_width=%s rendered_width=%s", len(text), text_box[2], text_width)
    return result


def render_mediasniper_video_overlay(base: Image.Image, display_text: str, *, target_site_url: str | None = None, favicon_path: Path | None = None) -> Image.Image:
    text = (display_text or "").strip()
    if not text:
        raise ValidationError("Адрес целевого сайта не должен быть пустым.")
    result = base.copy().convert("RGBA")
    if result.size != MEDIASNIPER_VIDEO_SIZE:
        logger.warning("mediasniper_video_overlay_unexpected_size actual=%sx%s expected=%sx%s", result.width, result.height, *MEDIASNIPER_VIDEO_SIZE)

    favicon_present = _paste_target_site_favicon(result, target_site_url=target_site_url, favicon_path=favicon_path, box=MEDIASNIPER_VIDEO_FAVICON_BBOX)
    draw = ImageDraw.Draw(result)
    site_font = _draw_fitted_text(draw, text=text, box=MEDIASNIPER_VIDEO_TARGET_SITE_BBOX, preferred_size=70, fill=MEDIASNIPER_VIDEO_TEXT_FILL)
    cta_font = _draw_fitted_text(draw, text="Перейти", box=MEDIASNIPER_VIDEO_CTA_BBOX, preferred_size=58, fill=MEDIASNIPER_VIDEO_TEXT_FILL)
    skip_font = _draw_fitted_text(draw, text="Пропустить 02", box=MEDIASNIPER_VIDEO_SKIP_BBOX, preferred_size=50, fill=MEDIASNIPER_VIDEO_TEXT_FILL)
    ad_font = _draw_fitted_text(draw, text="Реклама", box=MEDIASNIPER_VIDEO_AD_LABEL_BBOX, preferred_size=58, fill=MEDIASNIPER_VIDEO_TEXT_FILL)
    logger.info(
        "mediasniper_video_overlay_rendered text_length=%s favicon_box=%s favicon_present=%s target_box=%s cta_box=%s skip_box=%s ad_box=%s site_font_size=%s cta_font_size=%s skip_font_size=%s ad_font_size=%s",
        len(text),
        MEDIASNIPER_VIDEO_FAVICON_BBOX,
        favicon_present,
        MEDIASNIPER_VIDEO_TARGET_SITE_BBOX,
        MEDIASNIPER_VIDEO_CTA_BBOX,
        MEDIASNIPER_VIDEO_SKIP_BBOX,
        MEDIASNIPER_VIDEO_AD_LABEL_BBOX,
        getattr(site_font, "size", "unknown"),
        getattr(cta_font, "size", "unknown"),
        getattr(skip_font, "size", "unknown"),
        getattr(ad_font, "size", "unknown"),
    )
    return result


def render_getintent_video_minimum_age(base: Image.Image, minimum_age: str) -> Image.Image:
    age_text = f"{validate_minimum_age(minimum_age)}+"
    result = base.copy().convert("RGBA")
    if result.size != GETINTENT_VIDEO_MASK_SIZE:
        raise ValidationError("Маска GetIntent Video должна использовать исходный размер 1920×1215.")

    draw = ImageDraw.Draw(result)
    x, y, box_width, box_height = GETINTENT_VIDEO_MINIMUM_AGE_BBOX
    font = _fit_text_font(
        text=age_text,
        box_width=box_width,
        box_height=box_height,
        preferred_size=GETINTENT_VIDEO_MINIMUM_AGE_FONT_SIZE,
    )
    text_bbox = draw.textbbox((0, 0), age_text, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]
    text_x = x + (box_width - text_width) // 2 - text_bbox[0]
    text_y = y + (box_height - text_height) // 2 - text_bbox[1]
    draw.text((text_x, text_y), age_text, fill=GETINTENT_VIDEO_TEXT_FILL, font=font)
    logger.info(
        "getintent_video_minimum_age_rendered age=%s age_box=%s font_size=%s rendered_size=%sx%s",
        minimum_age,
        GETINTENT_VIDEO_MINIMUM_AGE_BBOX,
        getattr(font, "size", "unknown"),
        text_width,
        text_height,
    )
    return result


def _draw_fitted_text(draw: ImageDraw.ImageDraw, *, text: str, box: tuple[int, int, int, int], preferred_size: int, fill: tuple[int, int, int, int]) -> ImageFont.ImageFont:
    x, y, box_width, box_height = box
    font = _fit_text_font(text=text, box_width=box_width, box_height=box_height, preferred_size=preferred_size)
    text_bbox = draw.textbbox((0, 0), text, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]
    text_x = x
    text_y = y + max(0, (box_height - text_height) // 2) - text_bbox[1]
    draw.text((text_x, text_y), text, fill=fill, font=font)
    if text_width > box_width:
        logger.debug("fitted_text_overflow text_length=%s box_width=%s rendered_width=%s", len(text), box_width, text_width)
    return font


def _paste_target_site_favicon(base: Image.Image, *, target_site_url: str | None, favicon_path: Path | None = None, box: tuple[int, int, int, int]) -> bool:
    favicon = _load_uploaded_favicon(favicon_path) if favicon_path else _load_target_site_favicon(target_site_url)
    if favicon is None:
        return False
    x, y, width, height = box
    icon = favicon.convert("RGBA").resize((width, height), Image.Resampling.LANCZOS)
    base.alpha_composite(icon, dest=(x, y))
    return True


def _load_uploaded_favicon(favicon_path: Path | None) -> Image.Image | None:
    if favicon_path is None:
        return None
    try:
        with Image.open(favicon_path) as favicon_raw:
            favicon = favicon_raw.convert("RGBA")
    except (UnidentifiedImageError, OSError) as exc:
        logger.warning("uploaded_favicon_load_failed path=%s exception_class=%s", favicon_path.name, type(exc).__name__)
        return None
    logger.info("uploaded_favicon_loaded path=%s size=%sx%s", favicon_path.name, favicon.width, favicon.height)
    return favicon


def _load_target_site_favicon(target_site_url: str | None) -> Image.Image | None:
    parsed = urlparse((target_site_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        logger.info("yandex_video_favicon_fetch_skipped reason=missing_public_url")
        return None

    hostname = parsed.hostname.lower()
    if _hostname_resolves_to_private_address(hostname):
        logger.warning("yandex_video_favicon_fetch_skipped host=%s reason=private_or_local_address", hostname)
        return None

    origin_urls = _target_site_origin_urls(parsed)
    favicon_urls: list[str] = []
    for origin_url in origin_urls:
        favicon_urls.extend(_discover_favicon_urls_from_homepage(origin_url))
    for origin_url in origin_urls:
        favicon_urls.extend(f"{origin_url}{path}" for path in YANDEX_VIDEO_FAVICON_PATHS)
    seen_urls: set[str] = set()
    for favicon_url in favicon_urls:
        if favicon_url in seen_urls:
            continue
        seen_urls.add(favicon_url)
        logger.info("yandex_video_favicon_fetch_attempt host=%s url=%s", hostname, favicon_url)
        try:
            favicon = _fetch_favicon_image_from_public_url(favicon_url)
        except (HTTPError, URLError, TimeoutError, OSError, UnidentifiedImageError) as exc:
            logger.info("yandex_video_favicon_fetch_attempt_failed host=%s url=%s exception_class=%s", hostname, favicon_url, type(exc).__name__)
            continue
        logger.info("yandex_video_favicon_fetch_done host=%s url=%s size=%sx%s", hostname, favicon_url, favicon.width, favicon.height)
        return favicon

    logger.info("yandex_video_favicon_fetch_failed host=%s attempts=%s", hostname, len(seen_urls))
    return None


def _target_site_origin_urls(parsed_url) -> tuple[str, ...]:
    hostname = (parsed_url.hostname or "").strip().lower()
    if not hostname:
        return ()
    hostnames = [hostname]
    if hostname.startswith("www."):
        apex_hostname = hostname[4:]
        if apex_hostname and apex_hostname not in hostnames:
            hostnames.append(apex_hostname)

    origins: list[str] = []
    for candidate_hostname in hostnames:
        if candidate_hostname != hostname and _hostname_resolves_to_private_address(candidate_hostname):
            logger.warning("yandex_video_favicon_origin_skipped host=%s reason=private_or_local_address", candidate_hostname)
            continue
        port = parsed_url.port
        default_port = (parsed_url.scheme == "http" and port == 80) or (parsed_url.scheme == "https" and port == 443)
        port_suffix = f":{port}" if port and not default_port else ""
        origins.append(f"{parsed_url.scheme}://{candidate_hostname}{port_suffix}")
    return tuple(origins)


def _discover_favicon_urls_from_homepage(target_site_url: str) -> tuple[str, ...]:
    try:
        logger.info("yandex_video_favicon_html_fetch_attempt url=%s", target_site_url)
        request = Request(target_site_url, headers={"User-Agent": YANDEX_VIDEO_FAVICON_USER_AGENT})
        with urlopen(request, timeout=YANDEX_VIDEO_FAVICON_TIMEOUT_SECONDS) as response:
            html = response.read(YANDEX_VIDEO_FAVICON_HTML_MAX_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        logger.info("yandex_video_favicon_html_fetch_failed url=%s exception_class=%s", target_site_url, type(exc).__name__)
        return ()
    if len(html) > YANDEX_VIDEO_FAVICON_HTML_MAX_BYTES:
        logger.info("yandex_video_favicon_html_fetch_truncated url=%s bytes=%s", target_site_url, len(html))
        html = html[:YANDEX_VIDEO_FAVICON_HTML_MAX_BYTES]

    parser = _FaviconLinkParser(base_url=target_site_url)
    parser.feed(html.decode("utf-8", errors="ignore"))
    urls = parser.icon_urls
    logger.info("yandex_video_favicon_html_links_found url=%s count=%s", target_site_url, len(urls))
    return tuple(urls)


class _FaviconLinkParser(HTMLParser):
    def __init__(self, *, base_url: str) -> None:
        super().__init__()
        self._base_url = base_url
        self.icon_urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "link":
            return
        attributes = {key.lower(): value for key, value in attrs if value is not None}
        rel = attributes.get("rel", "").lower()
        href = attributes.get("href")
        if not href or "icon" not in rel:
            return
        icon_url = urljoin(self._base_url, href)
        if icon_url not in self.icon_urls:
            self.icon_urls.append(icon_url)


def _fetch_favicon_image_from_public_url(favicon_url: str) -> Image.Image:
    parsed = urlparse(favicon_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise OSError("favicon URL must be public http/https")
    hostname = parsed.hostname.lower()
    if _hostname_resolves_to_private_address(hostname):
        raise OSError("favicon URL resolves to a private or local address")
    return _fetch_favicon_image(favicon_url)


def _fetch_favicon_image(favicon_url: str) -> Image.Image:
    request = Request(favicon_url, headers={"User-Agent": YANDEX_VIDEO_FAVICON_USER_AGENT})
    with urlopen(request, timeout=YANDEX_VIDEO_FAVICON_TIMEOUT_SECONDS) as response:
        content = response.read(YANDEX_VIDEO_FAVICON_MAX_BYTES + 1)
        content_type = response.headers.get("Content-Type", "")
    if len(content) > YANDEX_VIDEO_FAVICON_MAX_BYTES:
        raise OSError("favicon response is too large")
    if _looks_like_svg_favicon(favicon_url=favicon_url, content_type=content_type, content=content):
        return _render_svg_favicon(content)
    with Image.open(io.BytesIO(content)) as favicon_raw:
        return favicon_raw.convert("RGBA")


def _looks_like_svg_favicon(*, favicon_url: str, content_type: str, content: bytes) -> bool:
    if favicon_url.lower().split("?", 1)[0].endswith(".svg"):
        return True
    if "svg" in content_type.lower():
        return True
    return content.lstrip().startswith((b"<svg", b"<?xml")) and b"<svg" in content[:512]


def _render_svg_favicon(content: bytes) -> Image.Image:
    png_bytes = cairosvg.svg2png(
        bytestring=content,
        output_width=YANDEX_VIDEO_FAVICON_SVG_RENDER_SIZE,
        output_height=YANDEX_VIDEO_FAVICON_SVG_RENDER_SIZE,
        unsafe=False,
    )
    with Image.open(io.BytesIO(png_bytes)) as favicon_raw:
        return favicon_raw.convert("RGBA")


def _hostname_resolves_to_private_address(hostname: str) -> bool:
    try:
        address_infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        logger.debug("yandex_video_favicon_dns_failed host=%s exception_class=%s", hostname, type(exc).__name__)
        return True

    for address_info in address_infos:
        address = address_info[4][0]
        if _is_private_or_local_ip_address(address):
            return True
    return False


def _is_private_or_local_ip_address(address: str) -> bool:
    import ipaddress

    ip_address = ipaddress.ip_address(address)
    return any(
        (
            ip_address.is_loopback,
            ip_address.is_private,
            ip_address.is_link_local,
            ip_address.is_multicast,
            ip_address.is_reserved,
            ip_address.is_unspecified,
        )
    )


def _clear_yandex_video_mask_region(base: Image.Image, raw_mask: Image.Image) -> Image.Image:
    if raw_mask.size != base.size:
        return base
    result = base.copy().convert("RGBA")
    alpha = raw_mask.getchannel("A")
    alpha_bbox = alpha.getbbox()
    if alpha_bbox is None:
        logger.warning("yandex_video_mask_region_clear_skipped reason=empty_alpha mask_size=%sx%s", raw_mask.width, raw_mask.height)
        return result
    overlay = Image.new("RGBA", result.size, (0, 0, 0, 255))
    result.paste(overlay, (0, 0), alpha)
    logger.info(
        "yandex_video_mask_region_cleared mask_size=%sx%s mask_alpha_extrema=%s mask_alpha_bbox=%s",
        raw_mask.width,
        raw_mask.height,
        alpha.getextrema(),
        alpha_bbox,
    )
    return result


def _scale_yandex_video_bbox(size: tuple[int, int], bbox: tuple[int, int, int, int] = YANDEX_VIDEO_TARGET_SITE_BBOX) -> tuple[int, int, int, int]:
    width, height = size
    scale_x = width / YANDEX_VIDEO_MASK_SIZE[0]
    scale_y = height / YANDEX_VIDEO_MASK_SIZE[1]
    x, y, box_width, box_height = bbox
    return (
        int(round(x * scale_x)),
        int(round(y * scale_y)),
        int(round(box_width * scale_x)),
        int(round(box_height * scale_y)),
    )


def _fit_text_font(*, text: str, box_width: int, box_height: int, preferred_size: int = 20) -> ImageFont.ImageFont:
    max_size = max(12, min(preferred_size, box_height - 4))
    min_size = 12
    for size in range(max_size, min_size - 1, -2):
        font = _load_font(size)
        bbox = ImageDraw.Draw(Image.new("RGBA", (1, 1))).textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= box_width and bbox[3] - bbox[1] <= box_height:
            return font
    return _load_font(min_size)


def _load_font(size: int) -> ImageFont.ImageFont:
    for candidate in YANDEX_VIDEO_FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
        logger.debug("yandex_video_font_loaded source=%s requested_size=%s actual_size=%s", candidate, size, getattr(font, "size", "unknown"))
        return font
    logger.warning("yandex_video_font_fallback_to_default requested_size=%s", size)
    return ImageFont.load_default()


def _validate_masked_banner_request(request: MaskedBannerRequest) -> None:
    if _is_video_flow(request.network_slug):
        if request.size_slug != VIDEO_FLOW_SIZE_SLUG:
            raise ValidationError("Видео-flow поддерживает только формат 1920×1080.")
        if not request.target_site_display:
            raise ValidationError("Для видео-flow нужно указать адрес целевого сайта.")
    if request.network_slug == GETINTENT_VIDEO_NETWORK_SLUG:
        if request.size_slug != GETINTENT_VIDEO_SIZE_SLUG:
            raise ValidationError("GetIntent Video поддерживает только формат 1920×1080.")
        if request.size.width != GETINTENT_VIDEO_MASK_SIZE[0] or request.size.height != GETINTENT_VIDEO_MASK_SIZE[1]:
            raise ValidationError("Маска GetIntent Video должна использовать исходный размер 1920×1215.")
        if not request.target_site_url or not request.target_site_display:
            raise ValidationError("Для GetIntent Video нужно указать адрес целевого сайта.")
        validate_minimum_age(request.minimum_age or "")


def _is_video_flow(network_slug: str | None) -> bool:
    return (network_slug or "") in VIDEO_FLOW_NETWORK_SLUGS


def _prepare_mask_overlay(mask: Image.Image) -> Image.Image:
    alpha = mask.getchannel("A")
    if alpha.getextrema() != (255, 255):
        return mask

    prepared = mask.copy()
    pixels = prepared.load()
    for y in range(prepared.height):
        for x in range(prepared.width):
            red, green, blue, current_alpha = pixels[x, y]
            if current_alpha == 255 and min(red, green, blue) >= 235 and max(red, green, blue) - min(red, green, blue) <= 12:
                pixels[x, y] = (red, green, blue, OPAQUE_MASK_BACKGROUND_ALPHA)
    return prepared
