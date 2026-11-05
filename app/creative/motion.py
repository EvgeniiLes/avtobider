"""Оживление баннера в видео: идеи движения под конкретное объявление и страховка, чтобы текст на баннере не плыл.

Видео делает модель по готовому баннеру (картинка плюс короткое описание движения). Идеи пишет ИИ индивидуально: по бизнесу, заголовку и сцене
баннера. Без ИИ даём несколько общих безопасных вариантов.
"""
import json
import re

import httpx

FAL_CHAT_URL = "https://fal.run/openrouter/router/openai/v1/chat/completions"
LABEL_MAX = 32
PROMPT_MAX = 300

GUARD = (" Keep every word of text, the button and the logo on the picture perfectly still, sharp and unchanged, and do not add any new text, letters, logos or symbols. "
         "Only the scene moves, with natural, smooth, physically plausible motion; people and objects keep their shape and faces.")

DEFAULT_MOTION = "Slow smooth camera push-in with a gentle natural movement in the scene."

FALLBACK = [
    {"label": "Плавный наезд камеры", "prompt": "Slow smooth camera push-in towards the main subject, subtle natural movement in the scene."},
    {"label": "Мягкое движение света", "prompt": "Soft light slowly shifts across the scene, gentle reflections and a calm atmosphere, the camera stays almost still."},
    {"label": "Живые детали", "prompt": "Small natural details move gently: fabric, hair, steam, water or dust in the light, the main subject stays steady."},
]

SYSTEM = (
    "Ты режиссёр коротких рекламных видео (5 секунд) для малого бизнеса. Тебе дают бизнес, заголовок объявления и сцену готового баннера. "
    "Придумай 4 разных способа оживить эту статичную картинку: что именно двигается (камера, вода, пена, пар, волосы, ткань, свет, люди, животные). "
    "Каждый способ конкретный и подходит именно этому бизнесу и этой сцене, а не общий. Правила: двигается только то, что уже есть на картинке; "
    "новых предметов, людей и надписей не добавляй; текст, кнопка и логотип на баннере остаются неподвижными; движение естественное и плавное, "
    "без резких смен кадра и превращений. "
    "Ответ: только JSON-массив из 4 объектов {\"label\": \"название по-русски, до 30 знаков\", \"prompt\": \"описание движения ПО-АНГЛИЙСКИ, 1-2 предложения\"}."
)


def build_prompt(biz, title: str, scene: str) -> str:
    return "\n".join([f"Бизнес: {biz['name']}, {biz['kind']}", f"Заголовок на баннере: {title}", f"Сцена баннера: {scene or 'не указана'}"])


def clean(raw: str) -> list[dict]:
    match = re.search(r"\[.*\]", raw or "", re.S)
    if not match:
        return []
    try:
        items = json.loads(match.group(0))
    except ValueError:
        return []
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        label = re.sub(r"\s+", " ", str(item.get("label", ""))).strip().rstrip(".")
        prompt = re.sub(r"\s+", " ", str(item.get("prompt", ""))).strip()
        if 3 <= len(label) <= LABEL_MAX and 10 <= len(prompt) <= PROMPT_MAX and label.lower() not in {o["label"].lower() for o in out}:
            out.append({"label": label, "prompt": prompt})
    return out[:4]


async def ideas(biz, title: str, scene: str, fal_key: str, model: str, transport: httpx.AsyncBaseTransport | None = None) -> tuple[list[dict], bool]:
    """(идеи, от ИИ ли они). Без ключа или при сбое возвращает общие варианты и False."""
    if not fal_key:
        return list(FALLBACK), False
    body = {"model": model, "max_tokens": 3500, "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": build_prompt(biz, title, scene)}]}
    try:
        async with httpx.AsyncClient(timeout=60, transport=transport) as http:
            r = await http.post(FAL_CHAT_URL, json=body, headers={"Authorization": f"Key {fal_key}", "Content-Type": "application/json"})
        found = clean(r.json()["choices"][0]["message"]["content"] or "") if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        found = []
    return (found, True) if len(found) >= 2 else (list(FALLBACK), False)


def final_prompt(user_prompt: str) -> str:
    """Что уходит модели: пожелание (или спокойное движение по умолчанию) плюс страховка для текста и логотипа."""
    text = re.sub(r"\s+", " ", user_prompt or "").strip()[:PROMPT_MAX] or DEFAULT_MOTION
    return text.rstrip(".") + "." + GUARD

async def to_english(text: str, fal_key: str, model: str, transport: httpx.AsyncBaseTransport | None = None) -> str:
    """Пожелание владельца по-русски в короткое английское описание движения (видеомодель лучше понимает английский). При сбое возвращаем как есть."""
    if not fal_key or not re.search(r"[а-яё]", text or "", re.I):
        return text
    body = {"model": model, "max_tokens": 1500, "messages": [{"role": "user", "content": (
        "Переведи на английский как короткое описание движения для видео (1-2 предложения, только перевод, без пояснений и кавычек): " + text)}]}
    try:
        async with httpx.AsyncClient(timeout=45, transport=transport) as http:
            r = await http.post(FAL_CHAT_URL, json=body, headers={"Authorization": f"Key {fal_key}", "Content-Type": "application/json"})
        out = (r.json()["choices"][0]["message"]["content"] or "").strip() if r.status_code == 200 else ""
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        out = ""
    return out[:PROMPT_MAX] if len(out) >= 5 else text
