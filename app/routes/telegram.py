"""Telegram владельца: привязка чата по коду, проверка, пробное сообщение, включение и выключение уведомлений."""
import asyncio
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import telegram
from app.config import get_settings
from app.db import get_db
from app.deps import current_user, telegram_transport

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
    row = conn.execute("SELECT telegram_code, telegram_code_at FROM users WHERE id = ?", (user["id"],)).fetchone()
    if not telegram.code_valid(row["telegram_code"], row["telegram_code_at"]):
        raise HTTPException(400, detail={"message": "Код устарел или не создан. Нажмите «Подключить Telegram» ещё раз."})
    try:
        found = asyncio.run(telegram.find_start(s.telegram_token, s.telegram_api, row["telegram_code"], transport))
        if found:
            conn.execute("UPDATE users SET telegram_chat = ?, telegram_code = '', telegram_code_at = '', notify_on = 1 WHERE id = ?", (found["chat_id"], user["id"]))
            conn.commit()
            asyncio.run(telegram.send_message(
                s.telegram_token, s.telegram_api, found["chat_id"],
                "Готово, Автобидер подключён. Сюда будут приходить итоги недели по понедельникам и тревоги, если с рекламой что-то не так.", transport))
    except telegram.TelegramError as e:
        raise HTTPException(502, detail={"message": e.message})
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
        raise HTTPException(502, detail={"message": e.message})
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
