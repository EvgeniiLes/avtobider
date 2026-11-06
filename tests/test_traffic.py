import json
from datetime import date, timedelta

import httpx
from fastapi.testclient import TestClient

from app.db import connect
from app.deps import creative_transport
from app.direct import audience as aud
from app.direct.runner import sync_and_run
from app.main import app
from tests.fake_direct import FakeDirect
from tests.test_audience import PROFILE, isolated, setup  # noqa: F401  (фикстура isolated нужна этому файлу)


async def _wider(*args, **kwargs):   # ИИ подбирает более широкие фразы (внутри синхронизации он ходит без тестового транспорта, поэтому подменяем)
    return ["автоматизация процессов", "внедрение нейросетей", "ии для бизнеса"]


def _ai():
    def handler(request):
        asked = json.loads(request.content)["messages"][0]["content"]
        if "У рекламы нет показов" in asked:
            answer = ["автоматизация процессов", "внедрение нейросетей", "ии для бизнеса"]
        elif "категории запросов" in asked:
            answer = {"accessory": False, "alternative": False, "why": "Местная услуга."}
        else:
            answer = PROFILE
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)}}]})
    app.dependency_overrides[creative_transport] = lambda: httpx.MockTransport(handler)


def test_service_widens_the_reach_step_by_step_when_active_ads_have_no_impressions(isolated, monkeypatch):
    import app.direct.client as client_module
    monkeypatch.setenv("FAL_KEY", "fal-test")
    monkeypatch.setattr(aud, "widen_keywords", _wider)
    _ai()
    fake = FakeDirect()
    monkeypatch.setattr(client_module, "DEFAULT_TRANSPORT", fake.transport)
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        assert http.post(f"/api/businesses/{bid}/audience").status_code == 200
        assert http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True}).status_code == 200
        conn = connect(isolated)
        conn.execute("UPDATE campaigns SET created_day = ?", ((date.today() - timedelta(days=10)).isoformat(),))   # идёт давно, а показов нет
        conn.commit()
        fake.accept_all(state="ON", payment="ALLOWED")
        for k in fake.keywords:
            if k["keyword"] != "---autotargeting":
                k["serving"] = "RARELY_SERVED"                                                                    # у фраз «мало показов»
        biz = lambda: conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        cats = lambda: [k["settings"]["Categories"] for k in fake.keywords if k["keyword"] == "---autotargeting"]
        assert all(c["Accessory"] == "NO" for c in cats())

        sync_and_run(conn, biz(), transport=fake.transport)
        profile = aud.load(biz())
        assert profile["traffic"]["step"] == 1 and any("Сопутствующие" in d for d in profile["traffic"]["log"][0]["did"])
        assert all(c["Accessory"] == "YES" and c["Alternative"] == "NO" for c in cats())                          # первый шаг: сопутствующие
        assert any(k["keyword"] == "автоматизация процессов" for k in fake.keywords)                              # и более широкие фразы в группе

        sync_and_run(conn, biz(), transport=fake.transport)
        assert aud.load(biz())["traffic"]["step"] == 1                                                            # раньше чем через два дня следующего шага нет

        profile = aud.load(biz())
        profile["traffic"]["day"] = (date.today() - timedelta(days=3)).isoformat()
        aud.save(conn, bid, profile)
        sync_and_run(conn, biz(), transport=fake.transport)
        assert aud.load(biz())["traffic"]["step"] == 2 and all(c["Alternative"] == "YES" for c in cats())          # второй шаг: альтернативные

        from app.reports import alerts
        texts = [a["text"] for a in alerts(conn, biz(), date.today().isoformat()) if a["kind"] == "widened"]
        assert len(texts) == 1 and "расширил охват" in texts[0] and "шаг 2 из 3" in texts[0]                     # владелец узнаёт об этом в Telegram
        conn.close()


def test_owner_chosen_categories_are_not_widened_and_no_widening_when_ads_show(isolated, monkeypatch):
    import app.direct.client as client_module
    monkeypatch.setenv("FAL_KEY", "fal-test")
    monkeypatch.setattr(aud, "widen_keywords", _wider)
    _ai()
    fake = FakeDirect()
    monkeypatch.setattr(client_module, "DEFAULT_TRANSPORT", fake.transport)
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        http.post(f"/api/businesses/{bid}/audience")
        http.put(f"/api/businesses/{bid}/audience", json={"categories": {"accessory": False}})                   # владелец сам выбрал категории
        assert http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True}).status_code == 200
        conn = connect(isolated)
        conn.execute("UPDATE campaigns SET created_day = ?", ((date.today() - timedelta(days=10)).isoformat(),))
        conn.commit()
        fake.accept_all(state="ON", payment="ALLOWED")
        biz = lambda: conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        sync_and_run(conn, biz(), transport=fake.transport)
        assert all(k["settings"]["Categories"]["Accessory"] == "NO" for k in fake.keywords if k["keyword"] == "---autotargeting")   # выбор владельца не трогаем
        assert any(k["keyword"] == "ии для бизнеса" for k in fake.keywords)                                      # но фразы расширяем

        unit = conn.execute("SELECT id FROM ad_units").fetchone()["id"]
        conn.execute("INSERT INTO stats_daily (unit_id, day, impressions, clicks, spend_micro, leads) VALUES (?, ?, 50, 2, 100000000, 0)",
                     (unit, (date.today() - timedelta(days=1)).isoformat()))
        profile = aud.load(biz())
        profile["traffic"]["day"] = (date.today() - timedelta(days=5)).isoformat()
        aud.save(conn, bid, profile)
        before = aud.load(biz())["traffic"]["step"]
        sync_and_run(conn, biz(), transport=fake.transport)
        assert aud.load(biz())["traffic"]["step"] == before                                                       # показы пошли: лестница не идёт дальше
        conn.close()