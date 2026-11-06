"""Подробности объявления: тексты, фразы группы, минус-слова, ссылка, картинки."""
from fastapi.testclient import TestClient

from app.db import connect
from app.main import app
from tests.fake_direct import FakeDirect
from tests.test_launch import isolated, launched  # noqa: F401


def test_unit_details_show_texts_keywords_negatives_and_link(isolated, monkeypatch):
    import app.direct.client as client_module
    fake = FakeDirect()
    monkeypatch.setattr(client_module, "DEFAULT_TRANSPORT", fake.transport)
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        unit = http.get(f"/api/businesses/{bid}/overview").json()["units"][0]
        d = http.get(f"/api/businesses/{bid}/units/{unit['id']}/details")
        assert d.status_code == 200
        data = d.json()
        assert data["titles"] and data["texts"] and data["state"]
        assert isinstance(data["keywords"], list) and data["negatives"]
        assert data["responsive"] is False and isinstance(data["pictures"], list)
        assert http.get(f"/api/businesses/{bid}/units/9999/details").status_code == 404
        conn = connect(isolated)
        conn.close()
