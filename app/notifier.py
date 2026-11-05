"""Отправка владельцу отчёта недели и тревог в Telegram. Вызывается фоновым процессом после каждого обновления данных.

Что уже отправлено, запоминается в notifications: отчёт раз в неделю (с понедельника 9:00 по Москве), тревога про конкретное объявление
один раз, остальные тревоги не чаще, чем раз в несколько дней. Если отправить не вышло, ничего не запоминается и попытка повторится
при следующем проходе.
"""
import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from app import telegram
from app.config import get_settings
from app.direct import moderation
from app.reports import alerts, week_report

log = logging.getLogger("autobider.notify")
MSK = timezone(timedelta(hours=3))   # у России нет перехода на летнее время
REPORT_HOUR = 9                      # дневной отчёт приходит после 9:00 по Москве


def make_sender(transport=None):
    """send(chat_id, text) или None, если бот не настроен на сервере."""
    s = get_settings()
    if not (s.telegram_token and s.telegram_api):
        return None
    return lambda chat_id, text: asyncio.run(telegram.send_message(s.telegram_token, s.telegram_api, chat_id, text, transport))


def _sent(conn: sqlite3.Connection, business_id: int, kind: str, key: str | None = None, within_days: int | None = None, now: datetime | None = None) -> bool:
    if key is not None and conn.execute("SELECT 1 FROM notifications WHERE business_id = ? AND kind = ? AND key = ?", (business_id, kind, key)).fetchone():
        return True
    if within_days is not None:
        since = ((now or datetime.now(timezone.utc)) - timedelta(days=within_days)).strftime("%Y-%m-%d %H:%M:%S")
        return conn.execute("SELECT 1 FROM notifications WHERE business_id = ? AND kind = ? AND sent_at >= ?", (business_id, kind, since)).fetchone() is not None
    return False


def _record(conn: sqlite3.Connection, business_id: int, kind: str, key: str) -> None:
    conn.execute("INSERT OR IGNORE INTO notifications (business_id, kind, key) VALUES (?,?,?)", (business_id, kind, key))
    conn.commit()


def run(conn: sqlite3.Connection, biz, send, now: datetime | None = None) -> list[str]:
    """Отправляет, что пора: отчёт недели и новые тревоги. Возвращает тексты отправленного."""
    owner = conn.execute("SELECT telegram_chat, notify_on FROM users WHERE id = ?", (biz["user_id"],)).fetchone()
    if send is None or owner is None or not owner["telegram_chat"] or not owner["notify_on"]:
        return []
    now_utc = now or datetime.now(timezone.utc)
    local = now_utc.astimezone(MSK)
    today = local.date().isoformat()
    messages = []

    # дневной отчёт: каждый день после 9:00 по Москве, один раз за день (если сервис в 9:00 не работал, придёт при первом проходе после)
    if local.hour >= REPORT_HOUR and not _sent(conn, biz["id"], "day_report", today):
        messages.append(("day_report", today, week_report(conn, biz, today)["day_text"]))
    for a in alerts(conn, biz, today):
        if _sent(conn, biz["id"], a["kind"], a["key"], a["cooldown"], now_utc):
            continue
        messages.append((a["kind"], a["key"], a["text"]))
    accepted = moderation.accepted(conn, biz, today)   # объявления, которые Яндекс только что принял после проверки
    if accepted:
        messages.append(("mod_ok_batch", ",".join(str(r["id"]) for r in accepted), moderation.accepted_text(accepted)))

    sent = []
    for kind, key, text in messages:
        try:
            send(owner["telegram_chat"], text)
        except telegram.TelegramError as e:
            log.warning("бизнес %s: не удалось отправить в Telegram: %s", biz["id"], e.message)
            break   # дальше пытаться бессмысленно: повторим при следующем проходе
        _record(conn, biz["id"], kind, key)
        if kind == "mod_ok_batch":
            moderation.record_accepted(conn, biz["id"], accepted)   # каждое принятое объявление уведомляет один раз
        sent.append(text)
    return sent
