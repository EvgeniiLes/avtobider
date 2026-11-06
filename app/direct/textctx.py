"""Данные рекламы для авторов текстов: по каким запросам приходят клиенты и какие заголовки и углы сработали, а какие нет.

Это замыкает цикл: тексты пишутся не «вообще», а под реальные запросы группы и с учётом того, что уже показала статистика.
"""
import sqlite3

from app.direct import audience as aud
from app.engine.util import MICRO, day_add

WINDOW_DAYS = 30
MIN_CLICKS = 20      # меньше кликов на объявлении: судить о тексте рано
MAX_QUERIES = 8
SHOWN = 3            # сколько лучших и сколько слабых объявлений называем


def _perf(conn: sqlite3.Connection, biz, today: str) -> list[dict]:
    start = day_add(today, -WINDOW_DAYS)
    rows = conn.execute(
        "SELECT u.title, u.angle, SUM(s.impressions) AS imp, SUM(s.clicks) AS clicks, SUM(s.spend_micro) AS spend, SUM(s.leads) AS leads "
        "FROM ad_units u JOIN stats_daily s ON s.unit_id = u.id WHERE u.business_id = ? AND s.day >= ? GROUP BY u.id", (biz["id"], start)).fetchall()
    out = []
    for r in rows:
        if (r["clicks"] or 0) < MIN_CLICKS:
            continue
        out.append({"title": r["title"], "angle": r["angle"] or "", "clicks": r["clicks"], "leads": r["leads"] or 0,
                    "ctr": round(r["clicks"] / r["imp"] * 100, 1) if r["imp"] else 0.0, "spend": round((r["spend"] or 0) / MICRO)})
    return out


def texts_context(conn: sqlite3.Connection, biz, today: str) -> str:
    """Текст для промпта: группы запросов, запросы с обращениями, лучшие и слабые заголовки. Пустая строка, если данных нет."""
    lines = []
    profile = aud.load(biz)
    for g in aud.intents_for(profile)[:4]:
        if g["keywords"]:
            lines.append(f"Группа запросов «{g['name'] or 'основная'}»: " + "; ".join(g["keywords"][:8]))
    queries = [r["query"] for r in conn.execute(
        "SELECT query FROM search_queries WHERE business_id = ? AND leads > 0 ORDER BY leads DESC, clicks DESC LIMIT ?", (biz["id"], MAX_QUERIES))]
    if queries:
        lines.append("Запросы, по которым уже приходили обращения: " + "; ".join(queries))
    perf = _perf(conn, biz, today)
    if perf:
        ranked = sorted(perf, key=lambda p: (-(p["leads"] / p["clicks"]), -p["ctr"]))
        fmt = lambda p: f"«{p['title']}» (угол {p['angle'] or '—'}): CTR {p['ctr']}%, кликов {p['clicks']}, обращений {p['leads']}"
        k = min(SHOWN, max(1, len(ranked) // 2))   # при двух-трёх объявлениях лучшее и слабое не пересекаются
        lines.append("Лучшие объявления: " + "; ".join(fmt(p) for p in ranked[:k]))
        weak = [p for p in ranked[::-1][:k] if p not in ranked[:k]]
        if weak:
            lines.append("Слабые объявления (не повторяй их подход): " + "; ".join(fmt(p) for p in weak))
    return "\n".join(lines)
