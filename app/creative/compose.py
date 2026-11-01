"""Подготовка и сборка картинок для объявлений.

prepare_for_direct: подгоняет любую картинку под требования Директа (для текстово-графических объявлений:
от 450 до 5000 пикселей по стороне, соотношение сторон от 1:1 до 4:3, файл до 10 МБ).
make_banner: добавляет на картинку заголовок и логотип в цветах бизнеса.
"""
import re
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageStat

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


def crop_window(size: tuple[int, int], ratio: float, focus: tuple[float, float] | None) -> tuple[tuple[int, int, int, int], tuple[float, float]]:
    """Окно обрезки до нужных пропорций так, чтобы главный объект (focus, доли от угла) остался в кадре, а не обрезался по центру.
    Возвращает рамку (left, top, right, bottom) и новое положение объекта внутри обрезанного кадра. Без focus режет почти по центру."""
    W, H = size
    fx, fy = focus if focus else (0.5, 0.45)
    if W / H > ratio:   # шире нужного: убираем с боков
        wc, hc = round(H * ratio), H
        left = min(max(round(fx * W - wc / 2), 0), W - wc)
        top = 0
    else:               # выше нужного: убираем сверху и снизу
        wc, hc = W, round(W / ratio)
        left = 0
        top = min(max(round(fy * H - hc / 2), 0), H - hc)
    return (left, top, left + wc, top + hc), ((fx * W - left) / wc, (fy * H - top) / hc)


def prepare_with_focus(data: bytes, focus: tuple[float, float] | None = None) -> tuple[bytes, tuple[float, float]]:
    """Обрезает до 4:3 по главному объекту, приводит к 1200×900 и сжимает до допустимого веса (JPEG). Вторым значением: где объект в итоге."""
    img = ImageOps.exif_transpose(Image.open(BytesIO(data))).convert("RGB")
    box, new_focus = crop_window(img.size, TARGET[0] / TARGET[1], focus)
    img = img.crop(box).resize(TARGET, Image.LANCZOS)
    for quality in (90, 80, 70, 55):
        out = BytesIO()
        img.save(out, "JPEG", quality=quality, optimize=True)
        if out.tell() <= MAX_BYTES:
            return out.getvalue(), new_focus
    return out.getvalue(), new_focus


def prepare_for_direct(data: bytes, focus: tuple[float, float] | None = None) -> bytes:
    """Обрезает до 4:3 (по главному объекту, если он известен), приводит к 1200×900 и сжимает до допустимого веса (JPEG)."""
    return prepare_with_focus(data, focus)[0]


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


FREE = re.compile(r"бесплатн|без оплаты|даром", re.IGNORECASE)


def _price_in(text: str) -> list[tuple[int, str]]:
    """Все цены в тексте: (сумма, «от » или пусто) в порядке появления."""
    found = []
    for m in PRICE.finditer(text or ""):
        digits = re.sub(r"\D", "", m.group(1))
        if digits and int(digits) > 0:
            found.append((int(digits), "от " if re.search(r"от\s*$", text[:m.start(1)], re.IGNORECASE) else ""))
    return found


def _label(amount: int, prefix: str) -> str:
    return f"{prefix}{amount:,}".replace(",", " ") + " ₽"   # неразрывные пробелы: сумма и знак рубля не переносятся


def find_price(*texts: str, fallback: str = "") -> str:
    """Цена вида «от 2 200 ₽» для баннера, только если она действительно названа. Ничего не придумываем.

    Сначала цена из текста самого объявления (texts). Нет её: цена из fallback (описание бизнеса), но только когда она там одна.
    В прайсе из нескольких цен непонятно, какая относится к этому объявлению, а чужая цена на баннере хуже, чем никакой
    (пилот за 39 000 ₽ не должен получить «от 9 000 ₽» от аудита). У бесплатного предложения цены нет."""
    for text in texts:
        own = _price_in(text)
        if own:
            return _label(*own[0])
    if any(FREE.search(text or "") for text in texts):
        return ""
    prices = _price_in(fallback)
    return _label(*prices[0]) if prices and len({amount for amount, _ in prices}) == 1 else ""


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


def _shaped_photo(canvas: Image.Image, photo: Image.Image, box: tuple, shape: str, radius: int = 40,
                  focus: tuple[float, float] | None = None) -> None:
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    crop, _ = crop_window(photo.size, w / h, focus)
    fitted = photo.crop(crop).resize((w, h), Image.LANCZOS).convert("RGBA")
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


