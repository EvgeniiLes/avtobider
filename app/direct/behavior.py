"""Поведение посетителей на сайте по объявлениям и фразам (из Метрики).

В ссылки объявлений добавлены метки: utm_content это номер объявления, utm_term это ключевая фраза (см. launch.tagged_href). Раз в день сервис
забирает у Метрики за 30 дней визиты, отказы, глубину просмотра и время на сайте по этим меткам. Видно не только «кликнули» и «обратились», но и
«пришли, посмотрели пять секунд и ушли»: такое объявление приводит не тех людей. Только сбор и показ, сама реклама по этим данным не меняется.
"""
import json
import sqlite3

import httpx

from app.direct.client import DirectError
from app.engine.util import day_add

METRIKA = "https://api-metrika.yandex.net"
WINDOW_DAYS = 30
MIN_VISITS = 20        # меньше визитов: судить рано
BOUNCE_HIGH = 70       # процент отказов, после которого объявление считаем подозрительным
BOUNCE_LOW = 40
FAST_SECONDS = 15      # столько секунд и меньше на сайте: ушли почти сразу
GOOD_SECONDS = 60
MAX_TERMS = 30
METRICS = "ym:s:visits,ym:s:bounceRate,ym:s:pageDepth,ym:s:avgVisitDurationSeconds"
FILTER = "ym:s:UTMMedium=='cpc'"   # метки ставим мы: utm_medium=cpc


def _num(value, digits: int = 1) -> float:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return 0.0


async def fetch_behavior(http: httpx.AsyncClient, token: str, counter_id: str, date_from: str, date_to: str) -> dict:
    """{ads: {номер объявления: показатели}, terms: {фраза: показатели}} по меткам. Ошибку Метрики поднимает наверх."""
    headers = {"Authorization": f"OAuth {token}"}

    async def stat(dimension: str) -> dict[str, dict]:
        r = await http.get(f"{METRIKA}/stat/v1/data", headers=headers, params={
            "ids": int(counter_id), "metrics": METRICS, "dimensions": dimension, "date1": date_from, "date2": date_to,
            "filters": FILTER, "limit": 1000, "accuracy": "full"})
        if r.status_code != 200:
            raise DirectError(f"Метрика ответила {r.status_code}", code=r.status_code)
        out: dict[str, dict] = {}
        for row in r.json().get("data") or []:
            dim = (row.get("dimensions") or [{}])[0]
            key = str(dim.get("name") or dim.get("id") or "").strip()
            values = row.get("metrics") or []
            if not key or key.startswith("{") or len(values) < 4:   # «{ad_id}» и пустое: метки не подставились
                continue
            out[key] = {"visits": int(_num(values[0], 0)), "bounce": _num(values[1]), "depth": _num(values[2]), "seconds": int(_num(values[3], 0))}
        return out

    return {"ads": await stat("ym:s:UTMContent"), "terms": await stat("ym:s:UTMTerm")}


async def sync_behavior(conn: sqlite3.Connection, biz, today: str, token: str, transport=None) -> str:
    """Загружает поведение раз в день. Возвращает пусто или причину сбоя (для экрана); основное обновление данных он не ломает."""
    if biz["behavior_day"] == today or not biz["metrika_counter_id"]:
        return ""
    try:
        async with httpx.AsyncClient(timeout=40, transport=transport) as http:
            data = await fetch_behavior(http, token, biz["metrika_counter_id"], day_add(today, -(WINDOW_DAYS - 1)), today)
    except (DirectError, httpx.HTTPError, ValueError) as e:
        return getattr(e, "message", None) or str(e)
    conn.execute("UPDATE businesses SET behavior = ?, behavior_day = ? WHERE id = ?", (json.dumps(data, ensure_ascii=False), today, biz["id"]))
    conn.commit()
    return ""


def verdict(row: dict) -> tuple[str, str]:
    """(код, пояснение простыми словами)."""
    if row["visits"] < MIN_VISITS:
        return "waiting", f"Мало визитов ({row['visits']}), судить рано"
    if row["bounce"] >= BOUNCE_HIGH or row["seconds"] <= FAST_SECONDS:
        return "bad", f"Многие уходят сразу: отказов {round(row['bounce'])}%, на сайте {row['seconds']} с. Возможно, объявление обещает не то, что на сайте"
    if row["bounce"] <= BOUNCE_LOW and row["seconds"] >= GOOD_SECONDS:
        return "good", f"Люди читают: отказов {round(row['bounce'])}%, на сайте {row['seconds']} с"
    return "ok", f"Обычное поведение: отказов {round(row['bounce'])}%, на сайте {row['seconds']} с"


def view(conn: sqlite3.Connection, biz) -> dict:
    """Для экрана: по каждому объявлению (с его названием) и по фразам. Объявления без данных не показываем."""
    try:
        data = json.loads(biz["behavior"] or "{}")
    except ValueError:
        data = {}
    by_ad = {str(r["external_ad_id"]): r["title"] for r in conn.execute(
        "SELECT external_ad_id, title FROM ad_units WHERE business_id = ? AND external_ad_id IS NOT NULL", (biz["id"],))}
    ads = []
    for ad_id, row in sorted((data.get("ads") or {}).items(), key=lambda x: -x[1]["visits"]):
        if ad_id in by_ad:
            code, why = verdict(row)
            ads.append({"title": by_ad[ad_id], **row, "verdict_code": code, "verdict": why})
    terms = []
    for term, row in sorted((data.get("terms") or {}).items(), key=lambda x: -x[1]["visits"])[:MAX_TERMS]:
        code, why = verdict(row)
        terms.append({"term": term, **row, "verdict_code": code, "verdict": why})
    return {"has_counter": bool(biz["metrika_counter_id"]), "synced": biz["behavior_day"] or "", "days": WINDOW_DAYS, "ads": ads, "terms": terms}