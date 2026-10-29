"""Тексты объявлений через Claude. Проверка длин и «безопасности» текста делается кодом, а не на слово модели."""
import json
import re

import httpx

from app.creative.angles import ANGLES, business_block, prompt_block
from app.direct.texts import TEXT_MAX, TITLE_MAX, check, fit, unsupported_claims

API_URL = "https://api.anthropic.com/v1/messages"

SYSTEM = (
    "Ты пишешь тексты для объявлений Яндекс Директа на русском для малого бизнеса. "
    f"Заголовок не длиннее {TITLE_MAX} символов, текст не длиннее {TEXT_MAX} символов. "
    "Используй только факты из описания бизнеса. Скидки, акции, цены, сроки, гарантии, опыт и рейтинг можно упоминать, "
    "только если они прямо названы в описании: дословно и с теми же цифрами. Ничего не выдумывай и не добавляй числа, которых там нет. "
    "Запрещены слова «лучший», «самый», «№1», «100%». Без эмодзи и восклицательных знаков. "
    "Каждый вариант пиши под свой угол подачи из списка в запросе, углы не повторяй. Заголовок должен цеплять сразу: "
    "конкретный результат, цена, удобство или вопрос к клиенту, а не общие слова. "
    "Ответ: только JSON-массив объектов {\"title\": \"...\", \"text\": \"...\", \"angle\": \"id угла из списка\"}, без пояснений."
)


def facts_of(biz) -> str:
    """Всё, что владелец сам рассказал о бизнесе: описание и адрес. Только это можно упоминать в рекламе."""
    try:
        address = (biz["address"] or "").strip()
    except (KeyError, IndexError):
        address = ""
    return f"{biz['description'] or ''} {address}".strip()


def build_prompt(biz, count: int, hint: str = "") -> str:
    parts = [
        f"Бизнес: {biz['name']}", f"Чем занимается: {biz['kind']}", f"Город: {biz['city'] or 'не указан'}",
        f"Описание от владельца: {biz['description'] or 'нет'}",
    ]
    try:
        if (biz["address"] or "").strip():
            parts.append(f"Адрес (можно упоминать улицу и дом, как написал владелец): {biz['address'].strip()}")
    except (KeyError, IndexError):
        pass
    tones = {"gentle": "нежный", "calm": "спокойный", "energetic": "энергичный", "strict": "строгий", "friendly": "дружелюбный"}
    if biz["tone"]:
        parts.append(f"Желаемый тон: {tones.get(biz['tone'], biz['tone'])}")
    if hint.strip():
        parts.append(f"Пожелание владельца: {hint.strip()[:300]}")
    parts.append(business_block(biz))
    parts.append(prompt_block(facts_of(biz)))
    parts.append(f"Нужно вариантов: {count}.")
    return "\n".join(parts)


def parse_variants(raw: str, facts: str = "") -> list[dict]:
    """Достаёт JSON-массив из ответа модели и оставляет только объявления, прошедшие проверку.
    facts: описание владельца, по нему решаем, какие обещания и числа допустимы."""
    match = re.search(r"\[.*\]", raw, re.S)
    if not match:
        return []
    try:
        items = json.loads(match.group(0))
    except ValueError:
        return []
    good, seen, seen_angles = [], set(), set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        title, text = str(item.get("title", "")).strip(), str(item.get("text", "")).strip()  # слишком длинное не режем, а отбраковываем
        angle = str(item.get("angle", "")).strip()
        angle = angle if angle in ANGLES else ""
        if check(title, text) or unsupported_claims(title + " " + text, facts) or "!" in title + text or title.lower() in seen:
            continue
        if angle and angle in seen_angles:  # для сравнения нужны разные углы: второй текст с тем же углом пропускаем
            continue
        seen.add(title.lower())
        seen_angles.add(angle)
        good.append({"title": title, "text": text, "angle": angle})
    return good


FAL_CHAT_URL = "https://fal.run/openrouter/router/openai/v1/chat/completions"


