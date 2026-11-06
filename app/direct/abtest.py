"""Сравнение текстов объявлений в Директе (A/B): сервис сам предлагает новый текст, спрашивает владельца и, если он не ответил, запускает сам.

Как это работает. В группе, где уже накопились клики, берём лучшее объявление и самое слабое. Вместо слабого ставим новый текст: сначала из запасных
(одобренные и черновые тексты, шаблоны), а когда их нет, ИИ пишет новый по данным о конверсиях и интересам клиента. В группе не больше трёх объявлений
и бюджет у неё один, поэтому новое объявление встаёт на место слабого, расход не растёт. Предложение уходит владельцу в Telegram; если за сутки он
не ответил, запускается само. Через неделю (движок: app/engine/cycle.py, _finish_experiments) лучший текст остаётся, проигравший останавливается.
"""
import asyncio
import json
import sqlite3
from datetime import date

from app.creative import fal
from app.creative.texts_ai import generate_texts
from app.direct.launch import MAX_VARIANTS, choose_variants, text_candidates
from app.direct.textctx import texts_context
from app.engine.cycle import approve, log_change, unit_stats
from app.engine.util import EngineError, day_add, days_between, plural

WINDOW_DAYS = 14          # за сколько дней смотрим клики и обращения объявлений
MIN_GROUP_CLICKS = 30     # меньше кликов в группе: судить, кто слабее, рано
WARMUP_DAYS = 7           # кампанию после запуска не трогаем
COOLDOWN_DAYS = 7         # объявление после правки не трогаем
AFTER_REJECT_DAYS = 7     # после отказа владельца новых предложений неделю нет
AUTO_AFTER = "-1 day"     # сколько ждём ответа владельца, потом запускаем сами


def _rate(s: dict) -> tuple[float, float]:
    """Чем больше, тем лучше: сначала доля обращений от кликов, затем доля кликов от показов."""
    return (s["leads"] / s["clicks"] if s["clicks"] else 0.0, s["clicks"] / s["impressions"] if s["impressions"] else 0.0)


def pick_pair(conn: sqlite3.Connection, biz, today: str) -> dict | None:
    """Группа, где можно провести сравнение: лучшее и самое слабое объявление. None, если нельзя (идёт другое сравнение, мало данных и т. п.)."""
    if conn.execute("SELECT 1 FROM experiments WHERE business_id = ? AND status = 'running'", (biz["id"],)).fetchone():
        return None
    if conn.execute("SELECT 1 FROM changes WHERE business_id = ? AND action = 'start_ab' AND (status = 'proposed' OR (status = 'rejected' AND day > ?))",
                    (biz["id"], day_add(today, -AFTER_REJECT_DAYS))).fetchone():
        return None
    rows = conn.execute(
        "SELECT u.*, c.created_day AS camp_day, c.intent AS camp_intent FROM ad_units u JOIN campaigns c ON c.id = u.campaign_id "
        "WHERE u.business_id = ? AND c.source = 'direct' AND u.state = 'active' AND u.ad_kind = '' AND u.external_ad_id IS NOT NULL", (biz["id"],)).fetchall()
    groups: dict = {}
    for r in rows:
        groups.setdefault(r["external_group_id"], []).append(r)
    tested = {r["unit_b"] for r in conn.execute("SELECT unit_b FROM experiments WHERE business_id = ?", (biz["id"],))}
    start = day_add(today, -WINDOW_DAYS)
    found = None
    for units in groups.values():
        if len(units) < 2 or any(u["camp_day"] and days_between(u["camp_day"], today) < WARMUP_DAYS for u in units):
            continue
        stats = {u["id"]: unit_stats(conn, u["id"], start, today) for u in units}
        clicks = sum(s["clicks"] for s in stats.values())
        if clicks < MIN_GROUP_CLICKS:
            continue
        best = max(units, key=lambda u: _rate(stats[u["id"]]))
        eligible = [u for u in units if u["id"] != best["id"] and u["variant_of"] is None and u["id"] not in tested
                    and (not u["last_changed_day"] or days_between(u["last_changed_day"], today) >= COOLDOWN_DAYS)]
        if not eligible:
            continue
        weakest = min(eligible, key=lambda u: _rate(stats[u["id"]]))
        if _rate(stats[best["id"]]) <= _rate(stats[weakest["id"]]):
            continue   # заметной разницы нет: заменять нечего
        if found is None or clicks > found["clicks"]:
            found = {"best": best, "weakest": weakest, "stats_best": stats[best["id"]], "stats_weakest": stats[weakest["id"]], "clicks": clicks}
    return found


def reserve_variants(conn: sqlite3.Connection, biz) -> list[dict]:
    """Запасные тексты: одобренные и черновые, затем шаблоны. Без тех, что уже в рекламе или что владелец уже отклонял как замену."""
    used = {r["title"].lower() for r in conn.execute("SELECT title FROM ad_units WHERE business_id = ?", (biz["id"],))}
    for r in conn.execute("SELECT evidence_json FROM changes WHERE business_id = ? AND action = 'start_ab' AND status = 'rejected'", (biz["id"],)):
        try:
            used.add(json.loads(r["evidence_json"])["variant"]["title"].lower())
        except (ValueError, KeyError, TypeError):
            pass
    variants, _ = choose_variants(biz, text_candidates(conn, biz), MAX_VARIANTS)
    return [v for v in variants if v["title"].lower() not in used]


