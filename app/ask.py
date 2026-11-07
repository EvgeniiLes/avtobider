"""Вопрос владельцу в Telegram с кнопками «Да / Нет» и ожиданием ответа.

Запуск на сервере: python -m app.ask --email почта "Выкладываю X?" [--wait 600]
Код выхода: 0 нажали «Да», 1 нажали «Нет», 2 не дождались ответа или Telegram недоступен или чат не привязан.
Нужен, чтобы перед выкладкой на сайт спрашивать разрешения в Telegram (deploy\\ask-telegram.ps1).
"""
import argparse
import sys
import time
import uuid

from app import approvals
from app.config import get_settings, load_env
from app.db import connect, init_db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("text")
    parser.add_argument("--email", required=True, help="почта владельца в сервисе: ему уйдёт вопрос")
    parser.add_argument("--wait", type=int, default=600, help="сколько секунд ждать ответа")
    args = parser.parse_args(argv)
    load_env()
    init_db(get_settings().db_path)
    conn = connect(get_settings().db_path)
    try:
        user = conn.execute("SELECT id, telegram_chat FROM users WHERE lower(email) = lower(?)", (args.email,)).fetchone()
        if user is None or not user["telegram_chat"]:
            print("Нет такого владельца или у него не подключён Telegram.", file=sys.stderr)
            return 2
        approval = approvals.propose(conn, None, user["id"], "ask", uuid.uuid4().hex, args.text, {})
        if approvals.dispatch(conn) == 0:
            print("Вопрос не ушёл в Telegram: бот не настроен или недоступен.", file=sys.stderr)
            conn.execute("UPDATE approvals SET status = 'stale' WHERE id = ?", (approval,))
            conn.commit()
            return 2
        deadline = time.monotonic() + args.wait
        while time.monotonic() < deadline:
            status = conn.execute("SELECT status FROM approvals WHERE id = ?", (approval,)).fetchone()[0]
            if status != "pending":
                print({"approved": "Да.", "declined": "Нет."}.get(status, status))
                return 0 if status == "approved" else 1
            time.sleep(3)
        conn.execute("UPDATE approvals SET status = 'stale' WHERE id = ? AND status = 'pending'", (approval,))
        conn.commit()
        print("Ответа не дождались.", file=sys.stderr)
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
