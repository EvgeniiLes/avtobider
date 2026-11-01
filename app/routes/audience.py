"""Профиль целевой аудитории: ИИ составляет, владелец правит, из профиля берутся фразы, минус-слова и автотаргетинг запуска."""
import asyncio
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.creative import fal
from app.db import get_db
from app.deps import creative_transport, current_user
from app.direct import audience as aud
from app.routes.businesses import _own

router = APIRouter(prefix="/api/businesses/{business_id}/audience", tags=["audience"])


def make_profile(conn: sqlite3.Connection, biz, transport=None) -> dict | None:
    """Просит ИИ составить профиль и сохраняет его. None, если ключа сервиса нет или ИИ не ответил."""
    s = get_settings()
    if not s.fal_key:
        return None
    profile = asyncio.run(aud.generate_profile(biz, s.fal_key, s.fal_text_model, transport))
    if profile:
        aud.save(conn, biz["id"], profile)
        conn.execute("INSERT INTO generations (business_id, kind, cents) VALUES (?, 'check', ?)", (biz["id"], fal.COST_CENTS["check"]))
        conn.commit()
    return profile


@router.get("")
def get_audience(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    biz = _own(conn, user, business_id)
    return {**aud.view(biz, aud.load(biz)), "ai_ready": bool(get_settings().fal_key)}


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
    return {**aud.view(biz, profile), "ai_ready": True}


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
    return {**aud.view(biz, updated), "ai_ready": bool(get_settings().fal_key), "ignored": ignored}
