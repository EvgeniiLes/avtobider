"""Подготовка и сборка картинок для объявлений.

prepare_for_direct: подгоняет любую картинку под требования Директа (для текстово-графических объявлений:
от 450 до 5000 пикселей по стороне, соотношение сторон от 1:1 до 4:3, файл до 10 МБ).
make_banner: добавляет на картинку заголовок и логотип в цветах бизнеса.
"""
import re
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


def _scrim(size: tuple[int, int], layout: str, strength: int = 215) -> Image.Image:
    """Плавное затемнение там, где лежит текст: фото остаётся видно целиком, заголовок читается."""
    w, h = size
    ramp = Image.linear_gradient("L")  # сверху чёрный, снизу белый
    if layout == "left":
        mask = ImageOps.mirror(ramp.rotate(90, expand=True)).resize((int(w * 0.62), h), Image.BILINEAR)  # слева густо, вправо тает
        origin = (0, 0)
    else:
        mask = ramp.resize((w, int(h * 0.56)), Image.BILINEAR)  # снизу густо, вверх тает
        origin = (0, h - mask.height)
    mask = mask.point(lambda v: int(v * strength / 255))
    scrim = Image.new("RGBA", size, (0, 0, 0, 0))
    scrim.paste(Image.new("RGBA", mask.size, (12, 14, 20, 255)), origin, mask)
    return scrim


PRICE = re.compile(r"(?:от\s*)?(\d{1,3}(?:[\s ]\d{3})+|\d{3,6})\s*(?:₽|руб\.?|р\.)", re.IGNORECASE)


def find_price(*texts: str) -> str:
    """Цена вида «от 2 200 ₽», только если она действительно названа в текстах бизнеса. Ничего не придумываем."""
    for text in texts:
        m = PRICE.search(text or "")
        if m:
            digits = re.sub(r"\D", "", m.group(1))
            if digits and int(digits) > 0:
                prefix = "от " if re.search(r"от\s*$", (text or "")[:m.start(1)], re.IGNORECASE) else ""
                return f"{prefix}{int(digits):,}".replace(",", " ") + " ₽"
    return ""


def make_banner(photo: bytes, title: str, color_main: str = "", logo: bytes | None = None, layout: str = "bottom",
                brand: str = "", badge: str = "", cta: str = "") -> bytes:
    """Фото с предложением. layout: bottom (текст внизу) или left (слева).
    Фото не закрываем плашкой: плавное затемнение под текстом, название бизнеса, крупный заголовок,
    бейдж с ценой (если она есть) и кнопка-призыв, как в обычной рекламе салонов."""
    base = Image.open(BytesIO(prepare_for_direct(photo))).convert("RGB")
    w, h = base.size
    accent = _hex(color_main, (255, 255, 255))
    on_accent = (20, 20, 20) if sum(accent) / 3 > 150 else (255, 255, 255)
    canvas = Image.alpha_composite(base.convert("RGBA"), _scrim(base.size, layout))
    d = ImageDraw.Draw(canvas)
    pad = 56
    text_w = (int(w * 0.46) if layout == "left" else w - 2 * pad)
    lines, font = [], _font(60)
    for size in (64, 58, 52, 46, 40, 36):  # самый крупный размер, при котором заголовок помещается в три строки
        font = _font(size)
        lines = _wrap(d, title, font, text_w)
        if len(lines) <= 3:
            break
    lines = lines[:3]
    line_h = int(getattr(font, "size", 48) * 1.22)
    block_h = line_h * len(lines)
    cta_h = 78 if cta else 0
    gap = 24 if cta else 0
    top_of_text = (h - (block_h + gap + cta_h)) // 2 if layout == "left" else h - pad - cta_h - gap - block_h
    y = top_of_text
    if brand:
        small = _font(30)
        d.text((pad + 2, y - 52), brand.upper(), font=small, fill=(0, 0, 0, 110))
        d.text((pad, y - 54), brand.upper(), font=small, fill=(255, 255, 255, 215))
    for line in lines:
        d.text((pad + 2, y + 3), line, font=font, fill=(0, 0, 0, 110))  # лёгкая тень
        d.text((pad, y), line, font=font, fill=(255, 255, 255, 255))
        y += line_h
    if cta:
        btn = _font(34)
        bw = int(d.textlength(cta, font=btn)) + 76
        by = y + gap
        d.rounded_rectangle((pad, by, pad + bw, by + cta_h), radius=cta_h // 2, fill=accent + (255,))
        d.text((pad + 38, by + (cta_h - 34) // 2 - 2), cta, font=btn, fill=on_accent + (255,))
    if badge:
        bf = _font(52)
        bw = int(d.textlength(badge, font=bf)) + 64
        d.rounded_rectangle((pad, 48, pad + bw, 48 + 92), radius=46, fill=accent + (255,))
        d.text((pad + 32, 48 + 14), badge, font=bf, fill=on_accent + (255,))
    if logo:
        mark = Image.open(BytesIO(logo)).convert("RGBA")
        mark.thumbnail((190, 100))
        plate = Image.new("RGBA", (mark.width + 32, mark.height + 24), (0, 0, 0, 0))
        ImageDraw.Draw(plate).rounded_rectangle((0, 0, plate.width - 1, plate.height - 1), radius=18, fill=(255, 255, 255, 240))
        plate.alpha_composite(mark, (16, 12))
        canvas.alpha_composite(plate, (w - plate.width - 32, 32))
    out = BytesIO()
    canvas.convert("RGB").save(out, "JPEG", quality=90, optimize=True)
    return out.getvalue()
