"""Если у работающей рекламы нет показов, сервис сам расширяет охват, шаг за шагом, и сообщает об этом владельцу.

Признак: кампания идёт не меньше двух суток, есть активные объявления, а показов за последние двое суток ноль. Лестница (раз в два дня, пока показов нет):
1. включить категорию «сопутствующие» запросы автотаргетинга, 2. «альтернативные», 3. «широкие»; на первых двух шагах (и всегда, когда у многих фраз
статус «мало показов») добавить в группы более широкие фразы, которые подбирает ИИ. Если категории выбрал сам владелец, их не трогаем, только фразы.
4. Если бизнес начал только с поиска (онлайн-услуга, режим не выбирал владелец), включить рекламную сеть.
Показы появились: лестница больше не идёт. Каждый шаг записывается в профиль (traffic) и приходит владельцу в Telegram.
"""
import sqlite3

from app.config import get_settings
from app.direct import audience as aud
from app.direct import placement
from app.direct.autotarget import ensure_autotargeting
from app.direct.client import DirectClient, DirectError
from app.engine.util import day_add, days_between

STALL_DAYS = 2      # столько суток кампания идёт без единого показа
STEP_GAP = 2        # и столько суток ждём между шагами
ORDER = ("accessory", "alternative", "broader")
RARELY_SHARE = 0.5  # доля фраз с малым числом показов, после которой фразы расширяем в любом случае


async def _open_network(client: DirectClient, conn: sqlite3.Connection, biz, profile: dict, traffic: dict, today: str) -> None:
    """Реклама без сетей (так начинают онлайн-услуги) всё равно не показывается: включаем сети и записываем режим «поиск и сети», чтобы сервис его не откатил.
    Если режим выбрал сам владелец, его решение не трогаем."""
    if placement.mode_of(biz) or placement.network_wanted(biz):
        return
    placement.save(conn, biz["id"], "all")
    fresh = conn.execute("SELECT * FROM businesses WHERE id = ?", (biz["id"],)).fetchone()
    try:
        await placement.apply(client, conn, fresh)
    except DirectError:
        pass   # сети включит и обычная синхронизация: режим уже записан
    traffic["step"] += 1
    traffic["day"] = today
    traffic["log"] = (list(traffic.get("log", [])) + [{"day": today, "step": traffic["step"], "did": ["включили показы в рекламной сети: только в поиске показов нет"], "rarely": 0}])[-10:]
    aud.save(conn, biz["id"], {**profile, "traffic": traffic})


def stalled(conn: sqlite3.Connection, biz, today: str) -> bool:
    from app.reports import period   # здесь, чтобы не было круга
    started = conn.execute("SELECT MIN(created_day) FROM campaigns WHERE business_id = ? AND source = 'direct'", (biz["id"],)).fetchone()[0]
    if not started or days_between(started, today) < STALL_DAYS + 1:
        return False
    if not conn.execute("SELECT 1 FROM ad_units WHERE business_id = ? AND state = 'active'", (biz["id"],)).fetchone():
        return False
    return period(conn, biz["id"], day_add(today, -STALL_DAYS), day_add(today, -1))["impressions"] == 0


async def ensure_traffic(client: DirectClient, conn: sqlite3.Connection, business_id: int, today: str, transport=None) -> None:
    biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (business_id,)).fetchone()
    profile = aud.load(biz)
    if profile is None or not stalled(conn, biz, today):
        return
    traffic = dict(profile.get("traffic") or {"step": 0, "day": "", "log": []})
    if traffic["day"] and days_between(traffic["day"], today) < STEP_GAP:
        return
    if traffic["step"] >= len(ORDER):
        await _open_network(client, conn, biz, profile, traffic, today)   # категории кончились, показов всё нет: последний шаг, сети
        return
    s = get_settings()
    did: list[str] = []
    cats = dict(aud.autotargeting_for(profile)["categories"])
    if not profile.get("auto_owner"):
        key = next((k for k in ORDER if not cats.get(k)), None)
        if key:
            cats[key] = True
            did.append(f"включили в автотаргетинге категорию «{aud.CATEGORIES[key]}»")
    groups = sorted({r["external_group_id"] for r in conn.execute(
        "SELECT DISTINCT external_group_id FROM ad_units WHERE business_id = ? AND external_group_id IS NOT NULL", (business_id,))})
    rarely_share = 0.0
    try:
        total, rarely = await client.keyword_serving(groups)
        rarely_share = rarely / total if total else 0.0
    except DirectError:
        pass   # статус показа фраз не получили: решаем по лестнице
    updated = dict(profile)
    if traffic["step"] < 2 or rarely_share >= RARELY_SHARE:
        wider = await aud.widen_keywords(biz, profile, s.fal_key, s.fal_text_model, transport)
        if wider:
            try:
                for gid in groups:
                    await client.add_keywords(gid, wider, None)
                updated["keywords"] = list(profile.get("keywords", [])) + wider
                did.append(f"добавили более широкие фразы: {', '.join(wider[:4])}" + (" и другие" if len(wider) > 4 else ""))
            except DirectError:
                pass
    traffic["step"] += 1
    traffic["day"] = today
    traffic["log"] = (list(traffic.get("log", [])) + [{"day": today, "step": traffic["step"], "did": did, "rarely": round(rarely_share * 100)}])[-10:]
    updated["traffic"] = traffic
    if not profile.get("auto_owner"):
        updated["autotargeting"] = {"categories": cats, "brands": aud.autotargeting_for(profile)["brands"]}
    aud.save(conn, business_id, updated)
    await ensure_autotargeting(client, conn, business_id, today, transport)   # новые категории уходят в кабинет сразу