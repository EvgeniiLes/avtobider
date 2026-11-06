"""Сверка с кабинетом Директа и проверка расхода против недельного лимита."""
from datetime import date

from fastapi.testclient import TestClient

from app.db import connect
from app.direct.runner import sync_and_run
from app.engine.util import MICRO, day_add
from app.main import app
from tests.fake_direct import FakeDirect
from tests.test_launch import isolated, launched  # noqa: F401


def setup_stats(http, fake, db_path, monkeypatch, cost):
    import app.direct.client as client_module
    monkeypatch.setattr(client_module, "DEFAULT_TRANSPORT", fake.transport)
    bid = launched(http, fake, db_path)
    fake.accept_all(state="ON", payment="ALLOWED")
    yesterday = day_add(date.today().isoformat(), -1)
    fake.report_rows = [(yesterday, 0, 1000, 30, cost * MICRO)]
    conn = connect(db_path)
    sync_and_run(conn, conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone(), transport=fake.transport)
    conn.close()
    return bid


def test_stats_match_the_cabinet_and_budget_is_not_exceeded(isolated, monkeypatch):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup_stats(http, fake, isolated, monkeypatch, cost=100)
        r = http.post(f"/api/businesses/{bid}/reconcile")
        assert r.status_code == 200
        data = r.json()
        assert data["checked"] and data["stats_ok"] and data["budget_ok"] and "совпадают" in data["text"]


def test_impressions_are_shown_per_ad_per_campaign_and_in_total(isolated, monkeypatch):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup_stats(http, fake, isolated, monkeypatch, cost=100)
        data = http.get(f"/api/businesses/{bid}/overview").json()
        assert data["totals"]["impressions"] == 1000 and data["totals"]["clicks"] == 30
        assert sum(u["impressions"] for u in data["units"]) == 1000
        assert data["campaigns"][0]["impressions"] == 1000


def test_a_difference_between_service_and_cabinet_is_reported(isolated, monkeypatch):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup_stats(http, fake, isolated, monkeypatch, cost=100)
        conn = connect(isolated)
        conn.execute("UPDATE stats_daily SET spend_micro = spend_micro + ?", (500 * MICRO,))   # в сервисе записано больше, чем в Директе
        conn.commit()
        conn.close()
        data = http.post(f"/api/businesses/{bid}/reconcile").json()
        assert not data["stats_ok"] and data["mismatches"][0]["service"]["spend"] == 600.0 and data["mismatches"][0]["direct"]["spend"] == 100.0
        assert "расхождения" in data["text"]


def test_spend_above_the_weekly_limit_is_called_out(isolated, monkeypatch):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup_stats(http, fake, isolated, monkeypatch, cost=100_000)
        data = http.post(f"/api/businesses/{bid}/reconcile").json()
        assert not data["budget_ok"] and data["budget"][0]["over"] and "превысил" in data["text"]
