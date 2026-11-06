"""Поисковые запросы: загрузка отчёта Директа, кандидаты в минус-слова и фразы, разбор ИИ, применение в кабинете."""
import json
import sqlite3
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.deps import creative_transport
from app.direct import audience as aud
from app.direct.runner import sync_and_run
from app.engine.util import MICRO, day_add
from app.main import app
from app.routes import auth
from tests.fake_direct import FakeDirect
from tests.test_audience import PROFILE, setup


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("FAL_KEY", "fal-test")
    auth._fails.clear()
    yield tmp_path / "test.db"
    app.dependency_overrides.clear()


def ai(irrelevant=None, status=200):
    """ИИ-заглушка: на просьбу разобрать запросы отвечает списком нецелевых, на всё остальное профилем аудитории."""
    def handler(request):
        system = json.loads(request.content)["messages"][0]["content"]
        content = json.dumps({"irrelevant": irrelevant or []} if "список реальных поисковых запросов" in system else PROFILE, ensure_ascii=False)
        return httpx.Response(status, json={"choices": [{"message": {"content": content}}]})

    app.dependency_overrides[creative_transport] = lambda: httpx.MockTransport(handler)


ROWS = [("маникюр цена", 1000, 30, 900 * MICRO, 3), ("маникюр самому", 400, 12, 500 * MICRO, 0), ("гель лак дома", 90, 1, 20 * MICRO, 0)]


def launched(http, fake, db_path, goal=True):
    ai()
    bid = setup(http, fake, db_path)
    assert http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True}).status_code == 200
    fake.accept_all(state="ON", payment="ALLOWED")
    if goal:
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE businesses SET metrika_counter_id = '555', metrika_goal_id = '9001' WHERE id = ?", (bid,))
        conn.commit()
        conn.close()
    return bid


def saved_profile(db_path, bid):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return aud.load(conn.execute("SELECT audience FROM businesses WHERE id = ?", (bid,)).fetchone())
    finally:
        conn.close()


def sync(db_path, bid, fake, today=None):
    conn = connect(db_path)
    try:
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        make_ai = app.dependency_overrides.get(creative_transport)
        return sync_and_run(conn, biz, today or date.today().isoformat(), transport=fake.transport, ai_transport=make_ai() if make_ai else None)
    finally:
        conn.close()


