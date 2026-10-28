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


def _first_sentence(description: str, limit: int) -> str:
    sentence = re.split(r"(?<=[.!?])\s", description.strip(), maxsplit=1)[0].rstrip(".")
    return fit(sentence, limit) if sentence else ""


def draft_variants(biz, count: int = 6) -> list[dict]:
    """Варианты объявлений с разными акцентами. Без выдуманных фактов: только данные владельца."""
    name, kind, city = biz["name"].strip(), biz["kind"].strip(), biz["city"].strip()
    kind_l = kind[:1].lower() + kind[1:]
    own = _first_sentence(biz["description"], TEXT_MAX)
    options = [
        (f"{kind}, {city}" if city else kind, f"Запишитесь онлайн. {name}"),
        (f"{name}: запись онлайн", f"{kind} {'в городе ' + city if city else 'рядом с вами'}. Подберём удобное время"),
        (f"{kind} рядом с вами", f"{name}. Узнайте цены и запишитесь сегодня"),
        (f"{kind}: запись на сегодня", f"{name}{', ' + city if city else ''}. Ответим на вопросы по телефону"),
        (f"{name}: {kind_l}", own or "Приходите знакомиться, будем рады вам"),
        (f"{kind}: удобный адрес", f"{name}. Выберите время и приходите"),
    ]
    variants = []
    for title, text in options[:count]:
        variants.append({"title": fit(title, TITLE_MAX), "text": fit(text, TEXT_MAX)})
    return variants


def alternative_variant(biz, used_titles: set[str]) -> dict:
    """Второй вариант для сравнения: другой акцент, чем у уже созданных. Если все заняты, берёт первый с пометкой."""
    for v in draft_variants(biz):
        if v["title"] not in used_titles:
            return v
    base = draft_variants(biz)[0]
    return {"title": fit(base["title"], TITLE_MAX - 4) + " ✓", "text": base["text"]}
