"""Стратег: по описанию бизнеса и его фото предлагает несколько разных идей объявлений для A/B-теста.

Идея (бриф) = гипотеза («что проверяем»), аудитория, угол подачи, заголовок и текст, какое фото взять и в каком оформлении сделать баннер.
Идеи специально различаются по аудитории, углу, фото и оформлению: иначе сравнивать нечего. Все тексты проходят те же проверки, что и обычные:
без выдуманных фактов, без запрещённых обещаний, в пределах длины.
"""
import json
import re

import httpx

from app.creative.angles import ANGLES, available, effective_description, profile, prompt_block, where_text
from app.creative.refs import REFS, catalog_text
from app.creative.texts_ai import FAL_CHAT_URL, JUDGE_SYSTEM, facts_of, parse_scores, pick_best
from app.direct.texts import check, check_hook, unsupported_claims

LOOKS = ("светлое", "яркое", "тёмное", "пастельное", "фото во весь баннер")

SYSTEM = (
    "Ты опытный стратег по рекламе малого бизнеса в Яндекс Директе. Предложи несколько РАЗНЫХ идей объявлений для A/B-теста: "
    "по ним владелец поймёт, какая аудитория, подача и фото работают лучше. Верни только JSON-массив объектов "
    "{\"hypothesis\": \"что проверяем, одно предложение по-русски\", \"audience\": \"кто целевая аудитория этой идеи, коротко\", "
    "\"angle\": \"id угла из списка\", \"title\": \"заголовок до 56 знаков\", \"text\": \"текст до 81 знака\", "
    "\"hook\": \"короткий текст для баннера, 2-5 слов, до 40 знаков, без точки в конце, не копия заголовка\", "
    "\"photo\": номер фото из списка или null, \"look\": число от 0 до 4, "
    "\"mood\": \"настроение картинки ПО-АНГЛИЙСКИ, до 120 знаков, без текста для надписей\", "
    "\"ref\": номер референса из списка или null, "
    "\"direction\": \"ПО-АНГЛИЙСКИ, 1-2 предложения: как применить приём референса к фото этого бизнеса (композиция, цвет, вес шрифта, где текст), без копирования\"}. "
    "look: 0 светлое, 1 яркое, 2 тёмное, 3 пастельное, 4 фото во весь баннер. "
    "Правила: идеи различаются аудиторией, углом подачи, фото и оформлением (look не повторяй). "
    "Ничего не выдумывай: цены, скидки, адреса, сроки, гарантии и числа бери только из фактов владельца. "
    "Заголовок и текст без восклицательных знаков, без слов «лучший», «№1», «100%». Пиши под действие клиента на кнопке. "
    "Пиши как сильный маркетолог и специалист по Директу: в заголовке слова из запросов клиента (ключевые фразы из данных рекламы), конкретика вместо общих слов, "
    "в тексте конкретная выгода из фактов и понятное действие, без повторов заголовка; если даны данные рекламы, повторяй то, что работает, и не повторяй слабое. "
    "Фото выбирай по смыслу заголовка, разным идеям разные фото, если фото несколько. Если фото нет, photo: null. "
    "Референс: выбери для каждой идеи один референс-вдохновение из каталога, предпочитая близкую нишу, но можно взять приём из другой ниши, "
    "если он подходит по смыслу. Разным идеям разные референсы. Если фото нет, ref: null. Приём нужно переосмыслить под бизнес, а не копировать. "
    "Если приём предполагает огромное слово, это должно быть короткое слово из заголовка (до 8 букв), написанное целиком одной строкой, без переносов. Никаких скидок и акций в приёме, если их нет в фактах. "
    "В direction не проси писать контакты, телефон, сайт, адрес, название бизнеса, логотип и любые слова, которых нет в заголовке и на кнопке; "
    "главным объектом остаётся фото владельца (животное, человек, товар), не заменяй его предметами из референса."
)


def _user_prompt(biz, photos: list[dict], count: int, context: str = "") -> str:
    p = profile(biz)
    lines = [f"Бизнес: {biz['name']}", f"Чем занимается: {biz['kind']}", f"Где показываем рекламу: {where_text(biz)}",
             f"Факты от владельца: {facts_of(biz) or 'нет'}", f"Действие клиента на кнопке: «{p['cta']}»",
             prompt_block(facts_of(biz))]
    if photos:
        lines.append("Фото владельца:")
        lines += [f"Фото {i + 1}: {ph['label']}, оценка {ph['score'] or '?'} из 5. {ph['note']}".strip() for i, ph in enumerate(photos)]
    else:
        lines.append("Фото владельца нет.")
    lines.append("Каталог референсов (номер: [ниша] приём):\n" + catalog_text())
    if context.strip():   # запросы клиентов и результаты рекламы: заголовки пишутся под них
        lines.append("Данные рекламы этого бизнеса:\n" + context.strip()[:2500])
    lines.append(f"Нужно идей: {count}.")
    return "\n".join(lines)


