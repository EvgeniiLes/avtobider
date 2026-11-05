"""Тексты объявлений через Claude. Проверка длин и «безопасности» текста делается кодом, а не на слово модели."""
import json
import re

import httpx

from app.creative.angles import ANGLES, business_block, districts_of, effective_description, prompt_block, where_text
from app.direct.audience import for_prompt as audience_prompt, load as load_audience
from app.direct.texts import TEXT_MAX, TITLE_MAX, check, fit, unsupported_claims

API_URL = "https://api.anthropic.com/v1/messages"

SYSTEM = (
    "Ты пишешь тексты для объявлений Яндекс Директа на русском для малого бизнеса. "
    f"Заголовок не длиннее {TITLE_MAX} символов, текст не длиннее {TEXT_MAX} символов. "
    "Используй только факты из описания бизнеса. Скидки, акции, цены, сроки, гарантии, опыт и рейтинг можно упоминать, "
    "только если они прямо названы в описании: дословно и с теми же цифрами. Ничего не выдумывай и не добавляй числа, которых там нет. "
    "Запрещены слова «лучший», «самый», «№1», «100%». Без эмодзи и восклицательных знаков. "
    "Заголовок понятен без картинки и без названия бизнеса: в нём или в тексте названо, что именно предлагают (услуга или товар), "
    "а не только «пилот», «аудит» или «разбор» без пояснения. "
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
    districts = districts_of(biz)
    return f"{effective_description(biz)} {address}{' Район: ' + ', '.join(districts) if districts else ''}".strip()   # слово «район» открывает угол «Расположение»


def build_prompt(biz, count: int, hint: str = "") -> str:
    parts = [
        f"Бизнес: {biz['name']}", f"Чем занимается: {biz['kind']}", f"Где показываем рекламу: {where_text(biz)}",
        f"Описание от владельца: {effective_description(biz) or 'нет'}",
    ]
    try:
        if not biz["allow_promo"]:
            parts.append("Не предлагай скидки, акции, подарки и бонусы, даже если они где-то упомянуты.")
    except (KeyError, IndexError):
        pass
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
    who = audience_prompt(load_audience(biz))   # профиль аудитории, если он составлен: тексты пишутся под конкретного клиента
    if who:
        parts.append(who)
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
    "{\"type\": \"interior|hands|people|product|other\", \"score\": число от 1 до 5, \"issues\": [\"...\"], \"summary\": \"одно предложение\", "
    "\"focus\": {\"x\": число от 0 до 1, \"y\": число от 0 до 1}}. "
    "focus: где центр главного объекта кадра (морда животного, лицо человека, товар, главная деталь), в долях от левого верхнего угла: "
    "x по ширине, y по высоте. По нему кадр обрезают, чтобы главное не отрезалось. Если главного объекта нет, верни 0.5 и 0.5. "
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
                      logo: bool = False, synthetic: bool = False, brand: str = "") -> dict | None:
    """Оценка фото (или логотипа) моделью со зрением: тип кадра, пригодность для рекламы, проблемы. None, если оценить не удалось.
    brand: название бизнеса владельца. Вывеска или логотип с ним на фото свои, а не чужой бренд."""
    own = (f" Бизнес владельца называется «{brand}». Вывеска, надпись или логотип с этим названием свои: это не чужой бренд и не проблема."
           if brand.strip() else "")
    body = {"model": model, "max_tokens": 1200, "image_urls": [image_data_url],
            "messages": [{"role": "system", "content": LOGO_CHECK_SYSTEM if logo else PHOTO_CHECK_SYSTEM},
                         {"role": "user", "content": ("Оцени этот логотип." if logo else "Оцени это фото." + (
                             " Это картинка, нарисованная нейросетью: людей на ней не существует, согласие на показ лиц не нужно." if synthetic else "")) + own}]}
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
    focus = data.get("focus") if isinstance(data.get("focus"), dict) else {}
    try:
        fx, fy = (max(0.05, min(0.95, float(focus["x"]))), max(0.05, min(0.95, float(focus["y"]))))
    except (KeyError, TypeError, ValueError):
        fx = fy = 0.5   # модель не назвала точку: режем по центру, как раньше
    return {"type": kind, "score": score, "note": note, "fx": fx, "fy": fy}


