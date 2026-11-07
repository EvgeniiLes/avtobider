"""Правки, которые сервис делает только после «Да» владельца в Telegram.

Порядок такой: ежедневный разбор (app/direct/review.py) или любой другой код кладёт предложение в таблицу approvals (propose),
фоновый процесс отправляет их в Telegram с кнопками «Да, сделать» и «Нет» (dispatch), а нажатие приходит в сервис на адрес
/api/telegram/hook (Telegram сам присылает ответы, см. routes/telegram.py) и попадает в decide. «Да» применяет правку в Директе,
«Нет» запоминается, и то же предложение больше не повторяется. Нажать может только тот чат, которому предложение отправили.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from app import telegram
from app.config import get_settings
from app.direct import review
from app.direct.client import DirectClient, DirectError
from app.direct.runner import account_token
from app.engine.util import EngineError

log = logging.getLogger("autobider.approvals")
MSK = timezone(timedelta(hours=3))
REVIEW_HOUR = 10    # ежедневный разбор после 10:00 по Москве
STALE_DAYS = 3      # предложение, на которое не ответили за столько дней, закрывается: данные за это время устарели
FAILED_PAUSE_DAYS = 3


def hook_secret() -> str:
    """Секрет, который Telegram присылает в заголовке каждого запроса на наш адрес: по нему отличаем Telegram от посторонних."""
    return hmac.new(get_settings().secret_key.encode(), b"telegram-hook", hashlib.sha256).hexdigest()[:40]


def hook_url() -> str:
    parts = urlsplit(get_settings().redirect_uri)
    return f"{parts.scheme}://{parts.netloc}/api/telegram/hook" if parts.scheme and parts.netloc else ""


def ensure_webhook(transport=None) -> bool:
    """Просит Telegram присылать нажатия кнопок на наш адрес (если ещё не просили). False, если бот или адрес сайта не настроены или Telegram недоступен."""
    s = get_settings()
    url = hook_url()
    if not (s.telegram_token and url):
        return False
    try:
        if asyncio.run(telegram.webhook_url(s.telegram_token, s.telegram_api, transport)) != url:
            asyncio.run(telegram.set_webhook(s.telegram_token, s.telegram_api, url, hook_secret(), transport))
        return True
    except telegram.TelegramError as e:
        log.warning("не удалось подключить приём ответов из Telegram: %s", e.message)
        return False


def propose(conn: sqlite3.Connection, business_id: int | None, user_id: int, kind: str, key: str, text: str, payload: dict) -> int | None:
    """Кладёт предложение в очередь. None, если то же самое уже предлагали (ждёт ответа, принято, отклонено; неудавшееся повторяем не раньше чем через пару дней)."""
    same = conn.execute(
        "SELECT 1 FROM approvals WHERE business_id IS ? AND kind = ? AND key = ? AND (status != 'failed' OR created_at >= datetime('now', ?)) LIMIT 1",
        (business_id, kind, key, f"-{FAILED_PAUSE_DAYS} days")).fetchone()
    if same:
        return None
    cur = conn.execute("INSERT INTO approvals (business_id, user_id, kind, key, text, payload) VALUES (?,?,?,?,?,?)",
                       (business_id, user_id, kind, key, text, json.dumps(payload, ensure_ascii=False)))
    conn.commit()
    return cur.lastrowid


def dispatch(conn: sqlite3.Connection, transport=None) -> int:
    """Отправляет в Telegram предложения, которые ещё не отправлены. Возвращает, сколько ушло."""
    s = get_settings()
    if not s.telegram_token:
        return 0
    rows = conn.execute(
        "SELECT a.id, a.text, u.telegram_chat AS chat FROM approvals a JOIN users u ON u.id = a.user_id "
        "WHERE a.status = 'pending' AND a.message_id IS NULL AND u.telegram_chat != '' AND u.notify_on = 1 ORDER BY a.id").fetchall()
    sent = 0
    for row in rows:
        try:
            message_id = asyncio.run(telegram.send_buttons(
                s.telegram_token, s.telegram_api, row["chat"], row["text"], [("Да, сделать", f"ap:{row['id']}:y"), ("Нет", f"ap:{row['id']}:n")], transport))
        except telegram.TelegramError as e:
            log.warning("предложение %s не отправлено: %s", row["id"], e.message)
            break   # Telegram недоступен: остальные тоже не уйдут, повторим при следующем проходе
        conn.execute("UPDATE approvals SET chat_id = ?, message_id = ? WHERE id = ?", (row["chat"], message_id, row["id"]))
        conn.commit()
        sent += 1
    return sent


def _message(e: Exception) -> str:
    return e.full_message() if isinstance(e, DirectError) else getattr(e, "message", None) or str(e)


async def _apply(conn: sqlite3.Connection, row, factory) -> str:
    handler = review.APPLY.get(row["kind"])
    if handler is None:
        raise review.ReviewError("Сервис не знает, как выполнить такую правку.")
    payload = json.loads(row["payload"])
    biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (row["business_id"],)).fetchone() if row["business_id"] else None
    if biz is None:
        return await handler(None, conn, None, payload)
    if not biz["account_id"]:
        raise review.ReviewError("К бизнесу не подключён рекламный кабинет.")
    s = get_settings()
    token, login = account_token(conn, biz["account_id"])
    async with factory(token, api_version=s.direct_api_version, login=login) as client:
        return await handler(client, conn, biz, payload)


def decide(conn: sqlite3.Connection, approval_id: int, yes: bool, chat_id: str, callback_id: str = "", transport=None, factory=DirectClient) -> str:
    """Ответ владельца. Возвращает итоговый статус; чужой чат и неизвестное предложение игнорируются."""
    s = get_settings()
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    if row is None or not row["chat_id"] or row["chat_id"] != str(chat_id):
        return "unknown"
    answer = lambda text, note: _finish(s, row, text, callback_id, note, transport)
    if row["status"] != "pending":
        answer(row["text"], "Уже обработано.")
        return row["status"]
    result = ""
    if not yes:
        status, shown, note = "declined", "Отклонено, больше не предлагаю это же.", "Отклонено."
    else:
        try:
            result = asyncio.run(_apply(conn, row, factory))
            status, shown, note = "approved", ("Принято." if row["kind"] == "ask" else f"Сделано: {result}."), "Принято."
        except (DirectError, EngineError, review.ReviewError) as e:
            result = _message(e)
            status, shown, note = "failed", f"Не получилось: {result}", "Не получилось."
        except Exception:   # непредвиденное не должно оставить кнопки висеть без ответа
            log.exception("предложение %s: ошибка при выполнении", approval_id)
            result = "Непредвиденная ошибка сервиса, подробности в журнале."
            status, shown, note = "failed", f"Не получилось: {result}", "Не получилось."
    conn.execute("UPDATE approvals SET status = ?, result = ?, decided_at = ? WHERE id = ?",
                 (status, result, datetime.now(timezone.utc).isoformat(timespec="seconds"), approval_id))
    conn.commit()
    answer(f"{row['text']}\n\n{shown}", note)
    return status


def _finish(s, row, text: str, callback_id: str, note: str, transport) -> None:
    try:
        asyncio.run(telegram.finish_buttons(s.telegram_token, s.telegram_api, row["chat_id"], row["message_id"], text, callback_id, note, transport))
    except telegram.TelegramError as e:
        log.warning("не удалось обновить сообщение %s в Telegram: %s", row["message_id"], e.message)


def run_daily(conn: sqlite3.Connection, biz, now: datetime | None = None, transport=None) -> int:
    """Раз в день после 10:00 по Москве: разбор рекламы бизнеса, предложения владельцу в Telegram. Возвращает, сколько предложений ушло."""
    from app.notifier import _record, _sent   # те же «что уже отправлено» и правило «один раз»
    owner = conn.execute("SELECT telegram_chat, notify_on FROM users WHERE id = ?", (biz["user_id"],)).fetchone()
    if owner is None or not owner["telegram_chat"] or not owner["notify_on"]:
        return 0
    now_utc = now or datetime.now(timezone.utc)
    local = now_utc.astimezone(MSK)
    today = local.date().isoformat()
    if local.hour < REVIEW_HOUR or _sent(conn, biz["id"], "daily_review", today):
        return 0
    conn.execute("UPDATE approvals SET status = 'stale', decided_at = ? WHERE status = 'pending' AND created_at < datetime('now', ?)",
                 (now_utc.isoformat(timespec="seconds"), f"-{STALE_DAYS} days"))
    for item in review.proposals(conn, biz, today):
        propose(conn, biz["id"], biz["user_id"], item["kind"], item["key"], item["text"], item["payload"])
    sent = dispatch(conn, transport)
    note = review.pace_note(conn, biz, today)
    s = get_settings()
    if note and s.telegram_token and not _sent(conn, biz["id"], "pace", None, 3, now_utc):
        try:
            asyncio.run(telegram.send_message(s.telegram_token, s.telegram_api, owner["telegram_chat"], note, transport))
            _record(conn, biz["id"], "pace", today)
        except telegram.TelegramError as e:
            log.warning("бизнес %s: не удалось отправить про расход: %s", biz["id"], e.message)
    _record(conn, biz["id"], "daily_review", today)
    return sent
