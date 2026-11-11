import json

import httpx
from fastapi.testclient import TestClient

from app.db import connect
from app.deps import creative_transport
from app.direct import audience as aud
from app.direct.runner import sync_and_run
from app.main import app
from tests.fake_direct import FakeDirect
from tests.test_audience import PROFILE, isolated, setup  # noqa: F401  (фикстура isolated нужна этому файлу)


def test_the_recommendation_starts_with_exact_and_narrow_only_whatever_the_model_says():
    reco = aud.clean_recommendation('{"accessory": true, "alternative": true, "why": "Консалтинг: спрос идёт запросами вокруг темы."}')
    assert reco["categories"] == {"exact": True, "narrow": True, "alternative": False, "accessory": False, "broader": False} and reco["why"]   # остальные включит сервис, если показов нет, или владелец
    assert aud.clean_recommendation("не json") is None and aud.clean_recommendation('{"why": "x"}') is None


def _ai(accessory: bool):
    def handler(request):
        asked = json.loads(request.content)["messages"][0]["content"]
        answer = json.dumps({"accessory": accessory, "alternative": False, "why": "Услуга без товара: спрос идёт вокруг темы."}, ensure_ascii=False) \
            if "категории запросов" in asked else json.dumps(PROFILE, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"message": {"content": answer}}]})
    app.dependency_overrides[creative_transport] = lambda: httpx.MockTransport(handler)


def _auto(fake):
    return [k["settings"]["Categories"] for k in fake.keywords if k["keyword"] == "---autotargeting"]


def test_service_picks_the_categories_and_keeps_the_cabinet_in_line_with_the_profile(isolated, monkeypatch):
    import app.direct.client as client_module
    monkeypatch.setenv("FAL_KEY", "fal-test")
    _ai(True)
    fake = FakeDirect()
    monkeypatch.setattr(client_module, "DEFAULT_TRANSPORT", fake.transport)
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        profile = http.post(f"/api/businesses/{bid}/audience").json()["profile"]
        assert profile["auto_why"] and profile["autotargeting"]["categories"]["accessory"] is False        # сервис начинает осторожно, даже если ИИ советует сопутствующие
        assert http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True}).status_code == 200
        assert all(c["Accessory"] == "NO" and c["Exact"] == "YES" and c["Narrow"] == "YES" and c["Broader"] == "NO" for c in _auto(fake))   # в кабинет ушли целевые и узкие

        edited = http.put(f"/api/businesses/{bid}/audience", json={"categories": {"accessory": True}}).json()   # владелец сам включил сопутствующие
        assert edited["profile"]["auto_owner"] is True
        conn = connect(isolated)
        fake.accept_all(state="ON", payment="ALLOWED")
        sync_and_run(conn, conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone(), transport=fake.transport)
        assert all(c["Accessory"] == "YES" for c in _auto(fake))                                             # синхронизация сама подправила кабинет
        again = len(fake.calls)
        sync_and_run(conn, conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone(), transport=fake.transport)
        assert [c for c in fake.calls[again:] if c == ("keywords", "update")] == []                          # без изменений кабинет не трогаем
        conn.close()