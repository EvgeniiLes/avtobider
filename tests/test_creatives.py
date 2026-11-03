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
        self.facts_text = None   # ответ проверяющему фактов на баннере; None: он отвечает тем же, что и все остальные запросы к модели
        self.fail_chat = 0       # столько ближайших запросов к модели закончатся ошибкой (сбой на стороне сервиса)
        self.jobs = 0
        self.image = None  # что отдаёт «генератор»; по умолчанию однотонная картинка
        self.requests = []
        self.keep = 6000   # сколько байт тела запроса помнить; тесты, которым нужны все картинки, ставят больше
        self.transport = httpx.MockTransport(self.handle)
        app.dependency_overrides[creative_transport] = lambda: self.transport

    def handle(self, request: httpx.Request):
        host, path = request.url.host, request.url.path
        self.requests.append((host, path, request.content[:self.keep]))
        if host == "api.anthropic.com":
            if self.claude_text is None:
                return httpx.Response(500, json={})
            return httpx.Response(200, json={"content": [{"type": "text", "text": self.claude_text}]})
        if host == "fal.run" and "chat/completions" in path:  # Claude через fal.ai
            if self.fail_chat > 0:
                self.fail_chat -= 1
                return httpx.Response(500, json={})
            first = (json.loads(request.content).get("messages") or [{}])[0].get("content")
            if self.facts_text is not None and isinstance(first, str) and "фактические ошибки" in first:
                return httpx.Response(200, json={"choices": [{"message": {"content": self.facts_text}}]})
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
            return httpx.Response(200, content=self.image or png())
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
    gpt = fal.image_payload("openai/gpt-image-2", "п")
    assert gpt["image_size"] == "landscape_4_3" and gpt["quality"] == "medium" and "aspect_ratio" not in gpt
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


def test_spend_limit_resets_every_month(monkeypatch):
    from app.config import get_settings
    from app.db import connect
    monkeypatch.setenv("FAL_KEY", "fal-test")
    monkeypatch.setenv("GEN_LIMIT_USD", "0.20")
    Fakes()
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}/creatives"
        conn = connect(get_settings().db_path)
        conn.execute("INSERT INTO generations (business_id, kind, cents, created_at) VALUES (?, 'image', 500, '2020-01-05 10:00:00')", (bid,))
        conn.commit(); conn.close()
        spend = http.get(base).json()["spend"]
        assert spend["spent_usd"] == 0 and spend["total_usd"] == 5.0                   # старый расход в лимит месяца не входит
        assert http.post(f"{base}/images/generate", json={"count": 2}).status_code == 200
        assert http.post(f"{base}/images/generate", json={"count": 1}).status_code == 400     # а в этом месяце лимит всё ещё действует


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
        assert data["count"] == 3 and len(data["ads"]) == 6 and data["has_images"] is False   # в кампанию войдут три, ещё три запасные
        assert [a["reserve"] for a in data["ads"]] == [False] * 3 + [True] * 3 and data["weekly"] > 0
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


def test_dragging_a_spare_ad_into_the_live_places_changes_what_is_launched(isolated):
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}/creatives"
        ads = http.get(f"{base}/ads").json()["ads"]
        keys = [{"title": a["title"], "text": a["text"]} for a in ads]
        spare = keys[4]                                         # запасное объявление, шаблонный текст без записи
        new = [spare] + keys[:4] + keys[5:]                     # перетащили на первое место
        assert http.put(f"{base}/ads/order", json={"ads": new}).status_code == 200
        after = http.get(f"{base}/ads").json()["ads"]
        assert [{"title": a["title"], "text": a["text"]} for a in after] == new
        assert after[0]["reserve"] is False and after[2]["reserve"] is False and after[3]["reserve"] is True and sum(a["reserve"] for a in after) == 3
        assert http.put(f"{base}/ads/order", json={"ads": new}).status_code == 200   # повтор ничего не ломает
        assert [a["title"] for a in http.get(f"{base}/ads").json()["ads"]] == [k["title"] for k in new]
        from app.db import connect
        from app.direct.launch import choose_variants, text_candidates
        conn = connect(isolated)
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        launched, _own = choose_variants(biz, text_candidates(conn, biz), 3)   # в кампанию уходит то же, что показано как «пойдут в рекламу»
        assert [v["title"] for v in launched] == [k["title"] for k in new[:3]]
        conn.close()


def test_a_template_kept_for_its_place_does_not_push_out_ai_drafts_with_the_same_angle(isolated):
    from app.db import connect
    from app.direct.launch import text_candidates
    with TestClient(app) as http:
        bid = setup(http)
        conn = connect(isolated)
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        conn.execute("INSERT INTO creatives (business_id, kind, title, body, angle, source, status, position) VALUES (?, 'text', 'Маникюр в центре Москвы', 'Запишитесь онлайн на удобное время', 'запись', 'template', 'approved', 0)", (bid,))
        conn.execute("INSERT INTO creatives (business_id, kind, title, body, angle, source, status) VALUES (?, 'text', 'Педикюр рядом с метро', 'Салон «Лотос» работает ежедневно', 'запись', 'claude', 'draft')", (bid,))
        conn.commit()
        titles = [c["title"] for c in text_candidates(conn, biz)]
        conn.close()
        assert titles == ["Маникюр в центре Москвы", "Педикюр рядом с метро"]   # черновик с тем же углом остался


def test_the_owner_can_take_fewer_or_more_ads_into_the_campaign_than_the_budget_advises(isolated):
    from app.db import connect
    from app.direct.launch import choose_variants, plan_launch, start_split, text_candidates
    with TestClient(app) as http:
        bid = setup(http, districts="Хамовники")
        base = f"/api/businesses/{bid}/creatives"
        data = http.get(f"{base}/ads").json()
        assert data["count"] == 3 and data["advised"] == 3
        assert http.put(f"{base}/ads/live", json={"count": 5}).status_code == 200
        data = http.get(f"{base}/ads").json()
        assert data["count"] == 5 and data["advised"] == 3 and [a["reserve"] for a in data["ads"]] == [False] * 5 + [True]
        assert http.put(f"{base}/ads/live", json={"count": 1}).status_code == 200
        assert [a["reserve"] for a in http.get(f"{base}/ads").json()["ads"]] == [False] + [True] * 5
        assert http.put(f"{base}/ads/live", json={"count": 0}).status_code == 422 and http.put(f"{base}/ads/live", json={"count": 9}).status_code == 422
        conn = connect(isolated)
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        assert start_split(biz)[1] == 1                                       # в кампанию уйдёт столько, сколько выбрано
        plan = plan_launch(biz, 213, custom=text_candidates(conn, biz))
        assert plan.count == 1 and len(plan.variants) == 1
        http.put(f"{base}/ads/live", json={"count": 5})
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        plan = plan_launch(biz, 213, custom=text_candidates(conn, biz))
        assert plan.count == 5 and any("бюджету подходит 3" in w for w in plan.warnings)
        conn.close()
        http.put(f"{base}/ads/live", json={"count": None})                  # вернуть число по бюджету
        assert http.get(f"{base}/ads").json()["count"] == 3
    with TestClient(app) as other:
        other.post("/api/auth/register", json={"email": "x@example.com", "password": "password123"})
        assert other.put(f"{base}/ads/live", json={"count": 2}).status_code == 404


def test_the_order_of_ads_is_checked_and_private():
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}/creatives"
        ads = http.get(f"{base}/ads").json()["ads"]
        keys = [{"title": a["title"], "text": a["text"]} for a in ads]
        assert http.put(f"{base}/ads/order", json={"ads": keys + [{"title": "Чужое", "text": "Нет такого"}]}).status_code == 400
        assert http.put(f"{base}/ads/order", json={"ads": [keys[0], keys[0]]}).status_code == 400
        assert http.put(f"{base}/ads/order", json={"ads": keys[::-1][:2]}).status_code == 200   # неполный список: остальные идут следом
        titles = [a["title"] for a in http.get(f"{base}/ads").json()["ads"]]
        assert titles[:2] == [keys[5]["title"], keys[4]["title"]] and len(titles) == 6
    with TestClient(app) as other:
        other.post("/api/auth/register", json={"email": "x@example.com", "password": "password123"})
        assert other.put(f"{base}/ads/order", json={"ads": []}).status_code == 404


def _ai_ad(http, bid):
    ad = http.get(f"/api/businesses/{bid}/creatives/ads").json()["ads"][4]
    assert ad["style"] == "ai"
    return ad


