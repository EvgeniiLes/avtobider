"""Движок на настоящем Директе: одна кампания, объявления внутри делят бюджет.

Яндекс учится на данных кампании, поэтому правила мягкие: две недели после запуска ничего не решаем, потом правим кампанию
не чаще раза в неделю и не больше чем на 10%. Кампания работает на «Максимум кликов», пока обращений мало, и переходит на
«Максимум конверсий», когда за неделю набирается 10 обращений."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

import app.direct.client as client_module
from app.db import connect
from app.direct.runner import sync_and_run
from app.engine.util import MICRO, day_add
from app.main import app
from tests.fake_direct import FakeDirect
from tests.test_launch import BUDGET, isolated, setup  # noqa: F401  (isolated: общая подготовка базы для каждого теста)

TODAY = date.today().isoformat()
LATER = day_add(TODAY, 20)      # разогрев (14 дней) давно прошёл
CAP = int(BUDGET * MICRO * 0.9)  # столько можно раздать кампании: 10% бюджета недели в запасе


def prepare(http, fake, db_path, cap=True, **business):
    """Запущенная реклама: Яндекс одобрил, счёт пополнен, Метрика подключена.
    cap: недельный лимит кампании уже равен потолку, так что бюджет не растёт и тесты про другое не мешают друг другу."""
    bid = setup(http, fake, db_path, **business)
    assert http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True}).status_code == 200
    fake.accept_all(state="ON", payment="ALLOWED")
    conn = connect(db_path)
    conn.execute("UPDATE businesses SET metrika_counter_id = '42', metrika_goal_id = '7' WHERE id = ?", (bid,))
    if cap:
        conn.execute("UPDATE campaigns SET weekly_micro = ?", (CAP,))
    conn.commit()
    return bid, conn


def run(conn, bid, fake, day):
    biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
    result = sync_and_run(conn, biz, day, transport=fake.transport)
    assert result["error"] == ""
    return result


def report(until, days, ads=(0,), clicks=20, cost=200, leads=2):
    """Строки отчёта за days дней до даты until включительно: показы, клики, расход (₽) и обращения по каждому объявлению."""
    return [(day_add(until, -d), a, 1000, clicks, cost * MICRO, leads) for a in ads for d in range(days)]


def changes(conn):
    return conn.execute("SELECT * FROM changes ORDER BY id").fetchall()


def campaign(fake):
    return next(iter(fake.campaigns.values()))


def test_nothing_is_decided_during_the_first_two_weeks(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, conn = prepare(http, fake, isolated)
        fake.report_rows = report(day_add(TODAY, 5), 6, ads=(0, 1, 2), clicks=20, cost=600, leads=0)   # плохо: расход есть, обращений нет
        for d in (5, 6, 7):
            run(conn, bid, fake, day_add(TODAY, d))
        assert changes(conn) == [] and ("ads", "suspend") not in fake.calls
        assert not any(a["suspended"] for a in fake.ads.values()) and campaign(fake)["Strategy"] == "WB_MAXIMUM_CLICKS"
        conn.close()


def test_campaign_moves_to_max_conversions_only_when_a_week_brings_ten_leads(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, conn = prepare(http, fake, isolated)
        fake.report_rows = report(day_add(LATER, -1), 3)                    # 6 обращений за неделю: рано
        run(conn, bid, fake, LATER)
        assert campaign(fake)["Strategy"] == "WB_MAXIMUM_CLICKS" and changes(conn) == []

        fake.report_rows = report(day_add(LATER, -1), 7, ads=(0, 1))        # 28 обращений за неделю
        run(conn, bid, fake, day_add(LATER, 1))
        direct = campaign(fake)
        assert direct["Strategy"] == "WB_MAXIMUM_CONVERSION_RATE" and direct["GoalId"] == 7   # цель Метрики из настроек бизнеса
        assert direct["Limit"] == CAP                                       # бюджет недели остался прежним
        row = conn.execute("SELECT * FROM campaigns").fetchone()
        assert row["strategy"] == "conversions" and row["last_changed_day"] == day_add(LATER, 1)
        (ch,) = changes(conn)
        assert ch["action"] == "set_strategy" and ch["campaign_id"] == row["id"] and ch["unit_id"] is None and ch["status"] == "applied"
        assert "Максимум конверсий" in ch["reason_text"] and "обращения" in ch["reason_text"]

        run(conn, bid, fake, day_add(LATER, 4))                              # неделя ещё не прошла: кампанию не трогаем
        assert len(changes(conn)) == 1
        conn.close()


def test_without_a_goal_in_metrika_the_strategy_never_changes(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, conn = prepare(http, fake, isolated)
        conn.execute("UPDATE businesses SET metrika_goal_id = '', metrika_counter_id = ''")
        conn.commit()
        fake.report_rows = report(day_add(LATER, -1), 7, ads=(0, 1))
        run(conn, bid, fake, LATER)
        assert campaign(fake)["Strategy"] == "WB_MAXIMUM_CLICKS" and changes(conn) == []
        conn.close()


def test_a_good_campaign_gets_ten_percent_more_budget_at_most_once_a_week(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, conn = prepare(http, fake, isolated, cap=False)
        conn.execute("UPDATE campaigns SET strategy = 'conversions'")       # уже на конверсиях: остаётся только бюджет
        conn.commit()
        before = conn.execute("SELECT weekly_micro FROM campaigns").fetchone()[0]
        fake.report_rows = report(day_add(LATER, 8), 24, ads=(0, 1))
        run(conn, bid, fake, LATER)
        raised = int(before * 1.10)
        assert campaign(fake)["Limit"] == raised and conn.execute("SELECT weekly_micro FROM campaigns").fetchone()[0] == raised
        (ch,) = changes(conn)
        assert ch["action"] == "set_budget" and ch["campaign_id"] and "10%" in ch["reason_text"] and "Бюджет на неделю" in ch["reason_text"]

        run(conn, bid, fake, day_add(LATER, 3))
        assert len(changes(conn)) == 1 and campaign(fake)["Limit"] == raised   # правим не чаще раза в неделю

        run(conn, bid, fake, day_add(LATER, 7))
        assert len(changes(conn)) == 2 and campaign(fake)["Limit"] == int(raised * 1.10)
        conn.close()


def test_budget_never_grows_past_the_ninety_percent_cap(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, conn = prepare(http, fake, isolated, cap=False)
        conn.execute("UPDATE campaigns SET strategy = 'conversions', weekly_micro = ?", (CAP - 100 * MICRO,))
        conn.commit()
        fake.report_rows = report(day_add(LATER, 8), 24, ads=(0, 1))
        run(conn, bid, fake, LATER)
        assert campaign(fake)["Limit"] == CAP                                # +10% не поместилось, добавили сколько можно
        run(conn, bid, fake, day_add(LATER, 8))
        assert campaign(fake)["Limit"] == CAP and len(changes(conn)) == 1
        conn.close()


def test_weak_ads_are_stopped_but_the_campaign_keeps_running(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, conn = prepare(http, fake, isolated)
        conn.execute("UPDATE campaigns SET strategy = 'conversions'")
        conn.commit()
        good = report(day_add(LATER, 8), 24, ads=(0,))
        weak = report(day_add(LATER, 8), 24, ads=(1, 2), clicks=20, cost=600, leads=0)   # тратят, обращений нет
        fake.report_rows = good + weak
        run(conn, bid, fake, day_add(LATER, -1))                              # первая оценка: решение подтверждается второй
        assert changes(conn) == []
        run(conn, bid, fake, LATER)
        assert [a["suspended"] for a in fake.ads.values()] == [False, True, True] and campaign(fake)["State"] == "ON"
        assert [c["action"] for c in changes(conn)] == ["pause", "pause"]
        assert [r["state"] for r in conn.execute("SELECT state FROM ad_units ORDER BY id")] == ["active", "paused", "paused"]
        assert conn.execute("SELECT state FROM campaigns").fetchone()[0] == "active"
        conn.close()


def test_the_last_working_ad_is_never_stopped(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, conn = prepare(http, fake, isolated)
        conn.execute("UPDATE campaigns SET strategy = 'conversions'")
        conn.commit()
        fake.report_rows = report(day_add(LATER, 8), 24, ads=(0, 1, 2), clicks=20, cost=600, leads=0)   # все три слабые
        run(conn, bid, fake, day_add(LATER, -1))
        run(conn, bid, fake, LATER)
        assert sum(a["suspended"] for a in fake.ads.values()) == 2 and campaign(fake)["State"] == "ON"
        assert conn.execute("SELECT COUNT(*) FROM ad_units WHERE state = 'active'").fetchone()[0] == 1
        conn.close()


def test_in_ask_mode_the_switch_is_a_proposal_that_waits_for_the_owner(isolated, monkeypatch):
    fake = FakeDirect()
    monkeypatch.setattr(client_module, "DEFAULT_TRANSPORT", fake.transport)
    with TestClient(app) as http:
        bid, conn = prepare(http, fake, isolated, mode="ask")
        fake.report_rows = report(day_add(LATER, -1), 7, ads=(0, 1))
        run(conn, bid, fake, LATER)
        (ch,) = changes(conn)
        assert ch["status"] == "proposed" and ch["action"] == "set_strategy" and "Предлагаем перевести" in ch["reason_text"]
        assert campaign(fake)["Strategy"] == "WB_MAXIMUM_CLICKS"             # без подтверждения в Директе ничего не меняется
        run(conn, bid, fake, day_add(LATER, 1))
        assert len(changes(conn)) == 1                                       # повторное предложение не плодим

        shown = http.get(f"/api/businesses/{bid}/changes").json()["changes"][0]
        assert shown["unit"] == "Вся кампания" and shown["action"] == "Смена стратегии" and shown["can_decide"]

        assert http.post(f"/api/businesses/{bid}/changes/{ch['id']}/approve").status_code == 200
        assert campaign(fake)["Strategy"] == "WB_MAXIMUM_CONVERSION_RATE"
        assert conn.execute("SELECT strategy FROM campaigns").fetchone()[0] == "conversions"
        assert conn.execute("SELECT status FROM changes WHERE id = ?", (ch["id"],)).fetchone()[0] == "applied"

        assert http.post(f"/api/businesses/{bid}/changes/{ch['id']}/rollback", json={}).status_code == 200   # владелец передумал
        assert campaign(fake)["Strategy"] == "WB_MAXIMUM_CLICKS" and conn.execute("SELECT strategy FROM campaigns").fetchone()[0] == "clicks"
        conn.close()


def test_a_rejected_proposal_is_not_repeated_the_next_day(isolated, monkeypatch):
    fake = FakeDirect()
    monkeypatch.setattr(client_module, "DEFAULT_TRANSPORT", fake.transport)
    with TestClient(app) as http:
        bid, conn = prepare(http, fake, isolated, mode="ask")
        fake.report_rows = report(day_add(LATER, -1), 7, ads=(0, 1))
        run(conn, bid, fake, LATER)
        (ch,) = changes(conn)
        assert http.post(f"/api/businesses/{bid}/changes/{ch['id']}/reject").status_code == 200
        assert conn.execute("SELECT last_changed_day FROM campaigns").fetchone()[0] == TODAY   # отклонение запоминает день, неделю кампанию не трогаем
        conn.execute("UPDATE campaigns SET last_changed_day = ?", (LATER,))   # в тесте «сегодня» это LATER, а не настоящая дата
        conn.commit()
        run(conn, bid, fake, day_add(LATER, 1))
        assert len(changes(conn)) == 1 and campaign(fake)["Strategy"] == "WB_MAXIMUM_CLICKS"
        conn.close()


def test_overview_shows_how_close_the_campaign_is_to_conversions(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, conn = prepare(http, fake, isolated)
        fake.report_rows = report(TODAY, 3)
        run(conn, bid, fake, TODAY)
        info = http.get(f"/api/businesses/{bid}/overview").json()["campaign"]
        assert info["strategy"] == "clicks" and info["strategy_label"] == "Максимум кликов" and info["switch_at"] == 10
        assert info["leads_week"] == 6 and info["conversions"] is True and info["warmup_left"] == 14
        conn.close()


def test_a_stopped_business_is_never_resumed_by_the_engine(isolated, monkeypatch):
    fake = FakeDirect()
    monkeypatch.setattr(client_module, "DEFAULT_TRANSPORT", fake.transport)
    with TestClient(app) as http:
        bid, conn = prepare(http, fake, isolated)
        run(conn, bid, fake, TODAY)
        assert http.post(f"/api/businesses/{bid}/stop-all").json()["stopped"] == 3
        fake.report_rows = report(day_add(LATER, -1), 7, ads=(0, 1))
        run(conn, bid, fake, LATER)
        assert campaign(fake)["State"] == "SUSPENDED" and campaign(fake)["Strategy"] == "WB_MAXIMUM_CLICKS"   # на паузе кампанию не переключаем
        conn.close()
