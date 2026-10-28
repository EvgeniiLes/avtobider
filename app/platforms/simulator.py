"""Симулятор рекламной площадки: «пример данных».

Нужен, чтобы показать и проверить логику решений без реального бюджета. У каждого
объявления есть скрытые свойства (цена клика и доля обращений), по которым за каждый
день случайно, но воспроизводимо рождается статистика. Движок этих свойств не видит.
"""
import random

from app.creative.compose import style_for
import sqlite3
from datetime import date

from app.engine.util import EngineError, MICRO, day_add

DEMO_TITLES = [
    "{name}: запишитесь онлайн",
    "{kind}, {city}",
    "Скидка новым клиентам: {kind}",
    "{name}: удобное время для записи",
    "{kind}: первое посещение со скидкой",
    "{kind} рядом с вами",
]
# Во сколько раз обращение дороже (>1) или дешевле (<1) цели: по два объявления каждого вида.
QUALITY = [(0.45, 0.75), (0.45, 0.75), (0.9, 1.15), (0.9, 1.15), (1.7, 2.6), (1.7, 2.6)]
CTR = 0.03


class SimulatedPlatform:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def set_state(self, unit_id: int, state: str, day: str) -> None:
        self.conn.execute("UPDATE ad_units SET state = ?, last_changed_day = ? WHERE id = ?", (state, day, unit_id))

    def set_budget(self, unit_id: int, budget_micro: int, day: str) -> None:
        self.conn.execute(
            "UPDATE ad_units SET budget_micro = ?, last_changed_day = ? WHERE id = ?", (budget_micro, day, unit_id)
        )

    def touch(self, unit_id: int, day: str) -> None:
        self.conn.execute("UPDATE ad_units SET last_changed_day = ? WHERE id = ?", (day, unit_id))

    def create_variant(self, unit_id: int, day: str) -> int:
        a = self.conn.execute("SELECT * FROM ad_units WHERE id = ?", (unit_id,)).fetchone()
        sim = self.conn.execute("SELECT * FROM sim_units WHERE unit_id = ?", (unit_id,)).fetchone()
        rng = random.Random(f"variant:{unit_id}:{day}")
        cur = self.conn.execute(
            "INSERT INTO ad_units (campaign_id, business_id, title, body, state, budget_micro, variant_of, "
            "last_changed_day, created_day, style, photo_type) VALUES (?,?,?,?, 'active', ?,?,?,?,?,?)",
            (a["campaign_id"], a["business_id"], f"{a['title']} (вариант Б)", a["body"], a["budget_micro"], unit_id, day, day, a["style"], a["photo_type"]),
        )
        self.conn.execute(
            "INSERT INTO sim_units (unit_id, cpc_micro, cr) VALUES (?,?,?)",
            (
                cur.lastrowid,
                int(sim["cpc_micro"] * rng.uniform(0.9, 1.1)),
                min(max(sim["cr"] * rng.uniform(0.6, 1.7), 0.01), 0.5),
            ),
        )
        return cur.lastrowid


def _binomial(rng: random.Random, n: int, p: float) -> int:
    return sum(1 for _ in range(n) if rng.random() < p)


def get_today(conn: sqlite3.Connection, business_id: int) -> str | None:
    row = conn.execute("SELECT today FROM sim_state WHERE business_id = ?", (business_id,)).fetchone()
    return row["today"] if row else None


def start_demo(conn: sqlite3.Connection, biz: sqlite3.Row, target_lead_micro: int, today: str | None = None) -> str:
    """Создаёт примерную кампанию с шестью объявлениями. Возвращает «сегодняшний» день симуляции.

    today нужен тестам: с фиксированной датой результат каждый раз одинаковый."""
    if get_today(conn, biz["id"]):
        raise EngineError("Пример уже запущен. Чтобы начать заново, сначала сбросьте его.")
    if biz["weekly_budget_micro"] <= 0:
        raise EngineError("Укажите бюджет на неделю в описании бизнеса, и тогда запустим пример.")
    today = today or date.today().isoformat()
    rng = random.Random(f"sim:{biz['id']}")
    cur = conn.execute(
        "INSERT INTO campaigns (business_id, name, source) VALUES (?, ?, 'simulator')",
        (biz["id"], f"Пример: {biz['kind']}"),
    )
    campaign_id = cur.lastrowid
    budget = int(biz["weekly_budget_micro"] * 0.9 * 0.7 / len(DEMO_TITLES))  # 70% доступного, остальное про запас
    order = list(range(len(DEMO_TITLES)))
    rng.shuffle(order)
    for position, (slot, title) in enumerate(zip(order, DEMO_TITLES)):
        low, high = QUALITY[slot]
        cpc = int(rng.uniform(18, 45) * MICRO)
        cr = min(cpc / (rng.uniform(low, high) * target_lead_micro), 0.5)
        unit = conn.execute(
            "INSERT INTO ad_units (campaign_id, business_id, title, state, budget_micro, last_changed_day, created_day, style, photo_type) "
            "VALUES (?,?,?, 'active', ?,?,?,?,?)",
            (
                campaign_id, biz["id"],
                title.format(name=biz["name"], kind=biz["kind"], city=biz["city"] or "вашем городе"),
                budget, today, today, style_for(position), ("interior", "work")[position % 2],
            ),
        )
        conn.execute("INSERT INTO sim_units (unit_id, cpc_micro, cr) VALUES (?,?,?)", (unit.lastrowid, cpc, cr))
    conn.execute("INSERT INTO sim_state (business_id, today) VALUES (?, ?)", (biz["id"], today))
    conn.commit()
    return today


def advance_day(conn: sqlite3.Connection, biz: sqlite3.Row) -> str:
    """Прокручивает симуляцию на один день вперёд и возвращает новый день."""
    today = get_today(conn, biz["id"])
    if not today:
        raise EngineError("Сначала запустите пример.")
    day = day_add(today, 1)
    units = conn.execute(
        "SELECT u.*, s.cpc_micro, s.cr FROM ad_units u JOIN sim_units s ON s.unit_id = u.id "
        "WHERE u.business_id = ? AND u.state = 'active'",
        (biz["id"],),
    ).fetchall()
    for u in units:
        rng = random.Random(f"{u['id']}:{day}")
        cpc = u["cpc_micro"] * rng.uniform(0.9, 1.1)
        clicks = int((u["budget_micro"] / 7) * rng.uniform(0.85, 1.0) / cpc)
        leads = _binomial(rng, clicks, u["cr"])
        clients = _binomial(rng, leads, biz["client_share"])
        conn.execute(
            "INSERT OR REPLACE INTO stats_daily (unit_id, day, impressions, clicks, spend_micro, leads, clients) "
            "VALUES (?,?,?,?,?,?,?)",
            (u["id"], day, int(clicks / CTR), clicks, int(clicks * cpc), leads, clients),
        )
    conn.execute("UPDATE sim_state SET today = ? WHERE business_id = ?", (day, biz["id"]))
    conn.commit()
    return day


def reset_demo(conn: sqlite3.Connection, business_id: int) -> None:
    conn.execute("DELETE FROM campaigns WHERE business_id = ? AND source = 'simulator'", (business_id,))
    conn.execute("DELETE FROM changes WHERE business_id = ?", (business_id,))
    conn.execute("DELETE FROM experiments WHERE business_id = ?", (business_id,))
    conn.execute("DELETE FROM sim_state WHERE business_id = ?", (business_id,))
    conn.commit()
