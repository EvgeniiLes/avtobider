"""Профиль целевой аудитории бизнеса и всё, что из него следует в Директе: ключевые фразы, минус-слова, категории автотаргетинга.

ИИ описывает, кто покупает (пол, возраст, платёжеспособность, с какой задачей приходит) и каким запросом это человек ищет услугу.
Из профиля код собирает настройки группы: список фраз, которые не нарушают правил Директа, минус-слова, которые не режут
собственные фразы бизнеса, и категории автотаргетинга. Профиль хранится в businesses.audience, владелец может его поправить.
"""
import json
import re
from datetime import date

import httpx

from app.creative.angles import effective_description, where_text
from app.direct.negatives import negatives_for

MAX_KEYWORDS = 20       # хватает на малый бюджет: больше фраз, меньше кликов на каждую
MAX_NEGATIVES = 60
MAX_PHRASE_WORDS = 7    # правило Директа
MAX_WORD_LEN = 35       # правило Директа

GENDERS = {"any": "любой", "female": "женщины", "male": "мужчины"}
AGES = ["18-24", "25-34", "35-44", "45-54", "55+"]
INCOMES = {"any": "любая", "above_average": "выше среднего", "high": "высокая"}
CONFIDENCE = {"low": "низкая", "medium": "средняя", "high": "высокая"}

# Категории автотаргетинга Директа. На малом бюджете оставляем только то, что близко к объявлению: «целевые» и «узкие».
CATEGORIES = {"exact": "Целевые", "narrow": "Узкие", "alternative": "Альтернативные", "accessory": "Сопутствующие", "broader": "Широкие"}
DEFAULT_CATEGORIES = {"exact": True, "narrow": True, "alternative": False, "accessory": False, "broader": False}
BRANDS = {"without": "Запросы без брендов", "own": "Запросы с вашим брендом", "competitors": "Запросы с брендами конкурентов"}
DEFAULT_BRANDS = {"without": True, "own": True, "competitors": False}

FAL_CHAT_URL = "https://fal.run/openrouter/router/openai/v1/chat/completions"

SYSTEM = (
    "Ты специалист по Яндекс Директу и аналитик целевой аудитории малого бизнеса в России. По описанию бизнеса составь профиль "
    "аудитории и набор поисковых фраз. Опирайся только на то, что бизнес сам про себя рассказал, ничего не выдумывай: "
    "не придумывай услуги, цены, акции и сроки. Если сведений мало, ставь confidence low и прямо пиши в note, чего не хватает.\n"
    "Ключевые фразы: настоящие запросы, которые человек вводит в Яндекс, когда хочет заказать или купить именно эту услугу "
    "(коммерческий интерес: заказать, цена, стоимость, записаться, под ключ, в компании и т. п.). От 2 до 4 слов, строчными буквами, "
    "без знаков препинания и без названий конкурентов. Город и слова «рядом», «в москве» не добавляй: регион показа задан отдельно. "
    "Фразы про обучение, бесплатные способы, вакансии и скачивание не нужны, если бизнес этим не занимается.\n"
    "Минус-слова: отдельные слова, по которым ищут не нашу услугу (бесплатно, самому, вакансия, скачать, определение и т. п.). "
    "Не включай слова, которые описывают услуги самого бизнеса.\n"
    "Намерения (intents): сгруппируй ключевые фразы по тому, что именно человек хочет заказать. Профессионал разводит разные услуги по разным группам, "
    "чтобы заголовок объявления содержал то, что человек искал. 2-4 группы по услугам (например «стрижка собак», «груминг кошек», «тримминг»), "
    "у каждой name (название услуги, 2-4 слова) и keywords (3-8 фраз; в каждой общее слово услуги). Самую важную и частую услугу ставь первой. "
    "Если услуга по сути одна, верни одну группу.\n"
    "Ответ: только JSON-объект без пояснений вида {\"summary\": \"кто клиент, 2-3 предложения\", \"gender\": \"any|female|male\", "
    "\"ages\": [\"25-34\"], \"income\": \"any|above_average|high\", \"need\": \"с какой задачей приходит человек\", "
    "\"pains\": [\"3-5 коротких проблем или желаний клиента\"], \"keywords\": [\"10-20 фраз\"], "
    "\"intents\": [{\"name\": \"услуга\", \"keywords\": [\"фразы этой услуги\"]}], \"negatives\": [\"15-40 слов\"], "
    "\"confidence\": \"low|medium|high\", \"note\": \"почему именно так и что неизвестно\"}. "
    f"ages только из списка {', '.join(AGES)}."
)

