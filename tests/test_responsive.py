from fastapi.testclient import TestClient

from app.db import connect
from app.direct.runner import sync_and_run
from app.main import app
from tests.fake_direct import FakeDirect
from tests.test_launch import isolated, rows, setup  # noqa: F401  (фикстура isolated нужна этому файлу)


def test_responsive_launch_makes_one_ad_with_two_titles_and_two_texts_per_group(isolated, monkeypatch):
    import app.direct.client as client_module
    fake = FakeDirect()
    monkeypatch.setattr(client_module, "DEFAULT_TRANSPORT", fake.transport)
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        preview = http.get(f"/api/businesses/{bid}/launch/preview", params={"ad_format": "responsive"}).json()
        assert preview["responsive"] is True and preview["count"] == 2 and preview["groups"] == 1          # по два заголовка и два текста: одна группа
        done = http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True, "ad_format": "responsive"})
        assert done.status_code == 200, done.text
        assert len(fake.ads) == 1                                                                         # одно комбинаторное объявление вместо двух обычных
        ad = next(iter(fake.ads.values()))
        assert ad["responsive"] and len(ad["ad"]["Titles"]) == 2 and len(ad["ad"]["Texts"]) == 2 and "Title" not in ad["ad"]
        units = rows(isolated, "SELECT title, ad_kind, variants_json FROM ad_units")
        assert len(units) == 1 and units[0]["ad_kind"] == "responsive" and units[0]["title"] == ad["ad"]["Titles"][0]
        assert rows(isolated, "SELECT ad_format FROM businesses")[0]["ad_format"] == "responsive"          # выбор запомнился

        fake.accept_all(state="ON", payment="ALLOWED")
        conn = connect(isolated)
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        result = sync_and_run(conn, biz, transport=fake.transport)
        assert not result["error"]                                                                          # обновление данных у комбинаторного объявления проходит
        assert conn.execute("SELECT state FROM ad_units").fetchone()["state"] == "active"
        overview = http.get(f"/api/businesses/{bid}/overview").json()
        assert overview["units"][0]["kind"] == "responsive" and len(overview["units"][0]["variants"]["titles"]) == 2
        conn.close()


def test_launch_falls_back_to_plain_ads_when_direct_refuses_the_combinatorial_one(isolated):
    fake = FakeDirect()
    fake.reject_responsive = True
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        done = http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True, "ad_format": "responsive"})
        assert done.status_code == 200, done.text
        assert len(fake.ads) == 2 and not any(a.get("responsive") for a in fake.ads.values())              # сделаны обычные объявления, запуск не сломался
        notes = [r["status_note"] for r in rows(isolated, "SELECT status_note FROM ad_units")]
        assert any("Комбинаторное объявление Директ не принял" in n for n in notes)