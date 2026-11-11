"""Вставка настоящего скриншота в экран телефона на баннере.

Нейросеть плохо воспроизводит чужой интерфейс, поэтому просим её нарисовать телефон с экраном, залитым однотонным
пурпурным цветом, а скриншот владельца вставляем кодом: он ложится ровно, без искажённых букв, с учётом наклона телефона.
"""
from collections import deque
from io import BytesIO

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageStat

KEY = (255, 0, 255)  # цвет заглушки на экране телефона


def _green_mask(img: Image.Image) -> Image.Image:
    r, g, b = img.split()
    return ImageChops.multiply(ImageChops.multiply(g.point(lambda v: 255 if v > 170 else 0), r.point(lambda v: 255 if v < 110 else 0)),
                               b.point(lambda v: 255 if v < 110 else 0))


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


def _hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    pts = sorted(set(points))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _simplify(poly: list[tuple[float, float]], keep: int) -> list[tuple[float, float]]:
    """Убирает вершины с наименьшим вкладом (площадью треугольника), пока не останется keep штук."""
    poly = list(poly)
    while len(poly) > keep:
        n = len(poly)
        areas = []
        for i in range(n):
            a, b, c = poly[i - 1], poly[i], poly[(i + 1) % n]
            areas.append(abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])))
        poly.pop(areas.index(min(areas)))
    return poly


def _line_cross(p1, p2, p3, p4):
    d = (p1[0] - p2[0]) * (p3[1] - p4[1]) - (p1[1] - p2[1]) * (p3[0] - p4[0])
    if abs(d) < 1e-9:
        return None
    a = p1[0] * p2[1] - p1[1] * p2[0]
    b = p3[0] * p4[1] - p3[1] * p4[0]
    return ((a * (p3[0] - p4[0]) - (p1[0] - p2[0]) * b) / d, (a * (p3[1] - p4[1]) - (p1[1] - p2[1]) * b) / d)


def _corners(pts: list[tuple[int, int]]) -> list[tuple[float, float]]:
    """Четыре угла экрана: верхний левый, верхний правый, нижний правый, нижний левый.

    Скруглённые края и перекрытия пальцем сдвигают крайние точки, поэтому углы берём как пересечения
    четырёх самых длинных сторон выпуклой оболочки области.
    """
    rows: dict[int, list[int]] = {}
    for x, y in pts:
        lo_hi = rows.setdefault(y, [x, x])
        lo_hi[0], lo_hi[1] = min(lo_hi[0], x), max(lo_hi[1], x)
    edge = [(lo, y) for y, (lo, hi) in rows.items()] + [(hi, y) for y, (lo, hi) in rows.items()]
    poly = _simplify(_hull(edge), 10)
    quad = None
    if len(poly) >= 4:
        n = len(poly)
        sides = sorted(range(n), key=lambda i: -((poly[(i + 1) % n][0] - poly[i][0]) ** 2 + (poly[(i + 1) % n][1] - poly[i][1]) ** 2))[:4]
        sides.sort()
        cs = []
        for k in range(4):
            i, j = sides[k], sides[(k + 1) % 4]
            c = _line_cross(poly[i], poly[(i + 1) % n], poly[j], poly[(j + 1) % n])
            if c is None:
                break
            cs.append(c)
        if len(cs) == 4:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            bw, bh = max(xs) - min(xs), max(ys) - min(ys)
            near = all(min(xs) - 0.2 * bw <= c[0] <= max(xs) + 0.2 * bw and min(ys) - 0.2 * bh <= c[1] <= max(ys) + 0.2 * bh for c in cs)
            if near:   # у узкого экрана почти параллельные стороны дают пересечения «в космосе»: такой результат не берём
                quad = cs
    if quad is None:
        tl = min(pts, key=lambda p: p[0] + p[1])
        br = max(pts, key=lambda p: p[0] + p[1])
        tr = max(pts, key=lambda p: p[0] - p[1])
        bl = min(pts, key=lambda p: p[0] - p[1])
        return [tl, tr, br, bl]
    tl = min(quad, key=lambda p: p[0] + p[1])
    br = max(quad, key=lambda p: p[0] + p[1])
    tr = max(quad, key=lambda p: p[0] - p[1])
    bl = min(quad, key=lambda p: p[0] - p[1])
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


