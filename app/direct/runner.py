"""Один проход сервиса по реальной рекламе бизнеса: данные из Директа → решения движка.

Вызывается и кнопкой «Обновить данные», и фоновым процессом (app/worker.py).
"""
import asyncio
import sqlite3
from datetime import date, datetime, timezone

from app.config import get_settings
from app.direct.client import DirectClient, DirectError
from app.direct.launch import sync_units
from app.engine.cycle import run_cycle
from app.engine.util import EngineError
from app.platforms.direct import DirectPlatform
from app.security import decrypt_token


def account_token(conn: sqlite3.Connection, account_id: int) -> tuple[str, str]:
    """Расшифрованный токен кабинета и его логин. Понятная ошибка, если токен не читается или истёк."""
    row = conn.execute("SELECT * FROM ad_accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise EngineError("Рекламный кабинет не найден. Подключите его заново.", "no_account")
    token = decrypt_token(row["token_enc"], get_settings().secret_key)
    if token is None:
        raise EngineError("Не удалось прочитать сохранённый доступ к кабинету. Подключите кабинет заново.", "bad_token")
    if row["expires_at"]:
        try:
            if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
                raise EngineError("Доступ к кабинету истёк. Подключите кабинет заново.", "expired")
        except ValueError:
            pass
    return token, row["client_login"]


def sync_and_run(conn: sqlite3.Connection, biz: sqlite3.Row, today: str | None = None, transport=None) -> dict:
    """Обновляет данные из Директа и запускает цикл решений. Ошибка записывается в бизнес, чтобы её было видно."""
    today = today or date.today().isoformat()
    s = get_settings()
    try:
        if not biz["account_id"]:
            raise EngineError("К бизнесу не подключён рекламный кабинет.", "no_account")
        token, login = account_token(conn, biz["account_id"])

        async def go():
            async with DirectClient(token, api_version=s.direct_api_version, login=login, transport=transport) as client:
                return await sync_units(client, conn, biz, today)

        synced = asyncio.run(go())
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (biz["id"],)).fetchone()
        platform = DirectPlatform(conn, token, s.direct_api_version, login, transport)
        # Без Метрики обращений не видно: судить о рекламе нечем, работает только защита бюджета.
        decide = bool(biz["metrika_counter_id"] and biz["metrika_goal_id"])
        changes = run_cycle(conn, biz, platform, today, decide=decide)
        error = ""
    except (EngineError, DirectError) as e:
        synced, changes, error = {"units": 0, "rows": 0}, [], e.message
    conn.execute(
        "UPDATE businesses SET last_sync_at = ?, last_sync_error = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), error, biz["id"]),
    )
    conn.commit()
    return {"synced": synced, "changes": len(changes), "error": error}