def test_ai_banner_is_ordered_checked_and_used(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=False)
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}/creatives"
        ad = _ai_ad(http, bid)
        assert ad["ai"]["status"] == "none" and ad["banner_url"] == ""
        assert http.post(f"{base}/ads/99/ai-banner").status_code == 400     # такого объявления нет
        assert http.post(f"{base}/ads/4/ai-banner").json() == {"started": True}
        assert http.post(f"{base}/ads/4/ai-banner").status_code == 400      # второй раз, пока рисуется, нельзя
        host, path, body = next(r for r in fakes.requests if r[0] == "queue.fal.run")   # перед рисованием арт-директор придумывает сцену
        assert "gpt-image-2" in path
        assert b"Visual direction" in body and b"ONLY a style inspiration" in body      # у объявления есть приём (свой или соседней ниши): референс уходит картинкой, размер и качество стоят после неё и в запомненную часть тела не входят
        http.get(base)
        assert _ai_ad(http, bid)["ai"]["status"] == "working"

        fakes.fal_done = True
        fakes.claude_text = json.dumps([ad["title"], "Лотос", "ЗАПИСАТЬСЯ"], ensure_ascii=False)  # регистр и кавычки не важны
        http.get(base)
        ready = _ai_ad(http, bid)
        assert ready["ai"]["status"] == "ready" and ready["banner_url"].endswith("/ads/4/banner.jpg")
        banner = http.get(ready["banner_url"])
        assert banner.status_code == 200 and banner.headers["content-type"] == "image/jpeg"
        assert http.get(f"{base}/ads/4/banner.jpg").status_code == 200
        assert http.get(base).json()["spend"]["spent_usd"] == 0.09   # баннер 7 центов и референс картинкой 2 цента


def test_banner_is_made_from_the_owners_photo_with_an_editable_prompt(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}"
        http.post(f"{base}/assets", data={"kind": "photo", "tag": "work"}, files={"file": ("a.png", png(), "image/png")})
        ad = http.get(f"{base}/creatives/ads").json()["ads"][0]
        assert ad["ai"]["prompt"] and ad["title"] in ad["ai"]["prompt"] and "unaltered" in ad["ai"]["prompt"]     # промпт виден до заказа
        fakes.claude_text = json.dumps([ad["title"], "Записаться"], ensure_ascii=False)
        assert http.post(f"{base}/creatives/ads/0/ai-banner").status_code == 200
        host, path, body = next(r for r in fakes.requests if r[0] == "queue.fal.run")
        assert path.endswith("gpt-image-2/edit") and b"image_urls" in body and b"data:image/jpeg;base64" in body    # фото ушло модели
        assert not any(r[0] == "fal.run" and b"\xd0\xa1\xd1\x86\xd0\xb5\xd0\xbd\xd0\xb0" in r[2] for r in fakes.requests)   # арт-директор не нужен
        http.get(f"{base}/creatives")
        ready = http.get(f"{base}/creatives/ads").json()["ads"][0]
        assert ready["ai"]["status"] == "ready" and "по мотивам референса" in ready["ai"]["scene"] and ready["photo_label"] == "Наши работы"   # подачу выбрали сами: приём из ниши
        mine = "Make a clean light banner from the attached photo. Headline «" + ad["title"] + "», button «Записаться». No other text."
        assert http.post(f"{base}/creatives/ads/0/ai-banner", json={"prompt": mine}).status_code == 200
        assert b"Make a clean light banner" in [b for h, p, b in fakes.requests if h == "queue.fal.run"][-1]       # свой промпт ушёл как есть
        assert http.post(f"{base}/creatives/ads/1/ai-banner", json={"prompt": "коротко"}).status_code == 400


def test_banner_history_and_restore(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}/creatives"
        ad = _ai_ad(http, bid)
        fakes.claude_text = json.dumps([ad["title"], "ЗАПИСАТЬСЯ"], ensure_ascii=False)
        assert http.post(f"{base}/ads/4/ai-banner").status_code == 200
        http.get(base)
        history = [c for c in http.get(base).json()["creatives"] if c["kind"] == "ai_banner"]
        assert len(history) == 1 and history[0]["status"] == "draft" and history[0]["url"]
        assert history[0]["meta"]["scene"]                                          # сюжет виден в истории
        first = history[0]["id"]
        fakes.claude_text = json.dumps(["не то", "совсем не то"], ensure_ascii=False)  # второй заказ с ошибками в тексте: не принят
        assert http.post(f"{base}/ads/4/ai-banner").status_code == 200
        http.get(base)
        assert _ai_ad(http, bid)["ai"]["status"] == "failed"                        # на карточке видна ошибка, хорошего баннера там нет
        spent = http.get(base).json()["spend"]["spent_usd"]
        assert http.post(f"{base}/{first}/restore").json() == {"restored": True}    # вернули прошлый
        assert _ai_ad(http, bid)["ai"]["status"] == "ready"
        assert http.get(base).json()["spend"]["spent_usd"] == spent                 # возврат бесплатный
        failed = next(c for c in http.get(base).json()["creatives"] if c["status"] == "failed")
        assert http.post(f"{base}/{failed['id']}/restore").status_code == 400       # неудачный вернуть нельзя
        banners = [c for c in http.get(base).json()["creatives"] if c["kind"] == "ai_banner"]
        assert len(banners) == 2                                                    # возврат не плодит копий: в истории прежние два заказа
        assert [c["current"] for c in banners if c["id"] == first] == [True] and not any(c["current"] for c in banners if c["status"] == "failed")
    with TestClient(app) as other:
        other.post("/api/auth/register", json={"email": "z@example.com", "password": "password123"})
        assert other.post(f"{base}/{first}/restore").status_code == 404             # чужое вернуть нельзя


def test_restoring_an_older_version_only_moves_the_marker(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}/creatives"
        ad = _ai_ad(http, bid)
        fakes.claude_text = json.dumps([ad["title"], "ЗАПИСАТЬСЯ"], ensure_ascii=False)
        banners = lambda: sorted((c for c in http.get(base).json()["creatives"] if c["kind"] == "ai_banner"), key=lambda c: c["id"])
        for color in ((200, 40, 40), (40, 40, 200)):
            out = BytesIO(); Image.new("RGB", (900, 900), color).save(out, "PNG"); fakes.image = out.getvalue()
            assert http.post(f"{base}/ads/4/ai-banner").status_code == 200
            http.get(base)
        old, new = banners()
        assert [old["current"], new["current"]] == [False, True]
        shown_new = http.get(f"{base}/ads/4/banner.jpg").content

        assert http.post(f"{base}/{old['id']}/restore").json() == {"restored": True}
        after = banners()
        assert [b["id"] for b in after] == [old["id"], new["id"]] and [b["current"] for b in after] == [True, False]   # ни копий, ни перестановки
        assert http.get(f"{base}/ads/4/banner.jpg").content != shown_new                       # на объявлении теперь прежняя версия
        assert http.post(f"{base}/{old['id']}/restore").status_code == 200                       # повторное нажатие ничего не ломает
        assert len(banners()) == 2

        assert http.post(f"{base}/ads/4/ai-banner").status_code == 200                           # новый заказ снова становится показываемым
        http.get(base)
        third = banners()
        assert len(third) == 3 and [b["current"] for b in third] == [False, False, True]

        fakes.fal_done = False
        assert http.post(f"{base}/ads/4/ai-banner").status_code == 200
        assert http.post(f"{base}/{old['id']}/restore").status_code == 400                     # пока рисуется новая, вернуть нельзя


def test_all_ai_banners_are_ordered_once_each_with_different_directions(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=False)
    fakes.keep = 10 ** 7
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}/creatives"
        assert http.post(f"{base}/ads/ai-banners").json() == {"started": 6}        # все шесть объявлений: и кампании, и запасные
        assert http.post(f"{base}/ads/ai-banners").json() == {"started": 0}        # повторно не платим за то, что уже рисуется
        prompts = [b.decode(errors="ignore") for h, _, b in fakes.requests if h == "queue.fal.run"]
        assert len(prompts) == 6 and len({p.split("Visual direction:")[1][:170] for p in prompts}) == 6   # у каждого баннера своё направление
        assert sum("ONLY a style inspiration" in p for p in prompts) == 6                                       # у маникюра в каталоге два приёма, ещё берутся приёмы соседних ниш: у каждого объявления свой референс
        ads = http.get(f"{base}/ads").json()["ads"]
        assert [a["ai"]["status"] for a in ads] == ["working"] * 6 and [a["reserve"] for a in ads] == [False] * 3 + [True] * 3
        assert http.get(base).json()["spend"]["spent_usd"] == 0.54     # шесть баннеров и шесть референсов картинкой


