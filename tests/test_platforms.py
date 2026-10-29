"""VK Реклама и Авито: клиенты, платформы для движка, подключение кабинетов по ключам (все запросы подменены)."""
import asyncio
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from app.deps import platform_transport
from app.engine.util import EngineError
from app.main import app
from app.platforms.avito import AvitoClient, AvitoError, AvitoPlatform
from app.platforms.vk import VkAdsClient, VkError, VkPlatform
from app.routes import auth


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    auth._fails.clear()
    yield tmp_path / "test.db"
    app.dependency_overrides.clear()


def vk_handler(log, secret_ok=True):
    def handler(request: httpx.Request) -> httpx.Response:
        log.append((request.method, request.url.path, request.headers.get("authorization", ""), request.content))
        path = request.url.path
        if path == "/api/v2/oauth2/token.json":
            if b"client_secret=good" not in request.content:
                return httpx.Response(401, json={"error": "bad"})
            return httpx.Response(200, json={"access_token": "tok", "refresh_token": "r", "expires_in": 86400})
        if path == "/api/v2/user.json":
            return httpx.Response(200, json={"id": 5, "username": "shop"})
        if path == "/api/v2/ad_plans.json" and request.method == "GET":
            return httpx.Response(200, json={"items": [{"id": 11, "name": "Осень", "status": "active", "objective": "traffic", "budget_limit_day": "500"}]})
        if path == "/api/v2/ad_plans.json" and request.method == "POST":
            return httpx.Response(200, json={"id": 77, "ad_groups": [{"id": 88}]})
        if path.startswith("/api/v2/ad_plans/") and request.method == "POST":
            return httpx.Response(200, json={})
        if path == "/api/v2/content/static.json":
            return httpx.Response(200, json={"id": 4242, "variants": {}})
        if path == "/api/v2/statistics/ad_plans/day.json":
            return httpx.Response(200, json={"items": [{"id": 11, "rows": [{"date": "2026-10-28", "base": {"shows": "1200", "clicks": "30", "spent": "450.5"}}]}]})
        return httpx.Response(404)
    return handler


def avito_handler(log):
    def handler(request: httpx.Request) -> httpx.Response:
        log.append((request.method, request.url.path, request.headers.get("authorization", ""), request.content))
        path = request.url.path
        if path == "/token":
            if b"client_secret=good" not in request.content:
                return httpx.Response(401, json={"error": "bad"})
            return httpx.Response(200, json={"access_token": "atok", "expires_in": 3600, "token_type": "Bearer"})
        if path == "/core/v1/accounts/self":
            return httpx.Response(200, json={"id": 901, "name": "Иван"})
        if path == "/core/v1/items":
            return httpx.Response(200, json={"resources": [{"id": 1001, "title": "Груминг", "status": "active", "price": 1500, "category": {"name": "Услуги"}}]})
        if path == "/stats/v1/accounts/901/items":
            return httpx.Response(200, json={"result": {"items": [{"itemId": 1001, "stats": [{"date": "2026-10-28", "uniqViews": 80, "uniqContacts": 4}]}]}})
        if path in ("/cpxpromo/1/setManual", "/cpxpromo/1/remove"):
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404)
    return handler


def run(coro):
    return asyncio.run(coro)


def test_vk_client_connects_lists_uploads_creates_and_reads_stats():
    log = []

    async def go():
        async with VkAdsClient("cid", "good", httpx.MockTransport(vk_handler(log))) as c:
            assert (await c.check())["id"] == 5
            campaigns = await c.list_campaigns()
            image = await c.upload_image(b"\xff\xd8jpeg")
            created = await c.create_campaign({"name": "Тест", "ad_groups": []})
            await c.set_status("11", False)
            await c.set_daily_budget("11", 300)
            stats = await c.day_stats(["11"], "2026-10-28", "2026-10-28")
        return campaigns, image, created, stats

    campaigns, image, created, stats = run(go())
    assert campaigns == [{"id": "11", "name": "Осень", "status": "active", "kind": "traffic", "budget": "500"}]
    assert image == 4242 and created["id"] == 77
    assert stats == [{"campaign_id": "11", "day": "2026-10-28", "impressions": 1200, "clicks": 30, "spent": 450.5}]
    assert sum(1 for m, p, *_ in log if p == "/api/v2/oauth2/token.json") == 1       # токен берётся один раз
    assert all(a == "Bearer tok" for _, p, a, _ in log if "oauth2" not in p)
    status_call = next(b for m, p, a, b in log if p == "/api/v2/ad_plans/11.json" and b'"status"' in b)
    assert json.loads(status_call) == {"status": "blocked"}                           # пауза = blocked
    budget_call = next(b for m, p, a, b in log if p == "/api/v2/ad_plans/11.json" and b"budget" in b)
    assert json.loads(budget_call) == {"budget_limit_day": "300"}


def test_vk_wrong_keys_give_a_plain_message():
    async def go():
        async with VkAdsClient("cid", "bad", httpx.MockTransport(vk_handler([]))) as c:
            await c.check()

    with pytest.raises(VkError) as e:
        run(go())
    assert "client_secret" in e.value.message and e.value.code == "auth"


