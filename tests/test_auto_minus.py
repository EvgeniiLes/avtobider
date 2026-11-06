"""Сервис сам ставит в минус слова из запросов, которые потратили деньги без обращений: после разбора ИИ и с предохранителями."""
from datetime import date

from fastapi.testclient import TestClient

from app import reports
from app.db import connect
from app.engine.util import MICRO, day_add
from app.main import app
from tests.fake_direct import FakeDirect
from tests.test_queries import ROWS, ai, isolated, launched, saved_profile, sync  # noqa: F401


def minus_in_cabinet(fake):
    return next(iter(fake.negatives.values()), [])


def test_wasteful_off_target_query_is_cut_by_the_service_itself(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        fake.query_rows = ROWS
        ai([{"query": "маникюр самому", "minus": "самому", "why": "ищут, как сделать самим"}])
        sync(isolated, bid, fake)
        assert "самому" in minus_in_cabinet(fake)
        profile = saved_profile(isolated, bid)
        assert "самому" in profile["negatives"] and profile["history"][-1]["auto"] is True
        conn = connect(isolated)
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        texts = [a["text"] for a in reports.alerts(conn, biz, date.today().isoformat()) if a["kind"] == "auto_minus"]
        assert len(texts) == 1 and "самому" in texts[0]
        conn.close()


def test_word_from_a_query_that_brought_leads_is_never_cut(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        fake.query_rows = ROWS + [("запись самому себе", 100, 5, 10 * MICRO, 2)]   # слово «самому» встречается в запросе с обращениями
        ai([{"query": "маникюр самому", "minus": "самому", "why": "x"}])
        sync(isolated, bid, fake)
        assert "самому" not in minus_in_cabinet(fake)
        assert "history" not in saved_profile(isolated, bid) or not saved_profile(isolated, bid)["history"]


def test_service_decides_once_a_day_and_not_without_waste_or_a_goal(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        fake.query_rows = [("маникюр цена", 1000, 30, 900 * MICRO, 3)]
        ai([{"query": "маникюр цена", "minus": "цена", "why": "x"}])
        sync(isolated, bid, fake)
        assert "цена" not in minus_in_cabinet(fake)                    # потерь нет: ИИ даже не спрашиваем
        fake.query_rows = ROWS
        ai([{"query": "маникюр самому", "minus": "самому", "why": "x"}])
        sync(isolated, bid, fake, today=day_add(date.today().isoformat(), 1))
        assert "самому" in minus_in_cabinet(fake)
        calls = len(fake.negatives_calls) if hasattr(fake, "negatives_calls") else None
        sync(isolated, bid, fake, today=day_add(date.today().isoformat(), 1))
        assert calls is None or len(fake.negatives_calls) == calls     # в тот же день повторно не разбираем


def test_service_cuts_at_most_five_words_at_a_time(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        fake.query_rows = [(f"маникюр слово{c}", 50, 10, 500 * MICRO, 0) for c in "абвгдеж"]
        ai([{"query": f"маникюр слово{c}", "minus": f"слово{c}", "why": "x"} for c in "абвгдеж"])
        sync(isolated, bid, fake)
        assert len(saved_profile(isolated, bid)["history"][-1]["minus"]) == 5
