"""Фоновый процесс: каждые несколько минут обновляет данные из Директа и принимает решения по рекламе.

Запуск: python -m app.worker  (на сервере это служба autobider-worker).
"""
import logging
import os
import signal
import time

from app.config import get_settings, load_env
from app.db import connect, init_db
from app.direct.runner import sync_and_run

log = logging.getLogger("autobider.worker")
DEFAULT_INTERVAL_MINUTES = 30


def real_businesses(conn):
    return conn.execute(
        "SELECT DISTINCT b.* FROM businesses b JOIN campaigns c ON c.business_id = b.id "
        "WHERE c.source = 'direct' ORDER BY b.id"
    ).fetchall()


def run_once(transport=None) -> int:
    """Один проход по всем бизнесам с настоящей рекламой. Ошибка одного не мешает остальным."""
    conn = connect(get_settings().db_path)
    done = 0
    try:
        for biz in real_businesses(conn):
            try:
                result = sync_and_run(conn, biz, transport=transport)
                log.info("бизнес %s: строк статистики %s, записей в истории %s%s", biz["id"], result["synced"]["rows"],
                         result["changes"], f", ошибка: {result['error']}" if result["error"] else "")
                done += 1
            except Exception:  # служба не должна падать из-за одного бизнеса
                log.exception("бизнес %s: неожиданная ошибка", biz["id"])
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
        run_once()
        for _ in range(interval):  # спим по секунде, чтобы быстро остановиться по сигналу
            if stop["now"]:
                break
            time.sleep(1)
    log.info("Фоновый процесс остановлен.")


if __name__ == "__main__":
    main()