ANALYSIS_SYSTEM = (
    "Ты аналитик рекламы малого бизнеса в Яндекс Директе. Тебе дают цифры по объявлениям (стиль баннера, тип фото, показы, клики, "
    "обращения, расход). Напиши разбор по-русски простыми словами, без жаргона, не больше 8 предложений: что видно, какие объявления "
    "и стили выглядят сильнее, что рекомендуешь сделать (оставить, остановить, усилить, что проверить дальше). "
    "Опирайся только на данные из запроса. Не выдумывай цифры. Если данных мало (мало показов или обращений), прямо скажи, "
    "что вывод предварительный, и не называй победителя. Не давай обещаний результата."
)


DISTRICT_SYSTEM = (
    "Ты определяешь по адресу, в каком районе города находится бизнес. Верни только JSON-массив строк: названия районов (максимум два) "
    "в именительном падеже, как их называет Яндекс (например «Хамовники», «Тверской», «Центральный»), без слов «район» и «округ». "
    "Если по адресу нельзя уверенно определить район, верни пустой массив []. Не выдумывай."
)


async def derive_districts(city: str, address: str, fal_key: str, model: str, transport: httpx.AsyncBaseTransport | None = None) -> list[str]:
    """Район города по адресу бизнеса. Пустой список, если ИИ не ответил или не уверен."""
    from app.creative.angles import split_districts  # здесь, чтобы не было круга
    body = {"model": model, "max_tokens": 600,
            "messages": [{"role": "system", "content": DISTRICT_SYSTEM}, {"role": "user", "content": f"Город: {city}. Адрес: {address}"}]}
    try:
        async with httpx.AsyncClient(timeout=60, transport=transport) as http:
            r = await http.post(FAL_CHAT_URL, json=body, headers={"Authorization": f"Key {fal_key}", "Content-Type": "application/json"})
        if r.status_code != 200:
            return []
        raw = r.json()["choices"][0]["message"]["content"] or ""
        items = json.loads(raw[raw.find("["):raw.rfind("]") + 1])
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return []
    return split_districts(", ".join(str(i) for i in items if isinstance(i, str)))[:2] if isinstance(items, list) else []


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


def foreign_prices(expected: list[str], read: list[str]) -> list[str]:
    """Суммы в рублях на баннере, которых нет в нужных надписях (заголовок и цена). Такую цену нейросеть дописала сама."""
    from app.creative.compose import PRICE   # здесь, чтобы не тянуть работу с картинками при импорте
    amount = lambda m: int(re.sub(r"\D", "", m.group(1)))
    allowed = {amount(m) for e in expected for m in PRICE.finditer(e or "")}
    found: list[str] = []
    for m in PRICE.finditer(" ".join(read)):
        label = re.sub(r"\s+", " ", m.group(0)).strip()
        if amount(m) not in allowed and label not in found:
            found.append(label)
    return found


FACTS_SYSTEM = (
    "Ты проверяешь рекламный баннер на фактические ошибки. Тебе дают факты о бизнесе (их написал владелец), текст объявления "
    "и весь текст, прочитанный на баннере. Найди только прямые ошибки: цена, срок, гарантия, количество, адрес или условие на баннере, "
    "которых нет в фактах, которые им противоречат или относятся к другому предложению (например, цена аудита на объявлении про пилот), "
    "а также цена у предложения, которое по фактам бесплатное. Допустимы сокращения и округление, если смысл не искажён. "
    "Надписи на кнопках («Записаться», «Узнать подробнее») и название бизнеса не факты. Стиль, орфографию и дизайн не оценивай. "
    "Верни только JSON {\"problems\": [\"коротко по-русски, что не так\"]}, а если ошибок нет, {\"problems\": []}."
)


async def check_facts(facts: str, title: str, text: str, read: list[str], fal_key: str, model: str,
                      transport: httpx.AsyncBaseTransport | None = None) -> list[str] | None:
    """Сверяет всё, что написано на баннере, с фактами бизнеса. Пустой список: ошибок нет. None: проверить не удалось
    (тогда баннер не отбраковываем: сомнение работает в пользу владельца, а не модели)."""
    if not read:
        return []
    body = {"model": model, "max_tokens": 900, "messages": [
        {"role": "system", "content": FACTS_SYSTEM},
        {"role": "user", "content": f"Факты о бизнесе: {facts[:1800] or 'не указаны'}\nОбъявление: {title}. {text}\nТекст на баннере: {' | '.join(read)}"}]}
    try:
        async with httpx.AsyncClient(timeout=60, transport=transport) as http:
            r = await http.post(FAL_CHAT_URL, json=body, headers={"Authorization": f"Key {fal_key}", "Content-Type": "application/json"})
        if r.status_code != 200:
            return None
        raw = r.json()["choices"][0]["message"]["content"] or ""
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start:end + 1]) if start != -1 and end > start else None
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("problems"), list):
        return None
    return [str(p)[:160] for p in data["problems"] if p][:3]


