"""Статистика, пример данных и история изменений с откатом."""
import json
import sqlite3
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.db import get_db
from app.deps import current_user
from app.engine.config import EngineConfig
from app.engine.cycle import (
    CLASS_LABELS, approve, reject, resume_all, rollback, run_cycle, stop_all, target_cpa_micro, target_lead_micro, unit_stats,
)
from app.config import get_settings
from app.direct.client import DirectError
from app.direct.launch import STATE_LABELS
from app.direct.runner import account_token
from app.platforms.direct import DirectPlatform
from app.engine.util import EngineError, MICRO, day_add
from app.platforms.simulator import SimulatedPlatform, advance_day, get_today, reset_demo, start_demo
from app.routes.businesses import _own

router = APIRouter(prefix="/api/businesses/{business_id}", tags=["stats"])

ACTION_LABELS = {
    "pause": "Остановка",
    "set_budget": "Изменение бюджета",
    "start_test": "Сравнение двух вариантов",
    "rollback": "Отмена",
}
STATUS = {"conflict": 409, "stale": 409, "not_found": 404}


def _platform(conn: sqlite3.Connection, biz: sqlite3.Row):
    """Площадка бизнеса: настоящий Директ, если реклама создана там, иначе пример."""
    if conn.execute("SELECT 1 FROM campaigns WHERE business_id = ? AND source = 'direct'", (biz["id"],)).fetchone():
        token, login = account_token(conn, biz["account_id"])
        return DirectPlatform(conn, token, get_settings().direct_api_version, login)
    return SimulatedPlatform(conn)


def _fail(e: EngineError | DirectError) -> HTTPException:
    code = getattr(e, "code", None)
    return HTTPException(STATUS.get(code, 400), detail={"message": e.message, "code": code})


def _rub(micro: float) -> float:
    return round(micro / MICRO, 2)


def _ratio(spend: int, count: int) -> float | None:
    return _rub(spend / count) if count else None


def checklist(biz: sqlite3.Row, accounts: int, units: int) -> list[dict]:
    """Шаги до запуска рекламы простыми словами. Первый невыполненный шаг без пометки «скоро» показываем как следующий."""
    steps = [
        {"key": "described", "title": "Бизнес описан",
         "done": all([biz["name"], biz["kind"], biz["description"].strip(), biz["city"]]),
         "hint": "Заполните название, чем занимаетесь, расскажите о бизнесе и укажите город.", "page": "business"},
        {"key": "money", "title": "Бюджет и цена клиента заданы",
         "done": biz["weekly_budget_micro"] > 0 and (biz["target_cpa_micro"] > 0 or biz["avg_check_micro"] > 0),
         "hint": "Укажите бюджет на неделю и сколько готовы платить за клиента (или средний чек).", "page": "business"},
        {"key": "cabinet", "title": "Рекламный кабинет подключён", "done": accounts > 0,
         "hint": "Подключите кабинет Яндекс Директа на вкладке «Рекламные кабинеты».", "page": "accounts"},
        {"key": "marking", "title": "Данные рекламодателя для маркировки внесены", "done": bool(biz["marking_confirmed"]),
         "hint": "В Яндекс Директе нажмите на свой логин → «Данные рекламодателя»: имя, ИНН, телефон. Тогда Директ сам промаркирует рекламу.",
         "page": "home"},
        {"key": "ads", "title": "Объявления созданы", "done": units > 0,
         "hint": "Нажмите «Создать рекламу» на главной. Пока можно посмотреть, как всё работает, на примере.", "page": "home"},
        {"key": "metrika", "title": "Обращения с сайта видны (Метрика)",
         "done": bool(biz["metrika_counter_id"] and biz["metrika_goal_id"]), "optional": True,
         "hint": "Выберите счётчик и цель «обращение» на странице «Бизнес». Без этого сервис не отличит хорошее объявление от плохого.",
         "page": "business"},
    ]
    return steps