_WORD = re.compile(r"^[a-zа-яё0-9]+(?:-[a-zа-яё0-9]+)*$")


def build_prompt(biz) -> str:
    parts = [f"Бизнес: {biz['name']}", f"Чем занимается: {biz['kind']}", f"Где показываем рекламу: {where_text(biz)}",
             f"Описание от владельца: {effective_description(biz) or 'нет'}"]
    try:
        if biz["avg_check_micro"]:
            parts.append(f"Средний чек: {round(biz['avg_check_micro'] / 1_000_000)} ₽")
        if (biz["site_url"] or "").strip():
            parts.append(f"Сайт: {biz['site_url'].strip()}")
    except (KeyError, IndexError):
        pass
    return "\n".join(parts)


def _words(phrase: str) -> list[str] | None:
    """Слова фразы в нижнем регистре или None, если во фразе есть то, чего Директ не принимает (знаки, операторы, длинные слова)."""
    words = re.sub(r"\s+", " ", str(phrase).lower().replace(" ", " ")).strip().split(" ")
    if not words or words == [""] or len(words) > MAX_PHRASE_WORDS:
        return None
    if any(len(w) > MAX_WORD_LEN or not _WORD.match(w) for w in words):
        return None
    return words


def clean_keywords(items, limit: int = MAX_KEYWORDS) -> list[str]:
    """Фразы без повторов, только допустимые. Слишком короткие (одно слово) не берём: такая фраза слишком широкая."""
    result, seen = [], set()
    for item in items if isinstance(items, list) else []:
        words = _words(item)
        if words is None or (len(words) < 2 and len(words[0]) < 6):
            continue
        phrase = " ".join(words)
        if phrase not in seen:
            seen.add(phrase)
            result.append(phrase)
    return result[:limit]


def clean_negatives(items, limit: int = MAX_NEGATIVES) -> list[str]:
    result, seen = [], set()
    for item in items if isinstance(items, list) else []:
        words = _words(item)
        if words is None or len(words) > 3 or len(words[0]) < 3:
            continue
        phrase = " ".join(words)
        if phrase not in seen:
            seen.add(phrase)
            result.append(phrase)
    return result[:limit]


MAX_INTENTS = 4


def clean_intents(items, keywords: list[str]) -> list[dict]:
    """Группы по намерениям: название услуги и её фразы (только допустимые, как у остальных). Пустые и безымянные отбрасываем."""
    result = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        name = re.sub(r"\s+", " ", str(item.get("name", ""))).strip()[:40]
        phrases = clean_keywords(item.get("keywords"))
        if name and phrases and name.lower() not in {r["name"].lower() for r in result}:
            result.append({"name": name, "keywords": phrases})
    return result[:MAX_INTENTS]


def intents_for(profile: dict | None) -> list[dict]:
    """Намерения профиля, согласованные с текущим списком фраз (владелец мог их поправить). Фразы вне групп уходят в ближайшую по словам
    или в первую группу. Нет намерений (старый профиль) или одно: одна группа со всеми фразами, как раньше."""
    keywords = list((profile or {}).get("keywords", []))
    groups = []
    for item in (profile or {}).get("intents") or []:
        mine = [k for k in item.get("keywords", []) if k in keywords]
        if mine:
            groups.append({"name": item["name"], "keywords": mine})
    if len(groups) < 2:
        return [{"name": groups[0]["name"] if groups else "", "keywords": keywords}] if keywords else []
    placed = {k for g in groups for k in g["keywords"]}
    for k in keywords:
        if k in placed:
            continue
        stems = {w[:5] for w in k.split(" ") if len(w) >= 4}
        best = max(groups, key=lambda g: len(stems & {w[:5] for p in g["keywords"] for w in p.split(" ") if len(w) >= 4}))
        best["keywords"].append(k)
    return groups


