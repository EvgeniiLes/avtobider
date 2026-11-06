"""Вкладка «Объявления (тест)»: одно объявление рисуют три модели с одним и тем же заданием, баннеры не подменяют настоящий."""
import json

from fastapi.testclient import TestClient

from app.main import app
from tests.test_creatives import Fakes, isolated, setup  # noqa: F401  (isolated: своя база на каждый тест)


def test_smart_scheme_needs_a_photo_and_a_reference_and_adds_the_art_directors_placement(monkeypatch):
    from tests.test_ad_editor import upload
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=False)
    fakes.keep = 10 ** 8
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}/creatives"
        put = lambda body: http.put(f"{base}/ads/0/settings", json=body)
        assert http.post(f"{base}/ads/0/model-test", json={"scheme": "smart"}).status_code == 400        # без фото умной схеме нечего вписывать
        photo = upload(http, f"/api/businesses/{bid}", "work")
        ref = http.get(f"{base}/ads").json()["styles"]["refs"][0]["id"]
        assert put({"asset_id": photo}).status_code == 200 and put({"ref_id": 0}).status_code == 200
        assert http.post(f"{base}/ads/0/model-test", json={"scheme": "smart"}).status_code == 400        # нет референса
        assert put({"ref_id": ref}).status_code == 200
        fakes.claude_text = json.dumps({"placement": "Keep the car large on the right, headline behind it on the left."})
        r = http.post(f"{base}/ads/0/model-test", json={"scheme": "smart"})
        assert r.status_code == 200 and r.json()["scheme"] == "smart"
        asked = [json.loads(b) for h, p, b in fakes.requests if b.startswith(b"{") and b"image_urls" in b and h == "fal.run"]
        assert any(len(x["image_urls"]) == 2 for x in asked)                                        # арт-директор видит фото и референс вместе
        calls = [json.loads(b) for h, p, b in fakes.requests if h == "queue.fal.run"]
        assert len(calls) == 3 and all("Art director's placement instructions" in c["prompt"] and len(c["image_urls"]) == 2 for c in calls)
        fakes.fal_done = True
        rows = [c for c in http.get(base).json()["creatives"] if c["kind"] == "test_banner"]
        assert len(rows) == 3 and all(c["meta"]["scheme"] == "smart" and "headline behind it" in c["meta"]["placement"] for c in rows)


def test_regular_order_follows_the_three_rules_and_draws_with_nano_banana(monkeypatch):
    from tests.test_ad_editor import upload
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=False)
    fakes.keep = 10 ** 8
    fakes.claude_text = json.dumps({"audience": "клиенты", "scene": "A hero object on a saturated backdrop with calm empty space above for the headline.",
                                    "logo": False, "logo_reason": "нет", "placement": "Keep the subject large on the right."})
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}/creatives"
        put = lambda i, body: http.put(f"{base}/ads/{i}/settings", json=body)
        paths = lambda: [(p, json.loads(b)) for h, p, b in fakes.requests if h == "queue.fal.run"]
        ref = http.get(f"{base}/ads").json()["styles"]["refs"][0]["id"]

        assert put(0, {"ref_id": ref, "no_photo": True}).status_code == 200                      # нет фото, есть референс: сцена и картинка референса
        assert http.post(f"{base}/ads/0/ai-banner").status_code == 200
        path, body = paths()[-1]
        assert path == "/google/nano-banana-2.1/edit" and len(body["image_urls"]) == 1

        photo = upload(http, f"/api/businesses/{bid}", "work")                                    # есть фото и референс: арт-директор вписывает фото
        assert put(1, {"asset_id": photo, "ref_id": ref}).status_code == 200
        assert http.post(f"{base}/ads/1/ai-banner").status_code == 200
        path, body = paths()[-1]
        assert path == "/google/nano-banana-2.1/edit" and len(body["image_urls"]) == 2 and "Art director's placement instructions" in body["prompt"]

        assert put(2, {"ref_id": 0, "no_photo": True}).status_code == 200                        # ничего нет: композиция из библиотеки, из текста
        assert http.post(f"{base}/ads/2/ai-banner").status_code == 200
        path, body = paths()[-1]
        assert path == "/google/nano-banana-2.1" and "image_urls" not in body and "Composition (" in body["prompt"]


