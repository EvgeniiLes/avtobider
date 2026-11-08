"""Ежедневный разбор рекламы: что в ней стоит поправить.

Сам разбор ничего в кабинете не меняет. Он собирает предложения (отключить площадки-приложения, выключить показы не целевому возрасту)
и сообщает о том, что требует взгляда владельца (расход идёт быстрее недельного лимита). Предложения уходят владельцу в Telegram
с кнопками «Да / Нет» (app/approvals.py) и применяются в Директе только после «Да».
"""
import hashlib
import json
import sqlite3
from datetime import date, timedelta

from app.direct import audience as aud
from app.direct import bids
from app.direct.client import DirectClient
from app.engine.util import MICRO, plural, rub

WINDOW_DAYS = 30
MIN_SPEND = 30 * MICRO        # меньше этой суммы за срез разбирать нечего
MIN_AGE_SHARE = 0.05          # возраст предлагаем выключить, если на него ушло не меньше 5% расхода
MAX_SITES = 100               # площадок в одном предложении
MAX_EXCLUDED = 1000           # столько площадок Директ принимает в списке кампании
PACE_RATIO = 1.5              # расход в неделю при текущем темпе больше лимита в столько раз: сообщаем

# Последняя часть названия у настоящих сайтов: sibling.yandex.ru. У приложений названия вида com.vitastudio.mahjong, их конец не доменная зона.
ZONES = {"ru", "com", "net", "org", "su", "by", "kz", "ua", "io", "info", "biz", "online", "site", "рф", "uz", "az", "ge", "md", "kg", "am", "tj", "de", "uk", "eu"}


class ReviewError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def is_app(placement: str) -> bool:
    return "." in placement and placement.rsplit(".", 1)[-1].lower() not in ZONES


def _digest(items: list[str]) -> str:
    return hashlib.md5("|".join(sorted(items)).encode()).hexdigest()[:12]


def _already_excluded(conn: sqlite3.Connection, business_id: int) -> set[str]:
    done = set()
    for row in conn.execute("SELECT payload FROM approvals WHERE business_id = ? AND kind = 'sites' AND status = 'approved'", (business_id,)):
        done.update(json.loads(row["payload"]).get("sites", []))
    return done


def _apps(conn: sqlite3.Connection, biz, since: str) -> dict | None:
    skip = _already_excluded(conn, biz["id"])
    rows = conn.execute(
        "SELECT key, SUM(clicks) c, SUM(spend_micro) s, SUM(leads) l FROM dimension_stats WHERE business_id = ? AND dimension = 'placement' AND day >= ? GROUP BY key",
        (biz["id"], since)).fetchall()
    measured = bool(biz["metrika_counter_id"] and biz["metrika_goal_id"])
    if measured and not sum(r["l"] for r in rows):
        return None   # цель выбрана, но обращений нет нигде: ноль у приложений ничего не доказывает, ждём первых обращений
    found = sorted((r for r in rows if is_app(r["key"]) and r["key"] not in skip and r["l"] == 0 and r["c"] >= 2), key=lambda r: -r["s"])
    spend = sum(r["s"] for r in found)
    if spend < MIN_SPEND:
        return None
    sites = [r["key"] for r in found][:MAX_SITES]
    top = ", ".join(f"{r['key']} ({rub(r['s'])})" for r in found[:5])
    gathered = (f"{plural(len(found), 'приложение', 'приложения', 'приложений')} в рекламной сети (в основном игры) собрали "
                f"{plural(sum(r['c'] for r in found), 'клик', 'клика', 'кликов')} и {rub(spend)}")
    why = ("обращений с них ни одного, хотя с других площадок они есть" if measured
           else "обращения у этой рекламы пока не измеряются, поэтому предлагаю по общему правилу для услуг для бизнеса: клики в играх чаще случайные")
    text = f"Реклама «{biz['name']}»: за {WINDOW_DAYS} дней {gathered}; {why}. Крупнейшие: {top}.\n\nПредлагаю отключить показы на них в кампании. Отключить?"
    return {"kind": "sites", "key": _digest(sites), "text": text, "payload": {"sites": sites}}


