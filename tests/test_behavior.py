import json

import httpx
from fastapi.testclient import TestClient

from app.db import connect
from app.direct import behavior
from app.main import app
from tests.fake_direct import FakeDirect
from tests.test_launch import isolated, setup  # noqa: F401  (фикстура isolated нужна этому файлу)


def _metrika(rows_ads, rows_terms):
    def handler(request):
        dim = request.url.params["dimensions"]
        rows = rows_ads if dim == "ym:s:UTMContent" else rows_terms
        return httpx.Response(200, json={"data": [{"dimensions": [{"name": k}], "metrics": [v["visits"], v["bounce"], v["depth"], v["seconds"]]} for k, v in rows.items()]})
    return httpx.MockTransport(handler)


def test_behavior_is_fetched_by_the_link_tags_and_judged_in_plain_words():
    import asyncio
    ads = {"111": {"visits": 40, "bounce": 82.5, "depth": 1.1, "seconds": 9}, "222": {"visits": 60, "bounce": 30.0, "depth": 3.2, "seconds": 140},
           "{ad_id}": {"visits": 5, "bounce": 50, "depth": 1, "seconds": 20}, "333": {"visits": 4, "bounce": 100, "depth": 1, "seconds": 3}}
    terms = {"заказать груминг": {"visits": 25, "bounce": 48.0, "depth": 2, "seconds": 70}}

    async def go():
        async with httpx.AsyncClient(transport=_metrika(ads, terms)) as http:
            return await behavior.fetch_behavior(http, "tok", "555", "2026-10-01", "2026-10-30")
    data = asyncio.run(go())
    assert set(data["ads"]) == {"111", "222", "333"} and data["terms"]["заказать груминг"]["seconds"] == 70      # подстановка «{ad_id}» отброшена
    assert behavior.verdict(data["ads"]["111"])[0] == "bad" and behavior.verdict(data["ads"]["222"])[0] == "good"
    assert behavior.verdict(data["ads"]["333"])[0] == "waiting" and behavior.verdict(data["terms"]["заказать груминг"])[0] == "ok"


def test_behavior_is_stored_once_a_day_and_shown_with_ad_titles(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        assert http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True}).status_code == 200
        conn = connect(isolated)
        ad_id = conn.execute("SELECT external_ad_id, title FROM ad_units ORDER BY id").fetchone()
        conn.execute("UPDATE businesses SET metrika_counter_id = '555' WHERE id = ?", (bid,))
        conn.commit()
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        import asyncio
        ads = {str(ad_id["external_ad_id"]): {"visits": 50, "bounce": 75.0, "depth": 1.2, "seconds": 11}}
        assert asyncio.run(behavior.sync_behavior(conn, biz, "2026-11-05", "tok", _metrika(ads, {"заказать": {"visits": 30, "bounce": 20, "depth": 3, "seconds": 90}}))) == ""
        shown = http.get(f"/api/businesses/{bid}/slices/behavior").json()
        assert shown["has_counter"] and shown["synced"] == "2026-11-05"
        assert shown["ads"][0]["title"] == ad_id["title"] and shown["ads"][0]["verdict_code"] == "bad" and shown["terms"][0]["verdict_code"] == "good"
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        assert asyncio.run(behavior.sync_behavior(conn, biz, "2026-11-05", "tok", httpx.MockTransport(lambda r: httpx.Response(500)))) == ""   # второй раз в тот же день не спрашиваем
        conn.close()