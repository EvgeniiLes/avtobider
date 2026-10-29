"""Проверка и подготовка загруженных файлов бизнеса.

Картинки перекодируются: так пропадают скрытые данные (например, координаты
съёмки в EXIF), а подделанный файл не пройдёт. Видео проверяется по началу файла.
"""
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

IMAGE_MAX_BYTES = 15 * 1024 * 1024
VIDEO_MAX_BYTES = 100 * 1024 * 1024
MAX_SIDE = 3000
MIN_PHOTO_SIDE = 450  # меньшая сторона; Директ требует от 450 пикселей
MIN_LOGO_SIDE = 100

KINDS = ("logo", "photo", "video")
PHOTO_TAGS = {
    "interior": "Помещение",
    "work": "Наши работы",
    "team": "Команда",
    "product": "Товар или услуга",
    "screen": "Скриншот приложения",
    "other": "Другое",
}
LIMITS = {"logo": 1, "photo": 20, "video": 3}


class AssetError(Exception):
    """Файл не подошёл. Текст можно показывать пользователю."""


@dataclass
class Prepared:
    data: bytes
    ext: str
    mime: str
    width: int | None
    height: int | None


def prepare_image(data: bytes, kind: str) -> Prepared:
    if len(data) > IMAGE_MAX_BYTES:
        raise AssetError("Картинка слишком тяжёлая. Загрузите файл до 15 МБ.")
    try:
        img = Image.open(BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise AssetError("Не получилось открыть файл. Загрузите картинку в формате JPG, PNG или WEBP.")
    img = ImageOps.exif_transpose(img)
    min_side = MIN_LOGO_SIDE if kind == "logo" else MIN_PHOTO_SIDE
    if min(img.size) < min_side:
        raise AssetError(f"Картинка слишком маленькая. Нужно хотя бы {min_side} пикселей по короткой стороне.")
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = BytesIO()
    if kind == "logo":
        img.convert("RGBA").save(out, "PNG", optimize=True)
        ext, mime = "png", "image/png"
    else:
        img.convert("RGB").save(out, "JPEG", quality=90, optimize=True)
        ext, mime = "jpg", "image/jpeg"
    return Prepared(out.getvalue(), ext, mime, img.size[0], img.size[1])


def prepare_video(data: bytes) -> Prepared:
    if len(data) > VIDEO_MAX_BYTES:
        raise AssetError("Видео слишком тяжёлое. Загрузите файл до 100 МБ.")
    if data[4:8] == b"ftyp":
        if data[8:12] == b"qt  ":
            return Prepared(data, "mov", "video/quicktime", None, None)
        return Prepared(data, "mp4", "video/mp4", None, None)
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return Prepared(data, "webm", "video/webm", None, None)
    raise AssetError("Не получилось открыть видео. Загрузите файл MP4, MOV или WEBM.")
