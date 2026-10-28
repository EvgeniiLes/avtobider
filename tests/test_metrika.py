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
        assert seen["Goals"] == ["9001"] and "Conversions" in seen["FieldNames"] and seen["AttributionModels"] == ["AUTO"]

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
