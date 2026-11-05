"""Уведомления о проверке объявлений Яндексом: приняли или отклонили, и что делать.

Статус и причина берутся из того, что сервис уже получил при обновлении данных (Ads.get: Status и StatusClarification лежат в ad_units),
отдельных запросов к Директу это не делает. Отклонённое объявление уведомляет один раз (его ключ в таблице notifications), принятое тоже:
сначала сервис помечает объявление «на проверке» (mod_pending), а когда оно принято, шлёт одно сообщение на все принятые сразу.
"""
import re
import sqlite3

from app.engine.util import day_add

RECENT_DAYS = 7   # о принятии сообщаем только по недавно отправленным объявлениям: включённые позже уведомления не завалят чат старым

# (признак в тексте Директа, что не так, что делать)
REASONS = [
    (r"превосход|лучш|№\s?1|номер один|самы[йе]\b", "В тексте сравнение с другими без подтверждения («лучший», «№1», «самый»).",
     "Уберите такие слова: в «Объявления» нажмите «Другой текст» или поправьте вручную."),
    (r"восклиц|пунктуац|заглавн|регистр|капс|знаки", "Лишние знаки или ВЕРХНИЙ РЕГИСТР в тексте.",
     "Перепишите без восклицательных знаков и слов заглавными буквами."),
    (r"гарант|100\s?%|бесплатн|скидк|акци|цен", "Обещание без подтверждения (гарантия, бесплатно, скидка или цена).",
     "Уберите обещание или подтвердите его на сайте, потом добавьте объявление заново."),
    (r"лиценз|разрешен|сертифик|медицин|предупрежд", "Для такой услуги нужна лицензия или предупреждение.",
     "Добавьте нужные данные на сайт и в рекламу (например, «Имеются противопоказания») или обратитесь в поддержку Директа."),
    (r"сайт|ссылк|недоступ|href|домен|страниц", "Яндексу не подошёл сайт или ссылка: сайт не открывается или не соответствует объявлению.",
     "Проверьте, что сайт открывается и на нём есть то, о чём говорит объявление."),
    (r"изображ|картинк|баннер|логотип|фото", "Не подошла картинка.", "Нарисуйте баннер заново («Другой баннер») без текста и логотипов чужих брендов."),
    (r"маркиров|erid|рекламодател|реклама", "Не хватает данных для маркировки рекламы.", "Заполните данные рекламодателя на странице «Бизнес»."),
]
FALLBACK_TODO = "Откройте «Объявления», поправьте текст или нажмите «Другой текст», затем добавьте объявление в рекламу заново."


def explain(note: str) -> tuple[str, str]:
    """(что не так простыми словами, что делать). Для неизвестной причины даём текст Директа и общий совет."""
    low = (note or "").lower()
    for pattern, why, todo in REASONS:
        if re.search(pattern, low):
            return why, todo
    clean = re.sub(r"\s+", " ", note or "").strip().rstrip(".")
    return (f"Яндекс не принял объявление: {clean}." if clean else "Яндекс не принял объявление, причину он не указал."), FALLBACK_TODO


def rejected_text(title: str, note: str) -> str:
    why, todo = explain(note)
    raw = re.sub(r"\s+", " ", note or "").strip()
    tail = f"\nОтвет Яндекса: {raw[:300]}" if raw and why.rstrip(".") != f"Яндекс не принял объявление: {raw.rstrip('.')}" else ""
    return f"❌ Яндекс отклонил объявление «{title}».\n{why}\nЧто делать: {todo}{tail}"


def mark_pending(conn: sqlite3.Connection, business_id: int, unit_id: int) -> None:
    """Объявление ушло на проверку: запоминаем, чтобы потом сказать, что его приняли."""
    conn.execute("INSERT OR IGNORE INTO notifications (business_id, kind, key) VALUES (?, 'mod_pending', ?)", (business_id, str(unit_id)))


def accepted(conn: sqlite3.Connection, biz, today: str) -> list[sqlite3.Row]:
    """Недавние объявления, которые были на проверке, а теперь приняты (идёт показ или ждёт оплаты) и о которых ещё не сообщали."""
    for u in conn.execute("SELECT id FROM ad_units WHERE business_id = ? AND state = 'moderation' AND external_ad_id IS NOT NULL", (biz["id"],)).fetchall():
        mark_pending(conn, biz["id"], u["id"])   # проверка могла закончиться между двумя проходами: помечаем и то, что видим на проверке сейчас
    since = day_add(today, -RECENT_DAYS)
    rows = conn.execute(
        "SELECT u.id, u.title, u.state, u.ad_kind FROM ad_units u "
        "JOIN notifications p ON p.business_id = u.business_id AND p.kind = 'mod_pending' AND p.key = CAST(u.id AS TEXT) "
        "WHERE u.business_id = ? AND u.state IN ('active', 'unpaid') AND u.created_day >= ? "
        "AND NOT EXISTS (SELECT 1 FROM notifications d WHERE d.business_id = u.business_id AND d.kind = 'mod_ok' AND d.key = CAST(u.id AS TEXT)) ORDER BY u.id",
        (biz["id"], since)).fetchall()
    conn.commit()
    return rows


def accepted_text(rows: list[sqlite3.Row]) -> str:
    names = ", ".join(f"«{r['title']}»" for r in rows[:5]) + (f" и ещё {len(rows) - 5}" if len(rows) > 5 else "")
    count = len(rows)
    head = f"✅ Яндекс принял {'объявление' if count == 1 else f'объявления ({count})'}: {names}."
    if any(r["state"] == "unpaid" for r in rows):
        return head + "\nПоказы начнутся после пополнения счёта в Яндекс Директе (минимум 300 ₽)."
    return head + "\nПоказы идут, сервис следит за результатом."


def record_accepted(conn: sqlite3.Connection, business_id: int, rows: list[sqlite3.Row]) -> None:
    for r in rows:
        conn.execute("INSERT OR IGNORE INTO notifications (business_id, kind, key) VALUES (?, 'mod_ok', ?)", (business_id, str(r["id"])))
    conn.commit()