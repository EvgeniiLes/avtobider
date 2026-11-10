"""Раскладка объявлений по группам-намерениям.

Профессиональная структура Директа: каждая услуга живёт в своей группе со своими фразами, а заголовок объявления содержит то, что человек искал.
У нас группа объявлений (до трёх) это отдельная кампания со своим бюджетом, поэтому одно намерение равно одной кампании.
Чтобы бюджет не умножался впустую, групп не больше, чем объявлений поровну по два: у каждой группы хотя бы два объявления для сравнения.
"""
import re

GROUP_LIMIT = 3   # больше трёх объявлений в группе Директ не принимает (ошибка 7001 «комбинаторных объявлений в группе»)
SEPARATE_LIMIT = 1   # режим «каждое объявление в своей кампании»: внутри группы Директ почти все показы отдаёт одному объявлению, второе остаётся без показов


RESPONSIVE_PARTS = 2   # комбинаторное объявление: столько заголовков и столько текстов в одном объявлении


def group_sizes(count: int, limit: int = GROUP_LIMIT) -> list[int]:
    """Как делим объявления одного намерения на группы: в каждой не больше limit и поровну (4 → 2+2, 5 → 3+2)."""
    groups = max(1, -(-count // limit))
    return [count // groups + (1 if i < count % groups else 0) for i in range(groups)]


def _stems(text: str) -> set[str]:
    """Основы слов (первые пять букв): «собак», «собаку» и «собаки» дают одну основу."""
    return {w[:5] for w in re.findall(r"[а-яёa-z0-9]{4,}", (text or "").lower())}


def score(variant: dict, intent: dict | None) -> int:
    """Сколько основ слов намерения (название и фразы) названо в объявлении. Чем больше, тем точнее объявление отвечает на то, что искал человек."""
    if not intent:
        return 0
    wanted = _stems(intent.get("name", "") + " " + " ".join(intent.get("keywords", [])))
    return len(wanted & _stems(f"{variant['title']} {variant['text']}"))


def build_groups(variants: list[dict], intents: list[dict], limit: int = GROUP_LIMIT) -> list[dict]:
    """[{intent, keywords, variants, indexes}]: объявления по группам. Одно намерение или меньше четырёх объявлений: прежняя схема, все фразы у всех групп.
    Иначе берём первые намерения (профиль ставит главные первыми) и раскладываем объявления по тому, какое из них названо в заголовке и тексте."""
    n = len(variants)
    everything = [k for i in intents for k in i["keywords"]]
    if limit == 1:   # каждое объявление в своей кампании: фразы берём того намерения, которое названо в объявлении (намерение одно или не названо: все фразы)
        out = []
        for index, variant in enumerate(variants):
            best = max(intents, key=lambda i: score(variant, i), default=None) if len(intents) > 1 else (intents[0] if intents else None)
            hit = best if best is not None and (len(intents) == 1 or score(variant, best) > 0) else None
            out.append({"intent": hit["name"] if hit else "", "keywords": list(hit["keywords"] if hit else everything), "variants": [variant], "indexes": [index]})
        return out
    if len(intents) < 2 or n < 4:
        name = intents[0]["name"] if len(intents) == 1 else ""
        out, position = [], 0
        for size in group_sizes(n, limit):
            out.append({"intent": name, "keywords": list(everything), "variants": variants[position:position + size],
                        "indexes": list(range(position, position + size))})
            position += size
        return out
    chosen = intents[:max(1, min(len(intents), n // 2))]
    groups = [{"intent": i["name"], "keywords": list(i["keywords"]), "variants": [], "indexes": []} for i in chosen]
    for index, variant in enumerate(variants):
        scores = [score(variant, i) if len(g["variants"]) < limit else -1 for g, i in zip(groups, chosen)]
        best = max(range(len(groups)), key=lambda j: (scores[j], -len(groups[j]["variants"])))
        groups[best]["variants"].append(variant)
        groups[best]["indexes"].append(index)
    for g, intent in zip(groups, chosen):   # у каждой группы хотя бы два объявления (для сравнения): добираем у самой большой то, что лучше подходит этой группе
        while len(g["variants"]) < 2:
            donor = max(groups, key=lambda x: len(x["variants"]))
            if len(donor["variants"]) <= 2 and g["variants"]:
                break
            if len(donor["variants"]) < 2:
                break
            pick = max(range(len(donor["variants"])), key=lambda k: score(donor["variants"][k], intent))
            g["variants"].append(donor["variants"].pop(pick))
            g["indexes"].append(donor["indexes"].pop(pick))
    return [g for g in groups if g["variants"]]
