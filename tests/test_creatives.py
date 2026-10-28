import json
import sqlite3
from io import BytesIO

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.creative.texts_ai import parse_variants
from app.deps import client_factory, creative_transport
from app.main import app
from app.routes import auth
from app.security import encrypt_token
from tests.fake_direct import FakeDirect


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("FAL_KEY", raising=False)
    auth._fails.clear()
    yield tmp_path / "test.db"
    app.dependency_overrides.clear()


def png() -> bytes:
    out = BytesIO()
    Image.new("RGB", (900, 900), (120, 80, 200)).save(out, "PNG")
    return out.getvalue()


class Fakes:
    """Claude и генератор картинок: заглушки с памятью."""

    def __init__(self, claude_text=None, fal_done=False, fal_status=200):
        self.claude_text, self.fal_done, self.fal_status = claude_text, fal_done, fal_status
        self.jobs = 0
        self.requests = []
        self.transport = httpx.MockTransport(self.handle)
        app.dependency_overrides[creative_transport] = lambda: self.transport

    def handle(self, request: httpx.Request):
        host, path = request.url.host, request.url.path
        self.requests.append((host, path, request.content[:2000]))
        if host == "api.anthropic.com":
            if self.claude_text is None:
                return httpx.Response(500, json={})
            return httpx.Response(200, json={"content": [{"type": "text", "text": self.claude_text}]})
        if host == "fal.run" and "chat/completions" in path:  # Claude через fal.ai
            if self.claude_text is None:
                return httpx.Response(500, json={})
            return httpx.Response(200, json={"choices": [{"message": {"content": self.claude_text}}]})
        if host == "queue.fal.run":
            if self.fal_status != 200:
                return httpx.Response(self.fal_status, json={})
            self.jobs += 1
            return httpx.Response(200, json={"request_id": f"r{self.jobs}", "status_url": f"https://fal.test/status/{self.jobs}",
                                             "response_url": f"https://fal.test/result/{self.jobs}"})
        if host == "fal.test" and "/status/" in path:
            return httpx.Response(200, json={"status": "COMPLETED" if self.fal_done else "IN_PROGRESS"})
        if host == "fal.test" and "/result/" in path:
            video = self.jobs and "video" in self.requests[0][2].decode(errors="ignore")
            return httpx.Response(200, json={"images": [{"url": "https://files.test/img.png"}]})
        if host == "files.test":
            return httpx.Response(200, content=png())
        return httpx.Response(404)


def setup(http, **business):
    http.post("/api/auth/register", json={"email": "o@example.com", "password": "password123"})
    body = {"name": "Лотос", "kind": "Маникюрный салон", "city": "Москва", "weekly_budget": 20000, "target_cpa": 800,
            "description": "Маникюр и педикюр в центре города. Работаем ежедневно.", "site_url": "salon.ru", "mode": "auto"}
    body.update(business)
    return http.post("/api/businesses", json=body).json()["id"]


GOOD = json.dumps([
    {"title": "Маникюр в центре Москвы", "text": "Запишитесь онлайн на удобное время", "angle": "запись"},
    {"title": "Педикюр рядом с метро", "text": "Салон «Лотос» работает ежедневно", "angle": "расположение"},
    {"title": "Лучший салон города", "text": "Мы самые лучшие", "angle": "плохой"},          # превосходная степень
    {"title": "Очень длинный заголовок " * 5, "text": "ок", "angle": "длинный"},              # обрежется и всё равно проверится
    {"title": "Скидка 20% на маникюр", "text": "Акция только сейчас", "angle": "выдумка"},    # выдуманная скидка
    {"title": "Запись!", "text": "Звоните", "angle": "восклицание"},
])


# ---------- разбор ответа модели ----------

def test_ai_answer_is_filtered_by_rules_not_trust():
    kept = parse_variants("Вот варианты:\n" + GOOD + "\nУдачи!")
    assert [v["title"] for v in kept] == ["Маникюр в центре Москвы", "Педикюр рядом с метро"]
    assert parse_variants("не json") == [] and parse_variants("[{]") == []


# ---------- тексты ----------

