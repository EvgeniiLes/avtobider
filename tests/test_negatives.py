from app.direct.negatives import negatives_for


def test_common_words_always_present_and_no_repeats():
    for kind in ("Маникюрный салон", "Йога-студия", "Что-то странное", ""):
        words = negatives_for(kind)
        assert {"бесплатно", "вакансии", "обучение"} <= set(words)
        assert len(words) == len(set(words)) <= 40


def test_niche_words_follow_the_business_kind():
    assert "лампа" in negatives_for("Маникюрный салон") and "коврик" not in negatives_for("Маникюрный салон")
    assert "коврик" in negatives_for("Йога-студия") and "лампа" not in negatives_for("Йога-студия")
    assert "машинка" in negatives_for("Барбершоп")


def test_never_blocks_what_the_business_sells():
    """Слова, которыми люди ищут саму услугу, в минус-словах быть не должны."""
    sells = {"маникюр", "педикюр", "йога", "запись", "салон", "студия", "цена", "стоимость", "занятие", "абонемент", "отзывы"}
    for kind in ("Маникюрный салон", "Йога-студия", "Фитнес-студия", "Массаж"):
        assert not (sells & set(negatives_for(kind))), kind
