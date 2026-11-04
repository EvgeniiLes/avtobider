"""Запуск настоящей рекламы бизнеса в Яндекс Директе."""
import asyncio
import sqlite3
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.db import get_db
from app.creative.angles import districts_of, is_nationwide
from app.deps import client_factory, creative_transport, current_user
from app.direct import audience as aud
from app.direct.bids import ensure_bids
from app.direct.client import DirectError
from app.direct.launch import STATE_LABELS, launch, plan_launch, tagged_href, text_candidates
from app.direct.runner import account_token, sync_and_run
from app.engine.util import EngineError, MICRO
from app.platforms.simulator import reset_demo
from app.routes.audience import make_profile
from app.routes.businesses import _own, fill_districts

router = APIRouter(prefix="/api/businesses/{business_id}", tags=["launch"])


def _fail(e: EngineError | DirectError) -> HTTPException:
    status = 409 if getattr(e, "code", "") in ("expired", "bad_token") else 400
    message = e.full_message() if isinstance(e, DirectError) else e.message   # ответ Директа показываем целиком
    return HTTPException(status, detail={"message": message, "code": getattr(e, "code", None)})


def _pick_account(conn: sqlite3.Connection, user, biz, account_id: int | None) -> sqlite3.Row:
    if account_id:
        row = conn.execute("SELECT * FROM ad_accounts WHERE id = ? AND user_id = ? AND platform = 'yandex_direct'", (account_id, user["id"])).fetchone()
    elif biz["account_id"]:
        row = conn.execute("SELECT * FROM ad_accounts WHERE id = ? AND user_id = ? AND platform = 'yandex_direct'", (biz["account_id"], user["id"])).fetchone()
    else:
        row = conn.execute("SELECT * FROM ad_accounts WHERE user_id = ? AND platform = 'yandex_direct' ORDER BY id LIMIT 1", (user["id"],)).fetchone()
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
            region = None if is_nationwide(biz) or not biz["city"].strip() else await client.find_region_id(biz["city"])   # вся Россия: регион задаст сам план
            minimum = await client.min_weekly_budget("RUB")
            districts = districts_of(biz)
            district_ids = await client.find_district_ids(region, districts) if region and districts else []   # районы как отдельные области показа
            return region, minimum, district_ids

    region, minimum, district_ids = asyncio.run(go())
    kwargs = {"min_weekly_micro": minimum} if minimum else {}
    custom = text_candidates(conn, biz)
    return account, token, login, plan_launch(biz, region, custom=custom, audience=aud.load(biz), district_ids=district_ids, **kwargs)


def _plan_json(plan, account, marking_confirmed: bool = False) -> dict:
    return {
        "marking": {"confirmed": marking_confirmed, "help": MARKING_HELP},
        "account": account["title"],
        "count": plan.count,
        "weekly": round(plan.campaign_micro / MICRO),
        "total": round(plan.campaign_micro / MICRO),
        "city": plan.city,
        "districts": plan.districts or [],
        "by_district": bool(plan.region_ids),   # показ ограничен районами (иначе весь город)
        "link": plan.href or "Профиль организации в Яндекс Картах",
        "tagged": bool(plan.href) and tagged_href(plan.href) != plan.href,   # ссылка получит utm-метки (откуда пришёл человек)
        "variants": plan.variants,
        "own_count": plan.own_count,
        "negatives": plan.negatives or [],
        "keywords": plan.keywords or [],
        "autotargeting": {"categories": [aud.CATEGORIES[k] for k, on in plan.autotargeting["categories"].items() if on],
                          "brands": [aud.BRANDS[k] for k, on in plan.autotargeting["brands"].items() if on]},
        "audience": {"summary": plan.audience["summary"], "confidence": aud.CONFIDENCE[plan.audience.get("confidence", "low")]} if plan.audience else None,
        "warnings": plan.warnings,
    }


@router.get("/launch/preview")
def preview(business_id: int, account_id: int | None = None, user: sqlite3.Row = Depends(current_user),
            conn: sqlite3.Connection = Depends(get_db), factory=Depends(client_factory), transport=Depends(creative_transport)):
    biz = _own(conn, user, business_id)
    fill_districts(conn, business_id, transport)   # район не указан, а адрес есть: берём район из адреса
    biz = _own(conn, user, business_id)
    try:
        account, _, _, plan = _plan_for(conn, user, biz, account_id, factory)
    except (EngineError, DirectError) as e:
        raise _fail(e)
    return _plan_json(plan, account, bool(biz["marking_confirmed"]))


MARKING_HELP = (
    "Закон требует маркировать интернет-рекламу. Директ делает это сам (создаёт метку erid и описание рекламы), "
    "но ему нужны данные рекламодателя: вид плательщика, ИНН и имя или название. Внесите их прямо здесь, "
    "мы передадим их в ваш кабинет Директа и не сохраним у себя. Или заполните «Данные рекламодателя» в самом Директе (нажмите на свой логин)."
)


class LaunchIn(BaseModel):
    account_id: int | None = None
    marking_confirmed: bool = False


@router.post("/launch")
def start_launch(business_id: int, data: LaunchIn, user: sqlite3.Row = Depends(current_user),
                 conn: sqlite3.Connection = Depends(get_db), factory=Depends(client_factory), transport=Depends(creative_transport)):
    biz = _own(conn, user, business_id)
    if conn.execute("SELECT 1 FROM campaigns WHERE business_id = ? AND source = 'direct'", (business_id,)).fetchone():
        raise HTTPException(400, detail={"message": "Реклама для этого бизнеса уже создана. Её состояние видно на вкладке «Статистика».", "code": "already"})
    if not (data.marking_confirmed or biz["marking_confirmed"]):
        raise HTTPException(400, detail={"message": "Сначала заполните «Данные рекламодателя» в Яндекс Директе: без них рекламу нельзя маркировать. " + MARKING_HELP, "code": "marking"})
    fill_districts(conn, business_id, transport)
    biz = _own(conn, user, business_id)
    if aud.load(biz) is None and make_profile(conn, biz, transport):   # профиль ещё не составлен: делаем его сами, чтобы запуск шёл с фразами и минус-словами
        biz = _own(conn, user, business_id)
    try:
        account, token, login, plan = _plan_for(conn, user, biz, data.account_id, factory)
        s = get_settings()
        today = date.today().isoformat()

        async def go():
            async with factory(token, api_version=s.direct_api_version, login=login) as client:
                created = await launch(client, conn, biz, account["id"], plan, today)
                await ensure_bids(client, conn, biz["id"], today)   # корректировки по полу и возрасту ставятся сразу, без кнопки
                for row in conn.execute("SELECT DISTINCT external_campaign_id FROM ad_units WHERE business_id = ? AND external_campaign_id IS NOT NULL", (biz["id"],)).fetchall():
                    try:
                        await client.ensure_alerts(row["external_campaign_id"])   # мониторинг сайта и почтовые предупреждения; кампания уже создана, поэтому сбой здесь запуск не ломает: следующая синхронизация повторит и покажет ошибку
                    except DirectError:
                        pass
                return created

        unit_ids = asyncio.run(go())
        conn.execute("UPDATE businesses SET marking_confirmed = 1 WHERE id = ?", (business_id,))
        reset_demo(conn, business_id)  # примерные данные заменяются настоящей рекламой
    except (EngineError, DirectError) as e:
        raise _fail(e)
    return {"created": len(unit_ids), "message": "Кампания с объявлениями создана и отправлена на проверку в Яндекс."}


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
