import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.deps import client_factory
from app.direct.runner import sync_and_run
from app.engine.cycle import run_cycle
from app.engine.util import MICRO, day_add
from app.main import app
from app.platforms.direct import DirectPlatform
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


def setup(http, fake, db_path, **business):
    http.post("/api/auth/register", json={"email": "o@example.com", "password": "password123"})
    app.dependency_overrides[client_factory] = fake.factory()
    body = {"name": "Лотос", "kind": "Маникюрный салон", "city": "Москва", "weekly_budget": BUDGET, "target_cpa": 800,
            "description": "Маникюр и педикюр в центре города.", "site_url": "salon.ru", "mode": "auto"}
    body.update(business)
    bid = http.post("/api/businesses", json=body).json()["id"]
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO ad_accounts (user_id, title, token_enc) VALUES (1, 'fake-login', ?)", (encrypt_token("tok", "test-secret-key"),))
    conn.commit()
    conn.close()
    return bid


def rows(db_path, sql, *args):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


# ---------- план и запуск ----------

def test_preview_shows_what_will_be_created(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        plan = http.get(f"/api/businesses/{bid}/launch/preview").json()
        assert plan["count"] == 6 and plan["city"] == "Москва" and plan["link"] == "https://salon.ru"
        assert plan["per_unit"] * plan["count"] <= BUDGET * 0.9 * 0.7 + 6
        assert all(len(v["title"]) <= 56 and len(v["text"]) <= 81 for v in plan["variants"])
        assert len({v["title"] for v in plan["variants"]}) == 6
        assert plan["warnings"] == []
        assert fake.campaigns == {}  # просмотр ничего не создаёт


@pytest.mark.parametrize("change,message", [
    ({"site_url": ""}, "сайт или ссылку"),
    ({"city": ""}, "город"),
    ({"city": "Нигдеград"}, "Не нашёл город"),
    ({"weekly_budget": 100}, "слишком мал"),
])
def test_preview_explains_problems_in_plain_words(isolated, change, message):
    with TestClient(app) as http:
        bid = setup(http, FakeDirect(), isolated, **change)
        r = http.get(f"/api/businesses/{bid}/launch/preview")
        assert r.status_code == 400 and message in r.json()["detail"]["message"]


def test_preview_needs_a_cabinet():
    with TestClient(app) as http:
        http.post("/api/auth/register", json={"email": "o@example.com", "password": "password123"})
        app.dependency_overrides[client_factory] = FakeDirect().factory()
        bid = http.post("/api/businesses", json={"name": "x", "kind": "y", "city": "Москва", "site_url": "a.ru", "weekly_budget": 20000}).json()["id"]
        r = http.get(f"/api/businesses/{bid}/launch/preview")
        assert r.status_code == 400 and "подключите рекламный кабинет" in r.json()["detail"]["message"]


def test_launch_creates_campaigns_with_hard_budgets_and_sends_to_moderation(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        r = http.post(f"/api/businesses/{bid}/launch", json={})
        assert r.status_code == 200 and r.json()["created"] == 6
        assert len(fake.campaigns) == 6 and all(c["Status"] == "MODERATION" for c in fake.campaigns.values())
        assert sum(c["Limit"] for c in fake.campaigns.values()) <= BUDGET * MICRO * 0.9 * 0.7 + 6
        assert all(c["Limit"] >= 300 * MICRO for c in fake.campaigns.values())
        assert ("ads", "moderate") in fake.calls
        again = http.post(f"/api/businesses/{bid}/launch", json={})
        assert again.status_code == 400 and "уже создана" in again.json()["detail"]["message"]
        ov = http.get(f"/api/businesses/{bid}/overview").json()
        assert ov["real"] and not ov["example"] and len(ov["units"]) == 6
        assert {u["verdict"] for u in ov["units"]} == {"На проверке в Яндексе"}
        assert any("проверяет" in n for n in ov["notices"]) and any("Метрика" in n for n in ov["notices"])


def test_launch_cleans_up_when_direct_refuses(isolated):
    fake = FakeDirect(fail_add_after=3)  # на четвёртой кампании Директ откажет
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        r = http.post(f"/api/businesses/{bid}/launch", json={})
        assert r.status_code == 400 and "лимит кампаний" in r.json()["detail"]["message"]
        assert fake.campaigns == {}, "уже созданные кампании должны быть удалены"
        assert rows(isolated, "SELECT COUNT(*) AS n FROM ad_units")[0]["n"] == 0


def test_launch_replaces_example_data(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        http.post(f"/api/businesses/{bid}/sim/start")
        assert http.get(f"/api/businesses/{bid}/overview").json()["example"]
        http.post(f"/api/businesses/{bid}/launch", json={})
        ov = http.get(f"/api/businesses/{bid}/overview").json()
        assert ov["real"] and not ov["example"] and len(ov["units"]) == 6


# ---------- синхронизация и движок на настоящих данных ----------

def launched(http, fake, db_path):
    bid = setup(http, fake, db_path)
    http.post(f"/api/businesses/{bid}/launch", json={})
    return bid


def test_sync_follows_moderation_payment_and_loads_stats(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        conn = connect(isolated)
        biz = lambda: conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        today = date.today().isoformat()

        sync_and_run(conn, biz(), today, transport=fake.transport)
        assert {r["state"] for r in conn.execute("SELECT state FROM ad_units")} == {"moderation"}

        fake.accept_all()  # одобрено, но счёт не пополнен
        sync_and_run(conn, biz(), today, transport=fake.transport)
        assert {r["state"] for r in conn.execute("SELECT state FROM ad_units")} == {"unpaid"}

        for c in fake.campaigns.values():
            c["State"], c["StatusPayment"] = "ON", "ALLOWED"
        fake.report_rows = [(day_add(today, -1), 0, 1000, 30, 450 * MICRO), (today, 1, 500, 10, 150 * MICRO)]
        result = sync_and_run(conn, biz(), today, transport=fake.transport)
        assert result["error"] == "" and result["synced"]["rows"] == 2
        assert {r["state"] for r in conn.execute("SELECT state FROM ad_units")} == {"active"}
        assert conn.execute("SELECT SUM(spend_micro) FROM stats_daily").fetchone()[0] == 600 * MICRO
        conn.close()

        ov = http.get(f"/api/businesses/{bid}/overview").json()
        assert ov["totals"]["spend"] == 600.0 and ov["sync"]["at"] and ov["sync"]["error"] == ""


def test_without_metrika_engine_only_guards_the_budget(isolated):
    """Обращений нет: тратить деньги слепо нельзя, но и останавливать хорошее по ошибке тоже."""
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        conn = connect(isolated)
        biz = lambda: conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        today = date.today().isoformat()
        fake.accept_all(state="ON", payment="ALLOWED")
        # на каждое объявление потрачено заметно, а обращений в данных нет
        fake.report_rows = [(day_add(today, -d), i, 1000, 40, 900 * MICRO) for i in range(3) for d in (1, 2, 3)]
        sync_and_run(conn, biz(), today, transport=fake.transport)
        assert conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 0
        assert ("campaigns", "suspend") not in fake.calls

        # бюджет недели исчерпан: защита срабатывает даже без Метрики и всё останавливается в Директе
        fake.report_rows = [(day_add(today, -1), i, 9000, 400, 4000 * MICRO) for i in range(6)]
        sync_and_run(conn, biz(), today, transport=fake.transport)
        assert ("campaigns", "suspend") in fake.calls
        assert {c["State"] for c in fake.campaigns.values()} == {"SUSPENDED"}
        assert {r["state"] for r in conn.execute("SELECT state FROM ad_units")} == {"paused"}
        conn.close()


def test_stop_all_on_real_ads_works_in_direct_and_holds_for_later_approvals(isolated, monkeypatch):
    import app.direct.client as client_module
    fake = FakeDirect()
    monkeypatch.setattr(client_module, "DEFAULT_TRANSPORT", fake.transport)
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        fake.accept_all(state="ON", payment="ALLOWED")
        conn = connect(isolated)
        biz = lambda: conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        sync_and_run(conn, biz(), transport=fake.transport)
        assert http.post(f"/api/businesses/{bid}/stop-all").json()["stopped"] == 6
        assert {c["State"] for c in fake.campaigns.values()} == {"SUSPENDED"}

        # владелец пополнил счёт, Яндекс «включил» кампании сам: пока стоп-кран нажат, сервис снова их останавливает
        for c in fake.campaigns.values():
            c["State"] = "ON"
        sync_and_run(conn, biz(), transport=fake.transport)
        assert {c["State"] for c in fake.campaigns.values()} == {"SUSPENDED"}

        assert http.post(f"/api/businesses/{bid}/resume-all").json()["resumed"] == 6
        assert {c["State"] for c in fake.campaigns.values()} == {"ON"}
        assert biz()["status"] == "running"
        conn.close()


def test_direct_platform_changes_budget_and_waits_for_variant_moderation(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        conn = connect(isolated)
        today = date.today().isoformat()
        unit = conn.execute("SELECT * FROM ad_units ORDER BY id LIMIT 1").fetchone()
        platform = DirectPlatform(conn, "tok", transport=fake.transport)

        platform.set_budget(unit["id"], 700 * MICRO, today)
        assert fake.campaigns[unit["external_campaign_id"]]["Limit"] == 700 * MICRO
        assert conn.execute("SELECT budget_micro FROM ad_units WHERE id = ?", (unit["id"],)).fetchone()[0] == 700 * MICRO

        variant_id = platform.create_variant(unit["id"], today)
        v = conn.execute("SELECT * FROM ad_units WHERE id = ?", (variant_id,)).fetchone()
        assert v["variant_of"] == unit["id"] and v["state"] == "moderation" and v["title"] != unit["title"]
        assert len(fake.campaigns) == 7
        conn.close()


def test_worker_syncs_every_real_business_and_survives_errors(isolated):
    from app.worker import run_once
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        fake.accept_all(state="ON", payment="ALLOWED")
        fake.report_rows = [(date.today().isoformat(), 0, 1000, 30, 450 * MICRO)]
        assert run_once(transport=fake.transport) == 1
        conn = connect(isolated)
        assert conn.execute("SELECT SUM(spend_micro) FROM stats_daily").fetchone()[0] == 450 * MICRO
        assert conn.execute("SELECT last_sync_at FROM businesses WHERE id = ?", (bid,)).fetchone()[0]
        # у второго бизнеса сломан доступ: первый всё равно обработан, ошибка записана
        conn.execute("UPDATE ad_accounts SET expires_at = '2020-01-01T00:00:00+00:00'")
        conn.commit()
        assert run_once(transport=fake.transport) == 1
        assert "истёк" in conn.execute("SELECT last_sync_error FROM businesses WHERE id = ?", (bid,)).fetchone()[0]
        conn.close()


def test_sync_error_is_saved_and_shown(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        conn = connect(isolated)
        conn.execute("UPDATE ad_accounts SET expires_at = '2020-01-01T00:00:00+00:00'")
        conn.commit()
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        result = sync_and_run(conn, biz, transport=fake.transport)
        assert "истёк" in result["error"]
        conn.close()
        ov = http.get(f"/api/businesses/{bid}/overview").json()
        assert any("истёк" in n for n in ov["notices"])
        r = http.post(f"/api/businesses/{bid}/sync")
        assert r.status_code == 400 and "истёк" in r.json()["detail"]["message"]
