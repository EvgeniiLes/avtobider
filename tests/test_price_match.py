from app.creative.compose import find_price

DESCRIPTION = "Аудит бизнес-процессов от 9 000 ₽, автоматизация с поддержкой после запуска, ИИ-агенты под ваш стек и контур."


def test_a_price_from_the_description_goes_only_to_the_ad_about_the_same_service():
    assert find_price("Автоматизация бизнеса с поддержкой после запуска", "Запишитесь", fallback=DESCRIPTION) == ""   # цена аудита чужая
    assert find_price("Аудит бизнес-процессов за пару дней", "Запишитесь", fallback=DESCRIPTION) == "от 9\xa0000\xa0₽"
    assert find_price("Маникюр в центре", "Запись", fallback="Маникюр от 1 500 ₽ в центре города.") == "от 1\xa0500\xa0₽"