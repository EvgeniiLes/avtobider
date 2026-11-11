"""Где показывать рекламу: онлайн-услуга начинает с поиска, местный бизнес остаётся с сетями; сеть без обращений предлагается к отключению."""
import asyncio
from datetime import date

from fastapi.testclient import TestClient

from app.db import _add_missing_columns, connect
from app.direct import placement, review, traffic
from app.direct.client import bidding_strategy
from app.engine.util import MICRO
from app.main import app
from tests.fake_direct import FakeDirect
from tests.test_cohort import run
from tests.test_launch import isolated, setup  # noqa: F401  (isolated: общая подготовка базы для каждого теста)

TODAY = date.today().isoformat()
ONLINE = {"nationwide": True, "city": "", "districts": "", "kind": "Автоматизация бизнеса"}


def launch(http, fake, db_path, **business):
    bid = setup(http, fake, db_path, **business)
    assert http.put(f"/api/businesses/{bid}/creatives/ads/live", json={"count": 2}).status_code == 200
    r = http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True})
    assert r.status_code == 200, r.text
    fake.accept_all(state="ON", payment="ALLOWED")
    return bid, connect(db_path)


def networks(fake):
    return {c["Network"] for c in fake.campaigns.values()}


def test_the_strategy_builder_can_leave_the_network_off():
    assert bidding_strategy("clicks", 1, network=False)["Network"] == {"BiddingStrategyType": "SERVING_OFF"}
    assert bidding_strategy("clicks", 1)["Network"] == {"BiddingStrategyType": "NETWORK_DEFAULT"}
    assert bidding_strategy("conversions", 1, "7", False)["Network"] == {"BiddingStrategyType": "SERVING_OFF"}


def test_an_online_service_starts_in_search_only_and_the_sync_does_not_turn_the_network_on(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, conn = launch(http, fake, isolated, **ONLINE)
        assert networks(fake) == {"SERVING_OFF"}
        run(conn, bid, fake, TODAY)
        assert networks(fake) == {"SERVING_OFF"}                                                    # синхронизация сети не включает
        shown = http.get(f"/api/businesses/{bid}/audience").json()["placement"]
        assert shown == {"mode": "", "network": False}
        conn.close()


def test_a_local_business_keeps_the_network(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, conn = launch(http, fake, isolated)
        assert networks(fake) == {"NETWORK_DEFAULT"}
        assert http.get(f"/api/businesses/{bid}/audience").json()["placement"] == {"mode": "", "network": True}
        conn.close()


def test_the_owner_switches_the_places_and_the_campaigns_follow(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, conn = launch(http, fake, isolated, **ONLINE)
        r = http.put(f"/api/businesses/{bid}/audience/placement", json={"mode": "all"})
        assert r.status_code == 200 and r.json()["placement"] == {"mode": "all", "network": True}
        assert networks(fake) == {"NETWORK_DEFAULT"}
        run(conn, bid, fake, TODAY)
        assert networks(fake) == {"NETWORK_DEFAULT"}                                                # выбор владельца держится и после синхронизации
        assert http.put(f"/api/businesses/{bid}/audience/placement", json={"mode": "search"}).json()["placement"]["network"] is False
        assert networks(fake) == {"SERVING_OFF"}
        assert http.put(f"/api/businesses/{bid}/audience/placement", json={"mode": "что-то"}).status_code == 422
        conn.close()


def test_ads_that_already_run_keep_the_network_when_the_setting_appears(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, conn = launch(http, fake, isolated, **ONLINE)
        other = setup(http, fake, isolated, name="Без запуска", **ONLINE)
        conn.execute("ALTER TABLE businesses DROP COLUMN placement")                                  # так выглядела база до появления настройки
        conn.commit()
        _add_missing_columns(conn)
        modes = {r["id"]: r["placement"] for r in conn.execute("SELECT id, placement FROM businesses")}
        assert modes[bid] == "all" and modes[other] == ""                                             # идущая реклама остаётся как была, новая начнёт с поиска
        conn.close()


def _rows(conn, bid, network, search, search_leads=0):
    for key, clicks, spend, leads in (("AD_NETWORK", *network, 0), ("SEARCH", *search, search_leads)):
        conn.execute("INSERT INTO dimension_stats (business_id, dimension, key, day, clicks, spend_micro, leads) VALUES (?, 'network', ?, ?, ?, ?, ?)",
                     (bid, key, TODAY, clicks, spend * MICRO, leads))
    conn.commit()


def test_a_network_that_spent_money_without_a_single_lead_is_proposed_for_switching_off(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, conn = launch(http, fake, isolated, **ONLINE)
        http.put(f"/api/businesses/{bid}/audience/placement", json={"mode": "all"})
        _rows(conn, bid, (1129, 2516), (10, 831))
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        (found,) = [p for p in review.proposals(conn, biz, TODAY) if p["kind"] == "network"]
        assert "1129" in found["text"].replace("\xa0", "") or "1 129" in found["text"].replace("\xa0", " ")
        assert "только поиск" in found["text"]

        async def go():
            async with fake.client() as client:
                return await review.apply_network(client, conn, biz, found["payload"])
        assert "только в поиске" in asyncio.run(go())
        assert networks(fake) == {"SERVING_OFF"}
        assert conn.execute("SELECT placement FROM businesses WHERE id = ?", (bid,)).fetchone()[0] == "search"
        again = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        assert [p for p in review.proposals(conn, again, TODAY) if p["kind"] == "network"] == []   # сети уже выключены: второй раз не предлагаем
        conn.close()


def test_no_proposal_when_the_network_brought_leads_or_spent_little(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, conn = launch(http, fake, isolated, **ONLINE)
        http.put(f"/api/businesses/{bid}/audience/placement", json={"mode": "all"})
        biz = lambda: conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        proposals = lambda: [p for p in review.proposals(conn, biz(), TODAY) if p["kind"] == "network"]
        _rows(conn, bid, (50, 100), (10, 80))
        assert proposals() == []                                                                    # потрачено мало и кликов мало
        conn.execute("DELETE FROM dimension_stats")
        conn.execute("INSERT INTO dimension_stats (business_id, dimension, key, day, clicks, spend_micro, leads) VALUES (?, 'network', 'AD_NETWORK', ?, 500, ?, 2)", (bid, TODAY, 2000 * MICRO))
        conn.commit()
        assert proposals() == []                                                                    # сеть приносит обращения: оставляем
        conn.close()


def test_the_traffic_ladder_opens_the_network_for_an_online_service_that_shows_nowhere(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, conn = launch(http, fake, isolated, **ONLINE)
        assert networks(fake) == {"SERVING_OFF"}
        biz = lambda: conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()

        async def step():
            async with fake.client() as client:
                await traffic._open_network(client, conn, biz(), {"summary": "x"}, {"step": 3, "day": "", "log": []}, TODAY)
        asyncio.run(step())
        assert networks(fake) == {"NETWORK_DEFAULT"} and biz()["placement"] == "all"
        conn.close()

        conn = connect(isolated)
        placement.save(conn, bid, "search")                                                         # режим выбрал владелец: ему сервис не перечит
        fake_before = networks(fake)
        asyncio.run(step())
        assert networks(fake) == fake_before
        conn.close()