async def _ask_anthropic(http: httpx.AsyncClient, prompt: str, api_key: str, model: str) -> str:
    # Запас большой: модель может «подумать» перед ответом, и мысли тоже считаются в лимит.
    body = {"model": model, "max_tokens": 8000, "system": SYSTEM, "messages": [{"role": "user", "content": prompt}]}
    r = await http.post(API_URL, json=body, headers={
        "x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    if r.status_code != 200:
        return ""
    return "".join(block.get("text", "") for block in r.json().get("content", []))


async def _ask_fal(http: httpx.AsyncClient, prompt: str, fal_key: str, model: str) -> str:
    """Тот же Claude, но через fal.ai: Anthropic не принимает запросы с российских серверов, fal принимает."""
    body = {"model": model, "max_tokens": 8000,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]}
    r = await http.post(FAL_CHAT_URL, json=body, headers={"Authorization": f"Key {fal_key}", "Content-Type": "application/json"})
    if r.status_code != 200:
        return ""
    return r.json()["choices"][0]["message"]["content"] or ""


PHOTO_CHECK_SYSTEM = (
    "Ты помогаешь малому бизнесу выбирать фотографии для рекламы в Яндекс Директе. Оцени фото и верни только JSON-объект "
    "{\"type\": \"interior|hands|people|product|other\", \"score\": число от 1 до 5, \"issues\": [\"...\"], \"summary\": \"одно предложение\"}. "
    "type: interior (помещение, интерьер, витрина), hands (руки или результат работы крупным планом), people (мастер, команда, клиент), "
    "product (товар, предметы), other. score: насколько фото подходит для рекламы (резкость, свет, композиция, свободное место для текста). "
    "В issues перечисли реальные проблемы по-русски коротко: чужие читаемые вывески, бренды или логотипы, водяные знаки, посторонний текст, "
    "узнаваемые лица людей (для рекламы нужно их согласие), беспорядок, тёмное или смазанное фото. Если проблем нет, issues пустой. "
    "Важно: если на фото читается чужой бренд, вывеска или логотип, либо есть водяной знак, score не выше 2. "
    "Если на картинке много собственного текста, заголовков или графики (готовый рекламный баннер, афиша, скриншот), score не выше 2, "
    "type other, а в issues напиши «на картинке свой текст»: поверх такого кадра нельзя ставить новый заголовок."
)


LOGO_CHECK_SYSTEM = (
    "Ты помогаешь малому бизнесу решить, годится ли логотип для рекламного баннера. Верни только JSON-объект "
    "{\"type\": \"other\", \"score\": число от 1 до 5, \"issues\": [\"...\"], \"summary\": \"одно предложение\"}. "
    "score: насколько логотип чёткий, читаемый в маленьком размере и контрастный на светлой подложке. "
    "В issues перечисли реальные проблемы по-русски коротко: мелкий нечитаемый текст, слишком сложный рисунок, низкое качество, "
    "бледные цвета, лишние надписи (адреса, телефоны), логотип явно другой компании. Если проблем нет, issues пустой."
)


async def check_photo(image_data_url: str, fal_key: str, model: str, transport: httpx.AsyncBaseTransport | None = None,
                      logo: bool = False, synthetic: bool = False) -> dict | None:
    """Оценка фото (или логотипа) моделью со зрением: тип кадра, пригодность для рекламы, проблемы. None, если оценить не удалось."""
    body = {"model": model, "max_tokens": 600, "image_urls": [image_data_url],
            "messages": [{"role": "system", "content": LOGO_CHECK_SYSTEM if logo else PHOTO_CHECK_SYSTEM},
                         {"role": "user", "content": "Оцени этот логотип." if logo else "Оцени это фото." + (
                             " Это картинка, нарисованная нейросетью: людей на ней не существует, согласие на показ лиц не нужно." if synthetic else "")}]}
    try:
        async with httpx.AsyncClient(timeout=90, transport=transport) as http:
            r = await http.post(FAL_CHAT_URL, json=body, headers={"Authorization": f"Key {fal_key}", "Content-Type": "application/json"})
        if r.status_code != 200:
            return None
        raw = r.json()["choices"][0]["message"]["content"] or ""
        data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return None
    kind = {"hands": "work", "people": "team"}.get(str(data.get("type")), str(data.get("type")))
    if kind not in ("interior", "work", "team", "product", "other"):
        kind = "other"
    try:
        score = max(1, min(5, int(data.get("score"))))
    except (TypeError, ValueError):
        score = 3
    issues = [str(i)[:120] for i in (data.get("issues") or []) if i][:4]
    note = (str(data.get("summary") or "")[:200] + (" Проблемы: " + "; ".join(issues) + "." if issues else "")).strip()
    return {"type": kind, "score": score, "note": note}


ANALYSIS_SYSTEM = (
    "Ты аналитик рекламы малого бизнеса в Яндекс Директе. Тебе дают цифры по объявлениям (стиль баннера, тип фото, показы, клики, "
    "обращения, расход). Напиши разбор по-русски простыми словами, без жаргона, не больше 8 предложений: что видно, какие объявления "
    "и стили выглядят сильнее, что рекомендуешь сделать (оставить, остановить, усилить, что проверить дальше). "
    "Опирайся только на данные из запроса. Не выдумывай цифры. Если данных мало (мало показов или обращений), прямо скажи, "
    "что вывод предварительный, и не называй победителя. Не давай обещаний результата."
)


async def analyze_results(data_text: str, fal_key: str, model: str, transport: httpx.AsyncBaseTransport | None = None) -> str:
    body = {"model": model, "max_tokens": 1200,
            "messages": [{"role": "system", "content": ANALYSIS_SYSTEM}, {"role": "user", "content": data_text}]}
    try:
        async with httpx.AsyncClient(timeout=90, transport=transport) as http:
            r = await http.post(FAL_CHAT_URL, json=body, headers={"Authorization": f"Key {fal_key}", "Content-Type": "application/json"})
        if r.status_code != 200:
            return ""
        return (r.json()["choices"][0]["message"]["content"] or "").strip()
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return ""


async def read_image_text(image_url: str, fal_key: str, model: str,
                          transport: httpx.AsyncBaseTransport | None = None) -> list[str] | None:
    """Читает весь текст с готового баннера (модель со зрением через fal.ai). None, если прочитать не удалось."""
    body = {
        "model": model, "max_tokens": 1000, "image_urls": [image_url],
        "messages": [{"role": "user", "content": (
            "Прочитай весь текст на картинке и верни только JSON-массив строк, ровно как написано, "
            "без исправлений ошибок и без пояснений. Если текста нет, верни [].")}],
    }
    try:
        async with httpx.AsyncClient(timeout=90, transport=transport) as http:
            r = await http.post(FAL_CHAT_URL, json=body, headers={"Authorization": f"Key {fal_key}", "Content-Type": "application/json"})
        if r.status_code != 200:
            return None
        raw = r.json()["choices"][0]["message"]["content"] or ""
        start, end = raw.find("["), raw.rfind("]")
        items = json.loads(raw[start:end + 1]) if start != -1 and end > start else None
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return None
    return [str(x) for x in items] if isinstance(items, list) else None


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-zа-я]", "", (text or "").lower().replace("ё", "е"))


