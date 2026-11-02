"""Срезы рекламы: кто и как приходит и сколько это стоит.

Раз в день сервис забирает у Директа отчёты по измерениям (устройство, пол, возраст, доход, площадка, регион, вид таргетинга, позиция,
формат) за 30 дней, по дням, а у Метрики визиты и обращения по часам суток и дням недели. Всё кладётся в `dimension_stats` и копится
(дни старше полугода убираются): это основа, по которой потом настраиваются ставки, расписание и площадки. Здесь только сбор и показ;
сегменты, где обращение заметно дороже или дешевле среднего, помечаются, но ничего само не меняется.
Каждое измерение забирается отдельным запросом, сбой одного не мешает остальным: что не вышло, видно в `slices_status`.
"""
import json
import re
import sqlite3
from datetime import date, datetime

import httpx

from app.direct.client import DirectClient, DirectError
from app.engine.util import MICRO, day_add

WINDOW_DAYS = 30
KEEP_DAYS = 180
METRIKA = "https://api-metrika.yandex.net"
MIN_CLICKS = 20            # меньше кликов в сегменте: судить рано
DEAR = 1.6                 # цена обращения во столько раз выше средней: «дорого»
CHEAP = 0.6                # и во столько раз ниже: «дёшево»
MIN_LEADS_CHEAP = 3
MAX_PLACEMENTS = 20

# измерение → поле отчёта Директа; для Метрики поле не нужно (см. fetch_metrika)
DIRECT_DIMENSIONS = {
    "device": "Device", "gender": "Gender", "age": "Age", "income": "IncomeGrade", "network": "AdNetworkType", "placement": "Placement",
    "region": "TargetingLocationName", "targeting": "CriteriaType", "slot": "Slot", "format": "AdFormat",
}
METRIKA_DIMENSIONS = ("hour", "weekday")

TITLES = {
    "device": "Устройства", "gender": "Пол", "age": "Возраст", "income": "Доход", "network": "Где показывалось",
    "placement": "Площадки сети", "region": "Регионы", "targeting": "Вид таргетинга", "slot": "Позиция на странице",
    "format": "Формат объявления", "hour": "Часы суток", "weekday": "Дни недели",
}
ORDER = ["device", "gender", "age", "income", "network", "targeting", "slot", "format", "region", "placement", "hour", "weekday"]
VISITS_NOTE = "По данным Метрики: вместо кликов здесь визиты с рекламы."

LABELS = {
    "device": {"DESKTOP": "Компьютер", "MOBILE": "Телефон", "TABLET": "Планшет"},
    "gender": {"GENDER_MALE": "Мужчины", "GENDER_FEMALE": "Женщины"},
    "income": {"VERY_HIGH": "Очень высокий", "HIGH": "Высокий", "ABOVE_AVERAGE": "Выше среднего", "AVERAGE": "Средний",
               "BELOW_AVERAGE": "Ниже среднего", "LOW": "Низкий", "VERY_LOW": "Очень низкий"},
    "network": {"SEARCH": "Поиск", "AD_NETWORK": "Рекламная сеть (РСЯ)"},
    "targeting": {"KEYWORD": "Ключевая фраза", "AUTOTARGETING": "Автотаргетинг", "RETARGETING": "Ретаргетинг", "AUDIENCE_TARGET": "Подбор аудитории"},
    "slot": {"PREMIUMBLOCK": "Спецразмещение", "OTHER": "Гарантия и другое"},
    "format": {"TEXT": "Текстовое", "IMAGE": "Графическое", "SMART_TILE": "Смарт-плитка", "VIDEO": "Видео"},
}
WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
UNKNOWN = "Не определено"


def label(dimension: str, key: str) -> str:
    if key in ("", "--"):
        return UNKNOWN
    if dimension == "age":
        m = re.fullmatch(r"AGE_(\d+)_(\d+)", key)
        if m:
            return f"до 18" if m.group(1) == "0" else f"{m.group(1)}–{m.group(2)}"
        m = re.fullmatch(r"AGE_(\d+)", key)
        return f"{m.group(1)} и старше" if m else key
    if dimension == "hour":
        return f"{key}:00"
    if dimension == "weekday":
        return WEEKDAYS[int(key) - 1]
    return LABELS.get(dimension, {}).get(key, key)


# ---------- загрузка ----------

