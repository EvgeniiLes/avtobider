"""Логотип без фона: если загруженный логотип лежит на сплошном фоне, фон убирает модель удаления фона (BiRefNet на fal.ai).

Такая модель вырезает настоящей маской и ничего не перерисовывает (GPT Image прозрачный фон не умеет). Результат всё равно
сверяем с оригиналом: знак не должен потерять части. Если модель недоступна или ошиблась, берём простое вырезание
однотонного фона кодом.
"""
import asyncio
import base64
from collections import deque
from io import BytesIO

import httpx
from PIL import Image, ImageChops, ImageFilter

from app.creative import fal

MODEL = "fal-ai/birefnet/v2"


def has_transparency(data: bytes) -> bool:
    """Есть ли у логотипа настоящий прозрачный фон: углы прозрачные и прозрачных пикселей заметная доля."""
    img = Image.open(BytesIO(data)).convert("RGBA")
    alpha = img.getchannel("A")
    w, h = img.size
    corners = [alpha.getpixel(p) for p in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]
    clear = sum(alpha.histogram()[:40]) / (w * h)
    return max(corners) < 40 and clear > 0.05


def _border_color(img: Image.Image) -> tuple[tuple[int, int, int], float]:
    """Цвет фона по краю картинки и доля краевых пикселей, близких к нему."""
    px = img.convert("RGB").load()
    w, h = img.size
    border = [px[x, 0] for x in range(w)] + [px[x, h - 1] for x in range(w)] + [px[0, y] for y in range(h)] + [px[w - 1, y] for y in range(h)]
    bg = tuple(sorted(c[i] for c in border)[len(border) // 2] for i in range(3))
    near = sum(1 for c in border if max(abs(c[i] - bg[i]) for i in range(3)) <= 40)
    return bg, near / len(border)


def knockout_background(data: bytes, tolerance: int = 40) -> bytes | None:
    """Запасной способ: вырезает однотонный фон, связанный с краями картинки. None, если фон не однотонный."""
    img = Image.open(BytesIO(data)).convert("RGBA")
    w, h = img.size
    if w * h > 6_000_000:
        return None
    bg, share = _border_color(img)
    if share < 0.9:
        return None  # по краю разные цвета: это не однотонный фон
    px = img.load()
    seen = [[False] * w for _ in range(h)]
    queue = deque()
    for x in range(w):
        queue.extend(((x, 0), (x, h - 1)))
    for y in range(h):
        queue.extend(((0, y), (w - 1, y)))
    mask = Image.new("L", (w, h), 255)
    mp = mask.load()
    while queue:
        x, y = queue.popleft()
        if not (0 <= x < w and 0 <= y < h) or seen[y][x]:
            continue
        seen[y][x] = True
        c = px[x, y]
        if max(abs(c[i] - bg[i]) for i in range(3)) > tolerance:
            continue
        mp[x, y] = 0
        queue.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))
    mask = mask.filter(ImageFilter.GaussianBlur(0.7))
    img.putalpha(ImageChops.multiply(img.getchannel("A"), mask))
    out = BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def _kept_everything(original: bytes, cut: bytes) -> bool:
    """Модель не отрезала часть знака и не оставила фон: видимое совпадает с тем, что отличается от фона в оригинале."""
    src = Image.open(BytesIO(original)).convert("RGBA")
    res = Image.open(BytesIO(cut)).convert("RGBA")
    if res.size != src.size:
        res = res.resize(src.size)
    visible = res.getchannel("A").point(lambda v: 255 if v > 128 else 0)
    total = src.width * src.height
    seen = sum(visible.histogram()[255:]) / total
    if not 0.01 < seen < 0.92:
        return False  # вырезано всё или ничего не вырезано
    bg, share = _border_color(src)
    if share < 0.9:
        return True   # фон не однотонный, сверить не с чем: достаточно прозрачности
    rgb = src.convert("RGB")
    content = ImageChops.difference(rgb, Image.new("RGB", src.size, bg)).convert("L").point(lambda v: 255 if v > 40 else 0)
    both = ImageChops.multiply(content, visible)
    content_n = sum(content.histogram()[255:])
    visible_n = sum(visible.histogram()[255:])
    if not content_n or not visible_n:
        return False
    kept = sum(both.histogram()[255:])
    return kept / content_n > 0.9 and kept / visible_n > 0.7   # модель может заполнить просветы между буквами, но не отрезать знак


async def remove_background(data: bytes, fal_key: str, transport=None, timeout: int = 90) -> tuple[bytes, str]:
    """Возвращает (логотип без фона, как это сделано: 'ai', 'code' или 'none')."""
    if has_transparency(data):
        return data, "none"
    if fal_key:
        try:
            payload = {"image_url": "data:image/png;base64," + base64.b64encode(data).decode(), "model": "General Use (Heavy)",
                       "operating_resolution": "2048x2048", "output_format": "png", "refine_foreground": True}
            async with httpx.AsyncClient(timeout=60, transport=transport) as http:
                job = await fal._submit(http, MODEL, fal_key, payload)
            for _ in range(timeout // 2):
                state, value = await fal.poll(job, fal_key, transport)
                if state == "failed":
                    break
                if state == "done":
                    cut = await fal.download(value, transport)
                    if has_transparency(cut) and _kept_everything(data, cut):
                        return cut, "ai"
                    break
                await asyncio.sleep(2)
        except (fal.FalError, OSError, ValueError):
            pass
    cut = knockout_background(data)
    return (cut, "code") if cut else (data, "none")
