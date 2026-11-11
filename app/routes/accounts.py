"""Подключённые рекламные кабинеты."""
import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.db import get_db
from app.deps import client_factory, current_user, platform_transport
from app.direct.client import DirectError
from app.direct.labels import describe_campaign
from app.platforms.avito import AvitoClient, AvitoError
from app.platforms.registry import PLATFORMS, label
from app.platforms.vk import VkAdsClient, VkError
from app.security import decrypt_token, encrypt_token

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

KEY_CLIENTS = {"vk_ads": (VkAdsClient, VkError), "avito": (AvitoClient, AvitoError)}


class NewAccount(BaseModel):
    title: str = ""
    token: str = ""
    login: str = ""
    use_dev_token: bool = False  # только при APP_ENV=development: взять токен из .env


class NewKeysAccount(BaseModel):
    platform: str
    title: str = ""
    client_id: str = Field(min_length=1, max_length=200)
    client_secret: str = Field(min_length=1, max_length=400)


def _public(row: sqlite3.Row) -> dict:
    """Кабинет для показа на странице. Токен и ключи сюда не попадают никогда."""
    return {
        "id": row["id"],
        "title": row["title"],
        "platform": label(row["platform"]),
        "platform_id": row["platform"],
        "login": row["client_login"],
    }


@router.get("/platforms")
def platforms(user: sqlite3.Row = Depends(current_user)):
    """Площадки, которые можно подключить, и что каждая уже умеет."""
    return {"platforms": [{"id": k, **{x: v[x] for x in ("label", "keys", "ready", "can") if x in v}, "where": v.get("where", "")}
                          for k, v in PLATFORMS.items()]}


@router.post("/keys")
async def add_keys_account(data: NewKeysAccount, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db),
                           transport=Depends(platform_transport)):
    """Подключение VK Рекламы или Авито по ключам API: сохраняем только после того, как площадка ответила."""
    if data.platform not in KEY_CLIENTS:
        raise HTTPException(400, detail={"message": "Эта площадка подключается по-другому или пока не поддерживается."})
    client_cls, error_cls = KEY_CLIENTS[data.platform]
    try:
        async with client_cls(data.client_id, data.client_secret, transport) as client:
            await client.check()
            found = await client.list_campaigns(limit=10)
    except error_cls as e:
        raise HTTPException(502, detail={"message": e.message, "code": e.code}) from e
    s = get_settings()
    keys = json.dumps({"client_id": data.client_id.strip(), "client_secret": data.client_secret.strip()})
    title = data.title.strip() or f"Мой кабинет {label(data.platform)}"
    cur = conn.execute("INSERT INTO ad_accounts (user_id, title, platform, token_enc, client_login) VALUES (?, ?, ?, ?, '')",
                       (user["id"], title, data.platform, encrypt_token(keys, s.secret_key)))
    conn.commit()
    row = conn.execute("SELECT * FROM ad_accounts WHERE id = ?", (cur.lastrowid,)).fetchone()
    return {"account": _public(row), "campaigns_count": len(found)}


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
        async with factory(token, api_version=s.direct_api_version, login=login) as client:
            campaigns = await client.list_campaigns()
            units = _units(client)
    except DirectError as e:
        raise _direct_error(e) from e

    title = data.title.strip() or "Мой кабинет"
    cur = conn.execute(
        "INSERT INTO ad_accounts (user_id, title, token_enc, client_login) VALUES (?, ?, ?, ?)",
        (user["id"], title, encrypt_token(token, s.secret_key), login),
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
    transport=Depends(platform_transport),
):
    row = _own_account(conn, user, account_id)
    s = get_settings()
    token = decrypt_token(row["token_enc"], s.secret_key)
    if token is None:
        raise HTTPException(
            409,
            detail={"message": "Не удалось прочитать сохранённый токен. Удалите кабинет и подключите его заново."},
        )
    if row["platform"] in KEY_CLIENTS:   # VK Реклама и Авито: ключи API, а не токен Директа
        client_cls, error_cls = KEY_CLIENTS[row["platform"]]
        try:
            keys = json.loads(token)
            async with client_cls(keys["client_id"], keys["client_secret"], transport) as client:
                items = await client.list_campaigns()
        except error_cls as e:
            raise HTTPException(502, detail={"message": e.message, "code": e.code}) from e
        except (ValueError, KeyError) as exc:
            raise HTTPException(409, detail={"message": "Не удалось прочитать сохранённые ключи. Удалите кабинет и подключите заново."}) from exc
        return {"account": _public(row), "units": None,
                "campaigns": [{"id": c["id"], "name": c["name"], "type": c["kind"], "state": c["status"], "status": "",
                               "currency": "", "start_date": ""} for c in items]}
    try:
        async with factory(token, api_version=s.direct_api_version, login=row["client_login"]) as client:
            items = await client.list_campaigns()
            units = _units(client)
    except DirectError as e:
        raise _direct_error(e) from e
    return {"account": _public(row), "campaigns": [describe_campaign(c) for c in items], "units": units}
