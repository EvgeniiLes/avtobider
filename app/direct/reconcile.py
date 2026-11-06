"""Сверка цифр сервиса с кабинетом Директа и проверка, что расход не превысил бюджет.

Сервис хранит статистику по объявлениям (отчёт AD_PERFORMANCE_REPORT). Здесь те же дни и кампании запрашиваются отдельным отчётом по кампаниям
и сравниваются с тем, что показано владельцу: показы, клики, расход. Сегодняшний день не сверяем: цифры в Директе за него ещё меняются.
Недельный расход каждой кампании сравнивается с её недельным лимитом.
"""
import sqlite3

from app.direct.client import DirectClient
from app.engine.util import MICRO, day_add

CHECK_DAYS = 14
MONEY_TOLERANCE = MICRO          # расхождение в расходе меньше рубля считаем округлением
BUDGET_TOLERANCE = 1.05          # Директ может чуть превысить недельный лимит: сообщаем, если больше чем на 5%


def _int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


async def reconcile(client: DirectClient, conn: sqlite3.Connection, biz, today: str, days: int = CHECK_DAYS) -> dict:
    campaigns = {int(r["external_campaign_id"]): r for r in conn.execute(
        "SELECT DISTINCT external_campaign_id FROM ad_units WHERE business_id = ? AND external_campaign_id IS NOT NULL", (biz["id"],))}
    if not campaigns:
        return {"checked": False, "reason": "Реклама ещё не создана: сверять нечего."}
    start, end = day_add(today, -days), day_add(today, -1)
    rows = await client.report("CAMPAIGN_PERFORMANCE_REPORT", ["Date", "CampaignId", "Impressions", "Clicks", "Cost"], start, end, name="autobider-check")
    theirs: dict[tuple[int, str], list[int]] = {}
    for r in rows:
        key = (_int(r["CampaignId"]), r["Date"])
        if key[0] in campaigns:
            total = theirs.setdefault(key, [0, 0, 0])
            total[0] += _int(r["Impressions"])
            total[1] += _int(r["Clicks"])
            total[2] += _int(r["Cost"])
    ours: dict[tuple[int, str], list[int]] = {}
    for r in conn.execute(
            "SELECT u.external_campaign_id AS cid, s.day, SUM(s.impressions) i, SUM(s.clicks) c, SUM(s.spend_micro) m FROM stats_daily s "
            "JOIN ad_units u ON u.id = s.unit_id WHERE u.business_id = ? AND s.day BETWEEN ? AND ? GROUP BY u.external_campaign_id, s.day",
            (biz["id"], start, end)):
        ours[(int(r["cid"]), r["day"])] = [r["i"], r["c"], r["m"]]
    mismatches = []
    for key in sorted(set(theirs) | set(ours)):
        a, b = ours.get(key, [0, 0, 0]), theirs.get(key, [0, 0, 0])
        if a[0] != b[0] or a[1] != b[1] or abs(a[2] - b[2]) > MONEY_TOLERANCE:
            mismatches.append({"campaign": key[0], "day": key[1], "service": {"impressions": a[0], "clicks": a[1], "spend": round(a[2] / MICRO, 2)},
                               "direct": {"impressions": b[0], "clicks": b[1], "spend": round(b[2] / MICRO, 2)}})
    budget = []
    week_from = day_add(today, -7)
    for cid in campaigns:
        row = conn.execute("SELECT name, weekly_micro FROM campaigns WHERE business_id = ? AND external_id = ?", (biz["id"], cid)).fetchone()
        limit = row["weekly_micro"] if row else 0
        spent = sum(v[2] for (c, day), v in theirs.items() if c == cid and day >= week_from)
        budget.append({"campaign": cid, "name": row["name"] if row else str(cid), "spent": round(spent / MICRO, 2), "limit": round(limit / MICRO, 2),
                       "over": bool(limit) and spent > limit * BUDGET_TOLERANCE})
    return {"checked": True, "from": start, "to": end, "days": len(set(day for _, day in theirs) | set(day for _, day in ours)),
            "stats_ok": not mismatches, "mismatches": mismatches[:20], "budget": budget, "budget_ok": not any(b["over"] for b in budget)}


def summary(result: dict) -> str:
    """Итог сверки простыми словами."""
    if not result.get("checked"):
        return result.get("reason", "Сверка не выполнена.")
    parts = [f"Сверили {result['from']} — {result['to']} (дней с данными: {result['days']})."]
    if result["stats_ok"]:
        parts.append("Показы, клики и расход в сервисе совпадают с кабинетом Директа.")
    else:
        first = result["mismatches"][0]
        parts.append(f"Нашли расхождения: {len(result['mismatches'])} (например, {first['day']}: в сервисе расход {first['service']['spend']} ₽, "
                     f"в Директе {first['direct']['spend']} ₽). Сервис обновит данные при ближайшей синхронизации.")
    if result["budget_ok"]:
        parts.append("Недельный расход кампаний не выше их лимита.")
    else:
        over = [b for b in result["budget"] if b["over"]]
        parts.append("Расход превысил недельный лимит: " + "; ".join(f"«{b['name']}» потратила {b['spent']} ₽ при лимите {b['limit']} ₽" for b in over) + ".")
    return " ".join(parts)
