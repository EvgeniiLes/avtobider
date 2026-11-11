"""Запуск A/B со страницы «A/B-тестирование»: одно новое объявление в своей кампании против идущего, дальше сравнение ведёт движок."""
from datetime import date

from fastapi.testclient import TestClient

from app.db import connect
from app.engine.util import MICRO
from app.main import app
from tests.fake_direct import FakeDirect
from tests.test_cohort import launched, run
from tests.test_launch import isolated  # noqa: F401  (isolated: общая подготовка базы для каждого теста)

TODAY = date.today().isoformat()
NEW = {"title": "Автоматизация без хлопот", "text": "Подключим ИИ к вашим процессам за неделю"}


def draft(conn, bid, original_id):
    conn.execute("INSERT INTO creatives (business_id, kind, title, body, source, status, position, ab_of) VALUES (?, 'text', ?, ?, 'claude', 'approved', 1000, ?)",
                 (bid, NEW["title"], NEW["text"], original_id))
    conn.commit()
    return conn.execute("SELECT id FROM creatives WHERE ab_of = ?", (original_id,)).fetchone()[0]


def test_ab_launch_needs_a_banner_then_adds_exactly_one_ad_and_an_experiment(isolated, monkeypatch):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, conn = launched(http, fake, isolated, separate=False, ads=2)
        run(conn, bid, fake, TODAY)                                       # Яндекс принял объявления
        original = conn.execute("SELECT id, campaign_id FROM ad_units ORDER BY id").fetchone()
        old_limit = next(iter(fake.campaigns.values()))["Limit"]
        creative_id = draft(conn, bid, original["id"])
        r = http.post(f"/api/businesses/{bid}/launch/ab", json={"creative_id": creative_id})
        assert r.status_code == 400 and "баннер" in r.json()["detail"]["message"]          # без баннера сравнение нечестное
        assert len(fake.campaigns) == 1

        monkeypatch.setattr("app.direct.launch.ai_banners_for", lambda c, b: {(NEW["title"], NEW["text"]): b"\xff\xd8jpeg"})
        r = http.post(f"/api/businesses/{bid}/launch/ab", json={"creative_id": creative_id})
        assert r.status_code == 200, r.text
        assert len(fake.campaigns) == 2 and len(fake.ads) == 3                                    # одно новое объявление в своей кампании
        new_limit = [c["Limit"] for i, c in fake.campaigns.items() if c["Limit"] != old_limit or len(fake.campaigns) == 2][-1]
        assert new_limit == old_limit // 2                                                         # бюджет как доля идущего объявления в его кампании
        unit = conn.execute("SELECT * FROM ad_units ORDER BY id DESC LIMIT 1").fetchone()
        assert unit["title"] == NEW["title"] and unit["variant_of"] == original["id"]
        ex = conn.execute("SELECT * FROM experiments").fetchone()
        assert (ex["unit_a"], ex["unit_b"], ex["status"]) == (original["id"], unit["id"], "running")
        assert conn.execute("SELECT ab_of FROM creatives WHERE id = ?", (creative_id,)).fetchone()[0] is None   # черновик ушёл в рекламу
        assert http.get(f"/api/businesses/{bid}/creatives/ab").json()["drafts"] == []
        again = http.post(f"/api/businesses/{bid}/launch/ab", json={"creative_id": creative_id})
        assert again.status_code == 404                                                            # второй раз то же самое запустить нельзя
        conn.close()
