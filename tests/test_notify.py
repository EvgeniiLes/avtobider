"""Telegram: привязка чата, отчёт недели, тревоги и защита от повторов."""
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app import notifier, telegram
from app.db import connect
from app.deps import telegram_transport
from app.direct.runner import sync_and_run
from app.engine.util import MICRO, day_add
from app.main import app
from app.reports import alerts, week_report
from app.routes import auth
from tests.fake_direct import FakeDirect
from tests.test_metrika import connected, setup


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.delenv("FAL_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_NAME", raising=False)
    monkeypatch.delenv("TELEGRAM_API_BASE", raising=False)
    auth._fails.clear()
    yield tmp_path / "test.db"
    app.dependency_overrides.clear()


class FakeTelegram:
    """Bot API: обновления, которые «видит» бот, и всё, что бот отправил."""

    def __init__(self):
        self.updates: list[dict] = []
        self.sent: list[tuple[str, str]] = []
        self.hosts: list[str] = []
        self.down = False
        self.transport = httpx.MockTransport(self.handle)
        app.dependency_overrides[telegram_transport] = lambda: self.transport

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("unreachable", request=request)
        self.hosts.append(request.url.host)
        method = request.url.path.rsplit("/", 1)[1]
        body = json.loads(request.content)
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": self.updates})
        if method == "sendMessage":
            self.sent.append((str(body["chat_id"]), body["text"]))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent)}})
        return httpx.Response(404, json={"ok": False, "description": "Not Found"})

    def start(self, code, chat_id=777, name="Анна", kind="private"):
        self.updates.append({"update_id": len(self.updates) + 1, "message": {"text": f"/start {code}", "chat": {"id": chat_id, "type": kind, "first_name": name}}})


def configure(monkeypatch, api=None):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret-token")
    monkeypatch.setenv("TELEGRAM_BOT_NAME", "@autobider_bot")
    if api:
        monkeypatch.setenv("TELEGRAM_API_BASE", api)


def register(http):
    http.post("/api/auth/register", json={"email": "o@example.com", "password": "password123"})


# ---------- привязка чата ----------

def test_without_a_bot_on_the_server_the_card_explains_it(isolated):
    with TestClient(app) as http:
        register(http)
        assert http.get("/api/telegram").json() | {"bot": ""} == {"configured": False, "linked": False, "notify": True, "bot": "", "relay": False, "code": "", "link": ""}
        r = http.post("/api/telegram/connect")
        assert r.status_code == 503 and "deploy\\set-telegram-keys.ps1" in r.json()["detail"]["message"]


def test_chat_is_linked_by_a_one_time_code_and_can_be_tested_switched_off_and_removed(isolated, monkeypatch):
    configure(monkeypatch)
    tg = FakeTelegram()
    with TestClient(app) as http:
        register(http)
        status = http.post("/api/telegram/connect").json()
        code = status["code"]
        assert status["configured"] and len(code) == 10 and status["link"] == f"https://t.me/autobider_bot?start={code}"

        assert http.post("/api/telegram/check").json()["found"] is False          # владелец ещё не нажал Start
        tg.start("ЧУЖОЙКОД")                                                       # чужой код и групповой чат не подходят
        tg.start(code, chat_id=-5, kind="group")
        assert http.post("/api/telegram/check").json()["found"] is False
        assert tg.sent == []

        tg.start(code)
        done = http.post("/api/telegram/check").json()
        assert done["found"] is True and done["linked"] is True and done["name"] == "Анна" and done["code"] == ""
        assert [chat for chat, _ in tg.sent] == ["777"] and "Автобидер подключён" in tg.sent[0][1]
        assert http.get("/api/telegram").json()["linked"] is True

        assert http.post("/api/telegram/test").json() == {"sent": True} and len(tg.sent) == 2
        assert http.put("/api/telegram", json={"notify": False}).json()["notify"] is False
        assert http.delete("/api/telegram").json()["linked"] is False
        assert http.post("/api/telegram/test").status_code == 400