def _store(conn: sqlite3.Connection, business_id: int, dimension: str, rows: list[dict], from_day: str | None) -> None:
    """Заменяет данные измерения за окно. from_day=None: строки без дня (итог за окно), заменяются целиком."""
    if from_day is None:
        conn.execute("DELETE FROM dimension_stats WHERE business_id = ? AND dimension = ? AND day = ''", (business_id, dimension))
    else:
        conn.execute("DELETE FROM dimension_stats WHERE business_id = ? AND dimension = ? AND day >= ?", (business_id, dimension, from_day))
    conn.executemany(
        "INSERT OR REPLACE INTO dimension_stats (business_id, dimension, key, day, impressions, clicks, spend_micro, leads) VALUES (?,?,?,?,?,?,?,?)",
        [(business_id, dimension, r["key"], r.get("day", ""), r["impressions"], r["clicks"], r["spend_micro"], r["leads"]) for r in rows])


def _number(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


async def fetch_metrika(http: httpx.AsyncClient, token: str, counter_id: str, goal_id: str | None, date_from: str, date_to: str) -> dict[str, list[dict]]:
    """Визиты с рекламы и достижения цели по часам суток и по дням недели (из данных по датам). Ошибка Метрики поднимается наверх."""
    headers = {"Authorization": f"OAuth {token}"}
    metrics = "ym:s:visits" + (f",ym:s:goal{int(goal_id)}reaches" if goal_id else "")

    async def stat(dimension: str) -> list[dict]:
        r = await http.get(f"{METRIKA}/stat/v1/data", headers=headers, params={
            "ids": int(counter_id), "metrics": metrics, "dimensions": dimension, "date1": date_from, "date2": date_to,
            "filters": "ym:s:lastsignTrafficSource=='ad'", "limit": 1000, "accuracy": "full"})
        if r.status_code != 200:
            raise DirectError(f"Метрика ответила {r.status_code}", code=r.status_code)
        data = r.json().get("data")
        return data if isinstance(data, list) else []

    def part(row) -> tuple[str, int, int]:
        dim = (row.get("dimensions") or [{}])[0]
        values = row.get("metrics") or []
        return str(dim.get("id") or dim.get("name") or ""), _number(values[0] if values else 0), _number(values[1] if len(values) > 1 else 0)

    hours: dict[str, dict] = {}
    for row in await stat("ym:s:hour"):
        key, visits, leads = part(row)
        digits = re.sub(r"\D", "", key.split(":")[0])
        if digits == "":
            continue
        key = f"{int(digits):02d}"
        item = hours.setdefault(key, {"key": key, "impressions": 0, "clicks": 0, "spend_micro": 0, "leads": 0})
        item["clicks"] += visits
        item["leads"] += leads
    days: dict[str, dict] = {}
    for row in await stat("ym:s:date"):
        key, visits, leads = part(row)
        try:
            weekday = str(datetime.strptime(key[:10], "%Y-%m-%d").isoweekday())
        except ValueError:
            continue
        item = days.setdefault(weekday, {"key": weekday, "impressions": 0, "clicks": 0, "spend_micro": 0, "leads": 0})
        item["clicks"] += visits
        item["leads"] += leads
    return {"hour": list(hours.values()), "weekday": list(days.values())}


async def sync_slices(client: DirectClient, conn: sqlite3.Connection, biz, today: str, token: str | None = None, transport=None) -> dict:
    """Все срезы за 30 дней, раз в день. Возвращает статус по каждому измерению (ok или текст ошибки)."""
    if biz["slices_day"] == today:
        return {}
    campaigns = [str(r["external_campaign_id"]) for r in conn.execute(
        "SELECT DISTINCT external_campaign_id FROM ad_units WHERE business_id = ? AND external_campaign_id IS NOT NULL", (biz["id"],))]
    if not campaigns:
        return {}
    goal = biz["metrika_goal_id"] or None
    date_from = day_add(today, -(WINDOW_DAYS - 1))
    status: dict[str, str] = {}
    for dimension, field in DIRECT_DIMENSIONS.items():
        try:
            rows = await client.slice_report(field, date_from, today, goal_id=goal, campaign_ids=campaigns)
            if not goal:
                for r in rows:
                    r["leads"] = 0
            _store(conn, biz["id"], dimension, rows, date_from)
            status[dimension] = "ok"
        except DirectError as e:
            status[dimension] = e.message[:200]
    if biz["metrika_counter_id"] and token:
        try:
            async with httpx.AsyncClient(timeout=30, transport=transport) as http:
                found = await fetch_metrika(http, token, biz["metrika_counter_id"], goal, date_from, today)
            for dimension in METRIKA_DIMENSIONS:
                _store(conn, biz["id"], dimension, found[dimension], None)
                status[dimension] = "ok"
        except (DirectError, httpx.HTTPError, ValueError) as e:
            for dimension in METRIKA_DIMENSIONS:
                status[dimension] = (getattr(e, "message", "") or str(e) or "Метрика не ответила")[:200]
    conn.execute("DELETE FROM dimension_stats WHERE business_id = ? AND day != '' AND day < ?", (biz["id"], day_add(today, -KEEP_DAYS)))
    conn.execute("UPDATE businesses SET slices_day = ?, slices_status = ? WHERE id = ?", (today, json.dumps(status, ensure_ascii=False), biz["id"]))
    conn.commit()
    return status


# ---------- чтение ----------

def _rub(micro: float) -> float:
    return round(micro / MICRO, 2)


def _segments(conn: sqlite3.Connection, business_id: int, dimension: str, from_day: str) -> list[dict]:
    rows = conn.execute(
        "SELECT key, SUM(impressions) AS impressions, SUM(clicks) AS clicks, SUM(spend_micro) AS spend_micro, SUM(leads) AS leads "
        "FROM dimension_stats WHERE business_id = ? AND dimension = ? AND (day = '' OR day >= ?) GROUP BY key",
        (business_id, dimension, from_day)).fetchall()
    return [dict(r) for r in rows if r["impressions"] or r["clicks"] or r["spend_micro"]]


def _flag(seg: dict, avg_cost: float | None, has_goal: bool) -> str:
    """dear: обращение заметно дороже среднего; cheap: заметно дешевле; none: кликов достаточно, обращений нет; пусто: судить рано."""
    if not has_goal or seg["clicks"] < MIN_CLICKS:
        return ""
    if not seg["leads"]:
        return "none"
    if avg_cost:
        cost = seg["spend_micro"] / seg["leads"]
        if cost >= avg_cost * DEAR:
            return "dear"
        if cost <= avg_cost * CHEAP and seg["leads"] >= MIN_LEADS_CHEAP:
            return "cheap"
    return ""


def summary(conn: sqlite3.Connection, biz, today: str, days: int = WINDOW_DAYS) -> dict:
    from_day = day_add(today, -(days - 1))
    has_goal = bool(biz["metrika_goal_id"])
    try:
        status = json.loads(biz["slices_status"] or "{}")
    except ValueError:
        status = {}
    out = []
    for dimension in ORDER:
        segments = _segments(conn, biz["id"], dimension, from_day)
        if not segments:
            continue
        spend = sum(s["spend_micro"] for s in segments)
        leads = sum(s["leads"] for s in segments)
        clicks = sum(s["clicks"] for s in segments)
        avg_cost = spend / leads if leads and spend else None
        if dimension in ("hour", "weekday"):
            segments.sort(key=lambda s: s["key"])
        else:
            segments.sort(key=lambda s: (-s["spend_micro"], -s["clicks"], s["key"]))
        if dimension == "placement":
            segments = segments[:MAX_PLACEMENTS]
        rows = []
        for s in segments:
            rows.append({
                "key": s["key"], "label": label(dimension, s["key"]), "impressions": s["impressions"], "clicks": s["clicks"], "spend": _rub(s["spend_micro"]),
                "leads": s["leads"], "ctr": round(s["clicks"] * 100 / s["impressions"], 2) if s["impressions"] else None,
                "cpc": _rub(s["spend_micro"] / s["clicks"]) if s["clicks"] and s["spend_micro"] else None,
                "cost_per_lead": _rub(s["spend_micro"] / s["leads"]) if s["leads"] and s["spend_micro"] else None,
                "share": round(s["clicks"] * 100 / clicks) if clicks else 0,
                "flag": _flag(s, avg_cost, has_goal) if dimension not in ("hour", "weekday") else "",
            })
        out.append({"id": dimension, "title": TITLES[dimension], "visits": dimension in ("hour", "weekday"), "rows": rows})
    failed = {TITLES[d]: msg for d, msg in status.items() if msg != "ok" and d in TITLES}
    return {"days": days, "synced": biz["slices_day"] or "", "has_goal": has_goal, "dimensions": out, "failed": failed, "visits_note": VISITS_NOTE}