URL_LIKE = re.compile(r"(https?://|www\.|\b[\w-]+\.(?:ru|com|net|org|io|рф)\b)", re.I)


def invented_links(read: list[str]) -> list[str]:
    """Адреса сайтов, которые нейросеть сама написала на баннере (их в задании не было: домен мог оказаться выдуманным)."""
    return [line for line in read if URL_LIKE.search(line)]


def missing_phrases(expected: list[str], read: list[str]) -> list[str]:
    """Какие нужные надписи не найдены на картинке (регистр, кавычки и пробелы не важны)."""
    haystack = _norm(" ".join(read))
    return [e for e in expected if _norm(e) and _norm(e) not in haystack]


async def generate_texts(biz, count: int, api_key: str, model: str, hint: str = "",
                         transport: httpx.AsyncBaseTransport | None = None,
                         fal_key: str = "", fal_model: str = "") -> list[dict]:
    """Просит у Claude варианты: сначала напрямую, при отказе (или без ключа Anthropic) через fal.ai.
    Возвращает только годные; пустой список, если сервис недоступен или все отбракованы."""
    prompt = build_prompt(biz, count, hint)
    raw = ""
    async with httpx.AsyncClient(timeout=90, transport=transport) as http:
        for ask, key, name in ((_ask_anthropic, api_key, model), (_ask_fal, fal_key, fal_model)):
            if not key:
                continue
            try:
                raw = await ask(http, prompt, key, name)
            except (httpx.HTTPError, ValueError, KeyError, IndexError):
                raw = ""
            if raw:
                break
    return parse_variants(raw, facts_of(biz)) if raw else []