def _same_word(a: str, b: str) -> bool:
    """Директ склеивает формы слова («курс», «курсы»), поэтому минус-слово режет и близкие формы."""
    if a == b:
        return True
    shortest = min(len(a), len(b))
    if shortest < 4 or abs(len(a) - len(b)) > 3:   # «мастер» и «мастер-класс» разные слова
        return False
    common = 0
    for x, y in zip(a, b):
        if x != y:
            break
        common += 1
    return common >= shortest - 2


def _guard(biz, profile: dict | None) -> set[str]:
    """Слова, которые нельзя ставить в минус: из собственных фраз бизнеса и из его вида деятельности."""
    guard = {w for phrase in (profile or {}).get("keywords", []) for w in phrase.split(" ")}
    return guard | set(re.findall(r"[a-zа-яё0-9]+", str(biz["kind"]).lower()))


def is_safe_negative(word: str, biz, profile: dict | None) -> bool:
    """Слово можно ставить в минус: оно допустимо по правилам Директа и не режет фразы и вид деятельности бизнеса."""
    cleaned = clean_negatives([word])
    guard = _guard(biz, profile)
    return bool(cleaned) and not any(_same_word(p, g) for p in cleaned[0].split(" ") for g in guard)


def effective_negatives(biz, profile: dict | None) -> list[str]:
    """Минус-слова кампании: общий список ниши плюс слова от ИИ. Слово, которое режет собственную фразу бизнеса или его вид деятельности, убираем."""
    guard = _guard(biz, profile)
    result, seen = [], set()
    for word in negatives_for(biz["kind"]) + list((profile or {}).get("negatives", [])):
        parts = word.split(" ")
        if word in seen or any(_same_word(p, g) for p in parts for g in guard):
            continue
        seen.add(word)
        result.append(word)
    return result[:MAX_NEGATIVES]


def autotargeting_for(profile: dict | None) -> dict:
    """Категории и бренды автотаргетинга в том виде, в котором их просит Директ. Выключить все категории нельзя: тогда берём целевые."""
    saved = (profile or {}).get("autotargeting") or {}
    categories = {**DEFAULT_CATEGORIES, **{k: bool(v) for k, v in (saved.get("categories") or {}).items() if k in CATEGORIES}}
    brands = {**DEFAULT_BRANDS, **{k: bool(v) for k, v in (saved.get("brands") or {}).items() if k in BRANDS}}
    if not any(categories.values()):
        categories["exact"] = True
    if not (brands["without"] or brands["own"]):
        brands["without"] = True
    return {"categories": categories, "brands": brands}


def _all_keywords(data: dict) -> list[str]:
    """Общий список фраз: то, что назвал ИИ, плюс фразы из намерений, которых в общем списке не оказалось."""
    extra = [k for item in (data.get("intents") if isinstance(data.get("intents"), list) else []) if isinstance(item, dict) for k in (item.get("keywords") or [])]
    base = list(data.get("keywords") if isinstance(data.get("keywords"), list) else [])
    return clean_keywords(base + extra, limit=MAX_KEYWORDS + 4)


def parse_profile(raw: str) -> dict | None:
    """Профиль из ответа модели. Всё, что не проходит проверку, отбрасывается; без описания клиента профиль не принимаем."""
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    summary = re.sub(r"\s+", " ", str(data.get("summary", ""))).strip()[:600]
    if not summary:
        return None
    ages = [a for a in (data.get("ages") if isinstance(data.get("ages"), list) else []) if a in AGES]
    pains = [re.sub(r"\s+", " ", str(p)).strip()[:160] for p in (data.get("pains") if isinstance(data.get("pains"), list) else []) if str(p).strip()][:5]
    return {
        "summary": summary,
        "gender": data.get("gender") if data.get("gender") in GENDERS else "any",
        "ages": ages,
        "income": data.get("income") if data.get("income") in INCOMES else "any",
        "need": re.sub(r"\s+", " ", str(data.get("need", ""))).strip()[:300],
        "pains": pains,
        "keywords": _all_keywords(data),
        "intents": clean_intents(data.get("intents"), []),
        "negatives": clean_negatives(data.get("negatives")),
        "confidence": data.get("confidence") if data.get("confidence") in CONFIDENCE else "low",
        "note": re.sub(r"\s+", " ", str(data.get("note", ""))).strip()[:500],
        "autotargeting": {"categories": dict(DEFAULT_CATEGORIES), "brands": dict(DEFAULT_BRANDS)},
        "edited": False,
        "made_at": date.today().isoformat(),
    }


