from io import BytesIO

from PIL import Image

from app.creative.compose import MAX_BYTES, make_banner, prepare_for_direct


def img(w, h, color=(120, 90, 200), fmt="PNG") -> bytes:
    out = BytesIO()
    Image.new("RGB", (w, h), color).save(out, fmt)
    return out.getvalue()


def test_any_photo_fits_direct_requirements():
    for size in [(1600, 1000), (800, 3000), (3000, 800), (500, 500), (4000, 4000)]:
        out = prepare_for_direct(img(*size))
        im = Image.open(BytesIO(out))
        assert im.size == (1200, 900) and len(out) <= MAX_BYTES and im.format == "JPEG"
        w, h = im.size
        assert 1 <= w / h <= 4 / 3 + 0.001 and 450 <= min(w, h) and max(w, h) <= 5000


def test_banner_has_brand_color_and_logo():
    banner = make_banner(img(1200, 900, (255, 255, 255)), "Маникюр в Москве: запись онлайн", "#c2185b", img(300, 120, (0, 0, 0)))
    im = Image.open(BytesIO(banner)).convert("RGB")
    assert im.size == (1200, 900)
    r, g, b = im.getpixel((20, 880))            # плашка внизу в цвете бизнеса
    assert abs(r - 194) < 25 and abs(g - 24) < 25 and abs(b - 91) < 25
    assert im.getpixel((20, 20)) == (255, 255, 255)  # сверху фото, без плашки
    assert sum(im.getpixel((1100, 70))) < 600     # у логотипа тёмный знак на светлой подложке


def test_left_layout_and_light_color_get_readable_text():
    banner = make_banner(img(1200, 900, (255, 255, 255)), "Очень длинный заголовок про запись на маникюр онлайн сегодня", "#ffeb3b", None, "left")
    im = Image.open(BytesIO(banner)).convert("RGB")
    assert im.size == (1200, 900)
    left = [im.getpixel((x, y)) for x in range(10, 500, 7) for y in range(10, 890, 9)]
    assert any(sum(p) < 200 for p in left)        # на светлой плашке текст тёмный, его видно
    assert im.getpixel((1100, 450)) == (255, 255, 255)  # справа фото


def test_bad_color_falls_back_to_default():
    assert Image.open(BytesIO(make_banner(img(900, 900), "Текст", "красный"))).size == (1200, 900)
