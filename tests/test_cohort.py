"""Объявления, запущенные вместе каждое в своей кампании: через три дня слабое уходит, лимит переходит лучшему.
Кнопка «Сравнить объявления в отдельных кампаниях» разносит объявления общей кампании по своим кампаниям."""
from datetime import date

from fastapi.testclient import TestClient

from app.db import connect
from app.direct.runner import sync_and_run
from app.engine.util import MICRO, day_add
from app.main import app
from tests.fake_direct import FakeDirect
from tests.test_launch import isolated, setup  # noqa: F401  (isolated: общая подготовка базы для каждого теста)

TODAY = date.today().isoformat()


def launched(http, fake, db_path, separate, ads=2):
    bid = setup(http, fake, db_path)
    assert http.put(f"/api/businesses/{bid}/creatives/ads/live", json={"count": ads}).status_code == 200
    body = {"marking_confirmed": True, **({"separate": True} if separate else {})}
    assert http.post(f"/api/businesses/{bid}/launch", json=body).status_code == 200
    fake.accept_all(state="ON", payment="ALLOWED")
    return bid, connect(db_path)


def run(conn, bid, fake, day):
    biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
    result = sync_and_run(conn, biz, day, transport=fake.transport)
    assert result["error"] == ""


def rows(until, days, shown):
    """Показы, клики и расход по дням: shown = {номер объявления: клики в день}; обращений нет, цель не измеряется."""
    return [(day_add(until, -d), ad, 1000, clicks, 100 * MICRO, 0) for ad, clicks in shown.items() for d in range(days)]


def test_the_weaker_of_ads_launched_together_stops_after_three_days_and_its_limit_goes_to_the_better(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, conn = launched(http, fake, isolated, separate=True)
        assert len(fake.campaigns) == 2
        limits = {c["Limit"] for c in fake.campaigns.values()}
        fake.report_rows = rows(day_add(TODAY, 3), 4, {0: 40, 1: 12})
        run(conn, bid, fake, day_add(TODAY, 2))                    # два дня: рано
        assert conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 0
        run(conn, bid, fake, day_add(TODAY, 4))                    # прошло больше трёх дней, данные до вчерашнего
        done = conn.execute("SELECT * FROM changes ORDER BY id").fetchall()
        assert [(c["action"], c["reason_code"]) for c in done] == [("pause", "cohort_loser"), ("set_budget", "cohort_winner")]
        assert "CTR" in done[0]["reason_text"] and "кликам" in done[0]["reason_text"]       # обращений нет: сравнивали по кликам, и это сказано
        assert sorted(a["suspended"] for a in fake.ads.values()) == [False, True]
        weekly = sorted(r["weekly_micro"] for r in conn.execute("SELECT weekly_micro FROM campaigns"))
        assert weekly[1] > weekly[0] and weekly[1] <= 2 * max(limits)
        run(conn, bid, fake, day_add(TODAY, 6))
        assert conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 2        # победитель остался один: сравнивать больше не с кем
        conn.close()


def test_a_draw_waits_for_a_week_and_then_keeps_the_better_by_numbers(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, conn = launched(http, fake, isolated, separate=True)
        fake.report_rows = rows(day_add(TODAY, 3), 4, {0: 21, 1: 20})
        run(conn, bid, fake, day_add(TODAY, 4))
        assert conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 0        # разницы нет: ждём
        run(conn, bid, fake, day_add(TODAY, 9))
        assert conn.execute("SELECT reason_code FROM changes ORDER BY id").fetchone()[0] == "cohort_loser"   # неделя прошла: оставляем лучшее по цифрам
        conn.close()


def test_ads_sharing_one_campaign_are_not_compared_this_way(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, conn = launched(http, fake, isolated, separate=False, ads=3)
        fake.report_rows = rows(day_add(TODAY, 3), 4, {0: 40, 1: 12, 2: 11})
        run(conn, bid, fake, day_add(TODAY, 4))
        assert conn.execute("SELECT COUNT(*) FROM changes WHERE reason_code = 'cohort_loser'").fetchone()[0] == 0
        conn.close()


def test_split_moves_each_ad_of_a_shared_campaign_into_its_own_campaign(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, conn = launched(http, fake, isolated, separate=False, ads=3)
        assert len(fake.campaigns) == 1
        old_limit = next(iter(fake.campaigns.values()))["Limit"]
        old_row = conn.execute("SELECT id FROM campaigns").fetchone()[0]
        run(conn, bid, fake, TODAY)                                                                       # Яндекс принял объявления: они работают
        ov = http.get(f"/api/businesses/{bid}/overview").json()
        assert next(g for g in ov["campaigns"] if g["id"] == old_row)["splittable"] is True
        r = http.post(f"/api/businesses/{bid}/launch/split", json={"campaign_id": old_row})
        assert r.status_code == 200 and r.json()["created"] == 3, r.text
        assert len(fake.campaigns) == 4 and len(fake.ads) == 6
        new = [c for i, c in fake.campaigns.items() if c["Limit"] != old_limit]
        assert len(new) == 3 and {c["Limit"] for c in new} == {old_limit // 3}
        assert sum(1 for a in fake.ads.values() if a["suspended"]) == 3                                   # старые остановлены
        states = {r["state"] for r in conn.execute("SELECT state FROM campaigns WHERE id = ?", (old_row,))}
        assert states == {"paused"} and conn.execute("SELECT COUNT(*) FROM campaigns WHERE state != 'paused'").fetchone()[0] == 3
        assert conn.execute("SELECT separate_campaigns FROM businesses").fetchone()[0] == 1
        assert http.post(f"/api/businesses/{bid}/launch/split", json={"campaign_id": old_row}).status_code == 400   # старая уже пуста: разносить нечего
        conn.close()
