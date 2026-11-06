"""Вкладка «Объявления (тест)»: одно объявление рисуют три модели с одним и тем же заданием, баннеры не подменяют настоящий."""
import json

from fastapi.testclient import TestClient

from app.main import app
from tests.test_creatives import Fakes, isolated, setup  # noqa: F401  (isolated: своя база на каждый тест)


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