def test_ai_banner_with_typos_is_rejected(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True, claude_text=json.dumps(["Маникюр", "Запистаться", "Лотос"], ensure_ascii=False))
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}/creatives"
        _ai_ad(http, bid)
        http.post(f"{base}/ads/4/ai-banner")
        http.get(base)
        ad = _ai_ad(http, bid)
        assert ad["ai"]["status"] == "failed" and "ошибки в тексте" in ad["ai"]["error"] and ad["banner_url"] == ""
        assert http.get(f"{base}/ads/4/banner.jpg").status_code == 404


def test_extra_words_ignores_short_words_repeats_and_noise_in_expected_texts():
    from app.creative.texts_ai import EXTRA_WORDS_OK, extra_words
    expected = ["Стрижка и укладка за час", "ЗАПИСАТЬСЯ", "от 1500 ₽"]
    assert extra_words(expected, ["СТРИЖКА и УКЛАДКА", "за час", "Записаться", "от 1500 ₽"]) == []   # регистр, знаки и «и/от» не важны
    assert extra_words(expected, ["Запи-сать-ся", "1 500 ₽"]) == []                                  # распознавание разорвало слова: части на месте
    assert extra_words(expected, ["Борода", "борода", "ул. Мира 5"]) == ["борода", "мира"]           # повторы и слова короче трёх букв не считаем
    assert len(extra_words(expected, ["Мужской зал", "Борода 800", "Лотос"])) > EXTRA_WORDS_OK         # список услуг с названием: это уже лишнее


def test_ai_banner_with_extra_small_text_is_rejected_but_a_stray_word_is_not(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}/creatives"
        ad = _ai_ad(http, bid)
        fakes.claude_text = json.dumps([ad["title"], "ЗАПИСАТЬСЯ", "Лотос"], ensure_ascii=False)   # одно постороннее слово: терпим
        http.post(f"{base}/ads/4/ai-banner")
        http.get(base)
        assert _ai_ad(http, bid)["ai"]["status"] == "ready"

        fakes.claude_text = json.dumps([ad["title"], "ЗАПИСАТЬСЯ", "Маникюр 1500", "Педикюр 1800", "Наращивание ресниц"], ensure_ascii=False)
        http.post(f"{base}/ads/4/ai-banner")
        http.get(base)
        bad = _ai_ad(http, bid)
        assert bad["ai"]["status"] == "failed" and "лишний текст" in bad["ai"]["error"]
        assert "педикюр" in bad["ai"]["error"] and "маникюр" not in bad["ai"]["error"]   # слова из заголовка лишними не считаются
        assert http.get(f"{base}/ads/4/banner.jpg").status_code == 200                    # а прежний принятый баннер остаётся на объявлении


def test_the_caption_says_who_advertises_what_and_skips_what_cannot_be_checked():
    from app.creative import fal
    shop = {"name": "Лотос", "kind": "Маникюрный салон", "description": "Маникюр и педикюр.", "allow_promo": 0, "color_main": "", "color_extra": "", "visual_hint": ""}
    assert fal.caption_parts(shop) == ["Лотос", "Маникюрный салон"] and fal.caption_for(shop) == "Лотос · Маникюрный салон"
    assert fal.caption_parts({**shop, "name": "burocart.ru"}) == ["Маникюрный салон"]                    # адрес сайта в подпись нельзя: проверка такие надписи бракует
    assert fal.caption_parts({**shop, "kind": "Карточные игры с ИИ ведущим: мафия, бункер, шпион и другие игры"}) == ["Лотос"]   # длинное не берём
    assert fal.caption_parts({**shop, "name": "burocart.ru", "kind": "x" * 60}) == []

    for prompt in (fal.banner_prompt(shop, "Маникюр", "", "Записаться"), fal.photo_banner_prompt(shop, "Маникюр", "", "Записаться"),
                   fal.reference_banner_prompt(shop, "Маникюр", "", "Записаться", "takes inspiration")):
        assert "Лотос · Маникюрный салон" not in prompt and "bottom 8%" in prompt                           # подпись нейросеть не рисует: низ баннера просто оставляем свободным
        assert "the business name," in prompt                                                              # и название бизнеса ей по-прежнему писать нельзя

    # цена уже названа в заголовке: отдельную плашку не рисуем, а когда в заголовке цены нет, плашка есть
    assert "price badge" not in fal.banner_prompt(shop, "Аудит от 9 000 ₽", "от 9 000 ₽", "Записаться")
    assert "price badge «от 9 000 ₽»" in fal.banner_prompt(shop, "Аудит процессов", "от 9 000 ₽", "Записаться")


def test_people_on_banners_are_natural_and_the_service_itself_rejects_stock_clichés():
    from app.creative import art, fal
    shop = {"name": "Лотос", "kind": "Маникюрный салон", "description": "Маникюр.", "allow_promo": 0, "color_main": "", "color_extra": "", "visual_hint": ""}
    assert "calm and natural" in fal.banner_prompt(shop, "Маникюр", "", "Записаться") and "stock-photo poses" in fal.banner_prompt(shop, "Маникюр", "", "Записаться")
    assert "без театрального удивления и шока" in art.ART_SYSTEM and "стоковые метафоры" in art.ART_SYSTEM                  # сцену арт-директор придумывает без клише
    assert "театральное удивление или шок" in art.REVIEW_SYSTEM and "стоковая метафора" in art.REVIEW_SYSTEM            # а проверка готового баннера такое бракует


def test_the_service_draws_the_caption_on_every_finished_banner_even_with_a_custom_prompt(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    out = BytesIO()
    Image.new("RGB", (1200, 900), (120, 80, 200)).save(out, "PNG")
    fakes.image = out.getvalue()                                                  # «нарисованный» баннер 4:3, однотонный
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}/creatives"
        ad = _ai_ad(http, bid)
        fakes.claude_text = json.dumps([ad["title"], "ЗАПИСАТЬСЯ"], ensure_ascii=False)

        def bottom_and_top():
            im = Image.open(BytesIO(http.get(f"{base}/ads/4/banner.jpg").content)).convert("L")
            w, h = im.size
            return im.crop((0, int(h * 0.92), w, h)).getextrema(), im.crop((0, 0, w, int(h * 0.6))).getextrema()

        http.post(f"{base}/ads/4/ai-banner")
        http.get(base)
        assert _ai_ad(http, bid)["ai"]["status"] == "ready"
        bottom, top = bottom_and_top()
        assert bottom[1] > 200 and bottom[0] < 70 and top[1] < 140               # внизу светлые буквы на тёмной полосе, выше картинка не тронута

        own = "Нарисуй спокойный баннер: крупный план рук мастера и короткий заголовок слева."
        assert http.post(f"{base}/ads/4/ai-banner", json={"prompt": own}).status_code == 200   # свой промпт: подпись всё равно ставит сервис
        http.get(base)
        assert _ai_ad(http, bid)["ai"]["status"] == "ready" and bottom_and_top()[0][1] > 200


