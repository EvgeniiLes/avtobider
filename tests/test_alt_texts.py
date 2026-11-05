import json

from fastapi.testclient import TestClient

from app.creative.texts_ai import clean_alt_texts
from app.main import app
from tests.test_ad_editor import Fakes, setup
from tests.test_creatives import isolated  # noqa: F401  (фикстура с отдельной базой на каждый тест)

FACTS = "Груминг собак и кошек. Работаем 7 дней в неделю с 9 до 21. Бережно, без седативных."


def test_alternative_texts_follow_directs_rules_and_the_owners_facts():
    raw = json.dumps(["Запишитесь онлайн: работаем 7 дней в неделю", "ЗАПИШИТЕСЬ ОНЛАЙН СЕГОДНЯ ДЛЯ ВАШЕГО ПИТОМЦА", "Скидка 30% на первый визит!",
                      "Бережный уход без седативных", "Бережный уход без седативных", "Груминг в Адлере", "Звоните +7 999 123-45-67 или salon.ru",
                      "Текущий текст объявления", "Работаем с 9 до 21 без выходных"], ensure_ascii=False)
    out = clean_alt_texts(raw, "Текущий текст объявления", "Груминг в Адлере", FACTS)
    assert out == ["Запишитесь онлайн: работаем 7 дней в неделю", "Бережный уход без седативных", "Работаем с 9 до 21 без выходных"]
    assert clean_alt_texts("не json", "x", "y", FACTS) == []

def test_text_options_endpoint_returns_alternatives_for_the_same_headline(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes()
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}/creatives"
        ad = http.get(f"{base}/ads").json()["ads"][0]
        fakes.claude_text = json.dumps(["Запишитесь онлайн в удобное время", "Мастера с опытом, аккуратная работа", "Маникюр рядом с метро и без очередей"], ensure_ascii=False)
        r = http.post(f"{base}/ads/{ad['index']}/text-options")
        assert r.status_code == 200 and 1 <= len(r.json()["options"]) <= 4 and ad["text"] not in r.json()["options"]
        assert http.post(f"{base}/ads/99/text-options").status_code == 400
