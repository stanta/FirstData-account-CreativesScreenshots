from __future__ import annotations

import ipaddress
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from PIL import Image, UnidentifiedImageError

from .models import (
    CreativeInfo,
    CreativeSpec,
    Placement,
    SUPPORTED_CREATIVE_EXTENSIONS,
    SUPPORTED_CREATIVE_MIME_TYPES,
    SUPPORTED_PLACEMENT_TEMPLATES,
)


class ValidationError(ValueError):
    """Validation error with a safe message suitable for Telegram users."""

    def __init__(self, safe_message: str) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message


def normalize_public_url(raw_url: str, *, allow_local: bool = False) -> str:
    value = (raw_url or "").strip()
    if not value:
        raise ValidationError("URL не должен быть пустым.")

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValidationError("URL должен начинаться с http:// или https://.")

    hostname = parsed.hostname
    if not hostname:
        raise ValidationError("URL должен содержать домен или IP-адрес.")

    if not allow_local and _is_local_or_private_host(hostname):
        raise ValidationError("Локальные и приватные адреса запрещены для обычного режима.")

    return urlunparse(parsed._replace(fragment=""))


def _is_local_or_private_host(hostname: str) -> bool:
    normalized = hostname.strip().lower().strip("[]")
    if normalized in {"localhost", "localhost.localdomain"} or normalized.endswith(".localhost"):
        return True

    try:
        ip = ipaddress.ip_address(normalized)
    except ValueError:
        return False

    return any(
        (
            ip.is_loopback,
            ip.is_private,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        )
    )


def parse_utm_params(raw_utm: str) -> dict[str, str]:
    value = (raw_utm or "").strip()
    if not value:
        raise ValidationError("UTM-строка не должна быть пустой.")

    if value.startswith("http://") or value.startswith("https://"):
        value = urlparse(value).query
    elif value.startswith("?"):
        value = value[1:]

    pairs = parse_qsl(value, keep_blank_values=True, strict_parsing=False)
    if not pairs:
        raise ValidationError("UTM-строка должна быть в формате utm_source=...&utm_medium=...")

    result: dict[str, str] = {}
    for key, param_value in pairs:
        key = key.strip()
        param_value = param_value.strip()
        if not key or not param_value:
            raise ValidationError("UTM-параметры не должны содержать пустые ключи или значения.")
        if not key.startswith("utm_"):
            raise ValidationError("В MVP принимаются только параметры с ключами utm_*.")
        if key in result:
            raise ValidationError(f"UTM-параметр {key} указан несколько раз.")
        result[key] = param_value

    if not result:
        raise ValidationError("Нужно указать хотя бы один UTM-параметр.")
    return result


def merge_url_with_utm(target_url: str, utm_params: dict[str, str], *, allow_local: bool = False) -> str:
    normalized_url = normalize_public_url(target_url, allow_local=allow_local)
    if not utm_params:
        raise ValidationError("Нужно указать хотя бы один UTM-параметр.")

    parsed = urlparse(normalized_url)
    existing_pairs = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key not in utm_params]
    merged_pairs = existing_pairs + list(utm_params.items())
    return urlunparse(parsed._replace(query=urlencode(merged_pairs, doseq=False)))


def validate_banner_type(raw_value: str) -> str:
    value = (raw_value or "").strip()
    if not value:
        raise ValidationError("Тип баннера не должен быть пустым.")
    if len(value) > 80:
        raise ValidationError("Тип баннера слишком длинный; максимум 80 символов.")
    return value


def validate_network_slug(raw_value: str, allowed_slugs: set[str]) -> str:
    value = (raw_value or "").strip()
    if value.isdigit():
        raise ValidationError("Отправьте slug рекламной сети, а не номер из списка.")
    if value not in allowed_slugs:
        raise ValidationError("Неизвестная рекламная сеть. Выберите slug из списка.")
    return value


def validate_spec_slug(raw_value: str, allowed_slugs: set[str]) -> str:
    value = (raw_value or "").strip()
    if value.isdigit():
        raise ValidationError("Отправьте slug типа баннера, а не номер из списка.")
    if value not in allowed_slugs:
        raise ValidationError("Неизвестный тип баннера для выбранной сети. Выберите slug из списка.")
    return value