def test_the_model_is_told_where_ads_are_shown_and_not_to_write_a_city_for_a_nationwide_business(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(claude_text="[]")
    fakes.keep = 10 ** 6
    asked = lambda: [json.loads(b)["messages"][-1]["content"] for h, p, b in fakes.requests if "chat/completions" in p]
    with TestClient(app) as http:
        local = setup(http)
        http.post(f"/api/businesses/{local}/creatives/texts/generate", json={"count": 3})
        assert any("Где показываем рекламу: Москва" in q for q in asked())

        fakes.requests.clear()
        online = http.post("/api/businesses", json={"name": "Сервис", "kind": "Онлайн-консультации", "city": "", "nationwide": True, "weekly_budget": 20000,
                                                    "description": "Консультации по видео.", "site_url": "a.ru"}).json()["id"]
        http.post(f"/api/businesses/{online}/creatives/texts/generate", json={"count": 3})
        assert any("Где показываем рекламу: вся Россия, онлайн" in q and "про город и «рядом» не пиши" in q for q in asked())


def test_foreign_prices_are_the_amounts_the_ad_does_not_name():
    from app.creative.texts_ai import foreign_prices
    expected = ["Аудит от 9 000 ₽", "Записаться", "от 9 000 ₽"]
    assert foreign_prices(expected, ["Аудит", "от 9 000", "₽", "Записаться"]) == []                      # та же цена, строки разорваны распознаванием
    assert foreign_prices(expected, ["Пилот", "от 39 000 ₽", "Записаться"]) == ["от 39 000 ₽"]           # чужая цена
    assert foreign_prices(["Маникюр", "Записаться"], ["Маникюр", "1500 руб.", "Записаться"]) == ["1500 руб."]   # цены в объявлении нет, а на баннере появилась
    assert foreign_prices(["Маникюр"], ["Работаем 30 дней, 5 мастеров", "Записаться"]) == []              # числа без рубля это не цена


def test_fact_check_reads_the_verdict_and_never_rejects_on_doubt():
    import asyncio
    from app.creative.texts_ai import check_facts

    def ask(content, status=200):
        transport = httpx.MockTransport(lambda r: httpx.Response(status, json={"choices": [{"message": {"content": content}}]}))
        return asyncio.run(check_facts("Пилот от 39 000 ₽", "Пилот", "Под ключ", ["Пилот", "от 9 000 ₽"], "k", "m", transport))

    assert ask('{"problems": ["цена пилота от 9 000 ₽, в фактах от 39 000 ₽"]}') == ["цена пилота от 9 000 ₽, в фактах от 39 000 ₽"]
    assert ask('Вот ответ: {"problems": []} Ошибок нет.') == []
    assert ask("не знаю") is None and ask('{"score": 4}') is None and ask('{"problems": []}', status=500) is None   # нет внятного ответа: не бракуем
    assert asyncio.run(check_facts("факты", "T", "X", [], "k", "m")) == []                               # на баннере нет текста: сверять нечего


def test_banner_with_a_price_that_is_not_in_the_ad_is_rejected(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}/creatives"
        ad = _ai_ad(http, bid)
        fakes.claude_text = json.dumps([ad["title"], "ЗАПИСАТЬСЯ", "от 9 000 ₽"], ensure_ascii=False)   # у объявления цены нет, а на баннере она появилась
        http.post(f"{base}/ads/4/ai-banner")
        http.get(base)
        bad = _ai_ad(http, bid)
        assert bad["ai"]["status"] == "failed" and "цена, которой нет" in bad["ai"]["error"] and "9 000" in bad["ai"]["error"]


def test_a_failed_text_reading_is_retried_so_the_banner_is_not_accepted_unchecked(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=False)
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}/creatives"
        ad = _ai_ad(http, bid)
        http.post(f"{base}/ads/4/ai-banner")                                  # заказ: арт-директору и художнику сбой не мешает
        fakes.fal_done = True
        fakes.claude_text = json.dumps([ad["title"], "ЗАПИСАТЬСЯ", "от 9 000 ₽"], ensure_ascii=False)   # на баннере чужая цена
        fakes.fail_chat = 1                                                    # первое чтение текста упало
        http.get(base)
        bad = _ai_ad(http, bid)
        assert bad["ai"]["status"] == "failed" and "цена, которой нет" in bad["ai"]["error"]   # повтор прочитал текст, и проверка сработала


def test_banner_that_contradicts_the_business_facts_is_rejected_and_a_clean_one_passes(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}/creatives"
        ad = _ai_ad(http, bid)
        fakes.claude_text = json.dumps([ad["title"], "ЗАПИСАТЬСЯ"], ensure_ascii=False)
        order = lambda: (http.post(f"{base}/ads/4/ai-banner"), http.get(base), _ai_ad(http, bid))[2]

        fakes.facts_text = '{"problems": ["срок указан 4 недели, а в описании 4–6 недель"]}'
        bad = order()
        assert bad["ai"]["status"] == "failed" and "противоречат" in bad["ai"]["error"] and "4–6 недель" in bad["ai"]["error"]

        fakes.facts_text = '{"problems": []}'
        assert order()["ai"]["status"] == "ready"

        fakes.facts_text = None                                              # проверяющий не ответил внятно: баннер не бракуем
        assert order()["ai"]["status"] == "ready"


def test_photo_check_tags_photos_and_rates_the_logo(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    Fakes(claude_text=json.dumps({"type": "hands", "score": 4, "issues": ["чужая вывеска"], "summary": "Руки с маникюром крупным планом."}, ensure_ascii=False))
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}"
        http.post(f"{base}/assets", data={"kind": "photo", "tag": ""}, files={"file": ("a.png", png(), "image/png")})
        http.post(f"{base}/assets", data={"kind": "logo", "tag": ""}, files={"file": ("l.png", _logo_on_background((0, 0, 0, 0)), "image/png")})
        assert http.post(f"{base}/creatives/photos/check").json() == {"checked": 2, "failed": 0}
        assert http.post(f"{base}/creatives/photos/check").json() == {"checked": 0, "failed": 0}  # уже проверено, повторно не платим
        photo = next(a for a in http.get(base).json()["assets"] if a["kind"] == "photo")
        assert photo["ai_type"] == "work" and photo["ai_label"] == "Наши работы" and photo["ai_score"] == 4
        assert "чужая вывеска" in photo["ai_note"]
        assert http.get(f"{base}/creatives").json()["spend"]["spent_usd"] == 0.02


def test_photo_check_failure_is_not_charged(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    Fakes(claude_text=None)  # модель не ответила
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}"
        http.post(f"{base}/assets", data={"kind": "photo", "tag": ""}, files={"file": ("a.png", png(), "image/png")})
        assert http.post(f"{base}/creatives/photos/check").json() == {"checked": 0, "failed": 1}
        assert http.get(f"{base}/creatives").json()["spend"]["spent_usd"] == 0


def test_photo_types_alternate_two_variants_per_type():
    from app.direct.launch import photo_for
    typed = [("interior", b"i1"), ("interior", b"i2"), ("work", b"w1"), ("work", b"w2")]
    assert [photo_for(typed, i) for i in range(4)] == [("interior", b"i1"), ("work", b"w1"), ("interior", b"i2"), ("work", b"w2")]
    assert photo_for([("other", b"x")], 3) == ("other", b"x")  # один тип: как раньше


def test_logo_with_bad_rating_is_not_put_on_banners(isolated=None):
    import sqlite3
    from app.config import get_settings
    from app.db import connect
    from app.direct.launch import logo_for
    with TestClient(app) as http:
        bid = setup(http)
        http.post(f"/api/businesses/{bid}/assets", data={"kind": "logo", "tag": ""}, files={"file": ("l.png", _logo_on_background((0, 0, 0, 0)), "image/png")})
        conn = connect(get_settings().db_path)
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        assert logo_for(conn, biz) is not None                                   # не проверен: берём
        conn.execute("UPDATE business_assets SET ai_score = 2 WHERE kind = 'logo'"); conn.commit()
        assert logo_for(conn, biz) is None                                       # ИИ нашёл проблемы: не ставим
        conn.execute("UPDATE business_assets SET ai_score = 4 WHERE kind = 'logo'"); conn.commit()
        assert logo_for(conn, biz) is not None
        conn.close()


def test_ai_banner_gets_the_logo_corner(monkeypatch):
    from app.creative.compose import add_logo
    from PIL import Image as PILImage
    base = BytesIO(); PILImage.new("RGB", (1200, 900), (30, 90, 60)).save(base, "JPEG")
    logo = BytesIO(); PILImage.new("RGBA", (300, 120), (200, 20, 20, 255)).save(logo, "PNG")
    out = PILImage.open(BytesIO(add_logo(base.getvalue(), logo.getvalue()))).convert("RGB")
    assert out.getpixel((600, 450)) != (255, 255, 255) and out.getpixel((1000, 80)) != (30, 90, 60)  # в углу появился логотип
    assert add_logo(base.getvalue(), None) == base.getvalue()


def test_logo_is_inserted_into_the_green_slot_the_ai_left():
    from app.creative.screen import insert_logo
    from PIL import Image as PILImage, ImageDraw
    img = PILImage.new("RGB", (1200, 900), (20, 20, 40))
    ImageDraw.Draw(img).rectangle((120, 700, 420, 800), fill=(0, 255, 0))  # слот 300x100 слева внизу
    base = BytesIO(); img.save(base, "JPEG", quality=95)
    logo = BytesIO(); PILImage.new("RGBA", (300, 100), (255, 200, 0, 255)).save(logo, "PNG")
    out = PILImage.open(BytesIO(insert_logo(base.getvalue(), logo.getvalue()))).convert("RGB")
    r, g, b = out.getpixel((270, 750))
    assert r > 200 and g > 150 and b < 80  # на месте слота настоящий логотип
    assert out.getpixel((270, 805))[1] < 100  # зелёного не осталось
    plain = BytesIO(); PILImage.new("RGB", (1200, 900), (20, 20, 40)).save(plain, "JPEG")
    assert insert_logo(plain.getvalue(), logo.getvalue()) is None  # слота нет: вызывающий код поставит логотип в угол


