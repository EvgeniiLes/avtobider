from datetime import datetime, timedelta, timezone

import httpx
import pytest

import app.direct.runner as runner
from app.db import connect, init_db
from app.engine.util import EngineError
from app.security import decrypt_token, encrypt_token

KEY = "test-secret-key"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("SECRET_KEY", KEY)
    monkeypatch.setenv("YANDEX_CLIENT_ID", "cid")
    monkeypatch.setenv("YANDEX_CLIENT_SECRET", "csecret")
    init_db(tmp_path / "t.db")
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO users (email, password_hash) VALUES ('a@b.c', 'x')")
    yield c
    c.close()


def add_account(conn, days_left, refresh=True):
    expires = (datetime.now(timezone.utc) + timedelta(days=days_left)).isoformat()
    conn.execute(
        "INSERT INTO ad_accounts (user_id, title, token_enc, refresh_token_enc, expires_at) VALUES (1, 'l', ?, ?, ?)",
        (encrypt_token("old-token", KEY), encrypt_token("old-refresh", KEY) if refresh else "", expires),
    )
    conn.commit()


def stub(monkeypatch, status=200, body=None):
    seen = {}

    def handler(request):
        seen["body"] = request.content.decode()
        return httpx.Response(status, json=body if body is not None else {"access_token": "new-token", "refresh_token": "new-refresh", "expires_in": 31536000})

    monkeypatch.setattr(runner, "OAUTH_TRANSPORT", httpx.MockTransport(handler))
    return seen


def test_fresh_token_is_left_alone(conn, monkeypatch):
    seen = stub(monkeypatch)
    add_account(conn, 200)
    assert runner.account_token(conn, 1)[0] == "old-token" and seen == {}


def test_token_close_to_expiry_is_refreshed_and_saved_encrypted(conn, monkeypatch):
    seen = stub(monkeypatch)
    add_account(conn, 5)
    assert runner.account_token(conn, 1)[0] == "new-token"
    assert "grant_type=refresh_token" in seen["body"] and "refresh_token=old-refresh" in seen["body"] and "client_secret=csecret" in seen["body"]
    row = conn.execute("SELECT * FROM ad_accounts").fetchone()
    assert "new-token" not in row["token_enc"] and decrypt_token(row["token_enc"], KEY) == "new-token"
    assert decrypt_token(row["refresh_token_enc"], KEY) == "new-refresh"
    assert datetime.fromisoformat(row["expires_at"]) > datetime.now(timezone.utc) + timedelta(days=300)
    assert runner.account_token(conn, 1)[0] == "new-token"  # второй раз обновлять уже не нужно


def test_still_valid_token_survives_failed_refresh(conn, monkeypatch):
    stub(monkeypatch, status=400, body={"error": "invalid_grant"})
    add_account(conn, 5)
    assert runner.account_token(conn, 1)[0] == "old-token"  # срок ещё не вышел: работаем со старым


def test_expired_token_without_refresh_asks_to_reconnect(conn, monkeypatch):
    stub(monkeypatch)
    add_account(conn, -1, refresh=False)
    with pytest.raises(EngineError) as e:
        runner.account_token(conn, 1)
    assert e.value.code == "expired" and "заново" in e.value.message


def test_expired_token_with_working_refresh_recovers(conn, monkeypatch):
    stub(monkeypatch)
    add_account(conn, -1)
    assert runner.account_token(conn, 1)[0] == "new-token"
