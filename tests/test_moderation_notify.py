from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app import notifier
from app.direct import moderation
from app.main import app
from tests.fake_direct import FakeDirect
from tests.test_notify import alerts_only, biz_row, isolated, owner_with_chat  # noqa: F401  (фикстура isolated нужна этому файлу)


def test_rejection_reasons_are_explained_in_plain_words_with_a_next_step():
    why, todo = moderation.explain("Отклонено: использована превосходная степень «лучший» без подтверждения")
    assert "сравнение" in why and "Уберите" in todo
    assert "Лишние знаки" in moderation.explain("Недопустимые знаки пунктуации и заглавные буквы")[0]
    why, todo = moderation.explain("")
    assert "причину он не указал" in why and "Другой текст" in todo
    text = moderation.rejected_text("Груминг в Адлере", "Непонятная причина X")
    assert "отклонил объявление «Груминг в Адлере»" in text and "Что делать" in text and "Ответ Яндекса: Непонятная причина X" not in text   # свой текст уже в причине, не дублируем


def test_owner_gets_one_message_when_ads_are_accepted_and_never_again(isolated):
    with TestClient(app) as http:
        conn, bid = owner_with_chat(http, FakeDirect(), isolated)
        send_log = []
        send = lambda chat, text: send_log.append(text)
        now = datetime.now(timezone.utc).replace(hour=12)
        assert not any("принял" in m for m in alerts_only(notifier.run(conn, biz_row(conn, bid), send, now=now)))      # пока на проверке: тишина
        conn.execute("UPDATE ad_units SET state = 'active'")
        conn.commit()
        first = alerts_only(notifier.run(conn, biz_row(conn, bid), send, now=now))
        assert len([m for m in first if "принял" in m]) == 1 and "объявления (3)" in " ".join(first)                  # одно сообщение на все три
        assert not any("принял" in m for m in alerts_only(notifier.run(conn, biz_row(conn, bid), send, now=now)))     # дублей нет
        conn.execute("UPDATE ad_units SET state = 'paused'")
        conn.execute("UPDATE ad_units SET state = 'active'")
        conn.commit()
        assert not any("принял" in m for m in alerts_only(notifier.run(conn, biz_row(conn, bid), send, now=now)))
        conn.close()


def test_old_ads_do_not_flood_the_chat_and_unpaid_ads_say_what_to_do(isolated):
    with TestClient(app) as http:
        conn, bid = owner_with_chat(http, FakeDirect(), isolated)
        send_log = []
        send = lambda chat, text: send_log.append(text)
        now = datetime.now(timezone.utc).replace(hour=12)
        conn.execute("UPDATE ad_units SET state = 'active', created_day = '2026-01-01'")                         # давно работающие
        conn.commit()
        assert not any("принял" in m for m in alerts_only(notifier.run(conn, biz_row(conn, bid), send, now=now)))
        conn.execute("UPDATE ad_units SET state = 'unpaid', created_day = ?", (now.date().isoformat(),))
        conn.commit()
        text = " ".join(alerts_only(notifier.run(conn, biz_row(conn, bid), send, now=now)))
        assert "принял" in text and "пополнения счёта" in text
        conn.close()


def test_rejected_ad_message_carries_the_reason_and_the_advice(isolated):
    with TestClient(app) as http:
        conn, bid = owner_with_chat(http, FakeDirect(), isolated)
        conn.execute("UPDATE ad_units SET state = 'rejected', status_note = 'Превосходная степень без подтверждения' WHERE id = (SELECT MIN(id) FROM ad_units)")
        conn.commit()
        sent = []
        out = alerts_only(notifier.run(conn, biz_row(conn, bid), lambda c, t: sent.append(t), now=datetime.now(timezone.utc).replace(hour=12)))
        rejected = [m for m in out if "отклонил" in m]
        assert len(rejected) == 1 and "Уберите такие слова" in rejected[0]
        assert not [m for m in alerts_only(notifier.run(conn, biz_row(conn, bid), lambda c, t: sent.append(t), now=datetime.now(timezone.utc).replace(hour=13))) if "отклонил" in m]
        conn.close()