def test_an_expired_code_must_be_asked_for_again(isolated, monkeypatch):
    configure(monkeypatch)
    FakeTelegram()
    with TestClient(app) as http:
        register(http)
        code = http.post("/api/telegram/connect").json()["code"]
        conn = sqlite3.connect(isolated)
        old = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(timespec="seconds")
        conn.execute("UPDATE users SET telegram_code_at = ?", (old,))
        conn.commit()
        conn.close()
        assert http.get("/api/telegram").json()["code"] == ""
        r = http.post("/api/telegram/check")
        assert r.status_code == 400 and "устарел" in r.json()["detail"]["message"]
        assert code != http.post("/api/telegram/connect").json()["code"]


def test_unreachable_telegram_is_explained_without_leaking_the_token(isolated, monkeypatch):
    configure(monkeypatch)
    tg = FakeTelegram()
    with TestClient(app) as http:
        register(http)
        http.post("/api/telegram/connect")
        tg.down = True
        r = http.post("/api/telegram/check")
        assert r.status_code == 502 and "TELEGRAM_API_BASE" in r.json()["detail"]["message"] and "secret-token" not in r.text


def test_requests_go_to_the_relay_when_one_is_configured(isolated, monkeypatch):
    configure(monkeypatch, api="https://relay.example.com/")
    tg = FakeTelegram()
    with TestClient(app) as http:
        register(http)
        assert http.post("/api/telegram/connect").json()["relay"] is True
        http.post("/api/telegram/check")
        assert tg.hosts == ["relay.example.com"]


# ---------- итоги недели ----------

def rep(text: str) -> str:
    return text.replace(" ", " ")


def test_week_report_compares_with_the_week_before_and_names_the_best_and_the_weakest(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = connected(http, fake, isolated)
        conn = connect(isolated)
        today = date.today().isoformat()
        rows = [(day_add(today, -d), 0, 2000, 20, 300 * MICRO, 3) for d in range(1, 8)]       # эта неделя: объявление 0 приводит обращения
        rows += [(day_add(today, -d), 1, 800, 10, 200 * MICRO, 0) for d in range(1, 8)]        # объявление 1 тратит и ничего не приводит
        rows += [(day_add(today, -d), 0, 900, 10, 200 * MICRO, 1) for d in range(8, 15)]      # неделей раньше
        fake.report_rows = rows
        biz = lambda: conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        sync_and_run(conn, biz(), today, transport=fake.transport)
        titles = [r["title"] for r in conn.execute("SELECT title FROM ad_units ORDER BY id")]

        data = week_report(conn, biz(), today)
        text = rep(data["text"])
        assert text.startswith("Итоги недели «Лотос»") and data["end"] == day_add(today, -1)
        assert "Потрачено: 3 500 ₽ (неделей ранее 1 400 ₽, +150%)" in text
        assert "Клики: 210 (было 70, +200%)" in text
        assert "Обращения: 21 (было 7, +200%), цена обращения 167 ₽" in text
        assert "Клиентов: 6 (оценка по доле обращений)" in text
        assert f"Лучшее объявление: «{titles[0]}», 21 обращение по 100 ₽." in text
        assert f"Слабое: «{titles[1]}», потратило 1 400 ₽, обращений нет." in text
        conn.close()

        overview = http.get(f"/api/businesses/{bid}/report").json()["report"]
        assert overview["text"] == data["text"] or overview["start"] == data["start"]


def test_report_does_not_blame_ads_when_the_counter_is_broken_and_asks_for_metrika_when_missing(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = connected(http, fake, isolated)
        conn = connect(isolated)
        today = date.today().isoformat()
        fake.report_rows = [(day_add(today, -d), 1, 800, 10, 200 * MICRO, 0) for d in range(1, 8)]
        sync_and_run(conn, conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone(), today, transport=fake.transport)
        conn.execute("UPDATE businesses SET counter_health = ?", (json.dumps({"level": "bad", "title": "Метрика не получает визитов", "message": "x"}),))
        conn.commit()
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        text = week_report(conn, biz, today)["text"]
        assert "Слабое:" not in text and "метрика не получает визитов" in text.lower()
        conn.execute("UPDATE businesses SET metrika_counter_id = '', metrika_goal_id = '', counter_health = ''")
        conn.commit()
        assert "Обращений не видно" in week_report(conn, conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone(), today)["text"]
        conn.close()


# ---------- что и когда уходит владельцу ----------

def owner_with_chat(http, fake, db_path, **user):
    bid = setup(http, fake, db_path)
    http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True})
    conn = connect(db_path)
    conn.execute("UPDATE users SET telegram_chat = '777', notify_on = ?", (user.get("notify_on", 1),))
    conn.commit()
    return conn, bid