FORBIDDEN_IN_DIRECTION = re.compile(r"contact|phone|tel\b|website|web site|url|domain|address|logo|brand name|watermark|qr|e-?mail|instagram|whatsapp", re.I)


def clean_direction(text: str) -> str:
    """Из подсказки модели картинок убираются предложения про контакты, адреса, сайты и логотипы: на баннере их быть не должно."""
    sentences = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return " ".join(s for s in sentences if s and not FORBIDDEN_IN_DIRECTION.search(s))[:500]


def _parse_items(raw: str, biz, photos: list[dict], count: int) -> list[dict]:
    """Достаёт идеи из ответа модели и оставляет только те, что прошли проверки текстов (не больше count)."""
    start, end = raw.find("["), raw.rfind("]")
    try:
        items = json.loads(raw[start:end + 1]) if start != -1 and end > start else []
    except ValueError:
        return []
    facts = facts_of(biz)
    allowed = set(available(facts))
    good, seen = [], set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        title, text = str(item.get("title") or "").strip(), str(item.get("text") or "").strip()
        if not title or check(title, text) or unsupported_claims(f"{title} {text}", facts) or "!" in title + text or title.lower() in seen:
            continue
        seen.add(title.lower())
        angle = str(item.get("angle") or "")
        photo = item.get("photo")
        photo = photo - 1 if isinstance(photo, int) and 1 <= photo <= len(photos) else None
        try:
            look = int(item.get("look"))
        except (TypeError, ValueError):
            look = len(good)
        ref = item.get("ref")
        hook = " ".join(str(item.get("hook") or "").split()).strip(" .,;:—-")
        if hook and (check_hook(hook) or unsupported_claims(hook, facts)):   # плохой текст для баннера не бракует идею: на баннере будет заголовок
            hook = ""
        good.append({"hypothesis": str(item.get("hypothesis") or "")[:240], "audience": str(item.get("audience") or "")[:120],
                     "angle": angle if angle in ANGLES and angle in allowed else "", "title": title, "text": text, "hook": hook,
                     "photo": photo, "look": look % len(LOOKS), "mood": str(item.get("mood") or "")[:140],
                     "ref": ref if isinstance(ref, int) and ref in REFS else None, "direction": clean_direction(str(item.get("direction") or ""))})
        if len(good) >= count:
            break
    return good


def _finalize(good: list[dict], photos: list[dict]) -> list[dict]:
    """Референсы и фото у идей разные (референс только вместе с фото)."""
    seen_refs: set[int] = set()   # референс только вместе с фото и у разных идей разный
    for b in good:
        if not photos or b["ref"] in seen_refs:
            b["ref"] = None
        elif b["ref"]:
            seen_refs.add(b["ref"])
    if photos:   # разным идеям разные фото, если модель не указала или повторила
        used = set()
        for i, b in enumerate(good):
            if b["photo"] is None or b["photo"] in used:
                b["photo"] = next((k for k in range(len(photos)) if k not in used), i % len(photos))
            used.add(b["photo"])
    return good


def parse_briefs(raw: str, biz, photos: list[dict], count: int) -> list[dict]:
    return _finalize(_parse_items(raw, biz, photos, count), photos)


async def _ask(http: httpx.AsyncClient, system: str, user: str, fal_key: str, model: str) -> str:
    body = {"model": model, "max_tokens": 6000, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    r = await http.post(FAL_CHAT_URL, json=body, headers={"Authorization": f"Key {fal_key}", "Content-Type": "application/json"})
    return (r.json()["choices"][0]["message"]["content"] or "") if r.status_code == 200 else ""


async def plan_briefs(biz, photos: list[dict], count: int, fal_key: str, model: str,
                      transport: httpx.AsyncBaseTransport | None = None, context: str = "") -> list[dict]:
    """Идеи как у маркетолога: стратег предлагает больше нужного (разные аудитории и приёмы), «старший специалист» оценивает по чек-листу, остаются лучшие.
    Пустой список, если сервис недоступен или все идеи отбракованы."""
    pool = min(8, count + 3)
    try:
        async with httpx.AsyncClient(timeout=120, transport=transport) as http:
            raw = await _ask(http, SYSTEM, _user_prompt(biz, photos, pool, context), fal_key, model)
            items = _parse_items(raw, biz, photos, pool) if raw else []
            if len(items) > count:
                listing = "\n".join(
                    f"{i + 1}. заголовок: {b['title']} | текст: {b['text']} | hook: {b['hook'] or '-'} | угол: {b['angle'] or '-'} | аудитория: {b['audience']}"
                    for i, b in enumerate(items))
                judge = (f"Бизнес: {biz['name']}, {biz['kind']}.\nФакты: {facts_of(biz)[:700]}\n"
                         + (f"Данные рекламы:\n{context.strip()[:1500]}\n" if context.strip() else "")
                         + f"Варианты (нужно оставить {count}, с разными аудиториями и приёмами):\n{listing}")
                verdict = await _ask(http, JUDGE_SYSTEM, judge, fal_key, model)
                items = pick_best(items, parse_scores(verdict, len(items)), count)
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return []
    return _finalize(items[:count], photos)

