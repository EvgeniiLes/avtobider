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
        assert path.endswith("nano-banana-2.1/edit") and b"warm friendly mood" in body             # баннер делается по промпту идеи


def test_reference_catalog_is_complete():
    from app.creative import refs
    assert sorted(refs.REFS) == list(range(1, 101))
    assert all(niche in refs.NICHES and note for niche, note in refs.REFS.values())
    for n in refs.REFS:
        data = refs.load(n)
        assert data and data[:2] == b"\xff\xd8", n                                     # у каждого референса есть файл (JPEG)
    assert refs.load(999) is None and refs.note(None) == "" and "17" not in refs.catalog_text().split("\n")[0]
    assert len(refs.catalog_text().split("\n")) == 100


def reference_ideas():
    return json.dumps([
        {"hypothesis": "Проверяем крупное слово", "audience": "Занятые", "angle": "booking", "title": "Маникюр на удобное время", "text": "Запишитесь онлайн",
         "photo": 1, "look": 0, "mood": "calm", "ref": 56, "direction": "huge first word behind the subject, deep navy background"},
        {"hypothesis": "Проверяем лёгкость", "audience": "Мамы", "angle": "booking", "title": "Запись онлайн без звонков", "text": "Запишитесь онлайн",
         "photo": 1, "look": 1, "mood": "soft", "ref": 56, "direction": "same ref again"},          # повтор референса у другой идеи
        {"hypothesis": "Без референса", "audience": "Гости", "angle": "booking", "title": "Маникюр в центре города", "text": "Запишитесь онлайн",
         "photo": 1, "look": 2, "mood": "bright", "ref": 9999, "direction": "x"}], ensure_ascii=False)      # такого референса нет


def test_ideas_pick_distinct_references_and_the_banner_gets_the_reference_image(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    fakes.keep = 10 ** 8
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}"
        upload(http, base, "work")
        fakes.claude_text = reference_ideas()
        briefs = http.post(f"{base}/briefs/generate", json={"count": 3}).json()["briefs"]
        assert len(briefs) == 3
        assert briefs[0]["inspiration"] and "SECOND attached image" in briefs[0]["prompt"] and "huge first word" in briefs[0]["prompt"]
        assert briefs[1]["inspiration"] == "" and "SECOND attached image" not in briefs[1]["prompt"]       # повтор референса отброшен
        assert briefs[2]["inspiration"] == "" and "SECOND attached image" not in briefs[2]["prompt"]       # несуществующий тоже
        spent_before = http.get(f"{base}/creatives").json()["spend"]["spent_usd"]
        used = http.post(f"{base}/briefs/{briefs[0]['id']}/use").json()
        fakes.claude_text = json.dumps([briefs[0]["title"], "Записаться"], ensure_ascii=False)
        assert http.post(f"{base}/creatives/ads/{used['ad_index']}/ai-banner").status_code == 200
        _h, path, body = next(r for r in fakes.requests if r[0] == "queue.fal.run")
        assert path.endswith("nano-banana-2.1/edit") and body.count(b"data:image/jpeg;base64") == 2           # фото владельца и референс
        spent = http.get(f"{base}/creatives").json()["spend"]["spent_usd"]
        assert round(spent - spent_before, 2) == 0.12                                                      # 7 центов баннер, 2 за вторую картинку и 3 арт-директору
        ad = http.get(f"{base}/creatives/ads").json()["ads"][used["ad_index"]]
        assert ad["ai"]["inspiration"] == briefs[0]["inspiration"]


def test_direction_loses_contacts_and_logos():
    from app.creative.strategist import clean_direction
    text = ("Pet paw on orange background, huge title. Contact information and website at the bottom. "
            "Add the brand logo in the corner. Soft cinematic light.")
    assert clean_direction(text) == "Pet paw on orange background, huge title. Soft cinematic light."
    assert clean_direction("Call us on WhatsApp. Phone number below.") == ""


def test_no_photo_means_no_reference(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    with TestClient(app) as http:
        bid = setup(http)
        fakes.claude_text = reference_ideas()
        briefs = http.post(f"/api/businesses/{bid}/briefs/generate", json={"count": 3}).json()["briefs"]
        assert all(b["inspiration"] == "" and b["prompt"] == "" for b in briefs)                           # без фото референс не нужен


def test_new_ideas_replace_unused_ones_but_keep_the_used(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}"
        fakes.claude_text = ideas(["Первая идея про маникюр", "Вторая идея про запись"], (None,))
        briefs = http.post(f"{base}/briefs/generate", json={"count": 2}).json()["briefs"]
        assert all(b["photo_url"] == "" and b["prompt"] == "" for b in briefs)                 # без фото промпта для баннера нет, баннер рисуется с нуля
        assert all(b["status"] == "used" for b in briefs)                                      # отобранные идеи сразу взяты в рекламу
        http.post(f"{base}/briefs/{briefs[1]['id']}/unuse")                                    # владелец снял вторую
        fakes.claude_text = ideas(["Третья идея про мастеров", "Четвёртая идея про время"], (None,))
        after = http.post(f"{base}/briefs/generate", json={"count": 2}).json()["briefs"]
        assert [b["title"] for b in after if b["status"] == "used"] == ["Первая идея про маникюр", "Третья идея про мастеров", "Четвёртая идея про время"]
        assert not [b for b in after if b["status"] == "idea"]                                 # снятая прежняя заменена новыми


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
        assert http.put(f"{base}/briefs/{bid1}", json={"title": "Маникюр в удобное время", "body": "Запишитесь онлайн", "prompt": ""}).status_code == 400   # идея уже в рекламе: правится на карточке
        http.post(f"{base}/briefs/{bid1}/unuse")
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
