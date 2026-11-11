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
from app.direct import extras as ad_extras
from app.direct.bids import ensure_bids
from app.direct.client import DirectError
from app.direct.launch import (STATE_LABELS, add_ads, existing_campaign, group_sizes, launch, new_campaigns_for, new_variants, plan_launch,
                               split_campaign, tagged_href, text_candidates)
from app.direct.refresh import refresh_campaigns
from app.direct.runner import account_token, sync_and_run
from app.engine.util import EngineError, MICRO, plural
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


def _with_extras(conn, biz, plan, transport=None) -> None:
    """Второй заголовок, уточнения и отображаемая ссылка: берём сохранённые для этих заголовков или просим ИИ (один раз). Нет ИИ или сбой: без дополнений."""
    titles = [v["title"] for v in plan.variants[:plan.count]]
    saved = ad_extras.load(biz)
    if not ad_extras.covers(saved, titles):
        s = get_settings()
        fresh = asyncio.run(ad_extras.generate(biz, titles, s.fal_key, s.fal_text_model, transport))
        if fresh is not None:
            fresh = {**fresh, "applied": (saved or {}).get("applied", [])}   # какие объявления в Директе уже получили дополнения: при новом составлении не теряем
            ad_extras.save(conn, biz["id"], fresh)
            saved = fresh
    plan.extras = saved if ad_extras.covers(saved, titles) else None


def _plan_for(conn, user, biz, account_id, factory, transport=None, ad_format: str | None = None, separate: bool | None = None):
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
    plan = plan_launch(biz, region, custom=custom, audience=aud.load(biz), district_ids=district_ids, responsive=(ad_format or biz["ad_format"]) == "responsive",
                       separate=bool(biz["separate_campaigns"]) if separate is None else separate, **kwargs)
    _with_extras(conn, biz, plan, transport)
    drawn = {(r["title"], r["body"]) for r in conn.execute(   # только проверка по базе: сами картинки открывать не нужно, это долго
        "SELECT title, body FROM creatives WHERE business_id = ? AND kind = 'ai_banner' AND status IN ('draft', 'approved') AND asset_id IS NOT NULL", (biz["id"],))}
    missing = [v for v in plan.variants[:plan.count] if (v["title"], v["text"]) not in drawn]
    if missing:   # шаблонные баннеры в рекламу не идут: без нарисованного баннера объявление уйдёт без картинки
        plan.warnings = [*plan.warnings, f"У {len(missing)} из {plan.count} объявлений нет баннера. Нарисуйте их на шаге «Баннеры» (кнопка «Нарисовать»), иначе они пойдут без картинки."]
    return account, token, login, plan


def _plan_json(plan, account, marking_confirmed: bool = False, fresh: list[dict] | None = None, new_campaigns: int | None = None) -> dict:
    """fresh: реклама уже идёт, и показываем только объявления, которых в ней ещё нет (их и добавит запуск).
    new_campaigns: сколько новых кампаний создаст добавление (бюджет задаётся на каждую группу объявлений, то есть на каждую кампанию)."""
    groups = len(plan.groups or group_sizes(plan.count)) if fresh is None else (new_campaigns or 0)
    return {
        "adding": fresh is not None,
        "responsive": plan.responsive,
        "separate": plan.separate,
        "existing": 0 if fresh is None else plan.count - len(fresh),
        "marking": {"confirmed": marking_confirmed, "help": MARKING_HELP},
        "account": account["title"],
        "count": plan.count,
        "weekly": round(plan.campaign_micro / MICRO),   # бюджет одной группы объявлений
        "groups": groups,
        "group_names": [g["intent"] for g in (plan.groups or []) if g["intent"]],   # услуги, по которым раскладываем объявления (пусто: одна общая группа)
        "total": round(plan.campaign_micro * groups / MICRO),
        "city": plan.city,
        "districts": plan.districts or [],
        "by_district": bool(plan.region_ids),   # показ ограничен районами (иначе весь город)
        "link": plan.href or "Профиль организации в Яндекс Картах",
        "tagged": bool(plan.href) and tagged_href(plan.href) != plan.href,   # ссылка получит utm-метки (откуда пришёл человек)
        "variants": plan.variants if fresh is None else fresh,
        "extras": {"callouts": (plan.extras or {}).get("callouts") or [], "display_path": (plan.extras or {}).get("display_path") or "",
                   "title2": (plan.extras or {}).get("title2") or {}},
        "own_count": plan.own_count,
        "negatives": plan.negatives or [],
        "keywords": plan.keywords or [],
        "autotargeting": {"categories": [aud.CATEGORIES[k] for k, on in plan.autotargeting["categories"].items() if on],
                          "brands": [aud.BRANDS[k] for k, on in plan.autotargeting["brands"].items() if on]},
        "audience": {"summary": plan.audience["summary"], "confidence": aud.CONFIDENCE[plan.audience.get("confidence", "low")]} if plan.audience else None,
        "warnings": plan.warnings,
    }


