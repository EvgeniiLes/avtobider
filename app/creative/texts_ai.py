"""Тексты объявлений через Claude. Проверка длин и «безопасности» текста делается кодом, а не на слово модели."""
import json
import re

import httpx

from app.direct.texts import TEXT_MAX, TITLE_MAX, check, fit

API_URL = "https://api.anthropic.com/v1/messages"
FORBIDDEN = re.compile(r"(?i)\b(лучш\w+|№\s?1|номер один|гарант\w+|100\s?%|самы\w+ дешев\w+|бесплатно|скидк\w+ \d+)")

SYSTEM = (
    "Ты пишешь тексты для объявлений Яндекс Директа на русском для малого бизнеса. "
    f"Заголовок не длиннее {TITLE_MAX} символов, текст не длиннее {TEXT_MAX} символов. "
    "Используй только факты из описания бизнеса. Не выдумывай скидки, цены, акции, гарантии, опыт и награды, "
    "не пиши превосходные степени («лучший», «самый») и слово «бесплатно». Без эмодзи и восклицательных знаков. "
    "Каждый вариант с другим акцентом (запись, удобство, расположение, услуга, доверие, спокойный тон). "
    "Ответ: только JSON-массив объектов {\"title\": \"...\", \"text\": \"...\", \"angle\": \"...\"}, без пояснений."
)


def build_prompt(biz, count: int, hint: str = "") -> str:
    parts = [
        f"Бизнес: {biz['name']}", f"Чем занимается: {biz['kind']}", f"Город: {biz['city'] or 'не указан'}",
        f"Описание от владельца: {biz['description'] or 'нет'}",
    ]
    tones = {"gentle": "нежный", "calm": "спокойный", "energetic": "энергичный", "strict": "строгий", "friendly": "дружелюбный"}
    if biz["tone"]:
        parts.append(f"Желаемый тон: {tones.get(biz['tone'], biz['tone'])}")
    if hint.strip():
        parts.append(f"Пожелание владельца: {hint.strip()[:300]}")
    parts.append(f"Нужно вариантов: {count}.")
    return "\n".join(parts)


def parse_variants(raw: str) -> list[dict]:
    """Достаёт JSON-массив из ответа модели и оставляет только объявления, прошедшие проверку."""
    match = re.search(r"\[.*\]", raw, re.S)
    if not match:
        return []
    try:
        items = json.loads(match.group(0))
    except ValueError:
        return []
    good, seen = [], set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        title, text = str(item.get("title", "")).strip(), str(item.get("text", "")).strip()  # слишком длинное не режем, а отбраковываем
        if check(title, text) or FORBIDDEN.search(title + " " + text) or "!" in title + text or title.lower() in seen:
            continue
        seen.add(title.lower())
        good.append({"title": title, "text": text, "angle": str(item.get("angle", ""))[:60]})
    return good


async def generate_texts(biz, count: int, api_key: str, model: str, hint: str = "",
                         transport: httpx.AsyncBaseTransport | None = None) -> list[dict]:
    """Просит у Claude варианты. Возвращает только годные; пустой список, если сервис недоступен или все отбракованы."""
    body = {
        # Запас большой: модель может «подумать» перед ответом, и мысли тоже считаются в лимит.
        "model": model, "max_tokens": 8000, "system": SYSTEM,
        "messages": [{"role": "user", "content": build_prompt(biz, count, hint)}],
    }
    async with httpx.AsyncClient(timeout=60, transport=transport) as http:
        try:
            r = await http.post(API_URL, json=body, headers={
                "x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        except httpx.HTTPError:
            return []
    if r.status_code != 200:
        return []
    try:
        raw = "".join(block.get("text", "") for block in r.json().get("content", []))
    except ValueError:
        return []
    return parse_variants(raw)
