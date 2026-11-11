"""Реальные поисковые запросы, по которым показывалась реклама, и что из них следует.

Раз в день сервис забирает у Директа отчёт по запросам за 30 дней. Из него код находит запросы, которые тратят деньги и не приводят
обращений (кандидаты в минус-слова), и запросы, которые обращения приводят (кандидаты в ключевые фразы). ИИ по просьбе владельца
отмечает запросы, по которым ищут не услугу бизнеса, и называет слово для минуса. Применяет всё только владелец, одним нажатием.
"""
import json
import re
import sqlite3

import httpx

from app.direct import audience as aud
from app.direct.client import DirectClient
from app.engine.util import MICRO, day_add

WINDOW_DAYS = 30
MIN_WASTE_CLICKS = 2          # меньше кликов: случайность, судить рано
WASTE_SHARE = 0.5             # запрос потратил больше этой доли цены обращения и не принёс ни одного обращения
MIN_WASTE_MICRO = 150 * MICRO # порог, если цена обращения не задана
MAX_SHOWN = 40
MAX_FOR_AI = 50
AUTO_MINUS_LIMIT = 5          # сколько слов сервис ставит в минус сам за один раз
MAX_KEYWORDS_AFTER = 50       # сколько фраз в группе после добавлений из запросов
HISTORY = 20

QUERIES_SYSTEM = (
    "Ты специалист по Яндекс Директу. Тебе дают бизнес и список реальных поисковых запросов, по которым показывалась его реклама, "
    "с кликами, расходом и обращениями. Отметь только те запросы, по которым ищут НЕ услугу этого бизнеса: бесплатное, «сделать самому», "
    "работу и вакансии, обучение (если бизнес не обучает), товары вместо услуги, чужой город или страну, определения и скачивание. "
    "Если сомневаешься, не отмечай. Для каждого отмеченного запроса назови ОДНО слово из этого запроса, которое лучше всего отсекает такой "
    "трафик (в нормальной форме, строчными, без знаков). Не предлагай слов, которыми описываются услуги самого бизнеса. "
    "Ответ: только JSON-объект {\"irrelevant\": [{\"query\": \"запрос из списка дословно\", \"minus\": \"слово\", \"why\": \"почему, коротко\"}]}."
)


class QueriesError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _rub(micro: float) -> float:
    return round(micro / MICRO, 2)


# ---------- загрузка и чтение ----------

async def sync_queries(client: DirectClient, conn: sqlite3.Connection, biz, today: str) -> int:
    """Запросы за 30 дней, раз в день. Заменяют прежние целиком. Ошибку отчёта глотает вызывающий: основная синхронизация важнее."""
    if biz["queries_day"] == today:
        return 0
    campaigns = [str(r["external_campaign_id"]) for r in conn.execute(
        "SELECT DISTINCT external_campaign_id FROM ad_units WHERE business_id = ? AND external_campaign_id IS NOT NULL", (biz["id"],))]
    if not campaigns:
        return 0
    rows = await client.search_queries(day_add(today, -(WINDOW_DAYS - 1)), today, goal_id=biz["metrika_goal_id"] or None, campaign_ids=campaigns)
    conn.execute("DELETE FROM search_queries WHERE business_id = ?", (biz["id"],))
    conn.executemany(
        "INSERT INTO search_queries (business_id, query, impressions, clicks, spend_micro, leads) VALUES (?,?,?,?,?,?)",
        [(biz["id"], r["query"], r["impressions"], r["clicks"], r["spend_micro"], r["leads"] if biz["metrika_goal_id"] else 0) for r in rows])
    conn.execute("UPDATE businesses SET queries_day = ? WHERE id = ?", (today, biz["id"]))
    conn.commit()
    return len(rows)


def _view(row) -> dict:
    return {"query": row["query"], "impressions": row["impressions"], "clicks": row["clicks"], "spend": _rub(row["spend_micro"]), "leads": row["leads"],
            "cost_per_lead": _rub(row["spend_micro"] / row["leads"]) if row["leads"] else None}


def top_queries(conn: sqlite3.Connection, business_id: int, limit: int = MAX_SHOWN, min_clicks: int = 0) -> list[dict]:
    return [_view(r) for r in conn.execute(
        "SELECT * FROM search_queries WHERE business_id = ? AND clicks >= ? ORDER BY spend_micro DESC, clicks DESC, query LIMIT ?",
        (business_id, min_clicks, limit))]


def waste_threshold(biz) -> int:
    target = biz["target_cpa_micro"] or 0
    return int(target * WASTE_SHARE) or MIN_WASTE_MICRO


