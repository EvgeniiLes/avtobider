"""Перенос новых данных в уже созданные кампании: дополнения объявлений, новые фразы, минус-слова, категории автотаргетинга.

Идущую рекламу сервис не пересоздаёт: он сверяет её с тем, что сейчас в профиле и описании, и добавляет недостающее. Ничего не удаляет и не останавливает.
Объявлениям, у которых меняются второй заголовок, ссылка или уточнения, Директ может заново отправить их на проверку.
"""
import sqlite3

from app.config import get_settings
from app.direct import audience as aud
from app.direct import extras as ad_extras
from app.direct.autotarget import ensure_autotargeting
from app.direct.client import DirectClient, DirectError


async def refresh_campaigns(client: DirectClient, conn: sqlite3.Connection, biz, today: str, transport=None) -> dict:
    """Возвращает {"changes": [что сделали по-русски], "problems": [что не вышло]}."""
    from app.direct.launch import _district_phrases   # здесь, чтобы не было круга
    from app.creative.angles import districts_of
    changes: list[str] = []
    problems: list[str] = []
    units = conn.execute("SELECT * FROM ad_units WHERE business_id = ? AND external_ad_id IS NOT NULL AND external_group_id IS NOT NULL", (biz["id"],)).fetchall()
    if not units:
        return {"changes": [], "problems": ["Реклама в Директе ещё не создана, обновлять пока нечего."]}
    profile = aud.load(biz)
    s = get_settings()

    # 1. второй заголовок, отображаемая ссылка, уточнения у объявлений, которым их ещё не ставили
    titles = [u["title"] for u in units]
    saved = ad_extras.load(biz)
    if not ad_extras.covers(saved, titles):
        fresh = await ad_extras.generate(biz, titles, s.fal_key, s.fal_text_model, transport)
        if fresh is not None:
            saved = {**fresh, "applied": (saved or {}).get("applied", [])}
            ad_extras.save(conn, biz["id"], saved)
    if saved and ad_extras.covers(saved, titles):
        applied = set(str(i) for i in saved.get("applied", []))
        todo = [u for u in units if str(u["external_ad_id"]) not in applied]
        if todo and (saved.get("callouts") or saved.get("display_path") or saved.get("title2")):
            try:
                callout_ids = await client.add_callouts(saved.get("callouts") or [])
            except DirectError as e:
                callout_ids = []
                problems.append(f"Уточнения не добавлены: {e.full_message()}")
            done = 0
            for u in todo:
                try:
                    await client.add_ad_extras(int(u["external_ad_id"]), ad_extras.title2_for(saved, u["title"]), ad_extras.fit_path(saved), callout_ids)
                    applied.add(str(u["external_ad_id"]))
                    done += 1
                except DirectError as e:
                    problems.append(f"«{u['title']}»: {e.full_message()}")
            if done:
                changes.append(f"Дополнения (второй заголовок, уточнения, ссылка) добавлены в {done} объявл.")
            ad_extras.save(conn, biz["id"], {**saved, "applied": sorted(applied)})

    # 2. новые фразы в группах и минус-слова в кампаниях
    if profile:
        groups = conn.execute("SELECT DISTINCT u.external_group_id AS gid, u.external_campaign_id AS cid, c.intent AS intent FROM ad_units u "
                              "JOIN campaigns c ON c.id = u.campaign_id WHERE u.business_id = ? AND u.external_group_id IS NOT NULL", (biz["id"],)).fetchall()
        by_name = {i["name"]: i for i in aud.intents_for(profile)}
        everything = [k for i in by_name.values() for k in i["keywords"]] or list(profile.get("keywords", []))
        districts = districts_of(biz)
        try:
            have = await client.group_keywords([g["gid"] for g in groups])
            added = 0
            for g in groups:
                wanted = list(by_name[g["intent"]]["keywords"]) if g["intent"] in by_name else everything
                wanted = wanted + _district_phrases(wanted, districts)
                missing = [k for k in dict.fromkeys(wanted) if k.lower() not in have.get(g["gid"], set())]
                if missing:
                    result = await client.add_keywords(g["gid"], missing, None)
                    added += result["added"]
            if added:
                changes.append(f"Добавлено ключевых фраз: {added}")
        except DirectError as e:
            problems.append(f"Фразы: {e.full_message()}")
        try:
            negatives = aud.effective_negatives(biz, profile)
            for cid in sorted({g["cid"] for g in groups}):
                await client.set_negative_keywords(cid, negatives)
            changes.append(f"Минус-слова обновлены в {len({g['cid'] for g in groups})} кампаниях ({len(negatives)} слов)")
        except DirectError as e:
            problems.append(f"Минус-слова: {e.full_message()}")

    # 3. категории автотаргетинга
    try:
        await ensure_autotargeting(client, conn, biz["id"], today, transport)
        after = aud.load(conn.execute("SELECT * FROM businesses WHERE id = ?", (biz["id"],)).fetchone()) or {}
        if after.get("auto_error"):
            problems.append(f"Автотаргетинг: {after['auto_error']}")
    except DirectError as e:
        problems.append(f"Автотаргетинг: {e.full_message()}")
    return {"changes": changes, "problems": problems}