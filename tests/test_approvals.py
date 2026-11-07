"""Ежедневный разбор рекламы и правки, которые делаются только после «Да» владельца в Telegram."""
import json
import sqlite3
from datetime import datetime, timezone

import httpx
from fastapi.testclient import TestClient

from app import approvals
from app.db import connect
from app.deps import telegram_transport
from app.direct import audience as aud
from app.direct import review
from app.engine.util import MICRO
from app.main import app
from tests.fake_direct import FakeDirect
from tests.test_metrika import connected, isolated  # noqa: F401  (isolated: своя база на каждый тест)

NOW = datetime(2026, 11, 8, 8, 0, tzinfo=timezone.utc)   # 11:00 по Москве


class FakeBot:
    """Bot API: всё, что бот отправил с кнопками и без, и как правили сообщения."""

    def __init__(self):
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.transport = httpx.MockTransport(self.handle)
        app.dependency_overrides[telegram_transport] = lambda: self.transport

    def handle(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[1]
        body = json.loads(request.content)
        if method == "sendMessage":
            self.sent.append(body)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent)}})
        if method == "editMessageText":
            self.edits.append(body)
        return httpx.Response(200, json={"ok": True, "result": True})


def prepare(http, isolated, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret-token")
    fake = FakeDirect()
    bot = FakeBot()
    bid = connected(http, fake, isolated)
    conn = connect(isolated)
    conn.execute("UPDATE users SET telegram_chat = '777'")
    aud.save(conn, bid, {"summary": "x", "gender": "any", "ages": ["25-34", "35-44", "45-54"], "income": "any"})
    rows = [("placement", "com.vitastudio.mahjong", 40, 100), ("placement", "com.fugo.wow", 15, 40), ("placement", "game.yandex.ru", 60, 150),
            ("age", "AGE_0_17", 50, 60), ("age", "AGE_25_34", 150, 200), ("age", "AGE_55", 3, 5)]
    for dimension, key, clicks, spend in rows:
        conn.execute("INSERT INTO dimension_stats (business_id, dimension, key, day, impressions, clicks, spend_micro, leads) VALUES (?,?,?,?,?,?,?,0)",
                     (bid, dimension, key, "2026-11-06", clicks * 10, clicks, spend * MICRO))
    conn.commit()
    return fake, bot, bid, conn


def press(http, approval_id, answer, chat=777, secret=None):
    update = {"callback_query": {"id": "cb1", "data": f"ap:{approval_id}:{answer}", "message": {"chat": {"id": chat}}}}
    return http.post("/api/telegram/hook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": secret or approvals.hook_secret()})


def test_daily_review_asks_once_and_acts_only_after_yes(isolated, monkeypatch):
    with TestClient(app) as http:
        fake, bot, bid, conn = prepare(http, isolated, monkeypatch)
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()

        assert approvals.run_daily(conn, biz, NOW, bot.transport) == 2
        assert [m["text"].split("\n")[0][:40] for m in bot.sent] and all(m["chat_id"] == "777" for m in bot.sent)
        assert all([b["text"] for b in m["reply_markup"]["inline_keyboard"][0]] == ["Да, сделать", "Нет"] for m in bot.sent)
        sites_text = next(m["text"] for m in bot.sent if "приложени" in m["text"])
        assert "com.vitastudio.mahjong" in sites_text and "game.yandex.ru" not in sites_text      # игры отключаем, настоящий сайт не трогаем
        assert approvals.run_daily(conn, biz, NOW, bot.transport) == 0                            # в тот же день второй раз не спрашиваем
        assert fake.excluded == {} and fake.bid_modifiers == {}                                    # без ответа в кабинете ничего не меняется

        pending = {r["kind"]: r["id"] for r in conn.execute("SELECT id, kind FROM approvals WHERE status = 'pending'")}
        assert set(pending) == {"sites", "ages"}

        assert press(http, pending["sites"], "y", secret="wrong").status_code == 403              # запрос не от Telegram
        assert press(http, pending["sites"], "y", chat=999).status_code == 200                    # нажал чужой чат: ничего не происходит
        assert fake.excluded == {} and conn.execute("SELECT status FROM approvals WHERE id = ?", (pending["sites"],)).fetchone()[0] == "pending"

        assert press(http, pending["sites"], "y").status_code == 200
        assert sorted(next(iter(fake.excluded.values()))) == ["com.fugo.wow", "com.vitastudio.mahjong"]
        assert conn.execute("SELECT status FROM approvals WHERE id = ?", (pending["sites"],)).fetchone()[0] == "approved"
        assert "Сделано" in bot.edits[-1]["text"] and bot.edits[-1]["message_id"] == 1
        assert press(http, pending["sites"], "y").status_code == 200 and len(fake.excluded) == 1   # повторное нажатие ничего не делает

        assert press(http, pending["ages"], "y").status_code == 200
        off = [m for m in fake.bid_modifiers.values() if m["percent"] == 0]
        assert [m["age"] for m in off] == ["AGE_0_17"]
        assert json.loads(conn.execute("SELECT audience FROM businesses WHERE id = ?", (bid,)).fetchone()[0])["ages_off"] == ["AGE_0_17"]