@router.get("/overview")
def overview(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    biz = _own(conn, user, business_id)
    cfg = EngineConfig()
    sim_today = get_today(conn, business_id)
    real = conn.execute("SELECT 1 FROM campaigns WHERE business_id = ? AND source = 'direct'", (business_id,)).fetchone() is not None
    today = sim_today or (date.today().isoformat() if real else None)
    result = {"example": bool(sim_today), "real": real, "today": today, "units": [], "totals": None, "budget": None, "needs": "",
              "mode": biz["mode"], "target_cpa": _rub(biz["target_cpa_micro"]), "proposals": 0, "stopped": False, "notices": [],
              "sync": {"at": biz["last_sync_at"], "error": biz["last_sync_error"]},
              "has_account": bool(conn.execute("SELECT 1 FROM ad_accounts WHERE user_id = ?", (user["id"],)).fetchone())}
    try:
        target_cpa_micro(biz)
    except EngineError as e:
        result["needs"] = e.message
    result["proposals"] = conn.execute(
        "SELECT COUNT(*) FROM changes WHERE business_id = ? AND status = 'proposed'", (business_id,)
    ).fetchone()[0]
    result["stopped"] = conn.execute(
        "SELECT 1 FROM changes WHERE business_id = ? AND reason_code = 'user_stop_all' AND status = 'applied' "
        "AND reverted_by IS NULL LIMIT 1", (business_id,)
    ).fetchone() is not None
    units_count = conn.execute("SELECT COUNT(*) FROM ad_units WHERE business_id = ?", (business_id,)).fetchone()[0]
    accounts = conn.execute("SELECT COUNT(*) FROM ad_accounts WHERE user_id = ?", (user["id"],)).fetchone()[0]
    result["checklist"] = checklist(biz, accounts, units_count)
    if not today:
        return result

    conversions = bool(sim_today) or bool(biz["metrika_counter_id"] and biz["metrika_goal_id"])
    result["conversions"] = conversions
    start = day_add(today, -(cfg.window_days - 1))
    units = conn.execute("SELECT * FROM ad_units WHERE business_id = ? ORDER BY id", (business_id,)).fetchall()
    total = {"spend": 0, "clicks": 0, "leads": 0, "clients": 0}
    in_test = {
        uid for r in conn.execute("SELECT unit_a, unit_b FROM experiments WHERE business_id = ? AND status = 'running'", (business_id,))
        for uid in (r["unit_a"], r["unit_b"])
    }
    for u in units:
        s = unit_stats(conn, u["id"], start, today)
        if real and conversions and not s["clients"]:
            # Метрика видит обращения, но не знает, кто стал клиентом: оцениваем по доле, которую назвал владелец.
            s["clients"] = round(s["leads"] * biz["client_share"])
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
            "state": u["state"],
            "note": u["status_note"] if "status_note" in u.keys() else "",
            "verdict": (CLASS_LABELS[cls] if not real or conversions else "Идёт показ") if u["state"] == "active" else STATE_LABELS.get(u["state"], "Остановлено"),
            "verdict_code": (cls if not real or conversions else "waiting") if u["state"] == "active" else u["state"],
        })
    marks = conn.execute(
        "SELECT COALESCE(SUM(count), 0) AS n, COUNT(*) AS days FROM client_marks WHERE business_id = ? AND day BETWEEN ? AND ?",
        (business_id, start, today),
    ).fetchone()
    if real and marks["days"]:
        total["clients"] = marks["n"]  # точное число от владельца вместо оценки
    result["totals"] = {
        "spend": _rub(total["spend"]),
        "clicks": total["clicks"],
        "leads": total["leads"],
        "clients": total["clients"],
        "cost_per_lead": _ratio(total["spend"], total["leads"]),
        "cost_per_client": _ratio(total["spend"], total["clients"]),
        "days": cfg.window_days,
        "clients_estimated": bool(real and conversions and not marks["days"]),
    }
    if real:
        states = {u["state"] for u in units}
        if "moderation" in states:
            result["notices"].append("Яндекс проверяет объявления. Обычно это занимает от нескольких минут до нескольких часов.")
        if "rejected" in states:
            result["notices"].append("Часть объявлений не прошла проверку. Причина указана рядом с объявлением. Сервис предложит новый текст.")
        if "unpaid" in states:
            result["notices"].append("Объявления одобрены. Чтобы начались показы, пополните счёт в Яндекс Директе: минимум 300 ₽.")
        if not conversions:
            result["notices"].append(
                "Яндекс Метрика ещё не подключена, поэтому сервис пока не видит обращений. Он следит за бюджетом, "
                "но не останавливает и не усиливает объявления, пока не поймёт, какие из них приводят клиентов.")
        if biz["last_sync_error"]:
            result["notices"].append(f"Последнее обновление данных не удалось: {biz['last_sync_error']}")
    week_spend = sum(unit_stats(conn, u["id"], day_add(today, -6), today)["spend"] for u in units)
    budget = biz["weekly_budget_micro"]
    result["budget"] = {
        "weekly": _rub(budget),
        "spent_week": _rub(week_spend),
        "percent": round(week_spend / budget * 100) if budget else 0,
    }
    return result


