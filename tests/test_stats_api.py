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
    body = {"name": "Лотос", "kind": "Маникюр", "city": "Москва", "weekly_budget": 20000, "target_cpa": 800}
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
