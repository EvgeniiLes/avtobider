"""Один проход сервиса по реальной рекламе бизнеса: данные из Директа → решения движка.

Вызывается и кнопкой «Обновить данные», и фоновым процессом (app/worker.py).
"""
import asyncio
import sqlite3
from datetime import date, datetime, timedelta, timezone

import httpx

from app.config import get_settings
from app.direct.client import DirectClient, DirectError
from app.direct.goals import fetch_goals, save_goals
from app.direct.launch import sync_units
from app.engine.config import DIRECT_CONFIG
from app.engine.cycle import run_cycle
from app.engine.util import EngineError
from app.platforms.direct import DirectPlatform
from app.security import decrypt_token, encrypt_token


OAUTH_TRANSPORT: httpx.BaseTransport | None = None  # в тестах сюда подставляют заглушку Яндекса
REFRESH_BEFORE = timedelta(days=14)


def _refresh(conn: sqlite3.Connection, row: sqlite3.Row) -> str | None:
    """Обновляет токен заранее, пока он не истёк. Возвращает новый токен или None, если обновить не вышло."""
    s = get_settings()
    refresh = decrypt_token(row["refresh_token_enc"], s.secret_key) if row["refresh_token_enc"] else None
    if not (refresh and s.client_id and s.client_secret):
        return None
    try:
        with httpx.Client(timeout=20, transport=OAUTH_TRANSPORT) as http:
            r = http.post("https://oauth.yandex.ru/token", data={
                "grant_type": "refresh_token", "refresh_token": refresh, "client_id": s.client_id, "client_secret": s.client_secret})
            data = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    if r.status_code != 200 or not data.get("access_token"):
        return None
    expires = datetime.now(timezone.utc) + timedelta(seconds=int(data.get("expires_in") or 0))
    conn.execute(
        "UPDATE ad_accounts SET token_enc = ?, refresh_token_enc = ?, expires_at = ? WHERE id = ?",
        (encrypt_token(data["access_token"], s.secret_key),
         encrypt_token(data["refresh_token"], s.secret_key) if data.get("refresh_token") else row["refresh_token_enc"],
         expires.isoformat(), row["id"]),
    )
    conn.commit()
    return data["access_token"]


def account_token(conn: sqlite3.Connection, account_id: int) -> tuple[str, str]:
    """Расшифрованный токен кабинета и его логин. За две недели до конца срока токен обновляется сам.
    Понятная ошибка, если токен не читается или истёк и обновить не удалось."""
    row = conn.execute("SELECT * FROM ad_accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise EngineError("Рекламный кабинет не найден. Подключите его заново.", "no_account")
    token = decrypt_token(row["token_enc"], get_settings().secret_key)
    if token is None:
        raise EngineError("Не удалось прочитать сохранённый доступ к кабинету. Подключите кабинет заново.", "bad_token")
    if row["expires_at"]:
        try:
            expires = datetime.fromisoformat(row["expires_at"])
        except ValueError:
            expires = None
        if expires and expires - datetime.now(timezone.utc) < REFRESH_BEFORE:
            fresh = _refresh(conn, row)
            if fresh:
                return fresh, row["client_login"]
            if expires < datetime.now(timezone.utc):
                raise EngineError("Доступ к кабинету истёк. Подключите кабинет заново.", "expired")
    return token, row["client_login"]


def sync_and_run(conn: sqlite3.Connection, biz: sqlite3.Row, today: str | None = None, transport=None) -> dict:
    """Обновляет данные из Директа и запускает цикл решений. Ошибка записывается в бизнес, чтобы её было видно."""
    today = today or date.today().isoformat()
    s = get_settings()
    try:
        if not biz["account_id"]:
            raise EngineError("К бизнесу не подключён рекламный кабинет.", "no_account")
        token, login = account_token(conn, biz["account_id"])
        if biz["metrika_counter_id"]:   # названия и список целей счётчика: по ним собираем все действия посетителей, а не одну цель
            save_goals(conn, biz["id"], asyncio.run(fetch_goals(token, biz["metrika_counter_id"], transport)))

        async def go():
            async with DirectClient(token, api_version=s.direct_api_version, login=login, transport=transport) as client:
                return await sync_units(client, conn, biz, today)

        synced = asyncio.run(go())
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (biz["id"],)).fetchone()
        platform = DirectPlatform(conn, token, s.direct_api_version, login, transport)
        # Без Метрики обращений не видно: судить о рекламе нечем, работает только защита бюджета.
        decide = bool(biz["metrika_counter_id"] and biz["metrika_goal_id"])
        changes = run_cycle(conn, biz, platform, today, cfg=DIRECT_CONFIG, decide=decide)
        error = ""
    except (EngineError, DirectError) as e:
        synced, changes, error = {"units": 0, "rows": 0}, [], e.message
    conn.execute(
        "UPDATE businesses SET last_sync_at = ?, last_sync_error = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), error, biz["id"]),
    )
    conn.commit()
    return {"synced": synced, "changes": len(changes), "error": error}
