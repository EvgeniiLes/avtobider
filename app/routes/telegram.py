"""Telegram владельца: привязка чата по коду, проверка, пробное сообщение, включение и выключение уведомлений."""
import asyncio
import hmac
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app import approvals, reports, telegram
from app.config import get_settings
from app.db import get_db
from app.deps import client_factory, current_user, telegram_transport

router = APIRouter(prefix="/api/telegram", tags=["telegram"])

NOT_CONFIGURED = ("Telegram на сервере пока не настроен: нужен бот. Администратору: создайте бота у @BotFather и запишите его токен "
                  "скриптом deploy\\set-telegram-keys.ps1 (подробности в docs/deployment.md).")


def _status(conn: sqlite3.Connection, user) -> dict:
    s = get_settings()
    row = conn.execute("SELECT telegram_chat, telegram_code, telegram_code_at, notify_on FROM users WHERE id = ?", (user["id"],)).fetchone()
    pending = telegram.code_valid(row["telegram_code"], row["telegram_code_at"])
    return {
        "configured": bool(s.telegram_token and s.telegram_bot), "linked": bool(row["telegram_chat"]), "notify": bool(row["notify_on"]),
        "bot": s.telegram_bot, "relay": s.telegram_api != "https://api.telegram.org",
        "code": row["telegram_code"] if pending else "",
        "link": f"https://t.me/{s.telegram_bot}?start={row['telegram_code']}" if pending and s.telegram_bot else "",
    }


def _need_bot():
    s = get_settings()
    if not (s.telegram_token and s.telegram_bot):
        raise HTTPException(503, detail={"message": NOT_CONFIGURED})
    return s


GREETING = ("Готово, Автобидер подключён. Сюда будут приходить итоги дня и недели и тревоги, если с рекламой что-то не так. "
            "Кнопка «Статистика» внизу покажет цифры на сейчас.")


def _bind(conn: sqlite3.Connection, user_id: int, chat_id: str) -> None:
    """Чат принадлежит одному аккаунту, тому, кто привязал его последним: с других аккаунтов он снимается, чтобы «Статистика» и отчёты шли только по нему."""
    conn.execute("UPDATE users SET telegram_chat = '' WHERE telegram_chat = ? AND id != ?", (chat_id, user_id))
    conn.execute("UPDATE users SET telegram_chat = ?, telegram_code = '', telegram_code_at = '', notify_on = 1 WHERE id = ?", (chat_id, user_id))
    conn.commit()


def _send_stats(conn: sqlite3.Connection, chat_id: str, transport) -> None:
    """Кнопка «Статистика»: владельцу, чей это чат, уходит статистика каждого его бизнеса с настоящей рекламой. Чужому чату не отвечаем."""
    s = get_settings()
    owners = [r["id"] for r in conn.execute("SELECT id FROM users WHERE telegram_chat = ?", (chat_id,))]   # один чат может быть привязан к нескольким аккаунтам
    if not owners:
        return
    send = lambda text: asyncio.run(telegram.send_message(s.telegram_token, s.telegram_api, chat_id, text, transport))
    today = datetime.now(approvals.MSK).date().isoformat()
    marks = ",".join("?" * len(owners))
    shown = conn.execute(f"SELECT DISTINCT b.* FROM businesses b JOIN campaigns c ON c.business_id = b.id WHERE b.user_id IN ({marks}) AND c.source = 'direct' ORDER BY b.id", owners).fetchall()
    if not shown:
        send("Реклама ещё не запущена: статистики пока нет.")
    for biz in shown:
        try:
            synced = datetime.fromisoformat(biz["last_sync_at"]).astimezone(approvals.MSK).strftime("%d.%m %H:%M") if biz["last_sync_at"] else ""
        except ValueError:
            synced = ""
        send(reports.now_text(conn, biz, today, synced))


