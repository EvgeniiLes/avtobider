import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from app.deps import client_factory, yandex_http
from app.direct.client import DirectClient
from app.main import app
from app.routes import auth
from app.security import decrypt_token


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("YANDEX_CLIENT_ID", "api-client")
    monkeypatch.setenv("YANDEX_CLIENT_SECRET", "api-secret")
    auth._fails.clear()
    yield tmp_path / "test.db"
    app.dependency_overrides.clear()


def fake(token_response=None, login="salon-login", direct_error=None, code_status=200):
    """Заглушка Яндекса: выдача кода, обмен кода на токен, проверка кабинета в Директе."""
    seen = {"token_bodies": []}
    token_response = token_response if token_response is not None else (200, {"access_token": "direct-token-123", "refresh_token": "refresh-456", "expires_in": 31536000})

    def oauth(request):
        if request.url.path.endswith("/device/code"):
            seen["code_body"] = request.content.decode()
            if code_status != 200:
                return httpx.Response(code_status, json={})
            return httpx.Response(200, json={"device_code": "dev-code", "user_code": "abcd1234", "verification_url": "https://ya.ru/device",
                                             "interval": 5, "expires_in": 300})
        seen["token_bodies"].append(request.content.decode())
        status, body = token_response
        return httpx.Response(status, json=body)

    def direct(request):
        seen["auth"] = request.headers["Authorization"]
        if direct_error:
            return httpx.Response(200, json={"error": {"error_code": direct_error, "error_string": "x"}})
        return httpx.Response(200, json={"result": {"Clients": [{"Login": login}]}})

    app.dependency_overrides[yandex_http] = lambda: httpx.AsyncClient(transport=httpx.MockTransport(oauth))
    app.dependency_overrides[client_factory] = lambda: (lambda token, **kw: DirectClient(token, transport=httpx.MockTransport(direct), **kw))
    return seen


def login(http):
    creds = {"email": "owner@example.com", "password": "password123"}
    if http.post("/api/auth/register", json=creds).status_code == 409:
        http.post("/api/auth/login", json=creds)


def start(http):
    r = http.post("/api/direct/connect/start")
    assert r.status_code == 200, r.text
    return r.json()


def test_requires_login():
    with TestClient(app) as http:
        assert http.post("/api/direct/connect/start").status_code == 401
        assert http.post("/api/direct/connect/poll", json={"pending_id": 1}).status_code == 401


def test_config_flag_and_not_configured(monkeypatch):
    with TestClient(app) as http:
        assert http.get("/api/config").json()["direct_oauth"] is True
        monkeypatch.setenv("YANDEX_CLIENT_SECRET", "")
        assert http.get("/api/config").json()["direct_oauth"] is False
        login(http)
        r = http.post("/api/direct/connect/start")
        assert r.status_code == 503 and "ещё не настроено" in r.json()["detail"]["message"]


def test_start_gives_a_short_code_for_the_user():
    seen = fake()
    with TestClient(app) as http:
        login(http)
        data = start(http)
        assert data["user_code"] == "ABCD1234" and data["url"] == "https://ya.ru/device" and data["interval"] == 5
        assert "client_id=api-client" in seen["code_body"] and "api-secret" not in seen["code_body"]


def test_polling_waits_then_connects_with_encrypted_token(isolated):
    seen = fake(token_response=(400, {"error": "authorization_pending"}))
    with TestClient(app) as http:
        login(http)
        pid = start(http)["pending_id"]
        assert http.post("/api/direct/connect/poll", json={"pending_id": pid}).json() == {"status": "waiting"}
        assert http.get("/api/accounts").json()["accounts"] == []

        seen = fake()  # пользователь ввёл код и нажал «Разрешить»
        pid = start(http)["pending_id"]
        r = http.post("/api/direct/connect/poll", json={"pending_id": pid}).json()
        assert r == {"status": "ok", "login": "salon-login"}
        assert "grant_type=device_code" in seen["token_bodies"][0] and "client_secret=api-secret" in seen["token_bodies"][0]
        assert seen["auth"] == "Bearer direct-token-123"
        accounts = http.get("/api/accounts").json()["accounts"]
        assert [a["title"] for a in accounts] == ["salon-login"] and "direct-token-123" not in http.get("/api/accounts").text
        # после подключения запись ожидания удалена: повторный опрос ничего не создаёт
        assert http.post("/api/direct/connect/poll", json={"pending_id": pid}).json()["status"] == "failed"

    row = sqlite3.connect(isolated).execute("SELECT token_enc, refresh_token_enc, expires_at FROM ad_accounts").fetchone()
    assert "direct-token-123" not in row[0] and "refresh-456" not in row[1] and row[2]
    assert decrypt_token(row[0], "test-secret-key") == "direct-token-123"


def test_reconnect_updates_instead_of_duplicating():
    fake()
    with TestClient(app) as http:
        login(http)
        for _ in range(2):
            pid = start(http)["pending_id"]
            assert http.post("/api/direct/connect/poll", json={"pending_id": pid}).json()["status"] == "ok"
        assert len(http.get("/api/accounts").json()["accounts"]) == 1


def test_denied_or_broken_confirmation_is_explained():
    fake(token_response=(400, {"error": "access_denied"}))
    with TestClient(app) as http:
        login(http)
        r = http.post("/api/direct/connect/poll", json={"pending_id": start(http)["pending_id"]}).json()
        assert r["status"] == "failed" and "не подтвердил" in r["message"]


def test_wrong_app_secret_is_reported_to_the_administrator():
    fake(token_response=(400, {"error": "invalid_client", "error_description": "Wrong client secret"}))
    with TestClient(app) as http:
        login(http)
        r = http.post("/api/direct/connect/poll", json={"pending_id": start(http)["pending_id"]}).json()
        assert r["status"] == "failed" and "YANDEX_CLIENT_SECRET" in r["message"]


def test_account_without_direct_access():
    fake(direct_error=58)
    with TestClient(app) as http:
        login(http)
        r = http.post("/api/direct/connect/poll", json={"pending_id": start(http)["pending_id"]}).json()
        assert r["status"] == "failed" and "нет доступа к Яндекс Директу" in r["message"]
        assert http.get("/api/accounts").json()["accounts"] == []


def test_yandex_not_answering_on_start():
    fake(code_status=500)
    with TestClient(app) as http:
        login(http)
        r = http.post("/api/direct/connect/start")
        assert r.status_code == 502 and "Яндекс" in r.json()["detail"]["message"]


def test_expired_and_foreign_pending_are_rejected(isolated):
    fake()
    with TestClient(app) as alice, TestClient(app) as bob:
        alice.post("/api/auth/register", json={"email": "a@example.com", "password": "password123"})
        bob.post("/api/auth/register", json={"email": "b@example.com", "password": "password123"})
        pid = start(alice)["pending_id"]
        assert bob.post("/api/direct/connect/poll", json={"pending_id": pid}).json()["status"] == "failed"  # чужой код
        conn = sqlite3.connect(isolated)
        conn.execute("UPDATE oauth_pending SET expires_at = '2020-01-01T00:00:00+00:00'")
        conn.commit()
        conn.close()
        r = alice.post("/api/direct/connect/poll", json={"pending_id": pid}).json()
        assert r["status"] == "failed" and "время" in r["message"].lower()
