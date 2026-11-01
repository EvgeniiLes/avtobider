"""Рекомендация по бюджету: считается по цифрам самого бизнеса и правилам Яндекса и сервиса."""
from fastapi.testclient import TestClient

from app.direct.advice import budget_advice
from app.engine.util import MICRO
from app.main import app
from tests.test_launch import isolated  # noqa: F401  (временная база для каждого теста, как у остальных)


def biz(budget=0, check=0, cpa=0, share=0.3):
    return {"weekly_budget_micro": budget * MICRO, "avg_check_micro": check * MICRO, "target_cpa_micro": cpa * MICRO, "client_share": share}


def plain(text: str) -> str:
    return text.replace(" ", " ")


def test_recommended_budget_covers_ten_leads_a_week_at_the_price_the_owner_is_ready_to_pay():
    # барбершоп: клиент до 900 ₽, из обращений клиентами становятся 30%: цена обращения 270 ₽, десять обращений 2 700 ₽,
    # а кампания получает 63% бюджета, значит, нужно 4 300, но для трёх объявлений нужно не меньше 4 800
    a = budget_advice(biz(budget=6000, cpa=900))
    assert a["target_lead"] == 270 and a["compare"] == 4800 and a["recommended"] == 4800 and a["level"] == "ok"
    assert a["leads_now"] == 14.0                                               # 6 000 × 0,63 / 270

    # B2B-агентство: чек 39 000 ₽, цена клиента 30% чека, цена обращения 3 510 ₽: десять обращений 35 100 ₽, всего около 56 000 ₽ в неделю
    b = budget_advice(biz(budget=25000, check=39000))
    assert b["target_lead"] == 3510 and b["recommended"] == 56000 and b["level"] == "low"
    assert b["leads_now"] == 4.5 and "56 000" in plain(b["summary"]) and "4.5" in b["summary"].replace(",", ".")
    assert any("10 обращений" in d for d in b["details"]) and any("оценка" in d for d in b["details"])

    assert budget_advice(biz(budget=60000, check=39000))["level"] == "ok"      # денег достаточно


def test_small_budgets_are_explained_in_plain_words():
    blocked = budget_advice(biz(budget=300, cpa=900))
    assert blocked["level"] == "blocked" and "300" in plain(blocked["summary"]) and blocked["minimum"] == 500
    tiny = budget_advice(biz(budget=2500, cpa=900))
    assert tiny["level"] == "tiny" and "три объявления" in tiny["summary"] and "4 800" in plain(tiny["summary"])
    assert budget_advice(biz(budget=0, cpa=900))["level"] == "none"


def test_without_check_and_cpa_the_advice_asks_for_them_or_uses_the_only_price_in_the_description():
    asked = budget_advice(biz(budget=8000), "Аудит от 9 000 ₽. Пилот от 39 000 ₽.")
    assert asked["recommended"] is None and asked["target_lead"] is None and asked["level"] == "ok"
    assert any("средний чек" in d and "9 000" in plain(d) and "39 000" in plain(d) for d in asked["details"])   # две цены: какая типичная, спросим

    one = budget_advice(biz(budget=8000), "Маникюр от 2 200 ₽ в центре.")
    assert one["assumed_check"] == 2200 and one["target_lead"] == 198 and one["recommended"] == 4800        # цена одна: считаем от неё и говорим об этом
    assert any("цены из описания" in plain(d) for d in one["details"])


def test_advice_endpoint_works_on_form_numbers_and_the_launch_warns_when_money_is_short():
    with TestClient(app) as http:
        http.post("/api/auth/register", json={"email": "adv@example.com", "password": "password123"})
        r = http.post("/api/businesses/budget-advice", json={"weekly_budget": 25000, "avg_check": 39000, "client_share_percent": 30, "description": "Пилот от 39 000 ₽"})
        assert r.status_code == 200 and r.json()["recommended"] == 56000 and r.json()["level"] == "low"
        assert http.post("/api/businesses/budget-advice", json={"weekly_budget": -5}).status_code == 422       # отрицательный бюджет не принимаем
        anon = TestClient(app)
        assert anon.post("/api/businesses/budget-advice", json={}).status_code == 401                          # без входа нельзя