def waste(conn: sqlite3.Connection, biz) -> list[dict]:
    """Запросы, которые потратили заметную сумму и не принесли обращений. Без цели «обращение» судить нечем, список пуст."""
    if not biz["metrika_goal_id"]:
        return []
    return [_view(r) for r in conn.execute(
        "SELECT * FROM search_queries WHERE business_id = ? AND leads = 0 AND clicks >= ? AND spend_micro >= ? ORDER BY spend_micro DESC LIMIT 20",
        (biz["id"], MIN_WASTE_CLICKS, waste_threshold(biz)))]


def keyword_candidates(conn: sqlite3.Connection, biz, profile: dict | None) -> list[dict]:
    """Запросы, которые приводили обращения, а в списке фраз их ещё нет (по набору слов)."""
    have = {frozenset(k.split(" ")) for k in (profile or {}).get("keywords", [])}
    found = []
    for r in conn.execute("SELECT * FROM search_queries WHERE business_id = ? AND leads >= 1 ORDER BY leads DESC, clicks DESC LIMIT 40", (biz["id"],)):
        phrase = aud.clean_keywords([r["query"]])
        if phrase and frozenset(phrase[0].split(" ")) not in have:
            found.append({**_view(r), "phrase": phrase[0]})
    return found[:15]


# ---------- ИИ: какие запросы нецелевые ----------

def build_prompt(biz, profile: dict | None, queries: list[dict]) -> str:
    lines = [f"Бизнес: {biz['name']}", f"Чем занимается: {biz['kind']}", f"Описание: {(biz['description'] or '')[:600]}"]
    if profile:
        lines.append(f"Клиент: {profile['summary']}")
        if profile.get("keywords"):
            lines.append("Наши ключевые фразы: " + "; ".join(profile["keywords"]))
    lines.append("Запросы (запрос | клики | расход ₽ | обращения):")
    lines += [f"- {q['query']} | {q['clicks']} | {q['spend']} | {q['leads']}" for q in queries]
    return "\n".join(lines)


def parse_irrelevant(raw: str, queries: list[dict], biz, profile: dict | None) -> list[dict]:
    """Минус-слова от ИИ, сгруппированные по слову. Слово принимается, только если оно допустимо, есть в самом запросе и не режет услугу бизнеса."""
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        return []
    try:
        items = json.loads(match.group(0)).get("irrelevant")
    except (ValueError, AttributeError):
        return []
    known = {" ".join(q["query"].lower().split()): q for q in queries}
    groups: dict[str, dict] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        query = known.get(" ".join(str(item.get("query", "")).lower().split()))
        cleaned = aud.clean_negatives([item.get("minus", "")])
        if query is None or not cleaned or not aud.is_safe_negative(cleaned[0], biz, profile):
            continue
        word = cleaned[0]
        if not any(aud._same_word(word, w) for w in query["query"].lower().split()):
            continue   # слова нет в самом запросе: минус ничего бы не отсёк
        group = groups.setdefault(word, {"word": word, "queries": [], "clicks": 0, "spend": 0.0, "why": ""})
        if query["query"] not in group["queries"]:
            group["queries"].append(query["query"])
            group["clicks"] += query["clicks"]
            group["spend"] = round(group["spend"] + query["spend"], 2)
        group["why"] = group["why"] or re.sub(r"\s+", " ", str(item.get("why", ""))).strip()[:200]
    return sorted(groups.values(), key=lambda g: -g["spend"])


async def classify(queries: list[dict], biz, profile: dict | None, fal_key: str, model: str,
                   transport: httpx.AsyncBaseTransport | None = None) -> list[dict] | None:
    """None, если ИИ недоступен (отличается от «ничего нецелевого не нашёл»: тогда пустой список)."""
    body = {"model": model, "max_tokens": 2500,
            "messages": [{"role": "system", "content": QUERIES_SYSTEM}, {"role": "user", "content": build_prompt(biz, profile, queries)}]}
    try:
        async with httpx.AsyncClient(timeout=90, transport=transport) as http:
            r = await http.post(aud.FAL_CHAT_URL, json=body, headers={"Authorization": f"Key {fal_key}", "Content-Type": "application/json"})
        if r.status_code != 200:
            return None
        return parse_irrelevant(r.json()["choices"][0]["message"]["content"] or "", queries, biz, profile)
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        return None


# ---------- автоматический минус ----------

def auto_pick(groups: list[dict], conn: sqlite3.Connection, biz, profile: dict) -> list[str]:
    """Какие из слов, предложенных ИИ, сервис ставит сам. Слово берём, только если оно не встречается ни в одном запросе, который приводил обращения,
    и его ещё нет в минусах. Не больше AUTO_MINUS_LIMIT слов, сначала те, что потратили больше."""
    have = set(profile.get("negatives", []))
    good = [w for r in conn.execute("SELECT query FROM search_queries WHERE business_id = ? AND leads > 0", (biz["id"],)) for w in r["query"].lower().split()]
    words = [g["word"] for g in groups if g["word"] not in have and not any(aud._same_word(g["word"], w) for w in good)]
    return words[:AUTO_MINUS_LIMIT]


