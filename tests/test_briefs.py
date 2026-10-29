"""Стратег: идеи для A/B-теста, от запроса к ИИ до баннера по выбранной идее."""
import json
import sys
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from test_creatives import Fakes, isolated, png, setup  # noqa: E402,F401


def ideas(titles, photo_numbers=(1, 2, None)):
    return json.dumps([
        {"hypothesis": f"Проверяем идею {i}", "audience": f"Аудитория {i}", "angle": "booking", "title": t, "text": "Запишитесь онлайн на удобное время",
         "photo": photo_numbers[i % len(photo_numbers)], "look": i, "mood": "warm friendly mood"}
        for i, t in enumerate(titles)], ensure_ascii=False)


def upload(http, base, tag="work"):
    r = http.post(f"{base}/assets", data={"kind": "photo", "tag": tag}, files={"file": ("a.png", png(), "image/png")})
    assert r.status_code == 200
    return r.json()["id"]


def test_strategist_makes_different_ideas_and_the_chosen_one_becomes_an_ad(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}"
        first, second = upload(http, base, "work"), upload(http, base, "interior")
        fakes.claude_text = ideas(["Маникюр без очередей", "Запись на удобное время", "Мастера и запись без звонков"])
        got = http.post(f"{base}/briefs/generate", json={"count": 3})
        assert got.status_code == 200
        briefs = got.json()["briefs"]
        assert len(briefs) == 3 and len({b["look"] for b in briefs}) == 3                      # разные оформления
        assert len({b["photo_url"] for b in briefs if b["photo_url"]}) == 2                    # и разные фото (у третьей фото не указано, но назначено другое)
        assert all(b["hypothesis"] and b["audience"] and b["title"] for b in briefs)
        assert briefs[0]["prompt"] and briefs[0]["title"] in briefs[0]["prompt"] and "warm friendly mood" in briefs[0]["prompt"]
        assert http.get(f"{base}/creatives").json()["spend"]["spent_usd"] == 0.03              # идеи стоят 3 цента

        used = http.post(f"{base}/briefs/{briefs[1]['id']}/use").json()
        assert used["status"] == "used" and used["ad_index"] is not None                       # текст стал одним из объявлений
        ad = http.get(f"{base}/creatives/ads").json()["ads"][used["ad_index"]]
        assert ad["title"] == "Запись на удобное время" and ad["own"] is True
        assert ad["ai"]["hypothesis"] == "Проверяем идею 1" and ad["ai"]["audience"] == "Аудитория 1"
        assert "Запись на удобное время" in ad["ai"]["prompt"]                                 # промпт идеи виден на карточке

        fakes.claude_text = json.dumps([ad["title"], "Записаться"], ensure_ascii=False)
        assert http.post(f"{base}/creatives/ads/{used['ad_index']}/ai-banner").status_code == 200
        _h, path, body = next(r for r in fakes.requests if r[0] == "queue.fal.run")
        assert path.endswith("gpt-image-2/edit") and b"warm friendly mood" in body             # баннер делается по промпту идеи


def test_new_ideas_replace_unused_ones_but_keep_the_used(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}"
        fakes.claude_text = ideas(["Первая идея про маникюр", "Вторая идея про запись"], (None,))
        briefs = http.post(f"{base}/briefs/generate", json={"count": 2}).json()["briefs"]
        assert all(b["photo_url"] == "" and b["prompt"] == "" for b in briefs)                 # без фото промпта для баннера нет, баннер рисуется с нуля
        http.post(f"{base}/briefs/{briefs[0]['id']}/use")
        fakes.claude_text = ideas(["Третья идея про мастеров", "Четвёртая идея про время"], (None,))
        after = http.post(f"{base}/briefs/generate", json={"count": 2}).json()["briefs"]
        assert [b["title"] for b in after if b["status"] == "used"] == ["Первая идея про маникюр"]
        assert {b["title"] for b in after if b["status"] == "idea"} == {"Третья идея про мастеров", "Четвёртая идея про время"}


def test_bad_ideas_are_dropped_and_nothing_is_charged_when_nothing_is_left(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}"
        bad = json.dumps([{"title": "Лучший маникюр в городе!", "text": "Скидка 50% всем", "angle": "promo", "look": 0},
                          {"title": "Маникюр № 1 в Москве", "text": "Запишитесь", "angle": "booking", "look": 1}], ensure_ascii=False)
        fakes.claude_text = bad
        r = http.post(f"{base}/briefs/generate", json={"count": 3})
        assert r.status_code == 502 and "не предложил" in r.json()["detail"]["message"]
        assert http.get(f"{base}/creatives").json()["spend"]["spent_usd"] == 0                # за неудачный запрос не берём
        fakes.claude_text = bad.replace("Лучший маникюр в городе!", "Маникюр на удобное время").replace("Скидка 50% всем", "Запишитесь онлайн")
        ok = http.post(f"{base}/briefs/generate", json={"count": 3}).json()["briefs"]
        assert [b["title"] for b in ok] == ["Маникюр на удобное время"]                         # запрещённое отброшено, хорошее осталось


def test_editing_and_dropping_ideas_and_privacy(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}"
        fakes.claude_text = ideas(["Идея про мастеров и запись", "Идея про удобное время"], (None,))
        briefs = http.post(f"{base}/briefs/generate", json={"count": 2}).json()["briefs"]
        bid1 = briefs[0]["id"]
        assert http.put(f"{base}/briefs/{bid1}", json={"title": "Лучший маникюр", "body": "Запишитесь", "prompt": ""}).status_code == 400
        ok = http.put(f"{base}/briefs/{bid1}", json={"title": "Маникюр в удобное время", "body": "Запишитесь онлайн", "prompt": "мой промпт"})
        assert ok.status_code == 200
        after = {b["id"]: b for b in http.get(f"{base}/briefs").json()["briefs"]}
        assert after[bid1]["title"] == "Маникюр в удобное время" and after[bid1]["prompt"] == "мой промпт"
        assert http.delete(f"{base}/briefs/{briefs[1]['id']}").status_code == 200
        assert len(http.get(f"{base}/briefs").json()["briefs"]) == 1
    with TestClient(app) as other:
        other.post("/api/auth/register", json={"email": "q@example.com", "password": "password123"})
        assert other.get(f"{base}/briefs").status_code == 404
        assert other.post(f"{base}/briefs/{bid1}/use").status_code == 404