def test_a_declined_proposal_is_not_repeated_and_a_failed_one_is_reported(isolated, monkeypatch):
    with TestClient(app) as http:
        fake, bot, bid, conn = prepare(http, isolated, monkeypatch)
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        approvals.run_daily(conn, biz, NOW, bot.transport)
        ids = {r["kind"]: r["id"] for r in conn.execute("SELECT id, kind FROM approvals")}

        assert press(http, ids["sites"], "n").status_code == 200
        assert conn.execute("SELECT status FROM approvals WHERE id = ?", (ids["sites"],)).fetchone()[0] == "declined" and fake.excluded == {}
        assert "Отклонено" in bot.edits[-1]["text"]

        conn.execute("UPDATE businesses SET audience = '' WHERE id = ?", (bid,))                  # профиль пропал: правку возраста сделать нельзя
        conn.commit()
        assert press(http, ids["ages"], "y").status_code == 200
        row = conn.execute("SELECT status, result FROM approvals WHERE id = ?", (ids["ages"],)).fetchone()
        assert row["status"] == "failed" and "Профиль" in row["result"] and "Не получилось" in bot.edits[-1]["text"]

        next_day = datetime(2026, 11, 9, 8, 0, tzinfo=timezone.utc)
        assert approvals.run_daily(conn, biz, next_day, bot.transport) == 0                       # отклонённое не повторяем, неудавшееся пока ждёт паузу
        assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 2


def test_nothing_is_sent_before_ten_or_without_a_linked_chat(isolated, monkeypatch):
    with TestClient(app) as http:
        fake, bot, bid, conn = prepare(http, isolated, monkeypatch)
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        assert approvals.run_daily(conn, biz, datetime(2026, 11, 8, 5, 0, tzinfo=timezone.utc), bot.transport) == 0   # 08:00 по Москве
        conn.execute("UPDATE users SET telegram_chat = ''")
        conn.commit()
        assert approvals.run_daily(conn, biz, NOW, bot.transport) == 0 and bot.sent == []


def test_game_apps_are_told_apart_from_real_sites():
    assert review.is_app("com.vitastudio.mahjong") and review.is_app("kidultlovin.word.zen") and review.is_app("com.avito.android")
    assert not review.is_app("game.yandex.ru") and not review.is_app("dsp.yandex.ru") and not review.is_app("dzen.ru") and not review.is_app("localhost")


def test_pace_note_appears_only_when_spending_runs_well_over_the_weekly_limit(isolated, monkeypatch):
    with TestClient(app) as http:
        fake, bot, bid, conn = prepare(http, isolated, monkeypatch)
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        unit = conn.execute("SELECT id FROM ad_units WHERE business_id = ? LIMIT 1", (bid,)).fetchone()[0]
        conn.execute("UPDATE campaigns SET weekly_micro = ?", (1000 * MICRO,))
        for day in ("2026-11-06", "2026-11-07"):
            conn.execute("INSERT OR REPLACE INTO stats_daily (unit_id, day, impressions, clicks, spend_micro, leads, clients) VALUES (?,?,100,10,?,0,0)", (unit, day, 500 * MICRO))
        conn.commit()
        note = review.pace_note(conn, biz, "2026-11-08")
        assert note and "быстрее недельного лимита" in note and "3 500 ₽" in note
        conn.execute("UPDATE campaigns SET weekly_micro = ?", (5000 * MICRO,))
        conn.commit()
        assert review.pace_note(conn, biz, "2026-11-08") is None


def test_ask_waits_for_the_owners_answer_and_reports_it_in_the_exit_code(isolated, monkeypatch):
    from app import ask
    with TestClient(app) as http:
        fake, bot, bid, conn = prepare(http, isolated, monkeypatch)
        real = approvals.dispatch
        monkeypatch.setattr(approvals, "dispatch", lambda c, transport=None: real(c, bot.transport))
        monkeypatch.setattr(ask.time, "sleep", lambda s: approvals.decide(conn, 1, True, "777", transport=bot.transport))   # владелец нажимает «Да»
        assert ask.main(["Выкладываю обновление. Выкладывать?", "--email", "o@example.com", "--wait", "30"]) == 0
        assert bot.sent[0]["text"] == "Выкладываю обновление. Выкладывать?" and "Принято" in bot.edits[-1]["text"]

        monkeypatch.setattr(ask.time, "sleep", lambda s: approvals.decide(conn, 2, False, "777", transport=bot.transport))   # а теперь «Нет»
        assert ask.main(["Ещё раз?", "--email", "o@example.com", "--wait", "30"]) == 1

        monkeypatch.setattr(ask.time, "sleep", lambda s: None)                                                                 # никто не отвечает
        assert ask.main(["Ждём?", "--email", "o@example.com", "--wait", "0"]) == 2
        assert ask.main(["Кому?", "--email", "нет@example.com"]) == 2


def test_stats_button_answers_the_owner_with_numbers_for_now_and_ignores_strangers(isolated, monkeypatch):
    with TestClient(app) as http:
        fake, bot, bid, conn = prepare(http, isolated, monkeypatch)
        unit = conn.execute("SELECT id FROM ad_units WHERE business_id = ? LIMIT 1", (bid,)).fetchone()[0]
        today = datetime.now(approvals.MSK).date().isoformat()
        conn.execute("INSERT OR REPLACE INTO stats_daily (unit_id, day, impressions, clicks, spend_micro, leads, clients) VALUES (?,?,300,12,?,2,0)", (unit, today, 480 * MICRO))
        conn.commit()
        say = lambda text, chat=777, kind="private": http.post("/api/telegram/hook", json={"message": {"text": text, "chat": {"id": chat, "type": kind}}},
                                                                headers={"X-Telegram-Bot-Api-Secret-Token": approvals.hook_secret()})
        assert say("Статистика").status_code == 200
        text = bot.sent[-1]["text"]
        assert "Лотос" in text and "Сегодня: показов 300, кликов 12" in text and "480 ₽" in text and "2 обращения" in text
        assert bot.sent[-1]["reply_markup"]["keyboard"][0][0]["text"] == "Статистика"            # кнопка остаётся под полем ввода
        sent = len(bot.sent)
        say("Статистика", chat=999)                                                                # чужой чат
        say("Статистика", kind="group")
        say("что-то другое")
        assert len(bot.sent) == sent
