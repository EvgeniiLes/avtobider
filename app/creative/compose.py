"""Подготовка и сборка картинок для объявлений.

prepare_for_direct: подгоняет любую картинку под требования Директа (для текстово-графических объявлений:
от 450 до 5000 пикселей по стороне, соотношение сторон от 1:1 до 4:3, файл до 10 МБ).
make_banner: добавляет на картинку заголовок и логотип в цветах бизнеса.
"""
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

TARGET = (1200, 900)  # 4:3: самая «широкая» из разрешённых пропорций, хорошо смотрится в выдаче
MAX_BYTES = 9 * 1024 * 1024

FONT_CANDIDATES = [
    "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def _font(size: int) -> ImageFont.ImageFont:
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def prepare_for_direct(data: bytes) -> bytes:
    """Обрезает по центру до 4:3, приводит к 1200×900 и сжимает до допустимого веса (JPEG)."""
    img = ImageOps.exif_transpose(Image.open(BytesIO(data))).convert("RGB")
    img = ImageOps.fit(img, TARGET, method=Image.LANCZOS, centering=(0.5, 0.45))
    for quality in (90, 80, 70, 55):
        out = BytesIO()
        img.save(out, "JPEG", quality=quality, optimize=True)
        if out.tell() <= MAX_BYTES:
            return out.getvalue()
    return out.getvalue()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    return lines + [line] if line else lines


def _hex(value: str, default: tuple) -> tuple:
    value = (value or "").lstrip("#")
    if len(value) == 6:
        try:
            return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            pass
    return default


def make_banner(photo: bytes, title: str, color_main: str = "", logo: bytes | None = None, layout: str = "bottom") -> bytes:
    """Картинка с заголовком на цветной плашке и логотипом. layout: bottom (плашка снизу) или left (слева)."""
    base = Image.open(BytesIO(prepare_for_direct(photo))).convert("RGB")
    w, h = base.size
    accent = _hex(color_main, (30, 30, 36))
    light = sum(accent) / 3 > 170
    ink = (20, 20, 20) if light else (255, 255, 255)
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    pad = 48
    if layout == "left":
        box = (0, 0, int(w * 0.46), h)
    else:
        box = (0, int(h * 0.62), w, h)
    d.rectangle(box, fill=accent + (235,))
    font = _font(54 if layout == "bottom" else 58)
    max_width = box[2] - box[0] - 2 * pad
    lines = _wrap(d, title, font, max_width)[:4]
    line_h = int(font.size * 1.25) if hasattr(font, "size") else 60
    y = box[1] + pad if layout == "left" else box[1] + (box[3] - box[1] - line_h * len(lines)) // 2
    for line in lines:
        d.text((box[0] + pad, y), line, font=font, fill=ink)
        y += line_h
    if logo:
        mark = Image.open(BytesIO(logo)).convert("RGBA")
        mark.thumbnail((200, 110))
        plate = Image.new("RGBA", (mark.width + 28, mark.height + 20), (255, 255, 255, 235))
        plate.alpha_composite(mark, (14, 10))
        overlay.alpha_composite(plate, (w - plate.width - 28, 28))
    result = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")
    out = BytesIO()
    result.save(out, "JPEG", quality=90, optimize=True)
    return out.getvalue()
