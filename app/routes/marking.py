"""Данные рекламодателя для маркировки: проверка и внесение прямо с сайта через API Директа."""
import asyncio
import re
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.db import get_db
from app.deps import client_factory, current_user
from app.direct.client import DirectError
from app.direct.marking import KINDS, is_filled, valid_inn
from app.direct.runner import account_token
from app.engine.util import EngineError
from app.routes.businesses import _own
from app.routes.launch import _fail, _pick_account

router = APIRouter(prefix="/api/businesses/{business_id}/marking", tags=["marking"])


def _read(conn, user, biz, factory) -> dict:
    account = _pick_account(conn, user, biz, None)
    token, login = account_token(conn, account["id"])
    s = get_settings()

    async def go():
        async with factory(token, api_version=s.direct_api_version, login=login) as client:
            return await client.advertiser_data()

    return asyncio.run(go())


def _public(data: dict) -> dict:
    """Что показываем на странице: ИНН не показываем, только факт, что он внесён."""
    return {"filled": is_filled(data), "tin_type": data["tin_type"], "has_tin": data["has_tin"],
            "name": data["org_name"], "has_phone": data["has_phone"], "kinds": {k: v[2] for k, v in KINDS.items()}}


@router.get("")
def read(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db),
         factory=Depends(client_factory)):
    """Смотрит в кабинете Директа, внесены ли данные рекламодателя. Если внесены, запуск больше не просит это подтверждать."""
    biz = _own(conn, user, business_id)
    try:
        data = _read(conn, user, biz, factory)
    except (EngineError, DirectError) as e:
        raise _fail(e)
    if is_filled(data) and not biz["marking_confirmed"]:
        conn.execute("UPDATE businesses SET marking_confirmed = 1 WHERE id = ?", (business_id,))
        conn.commit()
    return _public(data)


class MarkingIn(BaseModel):
    kind: str = Field(pattern="^(person|ip|org)$")
    tin: str = Field(max_length=20)
    name: str = Field(min_length=2, max_length=255)
    phone: str = Field(default="", max_length=30)


@router.post("")
def save(business_id: int, data: MarkingIn, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db),
         factory=Depends(client_factory)):
    """Вносит данные рекламодателя в кабинет Директа. ИНН у нас не сохраняется."""
    biz = _own(conn, user, business_id)
    tin_type, length, label = KINDS[data.kind]
    tin = re.sub(r"\s", "", data.tin)
    if not valid_inn(tin, length):
        raise HTTPException(400, detail={"message": f"ИНН указан неверно. Для вида «{label}» нужно {length} цифр, проверьте, нет ли опечатки."})
    phone = re.sub(r"[^\d+]", "", data.phone)
    if data.phone and not re.fullmatch(r"\+?\d{10,15}", phone):
        raise HTTPException(400, detail={"message": "Телефон указан неверно. Напишите его в формате +79001234567 или оставьте поле пустым."})
    account = _pick_account(conn, user, biz, None)
    token, login = account_token(conn, account["id"])
    s = get_settings()

    async def go():
        async with factory(token, api_version=s.direct_api_version, login=login) as client:
            await client.set_advertiser_data(tin_type, tin, data.name.strip(), phone)
            return await client.advertiser_data()

    try:
        after = asyncio.run(go())
    except (EngineError, DirectError) as e:
        raise _fail(e)
    if is_filled(after):
        conn.execute("UPDATE businesses SET marking_confirmed = 1 WHERE id = ?", (business_id,))
        conn.commit()
    return {**_public(after), "message": "Данные рекламодателя внесены в Яндекс Директ." if is_filled(after)
            else "Директ принял запрос, но данные пока не видны. Обновите страницу через минуту."}