def test_texts_by_template_without_ai_key():
    with TestClient(app) as http:
        bid = setup(http)
        r = http.post(f"/api/businesses/{bid}/creatives/texts/generate", json={"count": 4}).json()
        assert r["added"] == 4 and r["used_ai"] is False and "по шаблону" in r["message"]
        data = http.get(f"/api/businesses/{bid}/creatives").json()
        assert data["caps"] == {"text_ai": False, "image_ai": False, "video_ai": False}
        assert {c["source"] for c in data["creatives"]} == {"template"} and len(data["creatives"]) == 4
        again = http.post(f"/api/businesses/{bid}/creatives/texts/generate", json={"count": 4}).json()
        assert again["added"] == 2  # шаблонов всего шесть: повторов нет


def test_texts_by_claude_when_key_is_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fakes = Fakes(claude_text=GOOD)
    with TestClient(app) as http:
        bid = setup(http)
        r = http.post(f"/api/businesses/{bid}/creatives/texts/generate", json={"count": 4, "hint": "подчеркнуть запись онлайн"}).json()
        assert r["used_ai"] is True and r["ai"] == 2 and r["added"] == 4 and "с помощью ИИ" in r["message"]
        creatives = http.get(f"/api/businesses/{bid}/creatives").json()["creatives"]
        assert sorted(c["source"] for c in creatives) == ["claude", "claude", "template", "template"]
        host, path, body = fakes.requests[0]
        assert host == "api.anthropic.com" and "подчеркнуть запись онлайн".encode() in body and b"sk-test" not in body
        assert http.get(f"/api/businesses/{bid}/creatives").json()["spend"]["spent_usd"] == 0.01