def missing_phrases(expected: list[str], read: list[str]) -> list[str]:
    """Какие нужные надписи не найдены на картинке (регистр, кавычки и пробелы не важны)."""
    haystack = _norm(" ".join(read))
    return [e for e in expected if _norm(e) and _norm(e) not in haystack]


EXTRA_WORDS_OK = 2   # столько посторонних слов терпим: шумит распознавание, у цены бывает «руб.»; список услуг даёт больше


def extra_words(expected: list[str], read: list[str]) -> list[str]:
    """Слова на картинке, которых нет в нужных надписях (короткие слова и повторы не считаем). Нейросеть любит дописать мелким шрифтом
    список услуг или лозунг: в объявлении этого не было, а проверить такие факты нечем."""
    known = _norm(" ".join(expected))
    extra: list[str] = []
    for line in read:
        for word in re.findall(r"[0-9a-zа-яё]+", (line or "").lower()):
            if len(word) >= 3 and _norm(word) not in known and word not in extra:
                extra.append(word)
    return extra


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

ALT_SYSTEM = (
    "Ты копирайтер Яндекс Директа для малого бизнеса. Тебе дают бизнес, заголовок объявления и его текущий текст. Предложи 4 других текста к ЭТОМУ ЖЕ заголовку: "
    "до 81 символа, у каждого свой акцент (выгода для клиента, понятное действие, доверие и опыт, удобство и скорость). Не повторяй заголовок дословно и не повторяй "
    "тексты друг друга. Только факты из описания бизнеса; цены, скидки, сроки и числа только если они там названы. Без восклицательных знаков, "
    "без ВЕРХНЕГО РЕГИСТРА, без телефонов и сайтов. Ответ: только JSON-массив из 4 строк."
)


def clean_alt_texts(raw: str, current: str, title: str, facts: str) -> list[str]:
    """Тексты от ИИ, прошедшие правила Директа и факты владельца. Дубли, повтор заголовка и текущий текст отбрасываются."""
    from app.direct.extras import _BAD   # знаки, адреса сайтов, телефоны
    from app.direct.texts import TEXT_MAX, unsupported_claims
    match = re.search(r"\[.*\]", raw or "", re.S)
    if not match:
        return []
    try:
        items = json.loads(match.group(0))
    except ValueError:
        return []
    seen = {_norm(current), _norm(title)}
    out: list[str] = []
    for item in items if isinstance(items, list) else []:
        text = re.sub(r"\s+", " ", str(item or "")).strip()
        letters = [c for c in text if c.isalpha()]
        if not text or len(text) > TEXT_MAX or _BAD.search(text) or _norm(text) in seen or any(len(w) > 23 for w in text.split()):
            continue
        if len(letters) > 3 and sum(c.isupper() for c in letters) / len(letters) > 0.6:
            continue
        if unsupported_claims(text, facts):
            continue
        seen.add(_norm(text))
        out.append(text)
    return out[:4]


async def alt_texts(biz, title: str, current: str, fal_key: str, model: str, transport: httpx.AsyncBaseTransport | None = None) -> list[str] | None:
    """Другие тексты к тому же заголовку. None, если ИИ недоступен."""
    if not fal_key:
        return None
    user = "\n".join([f"Бизнес: {biz['name']}, {biz['kind']}", f"Описание от владельца: {effective_description(biz) or 'нет'}",
                      f"Заголовок: {title}", f"Текущий текст: {current}"])
    body = {"model": model, "max_tokens": 1200, "messages": [{"role": "system", "content": ALT_SYSTEM}, {"role": "user", "content": user}]}
    try:
        async with httpx.AsyncClient(timeout=60, transport=transport) as http:
            r = await http.post(FAL_CHAT_URL, json=body, headers={"Authorization": f"Key {fal_key}", "Content-Type": "application/json"})
        if r.status_code != 200:
            return None
        return clean_alt_texts(r.json()["choices"][0]["message"]["content"] or "", current, title, facts_of(biz))
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        return None
