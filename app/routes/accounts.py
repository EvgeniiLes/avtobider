"""Подключённые рекламные кабинеты."""
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.db import get_db
from app.deps import client_factory, current_user
from app.direct.client import DirectError
from app.direct.labels import describe_campaign
from app.security import decrypt_token, encrypt_token

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


class NewAccount(BaseModel):
    title: str = ""
    token: str = ""
    login: str = ""
    sandbox: bool = False
    use_dev_token: bool = False  # только при APP_ENV=development: взять токен из .env


def _public(row: sqlite3.Row) -> dict:
    """Кабинет для показа на странице. Токен сюда не попадает никогда."""
    return {
        "id": row["id"],
        "title": row["title"],
        "platform": "Яндекс Директ",
        "login": row["client_login"],
        "sandbox": bool(row["sandbox"]),
    }


def _units(client) -> dict | None:
    u = client.last_units
    return {"spent": u.spent, "remaining": u.remaining, "daily_limit": u.daily_limit} if u else None


def _direct_error(e: DirectError) -> HTTPException:
    return HTTPException(502, detail={"message": e.message, "code": e.code, "detail": e.detail, "request_id": e.request_id})


def _own_account(conn: sqlite3.Connection, user: sqlite3.Row, account_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM ad_accounts WHERE id = ? AND user_id = ?", (account_id, user["id"])).fetchone()
    if row is None:
        raise HTTPException(404, detail={"message": "Такой кабинет не найден."})
    return row


@router.get("")
def list_accounts(user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute("SELECT * FROM ad_accounts WHERE user_id = ? ORDER BY id", (user["id"],)).fetchall()
    return {"accounts": [_public(r) for r in rows]}


@router.post("")
async def add_account(
    data: NewAccount,
    user: sqlite3.Row = Depends(current_user),
    conn: sqlite3.Connection = Depends(get_db),
    factory=Depends(client_factory),
):
    s = get_settings()
    if data.use_dev_token:
        if not s.dev_mode:
            raise HTTPException(403, detail={"message": "Токен с сервера доступен только в режиме разработки."})
        token = s.dev_token
    else:
        token = data.token.strip()
    if not token:
        raise HTTPException(400, detail={"message": "Вставьте токен Яндекса."})
    login = data.login.strip() or s.direct_client_login

    # Сохраняем кабинет только после успешной проверки связи.
    try:
        async with factory(token, sandbox=data.sandbox, api_version=s.direct_api_version, login=login) as client:
            campaigns = await client.list_campaigns()
            units = _units(client)
    except DirectError as e:
        raise _direct_error(e)

    title = data.title.strip() or ("Тестовая песочница" if data.sandbox else "Мой кабинет")
    cur = conn.execute(
        "INSERT INTO ad_accounts (user_id, title, token_enc, client_login, sandbox) VALUES (?, ?, ?, ?, ?)",
        (user["id"], title, encrypt_token(token, s.secret_key), login, int(data.sandbox)),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM ad_accounts WHERE id = ?", (cur.lastrowid,)).fetchone()
    return {"account": _public(row), "campaigns_count": len(campaigns), "units": units}


@router.delete("/{account_id}")
def delete_account(account_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    _own_account(conn, user, account_id)
    conn.execute("DELETE FROM ad_accounts WHERE id = ?", (account_id,))
    conn.commit()
    return {"ok": True}


@router.get("/{account_id}/campaigns")
async def account_campaigns(
    account_id: int,
    user: sqlite3.Row = Depends(current_user),
    conn: sqlite3.Connection = Depends(get_db),
    factory=Depends(client_factory),
):
    row = _own_account(conn, user, account_id)
    s = get_settings()
    token = decrypt_token(row["token_enc"], s.secret_key)
    if token is None:
        raise HTTPException(
            409,
            detail={"message": "Не удалось прочитать сохранённый токен. Удалите кабинет и подключите его заново."},
        )
    try:
        async with factory(token, sandbox=bool(row["sandbox"]), api_version=s.direct_api_version, login=row["client_login"]) as client:
            items = await client.list_campaigns()
            units = _units(client)
    except DirectError as e:
        raise _direct_error(e)
    return {"account": _public(row), "campaigns": [describe_campaign(c) for c in items], "units": units}
