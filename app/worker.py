"""Фоновый процесс: каждые несколько минут обновляет данные из Директа и принимает решения по рекламе.

Запуск: python -m app.worker  (на сервере это служба autobider-worker).
"""
import logging
import os
import signal
import time

from app.config import get_settings, load_env
from app.db import connect, init_db
from app.approvals import dispatch, ensure_webhook, run_daily as review_daily
from app.direct.runner import sync_and_run
from app.notifier import make_sender, run as notify

log = logging.getLogger("autobider.worker")
DEFAULT_INTERVAL_MINUTES = 30


def real_businesses(conn):
    return conn.execute(
        "SELECT DISTINCT b.* FROM businesses b JOIN campaigns c ON c.business_id = b.id "
        "WHERE c.source = 'direct' ORDER BY b.id"
    ).fetchall()


def run_once(transport=None, telegram_transport=None) -> int:
    """Один проход по всем бизнесам с настоящей рекламой. Ошибка одного не мешает остальным."""
    conn = connect(get_settings().db_path)
    done = 0
    send = make_sender(telegram_transport)
    try:
        for biz in real_businesses(conn):
            try:
                result = sync_and_run(conn, biz, transport=transport)
                log.info("бизнес %s: строк статистики %s, записей в истории %s%s", biz["id"], result["synced"]["rows"],
                         result["changes"], f", ошибка: {result['error']}" if result["error"] else "")
                done += 1
            except Exception:  # служба не должна падать из-за одного бизнеса
                log.exception("бизнес %s: неожиданная ошибка", biz["id"])
            try:   # отчёт и тревоги владельцу: после обновления данных, чтобы они были свежими; сбой отправки не мешает остальному
                fresh = conn.execute("SELECT * FROM businesses WHERE id = ?", (biz["id"],)).fetchone()
                notify(conn, fresh, send)
            except Exception:
                log.exception("бизнес %s: не удалось разослать уведомления", biz["id"])
            try:   # ежедневный разбор рекламы: предложения владельцу в Telegram с кнопками «Да / Нет»
                review_daily(conn, fresh, transport=telegram_transport)
            except Exception:
                log.exception("бизнес %s: не удалось сделать ежедневный разбор", biz["id"])
        try:   # неотправленные предложения (Telegram мог быть недоступен) уходят при каждом проходе
            dispatch(conn, telegram_transport)
        except Exception:
            log.exception("не удалось отправить предложения в Telegram")
    finally:
        conn.close()
    return done


def main() -> None:
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    init_db(get_settings().db_path)
    interval = int(os.environ.get("WORKER_INTERVAL_MINUTES") or DEFAULT_INTERVAL_MINUTES) * 60
    stop = {"now": False}
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.update(now=True))
    log.info("Фоновый процесс запущен, интервал %s мин.", interval // 60)
    while not stop["now"]:
        ensure_webhook()   # Telegram присылает нажатия кнопок на наш адрес; проверка дешёвая и чинит адрес, если он слетел
        run_once()
        for _ in range(interval):  # спим по секунде, чтобы быстро остановиться по сигналу
            if stop["now"]:
                break
            time.sleep(1)
    log.info("Фоновый процесс остановлен.")


if __name__ == "__main__":
    main()