def test_avito_client_connects_lists_reads_stats_and_sets_bid():
    log = []

    async def go():
        async with AvitoClient("cid", "good", httpx.MockTransport(avito_handler(log))) as c:
            me = await c.check()
            items = await c.list_campaigns()
            stats = await c.day_stats(["1001"], "2026-10-28", "2026-10-28")
            await c.set_bid("1001", 12.5, 90)
            await c.remove_promotion("1001")
        return me, items, stats

    me, items, stats = run(go())
    assert me["id"] == 901 and items[0]["name"] == "Груминг" and items[0]["kind"] == "Услуги"
    assert stats == [{"campaign_id": "1001", "day": "2026-10-28", "impressions": 80, "clicks": 4, "spent": 0.0}]
    bid = json.loads(next(b for m, p, a, b in log if p == "/cpxpromo/1/setManual"))
    assert bid["itemID"] == 1001 and bid["bidPenny"] == 1250 and bid["limitPenny"] == 9000       # рубли в копейки


def test_avito_wrong_keys():
    async def go():
        async with AvitoClient("cid", "bad", httpx.MockTransport(avito_handler([]))) as c:
            await c.check()

    with pytest.raises(AvitoError) as e:
        run(go())
    assert e.value.code == "auth"


def _unit_conn(tmp_path):
    conn = sqlite3.connect(tmp_path / "u.db")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE ad_units (id INTEGER PRIMARY KEY, external_campaign_id TEXT, state TEXT, budget_micro INTEGER, last_changed_day TEXT);
        INSERT INTO ad_units VALUES (1, '11', 'active', 700000000, NULL);
        INSERT INTO ad_units VALUES (2, NULL, 'active', 0, NULL);
    """)
    return conn


def test_vk_platform_pauses_sets_daily_budget_and_refuses_what_is_not_ready(tmp_path):
    log = []
    conn = _unit_conn(tmp_path)
    p = VkPlatform(conn, "cid", "good", httpx.MockTransport(vk_handler(log)))
    p.set_state(1, "paused", "2026-10-29")
    p.set_budget(1, 700_000_000, "2026-10-29")          # 700 ₽ в неделю = 100 ₽ в день
    assert json.loads(next(b for m, pa, a, b in log if b"budget_limit_day" in b)) == {"budget_limit_day": "100"}
    assert conn.execute("SELECT state FROM ad_units WHERE id = 1").fetchone()["state"] == "paused"
    with pytest.raises(EngineError):
        p.set_state(2, "paused", "2026-10-29")           # не создано на площадке
    with pytest.raises(EngineError) as e:
        p.create_variant(1, "2026-10-29")
    assert e.value.code == "not_ready"


def test_avito_platform_translates_weekly_budget_to_a_bid(tmp_path):
    log = []
    conn = _unit_conn(tmp_path)
    p = AvitoPlatform(conn, "cid", "good", httpx.MockTransport(avito_handler(log)))
    p.set_budget(1, 700_000_000, "2026-10-29")
    bid = json.loads(next(b for m, pa, a, b in log if pa == "/cpxpromo/1/setManual"))
    assert bid["limitPenny"] == 10000 and 0 < bid["bidPenny"] <= 10000
    p.set_state(1, "paused", "2026-10-29")
    assert any(pa == "/cpxpromo/1/remove" for m, pa, a, b in log)
    with pytest.raises(EngineError):
        p.create_variant(1, "2026-10-29")


def test_connecting_vk_and_avito_accounts_keeps_keys_secret_and_does_not_confuse_direct():
    log = []
    routes = {"vk": vk_handler(log), "avito": avito_handler(log)}

    def handler(request: httpx.Request) -> httpx.Response:
        return (routes["avito"] if request.url.host == "api.avito.ru" else routes["vk"])(request)

    app.dependency_overrides[platform_transport] = lambda: httpx.MockTransport(handler)
    with TestClient(app) as http:
        http.post("/api/auth/register", json={"email": "p@example.com", "password": "password123"})
        listing = http.get("/api/accounts/platforms").json()["platforms"]
        assert [p["id"] for p in listing] == ["yandex_direct", "vk_ads", "avito"] and listing[1]["keys"] and listing[1]["where"]

        assert http.post("/api/accounts/keys", json={"platform": "yandex_direct", "client_id": "a", "client_secret": "b"}).status_code == 400
        wrong = http.post("/api/accounts/keys", json={"platform": "vk_ads", "client_id": "cid", "client_secret": "bad"})
        assert wrong.status_code == 502 and "client_secret" in wrong.json()["detail"]["message"]
        assert http.get("/api/accounts").json()["accounts"] == []                     # с неверными ключами ничего не сохраняется

        vk = http.post("/api/accounts/keys", json={"platform": "vk_ads", "client_id": "cid", "client_secret": "good", "title": "Магазин"}).json()
        assert vk["account"]["platform"] == "VK Реклама" and vk["campaigns_count"] == 1
        av = http.post("/api/accounts/keys", json={"platform": "avito", "client_id": "cid", "client_secret": "good"}).json()
        assert av["account"]["platform"] == "Авито" and av["account"]["title"].startswith("Мой кабинет")
        accounts = http.get("/api/accounts").json()["accounts"]
        assert {a["platform_id"] for a in accounts} == {"vk_ads", "avito"} and "good" not in json.dumps(accounts)

        shown = http.get(f"/api/accounts/{vk['account']['id']}/campaigns").json()
        assert shown["campaigns"][0]["name"] == "Осень" and shown["account"]["platform"] == "VK Реклама"
        assert http.get(f"/api/accounts/{av['account']['id']}/campaigns").json()["campaigns"][0]["name"] == "Груминг"

    conn = sqlite3.connect(__import__("os").environ["DB_PATH"])
    stored = conn.execute("SELECT token_enc FROM ad_accounts").fetchall()
    assert all("good" not in row[0] for row in stored)                              # в базе ключи только зашифрованными