async def auto_minus(client: DirectClient, conn: sqlite3.Connection, biz, today: str, fal_key: str, model: str,
                     transport: httpx.AsyncBaseTransport | None = None) -> list[str]:
    """Раз в день: ИИ разбирает все запросы, по которым были клики и не было обращений, и слова нерелевантных запросов, которые прошли проверку, сервис
    ставит в минус сам. Хватает одного клика, цель Метрики не нужна: автотаргетинг тратит деньги и на единичных клики по чужим темам (банки, CRM, субсидии).
    Шаг пишется в историю профиля с пометкой «auto», а владельцу уходит сообщение. Нет ключа ИИ или профиля: ничего не делаем."""
    if biz["auto_minus_day"] == today or not fal_key:
        return []
    profile = aud.load(biz)
    if profile is None:
        return []
    spent = [r for r in top_queries(conn, biz["id"], MAX_FOR_AI, min_clicks=1) if not r["leads"]]
    if not spent:
        return []
    conn.execute("UPDATE businesses SET auto_minus_day = ? WHERE id = ?", (today, biz["id"]))   # день отмечаем сразу: сбой ИИ не должен повторяться каждым запуском
    conn.commit()
    groups = await classify(spent, biz, profile, fal_key, model, transport)
    words = auto_pick(groups or [], conn, biz, profile)
    if not words:
        return []
    result = await apply_changes(client, conn, biz, profile, words, [], today, auto=True)
    return result["minus"]


# ---------- применение ----------

async def apply_changes(client: DirectClient, conn: sqlite3.Connection, biz, profile: dict, minus: list[str], keywords: list[str], today: str,
                        auto: bool = False) -> dict:
    """Минус-слова и новые фразы в кабинет и в профиль. Всё, что не прошло проверку, возвращаем с причиной.
    Если Директ откажет на втором шаге, то, что уже принято на первом, остаётся в профиле."""
    units = conn.execute("SELECT DISTINCT external_campaign_id, external_group_id FROM ad_units WHERE business_id = ? AND external_campaign_id IS NOT NULL", (biz["id"],)).fetchall()
    if not units:
        raise QueriesError("Реклама для этого бизнеса ещё не создана, менять пока нечего.")
    group_id = units[0]["external_group_id"]
    campaign_ids = sorted({u["external_campaign_id"] for u in units})   # у каждой группы объявлений своя кампания: минус-слова ставим на все
    skipped = []
    have_minus = set(profile.get("negatives", []))
    new_minus = []
    for word in aud.clean_negatives(minus):
        if not aud.is_safe_negative(word, biz, profile):
            skipped.append({"item": word, "reason": "режет вашу услугу или фразы"})
        elif word not in have_minus and word not in new_minus:
            new_minus.append(word)
    for raw in minus:
        if not aud.clean_negatives([raw]):
            skipped.append({"item": raw, "reason": "Директ не принимает такое слово"})
    have_keys = set(profile.get("keywords", []))
    new_keys = [k for k in aud.clean_keywords(keywords, limit=MAX_KEYWORDS_AFTER) if k not in have_keys][:max(0, MAX_KEYWORDS_AFTER - len(have_keys))]
    for raw in keywords:
        if not aud.clean_keywords([raw]):
            skipped.append({"item": raw, "reason": "Директ не принимает такую фразу"})
    updated = dict(profile)
    done = {"minus": [], "keywords": []}
    try:
        if new_minus:
            updated["negatives"] = (list(profile.get("negatives", [])) + new_minus)[:aud.MAX_NEGATIVES]
            for campaign_id in campaign_ids:
                await client.set_negative_keywords(campaign_id, aud.effective_negatives(biz, updated))
            done["minus"] = new_minus
        if new_keys:
            result = await client.add_keywords(group_id, new_keys, None)
            for other in sorted({u["external_group_id"] for u in units if u["external_group_id"] and u["external_group_id"] != group_id}):
                await client.add_keywords(other, new_keys, None)   # объявлений больше трёх: они в нескольких группах и кампаниях, и запросы у них одни
            refused = {r["phrase"]: r["reason"] for r in result["rejected"]}
            done["keywords"] = [k for k in new_keys if k not in refused]
            skipped += [{"item": p, "reason": reason} for p, reason in refused.items()]
            updated["keywords"] = list(profile.get("keywords", [])) + done["keywords"]
    finally:
        if not done["minus"]:
            updated["negatives"] = list(profile.get("negatives", []))   # Директ слова не принял: в профиле их тоже нет
        if done["minus"] or done["keywords"]:
            updated["history"] = (list(profile.get("history", [])) + [{"day": today, "minus": done["minus"], "keywords": done["keywords"], **({"auto": True} if auto else {})}])[-HISTORY:]
            aud.save(conn, biz["id"], updated)
    return {**done, "skipped": skipped}
