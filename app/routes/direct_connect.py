"""Подключение рекламного кабинета Яндекс Директа кнопкой (OAuth приложения сервиса).

Пользователь ничего не вставляет: нажимает «Подключить», разрешает доступ в Яндексе и возвращается.
Параметры приложения (ClientID, секрет) задаются в настройках сервиса, а не в коде."""
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Cookie, Depends, Request
from fastapi.responses import RedirectResponse

from app.config import get_settings
from app.db import get_db
from app.deps import client_factory, current_user, yandex_http
from app.direct.client import DirectError
from app.security import encrypt_token

router = APIRouter(prefix="/auth/direct", tags=["direct-connect"])

STATE_COOKIE = "autobider_direct_state"
AUTHORIZE_URL = "https://oauth.yandex.ru/authorize"
TOKEN_URL = "https://oauth.yandex.ru/token"


def _redirect_uri(request: Request) -> str:
    configured = get_settings().direct_redirect_uri
    return configured or f"{request.url.scheme}://{request.url.netloc}/auth/direct/callback"


def _home(result: str) -> RedirectResponse:
    """Возврат на сайт с кодом результата (не текстом): страница сама переводит его в понятную фразу."""
    response = RedirectResponse(f"/?connect={result}", status_code=303)
    response.delete_cookie(STATE_COOKIE)
    return response


@router.get("/start")
def start(request: Request, user: sqlite3.Row = Depends(current_user)):
    s = get_settings()
    if not (s.client_id and s.client_secret):
        return _home("not_configured")
    state = secrets.token_urlsafe(24)
    query = urlencode({
        "response_type": "code",
        "client_id": s.client_id,
        "redirect_uri": _redirect_uri(request),
        "state": state,
        "force_confirm": "yes",  # Яндекс спросит, какой аккаунт подключить
    })
    response = RedirectResponse(f"{AUTHORIZE_URL}?{query}", status_code=303)
    response.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True, samesite="lax", secure=request.url.scheme == "https")
    return response


@router.get("/callback")
async def callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    saved_state: str | None = Cookie(default=None, alias=STATE_COOKIE),
    user: sqlite3.Row = Depends(current_user),
    conn: sqlite3.Connection = Depends(get_db),
    http: httpx.AsyncClient = Depends(yandex_http),
    factory=Depends(client_factory),
):
    s = get_settings()
    if error:
        return _home("denied")
    if not code or not state or not saved_state or not hmac.compare_digest(state, saved_state):
        return _home("state")
    if not (s.client_id and s.client_secret):
        return _home("not_configured")

    async with http:
        try:
            response = await http.post(TOKEN_URL, data={
                "grant_type": "authorization_code", "code": code,
                "client_id": s.client_id, "client_secret": s.client_secret,
            })
            data = response.json()
        except (httpx.HTTPError, ValueError):
            return _home("failed")
    token = data.get("access_token")
    if response.status_code != 200 or not token:
        return _home("failed")

    # Проверяем, что у этого аккаунта есть доступ к Директу, и узнаём его логин.
    try:
        async with factory(token, api_version=s.direct_api_version) as client:
            login = await client.whoami()
    except DirectError as e:
        return _home("no_direct" if e.code in (58, 54, 53) else "failed")

    expires = datetime.now(timezone.utc) + timedelta(seconds=int(data.get("expires_in") or 0))
    existing = conn.execute("SELECT id FROM ad_accounts WHERE user_id = ? AND title = ?", (user["id"], login)).fetchone()
    token_enc = encrypt_token(token, s.secret_key)
    refresh_enc = encrypt_token(data["refresh_token"], s.secret_key) if data.get("refresh_token") else ""
    if existing:  # тот же кабинет подключают снова: обновляем доступ
        conn.execute(
            "UPDATE ad_accounts SET token_enc = ?, refresh_token_enc = ?, expires_at = ?, client_login = '' WHERE id = ?",
            (token_enc, refresh_enc, expires.isoformat(), existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO ad_accounts (user_id, title, token_enc, refresh_token_enc, expires_at, client_login) VALUES (?,?,?,?,?, '')",
            (user["id"], login, token_enc, refresh_enc, expires.isoformat()),
        )
    conn.commit()
    return _home("ok")
