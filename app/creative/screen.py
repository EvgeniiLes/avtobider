"""Вставка настоящего скриншота в экран телефона на баннере.

Нейросеть плохо воспроизводит чужой интерфейс, поэтому просим её нарисовать телефон с экраном, залитым однотонным
пурпурным цветом, а скриншот владельца вставляем кодом: он ложится ровно, без искажённых букв, с учётом наклона телефона.
"""
from collections import deque
from io import BytesIO

from PIL import Image, ImageChops, ImageFilter, ImageOps

KEY = (255, 0, 255)  # цвет заглушки на экране телефона


def _key_mask(img: Image.Image) -> Image.Image:
    """Чёрно-белая маска пикселей, похожих на пурпурную заглушку (с запасом на сглаживание и блики)."""
    r, g, b = img.split()
    hi_r = r.point(lambda v: 255 if v > 170 else 0)
    hi_b = b.point(lambda v: 255 if v > 170 else 0)
    lo_g = g.point(lambda v: 255 if v < 120 else 0)
    return ImageChops.multiply(ImageChops.multiply(hi_r, hi_b), lo_g)


def _largest_region(mask: Image.Image, step: int = 4) -> list[tuple[int, int]] | None:
    """Самая большая связная область маски (по крупной сетке), затем её точные пиксели."""
    w, h = mask.size
    small = mask.resize((w // step, h // step), Image.BILINEAR).point(lambda v: 255 if v > 127 else 0)
    sw, sh = small.size
    px = small.load()
    seen = [[False] * sw for _ in range(sh)]
    best: list[tuple[int, int]] = []
    for y0 in range(sh):
        for x0 in range(sw):
            if px[x0, y0] and not seen[y0][x0]:
                cells, queue = [], deque([(x0, y0)])
                seen[y0][x0] = True
                while queue:
                    x, y = queue.popleft()
                    cells.append((x, y))
                    for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                        if 0 <= nx < sw and 0 <= ny < sh and px[nx, ny] and not seen[ny][nx]:
                            seen[ny][nx] = True
                            queue.append((nx, ny))
                if len(cells) > len(best):
                    best = cells
    if not best:
        return None
    full = mask.load()
    pts = []
    for cx, cy in best:
        for y in range(cy * step, min(h, cy * step + step)):
            for x in range(cx * step, min(w, cx * step + step)):
                if full[x, y]:
                    pts.append((x, y))
    return pts


def _corners(pts: list[tuple[int, int]]) -> list[tuple[float, float]]:
    """Четыре угла экрана: верхний левый, верхний правый, нижний правый, нижний левый."""
    tl = min(pts, key=lambda p: p[0] + p[1])
    br = max(pts, key=lambda p: p[0] + p[1])
    tr = max(pts, key=lambda p: p[0] - p[1])
    bl = min(pts, key=lambda p: p[0] - p[1])
    return [tl, tr, br, bl]


def _perspective_coeffs(dest: list[tuple[float, float]], src: list[tuple[float, float]]) -> list[float]:
    """Коэффициенты перспективного преобразования для Image.transform (из четырёх пар точек)."""
    rows, rhs = [], []
    for (x, y), (u, v) in zip(dest, src):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y]); rhs.append(u)
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y]); rhs.append(v)
    n = 8
    a = [row[:] + [r] for row, r in zip(rows, rhs)]
    for i in range(n):  # метод Гаусса
        pivot = max(range(i, n), key=lambda k: abs(a[k][i]))
        a[i], a[pivot] = a[pivot], a[i]
        if abs(a[i][i]) < 1e-9:
            raise ValueError("вырожденный экран")
        for k in range(i + 1, n):
            f = a[k][i] / a[i][i]
            for c in range(i, n + 1):
                a[k][c] -= f * a[i][c]
    x = [0.0] * n
    for i in reversed(range(n)):
        x[i] = (a[i][n] - sum(a[i][c] * x[c] for c in range(i + 1, n))) / a[i][i]
    return x


def insert_screenshot(banner: bytes, screenshot: bytes) -> bytes | None:
    """Вставляет скриншот в пурпурный экран телефона. None, если экран на баннере не найден или выглядит неправдоподобно."""
    base = Image.open(BytesIO(banner)).convert("RGB")
    shot = ImageOps.exif_transpose(Image.open(BytesIO(screenshot))).convert("RGB")
    w, h = base.size
    mask = _key_mask(base)
    pts = _largest_region(mask)
    if not pts or len(pts) < w * h * 0.012:
        return None
    quad = _corners(pts)
    top, bottom = (quad[1][0] - quad[0][0], quad[1][1] - quad[0][1]), (quad[2][0] - quad[3][0], quad[2][1] - quad[3][1])
    left, right = (quad[3][0] - quad[0][0], quad[3][1] - quad[0][1]), (quad[2][0] - quad[1][0], quad[2][1] - quad[1][1])
    width = (abs(top[0]) + abs(bottom[0])) / 2 + (abs(top[1]) + abs(bottom[1])) / 2
    height = (abs(left[1]) + abs(right[1])) / 2 + (abs(left[0]) + abs(right[0])) / 2
    if width < 40 or height < 40 or not 0.25 < width / height < 4:
        return None
    # скриншот подрезаем по центру под пропорции экрана и сразу готовим к перспективной вставке
    ratio = width / height
    sw, sh = shot.size
    if sw / sh > ratio:
        new_w = int(sh * ratio)
        shot = shot.crop(((sw - new_w) // 2, 0, (sw - new_w) // 2 + new_w, sh))
    else:
        new_h = int(sw / ratio)
        shot = shot.crop((0, (sh - new_h) // 2, sw, (sh - new_h) // 2 + new_h))
    sw, sh = shot.size
    coeffs = _perspective_coeffs(quad, [(0, 0), (sw, 0), (sw, sh), (0, sh)])
    layer = shot.transform((w, h), Image.PERSPECTIVE, coeffs, Image.BICUBIC)
    region = Image.new("L", (w, h), 0)  # только сам экран, а не все пурпурные места баннера
    region_px = region.load()
    for x, y in pts:
        region_px[x, y] = 255
    cover = region.filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.GaussianBlur(0.8))  # чуть шире заглушки: без пурпурной каймы
    out = base.copy()
    out.paste(layer, (0, 0), cover)
    buf = BytesIO()
    out.save(buf, "JPEG", quality=92)
    return buf.getvalue()