def normalize_placement(raw_value: str) -> Placement:
    value = (raw_value or "").strip()
    if not value:
        raise ValidationError("Место размещения не должно быть пустым.")
    if len(value) > 200:
        raise ValidationError("CSS-селектор/template ID слишком длинный.")

    lowered = value.lower()
    if lowered.startswith("template:"):
        template_id = value.split(":", 1)[1].strip().lower()
        if template_id not in SUPPORTED_PLACEMENT_TEMPLATES:
            supported = ", ".join(sorted(SUPPORTED_PLACEMENT_TEMPLATES))
            raise ValidationError(f"Неизвестный template. Поддерживаются: {supported}.")
        return Placement(mode="template", value=template_id)

    if lowered.startswith("selector:"):
        selector = value.split(":", 1)[1].strip()
    else:
        selector = value

    if not selector or any(forbidden in selector for forbidden in ("<", ">", "{", "}")):
        raise ValidationError("CSS-селектор содержит недопустимые символы.")
    return Placement(mode="selector", value=selector)


def validate_creative_against_spec(creative: CreativeInfo, spec: CreativeSpec) -> None:
    if not spec.runtime_upload_supported:
        raise ValidationError("Выбранный формат пока не поддерживается runtime: MVP принимает только PNG/JPEG с фиксированными размерами.")
    if creative.mime_type not in set(spec.runtime_allowed_mime_types):
        raise ValidationError("MIME-тип креатива не поддерживается выбранным форматом.")
    if creative.path.suffix.lower().lstrip(".") not in set(spec.runtime_allowed_extensions):
        raise ValidationError("Расширение креатива не поддерживается выбранным форматом.")
    if spec.max_file_size_bytes and creative.size_bytes > spec.max_file_size_bytes:
        raise ValidationError(f"Файл превышает лимит выбранного формата: {spec.max_file_size_bytes} байт.")
    if spec.width and spec.height and (creative.width != spec.width or creative.height != spec.height):
        raise ValidationError(f"Размер креатива должен быть {spec.width}×{spec.height} пикселей.")


def validate_creative_file(
    path: Path | str,
    *,
    declared_mime_type: str | None = None,
    max_bytes: int = 10 * 1024 * 1024,
    max_pixels: int = 20_000_000,
    spec: CreativeSpec | None = None,
) -> CreativeInfo:
    file_path = Path(path)
    if not file_path.exists() or not file_path.is_file():
        raise ValidationError("Файл креатива не найден.")

    size_bytes = file_path.stat().st_size
    if size_bytes <= 0:
        raise ValidationError("Файл креатива пустой.")
    if size_bytes > max_bytes:
        raise ValidationError("Файл креатива превышает допустимый размер.")
    if file_path.suffix.lower() not in SUPPORTED_CREATIVE_EXTENSIONS:
        raise ValidationError("Поддерживаются только PNG/JPEG-файлы.")

    if declared_mime_type and declared_mime_type not in SUPPORTED_CREATIVE_MIME_TYPES:
        raise ValidationError("MIME-тип креатива не поддерживается.")

    try:
        with Image.open(file_path) as image:
            image.verify()
        with Image.open(file_path) as image:
            width, height = image.size
            image_format = (image.format or "").upper()
    except (UnidentifiedImageError, OSError):
        raise ValidationError("Файл не является корректным PNG/JPEG-изображением.") from None

    actual_mime = {"PNG": "image/png", "JPEG": "image/jpeg", "JPG": "image/jpeg"}.get(image_format)
    if actual_mime not in SUPPORTED_CREATIVE_MIME_TYPES:
        raise ValidationError("Фактический формат изображения не поддерживается.")
    if declared_mime_type and actual_mime != declared_mime_type:
        raise ValidationError("MIME-тип файла не совпадает с фактическим форматом изображения.")
    if width <= 0 or height <= 0 or width * height > max_pixels:
        raise ValidationError("Размеры изображения выходят за допустимые ограничения.")

    creative = CreativeInfo(path=file_path, mime_type=actual_mime, width=width, height=height, size_bytes=size_bytes)
    if spec is not None:
        validate_creative_against_spec(creative, spec)
    return creative