@router.get("/launch/preview")
def preview(business_id: int, account_id: int | None = None, ad_format: str | None = None, separate: bool | None = None, user: sqlite3.Row = Depends(current_user),
            conn: sqlite3.Connection = Depends(get_db), factory=Depends(client_factory), transport=Depends(creative_transport)):
    biz = _own(conn, user, business_id)
    fill_districts(conn, business_id, transport)   # район не указан, а адрес есть: берём район из адреса
    biz = _own(conn, user, business_id)
    try:
        account, _, _, plan = _plan_for(conn, user, biz, account_id, factory, transport, ad_format, separate)
    except (EngineError, DirectError) as e:
        raise _fail(e)
    target = existing_campaign(conn, business_id)
    pairs = new_variants(conn, biz, plan) if target else None
    fresh = [v for _, v in pairs] if target else None
    return _plan_json(plan, account, bool(biz["marking_confirmed"]), fresh, new_campaigns_for(conn, biz, plan, pairs) if target else None)


MARKING_HELP = (
    "Закон требует маркировать интернет-рекламу. Директ делает это сам (создаёт метку erid и описание рекламы), "
    "но ему нужны данные рекламодателя: вид плательщика, ИНН и имя или название. Внесите их прямо здесь, "
    "мы передадим их в ваш кабинет Директа и не сохраним у себя. Или заполните «Данные рекламодателя» в самом Директе (нажмите на свой логин)."
)


class LaunchIn(BaseModel):
    account_id: int | None = None
    marking_confirmed: bool = False
    ad_format: str | None = None   # responsive: комбинаторные объявления (два заголовка и два текста в одном); пусто: как у бизнеса
    separate: bool | None = None   # каждое объявление в своей кампании; пусто: как у бизнеса


@router.post("/launch")
def start_launch(business_id: int, data: LaunchIn, user: sqlite3.Row = Depends(current_user),
                 conn: sqlite3.Connection = Depends(get_db), factory=Depends(client_factory), transport=Depends(creative_transport)):
    biz = _own(conn, user, business_id)
    target = existing_campaign(conn, business_id)
    if target is None and conn.execute("SELECT 1 FROM campaigns WHERE business_id = ? AND source = 'direct'", (business_id,)).fetchone():
        raise HTTPException(400, detail={"message": "Реклама для этого бизнеса уже создана. Её состояние видно на вкладке «Статистика».", "code": "already"})
    if not (data.marking_confirmed or biz["marking_confirmed"]):
        raise HTTPException(400, detail={"message": "Сначала заполните «Данные рекламодателя» в Яндекс Директе: без них рекламу нельзя маркировать. " + MARKING_HELP, "code": "marking"})
    fill_districts(conn, business_id, transport)
    biz = _own(conn, user, business_id)
    if target is None and aud.load(biz) is None and make_profile(conn, biz, transport):   # профиль ещё не составлен: делаем его сами, чтобы запуск шёл с фразами и минус-словами
        biz = _own(conn, user, business_id)
    try:
        if data.ad_format in ("responsive", "text"):
            conn.execute("UPDATE businesses SET ad_format = ? WHERE id = ?", ("responsive" if data.ad_format == "responsive" else "", business_id))
            conn.commit()
            biz = _own(conn, user, business_id)
        if data.separate is not None:
            conn.execute("UPDATE businesses SET separate_campaigns = ? WHERE id = ?", (1 if data.separate else 0, business_id))
            conn.commit()
            biz = _own(conn, user, business_id)
        account, token, login, plan = _plan_for(conn, user, biz, data.account_id, factory, transport)
        s = get_settings()
        today = date.today().isoformat()

        async def finish(client):
            await ensure_bids(client, conn, biz["id"], today)   # корректировки по полу и возрасту ставятся сразу, без кнопки
            for row in conn.execute("SELECT DISTINCT external_campaign_id FROM ad_units WHERE business_id = ? AND external_campaign_id IS NOT NULL", (biz["id"],)).fetchall():
                try:
                    await client.ensure_alerts(row["external_campaign_id"])   # мониторинг сайта и почтовые предупреждения; кампании уже созданы, поэтому сбой здесь запуск не ломает: следующая синхронизация повторит и покажет ошибку
                except DirectError:
                    pass

        async def add():   # реклама уже идёт: новые объявления встают в последнюю группу, пока в ней есть место, остальные идут в новую кампанию со своим бюджетом
            async with factory(token, api_version=s.direct_api_version, login=login) as client:
                created = await add_ads(client, conn, biz, target, plan, today)
                await finish(client)
                return created

        async def go():
            async with factory(token, api_version=s.direct_api_version, login=login) as client:
                created = await launch(client, conn, biz, account["id"], plan, today)
                await finish(client)
                return created

        if target is not None:
            unit_ids = asyncio.run(add())
            return {"created": len(unit_ids), "message": f"Добавили в рекламу {plural(len(unit_ids), 'объявление', 'объявления', 'объявлений')}: они отправлены на проверку в Яндекс."}
        unit_ids = asyncio.run(go())
        conn.execute("UPDATE businesses SET marking_confirmed = 1 WHERE id = ?", (business_id,))
        reset_demo(conn, business_id)  # примерные данные заменяются настоящей рекламой
    except (EngineError, DirectError) as e:
        raise _fail(e)
    groups = conn.execute(f"SELECT COUNT(DISTINCT campaign_id) FROM ad_units WHERE id IN ({','.join('?' * len(unit_ids))})", unit_ids).fetchone()[0] if unit_ids else 1
    message = ("Кампания с объявлениями создана и отправлена на проверку в Яндекс." if groups == 1
               else f"Созданы кампании по группам объявлений ({groups}), у каждой свой бюджет. Они отправлены на проверку в Яндекс.")
    return {"created": len(unit_ids), "message": message}