def test_queries_are_loaded_once_a_day_and_only_for_our_campaign(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        fake.query_rows, fake.query_foreign_rows = ROWS, [("чужой запрос", 10, 1, MICRO, 0)]
        assert sync(isolated, bid, fake)["error"] == ""
        assert fake.query_requests[0]["Goals"] == ["9001"] and "Query" in fake.query_requests[0]["FieldNames"]
        conn = connect(isolated)
        assert {r["query"]: (r["clicks"], r["leads"]) for r in conn.execute("SELECT * FROM search_queries")} == {
            "маникюр цена": (30, 3), "маникюр самому": (12, 0), "гель лак дома": (1, 0)}        # чужой кампании в запросах нет
        sync(isolated, bid, fake)
        assert len(fake.query_requests) == 1                                                  # в тот же день отчёт не запрашиваем снова
        sync(isolated, bid, fake, today=day_add(date.today().isoformat(), 1))
        assert len(fake.query_requests) == 2
        conn.close()


def test_a_failing_queries_report_does_not_break_the_main_sync(isolated, monkeypatch):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        fake.query_rows = ROWS
        original = fake._query_report
        fake._query_report = lambda params: httpx.Response(400, json={"error": {"error_code": 8000, "error_string": "Отчёт недоступен"}})
        fake.report_rows = [(date.today().isoformat(), 0, 1000, 30, 450 * MICRO, 2)]
        assert sync(isolated, bid, fake)["error"] == ""
        conn = connect(isolated)
        assert conn.execute("SELECT COUNT(*) FROM search_queries").fetchone()[0] == 0 and conn.execute("SELECT SUM(clicks) FROM stats_daily").fetchone()[0] == 30
        fake._query_report = original
        conn.close()


def test_page_shows_wasteful_queries_and_the_ones_that_brought_leads(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        assert http.get(f"/api/businesses/{bid}/queries").json()["queries"] == []
        fake.query_rows = ROWS
        sync(isolated, bid, fake)
        data = http.get(f"/api/businesses/{bid}/queries").json()
        assert data["launched"] is True and data["has_profile"] and data["has_goal"] and data["ai_ready"] is True
        assert [x["query"] for x in data["queries"]] == ["маникюр цена", "маникюр самому", "гель лак дома"]   # по расходу
        assert data["queries"][0]["cost_per_lead"] == 300.0
        assert [w["query"] for w in data["waste"]] == ["маникюр самому"]    # 500 ₽ без обращений при цене клиента 800 ₽; копеечный запрос не в счёт
        assert [k["phrase"] for k in data["keywords"]] == ["маникюр цена"]  # привёл обращения, а такой фразы ещё нет


def test_without_a_goal_nothing_is_called_wasteful(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated, goal=False)
        fake.query_rows = ROWS
        sync(isolated, bid, fake)
        data = http.get(f"/api/businesses/{bid}/queries").json()
        assert data["has_goal"] is False and data["waste"] == [] and data["keywords"] == [] and len(data["queries"]) == 3
        assert "Goals" not in fake.query_requests[0]


def test_ai_marks_off_target_queries_and_only_safe_words_get_through(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        fake.query_rows = ROWS
        sync(isolated, bid, fake)
        ai([
            {"query": "маникюр самому", "minus": "самому", "why": "ищут, как сделать самим"},
            {"query": "гель лак дома", "minus": "ДОМА!", "why": "x"},          # знаки Директ не примет
            {"query": "маникюр цена", "minus": "маникюр", "why": "x"},           # режет собственную услугу
            {"query": "маникюр самому", "minus": "дёшево", "why": "x"},          # слова нет в запросе
            {"query": "несуществующий запрос", "minus": "запрос", "why": "x"},   # такого запроса не было
        ])
        data = http.post(f"/api/businesses/{bid}/queries/analyze").json()
        assert data["checked"] == 3 and [g["word"] for g in data["minus"]] == ["самому"]
        assert data["minus"][0] | {"why": ""} == {"word": "самому", "queries": ["маникюр самому"], "clicks": 12, "spend": 500.0, "why": ""}


def test_analysis_needs_key_clicks_and_a_working_ai(isolated, monkeypatch):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        assert http.post(f"/api/businesses/{bid}/queries/analyze").status_code == 400    # запросов ещё нет
        fake.query_rows = ROWS
        sync(isolated, bid, fake)
        ai(status=500)
        assert http.post(f"/api/businesses/{bid}/queries/analyze").status_code == 502
        monkeypatch.delenv("FAL_KEY")
        assert http.post(f"/api/businesses/{bid}/queries/analyze").status_code == 503


def test_applied_minus_words_and_phrases_reach_the_cabinet_and_the_profile(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        campaign = next(iter(fake.campaigns))
        r = http.post(f"/api/businesses/{bid}/queries/apply", json={
            "minus": ["самому", "маникюр", "плохое!!"], "keywords": ["маникюр цена", "запись маникюр!!"]})
        assert r.status_code == 200
        data = r.json()
        assert data["minus"] == ["самому"] and data["keywords"] == ["маникюр цена"]
        skipped = {s["item"]: s["reason"] for s in data["skipped"]}
        assert "режет" in skipped["маникюр"] and "плохое!!" in skipped and "запись маникюр!!" in skipped
        sent = fake.negatives[campaign]
        assert "самому" in sent and "бесплатно" in sent and "маникюр" not in sent        # весь список целиком, а не только новое слово
        assert [k["keyword"] for k in fake.keywords][-1] == "маникюр цена"
        profile = saved_profile(isolated, bid)
        assert "самому" in profile["negatives"] and "маникюр цена" in profile["keywords"]
        assert profile["history"][-1]["minus"] == ["самому"] and profile["history"][-1]["keywords"] == ["маникюр цена"]
        again = http.post(f"/api/businesses/{bid}/queries/apply", json={"minus": ["самому"], "keywords": ["маникюр цена"]}).json()
        assert again["minus"] == [] and again["keywords"] == []                          # повторно то же самое ничего не меняет


def test_a_phrase_direct_refuses_is_reported_and_not_remembered(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        fake.reject_keywords = {"маникюр цена"}
        data = http.post(f"/api/businesses/{bid}/queries/apply", json={"keywords": ["маникюр цена"]}).json()
        assert data["keywords"] == [] and data["skipped"] == [{"item": "маникюр цена", "reason": "Недопустимые символы во фразе"}]
        assert "маникюр цена" not in http.get(f"/api/businesses/{bid}/audience").json()["keywords"]


def test_apply_needs_a_launched_ad_a_profile_and_a_choice(isolated, monkeypatch):
    with TestClient(app) as http:
        fake = FakeDirect()
        monkeypatch.delenv("FAL_KEY")
        bid = setup(http, fake, isolated)          # без ключа ИИ профиль не составился, запуска тоже нет
        assert http.post(f"/api/businesses/{bid}/queries/apply", json={"minus": ["самому"]}).status_code == 400
        assert http.get(f"/api/businesses/{bid}/queries").json()["launched"] is False
        monkeypatch.setenv("FAL_KEY", "fal-test")
        ai()
        http.post(f"/api/businesses/{bid}/audience")
        r = http.post(f"/api/businesses/{bid}/queries/apply", json={"minus": ["самому"]})
        assert r.status_code == 400 and "ещё не создана" in r.json()["detail"]["message"]
        assert http.post(f"/api/businesses/{bid}/queries/apply", json={}).status_code == 400