def test_art_director_gives_different_scenes_and_decides_about_the_logo():
    import asyncio
    import httpx
    from app.creative.art import art_direction, scene_key, review_banner
    biz = {"kind": "Игры онлайн", "name": "М", "description": "Играйте в телефоне", "visual_hint": ""}
    v = {"title": "Играйте", "text": "Присоединяйтесь", "angle": ""}
    assert len({scene_key(i) for i in range(5)}) == 5 and scene_key(0) == scene_key(5)   # пять разных сюжетов по кругу

    def answer(content):
        return httpx.MockTransport(lambda req: httpx.Response(200, json={"choices": [{"message": {"content": content}}]}))

    good = json.dumps({"audience": "друзья 20-35", "scene": "Four friends around a table look at a phone in the middle, warm evening light.",
                       "logo": True, "logo_reason": "есть место"}, ensure_ascii=False)
    art = asyncio.run(art_direction(biz, v, 0, [], {"ratio": 2.0, "score": 4}, False, "k", "m", answer(good)))
    assert art["ai"] and art["logo"] is True and art["audience"] == "друзья 20-35" and "friends" in art["scene"]
    no_logo = asyncio.run(art_direction(biz, v, 0, [], None, False, "k", "m", answer(good)))
    assert no_logo["logo"] is False                                           # логотипа нет: ставить нечего
    broken = asyncio.run(art_direction(biz, v, 1, [], None, False, "k", "m", answer("не json")))
    assert broken["ai"] is False and broken["scene"] and broken["concept"] == scene_key(1)   # сбой модели: запасная сцена
    assert asyncio.run(art_direction(biz, v, 2, [], None, False, "", "m"))["ai"] is False
    verdict = asyncio.run(review_banner("data:image/jpeg;base64,AA==", "бизнес", "k", "m", answer('{"score": 2, "issues": ["кривые руки"]}')))
    assert verdict == {"score": 2, "issues": ["кривые руки"]}
    assert asyncio.run(review_banner("data:image/jpeg;base64,AA==", "бизнес", "k", "m", answer("ничего"))) is None


def test_stray_magenta_piece_next_to_the_screen_is_painted_over():
    from app.creative.screen import insert_screenshot
    from PIL import Image as PILImage, ImageDraw
    img = PILImage.new("RGB", (1024, 768), (60, 40, 30))
    d = ImageDraw.Draw(img)
    d.rectangle((500, 250, 640, 520), fill=(255, 0, 255))     # экран
    d.rectangle((520, 540, 560, 570), fill=(255, 0, 255))     # кусок заглушки ниже, отделённый «пальцем»
    base = BytesIO(); img.save(base, "JPEG", quality=95)
    shot = BytesIO(); PILImage.new("RGB", (600, 1300), (20, 90, 160)).save(shot, "PNG")
    out = PILImage.open(BytesIO(insert_screenshot(base.getvalue(), shot.getvalue()))).convert("RGB")
    r, g, b = out.getpixel((540, 555))
    assert not (r > 200 and b > 200 and g < 90)                # пурпурного не осталось


def test_narrow_vertical_screen_gets_sane_corners():
    from app.creative.screen import _corners, _key_mask, _largest_region
    from PIL import Image as PILImage, ImageDraw
    img = PILImage.new("RGB", (1024, 768), (30, 30, 30))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((560, 330, 610, 470), radius=10, fill=(255, 0, 255))      # узкий высокий экран, как у телефона в руке
    quad = _corners(_largest_region(_key_mask(img)))
    assert all(540 <= x <= 630 and 310 <= y <= 490 for x, y in quad)               # углы рядом с экраном, а не за тысячи пикселей


def test_portrait_screenshot_in_a_landscape_phone_is_fitted_not_cropped():
    from app.creative.screen import insert_screenshot
    from PIL import Image as PILImage, ImageDraw
    img = PILImage.new("RGB", (1200, 900), (30, 30, 30))
    ImageDraw.Draw(img).rectangle((500, 300, 900, 500), fill=(255, 0, 255))      # горизонтальный экран 400x200
    base = BytesIO(); img.save(base, "JPEG", quality=95)
    shot_img = PILImage.new("RGB", (600, 1300), (10, 10, 10))                    # вертикальный скриншот: яркое только по краям
    d = ImageDraw.Draw(shot_img); d.rectangle((0, 0, 600, 200), fill=(250, 250, 80)); d.rectangle((0, 1100, 600, 1300), fill=(80, 250, 250))
    shot = BytesIO(); shot_img.save(shot, "PNG")
    out = PILImage.open(BytesIO(insert_screenshot(base.getvalue(), shot.getvalue()))).convert("RGB")
    assert out.getpixel((700, 310))[0] > 150 and out.getpixel((700, 490))[2] > 150     # видны и верх, и низ скриншота, середину не вырезали


def test_green_leftovers_around_the_slot_are_cleaned():
    from app.creative.screen import insert_logo
    from PIL import Image as PILImage, ImageDraw
    img = PILImage.new("RGB", (1200, 900), (20, 20, 40))
    d = ImageDraw.Draw(img)
    d.rectangle((120, 700, 420, 800), fill=(0, 255, 0))
    for cx, cy in ((100, 680), (440, 680), (100, 820), (440, 820)):   # уголки-метки, как рисует нейросеть
        d.rectangle((cx - 4, cy - 4, cx + 14, cy + 14), fill=(0, 255, 0))
    base = BytesIO(); img.save(base, "JPEG", quality=95)
    logo = BytesIO(); PILImage.new("RGBA", (300, 100), (255, 200, 0, 255)).save(logo, "PNG")
    out = PILImage.open(BytesIO(insert_logo(base.getvalue(), logo.getvalue()))).convert("RGB")
    assert all(out.getpixel(p)[1] < 120 for p in ((106, 686), (446, 686), (106, 826), (446, 826)))


def _logo_on_background(bg):
    from PIL import Image as PILImage, ImageDraw
    img = PILImage.new("RGBA", (400, 120), bg)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((20, 20, 140, 100), radius=14, fill=(30, 200, 230, 255))
    d.rectangle((170, 30, 380, 90), fill=(255, 255, 255, 255))
    out = BytesIO(); img.save(out, "PNG")
    return out.getvalue()


def test_logo_background_detection_and_code_fallback():
    from app.creative.logo import has_transparency, knockout_background
    from PIL import Image as PILImage
    opaque = _logo_on_background((5, 6, 11, 255))
    clear = _logo_on_background((0, 0, 0, 0))
    assert has_transparency(clear) and not has_transparency(opaque)
    from app.creative.logo import _kept_everything
    assert _kept_everything(opaque, knockout_background(opaque))                    # знак цел
    assert not _kept_everything(opaque, opaque)                                      # фон не убран
    cut = knockout_background(opaque)
    assert cut is not None and has_transparency(cut)
    img = PILImage.open(BytesIO(cut)).convert("RGBA")
    assert img.getpixel((2, 2))[3] == 0 and img.getpixel((80, 60))[3] == 255        # фон прозрачный, знак цел
    noisy = PILImage.effect_noise((200, 100), 90).convert("RGBA"); buf = BytesIO(); noisy.save(buf, "PNG")
    assert knockout_background(buf.getvalue()) is None                              # не однотонный фон не трогаем


def test_logo_upload_removes_an_opaque_background(monkeypatch):
    monkeypatch.delenv("FAL_KEY", raising=False)         # без ключа работает вырезание кодом
    with TestClient(app) as http:
        bid = setup(http)
        solid = _logo_on_background((5, 6, 11, 255))
        keep = http.post(f"/api/businesses/{bid}/assets", data={"kind": "logo", "tag": ""}, files={"file": ("l.png", solid, "image/png")})
        assert keep.status_code == 200 and keep.json()["bg_removed"] == "none"      # по умолчанию фон не трогаем
        up = http.post(f"/api/businesses/{bid}/assets", data={"kind": "logo", "tag": "", "remove_bg": "true"}, files={"file": ("l.png", solid, "image/png")})
        assert up.status_code == 200 and up.json()["bg_removed"] == "code"
        again = http.post(f"/api/businesses/{bid}/assets", data={"kind": "logo", "tag": ""}, files={"file": ("l.png", _logo_on_background((0, 0, 0, 0)), "image/png")})
        assert again.json()["bg_removed"] == "none"      # прозрачный логотип не трогаем


def test_logo_slot_rule_reaches_the_prompt():
    from app.creative import fal
    row = {"kind": "Игры", "name": "М", "description": "Онлайн игры", "visual_hint": ""}
    assert "#00FF00" in fal.banner_prompt(row, "З", "", "Играть", logo_corner=True, logo_ratio=2.0)
    assert "#00FF00" not in fal.banner_prompt(row, "З", "", "Играть")


