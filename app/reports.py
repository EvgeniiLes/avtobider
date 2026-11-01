"""Итоги недели и тревоги: что случилось с рекламой простыми словами.

Отчёт охватывает семь полных дней до вчера и сравнивает их с предыдущей неделей. Тревоги нужны, чтобы владельцу не приходилось
заходить в сервис и проверять, не сломалось ли что-то: показов нет, объявление не прошло проверку, закончились деньги на счёте,
счётчик Метрики не считает, данные не обновляются.
"""
import sqlite3

from app.direct.counter_health import load_health
from app.engine.util import day_add, plural, rub

NO_CONVERSIONS = "Обращений не видно: подключите Метрику на странице «Бизнес», иначе сервис не отличит хорошее объявление от плохого."


def period(conn: sqlite3.Connection, business_id: int, start: str, end: str) -> dict:
    row = conn.execute(
        "SELECT COALESCE(SUM(s.impressions),0) AS impressions, COALESCE(SUM(s.clicks),0) AS clicks, COALESCE(SUM(s.spend_micro),0) AS spend, "
        "COALESCE(SUM(s.leads),0) AS leads FROM stats_daily s JOIN ad_units u ON u.id = s.unit_id WHERE u.business_id = ? AND s.day BETWEEN ? AND ?",
        (business_id, start, end)).fetchone()
    return dict(row)


def per_unit(conn: sqlite3.Connection, business_id: int, start: str, end: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT u.title AS title, u.state AS state, COALESCE(SUM(s.clicks),0) AS clicks, COALESCE(SUM(s.spend_micro),0) AS spend, "
        "COALESCE(SUM(s.leads),0) AS leads FROM ad_units u LEFT JOIN stats_daily s ON s.unit_id = u.id AND s.day BETWEEN ? AND ? "
        "WHERE u.business_id = ? GROUP BY u.id ORDER BY u.id", (start, end, business_id))]


def _change(cur: float, prev: float) -> str:
    if not prev:
        return ""
    pct = round((cur - prev) / prev * 100)
    return f", {'+' if pct >= 0 else '−'}{abs(pct)}%"


def _short(day: str) -> str:
    return f"{day[8:10]}.{day[5:7]}"


def week_report(conn: sqlite3.Connection, biz, today: str) -> dict:
    """Данные отчёта за семь полных дней до сегодняшнего (сегодня ещё не закончился)."""
    end = day_add(today, -1)
    start = day_add(end, -6)
    prev = period(conn, biz["id"], day_add(start, -7), day_add(start, -1))
    cur = period(conn, biz["id"], start, end)
    conversions = bool(biz["metrika_counter_id"] and biz["metrika_goal_id"])
    marks = conn.execute("SELECT COALESCE(SUM(count),0) AS n, COUNT(*) AS days FROM client_marks WHERE business_id = ? AND day BETWEEN ? AND ?",
                         (biz["id"], start, end)).fetchone()
    clients = marks["n"] if marks["days"] else (round(cur["leads"] * biz["client_share"]) if conversions else None)
    units = [u for u in per_unit(conn, biz["id"], start, end) if u["spend"] > 0]
    health = load_health(biz)
    counter_ok = not health or health["level"] != "bad"
    best = max((u for u in units if u["leads"] > 0), key=lambda u: (u["leads"], -u["spend"] / u["leads"]), default=None) if conversions else None
    worst = max((u for u in units if u["leads"] == 0), key=lambda u: u["spend"], default=None) if conversions and counter_ok else None
    done = [r["reason_text"] for r in conn.execute(
        "SELECT reason_text FROM changes WHERE business_id = ? AND actor = 'auto' AND status = 'applied' AND day BETWEEN ? AND ? ORDER BY id DESC LIMIT 4",
        (biz["id"], start, today))]
    waiting = conn.execute("SELECT COUNT(*) FROM changes WHERE business_id = ? AND status = 'proposed'", (biz["id"],)).fetchone()[0]
    from app.direct import queries   # здесь, чтобы не было круга
    waste = queries.waste(conn, biz) if counter_ok else []
    report = {"start": start, "end": end, "cur": cur, "prev": prev, "conversions": conversions, "clients": clients,
              "clients_estimated": not marks["days"], "best": best, "worst": worst, "done": done, "waiting": waiting,
              "counter": {"level": health["level"], "title": health["title"]} if health and health["level"] in ("bad", "warn") else None,
              "waste": {"count": len(waste), "spend": sum(w["spend"] for w in waste)} if waste else None, "name": biz["name"]}
    report["text"] = week_text(report)
    return report