def data_hint(conn: sqlite3.Connection, biz, pair: dict) -> str:
    """Что рассказываем ИИ, чтобы новый текст опирался на данные: что сработало, что нет и какие запросы приводили обращения."""
    best, weak = pair["best"], pair["weakest"]
    hint = [f"Лучше всего работает: «{best['title']}. {best['body']}» ({_pct(pair['stats_best'])}).",
            f"Хуже всего: «{weak['title']}. {weak['body']}» ({_pct(pair['stats_weakest'])}).",
            "Нужен новый заголовок и текст с другим подходом: не повторяй мысль и слова этих двух."]
    queries = [r["query"] for r in conn.execute(
        "SELECT query FROM search_queries WHERE business_id = ? AND leads > 0 ORDER BY leads DESC, clicks DESC LIMIT 5", (biz["id"],))]
    if queries:
        hint.append("Запросы, с которых приходили обращения: " + "; ".join(queries) + ".")
    return " ".join(hint)


def _pct(s: dict) -> str:
    if not s["clicks"]:
        return "кликов нет"
    return f"{plural(s['clicks'], 'клик', 'клика', 'кликов')}, обращений {s['leads']}"


def challenger(conn: sqlite3.Connection, biz, pair: dict, settings, transport=None) -> tuple[dict, str] | None:
    """(новый текст, откуда он): запасные сначала, затем ИИ. None, если взять нечего."""
    best_angle = pair["best"]["angle"]
    reserve = reserve_variants(conn, biz)
    if reserve:
        pick = next((v for v in reserve if v.get("angle") and v["angle"] != best_angle), reserve[0])
        return pick, "из запасных"
    if not (settings.anthropic_api_key or settings.fal_key):
        return None
    fresh = asyncio.run(generate_texts(biz, 3, settings.anthropic_api_key, settings.anthropic_model, data_hint(conn, biz, pair), transport,
                                       settings.fal_key, settings.fal_text_model, texts_context(conn, biz, date.today().isoformat())))
    conn.execute("INSERT INTO generations (business_id, kind, cents) VALUES (?, 'text', ?)", (biz["id"], fal.COST_CENTS["text"]))
    taken = {r["title"].lower() for r in conn.execute("SELECT title FROM ad_units WHERE business_id = ?", (biz["id"],))}
    fresh = [v for v in fresh if v["title"].lower() not in taken]
    if not fresh:
        return None
    return next((v for v in fresh if v.get("angle") and v["angle"] != best_angle), fresh[0]), "новый, ИИ написал по данным рекламы и интересам клиента"


def propose(conn: sqlite3.Connection, biz, pair: dict, variant: dict, source: str, today: str) -> int:
    best, weak = pair["best"], pair["weakest"]
    place = f"«{weak['camp_intent']}»" if weak["camp_intent"] else "кампании"
    text = (f"Предлагаем проверить новый текст в группе {place}: вместо самого слабого объявления «{weak['title']}» ({_pct(pair['stats_weakest'])}) "
            f"поставить «{variant['title']}» ({source}) и сравнить с лучшим «{best['title']}» ({_pct(pair['stats_best'])}) около недели. "
            "Бюджет не меняется. Останется тот, у кого лучше результат.")
    done = (f"Запустили сравнение в группе {place}: вместо «{weak['title']}» теперь «{variant['title']}» ({source}). "
            f"Сравниваем с лучшим «{best['title']}» около недели, останется сильный.")
    return log_change(conn, biz["id"], weak["id"], today, "auto", "start_ab", "ab_new_text", text,
                      {"title": weak["title"], "text": weak["body"], "angle": weak["angle"]}, {},
                      {"variant": variant, "best_unit_id": best["id"], "source": source, "done_text": done}, status="proposed")


def ab_cycle(conn: sqlite3.Connection, biz, platform, today: str, settings, transport=None) -> dict:
    """Раз в синхронизацию: запускает предложения, на которые владелец не ответил за сутки, и при необходимости делает новое.
    Работает только с платформами, которые умеют заменять текст объявления (Директ). Возвращает, что сделано."""
    done = {"started": [], "proposed": None}
    if not hasattr(platform, "rewrite_ad"):
        return done
    for ch in conn.execute("SELECT id FROM changes WHERE business_id = ? AND action = 'start_ab' AND status = 'proposed' "
                           "AND created_at <= datetime('now', ?)", (biz["id"], AUTO_AFTER)).fetchall():
        try:
            approve(conn, platform, biz, ch["id"], today)
            done["started"].append(ch["id"])
        except EngineError:
            pass   # предложение устарело или не вышло: оно закрыто, новое сделаем в следующий раз
    pair = pick_pair(conn, biz, today)
    if pair is None:
        return done
    found = challenger(conn, biz, pair, settings, transport)
    if found is None:
        return done
    done["proposed"] = propose(conn, biz, pair, found[0], found[1], today)
    conn.commit()
    return done