@router.get("/series")
def series(business_id: int, days: int = 14, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Расход и обращения по дням для графика."""
    _own(conn, user, business_id)
    days = max(2, min(days, 60))
    sim_today = get_today(conn, business_id)
    real = conn.execute("SELECT 1 FROM campaigns WHERE business_id = ? AND source = 'direct'", (business_id,)).fetchone() is not None
    if not sim_today and not real:
        return {"days": []}
    today = sim_today or date.today().isoformat()
    start = day_add(today, -(days - 1))
    rows = conn.execute(
        "SELECT s.day AS day, SUM(s.spend_micro) AS spend, SUM(s.leads) AS leads FROM stats_daily s "
        "JOIN ad_units u ON u.id = s.unit_id WHERE u.business_id = ? AND s.day BETWEEN ? AND ? GROUP BY s.day",
        (business_id, start, today),
    ).fetchall()
    by_day = {r["day"]: r for r in rows}
    out = []
    for i in range(days):
        d = day_add(start, i)
        r = by_day.get(d)
        out.append({"day": d, "spend": _rub(r["spend"]) if r else 0, "leads": r["leads"] if r else 0})
    return {"days": out}


STYLE_LABELS = {"photo": "Фото-герой", "luxe": "Тёмный люкс", "pastel": "Пастель", "bold": "Яркая типографика", "card": "Светлая карточка", "ai": "ИИ-дизайн"}
MIN_IMPRESSIONS = 1000  # меньше показов на стиль: разница в кликах может быть случайной
MIN_LEADS = 3           # меньше обращений: цену обращения сравнивать рано


@router.get("/insights")
def insights(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Что работает лучше: результаты по стилям объявлений. Выводы только когда данных достаточно."""
    _own(conn, user, business_id)
    rows = conn.execute(
        "SELECT u.style AS style, COUNT(DISTINCT u.id) AS units, COALESCE(SUM(s.impressions),0) AS imp, "
        "COALESCE(SUM(s.clicks),0) AS clicks, COALESCE(SUM(s.spend_micro),0) AS spend, COALESCE(SUM(s.leads),0) AS leads "
        "FROM ad_units u LEFT JOIN stats_daily s ON s.unit_id = u.id WHERE u.business_id = ? AND u.style != '' GROUP BY u.style",
        (business_id,),
    ).fetchall()
    groups = []
    for r in rows:
        groups.append({
            "style": r["style"], "label": STYLE_LABELS.get(r["style"], r["style"]), "units": r["units"],
            "impressions": r["imp"], "clicks": r["clicks"], "spend": _rub(r["spend"]), "leads": r["leads"],
            "ctr": round(r["clicks"] / r["imp"] * 100, 2) if r["imp"] else None,
            "cost_per_lead": _ratio(r["spend"], r["leads"]),
        })
    groups.sort(key=lambda g: list(STYLE_LABELS).index(g["style"]) if g["style"] in STYLE_LABELS else 99)
    conclusions: list[str] = []
    if len(groups) < 2:
        return {"groups": groups, "conclusions": ["Сравнивать пока не с чем: нужны объявления как минимум двух разных стилей."]}
    by_ctr = sorted((g for g in groups if g["impressions"] >= MIN_IMPRESSIONS and g["ctr"] is not None), key=lambda g: -g["ctr"])
    if len(by_ctr) >= 2 and by_ctr[0]["ctr"] >= 1.3 * by_ctr[1]["ctr"]:
        conclusions.append(f"Чаще всего кликают на стиль «{by_ctr[0]['label']}»: {by_ctr[0]['ctr']}% против {by_ctr[1]['ctr']}% у «{by_ctr[1]['label']}».")
    by_cpl = sorted((g for g in groups if g["leads"] >= MIN_LEADS and g["cost_per_lead"]), key=lambda g: g["cost_per_lead"])
    if len(by_cpl) >= 2 and by_cpl[0]["cost_per_lead"] <= 0.8 * by_cpl[1]["cost_per_lead"]:
        conclusions.append(
            f"Дешевле всего обращения у стиля «{by_cpl[0]['label']}»: {round(by_cpl[0]['cost_per_lead'])} ₽ против {round(by_cpl[1]['cost_per_lead'])} ₽ у «{by_cpl[1]['label']}».")
    if not conclusions:
        lack = [g["label"] for g in groups if g["impressions"] < MIN_IMPRESSIONS]
        conclusions.append(
            "Пока мало данных, чтобы назвать лучший стиль: разница может быть случайной. "
            + (f"Нужно хотя бы {MIN_IMPRESSIONS} показов на каждый стиль, не хватает у: {', '.join(lack)}." if lack
               else f"Нужно хотя бы {MIN_LEADS} обращений на стиль, чтобы сравнить цену обращения."))
    return {"groups": groups, "conclusions": conclusions}


class ClientsIn(BaseModel):
    day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    count: int = Field(ge=0, le=1000)


@router.post("/clients")
def mark_clients(business_id: int, data: ClientsIn, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Владелец записывает, сколько клиентов пришло из рекламы за день."""
    _own(conn, user, business_id)
    try:
        day = date.fromisoformat(data.day)
    except ValueError:
        raise HTTPException(400, detail={"message": "Дата указана неверно."})
    if day > date.today():
        raise HTTPException(400, detail={"message": "Нельзя указать клиентов из будущего."})
    conn.execute(
        "INSERT INTO client_marks (business_id, day, count) VALUES (?,?,?) "
        "ON CONFLICT(business_id, day) DO UPDATE SET count = excluded.count", (business_id, data.day, data.count))
    conn.commit()
    return {"ok": True}


@router.get("/clients")
def list_clients(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    _own(conn, user, business_id)
    rows = conn.execute("SELECT day, count FROM client_marks WHERE business_id = ? ORDER BY day DESC LIMIT 14", (business_id,)).fetchall()
    return {"marks": [{"day": r["day"], "count": r["count"]} for r in rows]}


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
                "status": r["status"],
                "reverted": r["reverted_by"] is not None,
                "can_rollback": r["status"] == "applied" and r["reverted_by"] is None and r["action"] != "rollback",
                "can_decide": r["status"] == "proposed",
            }
            for r in rows
        ]
    }


def _decide(business_id: int, change_id: int, user, conn, fn):
    biz = _own(conn, user, business_id)
    today = get_today(conn, business_id) or date.today().isoformat()
    try:
        fn(biz, _platform(conn, biz), today)
    except (EngineError, DirectError) as e:
        raise _fail(e)
    return {"ok": True}


@router.post("/changes/{change_id}/approve")
def change_approve(business_id: int, change_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    return _decide(business_id, change_id, user, conn, lambda biz, platform, today: approve(conn, platform, biz, change_id, today))


@router.post("/changes/{change_id}/reject")
def change_reject(business_id: int, change_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    return _decide(business_id, change_id, user, conn, lambda biz, platform, today: reject(conn, biz, change_id, today))


@router.post("/stop-all")
def stop_everything(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    biz = _own(conn, user, business_id)
    today = get_today(conn, business_id) or date.today().isoformat()
    try:
        return {"stopped": stop_all(conn, _platform(conn, biz), biz, today)}
    except (EngineError, DirectError) as e:
        raise _fail(e)


@router.post("/resume-all")
def resume_everything(business_id: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    biz = _own(conn, user, business_id)
    today = get_today(conn, business_id) or date.today().isoformat()
    try:
        return {"resumed": resume_all(conn, _platform(conn, biz), biz, today)}
    except (EngineError, DirectError) as e:
        raise _fail(e)


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
        rollback(conn, _platform(conn, biz), biz, change_id, today, data.force)
    except (EngineError, DirectError) as e:
        raise _fail(e)
    return {"ok": True}
