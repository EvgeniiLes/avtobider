"""Статистика, пример данных и история изменений с откатом."""
import json
import sqlite3
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.db import get_db
from app.deps import current_user
from app.engine.config import EngineConfig
from app.engine.cycle import CLASS_LABELS, rollback, run_cycle, target_cpa_micro, target_lead_micro, unit_stats
from app.engine.util import EngineError, MICRO, day_add
from app.platforms.simulator import SimulatedPlatform, advance_day, get_today, reset_demo, start_demo
from app.routes.businesses import _own

router = APIRouter(prefix="/api/businesses/{business_id}", tags=["stats"])

ACTION_LABELS = {
    "pause": "Остановили",
    "set_budget": "Изменили бюджет",
    "start_test": "Запустили сравнение",
    "rollback": "Отмена",
}
STATUS = {"conflict": 409, "not_found": 404}


def _fail(e: EngineError) -> HTTPException:
    return HTTPException(STATUS.get(e.code, 400), detail={"message": e.message, "code": e.code})


def _rub(micro: float) -> float:
    return round(micro / MICRO, 2)


def _ratio(spend: int, count: int) -> float | None:
    return _rub(spend / count) if count else None


@router.get("/overview")
def overview(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    biz = _own(conn, user, business_id)
    cfg = EngineConfig()
    today = get_today(conn, business_id)
    result = {"example": bool(today), "today": today, "units": [], "totals": None, "budget": None, "needs": ""}
    try:
        target_cpa_micro(biz)
    except EngineError as e:
        result["needs"] = e.message
    if not today:
        return result

    start = day_add(today, -(cfg.window_days - 1))
    units = conn.execute("SELECT * FROM ad_units WHERE business_id = ? ORDER BY id", (business_id,)).fetchall()
    total = {"spend": 0, "clicks": 0, "leads": 0, "clients": 0}
    in_test = {
        uid for r in conn.execute("SELECT unit_a, unit_b FROM experiments WHERE business_id = ? AND status = 'running'", (business_id,))
        for uid in (r["unit_a"], r["unit_b"])
    }
    for u in units:
        s = unit_stats(conn, u["id"], start, today)
        for k in total:
            total[k] += s[k]
        ev = conn.execute("SELECT cls FROM evaluations WHERE unit_id = ? ORDER BY day DESC, id DESC LIMIT 1", (u["id"],)).fetchone()
        cls = ev["cls"] if ev else "waiting"
        result["units"].append({
            "id": u["id"],
            "title": u["title"],
            "active": u["state"] == "active",
            "variant": u["variant_of"] is not None,
            "in_test": u["id"] in in_test,
            "weekly_budget": _rub(u["budget_micro"]),
            "spend": _rub(s["spend"]),
            "clicks": s["clicks"],
            "leads": s["leads"],
            "clients": s["clients"],
            "cost_per_lead": _ratio(s["spend"], s["leads"]),
            "cost_per_client": _ratio(s["spend"], s["clients"]),
            "verdict": CLASS_LABELS[cls] if u["state"] == "active" else "Остановлено",
            "verdict_code": cls if u["state"] == "active" else "paused",
        })
    result["totals"] = {
        "spend": _rub(total["spend"]),
        "clicks": total["clicks"],
        "leads": total["leads"],
        "clients": total["clients"],
        "cost_per_lead": _ratio(total["spend"], total["leads"]),
        "cost_per_client": _ratio(total["spend"], total["clients"]),
        "days": cfg.window_days,
    }
    week_spend = sum(unit_stats(conn, u["id"], day_add(today, -6), today)["spend"] for u in units)
    budget = biz["weekly_budget_micro"]
    result["budget"] = {
        "weekly": _rub(budget),
        "spent_week": _rub(week_spend),
        "percent": round(week_spend / budget * 100) if budget else 0,
    }
    return result


class Advance(BaseModel):
    days: int = Field(default=1, ge=1, le=30)


@router.post("/sim/start")
def sim_start(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    biz = _own(conn, user, business_id)
    try:
        start_demo(conn, biz, target_lead_micro(biz))
    except EngineError as e:
        raise _fail(e)
    return {"ok": True}


@router.post("/sim/advance")
def sim_advance(
    business_id: int,
    data: Advance,
    user: sqlite3.Row = Depends(current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    biz = _own(conn, user, business_id)
    platform = SimulatedPlatform(conn)
    made = 0
    try:
        for _ in range(data.days):
            today = advance_day(conn, biz)
            made += len(run_cycle(conn, biz, platform, today))
    except EngineError as e:
        raise _fail(e)
    return {"today": today, "changes_made": made}


@router.post("/sim/reset")
def sim_reset(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    _own(conn, user, business_id)
    reset_demo(conn, business_id)
    return {"ok": True}


@router.get("/changes")
def changes(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    _own(conn, user, business_id)
    rows = conn.execute(
        "SELECT c.*, u.title AS unit_title FROM changes c LEFT JOIN ad_units u ON u.id = c.unit_id "
        "WHERE c.business_id = ? ORDER BY c.id DESC LIMIT 200",
        (business_id,),
    ).fetchall()
    return {
        "changes": [
            {
                "id": r["id"],
                "day": r["day"],
                "who": "Вы" if r["actor"] == "user" else "Автобидер",
                "action": ACTION_LABELS.get(r["action"], r["action"]),
                "unit": r["unit_title"] or "",
                "reason": r["reason_text"],
                "reverted": r["reverted_by"] is not None,
                "can_rollback": r["reverted_by"] is None and r["action"] != "rollback",
            }
            for r in rows
        ]
    }


class RollbackIn(BaseModel):
    force: bool = False


@router.post("/changes/{change_id}/rollback")
def change_rollback(
    business_id: int,
    change_id: int,
    data: RollbackIn,
    user: sqlite3.Row = Depends(current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    biz = _own(conn, user, business_id)
    today = get_today(conn, business_id) or date.today().isoformat()
    try:
        rollback(conn, SimulatedPlatform(conn), biz, change_id, today, data.force)
    except EngineError as e:
        raise _fail(e)
    return {"ok": True}
