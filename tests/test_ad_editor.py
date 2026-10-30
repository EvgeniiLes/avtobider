"""Новая страница «Объявления»: редактор баннера (фото и подача для объявления), снятие идеи с рекламы, прежняя версия сайта."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from test_creatives import Fakes, isolated, png, setup  # noqa: E402,F401


def upload(http, base, tag="work"):
    r = http.post(f"{base}/assets", data={"kind": "photo", "tag": tag}, files={"file": ("a.png", png(), "image/png")})
    assert r.status_code == 200
    return r.json()["id"]


def test_the_previous_version_of_the_site_stays_available():
    with TestClient(app) as http:
        page = http.get("/old")
        assert page.status_code == 200 and "Новая версия сайта" in page.text and "/static-v1/app.js" in page.text
        assert http.get("/static-v1/app.js").status_code == 200 and http.get("/static-v1/style.css").status_code == 200
        assert "/static/app.js" in http.get("/").text and "Новая версия сайта" not in http.get("/").text


def test_ads_carry_everything_the_editor_needs(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    Fakes()
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}"
        first, second = upload(http, base, "work"), upload(http, base, "interior")
        data = http.get(f"{base}/creatives/ads").json()
        assert [p["id"] for p in data["photos"]] == [first, second] and all(p["url"] for p in data["photos"])
        assert [x["no"] for x in data["styles"]["looks"]] == [0, 1, 2, 3, 4] and data["styles"]["refs"]            # подачи есть всегда
        assert data["ref_extra"] == 0.02
        ad = data["ads"][0]
        assert ad["photo_id"] in (first, second) and ad["look_no"] == 0 and ad["ref_id"] is None


def test_photo_and_style_are_saved_per_ad_and_change_the_prompt(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    fakes.keep = 10 ** 8
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}"
        first, second = upload(http, base, "work"), upload(http, base, "interior")
        put = lambda body: http.put(f"{base}/creatives/ads/0/settings", json=body)
        ads = lambda: http.get(f"{base}/creatives/ads").json()
        ref = ads()["styles"]["refs"][0]["id"]

        assert put({"asset_id": 99999}).status_code == 400 and put({"ref_id": 9999}).status_code == 400 and put({"look": 7}).status_code == 422
        assert put({"asset_id": second, "look": 3}).status_code == 200
        ad = ads()["ads"][0]
        assert (ad["photo_id"], ad["look_no"], ad["ref_id"]) == (second, 3, None)
        assert "pastel" in ad["ai"]["prompt"] and "SECOND attached image" not in ad["ai"]["prompt"]           # выбрано обычное оформление

        assert put({"ref_id": ref}).status_code == 200                                                        # переключили на подачу по референсу
        ad = ads()["ads"][0]
        assert ad["ref_id"] == ref and ad["photo_id"] == second and "SECOND attached image" in ad["ai"]["prompt"]
        assert put({"look": 1}).status_code == 200                                                            # обратно к обычному оформлению: референс снят
        assert ads()["ads"][0]["ref_id"] is None

        put({"ref_id": ref})
        fakes.claude_text = json.dumps([ads()["ads"][0]["title"], "Записаться"], ensure_ascii=False)
        assert http.post(f"{base}/creatives/ads/0/ai-banner").status_code == 200
        _h, path, body = next(r for r in fakes.requests if r[0] == "queue.fal.run")
        assert path.endswith("gpt-image-2/edit") and body.count(b"data:image/jpeg;base64") == 2               # фото и референс
        assert http.get(f"{base}/creatives").json()["spend"]["spent_usd"] == 0.09                             # 7 центов баннер и 2 за референс


def test_renaming_the_text_keeps_the_photo_and_style_with_the_ad(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    Fakes()
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}"
        photo = upload(http, base, "work")
        added = http.post(f"{base}/creatives/texts", json={"title": "Маникюр в центре города", "body": "Запишитесь онлайн"}).json()
        http.put(f"{base}/creatives/{added['id']}", json={"status": "approved"})
        index = next(a["index"] for a in http.get(f"{base}/creatives/ads").json()["ads"] if a["title"] == "Маникюр в центре города")
        assert http.put(f"{base}/creatives/ads/{index}/settings", json={"asset_id": photo, "look": 2}).status_code == 200
        assert http.put(f"{base}/creatives/{added['id']}", json={"title": "Маникюр рядом с метро", "body": "Запишитесь онлайн"}).status_code == 200
        ad = next(a for a in http.get(f"{base}/creatives/ads").json()["ads"] if a["title"] == "Маникюр рядом с метро")
        assert ad["photo_id"] == photo and ad["look_no"] == 2                                                 # подача осталась
        assert "Маникюр рядом с метро" in ad["ai"]["prompt"] and "Маникюр в центре города" not in ad["ai"]["prompt"]


def test_idea_can_be_taken_off_the_ads(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}"
        upload(http, base, "work")
        fakes.claude_text = json.dumps([{"hypothesis": "Проверяем запись", "audience": "Занятые", "angle": "booking", "title": "Маникюр без очередей",
                                         "text": "Запишитесь онлайн", "photo": 1, "look": 1, "mood": "calm", "ref": None, "direction": ""}], ensure_ascii=False)
        idea = http.post(f"{base}/briefs/generate", json={"count": 2}).json()["briefs"][0]
        assert http.post(f"{base}/briefs/{idea['id']}/use").json()["status"] == "used"
        assert any(a["title"] == "Маникюр без очередей" and a["own"] for a in http.get(f"{base}/creatives/ads").json()["ads"])
        off = http.post(f"{base}/briefs/{idea['id']}/unuse").json()
        assert off["status"] == "idea"
        assert not any(a["title"] == "Маникюр без очередей" and a["own"] for a in http.get(f"{base}/creatives/ads").json()["ads"])
        assert http.post(f"{base}/briefs/{idea['id']}/use").json()["status"] == "used"                        # и снова можно взять
