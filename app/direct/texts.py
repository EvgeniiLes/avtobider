"""Тексты объявлений по описанию бизнеса и проверка длин Директа.

Пока тексты собираются из шаблонов, без придуманных скидок и обещаний. Позже их заменит
генерация через Claude, а проверка длин (fit, check) останется той же.
"""
import re

TITLE_MAX = 56  # заголовок объявления (документация Директа)
TEXT_MAX = 81   # текст объявления
WORD_MAX_TITLE = 22
WORD_MAX_TEXT = 23


def fit(text: str, limit: int) -> str:
    """Обрезает по границе слова, чтобы влезть в лимит. Хвостовые знаки препинания убирает."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:—-") or text[:limit]


def check(title: str, text: str) -> list[str]:
    """Понятные замечания по объявлению. Пустой список, если всё в порядке."""
    problems = []
    if not title.strip() or not text.strip():
        problems.append("Нужны и заголовок, и текст.")
    if len(title) > TITLE_MAX:
        problems.append(f"Заголовок длиннее {TITLE_MAX} символов.")
    if len(text) > TEXT_MAX:
        problems.append(f"Текст длиннее {TEXT_MAX} символов.")
    if any(len(w) > WORD_MAX_TITLE for w in title.split()):
        problems.append("В заголовке слишком длинное слово.")
    if any(len(w) > WORD_MAX_TEXT for w in text.split()):
        problems.append("В тексте слишком длинное слово.")
    return problems


ALWAYS_FORBIDDEN = re.compile(r"(?i)\b(лучш\w+|№\s?1|номер один|100\s?%|самы\w+ дешев\w+)")
DISCOUNT = re.compile(r"(?i)\bскидк\w+\s*(?:до\s*)?(\d+)")
NUMBER = re.compile(r"\d(?:[\d\s\u00a0]*\d)?")


# числа, написанные словами («больше пяти лет», «три мастера»): в описании так обычно и пишут
WORD_NUMBERS = {
    "2": r"\bдв(?:а|е|ух)\b", "3": r"\bтр(?:и|ёх|ех)\b", "4": r"\bчетыр\w*", "5": r"\bпят\w*", "6": r"\bшест\w*",
    "7": r"\bсем(?:ь|и)\b", "8": r"\bвосьм\w*", "9": r"\bдевят\w*", "10": r"\bдесят\w*",
}


def _numbers(text: str, words: bool = False) -> set[str]:
    found = {re.sub(r"\D", "", m.group(0)) for m in NUMBER.finditer(text or "")}
    if words:
        low = (text or "").lower()
        found |= {n for n, pattern in WORD_NUMBERS.items() if re.search(pattern, low)}
    return found


def unsupported_claims(text: str, facts: str) -> list[str]:
    """Обещания, которых нет в описании владельца. Сильные слова («лучший», «№1», «100%») запрещены всегда.
    «Бесплатно», «гарантия», «скидка N%», цены, сроки и любые числа разрешены, только если это прямо сказано в описании."""
    found = [m.group(0) for m in ALWAYS_FORBIDDEN.finditer(text)]
    low, facts_low, known = text.lower(), (facts or "").lower(), _numbers(facts or "", words=True)
    if "бесплатн" in low and "бесплатн" not in facts_low:
        found.append("бесплатно")
    if "гарант" in low and "гарант" not in facts_low:
        found.append("гарантия")
    for m in DISCOUNT.finditer(text):
        if "скидк" not in facts_low or m.group(1) not in known:
            found.append(m.group(0))
    for number in _numbers(text, words=True):
        if number and number not in known:
            found.append(f"число {number}")
    return found


def _first_sentence(description: str, limit: int) -> str:
    sentence = re.split(r"(?<=[.!?])\s", description.strip(), maxsplit=1)[0].rstrip(".")
    return fit(sentence, limit) if sentence else ""


def draft_variants(biz, count: int = 6) -> list[dict]:
    """Запасные варианты объявлений без ИИ. Без выдуманных фактов: только данные владельца. Подбираются под тип бизнеса:
    для онлайн-проекта нет «рядом с вами» и «записи», кнопка и призыв по сути услуги."""
    from app.creative.angles import profile  # импорт здесь, чтобы не было круга
    pr = profile(biz)
    name, kind, city = biz["name"].strip(), biz["kind"].strip(), biz["city"].strip()
    kind_l = kind[:1].lower() + kind[1:]
    own = _first_sentence(biz["description"], TEXT_MAX)
    act, where = pr["cta_sentence"], (f" в городе {city}" if city and pr["local"] else "")
    options = [
        ((f"{kind}, {city}" if city and pr["local"] else kind), f"{act}. {name}", "location" if pr["local"] else "booking"),
        (f"{name}: {kind_l}", own or f"{act}. {kind}{where}", "choice"),
        ((f"{kind} рядом с вами" if pr["local"] else f"{kind}: подробности на сайте"), f"{name}. {act}", "booking"),
        ((f"{kind}: запись на сегодня" if pr["appointment"] else f"{name}: {act.lower()}"), f"{name}{', ' + city if city and pr['local'] else ''}. {act}", "booking"),
        (f"{kind}: удобно и понятно", f"{name}. Выберите подходящее и {act[0].lower() + act[1:]}", "comfort"),
        (f"{name}: узнайте подробнее", f"{kind}{where}. {act}", "choice"),
    ]
    variants = []
    for title, text, angle in options[:count]:
        variants.append({"title": fit(title, TITLE_MAX), "text": fit(text, TEXT_MAX), "angle": angle})
    return variants

def alternative_variant(biz, used_titles: set[str]) -> dict:
    """Второй вариант для сравнения: другой акцент, чем у уже созданных. Если все заняты, берёт первый с пометкой."""
    for v in draft_variants(biz):
        if v["title"] not in used_titles:
            return v
    base = draft_variants(biz)[0]
    return {"title": fit(base["title"], TITLE_MAX - 4) + " ✓", "text": base["text"], "angle": base.get("angle", "")}
