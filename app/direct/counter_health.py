"""Работает ли счётчик Метрики: стоит ли его код на сайте и приходят ли визиты.

Без этой проверки сервис не отличал «объявление не даёт обращений» от «счётчик не считает»: если код пропал с сайта или его там
никогда не было, все обращения нулевые, и объявления выглядят плохими. Проверяем у Метрики два факта: статус установки кода
(Метрика сама заглядывает на сайт) и число визитов за 7 дней, и сравниваем с кликами по рекламе за те же дни.
Когда счётчик явно не работает, решения по обращениям не принимаются (работает только защита бюджета).
"""
import json
import sqlite3
from datetime import datetime, timezone

import httpx

from app.engine.util import day_add

BASE = "https://api-metrika.yandex.net"
MIN_CLICKS_TO_JUDGE = 5     # при меньшем числе кликов отсутствие визитов ещё ничего не значит
MIN_CLICKS_FOR_RATIO = 20   # с такого числа кликов сравниваем визиты с кликами
MIN_RATIO = 0.3             # визитов меньше этой доли от кликов: счётчик теряет большую часть людей


def _number(value) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def first_number(data: dict) -> int | None:
    """Число из ответа отчёта Метрики без группировок: итог (totals) или первая строка данных."""
    totals = data.get("totals")
    if isinstance(totals, list) and totals:
        first = totals[0]
        if isinstance(first, list):
            first = first[0] if first else None
        if _number(first) is not None:
            return _number(first)
    rows = data.get("data")
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        metrics = rows[0].get("metrics")
        if isinstance(metrics, list) and metrics:
            return _number(metrics[0])
    return None


async def fetch_health(http: httpx.AsyncClient, token: str, counter_id: str) -> dict:
    """Статус установки кода у Метрики и визиты за 7 дней. То, что спросить не вышло, остаётся None."""
    headers = {"Authorization": f"OAuth {token}"}
    result = {"code_status": None, "visits": None}
    try:
        r = await http.get(f"{BASE}/management/v1/counter/{int(counter_id)}", headers=headers)
        if r.status_code == 200:
            result["code_status"] = (r.json().get("counter") or {}).get("code_status")
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        pass
    try:
        r = await http.get(f"{BASE}/stat/v1/data", headers=headers, params={
            "ids": int(counter_id), "metrics": "ym:s:visits", "date1": "7daysAgo", "date2": "today", "accuracy": "full"})
        if r.status_code == 200:
            result["visits"] = first_number(r.json())
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        pass
    return result


def verdict(code_status: str | None, visits: int | None, clicks: int) -> dict:
    """Уровень: ok, warn (стоит поправить), bad (счётчик не считает: по обращениям не судим), unknown (проверить нечем)."""
    if visits is None and code_status is None:
        return {"level": "unknown", "title": "Не удалось проверить счётчик",
                "message": "Метрика не ответила. Проверка повторится при следующем обновлении данных."}
    if visits == 0 and clicks >= MIN_CLICKS_TO_JUDGE:
        return {"level": "bad", "title": "Метрика не получает визитов",
                "message": f"За 7 дней по рекламе было {clicks} кликов, а Метрика не увидела ни одного визита. Скорее всего, код счётчика не стоит на сайте "
                           "или стоит другой. Пока так, сервис не судит об объявлениях по обращениям, чтобы не остановить хорошее."}
    if code_status == "CS_NOT_FOUND":
        counted = visits is not None and visits > 0
        return {"level": "warn" if counted else "bad", "title": "Метрика не нашла код счётчика на сайте",
                "message": "Установите код на все страницы сайта (готовый код есть ниже). "
                           + ("Визиты пока приходят, но проверка Метрики кода не видит." if counted else "Пока кода нет, обращения посчитать нельзя.")}
    if code_status == "CS_ERR_OTHER_HTML_CODE":
        return {"level": "warn", "title": "На сайте стоит код другого счётчика",
                "message": "Метрика нашла на сайте код, но не этого счётчика. Обращения считаются в другом месте. Поставьте код выбранного счётчика."}
    if visits is not None and clicks >= MIN_CLICKS_FOR_RATIO and visits < MIN_RATIO * clicks:
        return {"level": "warn", "title": "Метрика видит лишь часть визитов",
                "message": f"За 7 дней {clicks} кликов по рекламе, а в Метрике только {visits} визитов. Возможно, код стоит не на всех страницах, "
                           "грузится слишком долго или блокируется. Из-за этого обращений будет меньше, чем есть на самом деле."}
    if visits == 0 and code_status != "CS_OK":   # визитов нет, и Метрика не подтвердила, что код стоит на сайте: работу счётчика ещё никто не видел
        return {"level": "warn", "title": "Счётчик пока не подтверждён",
                "message": "За 7 дней Метрика не получила ни одного визита и не смогла проверить код на сайте. Откройте свой сайт в браузере и через пару минут "
                           "нажмите «Проверить счётчик»: если визит не появился, в настройках счётчика проверьте разрешённые адреса сайта и установленный код."}
    return {"level": "ok", "title": "Счётчик работает",
            "message": "Метрика получает визиты." if visits else "Код найден, визиты появятся с первыми кликами."}


def week_clicks(conn: sqlite3.Connection, business_id: int, today: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(s.clicks), 0) FROM stats_daily s JOIN ad_units u ON u.id = s.unit_id WHERE u.business_id = ? AND s.day BETWEEN ? AND ?",
        (business_id, day_add(today, -6), today)).fetchone()
    return row[0]


async def check_counter(conn: sqlite3.Connection, biz, token: str, today: str, http: httpx.AsyncClient) -> dict:
    """Проверяет счётчик бизнеса и запоминает результат."""
    clicks = week_clicks(conn, biz["id"], today)
    raw = await fetch_health(http, token, biz["metrika_counter_id"])
    health = {**verdict(raw["code_status"], raw["visits"], clicks), "code_status": raw["code_status"], "visits": raw["visits"], "clicks": clicks,
              "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    conn.execute("UPDATE businesses SET counter_health = ? WHERE id = ?", (json.dumps(health, ensure_ascii=False), biz["id"]))
    conn.commit()
    return health


def load_health(biz) -> dict | None:
    try:
        raw = biz["counter_health"]
    except (KeyError, IndexError):
        return None
    if not raw or not biz["metrika_counter_id"]:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) and "level" in data else None
