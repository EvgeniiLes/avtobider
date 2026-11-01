"""Заявки с формы на сайте клиента: приём, защита от спама, доставка владельцу в Telegram."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routes import auth
from app.routes.leads import origin_allowed
from tests.fake_direct import FakeDirect
from tests.test_metrika import setup
from tests.test_notify import FakeTelegram, configure

ORIGIN = "https://salon.ru"
FORM = {"name": "Иван", "contact": "@ivan", "task": "Менеджеры тратят полдня на отчёты"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    for name in ("FAL_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_NAME", "TELEGRAM_API_BASE"):
        monkeypatch.delenv(name, raising=False)
    auth._fails.clear()
    yield tmp_path / "test.db"
    app.dependency_overrides.clear()


def rows(db_path, sql, *args):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def connected_owner(http, db_path, monkeypatch, linked=True):
    bid = setup(http, FakeDirect(), db_path)
    if linked:
        configure(monkeypatch)
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE users SET telegram_chat = '777'")
        conn.commit()
        conn.close()
    page = http.get(f"/api/businesses/{bid}/leads").json()
    return bid, page


def test_origin_must_be_the_site_itself_with_or_without_www():
    assert origin_allowed("https://salon.ru", "https://salon.ru") and origin_allowed("salon.ru", "https://www.salon.ru")
    assert origin_allowed("https://www.salon.ru", "https://salon.ru")
    assert not origin_allowed("https://salon.ru", "https://evil.example") and not origin_allowed("https://salon.ru", "https://salon.ru.evil.example")
    assert not origin_allowed("https://salon.ru", None) and not origin_allowed("", "https://salon.ru")


def test_a_lead_from_the_site_is_saved_and_reaches_the_owner_in_telegram(isolated, monkeypatch):
    tg = FakeTelegram()
    with TestClient(app) as http:
        bid, page = connected_owner(http, isolated, monkeypatch)
        endpoint = page["endpoint"]
        assert endpoint.endswith("/api/leads/" + endpoint.rsplit("/", 1)[1]) and page["telegram"] is True and endpoint in page["snippet"]
        key = endpoint.rsplit("/", 1)[1]

        r = http.post(f"/api/leads/{key}", data={**FORM, "page": "https://salon.ru/#contact"}, headers={"Origin": ORIGIN})
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert r.headers["access-control-allow-origin"] == ORIGIN             # браузер сайта сможет прочитать ответ
        saved = rows(isolated, "SELECT * FROM leads")
        assert len(saved) == 1 and saved[0]["contact"] == "@ivan" and saved[0]["delivered"] == 1
        chat, text = tg.sent[0]
        assert chat == "777" and "Новая заявка с сайта «Лотос»" in text and "Контакт: @ivan" in text and "Менеджеры тратят" in text
        assert http.get(f"/api/businesses/{bid}/leads").json()["leads"][0]["name"] == "Иван"


def test_a_lead_is_kept_even_when_telegram_is_not_connected_or_unreachable(isolated, monkeypatch):
    tg = FakeTelegram()
    with TestClient(app) as http:
        bid, page = connected_owner(http, isolated, monkeypatch, linked=False)
        key = page["endpoint"].rsplit("/", 1)[1]
        assert page["telegram"] is False
        assert http.post(f"/api/leads/{key}", data=FORM, headers={"Origin": ORIGIN}).status_code == 200
        assert rows(isolated, "SELECT delivered FROM leads")[0]["delivered"] == 0 and tg.sent == []

        configure(monkeypatch)
        conn = sqlite3.connect(isolated)
        conn.execute("UPDATE users SET telegram_chat = '777'")
        conn.commit()
        conn.close()
        tg.down = True
        assert http.post(f"/api/leads/{key}", data=FORM, headers={"Origin": ORIGIN}).status_code == 200   # сайту всё равно «принято»
        assert [r["delivered"] for r in rows(isolated, "SELECT delivered FROM leads ORDER BY id")] == [0, 0]


def test_spam_and_foreign_pages_are_turned_away(isolated, monkeypatch):
    FakeTelegram()
    with TestClient(app) as http:
        _, page = connected_owner(http, isolated, monkeypatch)
        key = page["endpoint"].rsplit("/", 1)[1]
        url = f"/api/leads/{key}"
        assert http.post(url, data=FORM, headers={"Origin": "https://evil.example"}).status_code == 403
        assert http.post("/api/leads/no-such-key", data=FORM, headers={"Origin": ORIGIN}).status_code == 404
        assert http.post(url, data={**FORM, "website": "http://spam"}, headers={"Origin": ORIGIN}).json() == {"ok": True}   # ловушка: молча «принято»
        assert http.post(url, data={"name": "Иван", "contact": "  "}, headers={"Origin": ORIGIN}).status_code == 422
        assert http.post(url, content=b"x" * 30_000, headers={"Origin": ORIGIN}).status_code == 413
        assert rows(isolated, "SELECT COUNT(*) AS n FROM leads")[0]["n"] == 0

        long = http.post(url, json={"name": "я" * 500, "contact": "@x" * 200, "task": "т" * 5000}, headers={"Origin": ORIGIN})
        assert long.status_code == 200
        saved = rows(isolated, "SELECT * FROM leads")[0]
        assert len(saved["name"]) == 100 and len(saved["contact"]) == 150 and len(saved["task"]) == 2000      # поля обрезаются


def test_too_many_leads_from_one_address_are_limited(isolated, monkeypatch):
    FakeTelegram()
    with TestClient(app) as http:
        _, page = connected_owner(http, isolated, monkeypatch)
        url = "/api/leads/" + page["endpoint"].rsplit("/", 1)[1]
        codes = [http.post(url, data=FORM, headers={"Origin": ORIGIN}).status_code for _ in range(7)]
        assert codes == [200] * 5 + [429] * 2


def test_preflight_is_answered_only_for_the_sites_own_pages(isolated, monkeypatch):
    FakeTelegram()
    with TestClient(app) as http:
        _, page = connected_owner(http, isolated, monkeypatch)
        url = "/api/leads/" + page["endpoint"].rsplit("/", 1)[1]
        ok = http.options(url, headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"})
        assert ok.status_code == 204 and ok.headers["access-control-allow-origin"] == ORIGIN and "POST" in ok.headers["access-control-allow-methods"]
        bad = http.options(url, headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
        assert "access-control-allow-origin" not in bad.headers


def test_a_new_key_kills_the_old_address(isolated, monkeypatch):
    FakeTelegram()
    with TestClient(app) as http:
        bid, page = connected_owner(http, isolated, monkeypatch)
        old = page["endpoint"]
        fresh = http.post(f"/api/businesses/{bid}/leads/key").json()["endpoint"]
        assert fresh != old
        assert http.post(old.replace("http://testserver", ""), data=FORM, headers={"Origin": ORIGIN}).status_code == 404
        assert http.post(fresh.replace("http://testserver", ""), data=FORM, headers={"Origin": ORIGIN}).status_code == 200