def test_free_scheme_draws_without_photo_and_reference_from_a_proven_composition(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=False)
    fakes.keep = 10 ** 8
    fakes.claude_text = json.dumps({"audience": "автовладельцы", "scene": "A hero object on a saturated backdrop with plenty of calm empty space above for the headline.",
                                    "logo": False, "logo_reason": "нет"})
    with TestClient(app) as http:
        bid = setup(http, color_main="#c2185b")
        base = f"/api/businesses/{bid}/creatives"
        r = http.post(f"{base}/ads/0/model-test", json={"scheme": "free"})
        assert r.status_code == 200 and r.json()["scheme"] == "free"
        calls = [(p, json.loads(b)) for h, p, b in fakes.requests if h == "queue.fal.run"]
        assert [p for p, _ in calls] == ["/openai/gpt-image-2", "/openai/gpt-image-2.6/sunburst/text-to-image", "/google/nano-banana-2.1"]   # режим «из текста»
        assert all("image_urls" not in body for _, body in calls)                                                  # ни фото, ни референса на входе
        assert len({body["prompt"] for _, body in calls}) == 1 and "aspect_ratio" in calls[2][1] and "image_size" in calls[0][1]
        prompt = calls[0][1]["prompt"]
        assert "Composition (Предмет-герой)" in prompt and "#c2185b" in prompt and "SECOND attached image" not in prompt   # композиция из библиотеки и цвета бизнеса
        asked = [json.loads(b)["messages"][-1]["content"] for h, p, b in fakes.requests if b.startswith(b"{") and b"messages" in b and h == "fal.run"]
        assert any("Обязательная композиция: Предмет-герой" in a for a in asked)
        fakes.fal_done = True
        rows = [c for c in http.get(base).json()["creatives"] if c["kind"] == "test_banner"]
        assert len(rows) == 3 and all(c["meta"]["scheme"] == "free" and c["meta"]["layout"] == "Предмет-герой" and c["status"] == "draft" for c in rows)


def test_three_models_draw_the_same_ad_and_the_owner_takes_the_best(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=False)
    fakes.keep = 10 ** 8
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}/creatives"
        ad = http.get(f"{base}/ads").json()["ads"][0]
        assert http.post(f"{base}/ads/99/model-test").status_code == 400
        r = http.post(f"{base}/ads/0/model-test")
        assert r.status_code == 200 and r.json()["started"] == 3

        calls = [(p, json.loads(b)) for h, p, b in fakes.requests if h == "queue.fal.run"]
        assert [p for p, _ in calls] == ["/openai/gpt-image-2/edit", "/openai/gpt-image-2.6/sunburst/edit", "/google/nano-banana-2.1/edit"]
        assert len({body["prompt"] for _, body in calls}) == 1                       # задание у всех трёх одно и то же
        assert all(body["image_urls"] for _, body in calls)
        assert "image_size" in calls[0][1] and "image_size" in calls[1][1]          # у GPT размер задаётся так,
        assert calls[2][1]["aspect_ratio"] == "4:3" and "image_size" not in calls[2][1]   # а у Google пропорциями
        assert http.post(f"{base}/ads/0/model-test").status_code == 400             # пока рисуется, второй раз нельзя

        fakes.fal_done = True
        fakes.claude_text = json.dumps([ad["title"], "Узнать подробнее"], ensure_ascii=False)
        rows = [c for c in http.get(base).json()["creatives"] if c["kind"] == "test_banner"]
        assert len(rows) == 3 and all(c["status"] == "draft" and c["url"] for c in rows)
        assert {c["meta"]["label"] for c in rows} == {"GPT Image 2", "GPT Image 2.6", "Nano Banana 2.1"}
        assert all(c["meta"]["run"] == r.json()["run"] for c in rows)

        assert http.get(f"{base}/ads").json()["ads"][0]["ai"]["status"] == "none"   # настоящий баннер объявления не подменён
        nano = next(c for c in rows if c["meta"]["model"].startswith("google/"))
        assert http.post(f"{base}/{nano['id']}/use-test").status_code == 200
        taken = http.get(f"{base}/ads").json()["ads"][0]
        assert taken["ai"]["status"] == "ready" and taken["banner_url"]                # теперь он на объявлении
        assert http.post(f"{base}/{nano['id']}/use-test").status_code == 400          # он уже обычный
