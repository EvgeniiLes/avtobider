"""Запуск настоящей рекламы бизнеса в Яндекс Директе."""
import asyncio
import sqlite3
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.db import get_db
from app.deps import client_factory, current_user
from app.direct.client import DirectError
from app.direct.launch import STATE_LABELS, launch, plan_launch
from app.direct.runner import account_token, sync_and_run
from app.engine.util import EngineError, MICRO
from app.platforms.simulator import reset_demo
from app.routes.businesses import _own

router = APIRouter(prefix="/api/businesses/{business_id}", tags=["launch"])


def _fail(e: EngineError | DirectError) -> HTTPException:
    status = 409 if getattr(e, "code", "") in ("expired", "bad_token") else 400
    return HTTPException(status, detail={"message": e.message, "code": getattr(e, "code", None)})


def _pick_account(conn: sqlite3.Connection, user, biz, account_id: int | None) -> sqlite3.Row:
    if account_id:
        row = conn.execute("SELECT * FROM ad_accounts WHERE id = ? AND user_id = ?", (account_id, user["id"])).fetchone()
    elif biz["account_id"]:
        row = conn.execute("SELECT * FROM ad_accounts WHERE id = ? AND user_id = ?", (biz["account_id"], user["id"])).fetchone()
    else:
        row = conn.execute("SELECT * FROM ad_accounts WHERE user_id = ? ORDER BY id LIMIT 1", (user["id"],)).fetchone()
    if row is None:
        raise HTTPException(400, detail={"message": "Сначала подключите рекламный кабинет Яндекс Директа на вкладке «Рекламные кабинеты».", "code": "no_account"})
    return row


def _plan_for(conn, user, biz, account_id, factory):
    """Строит план запуска, обращаясь к Директу за регионом и минимальным бюджетом."""
    account = _pick_account(conn, user, biz, account_id)
    token, login = account_token(conn, account["id"])
    s = get_settings()

    async def go():
        async with factory(token, api_version=s.direct_api_version, login=login) as client:
            region = await client.find_region_id(biz["city"]) if biz["city"].strip() else None
            minimum = await client.min_weekly_budget("RUB")
            return region, minimum

    region, minimum = asyncio.run(go())
    kwargs = {"min_weekly_micro": minimum} if minimum else {}
    approved = conn.execute(
        "SELECT title, body, angle FROM creatives WHERE business_id = ? AND kind = 'text' AND status = 'approved' ORDER BY id", (biz["id"],)
    ).fetchall()
    custom = [{"title": r["title"], "text": r["body"], "angle": r["angle"]} for r in approved]
    return account, token, login, plan_launch(biz, region, custom=custom, **kwargs)


def _plan_json(plan, account, marking_confirmed: bool = False) -> dict:
    return {
        "marking": {"confirmed": marking_confirmed, "help": MARKING_HELP},
        "account": account["title"],
        "count": plan.count,
        "per_unit": round(plan.per_unit_micro / MICRO),
        "total": round(plan.per_unit_micro * plan.count / MICRO),
        "city": plan.city,
        "link": plan.href or "Профиль организации в Яндекс Картах",
        "variants": plan.variants,
        "own_count": plan.own_count,
        "negatives": plan.negatives or [],
        "warnings": plan.warnings,
    }


@router.get("/launch/preview")
def preview(business_id: int, account_id: int | None = None, user: sqlite3.Row = Depends(current_user),
            conn: sqlite3.Connection = Depends(get_db), factory=Depends(client_factory)):
    biz = _own(conn, user, business_id)
    try:
        account, _, _, plan = _plan_for(conn, user, biz, account_id, factory)
    except (EngineError, DirectError) as e:
        raise _fail(e)
    return _plan_json(plan, account, bool(biz["marking_confirmed"]))


MARKING_HELP = (
    "Закон требует маркировать интернет-рекламу. Директ делает это сам (создаёт метку erid и описание рекламы), "
    "но ему нужны данные рекламодателя. Откройте Яндекс Директ, нажмите на свой логин, выберите «Данные рекламодателя», "
    "укажите имя или название, ИНН и телефон, оставьте включённым «Я — конечный рекламодатель» и сохраните."
)


class LaunchIn(BaseModel):
    account_id: int | None = None
    marking_confirmed: bool = False


@router.post("/launch")
def start_launch(business_id: int, data: LaunchIn, user: sqlite3.Row = Depends(current_user),
                 conn: sqlite3.Connection = Depends(get_db), factory=Depends(client_factory)):
    biz = _own(conn, user, business_id)
    if conn.execute("SELECT 1 FROM campaigns WHERE business_id = ? AND source = 'direct'", (business_id,)).fetchone():
        raise HTTPException(400, detail={"message": "Реклама для этого бизнеса уже создана. Её состояние видно на вкладке «Статистика».", "code": "already"})
    if not (data.marking_confirmed or biz["marking_confirmed"]):
        raise HTTPException(400, detail={"message": "Сначала заполните «Данные рекламодателя» в Яндекс Директе: без них рекламу нельзя маркировать. " + MARKING_HELP, "code": "marking"})
    try:
        account, token, login, plan = _plan_for(conn, user, biz, data.account_id, factory)
        s = get_settings()
        today = date.today().isoformat()

        async def go():
            async with factory(token, api_version=s.direct_api_version, login=login) as client:
                return await launch(client, conn, biz, account["id"], plan, today)

        unit_ids = asyncio.run(go())
        conn.execute("UPDATE businesses SET marking_confirmed = 1 WHERE id = ?", (business_id,))
        reset_demo(conn, business_id)  # примерные данные заменяются настоящей рекламой
    except (EngineError, DirectError) as e:
        raise _fail(e)
    return {"created": len(unit_ids), "message": "Объявления созданы и отправлены на проверку в Яндекс."}


@router.post("/sync")
def sync_now(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    biz = _own(conn, user, business_id)
    result = sync_and_run(conn, biz)
    if result["error"]:
        raise HTTPException(400, detail={"message": result["error"]})
    return result


@router.get("/state-labels")
def state_labels(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    _own(conn, user, business_id)
    return STATE_LABELS
