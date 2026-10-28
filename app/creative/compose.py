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


STYLES = ("photo", "luxe", "pastel", "bold", "ai")  # пять непохожих шаблонов: основа для сравнения вариантов (A/B)

SERIF_CANDIDATES = [
    "C:/Windows/Fonts/georgiab.ttf", "C:/Windows/Fonts/timesbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
]


def style_for(index: int) -> str:
    return STYLES[index % len(STYLES)]


def _serif(size: int) -> ImageFont.ImageFont:
    for path in SERIF_CANDIDATES:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return _font(size)


def _mix(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(int(a[i] * (1 - t) + b[i] * t) for i in range(3))


def _fit(d: ImageDraw.ImageDraw, text: str, sizes, width: int, max_lines: int, font_fn=None):
    """Самый крупный размер, при котором текст помещается в max_lines строк."""
    font_fn = font_fn or _font
    font, lines = font_fn(sizes[-1]), []
    for size in sizes:
        font = font_fn(size)
        lines = _wrap(d, text, font, width)
        if len(lines) <= max_lines:
            break
    return font, lines[:max_lines]


def _text(d, xy, text, font, fill):
    d.text(xy, text, font=font, fill=fill)


def _lines(d, x, y, lines, font, fill, gap=1.2) -> int:
    step = int(getattr(font, "size", 40) * gap)
    for line in lines:
        _text(d, (x, y), line, font, fill)
        y += step
    return y


def _pill(d, x, y, text, font, bg, fg, pad_x=38, h=76, outline=None) -> int:
    w = int(d.textlength(text, font=font)) + 2 * pad_x
    d.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=bg, outline=outline, width=3 if outline else 0)
    _text(d, (x + pad_x, y + (h - getattr(font, "size", 34)) // 2 - 3), text, font, fg)
    return x + w


def _shaped_photo(canvas: Image.Image, photo: Image.Image, box: tuple, shape: str, radius: int = 40) -> None:
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    fitted = ImageOps.fit(photo, (w, h), Image.LANCZOS, centering=(0.5, 0.45)).convert("RGBA")
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    if shape == "circle":
        md.ellipse((0, 0, w - 1, h - 1), fill=255)
    elif shape == "arch":
        md.ellipse((0, 0, w - 1, w - 1), fill=255)
        md.rectangle((0, w // 2, w - 1, h - 1), fill=255)
    else:
        md.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    canvas.paste(fitted, (x0, y0), mask)


def _logo_plate(logo: bytes | None, size=(190, 100)) -> Image.Image | None:
    if not logo:
        return None
    mark = Image.open(BytesIO(logo)).convert("RGBA")
    mark.thumbnail(size)
    plate = Image.new("RGBA", (mark.width + 32, mark.height + 24), (0, 0, 0, 0))
    ImageDraw.Draw(plate).rounded_rectangle((0, 0, plate.width - 1, plate.height - 1), radius=18, fill=(255, 255, 255, 240))
    plate.alpha_composite(mark, (16, 12))
    return plate


def _price_lines(badge: str) -> list[str]:
    return ["от", badge[3:]] if badge.startswith("от ") else [badge]


def _check(d, x, y, bg, mark, size=34) -> None:
    d.ellipse((x, y, x + size, y + size), fill=bg)
    d.line((x + size * .27, y + size * .52, x + size * .44, y + size * .70, x + size * .74, y + size * .32), fill=mark, width=4)


def _points(body: str) -> list[str]:
    parts = [p.strip(" .") for p in re.split(r"[.!;]", body or "") if p.strip(" .")]
    return parts[:2]


def _banner_styled(style: str, photo: bytes, title: str, color_main: str, logo: bytes | None, brand: str, badge: str, cta: str, body: str) -> bytes:
    pic = Image.open(BytesIO(prepare_for_direct(photo))).convert("RGB")
    W, H = pic.size
    d_default = {"luxe": (201, 164, 92), "pastel": (214, 120, 140), "bold": (214, 51, 132), "card": (46, 150, 120)}[style]
    accent = _hex(color_main, d_default)
    on_accent = (20, 20, 20) if sum(accent) / 3 > 165 else (255, 255, 255)
    logo_plate = _logo_plate(logo)
    logo_at = (W - 32 - (logo_plate.width if logo_plate else 0), 30)

    if style == "luxe":
        bg = (22, 22, 27)
        canvas = Image.new("RGBA", (W, H), bg + (255,))
        d = ImageDraw.Draw(canvas)
        d.rectangle((22, 22, W - 23, H - 23), outline=accent + (255,), width=2)
        _shaped_photo(canvas, pic, (560, 120, 1150, 710), "circle")
        d.ellipse((548, 108, 1162, 722), outline=accent + (255,), width=3)
        x, y = 80, 150
        if brand:
            _text(d, (x, y), brand.upper(), _font(28), accent + (255,))
            y += 64
        font, lines = _fit(d, title, (56, 50, 44, 38, 34), 420, 5, _serif)
        y = _lines(d, x, y, lines, font, (245, 240, 228), 1.25) + 18
        if badge:
            _text(d, (x, y), badge.replace("\u00a0", " "), _serif(52), accent + (255,))
            y += 80
        _pill(d, x, y + 34, cta or "Записаться", _font(32), accent + (255,), on_accent + (255,))
    elif style == "pastel":
        bg = _mix(accent, (255, 255, 255), 0.86) if color_main else (253, 238, 232)
        canvas = Image.new("RGBA", (W, H), bg + (255,))
        d = ImageDraw.Draw(canvas)
        _shaped_photo(canvas, pic, (660, 70, 1130, 830), "arch")
        ink = (52, 38, 44)
        x, y = 76, 190
        if brand:
            _text(d, (x, y), brand.upper(), _font(28), _mix(accent, (0, 0, 0), 0.25) + (255,))
            y += 56
        font, lines = _fit(d, title, (62, 56, 50, 44, 38), 520, 4)
        y = _lines(d, x, y, lines, font, ink + (255,), 1.2) + 14
        d.rounded_rectangle((x, y, x + 90, y + 6), radius=3, fill=accent + (255,))
        _pill(d, x, 690, cta or "Записаться", _font(32), ink + (255,), (255, 255, 255, 255))
        if badge:
            parts = _price_lines(badge)
            d.ellipse((520, 520, 730, 730), fill=accent + (255,))
            fonts = [_font(30), _font(44)] if len(parts) == 2 else [_font(44)]
            total = sum(int(getattr(f, "size", 30) * 1.25) for f in fonts)
            ty = 625 - total // 2
            for part, f in zip(parts, fonts):
                tw = d.textlength(part.strip(), font=f)
                _text(d, (625 - tw / 2, ty), part.strip(), f, on_accent + (255,))
                ty += int(getattr(f, "size", 30) * 1.25)
    elif style == "bold":
        canvas = Image.new("RGBA", (W, H), accent + (255,))
        d = ImageDraw.Draw(canvas)
        _shaped_photo(canvas, pic, (590, 250, 1150, 810), "circle")
        d.ellipse((580, 240, 1160, 820), outline=(255, 255, 255, 255), width=8)
        x, y = 70, 190
        if brand:
            _text(d, (x, y - 60), brand.upper(), _font(28), on_accent + (210,))
        font, lines = _fit(d, title.upper(), (60, 54, 48, 42, 36), 600, 4)
        y = _lines(d, x, y, lines, font, on_accent + (255,), 1.18) + 22
        for p in _points(body):
            _check(d, x, y + 4, (255, 255, 255, 255), accent + (255,))
            pf, pl = _fit(d, p, (34, 30, 27), 470, 2)
            y = _lines(d, x + 52, y, pl, pf, on_accent + (255,), 1.2) + 10
        _pill(d, x, 730, cta or "Записаться", _font(32), (255, 255, 255, 255), accent + (255,))
        if badge:
            parts = _price_lines(badge)
            d.ellipse((880, 40, 1130, 290), fill=(255, 255, 255, 255))
            fonts = [_font(32), _font(50)] if len(parts) == 2 else [_font(50)]
            total = sum(int(getattr(f, "size", 30) * 1.25) for f in fonts)
            ty = 165 - total // 2
            for part, f in zip(parts, fonts):
                tw = d.textlength(part.strip(), font=f)
                _text(d, (1005 - tw / 2, ty), part.strip(), f, accent + (255,))
                ty += int(getattr(f, "size", 30) * 1.25)
        logo_at = (70, 40)
    else:  # card: светлая карточка
        canvas = Image.new("RGBA", (W, H), (250, 244, 235, 255))
        d = ImageDraw.Draw(canvas)
        _shaped_photo(canvas, pic, (44, 44, 1156, 520), "round", 40)
        ink = (40, 36, 34)
        x, y = 70, 560
        if brand:
            _text(d, (x, y), brand.upper(), _font(26), accent + (255,))
            y += 46
        font, lines = _fit(d, title, (50, 46, 42, 38, 34), 660, 3)
        y = _lines(d, x, y, lines, font, ink + (255,), 1.2)
        for p in _points(body)[:1]:
            pf, pl = _fit(d, p, (30, 27, 24), 660, 2)
            _lines(d, x, y + 8, pl, pf, (110, 100, 94, 255), 1.25)
        right = 800
        if badge:
            _text(d, (right, 580), badge.replace("\u00a0", " "), _font(58), accent + (255,))
        _pill(d, right, 720, cta or "Записаться", _font(32), accent + (255,), on_accent + (255,))
    if logo_plate:
        canvas.alpha_composite(logo_plate, logo_at)
    out = BytesIO()
    canvas.convert("RGB").save(out, "JPEG", quality=90, optimize=True)
    return out.getvalue()


def make_banner(photo: bytes, title: str, color_main: str = "", logo: bytes | None = None, layout: str = "bottom",
                brand: str = "", badge: str = "", cta: str = "", style: str = "photo", body: str = "") -> bytes:
    """Баннер объявления. style: один из пяти шаблонов (photo, luxe, pastel, bold, card), они специально непохожи.
    Шаблон photo: фото с плавным затемнением; layout: bottom (текст внизу) или left (слева)."""
    if style == "ai":  # настоящий ИИ-баннер подставляется отдельно; без него остаётся светлая карточка
        style = "card"
    if style in ("luxe", "pastel", "bold", "card"):
        return _banner_styled(style, photo, title, color_main, logo, brand, badge, cta, body)
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
