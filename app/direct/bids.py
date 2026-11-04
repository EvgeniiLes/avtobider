"""Корректировки ставок по полу и возрасту из профиля аудитории.

В поиске нельзя показывать «только на ЦА»: показ решает запрос. Но Директ умеет менять цену клика для разных групп людей, и
стратегии с недельным бюджетом это учитывают. Мягкая схема: целевым группам ставка выше на 15%, остальным ниже на 25%, и ни одну
группу полностью не отключаем (профиль ИИ может ошибаться, а до первых данных это догадка). Срезы у Директа не должны пересекаться,
поэтому берём либо возраст, либо пол, либо все сочетания пола и возраста (не больше 12).
"""
import sqlite3
from datetime import date

from app.direct import audience as aud
from app.direct.client import DirectClient, DirectError

UP = 1.15
DOWN = 0.75

AGE_CODES = {"18-24": "AGE_18_24", "25-34": "AGE_25_34", "35-44": "AGE_35_44", "45-54": "AGE_45_54", "55+": "AGE_55"}
ALL_AGES = ["AGE_0_17", *AGE_CODES.values()]
GENDER_CODES = {"female": "GENDER_FEMALE", "male": "GENDER_MALE"}
AGE_LABELS = {"AGE_0_17": "до 18", "AGE_18_24": "18–24", "AGE_25_34": "25–34", "AGE_35_44": "35–44", "AGE_45_54": "45–54", "AGE_55": "55+"}
GENDER_LABELS = {"GENDER_FEMALE": "женщины", "GENDER_MALE": "мужчины"}


def slices(profile: dict | None) -> list[dict]:
    """Срезы с коэффициентом в процентах. Пусто, если профиль не привязан ни к полу, ни к возрасту: корректировать нечего."""
    if not profile:
        return []
    gender = GENDER_CODES.get(profile.get("gender", "any"))
    target_ages = {AGE_CODES[a] for a in profile.get("ages", []) if a in AGE_CODES}
    if not gender and not target_ages:
        return []
    genders = [GENDER_CODES["male"], GENDER_CODES["female"]] if gender else [None]
    ages = ALL_AGES if target_ages else [None]
    result = []
    for g in genders:
        for a in ages:
            factor = (1.0 if g is None else UP if g == gender else DOWN) * (1.0 if a is None else UP if a in target_ages else DOWN)
            percent = round(100 * factor)
            if percent != 100:
                result.append({"gender": g, "age": a, "percent": percent})
    return result


def label(item: dict) -> str:
    parts = [GENDER_LABELS.get(item.get("gender"), ""), AGE_LABELS.get(item.get("age"), "")]
    return " ".join(p for p in parts if p) or "все"


def describe(profile: dict | None, launched: bool) -> dict:
    """Что изменится в ставках, что уже применено и не устарело ли оно после правки профиля."""
    plan = [{"label": label(s), "percent": s["percent"]} for s in slices(profile)]
    applied = (profile or {}).get("bids")
    return {"plan": plan, "up": round((UP - 1) * 100), "down": round((1 - DOWN) * 100), "launched": launched, "needed": bool(plan),
            "applied": applied, "stale": bool(applied) and applied["slices"] != plan,
            "off": bool((profile or {}).get("bids_off")), "error": (profile or {}).get("bids_error", "")}


async def apply_bids(client: DirectClient, conn: sqlite3.Connection, biz, profile: dict, campaign_id: int, today: str | None = None) -> dict:
    """Ставит корректировки на кампанию: прежние по полу и возрасту заменяются целиком. Результат запоминается в профиле."""
    plan = slices(profile)
    existing = await client.demographic_adjustments(campaign_id)
    await client.delete_adjustments([e["id"] for e in existing])   # набор пересекающихся срезов Директ не примет, поэтому сначала убираем старые
    await client.add_demographic_adjustments(campaign_id, plan)
    updated = {k: v for k, v in profile.items() if k not in ("bids_off", "bids_error")}
    shown = [{"label": label(s), "percent": s["percent"]} for s in plan]
    before = profile.get("bids")
    campaigns = set(before.get("campaigns", [])) if before and before["slices"] == shown else set()   # те же срезы уже стоят на других кампаниях бизнеса
    updated["bids"] = {"day": today or date.today().isoformat(), "slices": shown, "campaigns": sorted(campaigns | {campaign_id})}
    aud.save(conn, biz["id"], updated)
    return updated


def campaign_ids(conn: sqlite3.Connection, business_id: int) -> list[int]:
    """Кампании бизнеса в Директе: у каждой группы объявлений своя кампания, и корректировки нужны на каждой."""
    return [r["external_campaign_id"] for r in conn.execute(
        "SELECT DISTINCT external_campaign_id FROM ad_units WHERE business_id = ? AND external_campaign_id IS NOT NULL ORDER BY external_campaign_id", (business_id,))]


async def apply_bids_all(client: DirectClient, conn: sqlite3.Connection, biz, profile: dict, today: str | None = None) -> dict:
    for campaign_id in campaign_ids(conn, biz["id"]):
        profile = await apply_bids(client, conn, biz, profile, campaign_id, today)
    return profile


async def clear_bids_all(client: DirectClient, conn: sqlite3.Connection, biz, profile: dict) -> dict:
    for campaign_id in campaign_ids(conn, biz["id"]):
        profile = await clear_bids(client, conn, biz, profile, campaign_id)
    return profile


async def clear_bids(client: DirectClient, conn: sqlite3.Connection, biz, profile: dict, campaign_id: int) -> dict:
    existing = await client.demographic_adjustments(campaign_id)
    await client.delete_adjustments([e["id"] for e in existing])
    updated = {k: v for k, v in profile.items() if k not in ("bids", "bids_error")}
    updated["bids_off"] = True   # владелец убрал корректировки сам: автоматически их больше не возвращаем
    aud.save(conn, biz["id"], updated)
    return updated


async def ensure_bids(client: DirectClient, conn: sqlite3.Connection, business_id: int, today: str | None = None) -> None:
    """Сервис сам держит корректировки по полу и возрасту в кабинете в соответствии с профилем: ставит после запуска и обновляет, если профиль изменился.
    Не трогает, если владелец убрал их сам. Ошибка Директа не ломает синхронизацию: она запоминается в профиле и видна на экране, а попытка повторится."""
    biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (business_id,)).fetchone()
    profile = aud.load(biz)
    if profile is None or profile.get("bids_off"):
        return
    ids = campaign_ids(conn, business_id)
    plan = slices(profile)
    applied = profile.get("bids")
    same = bool(applied) and applied["slices"] == [{"label": label(s), "percent": s["percent"]} for s in plan]
    todo = [c for c in ids if c not in set(applied.get("campaigns", []))] if same else ids   # срезы не менялись: ставим только на кампании, где их ещё нет
    if not ids or not plan or not todo:
        return
    try:
        for campaign_id in todo:
            profile = await apply_bids(client, conn, biz, profile, campaign_id, today)
    except DirectError as e:
        aud.save(conn, business_id, {**profile, "bids_error": e.full_message()})
