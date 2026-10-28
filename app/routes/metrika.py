"""Подключение Яндекс Метрики: выбор счётчика и цели «обращение»."""
import asyncio
import sqlite3

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.db import get_db
from app.deps import client_factory, current_user, yandex_http
from app.direct.client import DirectError
from app.direct.runner import account_token
from app.engine.util import EngineError
from app.routes.businesses import _own

router = APIRouter(prefix="/api/businesses/{business_id}/metrika", tags=["metrika"])
BASE = "https://api-metrika.yandex.net/management/v1"

HOW_TO = (
    "Чтобы сервис видел обращения, на вашем сайте должен стоять счётчик Яндекс Метрики и цель «Обращение». "
    "Создайте цель в Метрике типа «JavaScript-событие» с идентификатором lead и вызывайте "
    "ym(НОМЕР_СЧЁТЧИКА, 'reachGoal', 'lead') при заявке или звонке. Потом выберите счётчик и цель здесь."
)


def _token(conn, user, biz) -> str:
    account = biz["account_id"]
    if not account:
        row = conn.execute("SELECT id FROM ad_accounts WHERE user_id = ? ORDER BY id LIMIT 1", (user["id"],)).fetchone()
        account = row["id"] if row else None
    if not account:
        raise HTTPException(400, detail={"message": "Сначала подключите рекламный кабинет Яндекс Директа: через него сервис получает доступ и к Метрике."})
    try:
        return account_token(conn, account)[0]
    except EngineError as e:
        raise HTTPException(409, detail={"message": e.message})


async def _get(http: httpx.AsyncClient, token: str, path: str) -> dict:
    try:
        r = await http.get(f"{BASE}{path}", headers={"Authorization": f"OAuth {token}"})
    except httpx.HTTPError:
        raise HTTPException(502, detail={"message": "Не удалось связаться с Яндекс Метрикой. Попробуйте позже."})
    if r.status_code in (401, 403):
        raise HTTPException(409, detail={"message": "У сервиса нет доступа к Метрике. Подключите кабинет заново и разрешите доступ."})
    if r.status_code != 200:
        raise HTTPException(502, detail={"message": "Яндекс Метрика ответила ошибкой. Попробуйте позже."})
    return r.json()


@router.get("/counters")
def counters(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db),
             http: httpx.AsyncClient = Depends(yandex_http)):
    biz = _own(conn, user, business_id)
    token = _token(conn, user, biz)

    async def go():
        async with http:
            return await _get(http, token, "/counters?per_page=100")

    data = asyncio.run(go())
    items = [{"id": str(c["id"]), "name": c.get("name") or c.get("site") or str(c["id"]), "site": c.get("site", "")}
             for c in data.get("counters", [])]
    return {"counters": items, "selected": {"counter": biz["metrika_counter_id"], "goal": biz["metrika_goal_id"]}, "how_to": HOW_TO}


@router.get("/counters/{counter_id}/goals")
def goals(business_id: int, counter_id: str, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db),
          http: httpx.AsyncClient = Depends(yandex_http)):
    biz = _own(conn, user, business_id)
    token = _token(conn, user, biz)

    async def go():
        async with http:
            return await _get(http, token, f"/counter/{int(counter_id)}/goals")

    data = asyncio.run(go())
    return {"goals": [{"id": str(g["id"]), "name": g.get("name", str(g["id"]))} for g in data.get("goals", [])]}


class MetrikaIn(BaseModel):
    counter_id: str
    goal_id: str


@router.put("")
def save(business_id: int, data: MetrikaIn, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db),
         http: httpx.AsyncClient = Depends(yandex_http), factory=Depends(client_factory)):
    biz = _own(conn, user, business_id)
    token = _token(conn, user, biz)
    if not (data.counter_id.isdigit() and data.goal_id.isdigit()):
        raise HTTPException(400, detail={"message": "Выберите счётчик и цель из списка."})

    async def check():
        async with http:
            found = await _get(http, token, f"/counter/{data.counter_id}/goals")
        if data.goal_id not in {str(g["id"]) for g in found.get("goals", [])}:
            raise HTTPException(400, detail={"message": "Такой цели нет в этом счётчике. Выберите цель из списка."})

    asyncio.run(check())

    # Директ считает обращения только для кампаний, к которым привязан счётчик: привязываем к уже созданным.
    units = conn.execute("SELECT DISTINCT external_campaign_id FROM ad_units WHERE business_id = ? AND external_campaign_id IS NOT NULL", (business_id,)).fetchall()
    if units:
        s = get_settings()

        async def attach():
            async with factory(token, api_version=s.direct_api_version) as client:
                for u in units:
                    await client.set_counters(u["external_campaign_id"], [int(data.counter_id)])

        try:
            asyncio.run(attach())
        except DirectError as e:
            raise HTTPException(400, detail={"message": f"Не получилось привязать счётчик к кампаниям: {e.message}"})

    conn.execute("UPDATE businesses SET metrika_counter_id = ?, metrika_goal_id = ? WHERE id = ?", (data.counter_id, data.goal_id, business_id))
    conn.commit()
    return {"ok": True}


@router.delete("")
def clear(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    _own(conn, user, business_id)
    conn.execute("UPDATE businesses SET metrika_counter_id = '', metrika_goal_id = '' WHERE id = ?", (business_id,))
    conn.commit()
    return {"ok": True}
