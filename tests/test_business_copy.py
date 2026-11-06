"""Бизнес «такой же»: копия берёт настройки, фото и логотип, но не рекламу."""
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from app.main import app
from tests.test_launch import isolated  # noqa: F401


def png():
    out = BytesIO()
    Image.new("RGB", (900, 700), (10, 120, 200)).save(out, "PNG")
    return out.getvalue()


def test_copy_has_the_same_settings_photos_and_logo_but_its_own_name(isolated):
    with TestClient(app) as http:
        http.post("/api/auth/register", json={"email": "c@example.test", "password": "password123"})
        made = http.post("/api/businesses", json={
            "name": "Тритон", "kind": "Автомойка", "description": "Мойка и детейлинг", "city": "Москва", "address": "ул. Тверская, 1", "districts": "Арбат",
            "weekly_budget": 9000, "avg_check": 2500, "client_share_percent": 40, "mode": "auto", "color_main": "#112233"}).json()
        for kind in ("photo", "photo", "logo"):
            assert http.post(f"/api/businesses/{made['id']}/assets", data={"kind": kind, "tag": "work"}, files={"file": ("a.png", png(), "image/png")}).status_code == 200
        r = http.post(f"/api/businesses/{made['id']}/copy")
        assert r.status_code == 200
        copy = r.json()
        assert copy["id"] != made["id"] and copy["name"] == "Тритон (копия)"
        for field in ("kind", "description", "city", "address", "districts", "weekly_budget", "avg_check", "client_share_percent", "mode", "color_main"):
            assert copy[field] == made[field], field
        full = http.get(f"/api/businesses/{copy['id']}").json()
        assert sorted(a["kind"] for a in full["assets"]) == ["logo", "photo", "photo"]
        assert all(http.get(a["url"]).status_code == 200 for a in full["assets"])                 # файлы скопированы, а не общие
        old = http.get(f"/api/businesses/{made['id']}").json()
        assert {a["id"] for a in old["assets"]}.isdisjoint({a["id"] for a in full["assets"]})
        assert http.post("/api/businesses/9999/copy").status_code == 404


def test_copy_respects_the_business_limit(isolated):
    with TestClient(app) as http:
        http.post("/api/auth/register", json={"email": "d@example.test", "password": "password123"})
        first = http.post("/api/businesses", json={"name": "А", "kind": "Мойка"}).json()
        for i in range(9):
            assert http.post("/api/businesses", json={"name": f"Б{i}", "kind": "Мойка"}).status_code == 200
        assert http.post(f"/api/businesses/{first['id']}/copy").status_code == 400
