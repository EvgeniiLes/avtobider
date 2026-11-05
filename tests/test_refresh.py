import json

import httpx
from fastapi.testclient import TestClient

from app.deps import creative_transport
from app.main import app
from tests.fake_direct import FakeDirect
from tests.test_audience import PROFILE, isolated, setup  # noqa: F401  (фикстура isolated нужна этому файлу)


def _ai():
    def handler(request):
        asked = json.loads(request.content)["messages"][0]["content"]
        if "копирайтер" in asked:
            answer = {"title2": ["Запись онлайн", "Рядом с вами", "Опытные мастера"], "callouts": ["Маникюр и педикюр", "В центре города"], "display_path": "запись"}
        elif "категории запросов" in asked:
            answer = {"accessory": False, "alternative": False, "why": "Местная услуга: нужны только целевые запросы."}
        else:
            answer = PROFILE
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)}}]})
    app.dependency_overrides[creative_transport] = lambda: httpx.MockTransport(handler)


def test_refresh_pushes_new_data_into_the_running_campaigns(isolated, monkeypatch):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        assert http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True}).status_code == 200   # без ИИ: объявления без дополнений и без фраз
        assert all("Title2" not in a["ad"] and "CalloutSetting" not in a["ad"] for a in fake.ads.values()) and not fake.adextensions

        monkeypatch.setenv("FAL_KEY", "fal-test")                                                           # появился ИИ: профиль и дополнения можно составить
        _ai()
        assert http.post(f"/api/businesses/{bid}/audience").status_code == 200
        done = http.post(f"/api/businesses/{bid}/refresh-campaigns")
        assert done.status_code == 200, done.text
        data = done.json()
        assert data["problems"] == [] and any("Дополнения" in c for c in data["changes"]) and any("фраз" in c for c in data["changes"])
        assert all(a["ad"].get("Title2") or a["ad"].get("DisplayUrlPath") for a in fake.ads.values())
        assert all(a["ad"]["CalloutSetting"]["AdExtensions"][0]["Operation"] == "ADD" for a in fake.ads.values()) and len(fake.adextensions) == 2
        assert any(k["keyword"] == "записаться на маникюр" for k in fake.keywords)                          # фразы профиля дошли до группы
        assert fake.negatives[next(iter(fake.campaigns))]                                                   # и минус-слова до кампании

        again = http.post(f"/api/businesses/{bid}/refresh-campaigns").json()
        assert not any("Дополнения" in c for c in again["changes"]) and len(fake.adextensions) == 2         # второй раз дополнения не дублируются