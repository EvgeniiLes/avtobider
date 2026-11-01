"""Корректировки ставок по полу и возрасту из профиля аудитории.

В поиске нельзя показывать «только на ЦА»: показ решает запрос. Но Директ умеет менять цену клика для разных групп людей, и
стратегии с недельным бюджетом это учитывают. Мягкая схема: целевым группам ставка выше на 15%, остальным ниже на 25%, и ни одну
группу полностью не отключаем (профиль ИИ может ошибаться, а до первых данных это догадка). Срезы у Директа не должны пересекаться,
поэтому берём либо возраст, либо пол, либо все сочетания пола и возраста (не больше 12).
"""
import sqlite3
from datetime import date

from app.direct import audience as aud
from app.direct.client import DirectClient

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
            "applied": applied, "stale": bool(applied) and applied["slices"] != plan}


async def apply_bids(client: DirectClient, conn: sqlite3.Connection, biz, profile: dict, campaign_id: int, today: str | None = None) -> dict:
    """Ставит корректировки на кампанию: прежние по полу и возрасту заменяются целиком. Результат запоминается в профиле."""
    plan = slices(profile)
    existing = await client.demographic_adjustments(campaign_id)
    await client.delete_adjustments([e["id"] for e in existing])   # набор пересекающихся срезов Директ не примет, поэтому сначала убираем старые
    await client.add_demographic_adjustments(campaign_id, plan)
    updated = dict(profile)
    updated["bids"] = {"day": today or date.today().isoformat(), "slices": [{"label": label(s), "percent": s["percent"]} for s in plan]}
    aud.save(conn, biz["id"], updated)
    return updated


async def clear_bids(client: DirectClient, conn: sqlite3.Connection, biz, profile: dict, campaign_id: int) -> dict:
    existing = await client.demographic_adjustments(campaign_id)
    await client.delete_adjustments([e["id"] for e in existing])
    updated = {k: v for k, v in profile.items() if k != "bids"}
    aud.save(conn, biz["id"], updated)
    return updated