class SplitIn(BaseModel):
    campaign_id: int


@router.post("/launch/split")
def split(business_id: int, data: SplitIn, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db),
          factory=Depends(client_factory), transport=Depends(creative_transport)):
    """Сравнение на уже идущей рекламе: объявления общей кампании переезжают каждое в свою новую кампанию, старая останавливается."""
    biz = _own(conn, user, business_id)
    try:
        account, token, login, plan = _plan_for(conn, user, biz, None, factory, transport, None, True)
        s = get_settings()
        today = date.today().isoformat()

        async def go():
            async with factory(token, api_version=s.direct_api_version, login=login) as client:
                created = await split_campaign(client, conn, biz, data.campaign_id, plan, today)
                await ensure_bids(client, conn, biz["id"], today)
                for row in conn.execute("SELECT DISTINCT external_campaign_id FROM ad_units WHERE business_id = ? AND external_campaign_id IS NOT NULL", (biz["id"],)).fetchall():
                    try:
                        await client.ensure_alerts(row["external_campaign_id"])
                    except DirectError:
                        pass
                return created

        unit_ids = asyncio.run(go())
    except (EngineError, DirectError) as e:
        raise _fail(e)
    conn.execute("UPDATE businesses SET separate_campaigns = 1 WHERE id = ?", (business_id,))
    conn.commit()
    return {"created": len(unit_ids), "message": f"Создали {plural(len(unit_ids), 'кампанию', 'кампании', 'кампаний')} по одному объявлению, старую остановили. "
            "Новые объявления на проверке в Яндексе. Через несколько дней после старта сервис сравнит их и оставит лучшее."}


@router.post("/refresh-campaigns")
def refresh_campaigns_now(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db),
                          factory=Depends(client_factory), transport=Depends(creative_transport)):
    """Переносит новые данные в уже созданные кампании: дополнения объявлений, новые фразы, минус-слова, категории автотаргетинга. Ничего не удаляет."""
    biz = _own(conn, user, business_id)
    account = _pick_account(conn, user, biz, None)
    token, login = account_token(conn, account["id"])
    s = get_settings()

    async def go():
        async with factory(token, api_version=s.direct_api_version, login=login) as client:
            return await refresh_campaigns(client, conn, biz, date.today().isoformat(), transport)

    try:
        report = asyncio.run(go())
    except (EngineError, DirectError) as e:
        raise _fail(e)
    message = "; ".join(report["changes"]) or ("Всё уже актуально, добавлять нечего." if not report["problems"] else "")
    return {**report, "message": message}


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
