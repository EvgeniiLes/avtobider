"""Срезы рекламы: сбор отчётов Директа и Метрики по измерениям, повторная загрузка, показ и пометки «дорого» и «дёшево»."""
import json
import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.direct import slices
from app.engine.util import MICRO, day_add
from app.main import app
from app.routes import auth
from tests.fake_direct import FakeDirect
from tests.test_queries import launched, sync


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("FAL_KEY", "fal-test")
    auth._fails.clear()
    yield tmp_path / "test.db"
    app.dependency_overrides.clear()


TODAY = date.today().isoformat()
YESTERDAY = day_add(TODAY, -1)

DEVICES = [(TODAY, "MOBILE", 3000, 90, 900 * MICRO, 9), (YESTERDAY, "MOBILE", 2000, 60, 600 * MICRO, 6),
           (TODAY, "DESKTOP", 1000, 50, 1000 * MICRO, 2), (TODAY, "TABLET", 100, 25, 300 * MICRO, 0)]


def rows(conn, dimension):
    return {(r["key"], r["day"]): (r["impressions"], r["clicks"], r["spend_micro"], r["leads"])
            for r in conn.execute("SELECT * FROM dimension_stats WHERE dimension = ?", (dimension,))}


def test_every_dimension_is_requested_and_only_our_campaign_is_kept(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        fake.slice_rows = {"Device": DEVICES, "Gender": [(TODAY, "GENDER_FEMALE", 10, 1, MICRO, 1)]}
        fake.slice_foreign_rows = {"Device": [(TODAY, "MOBILE", 999, 99, 99 * MICRO, 9)]}
        assert sync(isolated, bid, fake)["error"] == ""
        asked = {r["FieldNames"][2] for r in fake.slice_requests}
        assert asked == set(slices.DIRECT_DIMENSIONS.values()) and all(r["ReportType"] == "CUSTOM_REPORT" and r["Goals"] == ["9001"] for r in fake.slice_requests)
        conn = connect(isolated)
        assert rows(conn, "device") == {("MOBILE", TODAY): (3000, 90, 900 * MICRO, 9), ("MOBILE", YESTERDAY): (2000, 60, 600 * MICRO, 6),
                                       ("DESKTOP", TODAY): (1000, 50, 1000 * MICRO, 2), ("TABLET", TODAY): (100, 25, 300 * MICRO, 0)}   # чужой кампании нет
        assert rows(conn, "gender") == {("GENDER_FEMALE", TODAY): (10, 1, MICRO, 1)}
        status = json.loads(conn.execute("SELECT slices_status FROM businesses WHERE id = ?", (bid,)).fetchone()[0])
        assert set(status.values()) == {"ok"} and conn.execute("SELECT slices_day FROM businesses WHERE id = ?", (bid,)).fetchone()[0] == TODAY
        conn.close()


def test_slices_load_once_a_day_and_a_reload_replaces_the_window_instead_of_doubling_it(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        fake.slice_rows = {"Device": DEVICES}
        sync(isolated, bid, fake)
        count = len(fake.slice_requests)
        sync(isolated, bid, fake)
        assert len(fake.slice_requests) == count                         # в тот же день отчёты не запрашиваем снова
        sync(isolated, bid, fake, today=day_add(TODAY, 1))
        assert len(fake.slice_requests) == 2 * count
        conn = connect(isolated)
        assert conn.execute("SELECT SUM(clicks) FROM dimension_stats WHERE dimension = 'device'").fetchone()[0] == 225   # те же дни не удвоились
        conn.close()


def test_one_failing_dimension_does_not_stop_the_rest_and_is_reported(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        fake.slice_fail = {"IncomeGrade"}
        fake.slice_rows = {"Device": DEVICES}
        fake.report_rows = [(TODAY, 0, 1000, 30, 450 * MICRO, 2)]
        assert sync(isolated, bid, fake)["error"] == ""
        conn = connect(isolated)
        assert len(rows(conn, "device")) == 4 and not rows(conn, "income")
        status = json.loads(conn.execute("SELECT slices_status FROM businesses WHERE id = ?", (bid,)).fetchone()[0])
        assert status["income"] != "ok" and status["device"] == "ok"
        assert conn.execute("SELECT SUM(clicks) FROM stats_daily").fetchone()[0] == 30    # основная синхронизация работает
        conn.close()
        data = http.get(f"/api/businesses/{bid}/slices").json()
        assert list(data["failed"]) == ["Доход"]


def test_hours_and_weekdays_come_from_metrika_visits_with_ad_traffic_only(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        fake.metrika_hours = [(9, 40, 4), (10, 60, 3), ("21", 30, 0)]
        fake.metrika_days = [("2026-11-02", 50, 5), ("2026-11-09", 30, 1), ("2026-11-03", 20, 0)]   # два понедельника и вторник
        sync(isolated, bid, fake)
        asked = [r for r in fake.metrika_stat_requests if r.get("dimensions") in ("ym:s:hour", "ym:s:date")]   # запросы поведения по меткам (app/direct/behavior.py) здесь не в счёт
        assert {r["dimensions"] for r in asked} == {"ym:s:hour", "ym:s:date"}
        assert all(r["ids"] == "555" and "goal9001reaches" in r["metrics"] and "'ad'" in r["filters"] for r in asked)
        conn = connect(isolated)
        assert rows(conn, "hour") == {("09", ""): (0, 40, 0, 4), ("10", ""): (0, 60, 0, 3), ("21", ""): (0, 30, 0, 0)}
        assert rows(conn, "weekday") == {("1", ""): (0, 80, 0, 6), ("2", ""): (0, 20, 0, 0)}
        conn.close()
        data = http.get(f"/api/businesses/{bid}/slices").json()
        hours = next(d for d in data["dimensions"] if d["id"] == "hour")
        assert hours["visits"] is True and [r["label"] for r in hours["rows"]] == ["09:00", "10:00", "21:00"]
        days = next(d for d in data["dimensions"] if d["id"] == "weekday")
        assert [r["label"] for r in days["rows"]] == ["Понедельник", "Вторник"]


def test_a_failing_metrika_does_not_break_the_direct_slices(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        fake.metrika_slices_fail = True
        fake.slice_rows = {"Device": DEVICES}
        assert sync(isolated, bid, fake)["error"] == ""
        conn = connect(isolated)
        assert len(rows(conn, "device")) == 4 and not rows(conn, "hour")
        status = json.loads(conn.execute("SELECT slices_status FROM businesses WHERE id = ?", (bid,)).fetchone()[0])
        assert status["hour"] != "ok" and status["weekday"] != "ok"
        conn.close()


def test_page_labels_segments_and_marks_the_dear_cheap_and_empty_ones(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        assert http.get(f"/api/businesses/{bid}/slices").json()["dimensions"] == []
        fake.slice_rows = {"Device": DEVICES + [(TODAY, "--", 10, 1, MICRO)],
                           "Age": [(TODAY, "AGE_25_34", 100, 5, 5 * MICRO, 1), (TODAY, "AGE_45", 100, 5, 5 * MICRO, 0), (TODAY, "AGE_0_17", 5, 1, MICRO, 0)]}
        sync(isolated, bid, fake)
        data = http.get(f"/api/businesses/{bid}/slices").json()
        assert data["launched"] is True and data["has_goal"] is True and data["synced"] == TODAY
        device = next(d for d in data["dimensions"] if d["id"] == "device")
        by_label = {r["label"]: r for r in device["rows"]}
        assert [r["label"] for r in device["rows"]][:3] == ["Телефон", "Компьютер", "Планшет"]           # по расходу
        assert by_label["Телефон"]["clicks"] == 150 and by_label["Телефон"]["leads"] == 15 and by_label["Телефон"]["cost_per_lead"] == 100.0
        assert by_label["Телефон"]["flag"] == ""            # 100 ₽ при средней 165 ₽: не дороже и не настолько дешевле
        assert by_label["Компьютер"]["flag"] == "dear"      # 500 ₽ за обращение
        assert by_label["Планшет"]["flag"] == "none"        # 25 кликов и ни одного обращения
        assert by_label["Не определено"]["clicks"] == 1 and by_label["Не определено"]["flag"] == ""
        age = {r["label"]: r for r in next(d for d in data["dimensions"] if d["id"] == "age")["rows"]}
        assert set(age) == {"25–34", "45 и старше", "до 18"} and age["25–34"]["share"] == 45


def test_flags_follow_the_thresholds():
    average = 100 * MICRO
    seg = lambda clicks, spend, leads: {"clicks": clicks, "spend_micro": spend, "leads": leads}
    assert slices._flag(seg(5, 50 * MICRO, 0), average, True) == ""                  # мало кликов: рано судить
    assert slices._flag(seg(30, 500 * MICRO, 0), average, True) == "none"            # кликов хватает, обращений нет
    assert slices._flag(seg(30, 800 * MICRO, 4), average, True) == "dear"            # 200 ₽ при средней 100 ₽
    assert slices._flag(seg(30, 240 * MICRO, 4), average, True) == "cheap"           # 60 ₽
    assert slices._flag(seg(30, 240 * MICRO, 2), average, True) == ""                # дёшево, но обращений мало для вывода
    assert slices._flag(seg(30, 500 * MICRO, 0), average, False) == ""               # без цели «обращение» не судим


def test_labels():
    assert slices.label("age", "AGE_25_34") == "25–34" and slices.label("age", "AGE_45") == "45 и старше" and slices.label("age", "AGE_0_17") == "до 18"
    assert slices.label("gender", "GENDER_MALE") == "Мужчины" and slices.label("device", "WEIRD") == "WEIRD" and slices.label("slot", "--") == "Не определено"
    assert slices.label("hour", "07") == "07:00" and slices.label("weekday", "7") == "Воскресенье"


def test_without_a_goal_nothing_is_judged_and_no_leads_are_stored(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated, goal=False)
        fake.slice_rows = {"Device": DEVICES}
        sync(isolated, bid, fake)
        assert all("Goals" not in r for r in fake.slice_requests)
        device = next(d for d in http.get(f"/api/businesses/{bid}/slices").json()["dimensions"] if d["id"] == "device")
        assert all(r["leads"] == 0 and r["flag"] == "" for r in device["rows"])


def test_old_days_are_dropped_after_half_a_year(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        conn = connect(isolated)
        conn.execute("INSERT INTO dimension_stats (business_id, dimension, key, day, clicks) VALUES (?, 'device', 'MOBILE', ?, 7)", (bid, day_add(TODAY, -200)))
        conn.commit()
        conn.close()
        sync(isolated, bid, fake)
        conn = connect(isolated)
        assert conn.execute("SELECT COUNT(*) FROM dimension_stats WHERE day = ?", (day_add(TODAY, -200),)).fetchone()[0] == 0
        conn.close()


def test_the_page_is_private(isolated):
    with TestClient(app) as http:
        assert http.get("/api/businesses/1/slices").status_code in (401, 403)
