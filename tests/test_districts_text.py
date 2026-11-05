from app.direct.texts import in_district


def test_district_names_are_declined_for_ad_texts():
    assert in_district("Адлер") == "в Адлере" and in_district("Арбат") == "в Арбате"
    assert in_district("Хамовники") == "в Хамовниках" and in_district("Черёмушки") == "в Черёмушках"
    assert in_district("Хоста") == "в Хосте" and in_district("Строгино") == "в Строгино"
    assert in_district("Центральный") == "в Центральном районе"
    assert in_district("Красная Поляна") == "в районе Красная Поляна"   # не уверены: говорим со словом «район»