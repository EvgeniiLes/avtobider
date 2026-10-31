import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routes import auth


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    auth._fails.clear()


def register(http, email="owner@example.com"):
    return http.post("/api/auth/register", json={"email": email, "password": "password123"})


def make_business(http, **extra):
    body = {"name": "Лотос", "kind": "Маникюр", "city": "Москва", "weekly_budget": 20000, "target_cpa": 800, "mode": "auto"}
    body.update(extra)
    return http.post("/api/businesses", json=body).json()["id"]


def test_requires_login():
    with TestClient(app) as http:
        assert http.get("/api/businesses/1/overview").status_code == 401
        assert http.post("/api/businesses/1/sim/start").status_code == 401


def test_overview_before_start_explains_what_is_missing():
    with TestClient(app) as http:
        register(http)
        bid = make_business(http, target_cpa=0, avg_check=0)
        ov = http.get(f"/api/businesses/{bid}/overview").json()
        assert ov["example"] is False
        assert "Укажите" in ov["needs"]
        r = http.post(f"/api/businesses/{bid}/sim/start")
        assert r.status_code == 400
        assert "Укажите" in r.json()["detail"]["message"]


def test_start_needs_budget():
    with TestClient(app) as http:
        register(http)
        bid = make_business(http, weekly_budget=0)
        r = http.post(f"/api/businesses/{bid}/sim/start")
        assert r.status_code == 400
        assert "бюджет" in r.json()["detail"]["message"]


def test_full_flow_overview_history_rollback():
    with TestClient(app) as http:
        register(http)
        bid = make_business(http)
        assert http.post(f"/api/businesses/{bid}/sim/start").status_code == 200
        assert http.post(f"/api/businesses/{bid}/sim/start").status_code == 400  # уже запущен

        adv = http.post(f"/api/businesses/{bid}/sim/advance", json={"days": 30})
        assert adv.status_code == 200

        ov = http.get(f"/api/businesses/{bid}/overview").json()
        assert ov["example"] is True
        assert len(ov["units"]) >= 6
        assert ov["totals"]["spend"] > 0
        assert ov["budget"]["spent_week"] <= ov["budget"]["weekly"]
        assert {u["verdict"] for u in ov["units"]} & {"Окупается", "Не окупается", "Остановлено", "Пока неясно", "Собираем данные"}

        history = http.get(f"/api/businesses/{bid}/changes").json()["changes"]
        assert history
        first = next(c for c in history if c["can_rollback"])
        assert first["reason"] and first["who"] == "Автобидер"

        ok = http.post(f"/api/businesses/{bid}/changes/{first['id']}/rollback", json={"force": True})
        assert ok.status_code == 200
        again = http.post(f"/api/businesses/{bid}/changes/{first['id']}/rollback", json={})
        assert again.status_code == 400
        assert "уже отменено" in again.json()["detail"]["message"]

        after = http.get(f"/api/businesses/{bid}/changes").json()["changes"]
        assert after[0]["who"] == "Вы" and after[0]["action"] == "Отмена"
        assert any(c["id"] == first["id"] and c["reverted"] for c in after)

        assert http.post(f"/api/businesses/{bid}/sim/reset").status_code == 200
        assert http.get(f"/api/businesses/{bid}/overview").json()["example"] is False
        assert http.get(f"/api/businesses/{bid}/changes").json()["changes"] == []


def test_default_mode_is_ask_and_proposals_can_be_decided():
    with TestClient(app) as http:
        register(http)
        bid = make_business(http, mode="ask")
        assert http.get(f"/api/businesses/{bid}").json()["mode"] == "ask"
        http.post(f"/api/businesses/{bid}/sim/start")
        http.post(f"/api/businesses/{bid}/sim/advance", json={"days": 30})
        ov = http.get(f"/api/businesses/{bid}/overview").json()
        assert ov["mode"] == "ask" and ov["proposals"] > 0
        assert all(u["active"] for u in ov["units"])  # ничего не изменено без подтверждения

        items = http.get(f"/api/businesses/{bid}/changes").json()["changes"]
        proposal = next(c for c in items if c["can_decide"])
        assert proposal["status"] == "proposed" and proposal["reason"].startswith("Предлагаем") and not proposal["can_rollback"]

        assert http.post(f"/api/businesses/{bid}/changes/{proposal['id']}/approve").status_code == 200
        done = next(c for c in http.get(f"/api/businesses/{bid}/changes").json()["changes"] if c["id"] == proposal["id"])
        assert done["status"] == "applied" and done["can_rollback"] and not done["reason"].startswith("Предлагаем")
        assert http.post(f"/api/businesses/{bid}/changes/{proposal['id']}/approve").status_code == 400

        other = next(c for c in http.get(f"/api/businesses/{bid}/changes").json()["changes"] if c["can_decide"])
        assert http.post(f"/api/businesses/{bid}/changes/{other['id']}/reject").status_code == 200
        after = {c["id"]: c for c in http.get(f"/api/businesses/{bid}/changes").json()["changes"]}
        assert after[other["id"]]["status"] == "rejected" and not after[other["id"]]["can_decide"]


