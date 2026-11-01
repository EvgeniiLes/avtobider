import json
import sqlite3
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.deps import client_factory, yandex_http
from app.direct.runner import sync_and_run
from app.engine.util import MICRO, day_add
from app.main import app
from app.routes import auth
from app.security import encrypt_token
from tests.fake_direct import FakeDirect

BUDGET = 20_000


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.delenv("FAL_KEY", raising=False)   # запуск сам составляет профиль аудитории, если есть ключ; в тестах ходить к настоящему ИИ нельзя
    auth._fails.clear()
    yield tmp_path / "test.db"
    app.dependency_overrides.clear()


def metrika_stub(counters=None, goals=None, status=200):
    counters = counters if counters is not None else [{"id": 555, "name": "Салон", "site": "salon.ru"}]
    goals = goals if goals is not None else [{"id": 9001, "name": "Обращение"}, {"id": 9002, "name": "Просмотр"}]
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers["Authorization"]))
        if status != 200:
            return httpx.Response(status, json={})
        if request.url.path.endswith("/counters"):
            return httpx.Response(200, json={"counters": counters})
        return httpx.Response(200, json={"goals": goals})

    app.dependency_overrides[yandex_http] = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return seen


def setup(http, fake, db_path):
    http.post("/api/auth/register", json={"email": "o@example.com", "password": "password123"})
    app.dependency_overrides[client_factory] = fake.factory()
    bid = http.post("/api/businesses", json={"name": "Лотос", "kind": "Маникюрный салон", "city": "Москва", "weekly_budget": BUDGET,
                                             "target_cpa": 800, "description": "Маникюр.", "site_url": "salon.ru", "mode": "auto"}).json()["id"]
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO ad_accounts (user_id, title, token_enc) VALUES (1, 'fake-login', ?)", (encrypt_token("tok", "test-secret-key"),))
    conn.commit()
    conn.close()
    return bid


def test_list_save_and_clear(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        seen = metrika_stub()
        data = http.get(f"/api/businesses/{bid}/metrika/counters").json()
        assert data["counters"] == [{"id": "555", "name": "Салон", "site": "salon.ru"}] and "reachGoal" in data["how_to"]
        assert seen[0][1] == "OAuth tok"
        goals = http.get(f"/api/businesses/{bid}/metrika/counters/555/goals").json()["goals"]
        assert [g["name"] for g in goals] == ["Обращение", "Просмотр"]

        assert http.put(f"/api/businesses/{bid}/metrika", json={"counter_id": "555", "goal_id": "1"}).status_code == 400  # чужая цель
        assert http.put(f"/api/businesses/{bid}/metrika", json={"counter_id": "x", "goal_id": "9001"}).status_code == 400
        assert http.put(f"/api/businesses/{bid}/metrika", json={"counter_id": "555", "goal_id": "9001"}).status_code == 200
        biz = http.get(f"/api/businesses/{bid}/metrika/counters").json()
        assert biz["selected"] == {"counter": "555", "goal": "9001"}
        assert http.delete(f"/api/businesses/{bid}/metrika").status_code == 200
        assert http.get(f"/api/businesses/{bid}/metrika/counters").json()["selected"] == {"counter": "", "goal": ""}


def test_metrika_errors_are_plain(isolated):
    with TestClient(app) as http:
        bid = setup(http, FakeDirect(), isolated)
        metrika_stub(status=403)
        r = http.get(f"/api/businesses/{bid}/metrika/counters")
        assert r.status_code == 409 and "нет доступа к Метрике" in r.json()["detail"]["message"]
        metrika_stub(status=500)
        assert http.get(f"/api/businesses/{bid}/metrika/counters").status_code == 502


def test_without_cabinet_metrika_asks_to_connect_it_first():
    with TestClient(app) as http:
        http.post("/api/auth/register", json={"email": "o@example.com", "password": "password123"})
        bid = http.post("/api/businesses", json={"name": "x", "kind": "y"}).json()["id"]
        metrika_stub()
        r = http.get(f"/api/businesses/{bid}/metrika/counters")
        assert r.status_code == 400 and "подключите рекламный кабинет" in r.json()["detail"]["message"]


def test_changing_the_goal_reaches_a_campaign_that_already_optimizes_for_it(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True})
        metrika_stub()
        assert http.put(f"/api/businesses/{bid}/metrika", json={"counter_id": "555", "goal_id": "9001"}).status_code == 200
        (campaign,) = fake.campaigns.values()
        assert campaign["Strategy"] == "WB_MAXIMUM_CLICKS"                      # на кликах цель Директу не нужна

        conn = connect(isolated)
        conn.execute("UPDATE campaigns SET strategy = 'conversions'")           # сервис уже перевёл кампанию на конверсии
        conn.commit()
        assert http.put(f"/api/businesses/{bid}/metrika", json={"counter_id": "555", "goal_id": "9002"}).status_code == 200
        assert campaign["Strategy"] == "WB_MAXIMUM_CONVERSION_RATE" and campaign["GoalId"] == 9002   # Яндекс оптимизирует под новую цель

        updates = fake.calls.count(("campaigns", "update"))
        assert http.put(f"/api/businesses/{bid}/metrika", json={"counter_id": "555", "goal_id": "9002"}).status_code == 200
        assert fake.calls.count(("campaigns", "update")) == updates + 1         # цель не менялась: только привязка счётчика, стратегию не трогаем
        conn.close()


