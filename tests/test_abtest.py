"""Сравнение текстов: сервис предлагает замену слабого объявления, спрашивает владельца, без ответа за сутки запускает сам, потом оставляет сильный."""
from datetime import date

from fastapi.testclient import TestClient

from app import reports
from app.db import connect
from app.direct import abtest
from app.direct.runner import sync_and_run
from app.engine.util import MICRO, day_add
from app.main import app
from tests.fake_direct import FakeDirect
from tests.test_launch import isolated, launched  # noqa: F401


def running_ads(http, fake, db_path, monkeypatch):
    import app.direct.client as client_module
    monkeypatch.setattr(client_module, "DEFAULT_TRANSPORT", fake.transport)
    bid = launched(http, fake, db_path)
    fake.accept_all(state="ON", payment="ALLOWED")
    today = date.today().isoformat()
    conn = connect(db_path)
    conn.execute("UPDATE businesses SET metrika_counter_id = '555', metrika_goal_id = '9001' WHERE id = ?", (bid,))
    conn.execute("UPDATE campaigns SET created_day = ?", (day_add(today, -12),))
    conn.execute("UPDATE ad_units SET last_changed_day = NULL")
    conn.commit()
    # объявление 0 приводит обращения, объявление 2 слабое: клики есть, обращений нет
    fake.report_rows = [(day_add(today, -d), i, 1000, clicks, 100 * MICRO, leads) for d in range(2, 8) for i, clicks, leads in ((0, 12, 2), (1, 8, 1), (2, 6, 0))]
    biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
    sync_and_run(conn, biz, today, transport=fake.transport)
    conn.close()
    return bid, today


def run(db_path, bid, fake, today):
    conn = connect(db_path)
    try:
        return sync_and_run(conn, conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone(), today, transport=fake.transport)
    finally:
        conn.close()


def test_a_replacement_is_proposed_and_started_by_the_service_after_a_day_of_silence(isolated, monkeypatch):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, today = running_ads(http, fake, isolated, monkeypatch)
        conn = connect(isolated)
        proposal = conn.execute("SELECT * FROM changes WHERE action = 'start_ab'").fetchone()
        assert proposal is not None and proposal["status"] == "proposed"
        weak = conn.execute("SELECT * FROM ad_units WHERE id = ?", (proposal["unit_id"],)).fetchone()
        old_title = weak["title"]
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        assert any(a["kind"] == "ab_proposed" for a in reports.alerts(conn, biz, today))      # в Telegram уходит предложение
        assert fake.ads[int(weak["external_ad_id"])]["ad"]["Title"] == old_title                # до ответа в кабинете прежний текст
        assert conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == 0              # пока владелец не ответил, в кабинете ничего не меняется
        run(isolated, bid, fake, today)
        assert conn.execute("SELECT status FROM changes WHERE id = ?", (proposal["id"],)).fetchone()[0] == "proposed"   # сутки ещё не прошли
        conn.execute("UPDATE changes SET created_at = datetime('now', '-2 days') WHERE id = ?", (proposal["id"],))
        conn.commit()
        run(isolated, bid, fake, today)
        assert conn.execute("SELECT status FROM changes WHERE id = ?", (proposal["id"],)).fetchone()[0] == "applied"
        ex = conn.execute("SELECT * FROM experiments").fetchone()
        assert ex["status"] == "running" and ex["unit_b"] == weak["id"]
        new = conn.execute("SELECT * FROM ad_units WHERE id = ?", (weak["id"],)).fetchone()
        assert new["title"] != old_title and new["variant_of"] == ex["unit_a"]
        assert fake.ads[int(weak["external_ad_id"])]["ad"]["Title"] == new["title"]               # новый текст ушёл в кабинет
        conn.close()