def insert_logo(banner: bytes, logo: bytes) -> bytes | None:
    """Вставляет настоящий логотип в зелёный прямоугольник, который нейросеть оставила под него. None, если слот не найден."""
    base = Image.open(BytesIO(banner)).convert("RGB")
    mark = Image.open(BytesIO(logo)).convert("RGBA")
    w, h = base.size
    pts = _largest_region(_green_mask(base))
    if not pts or len(pts) < w * h * 0.004:
        return None
    x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts) + 1
    y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts) + 1
    if len(pts) < (x1 - x0) * (y1 - y0) * 0.8:  # не прямоугольник: это не слот, а случайное зелёное место
        return None
    box = (x0 - 3, y0 - 3, x1 + 3, y1 + 3)
    ring = [(max(0, x0 - 12), max(0, y0 - 12), min(w, x1 + 12), max(0, y0 - 4)), (max(0, x0 - 12), min(h, y1 + 4), min(w, x1 + 12), min(h, y1 + 12)),
            (max(0, x0 - 12), y0, max(0, x0 - 4), y1), (min(w, x1 + 4), y0, min(w, x1 + 12), y1)]
    ring = [r for r in ring if r[2] > r[0] and r[3] > r[1]]
    if not ring:
        return None
    medians = [ImageStat.Stat(base.crop(r)).median for r in ring]  # цвет вокруг слота: медиана, а не среднее (иначе рядом буквы дают серую заплатку)
    around = tuple(sorted(m[i] for m in medians)[len(medians) // 2] for i in range(3))
    bg_light = 0.299 * around[0] + 0.587 * around[1] + 0.114 * around[2]
    solid = mark.getchannel("A").point(lambda v: 255 if v > 128 else 0)
    mark_light = ImageStat.Stat(mark.convert("L"), mask=solid).mean[0] if solid.getbbox() else bg_light
    opaque = mark.getchannel("A").getextrema()[0] > 240   # логотип с собственным фоном (JPG): он сам себе плашка, без полей и без рамки слота
    fill = around
    if not opaque and abs(mark_light - bg_light) < 110:  # логотип слился бы с фоном: слот заливаем контрастным тоном (чистая плашка-бейдж)
        fill = (250, 250, 250) if mark_light < 128 else (20, 20, 20)
    out = base.copy()
    near = (max(0, x0 - 40), max(0, y0 - 40), min(w, x1 + 40), min(h, y1 + 40))  # остатки зелёной заглушки рядом со слотом (метки по углам и т.п.)
    leftovers = _green_mask(base.crop(near)).filter(ImageFilter.MaxFilter(7))
    out.paste(Image.new("RGB", leftovers.size, around), near[:2], leftovers)
    ImageDraw.Draw(out).rounded_rectangle(box, radius=(y1 - y0) // 6, fill=fill)
    inner = mark.copy()
    share = 1.0 if opaque else 0.92
    inner.thumbnail((int((x1 - x0 + 6) * share), int((y1 - y0 + 6) * share)))
    out.paste(inner, ((x0 + x1 - inner.width) // 2, (y0 + y1 - inner.height) // 2), inner)
    buf = BytesIO()
    out.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def insert_screenshot(banner: bytes, screenshot: bytes) -> bytes | None:
    """Вставляет скриншот в пурпурный экран телефона. None, если экран на баннере не найден или выглядит неправдоподобно."""
    base = Image.open(BytesIO(banner)).convert("RGB")
    shot = ImageOps.exif_transpose(Image.open(BytesIO(screenshot))).convert("RGB")
    w, h = base.size
    mask = _key_mask(base)
    pts = _largest_region(mask)
    if not pts or len(pts) < w * h * 0.003:   # экран в сцене с людьми бывает небольшим, но скриншот на нём всё равно должен встать
        return None
    quad = _corners(pts)
    top, bottom = (quad[1][0] - quad[0][0], quad[1][1] - quad[0][1]), (quad[2][0] - quad[3][0], quad[2][1] - quad[3][1])
    left, right = (quad[3][0] - quad[0][0], quad[3][1] - quad[0][1]), (quad[2][0] - quad[1][0], quad[2][1] - quad[1][1])
    width = (abs(top[0]) + abs(bottom[0])) / 2 + (abs(top[1]) + abs(bottom[1])) / 2
    height = (abs(left[1]) + abs(right[1])) / 2 + (abs(left[0]) + abs(right[0])) / 2
    if width < 24 or height < 24 or not 0.25 < width / height < 4:
        return None
    # скриншот подрезаем по центру под пропорции экрана и сразу готовим к перспективной вставке
    ratio = width / height
    sw, sh = shot.size
    if max(ratio / (sw / sh), (sw / sh) / ratio) > 1.35:   # телефон повёрнут иначе, чем скриншот: вписываем целиком, а не режем середину
        fitted = Image.new("RGB", (max(sw, int(sh * ratio)), max(sh, int(sw / ratio))), (0, 0, 0))
        fitted.paste(shot, ((fitted.width - sw) // 2, (fitted.height - sh) // 2))
        shot = fitted
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
    # куски заглушки, отделённые от экрана пальцем, тоже закрашиваем (тёмным цветом экрана), но только рядом с телефоном
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    pad_x, pad_y = int((max(xs) - min(xs)) * 0.35) + 10, int((max(ys) - min(ys)) * 0.35) + 10
    near = (max(0, min(xs) - pad_x), max(0, min(ys) - pad_y), min(w, max(xs) + pad_x), min(h, max(ys) + pad_y))
    stray = ImageChops.subtract(_key_mask(base), cover.filter(ImageFilter.MaxFilter(9))).crop(near).filter(ImageFilter.MaxFilter(5))  # без самого экрана
    out.paste(Image.new("RGB", stray.size, (14, 14, 18)), near[:2], stray)
    buf = BytesIO()
    out.save(buf, "JPEG", quality=92)
    return buf.getvalue()