def test_stop_all_and_resume_all_api():
    with TestClient(app) as http:
        register(http)
        bid = make_business(http)
        http.post(f"/api/businesses/{bid}/sim/start")
        http.post(f"/api/businesses/{bid}/sim/advance", json={"days": 3})
        assert http.post(f"/api/businesses/{bid}/stop-all").json()["stopped"] == 6
        ov = http.get(f"/api/businesses/{bid}/overview").json()
        assert ov["stopped"] is True and not any(u["active"] for u in ov["units"])
        assert http.post(f"/api/businesses/{bid}/resume-all").json()["resumed"] == 6
        ov = http.get(f"/api/businesses/{bid}/overview").json()
        assert ov["stopped"] is False and all(u["active"] for u in ov["units"])


def test_insights_compare_styles_and_wait_for_enough_data():
    with TestClient(app) as http:
        register(http)
        bid = make_business(http)
        assert http.get(f"/api/businesses/{bid}/insights").json()["groups"] == []
        http.post(f"/api/businesses/{bid}/sim/start")
        data = http.get(f"/api/businesses/{bid}/insights").json()
        assert {g["style"] for g in data["groups"]} == {"photo", "luxe", "pastel", "bold", "ai"}  # шесть объявлений, пять стилей
        assert "мало данных" in data["conclusions"][0] or "Нужно" in data["conclusions"][0]
        http.post(f"/api/businesses/{bid}/sim/advance", json={"days": 30})
        data = http.get(f"/api/businesses/{bid}/insights").json()
        assert all(g["impressions"] > 0 and g["label"] for g in data["groups"]) and data["conclusions"]


def test_ai_analysis_uses_the_numbers_and_needs_a_key(monkeypatch):
    import json as _json
    import httpx
    from app.deps import creative_transport
    with TestClient(app) as http:
        register(http)
        bid = make_business(http)
        assert http.post(f"/api/businesses/{bid}/insights/analyze").status_code == 503      # без ключа
        monkeypatch.setenv("FAL_KEY", "fal-test")
        assert http.post(f"/api/businesses/{bid}/insights/analyze").status_code == 400      # объявлений ещё нет
        http.post(f"/api/businesses/{bid}/sim/start")
        http.post(f"/api/businesses/{bid}/sim/advance", json={"days": 10})
        seen = []

        def handler(request):
            seen.append(_json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "Данных пока мало, вывод предварительный."}}]})

        app.dependency_overrides[creative_transport] = lambda: httpx.MockTransport(handler)
        r = http.post(f"/api/businesses/{bid}/insights/analyze").json()
        assert r["text"].startswith("Данных пока мало")
        prompt = seen[0]["messages"][1]["content"]
        assert "Тёмный люкс" in prompt and "Помещение" in prompt and "показы" in prompt   # цифры и подписи переданы
        data = http.get(f"/api/businesses/{bid}/insights").json()
        assert {g["label"] for g in data["type_groups"]} == {"Помещение", "Наши работы"}


def test_checklist_follows_progress():
    with TestClient(app) as http:
        register(http)
        bid = make_business(http, description="", weekly_budget=0, target_cpa=0)
        steps = {s["key"]: s for s in http.get(f"/api/businesses/{bid}/overview").json()["checklist"]}
        assert not steps["described"]["done"] and not steps["money"]["done"] and not steps["cabinet"]["done"] and not steps["ads"]["done"]
        assert steps["metrika"]["done"] is False and steps["metrika"]["optional"] is True
        assert all(s["title_todo"] != s["title"] and " не " in s["title_todo"] for s in steps.values())     # пока не сделано: «не внесены», «не созданы»
        assert http.get(f"/api/businesses/{bid}/series").json() == {"days": []}
        http.put(f"/api/businesses/{bid}", json={"name": "Лотос", "kind": "Маникюр", "city": "Москва", "description": "Маникюр",
                                                   "weekly_budget": 20000, "target_cpa": 800})
        http.post(f"/api/businesses/{bid}/sim/start")
        steps = {s["key"]: s for s in http.get(f"/api/businesses/{bid}/overview").json()["checklist"]}
        assert steps["described"]["done"] and steps["money"]["done"] and steps["ads"]["done"]


def test_users_are_isolated():
    with TestClient(app) as alice, TestClient(app) as bob:
        register(alice, "alice@example.com")
        register(bob, "bob@example.com")
        bid = make_business(alice)
        alice.post(f"/api/businesses/{bid}/sim/start")
        alice.post(f"/api/businesses/{bid}/sim/advance", json={"days": 20})
        for method, path, body in [
            ("get", "overview", None),
            ("get", "changes", None),
            ("post", "sim/start", None),
            ("post", "sim/advance", {"days": 1}),
            ("post", "sim/reset", None),
            ("post", "changes/1/rollback", {}),
            ("post", "changes/1/approve", None),
            ("post", "changes/1/reject", None),
            ("post", "stop-all", None),
            ("post", "resume-all", None),
        ]:
            r = getattr(bob, method)(f"/api/businesses/{bid}/{path}", **({"json": body} if body is not None else {}))
            assert r.status_code == 404, path


def test_advance_limits():
    with TestClient(app) as http:
        register(http)
        bid = make_business(http)
        http.post(f"/api/businesses/{bid}/sim/start")
        assert http.post(f"/api/businesses/{bid}/sim/advance", json={"days": 0}).status_code == 422
        assert http.post(f"/api/businesses/{bid}/sim/advance", json={"days": 99}).status_code == 422


def test_advance_without_start():
    with TestClient(app) as http:
        register(http)
        bid = make_business(http)
        r = http.post(f"/api/businesses/{bid}/sim/advance", json={"days": 1})
        assert r.status_code == 400
        assert "Сначала запустите пример" in r.json()["detail"]["message"]
