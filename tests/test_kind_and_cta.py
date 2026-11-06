"""Длинное «чем занимаетесь» не режется в заголовке на полуслове, а у дизайн-студии кнопка не «Записаться»."""
from app.creative.angles import profile
from app.direct.texts import TITLE_MAX, draft_variants, short_kind

LONG = "Дизайн студия помогающая бизнесу продвигаться и иметь собственный узнаваемый стиль"


def biz(kind, **extra):
    return {"name": "IND Design Studio", "kind": kind, "city": "Москва", "description": "Делаем айдентику, полиграфию и баннеры.", "maps_url": "",
            "address": "", "nationwide": 0, "districts": "", **extra}


def test_a_whole_sentence_in_the_kind_field_is_shortened_to_the_point():
    assert short_kind(LONG) == "Дизайн студия"
    assert short_kind("Маникюрный салон") == "Маникюрный салон"                 # короткое не трогаем
    assert short_kind("Студия растяжки, йоги и пилатеса для всех желающих в центре") == "Студия растяжки"


def test_titles_never_end_on_a_cut_phrase():
    for v in draft_variants(biz(LONG)):
        assert "иметь" not in v["title"] and len(v["title"]) <= TITLE_MAX


def test_button_for_a_design_studio_is_about_the_project_but_a_salon_still_books():
    assert profile(biz("Дизайн-студия"))["cta"] == "Обсудить проект"
    assert profile(biz(LONG))["cta"] == "Обсудить проект"
    assert profile(biz("Салон красоты"))["cta"] == "Записаться"
    assert profile(biz("Студия маникюра и дизайна ногтей"))["cta"] == "Записаться"
    assert profile(biz("Йога-студия"))["cta"] == "Записаться"
