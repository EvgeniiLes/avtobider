"""Где показывать рекламу: только в поиске Яндекса или ещё и в рекламной сети (сайты-партнёры, приложения).

Режим бизнеса (businesses.placement): пусто, search или all. Пусто значит «по типу бизнеса»: онлайн-услугу (города нет, показ по всей России)
люди ищут сами, и на малом бюджете сеть тратит деньги на случайные клики, поэтому начинаем с поиска; местному бизнесу сети остаются включены.
Если у такой рекламы совсем нет показов, сервис включает сети сам (app/direct/traffic.py) и записывает режим all.
"""
import sqlite3

from app.creative.angles import is_nationwide
from app.direct.client import DirectClient

MODES = ("", "search", "all")


def mode_of(biz) -> str:
    value = biz["placement"] if "placement" in biz.keys() else ""
    return value if value in ("search", "all") else ""


def network_wanted(biz) -> bool:
    """Нужны ли этому бизнесу показы в рекламной сети."""
    mode = mode_of(biz)
    if mode:
        return mode == "all"
    return not is_nationwide(biz)


async def apply(client: DirectClient, conn: sqlite3.Connection, biz) -> list[str]:
    """Приводит все кампании бизнеса в Директе к выбранному режиму. Возвращает ошибки Директа, а не поднимает их."""
    errors = []
    for row in conn.execute("SELECT DISTINCT external_campaign_id FROM ad_units WHERE business_id = ? AND external_campaign_id IS NOT NULL", (biz["id"],)).fetchall():
        message = await client.set_network(row["external_campaign_id"], network_wanted(biz))
        if message:
            errors.append(message)
    return errors


def save(conn: sqlite3.Connection, business_id: int, mode: str) -> None:
    conn.execute("UPDATE businesses SET placement = ? WHERE id = ?", (mode if mode in MODES else "", business_id))
    conn.commit()