def week_text(r: dict) -> str:
    cur, prev = r["cur"], r["prev"]
    lines = [f"Итоги недели «{r['name']}», {_short(r['start'])}–{_short(r['end'])}", ""]
    if not cur["spend"] and not cur["clicks"]:
        lines.append("За эти дни показов и расхода не было." + (" Неделей ранее реклама шла: стоит проверить, что случилось." if prev["spend"] else ""))
    else:
        lines.append(f"Потрачено: {rub(cur['spend'])} (неделей ранее {rub(prev['spend'])}{_change(cur['spend'], prev['spend'])})")
        lines.append(f"Клики: {cur['clicks']} (было {prev['clicks']}{_change(cur['clicks'], prev['clicks'])})")
        if r["conversions"]:
            cost = f", цена обращения {rub(cur['spend'] / cur['leads'])}" if cur["leads"] else ""
            lines.append(f"Обращения: {cur['leads']} (было {prev['leads']}{_change(cur['leads'], prev['leads'])}){cost}")
            if r["clients"] is not None:
                lines.append(f"Клиентов: {r['clients']}{' (оценка по доле обращений)' if r['clients_estimated'] else ''}")
        else:
            lines.append(NO_CONVERSIONS)
        if r["best"]:
            b = r["best"]
            lines.append(f"Лучшее объявление: «{b['title']}», {plural(b['leads'], 'обращение', 'обращения', 'обращений')} по {rub(b['spend'] / b['leads'])}.")
        if r["worst"]:
            lines.append(f"Слабое: «{r['worst']['title']}», потратило {rub(r['worst']['spend'])}, обращений нет.")
    if r["done"]:
        lines += ["", "Что сделал сервис:"] + [f"• {t}" for t in r["done"]]
    if r["waiting"]:
        lines.append(f"Ждёт вашего решения: {plural(r['waiting'], 'предложение', 'предложения', 'предложений')}.")
    if r["waste"]:
        lines.append(f"Запросов без обращений: {r['waste']['count']} на {rub(r['waste']['spend'])}. Посмотрите «Результаты», карточка «Поисковые запросы».")
    if r["counter"]:
        lines.append(f"Внимание: {r['counter']['title'].lower()}.")
    return "\n".join(lines)


def alerts(conn: sqlite3.Connection, biz, today: str) -> list[dict]:
    """Что требует внимания прямо сейчас. cooldown: через сколько дней можно напомнить; None: сообщаем один раз про это объявление."""
    found = []
    health = load_health(biz)
    if health and health["level"] == "bad":
        found.append({"kind": "counter_bad", "key": today, "cooldown": 3, "text": f"⚠ {health['title']}. {health['message']}"})
    units = conn.execute("SELECT id, title, state, status_note FROM ad_units WHERE business_id = ? AND external_ad_id IS NOT NULL", (biz["id"],)).fetchall()
    for u in units:
        if u["state"] == "rejected":
            note = f" Причина: {u['status_note']}." if u["status_note"] else ""
            found.append({"kind": "rejected", "key": str(u["id"]), "cooldown": None,
                          "text": f"⚠ Объявление «{u['title']}» не прошло проверку в Яндексе.{note} Сервис предложит новый текст."})
    if any(u["state"] == "unpaid" for u in units):
        found.append({"kind": "unpaid", "key": today, "cooldown": 2,
                      "text": "⚠ Объявления одобрены, но показов нет: пополните счёт в Яндекс Директе (минимум 300 ₽)."})
    camp = conn.execute("SELECT created_day FROM campaigns WHERE business_id = ? AND source = 'direct' ORDER BY id LIMIT 1", (biz["id"],)).fetchone()
    if camp and camp["created_day"] and camp["created_day"] <= day_add(today, -3) and any(u["state"] == "active" for u in units):
        shown = period(conn, biz["id"], day_add(today, -2), day_add(today, -1))["impressions"]
        if shown == 0:
            found.append({"kind": "no_impressions", "key": today, "cooldown": 2,
                          "text": "⚠ Объявления активны, но показов не было уже два дня. Проверьте счёт в Директе и состояние кампании."})
    if biz["last_sync_error"]:
        found.append({"kind": "sync_error", "key": today, "cooldown": 1,
                      "text": f"⚠ Сервис не может получить данные из Директа: {biz['last_sync_error']}"})
    waiting = conn.execute("SELECT COUNT(*) FROM changes WHERE business_id = ? AND status = 'proposed'", (biz["id"],)).fetchone()[0]
    if waiting:
        found.append({"kind": "waiting", "key": today, "cooldown": 1,
                      "text": f"Ждёт вашего решения: {plural(waiting, 'предложение', 'предложения', 'предложений')} по рекламе «{biz['name']}». Откройте «Результаты»."})
    return found