def test_after_a_week_the_stronger_text_stays_and_the_loser_is_stopped(isolated, monkeypatch):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, today = running_ads(http, fake, isolated, monkeypatch)
        conn = connect(isolated)
        proposal = conn.execute("SELECT * FROM changes WHERE action = 'start_ab'").fetchone()
        assert http.post(f"/api/businesses/{bid}/changes/{proposal['id']}/approve").status_code == 200
        ex = conn.execute("SELECT * FROM experiments").fetchone()
        fake.accept_all(state="ON", payment="ALLOWED")
        later = day_add(today, 9)
        # новый текст (объявление 2) собрал клики без обращений, лучшее (объявление 0) приводит обращения
        fake.report_rows = [(day_add(today, d), i, 1000, clicks, 100 * MICRO, leads) for d in range(1, 8) for i, clicks, leads in ((0, 15, 3), (2, 15, 0))]
        run(isolated, bid, fake, later)
        done = conn.execute("SELECT * FROM experiments WHERE id = ?", (ex["id"],)).fetchone()
        assert done["status"] == "done" and done["verdict"] == "a"
        assert conn.execute("SELECT state FROM ad_units WHERE id = ?", (ex["unit_b"],)).fetchone()[0] == "paused"
        assert conn.execute("SELECT state FROM ad_units WHERE id = ?", (ex["unit_a"],)).fetchone()[0] == "active"
        assert conn.execute("SELECT COUNT(*) FROM changes WHERE reason_code = 'ab_loser'").fetchone()[0] == 1
        conn.close()


def test_owner_can_approve_and_roll_back_the_replacement(isolated, monkeypatch):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, today = running_ads(http, fake, isolated, monkeypatch)
        conn = connect(isolated)
        proposal = conn.execute("SELECT * FROM changes WHERE action = 'start_ab'").fetchone()
        old = conn.execute("SELECT title FROM ad_units WHERE id = ?", (proposal["unit_id"],)).fetchone()[0]
        assert http.post(f"/api/businesses/{bid}/changes/{proposal['id']}/approve").status_code == 200
        assert conn.execute("SELECT title FROM ad_units WHERE id = ?", (proposal["unit_id"],)).fetchone()[0] != old
        assert http.post(f"/api/businesses/{bid}/changes/{proposal['id']}/rollback", json={}).status_code == 200
        assert conn.execute("SELECT title FROM ad_units WHERE id = ?", (proposal["unit_id"],)).fetchone()[0] == old
        assert conn.execute("SELECT status FROM experiments").fetchone()[0] == "cancelled"
        conn.close()


def test_rejected_proposal_is_not_repeated_for_a_week(isolated, monkeypatch):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, today = running_ads(http, fake, isolated, monkeypatch)
        conn = connect(isolated)
        proposal = conn.execute("SELECT * FROM changes WHERE action = 'start_ab'").fetchone()
        assert http.post(f"/api/businesses/{bid}/changes/{proposal['id']}/reject").status_code == 200
        run(isolated, bid, fake, today)
        assert conn.execute("SELECT COUNT(*) FROM changes WHERE action = 'start_ab'").fetchone()[0] == 1
        conn.close()


def test_no_test_while_there_is_too_little_data(isolated, monkeypatch):
    fake = FakeDirect()
    with TestClient(app) as http:
        import app.direct.client as client_module
        monkeypatch.setattr(client_module, "DEFAULT_TRANSPORT", fake.transport)
        bid = launched(http, fake, isolated)
        fake.accept_all(state="ON", payment="ALLOWED")
        conn = connect(isolated)
        conn.execute("UPDATE businesses SET metrika_counter_id = '555', metrika_goal_id = '9001' WHERE id = ?", (bid,))
        conn.commit()
        today = date.today().isoformat()
        fake.report_rows = [(day_add(today, -2), 0, 100, 3, 10 * MICRO, 1)]
        run(isolated, bid, fake, today)
        assert conn.execute("SELECT COUNT(*) FROM changes WHERE action = 'start_ab'").fetchone()[0] == 0
        assert abtest.pick_pair(conn, conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone(), today) is None
        conn.close()