@router.post("/hook")
def hook(update: dict, x_telegram_bot_api_secret_token: str = Header(default=""), conn: sqlite3.Connection = Depends(get_db),
         transport=Depends(telegram_transport), factory=Depends(client_factory)):
    """Сюда Telegram присылает всё, что владелец сделал в боте: нажал кнопку под предложением или открыл бота по ссылке с кодом.
    Всегда отвечаем 200: на ошибку Telegram стал бы присылать то же самое снова и снова."""
    if not hmac.compare_digest(x_telegram_bot_api_secret_token, approvals.hook_secret()):
        raise HTTPException(403)
    s = get_settings()
    try:
        query = update.get("callback_query")
        if query:
            parts = (query.get("data") or "").split(":")
            chat = ((query.get("message") or {}).get("chat") or {}).get("id")
            if len(parts) == 3 and parts[0] == "ap" and parts[1].isdigit() and parts[2] in ("y", "n") and chat is not None:
                approvals.decide(conn, int(parts[1]), parts[2] == "y", str(chat), str(query.get("id") or ""), transport, factory)
            return {"ok": True}
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        chat = message.get("chat") or {}
        if text in (telegram.STATS_BUTTON, "/stats") and chat.get("type") == "private":
            _send_stats(conn, str(chat["id"]), transport)
            return {"ok": True}
        if text.startswith("/start ") and chat.get("type") == "private":
            code = text.split(None, 1)[1].strip()
            user = conn.execute("SELECT id, telegram_code, telegram_code_at FROM users WHERE telegram_code = ?", (code,)).fetchone() if code else None
            if user and telegram.code_valid(user["telegram_code"], user["telegram_code_at"]):
                _bind(conn, user["id"], str(chat["id"]))
                asyncio.run(telegram.send_message(s.telegram_token, s.telegram_api, str(chat["id"]), GREETING, transport))
    except telegram.TelegramError:
        pass
    return {"ok": True}


@router.get("")
def get_status(user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    return _status(conn, user)


@router.post("/connect")
def connect_chat(user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Выдаёт одноразовый код и ссылку на бота. Владелец открывает ссылку и нажимает «Start»."""
    _need_bot()
    conn.execute("UPDATE users SET telegram_code = ?, telegram_code_at = ? WHERE id = ?",
                 (telegram.new_code(), datetime.now(timezone.utc).isoformat(timespec="seconds"), user["id"]))
    conn.commit()
    return _status(conn, user)


@router.post("/check")
def check_chat(user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db), transport=Depends(telegram_transport)):
    """Ищет в сообщениях боту «/start <код>». Нашёл, значит, чат привязан, и бот здоровается."""
    s = _need_bot()
    row = conn.execute("SELECT telegram_chat, telegram_code, telegram_code_at FROM users WHERE id = ?", (user["id"],)).fetchone()
    if row["telegram_chat"] and not row["telegram_code"]:   # бот уже привязал чат сам, когда владелец нажал «Start» (приём ответов из Telegram включён)
        return {**_status(conn, user), "found": True, "name": ""}
    if not telegram.code_valid(row["telegram_code"], row["telegram_code_at"]):
        raise HTTPException(400, detail={"message": "Код устарел или не создан. Нажмите «Подключить Telegram» ещё раз."})
    try:
        found = asyncio.run(telegram.find_start(s.telegram_token, s.telegram_api, row["telegram_code"], transport))
        if found:
            _bind(conn, user["id"], found["chat_id"])
            asyncio.run(telegram.send_message(
                s.telegram_token, s.telegram_api, found["chat_id"],
                "Готово, Автобидер подключён. Сюда будут приходить итоги недели по понедельникам и тревоги, если с рекламой что-то не так.", transport))
    except telegram.TelegramError as e:
        if "webhook" in e.message.lower():   # приём ответов из Telegram включён: бот сам привяжет чат, когда владелец нажмёт «Start»
            return {**_status(conn, user), "found": False, "name": ""}
        raise HTTPException(502, detail={"message": e.message}) from e
    return {**_status(conn, user), "found": bool(found), "name": found["title"] if found else ""}


@router.post("/test")
def send_test(user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db), transport=Depends(telegram_transport)):
    s = _need_bot()
    chat = conn.execute("SELECT telegram_chat FROM users WHERE id = ?", (user["id"],)).fetchone()["telegram_chat"]
    if not chat:
        raise HTTPException(400, detail={"message": "Telegram ещё не подключён."})
    try:
        asyncio.run(telegram.send_message(s.telegram_token, s.telegram_api, chat, "Проверка связи: если вы это читаете, отчёты и тревоги будут доходить.", transport))
    except telegram.TelegramError as e:
        raise HTTPException(502, detail={"message": e.message}) from e
    return {"sent": True}


class NotifyIn(BaseModel):
    notify: bool


@router.put("")
def set_notify(data: NotifyIn, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    conn.execute("UPDATE users SET notify_on = ? WHERE id = ?", (int(data.notify), user["id"]))
    conn.commit()
    return _status(conn, user)


@router.delete("")
def disconnect(user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    conn.execute("UPDATE users SET telegram_chat = '', telegram_code = '', telegram_code_at = '' WHERE id = ?", (user["id"],))
    conn.commit()
    return _status(conn, user)
