"""Видео объявления уходит в Директ к конкретному объявлению: загрузка, ожидание конвертации, креатив и привязка."""
from datetime import date

from fastapi.testclient import TestClient

from app.main import app
from app.routes.businesses import _asset_path
from tests.fake_direct import FakeDirect
from tests.test_cohort import launched, run
from tests.test_launch import isolated  # noqa: F401  (isolated: общая подготовка базы для каждого теста)

TODAY = date.today().isoformat()


def video_of(conn, bid, unit, tail="a"):
    """Готовое видео, сделанное из баннера этого объявления: файл на диске, строка файла и строка креатива."""
    conn.execute("INSERT INTO business_assets (business_id, kind, stored_name, mime, size_bytes) VALUES (?, 'video', ?, 'video/mp4', 8)", (bid, f"clip-{tail}.mp4"))
    asset = conn.execute("SELECT MAX(id) FROM business_assets").fetchone()[0]
    path = _asset_path(bid, f"clip-{tail}.mp4")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    conn.execute("INSERT INTO creatives (business_id, kind, title, body, status, source, asset_id) VALUES (?, 'video', ?, ?, 'draft', 'fal', ?)",
                 (bid, unit["title"], unit["body"], asset))
    conn.commit()
    return conn.execute("SELECT MAX(id) FROM creatives").fetchone()[0], asset


def setup_ad(http, fake, db_path):
    bid, conn = launched(http, fake, db_path, separate=False, ads=2)
    run(conn, bid, fake, TODAY)
    unit = conn.execute("SELECT * FROM ad_units ORDER BY id").fetchone()
    assert unit["ad_kind"] == "" and unit["external_ad_id"]
    return bid, conn, unit


def test_video_goes_to_its_own_ad_after_direct_has_converted_it(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, conn, unit = setup_ad(http, fake, isolated)
        video, _asset = video_of(conn, bid, unit)
        r = http.post(f"/api/businesses/{bid}/creatives/{video}/direct")
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "converting" and len(fake.videos) == 1
        assert "VideoExtension" not in fake.ads[int(unit["external_ad_id"])]["ad"]                    # пока Директ конвертирует, к объявлению ничего не привязано
        listed = next(c for c in http.get(f"/api/businesses/{bid}/creatives").json()["creatives"] if c["id"] == video)
        assert listed["direct"]["can"] is True and listed["direct"]["state"] == "converting"

        again = http.post(f"/api/businesses/{bid}/creatives/{video}/direct")                       # «Проверить» до готовности: ничего нового не загружает
        assert again.json()["state"] == "converting" and len(fake.videos) == 1

        next(iter(fake.videos.values()))["status"] = "READY"
        done = http.post(f"/api/businesses/{bid}/creatives/{video}/direct")
        assert done.json()["state"] == "attached"
        (creative_id,) = fake.video_creatives
        assert fake.ads[int(unit["external_ad_id"])]["ad"]["VideoExtension"] == {"CreativeId": creative_id}
        other = conn.execute("SELECT * FROM ad_units ORDER BY id").fetchall()[1]
        assert "VideoExtension" not in fake.ads[int(other["external_ad_id"])]["ad"]                    # второе объявление видео не получило
        conn.close()


def test_the_regular_sync_finishes_the_video_without_the_owner_pressing_anything(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, conn, unit = setup_ad(http, fake, isolated)
        video, _asset = video_of(conn, bid, unit)
        http.post(f"/api/businesses/{bid}/creatives/{video}/direct")
        next(iter(fake.videos.values()))["status"] = "READY"
        run(conn, bid, fake, TODAY)
        row = conn.execute("SELECT video_state, video_creative FROM ad_units WHERE id = ?", (unit["id"],)).fetchone()
        assert row["video_state"] == "attached" and row["video_creative"] in fake.video_creatives
        conn.close()


def test_a_video_direct_cannot_process_is_reported_and_can_be_sent_again(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, conn, unit = setup_ad(http, fake, isolated)
        video, _asset = video_of(conn, bid, unit)
        http.post(f"/api/businesses/{bid}/creatives/{video}/direct")
        next(iter(fake.videos.values()))["status"] = "ERROR"
        failed = http.post(f"/api/businesses/{bid}/creatives/{video}/direct")
        assert failed.json()["state"] == "error" and "формат" in failed.json()["message"]
        assert fake.video_creatives == {}                                                          # креатив из негодного видео не создаётся
        retry = http.post(f"/api/businesses/{bid}/creatives/{video}/direct")                       # после ошибки отправляется заново, а не «проверяется»
        assert retry.json()["state"] == "converting" and len(fake.videos) == 2
        conn.close()


def test_direct_refusing_the_file_gives_the_owner_a_message_and_marks_nothing(isolated):
    fake = FakeDirect()
    fake.reject_video = True
    with TestClient(app) as http:
        bid, conn, unit = setup_ad(http, fake, isolated)
        video, _asset = video_of(conn, bid, unit)
        r = http.post(f"/api/businesses/{bid}/creatives/{video}/direct")
        assert r.status_code == 400 and r.json()["detail"]["message"]
        assert conn.execute("SELECT video_state FROM ad_units WHERE id = ?", (unit["id"],)).fetchone()[0] == ""
        conn.close()


def test_a_video_of_an_ad_that_is_not_launched_yet_is_not_sent(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, conn, unit = setup_ad(http, fake, isolated)
        video, _asset = video_of(conn, bid, {"title": "Объявление, которого нет в Директе", "body": "Другой текст"})
        r = http.post(f"/api/businesses/{bid}/creatives/{video}/direct")
        assert r.status_code == 400 and "не запущено" in r.json()["detail"]["message"]
        assert fake.videos == {}
        conn.close()


def test_a_combined_ad_gets_no_video_with_a_clear_reason(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, conn, unit = setup_ad(http, fake, isolated)
        conn.execute("UPDATE ad_units SET ad_kind = 'responsive' WHERE id = ?", (unit["id"],))
        conn.commit()
        video, _asset = video_of(conn, bid, unit)
        r = http.post(f"/api/businesses/{bid}/creatives/{video}/direct")
        assert r.status_code == 400 and "комбинаторн" in r.json()["detail"]["message"] and fake.videos == {}
        conn.close()
