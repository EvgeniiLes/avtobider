import json

from fastapi.testclient import TestClient

from app.main import app
from tests.test_ad_editor import Fakes, _draw, setup, upload
from tests.test_creatives import isolated  # noqa: F401  (фикстура с отдельной базой на каждый тест)


def test_a_banner_can_be_animated_with_chosen_or_own_motion(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}/creatives"
        upload(http, f"/api/businesses/{bid}", "work")
        ads = lambda: http.get(f"{base}/ads").json()["ads"]
        assert http.post(f"{base}/ads/0/video", json={"prompt": ""}).status_code == 400                      # баннера ещё нет: видео не из чего делать
        _draw(http, base, fakes, ads()[0])
        assert ads()[0]["ai"]["status"] == "ready"

        fakes.claude_text = json.dumps([{"label": "Блики на ногтях", "prompt": "Soft light glints slowly move across the polished nails."},
                                        {"label": "Рука тянется к инструментам", "prompt": "The hand slowly reaches for the tools, the camera pushes in."}], ensure_ascii=False)
        found = http.post(f"{base}/ads/0/motion-ideas").json()
        assert found["ai"] is True and [i["label"] for i in found["ideas"]] == ["Блики на ногтях", "Рука тянется к инструментам"]

        from app.creative import fal
        seen = {}
        original = fal.start_video

        async def spy(image_url, prompt, key, model, transport=None):
            seen.update(image=image_url, prompt=prompt)
            return await original(image_url, prompt, key, model, transport)

        monkeypatch.setattr(fal, "start_video", spy)
        assert http.post(f"{base}/ads/0/video", json={"prompt": found["ideas"][0]["prompt"]}).json() == {"started": 1}
        assert "glints" in seen["prompt"] and "do not add any new text" in seen["prompt"]                    # идея и страховка для текста и логотипа
        assert seen["image"].startswith("data:image/jpeg;base64,")                                           # видео делается из готового баннера
        videos = [c for c in http.get(base).json()["creatives"] if c["kind"] == "video"]
        assert len(videos) == 1 and videos[0]["title"] == ads()[0]["title"] and videos[0]["body"] == ads()[0]["text"]   # видео привязано к объявлению