def biz_row(conn, bid):
    return conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()


MONDAY_10_MSK = datetime(2026, 11, 2, 7, 0, tzinfo=timezone.utc)   # понедельник, 10:00 по Москве


def test_weekly_report_goes_out_once_a_week_on_monday_morning_and_not_before(isolated):
    with TestClient(app) as http:
        conn, bid = owner_with_chat(http, FakeDirect(), isolated)
        sent = []
        send = lambda chat, text: sent.append((chat, text))
        sunday = datetime(2026, 11, 1, 20, 0, tzinfo=timezone.utc)
        assert notifier.run(conn, biz_row(conn, bid), send, now=sunday) == []                       # воскресенье: ещё рано
        early = datetime(2026, 11, 2, 5, 30, tzinfo=timezone.utc)                                   # понедельник 8:30 по Москве
        assert notifier.run(conn, biz_row(conn, bid), send, now=early) == []
        out = notifier.run(conn, biz_row(conn, bid), send, now=MONDAY_10_MSK)
        assert len(out) == 1 and out[0].startswith("Итоги недели «Лотос»") and sent[0][0] == "777"
        assert notifier.run(conn, biz_row(conn, bid), send, now=MONDAY_10_MSK + timedelta(hours=3)) == []   # в ту же неделю второй раз не шлём
        assert len(notifier.run(conn, biz_row(conn, bid), send, now=MONDAY_10_MSK + timedelta(days=7))) == 1  # на следующей неделе снова
        conn.close()


def test_a_missed_monday_is_made_up_on_tuesday_but_not_later_in_the_week(isolated):
    with TestClient(app) as http:
        conn, bid = owner_with_chat(http, FakeDirect(), isolated)
        send = lambda chat, text: None
        wednesday = datetime(2026, 11, 4, 7, 0, tzinfo=timezone.utc)
        assert notifier.run(conn, biz_row(conn, bid), send, now=wednesday) == []        # подключили в среду: отчёта за «неделю» ещё нет
        tuesday = datetime(2026, 11, 3, 7, 0, tzinfo=timezone.utc)
        assert len(notifier.run(conn, biz_row(conn, bid), send, now=tuesday)) == 1
        conn.close()


def test_nothing_is_sent_without_a_chat_or_when_the_owner_switched_it_off_or_the_bot_is_missing(isolated, monkeypatch):
    with TestClient(app) as http:
        conn, bid = owner_with_chat(http, FakeDirect(), isolated, notify_on=0)
        sent = []
        assert notifier.run(conn, biz_row(conn, bid), lambda c, t: sent.append(t), now=MONDAY_10_MSK) == []
        conn.execute("UPDATE users SET notify_on = 1, telegram_chat = ''")
        conn.commit()
        assert notifier.run(conn, biz_row(conn, bid), lambda c, t: sent.append(t), now=MONDAY_10_MSK) == []
        conn.execute("UPDATE users SET telegram_chat = '777'")
        conn.commit()
        assert notifier.run(conn, biz_row(conn, bid), None, now=MONDAY_10_MSK) == [] and sent == []    # бот на сервере не настроен
        assert notifier.make_sender() is None
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
        assert notifier.make_sender() is not None
        conn.close()