def test_real_leads_flow_into_engine_decisions(isolated):
    """С Метрикой сервис видит обращения: отчёт Директа запрашивается с целью, слабые объявления получают оценку."""
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True})
        metrika_stub()
        assert http.put(f"/api/businesses/{bid}/metrika", json={"counter_id": "555", "goal_id": "9001"}).status_code == 200
        conn = connect(isolated)
        assert ("campaigns", "update") in fake.calls  # счётчик привязан к уже созданным кампаниям

        fake.accept_all(state="ON", payment="ALLOWED")
        today = date.today().isoformat()
        biz = lambda: conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        sync_and_run(conn, biz(), today, transport=fake.transport)
        seen = fake.report_requests[-1]
        assert seen["Goals"] == ["9001", "9002"] and "Conversions" in seen["FieldNames"]   # «обращение» и остальные цели счётчика and seen["AttributionModels"] == ["AUTO"]

        # объявление 0 дорогое и без обращений, объявление 1 дешёвое с обращениями
        fake.report_rows = [(day_add(today, -d), 0, 1000, 40, 1000 * MICRO, 0) for d in (1, 2, 3, 4)] + \
                           [(day_add(today, -d), 1, 1000, 40, 300 * MICRO, 6) for d in (1, 2, 3, 4)]
        sync_and_run(conn, biz(), today, transport=fake.transport)
        assert conn.execute("SELECT SUM(leads) FROM stats_daily").fetchone()[0] == 24
        assert conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] > 0  # решения теперь принимаются
        conn.close()

        ov = http.get(f"/api/businesses/{bid}/overview").json()
        assert ov["conversions"] is True and ov["totals"]["leads"] == 24
        assert ov["totals"]["clients_estimated"] is True and ov["totals"]["clients"] == round(24 * 0.3)
        assert not any("Метрика ещё не подключена" in n for n in ov["notices"])

        # владелец записал настоящее число клиентов: оценка заменяется точной цифрой
        today = date.today().isoformat()
        assert http.post(f"/api/businesses/{bid}/clients", json={"day": today, "count": 5}).status_code == 200
        assert http.post(f"/api/businesses/{bid}/clients", json={"day": today, "count": 6}).status_code == 200  # правка того же дня
        ov = http.get(f"/api/businesses/{bid}/overview").json()
        assert ov["totals"]["clients"] == 6 and ov["totals"]["clients_estimated"] is False
        assert http.get(f"/api/businesses/{bid}/clients").json()["marks"] == [{"day": today, "count": 6}]
        assert http.post(f"/api/businesses/{bid}/clients", json={"day": "2999-01-01", "count": 1}).status_code == 400
        assert http.post(f"/api/businesses/{bid}/clients", json={"day": "вчера", "count": 1}).status_code == 422
        assert http.post(f"/api/businesses/{bid}/clients", json={"day": today, "count": -1}).status_code == 422


# ---------- все цели счётчика: весь путь посетителя, а не одно «обращение» ----------

GOALS = [{"id": 9001, "name": "Заявка"}, {"id": 9002, "name": "Клик по кнопке «Позвонить»"}, {"id": 9003, "name": "Открыл форму"}, {"id": 9004, "name": "Дошёл до оплаты"}]


