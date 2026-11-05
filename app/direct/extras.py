"""Дополнения к объявлениям Директа: второй заголовок, уточнения и отображаемая ссылка.

Это бесплатные элементы, которые увеличивают заметность и CTR объявления. ИИ предлагает их, а код проверяет всё по правилам Директа
(длины, знаки, слова) и по фактам владельца: уточнение или заголовок с цифрой, скидкой или обещанием, которых нет в описании, отбрасываются.
Результат хранится в businesses.ad_extras (JSON), чтобы предпросмотр и запуск использовали одни и те же тексты.
"""
import json
import re
import sqlite3

import httpx

from app.creative.angles import effective_description, where_text
from app.direct.texts import fit, unsupported_claims

TITLE2_MAX = 30       # второй заголовок (документация Директа; узкие символы !,.;:" не считаются, мы их не используем)
CALLOUT_MAX = 25      # уточнение
CALLOUTS_MAX = 6      # больше четырёх-шести Директ в объявлении всё равно не показывает
PATH_MAX = 20         # отображаемая ссылка: домен/путь
FAL_CHAT_URL = "https://fal.run/openrouter/router/openai/v1/chat/completions"

SYSTEM = (
    "Ты опытный специалист по Яндекс Директу (поиск) и копирайтер для малого бизнеса в России. Тебе дают бизнес и заголовки его объявлений. "
    "Нужно составить дополнения, которые повышают заметность и CTR, и ничего не выдумывать: только то, что бизнес сам про себя рассказал.\n"
    "1) title2 для каждого заголовка (в том же порядке): второй заголовок до 30 символов. Он дополняет первый, а не повторяет его: "
    "действие или выгода («Запись онлайн», «Работаем без выходных», «Бережно, без седативных»). Без восклицательных знаков, без ВЕРХНЕГО РЕГИСТРА, "
    "без точки в конце, без номеров телефонов и сайтов.\n"
    "2) callouts: 4-6 уточнений до 25 символов каждое: короткие факты о бизнесе из описания (график, формат, особенность услуги, оснащение). "
    "Каждое самостоятельное и разное, без точки в конце, без восклицательных знаков, без цен и скидок, если их нет в описании. "
    "Не повторяй слова одного и того же уточнения.\n"
    "3) display_path: отображаемая ссылка до 20 символов: строчные русские или латинские буквы, цифры и дефис, одно-два слова по сути услуги "
    "(например «груминг-собак» или «запись»). Без пробелов и подчёркиваний.\n"
    "Нельзя: превосходные степени («лучший», «№1», «самый»), «100%», «гарантия», «бесплатно» и числа, если этого нет в описании владельца.\n"
    "Ответ: только JSON-объект {\"title2\": [\"...\"], \"callouts\": [\"...\"], \"display_path\": \"...\"}."
)

_BAD = re.compile(r"[!?]|https?://|www\.|\.(?:ru|com|net|рф)\b|\+?\d[\d\s()-]{8,}")   # знаки, адреса сайтов, телефоны


def build_prompt(biz, titles: list[str]) -> str:
    parts = [f"Бизнес: {biz['name']}", f"Чем занимается: {biz['kind']}", f"Где показываем: {where_text(biz)}",
             f"Описание от владельца: {effective_description(biz) or 'нет'}"]
    try:
        if not biz["allow_promo"]:
            parts.append("Скидки, акции, подарки и бонусы не упоминай.")
    except (KeyError, IndexError):
        pass
    parts.append("Заголовки объявлений (по порядку):")
    parts += [f"{i + 1}. {t}" for i, t in enumerate(titles)]
    return "\n".join(parts)


def _clean_phrase(value, limit: int, facts: str) -> str:
    """Одна короткая фраза или пустая строка, если она не годится для Директа."""
    text = re.sub(r"\s+", " ", str(value or "")).strip().rstrip(".,;:—-").strip()
    if not text or len(text) > limit or _BAD.search(text):
        return ""
    if any(len(w) > 22 for w in text.split()):
        return ""
    letters = [c for c in text if c.isalpha()]
    if len(letters) > 3 and sum(c.isupper() for c in letters) / len(letters) > 0.6:   # ВЕРХНИЙ РЕГИСТР Директ не принимает
        return ""
    if unsupported_claims(text, facts):
        return ""
    return text


def _clean_path(value) -> str:
    text = re.sub(r"[^a-zа-яё0-9-]", "", str(value or "").lower().replace(" ", "-").replace("_", "-"))
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text[:PATH_MAX].rstrip("-")


def clean(raw: dict, biz, titles: list[str]) -> dict:
    """Проверяет ответ ИИ. title2 хранится по заголовку (нижний регистр), чтобы совпадать с объявлением при любом порядке."""
    from app.creative.texts_ai import facts_of   # здесь, чтобы не было круга
    facts = facts_of(biz)
    title2s = list(raw.get("title2") or []) if isinstance(raw, dict) else []
    by_title: dict[str, str] = {}
    for title, candidate in zip(titles, title2s):
        phrase = _clean_phrase(candidate, TITLE2_MAX, facts)
        if phrase and phrase.lower() != title.lower() and phrase.lower() not in title.lower():
            by_title[title.lower()] = phrase
    callouts: list[str] = []
    seen_words: set[str] = set()
    for candidate in (raw.get("callouts") or []) if isinstance(raw, dict) else []:
        phrase = _clean_phrase(candidate, CALLOUT_MAX, facts)
        key = phrase.lower()
        if phrase and key not in {c.lower() for c in callouts} and not (set(key.split()) <= seen_words and len(key.split()) > 1):
            callouts.append(phrase)
            seen_words |= set(key.split())
        if len(callouts) == CALLOUTS_MAX:
            break
    return {"title2": by_title, "callouts": callouts, "display_path": _clean_path(raw.get("display_path") if isinstance(raw, dict) else ""),
            "titles": [t.lower() for t in titles]}


async def generate(biz, titles: list[str], fal_key: str, model: str, transport: httpx.AsyncBaseTransport | None = None) -> dict | None:
    """Дополнения от ИИ. None, если ИИ недоступен или ответ не разобрать: тогда объявления идут без дополнений."""
    if not fal_key or not titles:
        return None
    body = {"model": model, "max_tokens": 4000, "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": build_prompt(biz, titles)}]}
    try:
        async with httpx.AsyncClient(timeout=90, transport=transport) as http:
            r = await http.post(FAL_CHAT_URL, json=body, headers={"Authorization": f"Key {fal_key}", "Content-Type": "application/json"})
        if r.status_code != 200:
            return None
        raw = r.json()["choices"][0]["message"]["content"] or ""
        data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return None
    return clean(data, biz, titles)


def load(biz) -> dict | None:
    try:
        value = json.loads(biz["ad_extras"] or "")
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def save(conn: sqlite3.Connection, business_id: int, extras: dict) -> None:
    conn.execute("UPDATE businesses SET ad_extras = ? WHERE id = ?", (json.dumps(extras, ensure_ascii=False), business_id))
    conn.commit()


def covers(extras: dict | None, titles: list[str]) -> bool:
    """Хранятся ли дополнения для всех этих заголовков (иначе нужно составить заново)."""
    return bool(extras) and all(t.lower() in (extras.get("titles") or []) for t in titles)


def title2_for(extras: dict | None, title: str) -> str:
    return ((extras or {}).get("title2") or {}).get(title.lower(), "")


def fit_path(extras: dict | None) -> str:
    return fit((extras or {}).get("display_path") or "", PATH_MAX)
