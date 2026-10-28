import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from app.deps import client_factory
from app.direct.client import DirectClient
from app.main import app
from app.routes import auth


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("YANDEX_DEV_TOKEN", "dev-token")
    monkeypatch.setenv("APP_ENV", "")
    auth._fails.clear()
    yield tmp_path / "test.db"
    app.dependency_overrides.clear()


def direct_stub(handler):
    def factory(token, **kwargs):
        return DirectClient(token, transport=httpx.MockTransport(handler), **kwargs)

    app.dependency_overrides[client_factory] = lambda: factory


def campaigns_ok(request):
    return httpx.Response(
        200,
        json={"result": {"Campaigns": [{"Id": 7, "Name": "Йога", "Type": "TEXT_CAMPAIGN", "State": "ON", "Status": "ACCEPTED"}]}},
        headers={"Units": "10/100/200"},
    )


def register(http, email="owner@example.com", password="password123"):
    return http.post("/api/auth/register", json={"email": email, "password": password})


# ---------- вход и регистрация ----------

def test_register_login_logout():
    with TestClient(app) as http:
        assert http.get("/api/auth/me").status_code == 401
        assert register(http).status_code == 200
        assert http.get("/api/auth/me").json() == {"email": "owner@example.com"}
        http.post("/api/auth/logout")
        assert http.get("/api/auth/me").status_code == 401
        assert http.post("/api/auth/login", json={"email": "OWNER@example.com", "password": "password123"}).status_code == 200
        assert http.get("/api/auth/me").status_code == 200


def test_register_validation():
    with TestClient(app) as http:
        assert "e-mail" in register(http, email="не-почта").json()["detail"]["message"]
        assert "короткий" in register(http, password="123").json()["detail"]["message"]
        assert register(http).status_code == 200
        again = register(http)
        assert again.status_code == 409


def test_wrong_password_and_lockout():
    with TestClient(app) as http:
        register(http)
        http.post("/api/auth/logout")
        for _ in range(auth.MAX_FAILS):
            r = http.post("/api/auth/login", json={"email": "owner@example.com", "password": "bad-password"})
            assert r.status_code == 401
        r = http.post("/api/auth/login", json={"email": "owner@example.com", "password": "password123"})
        assert r.status_code == 429


def test_password_not_stored_in_plain_text(isolated):
    with TestClient(app) as http:
        register(http, password="very-secret-pass")
    row = sqlite3.connect(isolated).execute("SELECT password_hash FROM users").fetchone()
    assert "very-secret-pass" not in row[0]


# ---------- кабинеты ----------

def test_accounts_require_login():
    with TestClient(app) as http:
        assert http.get("/api/accounts").status_code == 401
        assert http.post("/api/accounts", json={"token": "t"}).status_code == 401


def test_add_account_encrypts_token_and_shows_campaigns(isolated):
    direct_stub(campaigns_ok)
    with TestClient(app) as http:
        register(http)
        r = http.post("/api/accounts", json={"token": "user-token-123", "title": "Салон"})
        assert r.status_code == 200
        assert r.json()["campaigns_count"] == 1
        assert "user-token-123" not in r.text

        listed = http.get("/api/accounts")
        assert "user-token-123" not in listed.text
        account_id = listed.json()["accounts"][0]["id"]

        shown = http.get(f"/api/accounts/{account_id}/campaigns").json()
        assert shown["campaigns"][0]["state"] == "Показы идут"
        assert shown["campaigns"][0]["status"] == "Проверка пройдена"
        assert shown["units"]["remaining"] == 100

    stored = sqlite3.connect(isolated).execute("SELECT token_enc FROM ad_accounts").fetchone()[0]
    assert "user-token-123" not in stored


def test_bad_token_is_not_saved():
    direct_stub(lambda r: httpx.Response(200, json={"error": {"error_code": 53, "error_string": "x"}}))
    with TestClient(app) as http:
        register(http)
        r = http.post("/api/accounts", json={"token": "bad"})
        assert r.status_code == 502
        assert "токен" in r.json()["detail"]["message"].lower()
        assert http.get("/api/accounts").json()["accounts"] == []


def test_users_do_not_see_each_others_accounts():
    direct_stub(campaigns_ok)
    with TestClient(app) as alice, TestClient(app) as bob:
        register(alice, "alice@example.com")
        register(bob, "bob@example.com")
        account_id = alice.post("/api/accounts", json={"token": "t"}).json()["account"]["id"]
        assert bob.get("/api/accounts").json()["accounts"] == []
        assert bob.get(f"/api/accounts/{account_id}/campaigns").status_code == 404
        assert bob.delete(f"/api/accounts/{account_id}").status_code == 404
        assert alice.delete(f"/api/accounts/{account_id}").status_code == 200


def test_dev_token_only_in_development_mode(monkeypatch):
    direct_stub(campaigns_ok)
    with TestClient(app) as http:
        register(http)
        assert http.get("/api/config").json()["dev_token_available"] is False
        assert http.post("/api/accounts", json={"use_dev_token": True}).status_code == 403
        monkeypatch.setenv("APP_ENV", "development")
        assert http.get("/api/config").json()["dev_token_available"] is True
        assert http.post("/api/accounts", json={"use_dev_token": True}).status_code == 200


def test_missing_token():
    with TestClient(app) as http:
        register(http)
        r = http.post("/api/accounts", json={"token": "  "})
        assert r.status_code == 400


def test_unreadable_token_after_key_change(monkeypatch):
    direct_stub(campaigns_ok)
    with TestClient(app) as http:
        register(http)
        account_id = http.post("/api/accounts", json={"token": "t"}).json()["account"]["id"]
        monkeypatch.setenv("SECRET_KEY", "another-key")
        r = http.get(f"/api/accounts/{account_id}/campaigns")
        assert r.status_code == 409


def test_security_headers_everywhere():
    with TestClient(app) as http:
        for path in ("/", "/api/health", "/static/app.js"):
            h = http.get(path).headers
            assert "frame-ancestors 'none'" in h["content-security-policy"] and "script-src 'self'" in h["content-security-policy"]
            assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
        assert "strict-transport-security" not in http.get("/").headers  # по обычному http HSTS не отправляем
        assert "max-age" in http.get("https://testserver/").headers["strict-transport-security"]


def test_page_has_no_inline_scripts_or_handlers():
    """CSP запрещает встроенные скрипты: страница должна жить только на подключённых файлах."""
    import re
    with TestClient(app) as http:
        html = http.get("/").text
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html, re.I)
        assert not re.search(r"\son\w+\s*=", html, re.I) and "style=" not in html


def test_signup_rate_limit():
    from app.routes import auth as auth_routes
    auth_routes._signups.clear()
    with TestClient(app) as http:
        for i in range(auth_routes.MAX_SIGNUPS):
            assert http.post("/api/auth/register", json={"email": f"u{i}@example.com", "password": "password123"}).status_code == 200
        r = http.post("/api/auth/register", json={"email": "extra@example.com", "password": "password123"})
        assert r.status_code == 429 and "слишком много регистраций" in r.json()["detail"]["message"]
    auth_routes._signups.clear()


def test_health_checks_the_database(monkeypatch):
    with TestClient(app) as http:
        assert http.get("/api/health").json() == {"ok": True}
        def broken(_):
            raise RuntimeError("нет базы")
        monkeypatch.setattr("app.main.connect", broken)
        assert http.get("/api/health").status_code == 503


def test_pages_served():
    with TestClient(app) as http:
        assert http.get("/api/health").json() == {"ok": True}
        assert "Автобидер" in http.get("/").text
        assert http.get("/static/app.js").status_code == 200
