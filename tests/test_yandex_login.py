import httpx
import pytest
from fastapi.testclient import TestClient

from app.deps import yandex_http
from app.main import app
from app.routes import auth


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("YANDEX_LOGIN_CLIENT_ID", "login-client")
    monkeypatch.setenv("YANDEX_LOGIN_CLIENT_SECRET", "login-secret")
    monkeypatch.setenv("YANDEX_REDIRECT_URI", "https://example.test/auth/yandex/callback")
    auth._fails.clear()
    yield
    app.dependency_overrides.clear()


def fake_yandex(info=None, token_status=200, info_status=200):
    info = info or {"id": "42", "login": "ivan", "default_email": "Ivan@Yandex.ru"}
    seen = {}

    def handler(request: httpx.Request):
        if request.url.host == "oauth.yandex.ru":
            seen["token_body"] = request.content.decode()
            body = {"access_token": "tok"} if token_status == 200 else {"error": "bad"}
            return httpx.Response(token_status, json=body)
        seen["info_auth"] = request.headers.get("Authorization")
        return httpx.Response(info_status, json=info)

    app.dependency_overrides[yandex_http] = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return seen


def begin(http):
    r = http.get("/auth/yandex/start", follow_redirects=False)
    assert r.status_code == 303
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(r.headers["location"]).query)
    return r, q["state"][0]


def test_config_flag(monkeypatch):
    with TestClient(app) as http:
        assert http.get("/api/config").json()["yandex_login"] is True
        monkeypatch.setenv("YANDEX_LOGIN_CLIENT_SECRET", "")
        assert http.get("/api/config").json()["yandex_login"] is False
        assert http.get("/auth/yandex/start", follow_redirects=False).headers["location"].endswith("login_error=not_configured")


def test_start_redirects_to_yandex_with_state():
    with TestClient(app) as http:
        r, state = begin(http)
        loc = r.headers["location"]
        assert loc.startswith("https://oauth.yandex.ru/authorize?")
        assert "client_id=login-client" in loc
        assert "redirect_uri=https%3A%2F%2Fexample.test%2Fauth%2Fyandex%2Fcallback" in loc
        assert "login-secret" not in loc
        assert http.cookies.get("autobider_oauth_state") == state


def test_successful_login_creates_user_and_session():
    seen = fake_yandex()
    with TestClient(app) as http:
        _, state = begin(http)
        r = http.get(f"/auth/yandex/callback?code=abc&state={state}", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert http.get("/api/auth/me").json() == {"email": "ivan@yandex.ru"}
        assert "code=abc" in seen["token_body"] and "client_secret=login-secret" in seen["token_body"]
        assert seen["info_auth"] == "OAuth tok"
        http.post("/api/auth/logout")
        _, state = begin(http)
        http.get(f"/auth/yandex/callback?code=abc&state={state}", follow_redirects=False)
        assert http.get("/api/auth/me").status_code == 200  # тот же пользователь, второй вход


def test_links_to_existing_account_with_same_email():
    fake_yandex()
    with TestClient(app) as http:
        http.post("/api/auth/register", json={"email": "ivan@yandex.ru", "password": "password123"})
        http.post("/api/auth/logout")
        _, state = begin(http)
        http.get(f"/auth/yandex/callback?code=abc&state={state}", follow_redirects=False)
        assert http.get("/api/auth/me").json()["email"] == "ivan@yandex.ru"
        http.post("/api/auth/logout")
        # пароль у старого аккаунта остался рабочим
        assert http.post("/api/auth/login", json={"email": "ivan@yandex.ru", "password": "password123"}).status_code == 200


def test_yandex_only_user_cannot_log_in_with_empty_password():
    fake_yandex()
    with TestClient(app) as http:
        _, state = begin(http)
        http.get(f"/auth/yandex/callback?code=abc&state={state}", follow_redirects=False)
        http.post("/api/auth/logout")
        assert http.post("/api/auth/login", json={"email": "ivan@yandex.ru", "password": ""}).status_code in (401, 422)


@pytest.mark.parametrize("query,expected", [
    ("code=abc&state=wrong", "state"),
    ("code=abc", "state"),
    ("error=access_denied", "denied"),
])
def test_bad_callbacks_do_not_log_in(query, expected):
    fake_yandex()
    with TestClient(app) as http:
        begin(http)
        r = http.get(f"/auth/yandex/callback?{query}", follow_redirects=False)
        assert r.headers["location"].endswith(f"login_error={expected}")
        assert http.get("/api/auth/me").status_code == 401


def test_callback_without_prior_start_is_rejected():
    fake_yandex()
    with TestClient(app) as http:
        r = http.get("/auth/yandex/callback?code=abc&state=anything", follow_redirects=False)
        assert r.headers["location"].endswith("login_error=state")


def test_yandex_errors_are_reported():
    for kwargs in ({"token_status": 400}, {"info_status": 401}):
        fake_yandex(**kwargs)
        with TestClient(app) as http:
            _, state = begin(http)
            r = http.get(f"/auth/yandex/callback?code=abc&state={state}", follow_redirects=False)
            assert r.headers["location"].endswith("login_error=failed")
            assert http.get("/api/auth/me").status_code == 401


def test_state_cannot_be_reused():
    fake_yandex()
    with TestClient(app) as http:
        _, state = begin(http)
        http.get(f"/auth/yandex/callback?code=abc&state={state}", follow_redirects=False)
        http.post("/api/auth/logout")
        r = http.get(f"/auth/yandex/callback?code=abc&state={state}", follow_redirects=False)
        assert r.headers["location"].endswith("login_error=state")
