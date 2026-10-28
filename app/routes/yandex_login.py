"""Вход через Яндекс ID (OAuth, код авторизации)."""
import hmac
import secrets
import sqlite3
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Cookie, Depends, Request
from fastapi.responses import RedirectResponse

from app.config import get_settings
from app.db import get_db
from app.deps import yandex_http
from app.security import SESSION_COOKIE, SESSION_DAYS, create_session

router = APIRouter(prefix="/auth/yandex", tags=["yandex-login"])

STATE_COOKIE = "autobider_oauth_state"
AUTHORIZE_URL = "https://oauth.yandex.ru/authorize"
TOKEN_URL = "https://oauth.yandex.ru/token"
INFO_URL = "https://login.yandex.ru/info"


def _redirect_uri(request: Request) -> str:
    configured = get_settings().redirect_uri
    return configured or f"{request.url.scheme}://{request.url.netloc}/auth/yandex/callback"


def _home(error: str = "") -> RedirectResponse:
    """Возвращает на главную. Код ошибки (не текст) страница переводит в понятную фразу сама."""
    response = RedirectResponse(f"/?login_error={error}" if error else "/", status_code=303)
    response.delete_cookie(STATE_COOKIE)
    return response


@router.get("/start")
def start(request: Request):
    s = get_settings()
    if not (s.login_client_id and s.login_client_secret):
        return _home("not_configured")
    state = secrets.token_urlsafe(24)
    query = urlencode({
        "response_type": "code",
        "client_id": s.login_client_id,
        "redirect_uri": _redirect_uri(request),
        "state": state,
        "force_confirm": "yes",  # Яндекс покажет выбор аккаунта, а не войдёт сразу под тем, что открыт в браузере
    })
    response = RedirectResponse(f"{AUTHORIZE_URL}?{query}", status_code=303)
    response.set_cookie(
        STATE_COOKIE, state, max_age=600, httponly=True, samesite="lax", secure=request.url.scheme == "https"
    )
    return response


@router.get("/callback")
async def callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    saved_state: str | None = Cookie(default=None, alias=STATE_COOKIE),
    conn: sqlite3.Connection = Depends(get_db),
    http: httpx.AsyncClient = Depends(yandex_http),
):
    s = get_settings()
    async with http:
        if error:
            return _home("denied")
        if not code or not state or not saved_state or not hmac.compare_digest(state, saved_state):
            return _home("state")
        if not (s.login_client_id and s.login_client_secret):
            return _home("not_configured")
        try:
            token_response = await http.post(TOKEN_URL, data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": s.login_client_id,
                "client_secret": s.login_client_secret,
            })
            access_token = token_response.json().get("access_token")
            if token_response.status_code != 200 or not access_token:
                return _home("failed")
            info_response = await http.get(INFO_URL, params={"format": "json"}, headers={"Authorization": f"OAuth {access_token}"})
            info = info_response.json()
        except (httpx.HTTPError, ValueError):
            return _home("failed")
        if info_response.status_code != 200 or not info.get("id"):
            return _home("failed")

    yandex_id = str(info["id"])
    email = (info.get("default_email") or "").strip().lower() or f"{info.get('login', yandex_id)}@yandex.ru"

    user = conn.execute("SELECT id FROM users WHERE yandex_id = ?", (yandex_id,)).fetchone()
    if user is None:
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:  # тот же e-mail: привязываем Яндекс ID к имеющемуся аккаунту
            conn.execute("UPDATE users SET yandex_id = ? WHERE id = ?", (yandex_id, existing["id"]))
            user = existing
        else:
            cur = conn.execute("INSERT INTO users (email, password_hash, yandex_id) VALUES (?, '', ?)", (email, yandex_id))
            user = {"id": cur.lastrowid}
        conn.commit()

    response = _home()
    response.set_cookie(
        SESSION_COOKIE, create_session(conn, user["id"]), max_age=SESSION_DAYS * 86400,
        httponly=True, samesite="lax", secure=request.url.scheme == "https",
    )
    return response