def _ages(conn: sqlite3.Connection, biz, since: str) -> dict | None:
    profile = aud.load(biz)
    targets = {bids.AGE_CODES[a] for a in (profile or {}).get("ages", []) if a in bids.AGE_CODES}
    if not targets:
        return None
    rows = conn.execute(
        "SELECT key, SUM(clicks) c, SUM(spend_micro) s FROM dimension_stats WHERE business_id = ? AND dimension = 'age' AND day >= ? GROUP BY key",
        (biz["id"], since)).fetchall()
    total = sum(r["s"] for r in rows)
    off = bids.off_ages(profile)
    found = [r for r in rows if r["key"] in bids.ALL_AGES and r["key"] not in targets and r["key"] not in off
             and r["s"] >= max(MIN_SPEND, MIN_AGE_SHARE * total)]
    if not found:
        return None
    ages = sorted(r["key"] for r in found)
    wanted = ", ".join(a for a in profile["ages"])
    spent = "; ".join(f"{bids.AGE_LABELS[r['key']]}: {rub(r['s'])}, {plural(r['c'], 'клик', 'клика', 'кликов')}" for r in sorted(found, key=lambda r: -r["s"]))
    text = (f"Реклама «{biz['name']}»: целевой возраст в профиле {wanted}, а за {WINDOW_DAYS} дней деньги ушли и на другие группы. {spent}.\n\n"
            "Предлагаю выключить показы этим возрастам совсем (ставка 0%); у остальных групп ставки не изменятся. Выключить?")
    return {"kind": "ages", "key": _digest(ages), "text": text, "payload": {"ages": ages}}


def proposals(conn: sqlite3.Connection, biz, today: str) -> list[dict]:
    """Правки, которые стоит предложить владельцу сегодня."""
    since = (date.fromisoformat(today) - timedelta(days=WINDOW_DAYS)).isoformat()
    return [p for p in (_apps(conn, biz, since), _ages(conn, biz, since)) if p]


def pace_note(conn: sqlite3.Connection, biz, today: str) -> str | None:
    """Текст, если расход идёт заметно быстрее недельного лимита кампании. Ничего не предлагает: причину (стратегия Директа, добор остатка)
    видно только в кабинете."""
    limit = conn.execute("SELECT COALESCE(SUM(weekly_micro), 0) FROM campaigns WHERE business_id = ? AND source = 'direct' AND state != 'paused'", (biz["id"],)).fetchone()[0]
    if not limit:
        return None
    start = (date.fromisoformat(today) - timedelta(days=3)).isoformat()
    rows = conn.execute(
        "SELECT s.day, SUM(s.spend_micro) spend FROM stats_daily s JOIN ad_units u ON u.id = s.unit_id WHERE u.business_id = ? AND s.day >= ? AND s.day < ? GROUP BY s.day",
        (biz["id"], start, today)).fetchall()
    if not rows:
        return None
    per_day = sum(r["spend"] for r in rows) / len(rows)
    if per_day * 7 <= PACE_RATIO * limit:
        return None
    return (f"Реклама «{biz['name']}»: расход идёт быстрее недельного лимита. За последние {plural(len(rows), 'день', 'дня', 'дней')} в среднем {rub(per_day)} в день, "
            f"это около {rub(per_day * 7)} в неделю при лимите кампании {rub(limit)}. Проверьте стратегию и лимит в кабинете Директа.")


async def apply_sites(client: DirectClient, conn: sqlite3.Connection, biz, payload: dict) -> str:
    sites = list(payload.get("sites", []))
    campaigns = bids.campaign_ids(conn, biz["id"])
    if not sites or not campaigns:
        raise ReviewError("Нечего отключать: в Директе нет кампании этого бизнеса.")
    for campaign_id in campaigns:
        have = await client.excluded_sites(campaign_id)
        await client.set_excluded_sites(campaign_id, list(dict.fromkeys(have + sites))[:MAX_EXCLUDED])
    return f"отключено площадок: {len(sites)}"


async def apply_ages(client: DirectClient, conn: sqlite3.Connection, biz, payload: dict) -> str:
    profile = aud.load(biz)
    if profile is None:
        raise ReviewError("Профиль аудитории пропал, возраст не изменён.")
    updated = {**{k: v for k, v in profile.items() if k != "bids_off"}, "ages_off": sorted(set(profile.get("ages_off", [])) | set(payload.get("ages", [])))}
    if not bids.slices(updated):
        raise ReviewError("В профиле нет целевого возраста, менять ставки не к чему.")
    if not bids.campaign_ids(conn, biz["id"]):
        raise ReviewError("В Директе нет кампании этого бизнеса.")
    await bids.apply_bids_all(client, conn, biz, updated)
    return "показы выключены для возрастов: " + ", ".join(bids.AGE_LABELS[a] for a in payload["ages"])


async def apply_ask(client: DirectClient, conn: sqlite3.Connection, biz, payload: dict) -> str:
    return "принято"


APPLY = {"sites": apply_sites, "ages": apply_ages, "ask": apply_ask}
