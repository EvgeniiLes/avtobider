"""Раскладка баннера без референса зависит от текста и бизнеса, а общее правило кадра есть в каждом промпте с нуля."""
from app.creative import fal
from app.creative.art import LAYOUTS, TEXT_HEAVY, layout_for


def test_long_text_or_price_gets_a_wide_text_layout():
    for i in range(8):
        assert layout_for(i, "Капучино от 250 ₽, завтрак от 400 ₽", "Автосервис")[0] in TEXT_HEAVY
        assert layout_for(i, "Замена масла", "Автосервис", has_price=True)[0] in TEXT_HEAVY


def test_short_text_gets_a_picture_led_layout_and_giant_word_needs_a_short_word():
    keys = {layout_for(i, "Маникюр", "Салон")[0] for i in range(8)}
    assert keys <= {"giant_word", "still_life", "metaphor", "hand", "arch"}
    assert "giant_word" not in {layout_for(i, "Шиномонтажная", "Шиномонтаж")[0] for i in range(8)}   # слово длиннее десяти букв огромным не выйдет


def test_food_keeps_to_food_friendly_layouts_and_neighbours_differ():
    keys = [layout_for(i, "Капучино от 250 ₽", "Кофейня", True)[0] for i in range(4)]
    assert set(keys) <= {"still_life", "color_split", "airy", "dark", "arch"} and len(set(keys)) > 1
    assert layout_for(0)[0] in LAYOUTS                                                                # без текста выбор как раньше


def test_every_scratch_prompt_keeps_the_subject_whole_and_the_picture_unframed():
    biz = {"name": "Кофейня", "kind": "Кофейня", "city": "", "description": "", "visual_hint": "", "allow_promo": 0}
    prompt = fal.banner_prompt(biz, "Кофе рядом", "", "Узнать подробнее", scene="a barista", direction="x")
    assert "fully inside the frame" in prompt and "no person's head, face or hands" in prompt