def connected(http, fake, db_path):
    """Бизнес с запущенной рекламой и подключённой Метрикой: главная цель 9001, в счётчике ещё три."""
    bid = setup(http, fake, db_path)
    http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True})
    metrika_stub(goals=GOALS)
    assert http.put(f"/api/businesses/{bid}/metrika", json={"counter_id": "555", "goal_id": "9001"}).status_code == 200
    fake.metrika_goals = GOALS
    fake.accept_all(state="ON", payment="ALLOWED")
    return bid


def test_sync_keeps_every_goal_by_day_and_ad_and_only_the_main_one_counts_as_lead(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = connected(http, fake, isolated)
        conn = connect(isolated)
        today = date.today().isoformat()
        fake.report_rows = [(today, 0, 1000, 30, 450 * MICRO, {"9001": 2, "9002": 9, "9003": 4}),
                            (today, 1, 800, 20, 200 * MICRO, {"9002": 5})]
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        assert sync_and_run(conn, biz, today, transport=fake.transport)["error"] == ""

        assert fake.report_requests[-1]["Goals"] == ["9001", "9002", "9003", "9004"]
        per_goal = {r["goal_id"]: r["n"] for r in conn.execute("SELECT goal_id, SUM(conversions) AS n FROM goal_stats GROUP BY goal_id")}
        assert per_goal == {"9001": 2, "9002": 14, "9003": 4, "9004": 0}
        assert conn.execute("SELECT SUM(leads) FROM stats_daily").fetchone()[0] == 2   # решает движок по «обращению», остальные цели его не сбивают
        assert {r["goal_id"]: r["name"] for r in conn.execute("SELECT * FROM metrika_goals")}["9002"] == "Клик по кнопке «Позвонить»"

        # повторная синхронизация за те же дни не удваивает достижения
        sync_and_run(conn, biz, today, transport=fake.transport)
        assert conn.execute("SELECT SUM(conversions) FROM goal_stats").fetchone()[0] == 20
        conn.close()

        data = http.get(f"/api/businesses/{bid}/goals").json()
        assert data["connected"] is True and data["clicks"] == 50
        assert [g["id"] for g in data["goals"]] == ["9001", "9002", "9003", "9004"]   # «обращение» первым, дальше по числу достижений
        main, call, form, pay = data["goals"]
        assert main["main"] is True and main["name"] == "Заявка" and main["count"] == 2
        assert call["name"] == "Клик по кнопке «Позвонить»" and call["count"] == 14 and call["per_100_clicks"] == 28.0
        assert call["cost"] == round(650 / 14, 2)   # расход всех объявлений, делённый на достижения цели
        assert pay["count"] == 0 and pay["cost"] is None   # цель без достижений тоже видна: по нулю понятно, что действие не происходит


def test_directs_refusal_of_many_goals_does_not_lose_the_main_one(isolated):
    fake = FakeDirect()
    fake.single_goal_only = True
    with TestClient(app) as http:
        bid = connected(http, fake, isolated)
        conn = connect(isolated)
        today = date.today().isoformat()
        fake.report_rows = [(today, 0, 1000, 30, 450 * MICRO, 3)]
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        assert sync_and_run(conn, biz, today, transport=fake.transport)["error"] == ""
        assert fake.report_requests[-1]["Goals"] == ["9001"]
        assert conn.execute("SELECT SUM(leads) FROM stats_daily").fetchone()[0] == 3
        assert {r["goal_id"]: r["conversions"] for r in conn.execute("SELECT * FROM goal_stats")} == {"9001": 3}
        conn.close()


def test_goal_list_is_not_wiped_when_metrika_does_not_answer(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = connected(http, fake, isolated)
        conn = connect(isolated)
        fake.metrika_goals = []   # Метрика промолчала: прежний список остаётся
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        sync_and_run(conn, biz, date.today().isoformat(), transport=fake.transport)
        assert conn.execute("SELECT COUNT(*) FROM metrika_goals WHERE business_id = ?", (bid,)).fetchone()[0] == 4
        conn.close()


def test_goals_page_is_empty_without_metrika(isolated):
    with TestClient(app) as http:
        bid = setup(http, FakeDirect(), isolated)
        data = http.get(f"/api/businesses/{bid}/goals").json()
        assert data["connected"] is False and data["goals"] == []