def test_logo_goes_to_the_calmest_corner_without_white_frame():
    from app.creative.compose import add_logo
    from PIL import Image as PILImage, ImageDraw
    base_img = PILImage.new("RGB", (1200, 900), (20, 20, 40))
    d = ImageDraw.Draw(base_img)
    for i in range(0, 400, 20):  # правый верхний угол «шумный»
        d.rectangle((900 + i % 300, 20 + i // 3, 960 + i % 300, 60 + i // 3), fill=(255, 255 - i // 2, i // 2))
    base = BytesIO(); base_img.save(base, "JPEG", quality=95)
    logo = BytesIO(); PILImage.new("RGBA", (300, 120), (255, 200, 0, 255)).save(logo, "PNG")
    out = PILImage.open(BytesIO(add_logo(base.getvalue(), logo.getvalue()))).convert("RGB")
    r, g, b = out.getpixel((100, 70))  # левый верхний угол: жёлтый логотип прямо на фоне
    assert r > 200 and g > 150 and b < 80
    assert out.getpixel((30, 30)) != (255, 255, 255)  # белой рамки вокруг нет


FACTS = ("Студия на три места. Маникюр с покрытием от 2 200 ₽, педикюр от 2 800 ₽. Первый визит со скидкой 20%. "
         "Покрытие держится до 4 недель. Инструменты в крафт-пакетах. Гарантия: бесплатная коррекция в течение 3 дней.")


def test_promises_are_allowed_only_when_the_owner_said_them():
    from app.direct.texts import unsupported_claims as bad
    assert bad("Скидка 20% на первый визит", FACTS) == []                      # акция названа владельцем
    assert bad("Скидка 20% на первый визит", "Просто студия маникюра") != []    # не названа: нельзя
    assert bad("Скидка 30% на первый визит", FACTS) != []                      # другая цифра: нельзя
    assert bad("Гарантия и бесплатная коррекция", FACTS) == [] and bad("Гарантия", "Салон") != []
    assert bad("Маникюр от 2 200 ₽", FACTS) == [] and bad("Маникюр от 999 ₽", FACTS) != []   # цена только из описания
    assert bad("Держится до 4 недель", FACTS) == [] and bad("Держится до 9 недель", FACTS) != []
    assert bad("Мастера с опытом больше 5 лет", "Опыт больше пяти лет, три мастера") == []          # число словом в описании
    assert bad("Три мастера", "Опыт больше пяти лет, три мастера") == [] and bad("Семь мастеров", "Опыт больше пяти лет, три мастера") != []
    assert bad("Лучший салон города", FACTS) != [] and bad("Номер один и 100% результат", FACTS) != []  # сильные слова всегда нельзя


def test_ai_variants_follow_facts_and_have_different_angles():
    raw = json.dumps([
        {"title": "Первый визит со скидкой 20%", "text": "Запишитесь на маникюр онлайн", "angle": "promo"},
        {"title": "Маникюр с покрытием от 2 200 ₽", "text": "Запись онлайн", "angle": "price"},
        {"title": "Покрытие держится до 4 недель", "text": "Инструменты в крафт-пакетах", "angle": "durability"},
        {"title": "Покрытие до 4 недель, без сколов", "text": "Запишитесь сегодня", "angle": "durability"},   # угол уже занят
        {"title": "Скидка 50% на всё", "text": "Только у нас", "angle": "promo"},                           # выдумка
        {"title": "Придумано", "text": "Текст", "angle": "несуществующий"},
    ], ensure_ascii=False)
    kept = parse_variants(raw, FACTS)
    assert [v["angle"] for v in kept] == ["promo", "price", "durability", ""]
    assert parse_variants(raw, "") != kept and all("Скидк" not in v["title"] for v in parse_variants(raw, ""))   # без фактов акция отсеивается


def test_angles_depend_on_facts_in_the_description():
    from app.creative.angles import available, label, prompt_block
    plain = available("Небольшая студия")
    assert {"booking", "choice", "identity", "comfort"} <= set(plain) and not {"price", "promo", "durability"} & set(plain)
    rich = available(FACTS)
    assert {"price", "promo", "durability", "safety", "kit"} <= set(rich)
    assert label("promo") == "Акция" and label("нет") == ""
    assert "price:" in prompt_block(FACTS) and "price:" not in prompt_block("Небольшая студия")


def test_ai_proposals_go_into_ads_before_templates(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    Fakes(claude_text=json.dumps([
        {"title": "Покрытие держится до 4 недель", "text": "Инструменты в крафт-пакетах", "angle": "durability"},
        {"title": "Маникюр от 2 200 ₽", "text": "Запись онлайн", "angle": "price"}], ensure_ascii=False))
    with TestClient(app) as http:
        bid = setup(http, description=FACTS)
        base = f"/api/businesses/{bid}/creatives"
        http.post(f"{base}/texts/generate", json={"count": 2})
        ads = http.get(f"{base}/ads").json()["ads"]
        assert [a["title"] for a in ads[:2]] == ["Маникюр от 2 200 ₽", "Покрытие держится до 4 недель"]   # новые первыми
        assert all(a["text_status"] == "draft" and not a["own"] and a["creative_id"] for a in ads[:2])
        assert all(a["text_status"] == "template" for a in ads[2:])
        ai = ads[0]["creative_id"]
        http.put(f"{base}/{ai}", json={"status": "approved"})
        assert http.get(f"{base}/ads").json()["ads"][0]["own"] is True                              # одобренный: «ваш выбор»
        http.put(f"{base}/{ads[1]['creative_id']}", json={"status": "rejected"})
        titles = [a["title"] for a in http.get(f"{base}/ads").json()["ads"]]
        assert "Покрытие держится до 4 недель" not in titles                                         # убранный не возвращается


def test_rewrite_puts_a_new_text_in_the_same_place(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fakes = Fakes(claude_text=json.dumps([
        {"title": "Покрытие держится до 4 недель", "text": "Инструменты в крафт-пакетах", "angle": "durability"},
        {"title": "Маникюр от 2 200 ₽", "text": "Запись онлайн", "angle": "price"}], ensure_ascii=False))
    with TestClient(app) as http:
        bid = setup(http, description=FACTS)
        base = f"/api/businesses/{bid}/creatives"
        http.post(f"{base}/texts/generate", json={"count": 2})
        first = http.get(f"{base}/ads").json()["ads"][0]
        spent = http.get(base).json()["spend"]["spent_usd"]
        fakes.claude_text = json.dumps([
            {"title": "Первый визит со скидкой 20%", "text": "Запишитесь на маникюр", "angle": "promo"},
            {"title": "Маникюр от 2 200 ₽ с покрытием", "text": "Запись онлайн в любое время", "angle": "price"}], ensure_ascii=False)
        r = http.post(f"{base}/ads/0/rewrite").json()
        assert r["angle"] == first["angle"] and r["title"] != first["title"]                       # тот же угол, другой текст
        now = http.get(f"{base}/ads").json()["ads"][0]
        assert now["creative_id"] == first["creative_id"] and now["title"] == r["title"] and now["own"] is True   # то же место и тот же номер
        assert http.get(base).json()["spend"]["spent_usd"] == round(spent + 0.01, 2)
        fakes.claude_text = json.dumps([{"title": now["title"], "text": "тот же", "angle": "price"}], ensure_ascii=False)   # ИИ повторился
        bad = http.post(f"{base}/ads/0/rewrite")
        assert bad.status_code == 502 and "не предложил" in bad.json()["detail"]["message"]
        assert http.get(base).json()["spend"]["spent_usd"] == round(spent + 0.01, 2)                # за неудачу не списали
        assert http.post(f"{base}/ads/99/rewrite").status_code == 400


def test_address_is_saved_goes_to_texts_and_is_not_an_invented_fact(monkeypatch):
    from app.creative.texts_ai import build_prompt, facts_of, parse_variants
    from app.direct.texts import draft_variants
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}"
        cur = http.get(base).json()
        body = {k: cur[k] for k in ("name", "kind", "description", "city", "site_url", "maps_url", "tone", "color_main", "color_extra")}
        body.update(avg_check=0, target_cpa=0, weekly_budget=cur.get("weekly_budget", 0) or 0, address="ул. Тверская, 12, вход со двора, 2 этаж")
        assert http.put(base, json=body).status_code == 200
        saved = http.get(base).json()
        assert saved["address"] == "ул. Тверская, 12, вход со двора, 2 этаж"
    row = {"name": "Лотос", "kind": "Маникюр", "city": "Москва", "description": "Маникюр.", "tone": "", "address": "ул. Тверская, 12"}
    assert "ул. Тверская, 12" in build_prompt(row, 3) and "12" in facts_of(row)
    raw = json.dumps([{"title": "Маникюр на Тверской, 12", "text": "Запишитесь онлайн", "angle": "location"}], ensure_ascii=False)
    assert parse_variants(raw, facts_of(row)) and not parse_variants(raw, row["description"])     # номер дома из адреса не выдумка, без адреса был бы
    assert any("Тверская, 12" in v["text"] for v in draft_variants({**row, "kind": "Маникюр", "name": "Лотос"}, 6))


def test_smart_crop_keeps_the_main_subject_in_the_frame():
    from app.creative.compose import crop_window, make_banner, prepare_with_focus
    from app.direct.launch import Photo
    from PIL import Image as PILImage, ImageDraw
    # широкое фото 3000×1000: собака (красный круг) у правого края
    img = PILImage.new("RGB", (3000, 1000), (200, 200, 200))
    ImageDraw.Draw(img).ellipse((2600, 350, 2800, 650), fill=(255, 0, 0))
    buf = BytesIO(); img.save(buf, "JPEG", quality=92)
    center, _ = crop_window((3000, 1000), 4 / 3, None)
    focused, new_focus = crop_window((3000, 1000), 4 / 3, (2700 / 3000, 0.5))
    assert center[0] > 800 and center[2] < 2300                                   # по центру собаки в кадре нет
    assert focused[0] <= 2600 and focused[2] >= 2800 and focused[2] == 3000        # по фокусу кадр сдвинут к правому краю
    assert 0.5 < new_focus[0] < 1 and abs(new_focus[1] - 0.5) < 0.01
    plain, _ = prepare_with_focus(buf.getvalue(), None)
    kept, _ = prepare_with_focus(buf.getvalue(), (0.9, 0.5))
    red = lambda data: sum(1 for p in PILImage.open(BytesIO(data)).convert("RGB").resize((120, 90)).getdata() if p[0] > 200 and p[1] < 80)
    assert red(plain) == 0 and red(kept) > 20                                      # без фокуса собаки не видно, с фокусом видно
    shaped = make_banner(Photo(buf.getvalue(), 1, 0.9, 0.5), "Заголовок", style="bold")   # и шаблон «круг» берёт фокус
    plain_shape = make_banner(buf.getvalue(), "Заголовок", style="bold")
    assert red(shaped) > red(plain_shape)


def test_photo_check_returns_the_focus_point_and_it_is_saved(monkeypatch):
    import asyncio
    import httpx
    from app.creative.texts_ai import check_photo
    ok = json.dumps({"type": "product", "score": 4, "issues": [], "summary": "Хорошо", "focus": {"x": 0.8, "y": 0.4}})

    def answer(content):
        return httpx.MockTransport(lambda req: httpx.Response(200, json={"choices": [{"message": {"content": content}}]}))

    r = asyncio.run(check_photo("data:image/jpeg;base64,AA==", "k", "m", answer(ok)))
    assert (r["fx"], r["fy"]) == (0.8, 0.4)
    bad = json.dumps({"type": "product", "score": 4, "issues": [], "summary": "x", "focus": {"x": "слева"}})
    r = asyncio.run(check_photo("data:image/jpeg;base64,AA==", "k", "m", answer(bad)))
    assert (r["fx"], r["fy"]) == (0.5, 0.5)                                       # непонятный ответ: по центру
    far = json.dumps({"type": "product", "score": 4, "issues": [], "summary": "x", "focus": {"x": 3, "y": -1}})
    r = asyncio.run(check_photo("data:image/jpeg;base64,AA==", "k", "m", answer(far)))
    assert (r["fx"], r["fy"]) == (0.95, 0.05)                                     # за краем: зажимается


def test_discounts_from_the_description_are_off_by_default_and_can_be_enabled():
    from app.creative.angles import available, effective_description
    from app.creative.texts_ai import build_prompt, facts_of, parse_variants
    from app.direct.launch import text_candidates
    from app.direct.texts import draft_variants
    desc = "Стрижка и мытьё для собак. Работаем с 9 до 21. Первым клиентам скидка 15%. Есть парковка."
    off = {"name": "Л", "kind": "Груминг", "city": "Москва", "description": desc, "tone": "", "address": "", "allow_promo": 0, "id": 1}
    on = {**off, "allow_promo": 1}
    assert "скидк" not in effective_description(off) and "парковка" in effective_description(off) and "15%" in effective_description(on)
    assert "promo" not in available(facts_of(off)) and "promo" in available(facts_of(on))
    assert "скидк" not in build_prompt(off, 3).split("Не предлагай")[0] and "Не предлагай скидки" in build_prompt(off, 3)
    raw = json.dumps([{"title": "Скидка 15% первым клиентам", "text": "Запишитесь онлайн", "angle": "promo"}], ensure_ascii=False)
    assert parse_variants(raw, facts_of(off)) == [] and len(parse_variants(raw, facts_of(on))) == 1
    assert all("скидк" not in (v["title"] + v["text"]).lower() for v in draft_variants(off, 6))

    with TestClient(app) as http:                                        # ранее придуманный черновик со скидкой в рекламу не идёт
        bid = setup(http)
        base = f"/api/businesses/{bid}"
        cur = http.get(base).json()
        body = {k: cur[k] for k in ("name", "kind", "city", "site_url", "maps_url", "tone", "color_main", "color_extra")}
        body.update(description=desc, avg_check=0, target_cpa=0, weekly_budget=cur.get("weekly_budget", 0) or 0)
        assert http.put(base, json=body).json()["allow_promo"] is False   # по умолчанию выключено
        http.post(f"{base}/creatives/texts", json={"title": "Маникюр", "body": "Запишитесь онлайн"})
        from app.config import get_settings
        from app.db import connect
        conn = connect(get_settings().db_path)
        conn.execute("INSERT INTO creatives (business_id, kind, title, body, status, source, angle) VALUES (?, 'text', 'Скидка 15% первым клиентам', "
                     "'Запишитесь онлайн', 'draft', 'claude', 'promo')", (bid,))
        conn.commit()
        row = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        assert all("Скидка" not in c["title"] for c in text_candidates(conn, row))
        conn.execute("UPDATE businesses SET allow_promo = 1 WHERE id = ?", (bid,))
        conn.commit()
        row = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        assert any("Скидка" in c["title"] for c in text_candidates(conn, row))
        assert http.put(base, json={**body, "allow_promo": True}).json()["allow_promo"] is True
        conn.close()


def test_region_is_found_even_when_the_city_field_has_an_address():
    import asyncio
    from app.direct.client import DirectClient

    class Fake(DirectClient):
        def __init__(self): pass
        async def call(self, *a, **k):
            return {"GeoRegions": [{"GeoRegionName": "Москва", "GeoRegionType": "City", "GeoRegionId": 213}]}

    assert asyncio.run(Fake().find_region_id("Москва, ул. Тверская, 12")) == 213
    assert asyncio.run(Fake().find_region_id("г. Москва")) == 213


def test_visual_hint_reaches_the_banner_prompt_and_is_saved(monkeypatch):
    from app.creative import fal
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=False)
    with TestClient(app) as http:
        bid = setup(http, kind="Карточные игры онлайн с ИИ ведущим", name="Мафия онлайн",
                    description="Мафия и другие игры по видео. ИИ ведёт партию, карт нет, всё в телефоне.",
                    visual_hint="Человек играет в телефоне. Бумажных карт на картинках нет.")
        assert http.get(f"/api/businesses/{bid}").json()["visual_hint"] == "Человек играет в телефоне. Бумажных карт на картинках нет."
        http.post(f"/api/businesses/{bid}/creatives/ads/0/ai-banner")
        prompt = [b for h, _, b in fakes.requests if h == "queue.fal.run"][0].decode()
        assert "follow it strictly" in prompt and "Бумажных карт на картинках нет" in prompt and "smartphone" in prompt
    row = {"kind": "Игры", "name": "М", "description": "Всё в телефоне.", "visual_hint": "Только смартфон"}
    assert "Только смартфон" in fal.banner_prompt(row, "З", "", "Играть") and "smartphone" in fal.banner_prompt(row, "З", "", "Играть")
    assert "Только смартфон" in fal.image_prompt(row, "", 0) and "смартфон" in fal.image_prompt(row, "", 0)


def test_rewrite_needs_ai():
    with TestClient(app) as http:
        bid = setup(http)
        assert http.post(f"/api/businesses/{bid}/creatives/ads/0/rewrite").status_code == 503


def test_generated_texts_keep_their_angle(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    Fakes(claude_text=json.dumps([{"title": "Покрытие держится до 4 недель", "text": "Инструменты в крафт-пакетах", "angle": "durability"}], ensure_ascii=False))
    with TestClient(app) as http:
        bid = setup(http, description=FACTS)
        r = http.post(f"/api/businesses/{bid}/creatives/texts/generate", json={"count": 2}).json()
        assert r["ai"] == 1
        texts = [c for c in http.get(f"/api/businesses/{bid}/creatives").json()["creatives"] if c["kind"] == "text"]
        ai = next(c for c in texts if c["source"] == "claude")
        assert ai["angle"] == "durability" and ai["angle_label"] == "Долговечность"
        http.put(f"/api/businesses/{bid}/creatives/{ai['id']}", json={"status": "approved"})
        shown = next(a for a in http.get(f"/api/businesses/{bid}/creatives/ads").json()["ads"] if a["own"])
        assert shown["angle"] == "durability" and shown["angle_label"] == "Долговечность"


def test_online_business_gets_its_own_button_and_no_offline_phrases():
    from app.creative.angles import profile
    from app.direct.texts import draft_variants
    game = {"name": "Мафия онлайн", "kind": "Карточные игры онлайн с ИИ ведущим", "city": "Москва", "description": "Играйте в мафию в телефоне.", "maps_url": ""}
    salon = {"name": "Мята", "kind": "Маникюрная студия", "city": "Москва", "description": "Маникюр и педикюр рядом с метро.", "maps_url": ""}
    shop = {"name": "Тесто", "kind": "Магазин выпечки с доставкой", "city": "Казань", "description": "Хлеб и булки.", "maps_url": ""}
    assert (profile(game)["online"], profile(game)["local"], profile(game)["cta"]) == (True, False, "Играть")
    assert (profile(salon)["online"], profile(salon)["local"], profile(salon)["cta"]) == (False, True, "Записаться")
    assert profile(shop)["cta"] == "Заказать"
    text = " ".join(v["title"] + " " + v["text"] for v in draft_variants(game))
    assert "рядом" not in text and "запись" not in text.lower() and "Запиш" not in text and "Москва" not in text   # офлайн-фраз нет
    assert {v["angle"] for v in draft_variants(game)} <= {"booking", "choice", "comfort"}                           # угла «Расположение» нет
    salon_text = " ".join(v["title"] + " " + v["text"] for v in draft_variants(salon))
    assert "рядом с вами" in salon_text and "запись на сегодня" in salon_text and "Москва" in salon_text            # у салона всё как раньше
    assert len({v["title"] for v in draft_variants(game)}) == 6                                                      # шесть разных заголовков


def _phone_banner():
    """Баннер с «телефоном»: наклонённый пурпурный экран и посторонний пурпурный элемент рядом (заголовок, кнопка)."""
    from PIL import ImageDraw as D
    img = Image.new("RGB", (1200, 900), (30, 20, 70))
    d = D.Draw(img)
    d.polygon([(700, 120), (1000, 160), (1040, 800), (680, 760)], fill=(255, 0, 255))   # экран
    d.rectangle((60, 300, 400, 380), fill=(255, 0, 255))                                  # пурпурное слово, не экран
    out = BytesIO(); img.save(out, "PNG")
    return out.getvalue()


def _screen_shot():
    shot = Image.new("RGB", (540, 1170), (20, 200, 60))   # зелёный «скриншот»
    out = BytesIO(); shot.save(out, "PNG")
    return out.getvalue()


def test_screenshot_goes_into_the_screen_only():
    from app.creative.screen import insert_screenshot
    done = Image.open(BytesIO(insert_screenshot(_phone_banner(), _screen_shot()))).convert("RGB")
    g = lambda p: tuple(done.getpixel(p))
    assert g((850, 450))[1] > 150 and g((850, 450))[0] < 80          # внутри экрана теперь скриншот
    assert g((200, 340)) == (255, 0, 255) or g((200, 340))[0] > 200   # посторонний пурпурный элемент не тронут
    assert g((200, 340))[1] < 60
    assert insert_screenshot(png(), _screen_shot()) is None            # экрана на картинке нет


def test_ai_banner_with_a_screenshot_is_composited(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    monkeypatch.setenv("BANNER_EXTRAS", "1")   # скриншот в экран телефона и логотип в слоте: расширения, по умолчанию выключены
    fakes = Fakes(fal_done=True)
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}"
        up = http.post(f"{base}/assets", data={"kind": "photo", "tag": "screen"}, files={"file": ("s.png", _screen_shot(), "image/png")})
        assert up.status_code == 200 and up.json()["tag"] == "screen"
        ad = http.get(f"{base}/creatives/ads").json()["ads"][0]
        fakes.image = _phone_banner()
        fakes.claude_text = json.dumps([ad["title"], "Лотос", "Записаться"], ensure_ascii=False)
        assert http.post(f"{base}/creatives/ads/0/ai-banner").status_code == 200
        prompt = [b for h, _, b in fakes.requests if h == "queue.fal.run"][0].decode()
        assert "FF00FF" in prompt and "smartphone" in prompt                                         # просим экран-заглушку
        http.get(f"{base}/creatives")
        assert http.get(f"{base}/creatives/ads").json()["ads"][0]["ai"]["status"] == "ready"
        banner = Image.open(BytesIO(http.get(f"{base}/creatives/ads/0/banner.jpg").content)).convert("RGB")
        assert banner.size == (1200, 900) and banner.getpixel((850, 450))[1] > 150               # скриншот на месте
        assert http.post(f"{base}/creatives/photos/check").json() == {"checked": 0, "failed": 0}   # скриншот ИИ не «проверяет»


def test_screenshot_without_a_phone_screen_makes_the_banner_fail(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    monkeypatch.setenv("BANNER_EXTRAS", "1")
    fakes = Fakes(fal_done=True)
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}"
        http.post(f"{base}/assets", data={"kind": "photo", "tag": "screen"}, files={"file": ("s.png", _screen_shot(), "image/png")})
        ad = http.get(f"{base}/creatives/ads").json()["ads"][0]
        fakes.claude_text = json.dumps([ad["title"], "Лотос", "Записаться"], ensure_ascii=False)       # картинка без экрана (однотонная)
        http.post(f"{base}/creatives/ads/0/ai-banner")
        http.get(f"{base}/creatives")
        state = http.get(f"{base}/creatives/ads").json()["ads"][0]["ai"]
        assert state["status"] == "failed" and "экран телефона" in state["error"]


def test_missing_phrases_ignore_case_quotes_and_spaces():
    from app.creative.texts_ai import missing_phrases
    read = ["МАНИКЮР  с покрытием", "от 2 200 ₽", "Студия «Мята»"]
    assert missing_phrases(["Маникюр с покрытием", "от 2 200 ₽", "студия мята"], read) == []
    assert missing_phrases(["Маникюр с покрытием", "Записаться"], read) == ["Записаться"]


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
        bid = setup(http, districts="Хамовники")
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


def test_a_photo_rejected_for_a_foreign_sign_is_checked_again_once_the_business_name_is_known(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    bad = json.dumps({"type": "interior", "score": 2, "issues": ["чужой бренд eden nails"], "summary": "Витрина салона."}, ensure_ascii=False)
    fakes = Fakes(claude_text=bad)
    fakes.keep = 10**8
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}"
        http.post(f"{base}/assets", data={"kind": "photo", "tag": ""}, files={"file": ("a.png", png(), "image/png")})
        photo = lambda: next(a for a in http.get(base).json()["assets"] if a["kind"] == "photo")
        assert http.post(f"{base}/creatives/photos/check").json() == {"checked": 1, "failed": 0} and photo()["ai_score"] == 2
        asked = [json.loads(b)["messages"][-1]["content"] for h, p, b in fakes.requests if "chat/completions" in p][-1]
        assert "Бизнес владельца называется" in asked
        assert http.post(f"{base}/creatives/photos/check").json() == {"checked": 0, "failed": 0}   # название то же: повторно не платим

        body = http.get(base).json()
        body["name"] = "Eden Nails"
        assert http.put(base, json=body).status_code == 200
        fakes.claude_text = json.dumps({"type": "interior", "score": 4, "issues": [], "summary": "Витрина салона."}, ensure_ascii=False)
        assert http.post(f"{base}/creatives/photos/check").json() == {"checked": 1, "failed": 0}   # название сменилось: забракованное смотрим заново
        assert photo()["ai_score"] == 4
        asked = [json.loads(b)["messages"][-1]["content"] for h, p, b in fakes.requests if "chat/completions" in p][-1]
        assert "«Eden Nails»" in asked
