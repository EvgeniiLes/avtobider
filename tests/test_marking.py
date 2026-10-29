import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.deps import client_factory
from app.direct.marking import KINDS, is_filled, valid_inn
from app.main import app
from app.routes import auth
from app.security import encrypt_token
from tests.fake_direct import FakeDirect

# настоящие по контрольным цифрам ИНН для проверки алгоритма (взяты из открытых примеров и посчитаны по формуле)
ORG_INN = "7707083893"      # 10 цифр
PERSON_INN = "500100732259"  # 12 цифр


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    auth._fails.clear()
    yield tmp_path / "test.db"
    app.dependency_overrides.clear()


def setup(http, fake, db_path):
    http.post("/api/auth/register", json={"email": "o@example.com", "password": "password123"})
    app.dependency_overrides[client_factory] = fake.factory()
    bid = http.post("/api/businesses", json={"name": "Лотос", "kind": "Маникюрный салон", "city": "Москва", "weekly_budget": 20000,
                                             "target_cpa": 800, "description": "Маникюр.", "site_url": "salon.ru", "mode": "auto"}).json()["id"]
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO ad_accounts (user_id, title, token_enc) VALUES (1, 'fake-login', ?)", (encrypt_token("tok", "test-secret-key"),))
    conn.commit()
    conn.close()
    return bid


def test_inn_checksum():
    assert valid_inn(ORG_INN, 10) and valid_inn(PERSON_INN, 12)
    assert not valid_inn("7707083894", 10) and not valid_inn("500100732250", 12)   # неверная контрольная цифра
    assert not valid_inn("123", 10) and not valid_inn("77070838930", 10) and not valid_inn("abcdefghij", 10)
    assert {k: v[0] for k, v in KINDS.items()} == {"person": "PHYSICAL", "ip": "INDIVIDUAL", "org": "LEGAL"}
    assert is_filled({"tin_type": "LEGAL", "has_tin": True, "org_name": "ООО"}) and not is_filled({"tin_type": "LEGAL", "has_tin": True, "org_name": ""})


def test_empty_cabinet_is_reported_and_launch_still_needs_marking(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        data = http.get(f"/api/businesses/{bid}/marking").json()
        assert data["filled"] is False and data["has_tin"] is False and set(data["kinds"]) == {"person", "ip", "org"}
        assert http.post(f"/api/businesses/{bid}/launch", json={}).status_code == 400


def test_saving_writes_to_direct_and_unlocks_launch_without_storing_the_inn(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        r = http.post(f"/api/businesses/{bid}/marking", json={"kind": "org", "tin": ORG_INN, "name": "ООО «Лотос»", "phone": "+7 900 123-45-67"})
        assert r.status_code == 200 and r.json()["filled"] is True and ORG_INN not in r.text            # ИНН в ответ не возвращаем
        assert fake.advertiser == {"tin_type": "LEGAL", "tin": ORG_INN, "name": "ООО «Лотос»", "phone": "+79001234567"}
        assert ("clients", "update") in fake.calls
        assert http.get(f"/api/businesses/{bid}/launch/preview").json()["marking"]["confirmed"] is True
        assert http.post(f"/api/businesses/{bid}/launch", json={}).status_code == 200                  # без подтверждения галочкой
    dump = open(isolated, "rb").read()
    assert ORG_INN.encode() not in dump                                                                 # в нашей базе ИНН нет


def test_already_filled_cabinet_is_noticed_automatically(isolated):
    fake = FakeDirect()
    fake.advertiser = {"tin_type": "INDIVIDUAL", "tin": PERSON_INN, "name": "ИП Иванов", "phone": "+79990000000"}
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        assert http.get(f"/api/businesses/{bid}/marking").json()["filled"] is True
        step = {s["key"]: s for s in http.get(f"/api/businesses/{bid}/overview").json()["checklist"]}["marking"]
        assert step["done"] is True


def test_bad_inn_phone_and_direct_errors_are_explained(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        url = f"/api/businesses/{bid}/marking"
        r = http.post(url, json={"kind": "org", "tin": "7707083894", "name": "ООО"})
        assert r.status_code == 400 and "ИНН указан неверно" in r.json()["detail"]["message"] and fake.advertiser["tin"] is None
        r = http.post(url, json={"kind": "org", "tin": ORG_INN, "name": "ООО", "phone": "не телефон"})
        assert r.status_code == 400 and "Телефон" in r.json()["detail"]["message"]
        assert http.post(url, json={"kind": "bad", "tin": ORG_INN, "name": "ООО"}).status_code == 422
        fake.update_error = "Недостаточно прав для изменения данных"
        r = http.post(url, json={"kind": "org", "tin": ORG_INN, "name": "ООО «Лотос»"})
        assert r.status_code == 400 and "Недостаточно прав" in r.json()["detail"]["message"]


def test_other_users_cannot_touch_the_data(isolated):
    fake = FakeDirect()
    with TestClient(app) as alice, TestClient(app) as bob:
        bid = setup(alice, fake, isolated)
        bob.post("/api/auth/register", json={"email": "b@example.com", "password": "password123"})
        assert bob.get(f"/api/businesses/{bid}/marking").status_code == 404
        assert bob.post(f"/api/businesses/{bid}/marking", json={"kind": "org", "tin": ORG_INN, "name": "ООО"}).status_code == 404
