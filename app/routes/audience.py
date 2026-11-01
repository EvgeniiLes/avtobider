"""Профиль целевой аудитории: ИИ составляет, владелец правит, из профиля берутся фразы, минус-слова и автотаргетинг запуска."""
import asyncio
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.creative import fal
from app.db import get_db
from app.deps import client_factory, creative_transport, current_user
from app.direct import audience as aud
from app.direct import bids
from app.direct.client import DirectError
from app.direct.runner import account_token
from app.engine.util import EngineError
from app.routes.businesses import _own

router = APIRouter(prefix="/api/businesses/{business_id}/audience", tags=["audience"])

KEEP = ("launched", "history", "bids")   # что уже случилось в кабинете: новый профиль это не стирает


def _campaign_id(conn: sqlite3.Connection, business_id: int) -> int | None:
    row = conn.execute("SELECT external_campaign_id FROM ad_units WHERE business_id = ? AND external_campaign_id IS NOT NULL LIMIT 1", (business_id,)).fetchone()
    return row["external_campaign_id"] if row else None


def _full(conn: sqlite3.Connection, biz, profile: dict | None) -> dict:
    return {**aud.view(biz, profile), "ai_ready": bool(get_settings().fal_key), "bids": bids.describe(profile, _campaign_id(conn, biz["id"]) is not None)}


def make_profile(conn: sqlite3.Connection, biz, transport=None) -> dict | None:
    """Просит ИИ составить профиль и сохраняет его. None, если ключа сервиса нет или ИИ не ответил."""
    s = get_settings()
    if not s.fal_key:
        return None
    profile = asyncio.run(aud.generate_profile(biz, s.fal_key, s.fal_text_model, transport))
    if profile:
        old = aud.load(biz) or {}
        profile.update({k: old[k] for k in KEEP if k in old})
        aud.save(conn, biz["id"], profile)
        conn.execute("INSERT INTO generations (business_id, kind, cents) VALUES (?, 'check', ?)", (biz["id"], fal.COST_CENTS["check"]))
        conn.commit()
    return profile


@router.get("")
def get_audience(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    biz = _own(conn, user, business_id)
    return _full(conn, biz, aud.load(biz))


@router.post("")
def build_audience(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db),
                   transport=Depends(creative_transport)):
    """Составляет профиль заново (прежние правки владельца заменяются)."""
    biz = _own(conn, user, business_id)
    if not get_settings().fal_key:
        raise HTTPException(503, detail={"message": "Разбор аудитории пока не подключён на сервере: нужен ключ сервиса генерации."})
    if not (biz["description"] or "").strip():
        raise HTTPException(400, detail={"message": "Сначала опишите бизнес на странице «Бизнес»: по описанию ИИ определяет, кто ваш клиент."})
    profile = make_profile(conn, biz, transport)
    if profile is None:
        raise HTTPException(502, detail={"message": "ИИ не ответил. Попробуйте ещё раз чуть позже."})
    return _full(conn, biz, aud.load(_own(conn, user, business_id)))


class AudienceIn(BaseModel):
    summary: str | None = Field(default=None, max_length=600)
    gender: str | None = None
    ages: list[str] | None = Field(default=None, max_length=10)
    income: str | None = None
    keywords: list[str] | None = Field(default=None, max_length=100)
    negatives: list[str] | None = Field(default=None, max_length=200)
    categories: dict[str, bool] | None = None
    brands: dict[str, bool] | None = None


def _norm(phrase: str) -> str:
    return " ".join(str(phrase).lower().replace(" ", " ").split())


@router.put("")
def edit_audience(business_id: int, data: AudienceIn, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    biz = _own(conn, user, business_id)
    profile = aud.load(biz)
    if profile is None:
        raise HTTPException(400, detail={"message": "Профиля пока нет. Сначала составьте его кнопкой «Составить профиль»."})
    edits = {k: v for k, v in data.model_dump(exclude={"categories", "brands"}).items() if v is not None}
    if data.categories is not None or data.brands is not None:
        current = aud.autotargeting_for(profile)
        edits["autotargeting"] = {"categories": {**current["categories"], **(data.categories or {})},
                                  "brands": {**current["brands"], **(data.brands or {})}}
    updated = aud.apply_edits(profile, edits)
    aud.save(conn, business_id, updated)
    # что не прошло проверку Директа, возвращаем владельцу: пусть знает, что именно не попадёт в рекламу
    ignored = {
        "keywords": [k for k in (data.keywords or []) if _norm(k) not in updated["keywords"]],
        "negatives": [k for k in (data.negatives or []) if _norm(k) not in updated["negatives"]],
    }
    return {**_full(conn, biz, updated), "ignored": ignored}


def _with_client(conn: sqlite3.Connection, biz, factory, work):
    """Выполняет work(client, campaign_id) в кабинете Директа этого бизнеса; ошибки Директа возвращает владельцу понятным текстом."""
    campaign_id = _campaign_id(conn, biz["id"])
    if campaign_id is None or not biz["account_id"]:
        raise HTTPException(400, detail={"message": "Реклама для этого бизнеса ещё не создана: корректировки можно поставить после запуска."})
    s = get_settings()
    try:
        token, login = account_token(conn, biz["account_id"])

        async def go():
            async with factory(token, api_version=s.direct_api_version, login=login) as client:
                return await work(client, campaign_id)

        return asyncio.run(go())
    except (EngineError, DirectError) as e:
        raise HTTPException(400, detail={"message": e.message})


@router.post("/bids")
def put_bids(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db), factory=Depends(client_factory)):
    """Ставит на кампанию корректировки ставок по полу и возрасту из профиля (прежние по полу и возрасту заменяются)."""
    biz = _own(conn, user, business_id)
    profile = aud.load(biz)
    if profile is None:
        raise HTTPException(400, detail={"message": "Сначала составьте профиль аудитории."})
    if not bids.slices(profile):
        raise HTTPException(400, detail={"message": "В профиле не выбраны ни пол, ни возраст: корректировать ставки не к чему."})
    updated = _with_client(conn, biz, factory, lambda client, campaign_id: bids.apply_bids(client, conn, biz, profile, campaign_id))
    return _full(conn, biz, updated)


@router.delete("/bids")
def remove_bids(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db), factory=Depends(client_factory)):
    biz = _own(conn, user, business_id)
    profile = aud.load(biz)
    if profile is None:
        raise HTTPException(400, detail={"message": "Профиля пока нет."})
    updated = _with_client(conn, biz, factory, lambda client, campaign_id: bids.clear_bids(client, conn, biz, profile, campaign_id))
    return _full(conn, biz, updated)
