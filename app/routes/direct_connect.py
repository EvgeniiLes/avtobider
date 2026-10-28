"""Подключение рекламного кабинета Яндекс Директа кнопкой (приложение сервиса, вход по коду).

У приложения Яндекса «для доступа к API» нет адреса возврата, поэтому используется штатный вход по коду:
сервис показывает короткий код, пользователь вводит его на странице Яндекса (ya.ru/device) и нажимает
«Разрешить». Токены нигде не вставляются. Параметры приложения задаются в настройках сервиса.
"""
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.db import get_db
from app.deps import client_factory, current_user, yandex_http
from app.direct.client import DirectError
from app.security import encrypt_token

router = APIRouter(prefix="/api/direct/connect", tags=["direct-connect"])

CODE_URL = "https://oauth.yandex.ru/device/code"
TOKEN_URL = "https://oauth.yandex.ru/token"


def _configured():
    s = get_settings()
    if not (s.client_id and s.client_secret):
        raise HTTPException(503, detail={"message": "Подключение кабинетов ещё не настроено на сервере: нужны ключи приложения Яндекса."})
    return s


@router.post("/start")
async def start(user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db),
                http: httpx.AsyncClient = Depends(yandex_http)):
    s = _configured()
    async with http:
        try:
            r = await http.post(CODE_URL, data={"client_id": s.client_id, "device_id": str(uuid.uuid4()), "device_name": "Автобидер"})
            data = r.json()
        except (httpx.HTTPError, ValueError):
            raise HTTPException(502, detail={"message": "Не удалось связаться с Яндексом. Попробуйте ещё раз чуть позже."})
    if r.status_code != 200 or "device_code" not in data:
        raise HTTPException(502, detail={"message": "Яндекс не выдал код для подключения. Попробуйте ещё раз чуть позже."})
    now = datetime.now(timezone.utc)
    conn.execute("DELETE FROM oauth_pending WHERE expires_at < ?", (now.isoformat(),))
    expires = (now + timedelta(seconds=int(data.get("expires_in", 300)))).isoformat()
    cur = conn.execute(
        "INSERT INTO oauth_pending (user_id, device_code, user_code, expires_at) VALUES (?,?,?,?)",
        (user["id"], data["device_code"], data["user_code"], expires),
    )
    conn.commit()
    return {
        "pending_id": cur.lastrowid,
        "user_code": data["user_code"].upper(),
        "url": data.get("verification_url", "https://ya.ru/device"),
        "interval": int(data.get("interval", 5)),
        "expires_in": int(data.get("expires_in", 300)),
    }


class Poll(BaseModel):
    pending_id: int


def save_account(conn: sqlite3.Connection, user_id: int, login: str, token_data: dict) -> None:
    """Сохраняет кабинет (или обновляет доступ, если он уже подключён). Токен хранится только зашифрованным."""
    s = get_settings()
    expires = datetime.now(timezone.utc) + timedelta(seconds=int(token_data.get("expires_in") or 0))
    token_enc = encrypt_token(token_data["access_token"], s.secret_key)
    refresh = token_data.get("refresh_token")
    refresh_enc = encrypt_token(refresh, s.secret_key) if refresh else ""
    existing = conn.execute("SELECT id FROM ad_accounts WHERE user_id = ? AND title = ?", (user_id, login)).fetchone()
    if existing:
        conn.execute(
            "UPDATE ad_accounts SET token_enc = ?, refresh_token_enc = ?, expires_at = ?, client_login = '' WHERE id = ?",
            (token_enc, refresh_enc, expires.isoformat(), existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO ad_accounts (user_id, title, token_enc, refresh_token_enc, expires_at, client_login) VALUES (?,?,?,?,?, '')",
            (user_id, login, token_enc, refresh_enc, expires.isoformat()),
        )
    conn.commit()


@router.post("/poll")
async def poll(data: Poll, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db),
               http: httpx.AsyncClient = Depends(yandex_http), factory=Depends(client_factory)):
    """Страница спрашивает раз в несколько секунд: разрешил ли пользователь доступ."""
    s = _configured()
    row = conn.execute("SELECT * FROM oauth_pending WHERE id = ? AND user_id = ?", (data.pending_id, user["id"])).fetchone()
    if row is None:
        return {"status": "failed", "message": "Подключение устарело. Нажмите «Подключить Яндекс Директ» ещё раз."}
    if row["expires_at"] < datetime.now(timezone.utc).isoformat():
        conn.execute("DELETE FROM oauth_pending WHERE id = ?", (row["id"],))
        conn.commit()
        return {"status": "failed", "message": "Время на ввод кода вышло. Нажмите «Подключить Яндекс Директ» ещё раз."}

    async with http:
        try:
            r = await http.post(TOKEN_URL, data={
                "grant_type": "device_code", "code": row["device_code"],
                "client_id": s.client_id, "client_secret": s.client_secret,
            })
            token = r.json()
        except (httpx.HTTPError, ValueError):
            return {"status": "waiting"}  # временный сбой: страница спросит ещё раз
    if r.status_code != 200 or not token.get("access_token"):
        if token.get("error") in ("authorization_pending", "slow_down", None):
            return {"status": "waiting"}
        conn.execute("DELETE FROM oauth_pending WHERE id = ?", (row["id"],))
        conn.commit()
        if token.get("error") == "invalid_client":
            return {"status": "failed", "message": (
                "Ключи приложения Яндекса на сервере указаны неверно (YANDEX_CLIENT_ID и YANDEX_CLIENT_SECRET). "
                "Администратору нужно проверить их в настройках сервиса.")}
        return {"status": "failed", "message": "Яндекс не подтвердил подключение. Нажмите «Подключить Яндекс Директ» и попробуйте ещё раз."}

    # Проверяем, что у этого аккаунта есть доступ к Директу, и узнаём его логин.
    try:
        async with factory(token["access_token"], api_version=s.direct_api_version) as client:
            login = await client.whoami()
    except DirectError as e:
        conn.execute("DELETE FROM oauth_pending WHERE id = ?", (row["id"],))
        conn.commit()
        no_access = e.code in (53, 54, 58)
        return {"status": "failed", "message": (
            "У этого аккаунта нет доступа к Яндекс Директу. Подключите аккаунт, в котором есть рекламный кабинет."
            if no_access else f"Не удалось проверить кабинет: {e.message}")}

    save_account(conn, user["id"], login, token)
    conn.execute("DELETE FROM oauth_pending WHERE id = ?", (row["id"],))
    conn.commit()
    return {"status": "ok", "login": login}