def add_logo(banner: bytes, logo: bytes | None) -> bytes:
    """Ставит логотип в правый верхний угол готового баннера (например, нарисованного нейросетью)."""
    if not logo:
        return banner
    canvas = Image.open(BytesIO(banner)).convert("RGBA")
    W, H = canvas.size
    mark = Image.open(BytesIO(logo)).convert("RGBA")
    mark.thumbnail((int(W * 0.2), int(H * 0.11)))
    margin = int(W * 0.03)
    spots = {  # угол -> (позиция, штраф: правый верхний угол предпочтительнее, там мы просим ИИ оставить место)
        "tr": ((W - mark.width - margin, margin), 0),
        "tl": ((margin, margin), 6),
        "br": ((W - mark.width - margin, H - mark.height - margin), 8),
        "bl": ((margin, H - mark.height - margin), 8),
    }
    gray = canvas.convert("L")
    best, best_score = None, None
    for pos, penalty in spots.values():
        stat = ImageStat.Stat(gray.crop((pos[0], pos[1], pos[0] + mark.width, pos[1] + mark.height)))
        score = stat.stddev[0] + penalty
        if best_score is None or score < best_score:
            best, best_score = pos, score
    area = gray.crop((best[0], best[1], best[0] + mark.width, best[1] + mark.height))
    bg_light = ImageStat.Stat(area).mean[0]
    solid = mark.getchannel("A").point(lambda v: 255 if v > 128 else 0)  # только заметные пиксели логотипа
    mark_light = ImageStat.Stat(mark.convert("L"), mask=solid).mean[0] if solid.getbbox() else bg_light
    if abs(mark_light - bg_light) < 70:  # логотип сливается с фоном: мягкая подложка контрастного тона, без белой рамки
        tone = (255, 255, 255, 150) if bg_light < 128 else (20, 20, 20, 150)
        pad = int(margin * 0.5)
        plate = Image.new("RGBA", (mark.width + 2 * pad, mark.height + 2 * pad), (0, 0, 0, 0))
        ImageDraw.Draw(plate).rounded_rectangle((0, 0, plate.width - 1, plate.height - 1), radius=pad, fill=tone)
        canvas.alpha_composite(plate, (best[0] - pad, best[1] - pad))
    canvas.alpha_composite(mark, best)
    out = BytesIO()
    canvas.convert("RGB").save(out, "JPEG", quality=90, optimize=True)
    return out.getvalue()


def add_caption(banner: bytes, text: str) -> bytes:
    """Подпись внизу готового баннера (название бизнеса и вид деятельности) на затемнённой плавной полосе.

    Рисуем кодом, а не просим нейросеть: нейросеть делает такую строку мелкой и порой искажает буквы, а код держит размер (около 3,4% высоты
    баннера, чтобы читалось и в уменьшенном виде) и пишет текст ровно как задано."""
    text = (text or "").strip()
    if not text:
        return banner
    canvas = Image.open(BytesIO(banner)).convert("RGBA")
    W, H = canvas.size
    draw = ImageDraw.Draw(canvas)
    pad = round(W * 0.035)
    size = round(H * 0.034)
    font = _font(size)
    while size > round(H * 0.022) and draw.textlength(text, font=font) > W - 2 * pad:   # длинная подпись становится мельче, но не мельче 2,2% высоты
        size -= 1
        font = _font(size)
    while len(text) > 8 and draw.textlength(text, font=font) > W - 2 * pad:   # и если всё равно не влезла, обрезаем с многоточием
        text = text[:-2].rstrip() + "…"
    fade = round(size * 4.2)
    mask = Image.linear_gradient("L").resize((W, fade)).point(lambda v: int(v * 0.86))   # сверху прозрачно, к нижнему краю тёмно
    strip = Image.new("RGBA", (W, fade), (8, 8, 16, 255))
    strip.putalpha(mask)
    canvas.alpha_composite(strip, (0, H - fade))
    draw.text((pad, H - size - round(size * 0.7)), text, font=font, fill=(244, 244, 248, 255))
    out = BytesIO()
    canvas.convert("RGB").save(out, "JPEG", quality=90, optimize=True)
    return out.getvalue()


def _price_lines(badge: str) -> list[str]:
    return ["от", badge[3:]] if badge.startswith("от ") else [badge]


def _check(d, x, y, bg, mark, size=34) -> None:
    d.ellipse((x, y, x + size, y + size), fill=bg)
    d.line((x + size * .27, y + size * .52, x + size * .44, y + size * .70, x + size * .74, y + size * .32), fill=mark, width=4)


def _points(body: str) -> list[str]:
    parts = [p.strip(" .") for p in re.split(r"[.!;]", body or "") if p.strip(" .")]
    return parts[:2]


def _banner_styled(style: str, photo: bytes, title: str, color_main: str, logo: bytes | None, brand: str, badge: str, cta: str, body: str) -> bytes:
    prepared, pic_focus = prepare_with_focus(photo, getattr(photo, "focus", None))   # кадр режется по главному объекту фото
    pic = Image.open(BytesIO(prepared)).convert("RGB")
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
        _shaped_photo(canvas, pic, (560, 120, 1150, 710), "circle", focus=pic_focus)
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
        _shaped_photo(canvas, pic, (660, 70, 1130, 830), "arch", focus=pic_focus)
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
        _shaped_photo(canvas, pic, (590, 250, 1150, 810), "circle", focus=pic_focus)
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
        _shaped_photo(canvas, pic, (44, 44, 1156, 520), "round", 40, focus=pic_focus)
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
    base = Image.open(BytesIO(prepare_for_direct(photo, getattr(photo, "focus", None)))).convert("RGB")
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
