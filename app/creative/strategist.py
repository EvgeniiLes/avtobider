"""Стратег: по описанию бизнеса и его фото предлагает несколько разных идей объявлений для A/B-теста.

Идея (бриф) = гипотеза («что проверяем»), аудитория, угол подачи, заголовок и текст, какое фото взять и в каком оформлении сделать баннер.
Идеи специально различаются по аудитории, углу, фото и оформлению: иначе сравнивать нечего. Все тексты проходят те же проверки, что и обычные:
без выдуманных фактов, без запрещённых обещаний, в пределах длины.
"""
import json

import httpx

from app.creative.angles import ANGLES, available, effective_description, profile, prompt_block
from app.creative.texts_ai import FAL_CHAT_URL, facts_of
from app.direct.texts import check, unsupported_claims

LOOKS = ("светлое", "яркое", "тёмное", "пастельное", "фото во весь баннер")

SYSTEM = (
    "Ты опытный стратег по рекламе малого бизнеса в Яндекс Директе. Предложи несколько РАЗНЫХ идей объявлений для A/B-теста: "
    "по ним владелец поймёт, какая аудитория, подача и фото работают лучше. Верни только JSON-массив объектов "
    "{\"hypothesis\": \"что проверяем, одно предложение по-русски\", \"audience\": \"кто целевая аудитория этой идеи, коротко\", "
    "\"angle\": \"id угла из списка\", \"title\": \"заголовок до 56 знаков\", \"text\": \"текст до 81 знака\", "
    "\"photo\": номер фото из списка или null, \"look\": число от 0 до 4, "
    "\"mood\": \"настроение картинки ПО-АНГЛИЙСКИ, до 120 знаков, без текста для надписей\"}. "
    "look: 0 светлое, 1 яркое, 2 тёмное, 3 пастельное, 4 фото во весь баннер. "
    "Правила: идеи различаются аудиторией, углом подачи, фото и оформлением (look не повторяй). "
    "Ничего не выдумывай: цены, скидки, адреса, сроки, гарантии и числа бери только из фактов владельца. "
    "Заголовок и текст без восклицательных знаков, без слов «лучший», «№1», «100%». Пиши под действие клиента на кнопке. "
    "Фото выбирай по смыслу заголовка, разным идеям разные фото, если фото несколько. Если фото нет, photo: null."
)


def _user_prompt(biz, photos: list[dict], count: int) -> str:
    p = profile(biz)
    lines = [f"Бизнес: {biz['name']}", f"Чем занимается: {biz['kind']}", f"Город: {biz['city'] or 'не указан'}",
             f"Факты от владельца: {facts_of(biz) or 'нет'}", f"Действие клиента на кнопке: «{p['cta']}»",
             prompt_block(facts_of(biz))]
    if photos:
        lines.append("Фото владельца:")
        lines += [f"Фото {i + 1}: {ph['label']}, оценка {ph['score'] or '?'} из 5. {ph['note']}".strip() for i, ph in enumerate(photos)]
    else:
        lines.append("Фото владельца нет.")
    lines.append(f"Нужно идей: {count}.")
    return "\n".join(lines)


def parse_briefs(raw: str, biz, photos: list[dict], count: int) -> list[dict]:
    """Достаёт идеи из ответа модели и оставляет только те, что прошли проверки текстов."""
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
        good.append({"hypothesis": str(item.get("hypothesis") or "")[:240], "audience": str(item.get("audience") or "")[:120],
                     "angle": angle if angle in ANGLES and angle in allowed else "", "title": title, "text": text,
                     "photo": photo, "look": look % len(LOOKS), "mood": str(item.get("mood") or "")[:140]})
        if len(good) >= count:
            break
    if photos:   # разным идеям разные фото, если модель не указала или повторила
        used = set()
        for i, b in enumerate(good):
            if b["photo"] is None or b["photo"] in used:
                b["photo"] = next((k for k in range(len(photos)) if k not in used), i % len(photos))
            used.add(b["photo"])
    return good


async def plan_briefs(biz, photos: list[dict], count: int, fal_key: str, model: str,
                      transport: httpx.AsyncBaseTransport | None = None) -> list[dict]:
    """Просит у Claude (через fal.ai) идеи. Пустой список, если сервис недоступен или все идеи отбракованы."""
    body = {"model": model, "max_tokens": 4000,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": _user_prompt(biz, photos, count)}]}
    try:
        async with httpx.AsyncClient(timeout=120, transport=transport) as http:
            r = await http.post(FAL_CHAT_URL, json=body, headers={"Authorization": f"Key {fal_key}", "Content-Type": "application/json"})
        if r.status_code != 200:
            return []
        raw = r.json()["choices"][0]["message"]["content"] or ""
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return []
    return parse_briefs(raw, biz, photos, count)