async def generate_profile(biz, fal_key: str, model: str, transport: httpx.AsyncBaseTransport | None = None) -> dict | None:
    """Просит ИИ составить профиль. Один повтор, если ответ не разобрать. None, если сервис недоступен."""
    body = {"model": model, "max_tokens": 5000,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": build_prompt(biz)}]}
    async with httpx.AsyncClient(timeout=90, transport=transport) as http:
        for _ in range(2):
            try:
                r = await http.post(FAL_CHAT_URL, json=body, headers={"Authorization": f"Key {fal_key}", "Content-Type": "application/json"})
                if r.status_code != 200:
                    continue
                profile = parse_profile(r.json()["choices"][0]["message"]["content"] or "")
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
                continue
            if profile:
                return profile
    return None


def load(biz) -> dict | None:
    try:
        raw = biz["audience"]
    except (KeyError, IndexError):
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("summary") else None


def save(conn, business_id: int, profile: dict) -> None:
    conn.execute("UPDATE businesses SET audience = ? WHERE id = ?", (json.dumps(profile, ensure_ascii=False), business_id))
    conn.commit()


def apply_edits(profile: dict, edits: dict) -> dict:
    """Правки владельца поверх профиля. Фразы и слова проходят ту же проверку, что и от ИИ."""
    result = dict(profile)
    if "summary" in edits and str(edits["summary"]).strip():
        result["summary"] = re.sub(r"\s+", " ", str(edits["summary"])).strip()[:600]
    if edits.get("gender") in GENDERS:
        result["gender"] = edits["gender"]
    if "ages" in edits:
        result["ages"] = [a for a in edits["ages"] if a in AGES]
    if edits.get("income") in INCOMES:
        result["income"] = edits["income"]
    if "keywords" in edits:
        result["keywords"] = clean_keywords(edits["keywords"])
        result["intents"] = [g for g in ({"name": i["name"], "keywords": [k for k in i["keywords"] if k in result["keywords"]]} for i in result.get("intents") or []) if g["keywords"]]
    if "negatives" in edits:
        result["negatives"] = clean_negatives(edits["negatives"])
    if "autotargeting" in edits:
        result["autotargeting"] = autotargeting_for({"autotargeting": edits["autotargeting"]})
    result["edited"] = True
    return result


def for_prompt(profile: dict | None) -> str:
    """Короткий абзац для запроса текстов объявлений: кому пишем."""
    if not profile:
        return ""
    gender = "" if profile.get("gender", "any") == "any" else f" Пол: {GENDERS[profile['gender']]}."
    ages = f" Возраст: {', '.join(profile['ages'])}." if profile.get("ages") else ""
    pains = f" Что волнует клиента: {'; '.join(profile['pains'])}." if profile.get("pains") else ""
    need = f" С чем приходит: {profile['need']}." if profile.get("need") else ""
    return f"Кому рекламируем: {profile['summary']}{gender}{ages}{need}{pains} Пиши так, чтобы это попало именно в этого человека."


def view(biz, profile: dict | None) -> dict:
    """Профиль для экрана вместе с тем, что реально уйдёт в Директ."""
    return {
        "profile": profile,
        "labels": {"gender": GENDERS, "income": INCOMES, "confidence": CONFIDENCE, "categories": CATEGORIES, "brands": BRANDS, "ages": AGES},
        "keywords": (profile or {}).get("keywords", []),
        "negatives": effective_negatives(biz, profile),
        "autotargeting": autotargeting_for(profile),
    }
