"""На баннере короткий текст (hook), а не весь заголовок объявления; без hook остаётся заголовок."""
import json

from fastapi.testclient import TestClient

from app.main import app
from tests.test_creatives import Fakes, isolated, setup  # noqa: F401


def test_banner_shows_the_short_hook_while_the_ad_keeps_the_full_title(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=False)
    fakes.keep = 10 ** 8
    fakes.claude_text = json.dumps({"audience": "клиенты", "scene": "A hero object on a saturated backdrop, calm empty space above for text.", "logo": False, "logo_reason": "нет"})
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}/creatives"
        assert http.post(f"{base}/texts", json={"title": "Маникюр в центре Москвы", "body": "Запишитесь онлайн на удобное время", "hook": "Маникюр в центре!"}).status_code == 400   # «!» нельзя
        made = http.post(f"{base}/texts", json={"title": "Маникюр в центре Москвы", "body": "Запишитесь онлайн на удобное время", "hook": "Маникюр в центре"}).json()
        assert made["hook"] == "Маникюр в центре"
        assert http.put(f"{base}/{made['id']}", json={"status": "approved"}).status_code == 200
        ad = http.get(f"{base}/ads").json()["ads"][0]
        assert ad["title"] == "Маникюр в центре Москвы" and ad["hook"] == "Маникюр в центре"
        assert http.post(f"{base}/ads/0/ai-banner").status_code == 200
        prompt = json.loads([b for h, p, b in fakes.requests if h == "queue.fal.run"][-1])["prompt"]
        assert "headline «Маникюр в центре»" in prompt and "Маникюр в центре Москвы" not in prompt     # на баннер идёт короткий текст

        assert http.put(f"{base}/{made['id']}", json={"hook": ""}).status_code == 200                  # без hook на баннере заголовок
        fakes.fal_done = True
        http.get(base)
        assert http.post(f"{base}/ads/0/ai-banner").status_code == 200
        prompt = json.loads([b for h, p, b in fakes.requests if h == "queue.fal.run"][-1])["prompt"]
        assert "headline «Маникюр в центре Москвы»" in prompt
