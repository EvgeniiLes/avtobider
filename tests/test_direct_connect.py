import sqlite3
from urllib.parse import parse_qs, urlparse

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
    monkeypatch.setenv("DIRECT_REDIRECT_URI", "https://example.test/auth/direct/callback")
    auth._fails.clear()
    yield tmp_path / "test.db"
    app.dependency_overrides.clear()


def fake(token_status=200, login="salon-login", direct_error=None):
    seen = {}

    def oauth(request):
        seen["body"] = request.content.decode()
        if token_status != 200:
            return httpx.Response(token_status, json={"error": "bad"})
        return httpx.Response(200, json={"access_token": "direct-token-123", "refresh_token": "refresh-456", "expires_in": 31536000})

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
    if http.post("/api/auth/register", json=creds).status_code == 409:  # пользователь уже есть: просто входим
        http.post("/api/auth/login", json=creds)


def begin(http):
    r = http.get("/auth/direct/start", follow_redirects=False)
    assert r.status_code == 303
    return r, parse_qs(urlparse(r.headers["location"]).query)["state"][0]


def test_requires_login():
    with TestClient(app) as http:
        assert http.get("/auth/direct/start", follow_redirects=False).status_code == 401


def test_config_flag(monkeypatch):
    with TestClient(app) as http:
        assert http.get("/api/config").json()["direct_oauth"] is True
        monkeypatch.setenv("YANDEX_CLIENT_SECRET", "")
        assert http.get("/api/config").json()["direct_oauth"] is False
        login(http)
        assert http.get("/auth/direct/start", follow_redirects=False).headers["location"].endswith("connect=not_configured")


def test_start_goes_to_yandex_with_our_app():
    with TestClient(app) as http:
        login(http)
        r, state = begin(http)
        loc = r.headers["location"]
        assert loc.startswith("https://oauth.yandex.ru/authorize?")
        assert "client_id=api-client" in loc and "api-secret" not in loc
        assert "redirect_uri=https%3A%2F%2Fexample.test%2Fauth%2Fdirect%2Fcallback" in loc


def test_connect_saves_encrypted_token_and_login(isolated):
    seen = fake()
    with TestClient(app) as http:
        login(http)
        _, state = begin(http)
        r = http.get(f"/auth/direct/callback?code=abc&state={state}", follow_redirects=False)
        assert r.headers["location"] == "/?connect=ok"
        assert "client_secret=api-secret" in seen["body"] and "code=abc" in seen["body"]
        assert seen["auth"] == "Bearer direct-token-123"
        accounts = http.get("/api/accounts").json()["accounts"]
        assert [a["title"] for a in accounts] == ["salon-login"]
        assert "direct-token-123" not in http.get("/api/accounts").text

    row = sqlite3.connect(isolated).execute("SELECT token_enc, refresh_token_enc, expires_at FROM ad_accounts").fetchone()
    assert "direct-token-123" not in row[0] and "refresh-456" not in row[1] and row[2]
    assert decrypt_token(row[0], "test-secret-key") == "direct-token-123"


def test_reconnect_updates_instead_of_duplicating(isolated):
    fake()
    with TestClient(app) as http:
        login(http)
        for _ in range(2):
            _, state = begin(http)
            http.get(f"/auth/direct/callback?code=abc&state={state}", follow_redirects=False)
        assert len(http.get("/api/accounts").json()["accounts"]) == 1


@pytest.mark.parametrize("query,expected", [
    ("code=abc&state=wrong", "state"),
    ("code=abc", "state"),
    ("error=access_denied", "denied"),
])
def test_bad_callbacks_connect_nothing(query, expected):
    fake()
    with TestClient(app) as http:
        login(http)
        begin(http)
        r = http.get(f"/auth/direct/callback?{query}", follow_redirects=False)
        assert r.headers["location"].endswith(f"connect={expected}")
        assert http.get("/api/accounts").json()["accounts"] == []


def test_token_exchange_failure_and_no_direct_access():
    fake(token_status=400)
    with TestClient(app) as http:
        login(http)
        _, state = begin(http)
        assert http.get(f"/auth/direct/callback?code=abc&state={state}", follow_redirects=False).headers["location"].endswith("connect=failed")
    fake(direct_error=58)
    with TestClient(app) as http:
        login(http)
        _, state = begin(http)
        assert http.get(f"/auth/direct/callback?code=abc&state={state}", follow_redirects=False).headers["location"].endswith("connect=no_direct")
        assert http.get("/api/accounts").json()["accounts"] == []


def test_state_cannot_be_reused():
    fake()
    with TestClient(app) as http:
        login(http)
        _, state = begin(http)
        http.get(f"/auth/direct/callback?code=abc&state={state}", follow_redirects=False)
        r = http.get(f"/auth/direct/callback?code=abc&state={state}", follow_redirects=False)
        assert r.headers["location"].endswith("connect=state")
