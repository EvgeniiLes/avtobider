"""Поисковые запросы, по которым показывалась реклама: что тратит деньги впустую, что приводит обращения, что предлагает ИИ."""
import asyncio
import sqlite3
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.creative import fal
from app.db import get_db
from app.deps import client_factory, creative_transport, current_user
from app.direct import audience as aud
from app.direct import queries as q
from app.direct.client import DirectError
from app.direct.runner import account_token
from app.engine.util import EngineError
from app.routes.businesses import _own

router = APIRouter(prefix="/api/businesses/{business_id}/queries", tags=["queries"])


@router.get("")
def get_queries(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    biz = _own(conn, user, business_id)
    profile = aud.load(biz)
    launched = conn.execute("SELECT 1 FROM campaigns WHERE business_id = ? AND source = 'direct'", (business_id,)).fetchone() is not None
    return {
        "launched": launched, "synced": biz["queries_day"], "has_profile": profile is not None, "has_goal": bool(biz["metrika_goal_id"]),
        "ai_ready": bool(get_settings().fal_key), "queries": q.top_queries(conn, business_id),
        "waste": q.waste(conn, biz), "keywords": q.keyword_candidates(conn, biz, profile),
        "waste_from": round(q.waste_threshold(biz) / 1_000_000), "history": (profile or {}).get("history", []),
    }


@router.post("/analyze")
def analyze_queries(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db),
                    transport=Depends(creative_transport)):
    """ИИ отмечает запросы, по которым ищут не вашу услугу, и предлагает слова для минуса."""
    biz = _own(conn, user, business_id)
    s = get_settings()
    if not s.fal_key:
        raise HTTPException(503, detail={"message": "Разбор запросов пока не подключён на сервере: нужен ключ сервиса генерации."})
    queries = q.top_queries(conn, business_id, q.MAX_FOR_AI, min_clicks=1)
    if not queries:
        raise HTTPException(400, detail={"message": "Пока нечего разбирать: по рекламе ещё не было кликов. Запросы появятся, когда пойдут показы."})
    found = asyncio.run(q.classify(queries, biz, aud.load(biz), s.fal_key, s.fal_text_model, transport))
    if found is None:
        why = asyncio.run(fal.why_down(s.fal_key, s.fal_text_model, transport))
        raise HTTPException(502, detail={"message": why or "ИИ не ответил. Попробуйте ещё раз чуть позже."})
    conn.execute("INSERT INTO generations (business_id, kind, cents) VALUES (?, 'check', ?)", (business_id, fal.COST_CENTS["check"]))
    conn.commit()
    return {"minus": found, "checked": len(queries)}


class ApplyIn(BaseModel):
    minus: list[str] = Field(default_factory=list, max_length=100)
    keywords: list[str] = Field(default_factory=list, max_length=50)


@router.post("/apply")
def apply_queries(business_id: int, data: ApplyIn, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db),
                  factory=Depends(client_factory)):
    """Минус-слова и новые фразы в кабинет Директа и в профиль аудитории."""
    biz = _own(conn, user, business_id)
    profile = aud.load(biz)
    if profile is None:
        raise HTTPException(400, detail={"message": "Сначала составьте профиль аудитории на шаге «Запуск»: в нём хранятся ваши фразы и минус-слова."})
    if not biz["account_id"]:
        raise HTTPException(400, detail={"message": "Реклама для этого бизнеса ещё не создана, менять пока нечего."})
    if not (data.minus or data.keywords):
        raise HTTPException(400, detail={"message": "Ничего не выбрано."})
    s = get_settings()
    try:
        token, login = account_token(conn, biz["account_id"])

        async def go():
            async with factory(token, api_version=s.direct_api_version, login=login) as client:
                return await q.apply_changes(client, conn, biz, profile, data.minus, data.keywords, date.today().isoformat())

        return asyncio.run(go())
    except q.QueriesError as e:
        raise HTTPException(400, detail={"message": e.message})
    except (EngineError, DirectError) as e:
        raise HTTPException(400, detail={"message": e.message})
