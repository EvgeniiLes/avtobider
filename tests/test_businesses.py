from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import app
from app.routes import auth


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    auth._fails.clear()
    return tmp_path


def register(http, email="owner@example.com"):
    return http.post("/api/auth/register", json={"email": email, "password": "password123"})


def png(width=800, height=600, color=(200, 30, 90)) -> bytes:
    out = BytesIO()
    Image.new("RGB", (width, height), color).save(out, "PNG")
    return out.getvalue()


def upload(http, business_id, data, kind="photo", tag="interior", name="a.png"):
    return http.post(
        f"/api/businesses/{business_id}/assets",
        data={"kind": kind, "tag": tag, "caption": "Подпись"},
        files={"file": (name, data, "application/octet-stream")},
    )


def make_business(http, **extra):
    body = {"name": "Салон «Лотос»", "kind": "Маникюр", "description": "Маникюр и педикюр", "city": "Москва"}
    body.update(extra)
    return http.post("/api/businesses", json=body)


# ---------- бизнес ----------

def test_business_requires_login():
    with TestClient(app) as http:
        assert http.get("/api/businesses").status_code == 401
        assert http.post("/api/businesses", json={"name": "x", "kind": "y"}).status_code == 401


def test_create_update_and_money_in_rubles():
    with TestClient(app) as http:
        register(http)
        r = make_business(http, avg_check=2500.5, target_cpa=800, weekly_budget=10000, site_url="salon.ru", color_main="#AABBCC", tone="gentle")
        assert r.status_code == 200
        b = r.json()
        assert b["avg_check"] == 2500.5 and b["weekly_budget"] == 10000
        assert b["site_url"] == "https://salon.ru"
        assert b["color_main"] == "#aabbcc"

        upd = http.put(f"/api/businesses/{b['id']}", json={"name": "Новое имя", "kind": "Маникюр", "weekly_budget": 5000})
        assert upd.json()["name"] == "Новое имя"
        assert upd.json()["weekly_budget"] == 5000
        assert len(http.get("/api/businesses").json()["businesses"]) == 1


def test_validation_messages():
    with TestClient(app) as http:
        register(http)
        assert make_business(http, name="").status_code == 422
        assert make_business(http, color_main="красный").status_code == 422
        assert make_business(http, tone="злой").status_code == 422
        assert make_business(http, weekly_budget=-5).status_code == 422


def test_maps_link_is_checked_and_org_id_extracted():
    with TestClient(app) as http:
        register(http)
        url = "https://yandex.ru/maps/org/lotos/1234567890/?ll=37.6%2C55.7&z=17"
        b = make_business(http, maps_url=url).json()
        assert b["maps_url"] == url and b["maps_org_id"] == "1234567890"
        short = make_business(http, maps_url="yandex.ru/maps/-/CDxyz123").json()
        assert short["maps_url"] == "https://yandex.ru/maps/-/CDxyz123" and short["maps_org_id"] == ""
        profile = make_business(http, maps_url="https://yandex.ru/profile/9876543210").json()
        assert profile["maps_org_id"] == "9876543210"
        empty = make_business(http, maps_url="").json()
        assert empty["maps_url"] == ""
        bad = make_business(http, maps_url="https://evil.example/maps/org/1/22222")
        assert bad.status_code == 422
        assert "Яндекс Картах" in str(bad.json())
        updated = http.put(f"/api/businesses/{b['id']}", json={"name": "x", "kind": "y", "maps_url": ""}).json()
        assert updated["maps_url"] == "" and updated["maps_org_id"] == ""


def test_business_limit():
    with TestClient(app) as http:
        register(http)
        for _ in range(5):
            assert make_business(http).status_code == 200
        r = make_business(http)
        assert r.status_code == 400
        assert "не больше 5" in r.json()["detail"]["message"]


def test_users_do_not_see_each_others_businesses():
    with TestClient(app) as alice, TestClient(app) as bob:
        register(alice, "alice@example.com")
        register(bob, "bob@example.com")
        bid = make_business(alice).json()["id"]
        photo = upload(alice, bid, png()).json()
        assert bob.get("/api/businesses").json()["businesses"] == []
        assert bob.get(f"/api/businesses/{bid}").status_code == 404
        assert bob.put(f"/api/businesses/{bid}", json={"name": "x", "kind": "y"}).status_code == 404
        assert bob.delete(f"/api/businesses/{bid}").status_code == 404
        assert upload(bob, bid, png()).status_code == 404
        assert bob.get(photo["url"]).status_code == 404
        assert alice.get(photo["url"]).status_code == 200


