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


def test_banner_darkens_photo_under_text_and_keeps_the_rest():
    banner = make_banner(img(1200, 900, (255, 255, 255)), "Маникюр в Москве: запись онлайн", "#c2185b", img(300, 120, (0, 0, 0)))
    im = Image.open(BytesIO(banner)).convert("RGB")
    assert im.size == (1200, 900)
    assert sum(im.getpixel((1150, 880))) < 300       # внизу плавное затемнение под текстом, фото не закрыто плашкой
    assert im.getpixel((600, 100)) == (255, 255, 255)  # верх фото нетронут
    assert sum(im.getpixel((1100, 70))) < 600         # у логотипа тёмный знак на светлой подложке


def test_badge_and_button_use_brand_color():
    banner = make_banner(img(1200, 900, (255, 255, 255)), "Маникюр", "#c2185b", None, "bottom",
                         brand="Мята", badge="от 2 200 ₽", cta="Записаться")
    im = Image.open(BytesIO(banner)).convert("RGB")
    colors = {im.getpixel((x, 60)) for x in range(60, 140)}      # бейдж с ценой слева сверху
    assert any(abs(r - 194) < 6 and abs(g - 24) < 6 and abs(b - 91) < 6 for r, g, b in colors)
    button = [im.getpixel((x, y)) for x in range(70, 120, 3) for y in range(700, 900, 5)]  # кнопка внизу слева
    assert any(abs(r - 194) < 6 and abs(g - 24) < 6 and abs(b - 91) < 6 for r, g, b in button)


def test_left_layout_gets_readable_white_text_on_dark_edge():
    banner = make_banner(img(1200, 900, (255, 255, 255)), "Очень длинный заголовок про запись на маникюр онлайн сегодня", "#ffeb3b", None, "left")
    im = Image.open(BytesIO(banner)).convert("RGB")
    assert im.size == (1200, 900)
    assert sum(im.getpixel((10, 450))) < 300           # слева затемнение, на нём виден белый текст
    assert im.getpixel((1100, 450)) == (255, 255, 255)  # справа фото нетронуто


def test_five_templates_are_really_different():
    from app.creative.compose import STYLES, style_for
    assert len(STYLES) == 5 and [style_for(i) for i in range(6)] == list(STYLES) + [STYLES[0]]
    photo = img(1200, 900, (200, 150, 120))
    banners = [Image.open(BytesIO(make_banner(photo, "Маникюр в центре Москвы", "", None, "bottom", "Мята", "от 2 200 ₽",
                                             "Записаться", style, "Запись онлайн. Работаем ежедневно"))).convert("RGB") for style in STYLES]
    assert all(b.size == (1200, 900) for b in banners)
    corners = {b.getpixel((12, 12)) for b in banners} | {b.getpixel((600, 880)) for b in banners}
    assert len(corners) >= 4  # фоны разные: тёмный, пастельный, яркий, кремовый, фото
    assert banners[1].getpixel((12, 12)) != banners[3].getpixel((12, 12))


def test_price_badge_does_not_split_a_number():
    from app.creative.compose import _price_lines
    assert _price_lines("от 2 800 ₽") == ["от", "2 800 ₽"]
    assert _price_lines("990 ₽") == ["990 ₽"]


def test_price_is_taken_only_from_real_texts():
    from app.creative.compose import find_price
    assert find_price("Маникюр от 2 200 ₽") == "от 2 200 ₽"
    assert find_price("Привет", "Запись онлайн", "педикюр 2800 руб.") == "2 800 ₽"
    assert find_price("Маникюр в центре", "Запись онлайн", "Работаем с 2021 года, 3 мастера") == ""  # цифры без цены: ничего не придумываем
    assert find_price("", None) == ""


def test_price_from_the_description_is_used_only_when_it_is_unambiguous():
    from app.creative.compose import find_price as raw_price
    find_price = lambda *a, **k: raw_price(*a, **k).replace(" ", " ")   # в ответе неразрывные пробелы, в проверках обычные
    prices = "Аудит от 9 000 ₽ (1 неделя). Пилот от 39 000 ₽ (4–6 недель)."
    assert find_price("Аудит бизнес-процессов от 9 000 ₽", "Карта процессов", fallback=prices) == "от 9 000 ₽"   # цена самого объявления главнее
    assert find_price("Пилот с поддержкой 30 дней", "Решение под ключ", fallback=prices) == ""                   # прайс из двух цен: чужую не берём
    assert find_price("Маникюр", "Запись онлайн", fallback="Маникюр от 2 200 ₽, гель-лак. Снова от 2 200 ₽") == "от 2 200 ₽"   # цена одна, хоть и повторена
    assert find_price("Бесплатный разбор за 30 минут", "Без обязательств", fallback="Консультация от 2 200 ₽") == ""   # у бесплатного цены нет
    assert find_price("Бесплатный разбор, потом от 9 000 ₽", "", fallback=prices) == "от 9 000 ₽"                  # а названная в тексте остаётся


def test_bad_color_falls_back_to_default():
    assert Image.open(BytesIO(make_banner(img(900, 900), "Текст", "красный"))).size == (1200, 900)