def test_texts_fall_back_to_claude_through_fal(monkeypatch):
    """Anthropic не отвечает с российских серверов: тексты должны прийти через fal.ai."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("FAL_KEY", "fal-test")
    real = Fakes.handle

    def anthropic_blocked(self, request):
        if request.url.host == "api.anthropic.com":
            self.requests.append((request.url.host, request.url.path, b""))
            return httpx.Response(403, json={"error": {"message": "Request not allowed"}})
        return real(self, request)

    monkeypatch.setattr(Fakes, "handle", anthropic_blocked)
    fakes = Fakes(claude_text=GOOD)
    with TestClient(app) as http:
        bid = setup(http)
        r = http.post(f"/api/businesses/{bid}/creatives/texts/generate", json={"count": 2}).json()
        assert r["ai"] == 2 and "с помощью ИИ" in r["message"]
        assert [h for h, _, _ in fakes.requests] == ["api.anthropic.com", "fal.run"]


def test_thinking_blocks_and_big_token_budget(monkeypatch):
    """Модель может «думать» перед ответом: блоки размышления пропускаем, а запас токенов делаем большим."""
    import asyncio
    from app.creative.texts_ai import generate_texts
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"content": [
            {"type": "thinking", "thinking": "", "signature": "x"},
            {"type": "text", "text": GOOD},
        ]})

    biz = {"name": "Лотос", "kind": "Маникюр", "city": "Москва", "description": "", "tone": ""}
    got = asyncio.run(generate_texts(biz, 4, "k", "claude-sonnet-5-5", "", httpx.MockTransport(handler)))
    assert len(got) == 2 and seen["body"]["max_tokens"] >= 4000 and seen["body"]["model"] == "claude-sonnet-5-5"


def test_claude_down_falls_back_to_template(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    Fakes(claude_text=None)
    with TestClient(app) as http:
        bid = setup(http)
        r = http.post(f"/api/businesses/{bid}/creatives/texts/generate", json={"count": 3}).json()
        assert r["added"] == 3 and r["ai"] == 0 and "ИИ не ответил" in r["message"]


def test_edit_approve_and_validation():
    with TestClient(app) as http:
        bid = setup(http)
        http.post(f"/api/businesses/{bid}/creatives/texts/generate", json={"count": 2})
        first = http.get(f"/api/businesses/{bid}/creatives").json()["creatives"][0]
        ok = http.put(f"/api/businesses/{bid}/creatives/{first['id']}", json={"title": "Мой заголовок", "body": "Мой текст", "status": "approved"})
        assert ok.status_code == 200 and ok.json()["status"] == "approved" and ok.json()["title"] == "Мой заголовок"
        too_long = http.put(f"/api/businesses/{bid}/creatives/{first['id']}", json={"title": "а" * 70})
        assert too_long.status_code == 400 and "длиннее 56" in too_long.json()["detail"]["message"]
        added = http.post(f"/api/businesses/{bid}/creatives/texts", json={"title": "Свой вариант", "body": "Звоните и записывайтесь"})
        assert added.status_code == 200 and added.json()["source"] == "user"
        assert http.post(f"/api/businesses/{bid}/creatives/texts", json={"title": "", "body": ""}).status_code == 400
        assert http.delete(f"/api/businesses/{bid}/creatives/{first['id']}").status_code == 200
        assert http.put(f"/api/businesses/{bid}/creatives/{first['id']}", json={"status": "draft"}).status_code == 404


def test_users_do_not_see_each_others_creatives():
    with TestClient(app) as alice, TestClient(app) as bob:
        alice.post("/api/auth/register", json={"email": "a@example.com", "password": "password123"})
        bob.post("/api/auth/register", json={"email": "b@example.com", "password": "password123"})
        bid = alice.post("/api/businesses", json={"name": "x", "kind": "y"}).json()["id"]
        alice.post(f"/api/businesses/{bid}/creatives/texts/generate", json={"count": 2})
        cid = alice.get(f"/api/businesses/{bid}/creatives").json()["creatives"][0]["id"]
        assert bob.get(f"/api/businesses/{bid}/creatives").status_code == 404
        assert bob.put(f"/api/businesses/{bid}/creatives/{cid}", json={"status": "approved"}).status_code == 404
        assert bob.delete(f"/api/businesses/{bid}/creatives/{cid}").status_code == 404
        assert bob.post(f"/api/businesses/{bid}/creatives/images/generate", json={}).status_code == 404


# ---------- картинки и видео ----------

def test_images_need_a_key():
    with TestClient(app) as http:
        bid = setup(http)
        r = http.post(f"/api/businesses/{bid}/creatives/images/generate", json={})
        assert r.status_code == 503 and "пока не подключено" in r.json()["detail"]["message"]


def test_image_is_queued_then_collected_and_stored(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=False)
    with TestClient(app) as http:
        bid = setup(http)
        assert http.post(f"/api/businesses/{bid}/creatives/images/generate", json={"count": 2, "idea": "светлый интерьер"}).json() == {"started": 2}
        host, path, body = fakes.requests[0]
        assert host == "queue.fal.run" and b"\\u0441\\u0432\\u0435\\u0442\\u043b\\u044b\\u0439" in body or "светлый интерьер".encode() in body
        pending = http.get(f"/api/businesses/{bid}/creatives").json()["creatives"]
        assert [c["status"] for c in pending] == ["working", "working"]

        fakes.fal_done = True
        done = http.get(f"/api/businesses/{bid}/creatives").json()["creatives"]
        assert [c["status"] for c in done] == ["draft", "draft"] and all(c["url"] for c in done)
        picture = http.get(done[0]["url"])
        assert picture.status_code == 200 and Image.open(BytesIO(picture.content)).size == (900, 900)
        assert http.get(f"/api/businesses/{bid}/creatives").json()["spend"]["spent_usd"] == 0.16


def test_fal_payloads_follow_each_models_schema():
    from app.creative import fal
    nb = fal.image_payload("fal-ai/nano-banana-2", "п")
    assert nb["aspect_ratio"] == "4:3" and "image_size" not in nb
    assert "image_size" in fal.image_payload("fal-ai/flux-pro/v1.1", "п")
    kling = fal.video_payload("fal-ai/kling-video/v3/pro/image-to-video", "u", "п")
    assert kling["start_image_url"] == "u" and kling["generate_audio"] is False and "image_url" not in kling
    assert fal.video_payload("xai/grok-imagine-video/image-to-video", "u", "п")["duration"] == 6
    assert fal.video_payload("fal-ai/kling-video/v2.1/standard/image-to-video", "u", "п")["image_url"] == "u"


def test_generation_spend_limit(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    monkeypatch.setenv("GEN_LIMIT_USD", "0.20")
    Fakes()
    with TestClient(app) as http:
        bid = setup(http)
        assert http.post(f"/api/businesses/{bid}/creatives/images/generate", json={"count": 2}).status_code == 200
        r = http.post(f"/api/businesses/{bid}/creatives/images/generate", json={"count": 1})
        assert r.status_code == 400 and "Лимит расходов" in r.json()["detail"]["message"]


def test_fal_errors_are_plain(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-bad")
    Fakes(fal_status=403)
    with TestClient(app) as http:
        bid = setup(http)
        r = http.post(f"/api/businesses/{bid}/creatives/images/generate", json={})
        assert r.status_code == 502 and "FAL_KEY" in r.json()["detail"]["message"]
        assert http.get(f"/api/businesses/{bid}/creatives").json()["creatives"] == []


def test_ready_ads_pair_text_with_banner():
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}/creatives"
        data = http.get(f"{base}/ads").json()
        assert data["count"] == 6 and len(data["ads"]) == 6 and data["has_images"] is False
        assert all(a["banner_url"] == "" and not a["own"] for a in data["ads"])  # без картинок и своих текстов
        assert http.get(f"{base}/ads/0/banner.jpg").status_code == 404

        text = http.post(f"{base}/texts", json={"title": "Маникюр в центре Москвы", "body": "Запишитесь онлайн на удобное время"}).json()
        http.put(f"{base}/{text['id']}", json={"status": "approved"})
        http.post(f"/api/businesses/{bid}/assets", data={"kind": "photo", "tag": ""}, files={"file": ("a.png", png(), "image/png")})
        data = http.get(f"{base}/ads").json()
        first = data["ads"][0]
        assert first["title"] == "Маникюр в центре Москвы" and first["own"] is True and first["creative_id"] == text["id"]
        assert data["has_images"] is True and first["banner_url"].endswith("/ads/0/banner.jpg")
        banner = http.get(first["banner_url"])
        assert banner.status_code == 200 and banner.headers["content-type"] == "image/jpeg"
        assert Image.open(BytesIO(banner.content)).size[0] > 300
        assert http.get(f"{base}/ads/99/banner.jpg").status_code == 404
    with TestClient(app) as other:
        other.post("/api/auth/register", json={"email": "x@example.com", "password": "password123"})
        assert other.get(f"{base}/ads").status_code == 404  # чужие объявления не видны


def test_video_from_ready_image_only(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    with TestClient(app) as http:
        bid = setup(http)
        http.post(f"/api/businesses/{bid}/creatives/images/generate", json={"count": 1})
        image = http.get(f"/api/businesses/{bid}/creatives").json()["creatives"][0]
        r = http.post(f"/api/businesses/{bid}/creatives/videos/generate", json={"image_creative_id": image["id"]})
        assert r.status_code == 200
        host, path, body = fakes.requests[-1]
        assert host == "queue.fal.run" and b"data:image/jpeg;base64," in body  # картинка передаётся самим файлом, наружу ссылок не даём
        text = http.post(f"/api/businesses/{bid}/creatives/texts", json={"title": "Т", "body": "Т"}).json()
        assert http.post(f"/api/businesses/{bid}/creatives/videos/generate", json={"image_creative_id": text["id"]}).status_code == 400


# ---------- использование в запуске ----------

def test_approved_texts_go_into_the_launch_plan(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http)
        app.dependency_overrides[client_factory] = fake.factory()
        conn = sqlite3.connect(isolated)
        conn.execute("INSERT INTO ad_accounts (user_id, title, token_enc) VALUES (1, 'fake-login', ?)", (encrypt_token("tok", "test-secret-key"),))
        conn.commit()
        conn.close()
        a = http.post(f"/api/businesses/{bid}/creatives/texts", json={"title": "Мой первый заголовок", "body": "Мой первый текст"}).json()
        b = http.post(f"/api/businesses/{bid}/creatives/texts", json={"title": "Мой второй заголовок", "body": "Мой второй текст"}).json()
        rejected = http.post(f"/api/businesses/{bid}/creatives/texts", json={"title": "Отклонённый вариант", "body": "Не берём"}).json()
        for c in (a, b):
            http.put(f"/api/businesses/{bid}/creatives/{c['id']}", json={"status": "approved"})
        http.put(f"/api/businesses/{bid}/creatives/{rejected['id']}", json={"status": "rejected"})
        plan = http.get(f"/api/businesses/{bid}/launch/preview").json()
        titles = [v["title"] for v in plan["variants"]]
        assert titles[:2] == ["Мой первый заголовок", "Мой второй заголовок"] and "Отклонённый вариант" not in titles
        assert plan["own_count"] == 2 and len(titles) == plan["count"] == len(set(titles))