# ---------- файлы ----------

def test_photo_upload_is_reencoded_and_served():
    with TestClient(app) as http:
        register(http)
        bid = make_business(http).json()["id"]
        r = upload(http, bid, png(1000, 700))
        assert r.status_code == 200
        asset = r.json()
        assert asset["mime"] == "image/jpeg"
        assert asset["tag_label"] == "Помещение"
        assert (asset["width"], asset["height"]) == (1000, 700)
        served = http.get(asset["url"])
        assert served.headers["content-type"] == "image/jpeg"
        assert served.headers["x-content-type-options"] == "nosniff"
        assert Image.open(BytesIO(served.content)).size == (1000, 700)


def test_big_photo_is_downscaled():
    with TestClient(app) as http:
        register(http)
        bid = make_business(http).json()["id"]
        r = upload(http, bid, png(4000, 2000))
        assert max(r.json()["width"], r.json()["height"]) == 3000


def test_exif_is_stripped():
    out = BytesIO()
    img = Image.new("RGB", (800, 600), (10, 10, 10))
    exif = Image.Exif()
    exif[0x010F] = "SecretCamera"
    img.save(out, "JPEG", exif=exif)
    with TestClient(app) as http:
        register(http)
        bid = make_business(http).json()["id"]
        asset = upload(http, bid, out.getvalue(), name="a.jpg").json()
        stored = Image.open(BytesIO(http.get(asset["url"]).content))
        assert 0x010F not in stored.getexif()


def test_bad_files_rejected_with_plain_message():
    with TestClient(app) as http:
        register(http)
        bid = make_business(http).json()["id"]
        fake = upload(http, bid, b"MZ\x90\x00 this is not an image", name="virus.png")
        assert fake.status_code == 400
        assert "картинку" in fake.json()["detail"]["message"]
        small = upload(http, bid, png(200, 200))
        assert small.status_code == 400
        assert "маленькая" in small.json()["detail"]["message"]
        assert upload(http, bid, b"abc", kind="video").status_code == 400
        assert upload(http, bid, png(), kind="something").status_code == 400


def test_logo_is_png_and_replaced():
    with TestClient(app) as http:
        register(http)
        bid = make_business(http).json()["id"]
        first = upload(http, bid, png(300, 300), kind="logo", tag="").json()
        assert first["mime"] == "image/png"
        second = upload(http, bid, png(400, 400, (1, 2, 3)), kind="logo", tag="").json()
        assets = http.get(f"/api/businesses/{bid}").json()["assets"]
        assert [a["id"] for a in assets if a["kind"] == "logo"] == [second["id"]]
        assert http.get(first["url"]).status_code == 404


def test_photo_limit(monkeypatch):
    monkeypatch.setitem(__import__("app.assets", fromlist=["LIMITS"]).LIMITS, "photo", 2)
    with TestClient(app) as http:
        register(http)
        bid = make_business(http).json()["id"]
        assert upload(http, bid, png()).status_code == 200
        assert upload(http, bid, png()).status_code == 200
        r = upload(http, bid, png())
        assert r.status_code == 400
        assert "предел" in r.json()["detail"]["message"]


def test_video_accepted_by_content_not_name():
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
    with TestClient(app) as http:
        register(http)
        bid = make_business(http).json()["id"]
        r = upload(http, bid, mp4, kind="video", tag="", name="clip.exe")
        assert r.status_code == 200
        assert r.json()["mime"] == "video/mp4"


def test_delete_asset_and_business_remove_files(isolated):
    with TestClient(app) as http:
        register(http)
        bid = make_business(http).json()["id"]
        a = upload(http, bid, png()).json()
        folder = isolated / "uploads" / str(bid)
        assert len(list(folder.iterdir())) == 1
        assert http.delete(f"/api/businesses/{bid}/assets/{a['id']}").status_code == 200
        assert list(folder.iterdir()) == []
        upload(http, bid, png())
        assert http.delete(f"/api/businesses/{bid}").status_code == 200
        assert not folder.exists()


def test_options_for_form():
    with TestClient(app) as http:
        opts = http.get("/api/businesses/options").json()
        assert {t["value"] for t in opts["photo_tags"]} >= {"interior", "work", "team", "product", "other"}
        assert opts["limits"]["logo"] == 1
