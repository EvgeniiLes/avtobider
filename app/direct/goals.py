"""Все цели счётчика Метрики: названия и посуточные достижения по объявлениям.

Раньше сервис видел только одну цель, ту, что выбрана «обращением». Теперь он сохраняет все цели счётчика (клик по кнопке, открытие
формы, звонок, заявка…) по дням и объявлениям: так видно путь посетителя до обращения, а у цели, которая приводит людей, но редко
доходит до обращения, видно, где они отваливаются. Обращением по-прежнему считается выбранная цель (на ней решает движок).
"""
import sqlite3

import httpx

METRIKA = "https://api-metrika.yandex.net/management/v1"
MAX_GOALS = 10   # столько целей Директ принимает в одном отчёте


async def fetch_goals(token: str, counter_id: str, transport: httpx.AsyncBaseTransport | None = None) -> list[dict]:
    """Цели счётчика (номер и название) из Метрики. Пустой список, если спросить не вышло: сервису это не мешает, он возьмёт прежний список."""
    try:
        async with httpx.AsyncClient(timeout=20, transport=transport) as http:
            r = await http.get(f"{METRIKA}/counter/{int(counter_id)}/goals", headers={"Authorization": f"OAuth {token}"})
        if r.status_code != 200:
            return []
        return [{"id": str(g["id"]), "name": g.get("name") or str(g["id"])} for g in r.json().get("goals", [])]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return []


def save_goals(conn: sqlite3.Connection, business_id: int, goals: list[dict]) -> None:
    """Запоминает цели счётчика. Список целиком заменяется: удалённой в Метрике цели в статистике больше нет, а её история остаётся в goal_stats."""
    if not goals:
        return
    conn.execute("DELETE FROM metrika_goals WHERE business_id = ?", (business_id,))
    conn.executemany("INSERT INTO metrika_goals (business_id, goal_id, name) VALUES (?,?,?)", [(business_id, g["id"], g["name"]) for g in goals])


def goal_ids_for_report(conn: sqlite3.Connection, biz) -> list[str]:
    """Цели для отчёта Директа: сначала «обращение», потом остальные цели счётчика (не больше MAX_GOALS)."""
    main = biz["metrika_goal_id"] or ""
    rest = [r["goal_id"] for r in conn.execute("SELECT goal_id FROM metrika_goals WHERE business_id = ? ORDER BY goal_id", (biz["id"],))]
    return list(dict.fromkeys(([main] if main else []) + rest))[:MAX_GOALS]