def test_alerts_are_sent_once_and_repeat_only_after_their_pause(isolated):
    with TestClient(app) as http:
        conn, bid = owner_with_chat(http, FakeDirect(), isolated)
        conn.execute("UPDATE businesses SET metrika_counter_id = '555', counter_health = ?",
                     (json.dumps({"level": "bad", "title": "Метрика не получает визитов", "message": "Код не стоит на сайте."}),))
        conn.execute("UPDATE ad_units SET state = 'rejected', status_note = 'Запрещённое слово' WHERE id = (SELECT MIN(id) FROM ad_units)")
        conn.commit()
        sent = []
        send = lambda chat, text: sent.append(text)
        tuesday = datetime(2026, 11, 3, 7, 0, tzinfo=timezone.utc)
        first = notifier.run(conn, biz_row(conn, bid), send, now=tuesday)
        assert any("Метрика не получает визитов" in t for t in first) and any("не прошло проверку" in t and "Запрещённое слово" in t for t in first)
        assert notifier.run(conn, biz_row(conn, bid), send, now=tuesday + timedelta(hours=2)) == []          # тот же день: тишина
        later = notifier.run(conn, biz_row(conn, bid), send, now=tuesday + timedelta(days=1))
        assert later == []                                                                                    # счётчик: не чаще раза в 3 дня; объявление: один раз
        again = notifier.run(conn, biz_row(conn, bid), send, now=tuesday + timedelta(days=4))
        assert len(again) == 1 and "Метрика не получает визитов" in again[0]
        conn.close()


def test_a_failed_send_is_retried_next_time_instead_of_being_forgotten(isolated):
    with TestClient(app) as http:
        conn, bid = owner_with_chat(http, FakeDirect(), isolated)

        def broken(chat, text):
            raise telegram.TelegramError("недоступен")

        assert notifier.run(conn, biz_row(conn, bid), broken, now=MONDAY_10_MSK) == []
        assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
        sent = []
        assert len(notifier.run(conn, biz_row(conn, bid), lambda c, t: sent.append(t), now=MONDAY_10_MSK + timedelta(minutes=30))) == 1
        conn.close()


def test_alerts_cover_sync_errors_missing_payment_waiting_decisions_and_silent_ads(isolated):
    with TestClient(app) as http:
        conn, bid = owner_with_chat(http, FakeDirect(), isolated)
        today = "2026-11-10"
        conn.execute("UPDATE businesses SET last_sync_error = 'Нет доступа к кабинету'")
        conn.execute("UPDATE ad_units SET state = 'unpaid'")
        conn.commit()
        kinds = {a["kind"] for a in alerts(conn, biz_row(conn, bid), today)}
        assert {"sync_error", "unpaid"} <= kinds and "no_impressions" not in kinds
        conn.execute("UPDATE ad_units SET state = 'active'")
        conn.execute("UPDATE campaigns SET created_day = '2026-11-01' WHERE source = 'direct'")
        conn.execute("INSERT INTO changes (business_id, day, actor, action, reason_code, reason_text, before_json, after_json, status) "
                     "VALUES (?, ?, 'auto', 'pause', 'x', 'Остановить слабое объявление', '{}', '{}', 'proposed')", (bid, today))
        conn.commit()
        found = {a["kind"]: a["text"] for a in alerts(conn, biz_row(conn, bid), today)}
        assert "no_impressions" in found and "Ждёт вашего решения: 1 предложение" in found["waiting"]
        conn.close()


def test_worker_calls_the_notifier_after_each_business_even_if_syncing_failed(isolated, monkeypatch):
    from app import worker
    fake = FakeDirect()
    with TestClient(app) as http:
        conn, bid = owner_with_chat(http, fake, isolated)
        conn.close()
        calls = []
        monkeypatch.setattr(worker, "notify", lambda conn, biz, send: calls.append((biz["id"], send is None)))
        worker.run_once(transport=fake.transport)
        assert calls == [(bid, True)]      # бот не настроен: отправителя нет, но вызов был и использовал свежую строку бизнеса
