"""Категории автотаргетинга подбирает и держит в кабинете сам сервис.

ИИ один раз выбирает, какие категории запросов включить бизнесу (услуге без товара нужны «сопутствующие», местному салону нет), и это сохраняется
в профиле аудитории. Дальше при каждой синхронизации сервис сверяет кабинет с профилем и правит группы, где настройки отличаются. Если категории
выбрал сам владелец, сервис их не пересматривает, но в кабинете держит такими, как выбрано.
"""
import json
import sqlite3
from datetime import date

from app.config import get_settings
from app.direct import audience as aud
from app.direct.client import DirectClient, DirectError


async def ensure_autotargeting(client: DirectClient, conn: sqlite3.Connection, business_id: int, today: str | None = None, transport=None) -> None:
    """Подбирает категории (если ещё не подбирали и владелец не выбирал) и применяет их ко всем группам бизнеса. Ошибки не ломают синхронизацию:
    причина запоминается в профиле и видна на экране."""
    biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (business_id,)).fetchone()
    profile = aud.load(biz)
    if profile is None:
        return
    s = get_settings()
    if not profile.get("auto_owner") and not profile.get("auto_reco") and s.fal_key:
        reco = await aud.recommend_autotargeting(biz, profile, s.fal_key, s.fal_text_model, transport)
        if reco:
            profile = {**profile, "autotargeting": {"categories": reco["categories"], "brands": aud.autotargeting_for(profile)["brands"]},
                       "auto_why": reco["why"], "auto_reco": today or date.today().isoformat()}
            aud.save(conn, business_id, profile)
    desired = aud.autotargeting_for(profile)
    groups = sorted({r["external_group_id"] for r in conn.execute(
        "SELECT DISTINCT external_group_id FROM ad_units WHERE business_id = ? AND external_group_id IS NOT NULL", (business_id,))})
    if not groups:
        return
    key = json.dumps([desired, groups], sort_keys=True)
    if profile.get("auto_applied") == key:
        return
    try:
        ids = await client.autotargeting_ids(groups)
        for keyword_id in ids.values():
            await client.update_autotargeting(keyword_id, desired)
    except DirectError as e:
        aud.save(conn, business_id, {**profile, "auto_error": e.full_message()})
        return
    aud.save(conn, business_id, {k: v for k, v in {**profile, "auto_applied": key}.items() if k != "auto_error"})