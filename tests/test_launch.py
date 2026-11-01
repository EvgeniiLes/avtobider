import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.deps import client_factory
from app.direct.runner import sync_and_run
from app.engine.cycle import run_cycle
from app.engine.util import MICRO, day_add
from app.main import app
from app.platforms.direct import DirectPlatform
from app.routes import auth
from app.security import encrypt_token
from tests.fake_direct import FakeDirect

BUDGET = 20_000


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.delenv("FAL_KEY", raising=False)   # запуск сам составляет профиль аудитории, если есть ключ; в тестах ходить к настоящему ИИ нельзя
    auth._fails.clear()
    yield tmp_path / "test.db"
    app.dependency_overrides.clear()


def setup(http, fake, db_path, **business):
    http.post("/api/auth/register", json={"email": "o@example.com", "password": "password123"})
    app.dependency_overrides[client_factory] = fake.factory()
    body = {"name": "Лотос", "kind": "Маникюрный салон", "city": "Москва", "weekly_budget": BUDGET, "target_cpa": 800,
            "description": "Маникюр и педикюр в центре города.", "site_url": "salon.ru", "mode": "auto"}
    body.update(business)
    bid = http.post("/api/businesses", json=body).json()["id"]
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO ad_accounts (user_id, title, token_enc) VALUES (1, 'fake-login', ?)", (encrypt_token("tok", "test-secret-key"),))
    conn.commit()
    conn.close()
    return bid


def rows(db_path, sql, *args):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


# ---------- план и запуск ----------

def test_preview_shows_what_will_be_created(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        plan = http.get(f"/api/businesses/{bid}/launch/preview").json()
        assert plan["count"] == 3 and plan["city"] == "Москва" and plan["link"] == "https://salon.ru"
        assert plan["weekly"] <= BUDGET * 0.9 * 0.7 + 6 and plan["total"] == plan["weekly"]   # одна кампания, один недельный лимит
        assert all(len(v["title"]) <= 56 and len(v["text"]) <= 81 for v in plan["variants"])
        assert len({v["title"] for v in plan["variants"]}) == 3
        assert [w for w in plan["warnings"] if "Профиль аудитории" not in w] == []
        assert any("Профиль аудитории не составлен" in w for w in plan["warnings"]) and plan["keywords"] == [] and plan["audience"] is None
        assert plan["autotargeting"]["categories"] == ["Целевые", "Узкие"] and "Запросы с брендами конкурентов" not in plan["autotargeting"]["brands"]
        assert fake.campaigns == {}  # просмотр ничего не создаёт


@pytest.mark.parametrize("change,message", [
    ({"site_url": ""}, "сайт или ссылку"),
    ({"city": ""}, "всей России"),   # без города и без отметки «по всей России» запуск объясняет, что делать
    ({"city": "Нигдеград"}, "Не нашёл город"),
    ({"weekly_budget": 100}, "слишком мал"),
])
def test_preview_explains_problems_in_plain_words(isolated, change, message):
    with TestClient(app) as http:
        bid = setup(http, FakeDirect(), isolated, **change)
        r = http.get(f"/api/businesses/{bid}/launch/preview")
        assert r.status_code == 400 and message in r.json()["detail"]["message"]


def test_online_business_is_shown_across_russia_and_needs_no_city(isolated):
    fake = FakeDirect(regions={})     # Яндекс не нашёл бы ни одного города, и для онлайн-услуги это не важно
    with TestClient(app) as http:
        bid = setup(http, fake, isolated, city="", nationwide=True, description="Онлайн-консультации по всей стране. Запись на сайте.")
        assert http.get(f"/api/businesses/{bid}").json()["nationwide"] is True
        plan = http.get(f"/api/businesses/{bid}/launch/preview").json()
        assert plan["city"] == "вся Россия" and [w for w in plan["warnings"] if "Профиль аудитории" not in w] == []
        steps = {s["key"]: s for s in http.get(f"/api/businesses/{bid}/overview").json()["checklist"]}
        assert steps["described"]["done"] is True                                  # города нет, а бизнес описан
        assert http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True}).status_code == 200
        assert list(fake.group_regions.values()) == [[225]]                         # регион показа «Россия», а не город


def test_launch_preview_warns_when_the_budget_is_below_the_recommended_one(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated, weekly_budget=25000, target_cpa=0, avg_check=39000)   # цена обращения 3 510 ₽: для десяти обращений нужно около 56 000 ₽
        warnings = http.get(f"/api/businesses/{bid}/launch/preview").json()["warnings"]
        assert any("Рекомендуем около 56" in w.replace(" ", " ") for w in warnings)
        enough = setup_second_business_with(http, bid, weekly_budget=60000)
        assert not any("Рекомендуем" in w for w in http.get(f"/api/businesses/{enough}/launch/preview").json()["warnings"])


def setup_second_business_with(http, first_id, **change):
    """Ещё один бизнес того же владельца с теми же данными, но другими цифрами."""
    body = http.get(f"/api/businesses/{first_id}").json()
    body.update(change)
    for key in ("id", "status", "assets", "maps_org_id"):
        body.pop(key, None)
    return http.post("/api/businesses", json=body).json()["id"]


def test_a_business_with_a_city_but_marked_nationwide_never_mentions_the_city_in_ads(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated, nationwide=True)                        # город «Москва» остался в карточке, но показываем по всей России
        plan = http.get(f"/api/businesses/{bid}/launch/preview").json()
        assert all("Москва" not in v["title"] + v["text"] for v in plan["variants"])
        http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True})
        assert list(fake.group_regions.values()) == [[225]]
        assert http.put(f"/api/businesses/{bid}", json={"name": "Лотос", "kind": "Маникюрный салон", "city": "Москва", "nationwide": False}).json()["nationwide"] is False


def test_preview_needs_a_cabinet():
    with TestClient(app) as http:
        http.post("/api/auth/register", json={"email": "o@example.com", "password": "password123"})
        app.dependency_overrides[client_factory] = FakeDirect().factory()
        bid = http.post("/api/businesses", json={"name": "x", "kind": "y", "city": "Москва", "site_url": "a.ru", "weekly_budget": 20000}).json()["id"]
        r = http.get(f"/api/businesses/{bid}/launch/preview")
        assert r.status_code == 400 and "подключите рекламный кабинет" in r.json()["detail"]["message"]


def test_launch_creates_one_campaign_with_several_ads_and_sends_to_moderation(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        r = http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True})
        assert r.status_code == 200 and r.json()["created"] == 3
        (cid, campaign), = fake.campaigns.items()                                  # одна кампания на всю рекламу
        assert campaign["Status"] == "MODERATION" and campaign["Strategy"] == "WB_MAXIMUM_CLICKS"   # пока обращений мало, платим за клики
        assert 300 * MICRO <= campaign["Limit"] <= BUDGET * MICRO * 0.9 * 0.7 + 6
        assert len(fake.ads) == 3 and {a["campaign"] for a in fake.ads.values()} == {cid} and len({a["group"] for a in fake.ads.values()}) == 1
        assert ("ads", "moderate") in fake.calls
        assert "обучение" in fake.negatives[cid] and "бесплатно" in fake.negatives[cid] and "лампа" in fake.negatives[cid]   # минус-слова ниши «маникюр»
        again = http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True})
        assert again.status_code == 400 and "уже создана" in again.json()["detail"]["message"]
        ov = http.get(f"/api/businesses/{bid}/overview").json()
        assert ov["real"] and not ov["example"] and len(ov["units"]) == 3
        assert all(u["weekly_budget"] is None for u in ov["units"])               # у объявления своего бюджета нет, он общий
        assert ov["campaign"]["strategy"] == "clicks" and ov["campaign"]["weekly"] == campaign["Limit"] // MICRO and ov["campaign"]["switch_at"] == 10
        assert {u["verdict"] for u in ov["units"]} == {"На проверке в Яндексе"}
        assert any("проверяет" in n for n in ov["notices"]) and any("Метрика" in n for n in ov["notices"])


def test_launch_adds_banners_from_uploaded_photos_and_logo(isolated):
    from io import BytesIO
    from PIL import Image
    def png(color):
        out = BytesIO()
        Image.new("RGB", (900, 700), color).save(out, "PNG")
        return out.getvalue()
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated, color_main="#c2185b")
        for kind, color in (("photo", (200, 100, 50)), ("photo", (50, 100, 200)), ("logo", (255, 255, 255))):
            r = http.post(f"/api/businesses/{bid}/assets", data={"kind": kind, "tag": "work"}, files={"file": ("a.png", png(color), "image/png")})
            assert r.status_code == 200, r.text
        assert http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True}).status_code == 200
        assert len(fake.images) == 3 and fake.images[0].startswith("autobider-")
        attached = [a["ad"].get("AdImageHash") for a in fake.ads.values()]
        assert all(h and h.startswith("hash") for h in attached) and len(set(attached)) == 3  # у каждого объявления свой баннер


def test_launch_without_photos_has_no_banners(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        assert http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True}).status_code == 200
        assert fake.images == []


def test_launch_requires_advertiser_data_for_marking(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        plan = http.get(f"/api/businesses/{bid}/launch/preview").json()
        assert plan["marking"]["confirmed"] is False and "Данные рекламодателя" in plan["marking"]["help"]
        r = http.post(f"/api/businesses/{bid}/launch", json={})
        assert r.status_code == 400 and "Данные рекламодателя" in r.json()["detail"]["message"]
        assert fake.campaigns == {}  # без данных рекламодателя в Директе ничего не создаётся
        assert http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True}).status_code == 200
        steps = {s["key"]: s for s in http.get(f"/api/businesses/{bid}/overview").json()["checklist"]}
        assert steps["marking"]["done"] is True  # запомнили: второй раз спрашивать не нужно


def test_launch_cleans_up_when_direct_refuses(isolated):
    fake = FakeDirect(fail_add_after=0)  # кампанию Директ не создаст
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        r = http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True})
        assert r.status_code == 400 and "лимит кампаний" in r.json()["detail"]["message"]
        assert fake.campaigns == {} and rows(isolated, "SELECT COUNT(*) AS n FROM ad_units")[0]["n"] == 0


def test_launch_deletes_the_campaign_when_an_ad_cannot_be_created(isolated):
    fake = FakeDirect(fail_ads_after=1)  # первое объявление создалось, на втором Директ отказывает
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        r = http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True})
        assert r.status_code == 400 and "объявление" in r.json()["detail"]["message"]
        assert fake.campaigns == {}, "недоделанную кампанию нельзя оставлять в кабинете"
        assert rows(isolated, "SELECT COUNT(*) AS n FROM ad_units")[0]["n"] == 0 and rows(isolated, "SELECT COUNT(*) AS n FROM campaigns")[0]["n"] == 0


def test_launch_replaces_example_data(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        http.post(f"/api/businesses/{bid}/sim/start")
        assert http.get(f"/api/businesses/{bid}/overview").json()["example"]
        http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True})
        ov = http.get(f"/api/businesses/{bid}/overview").json()
        assert ov["real"] and not ov["example"] and len(ov["units"]) == 3


# ---------- синхронизация и движок на настоящих данных ----------

def launched(http, fake, db_path):
    bid = setup(http, fake, db_path)
    http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True})
    return bid


def test_sync_follows_moderation_payment_and_loads_stats(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        conn = connect(isolated)
        biz = lambda: conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        today = date.today().isoformat()

        sync_and_run(conn, biz(), today, transport=fake.transport)
        assert {r["state"] for r in conn.execute("SELECT state FROM ad_units")} == {"moderation"}

        fake.accept_all()  # одобрено, но счёт не пополнен
        sync_and_run(conn, biz(), today, transport=fake.transport)
        assert {r["state"] for r in conn.execute("SELECT state FROM ad_units")} == {"unpaid"}

        for c in fake.campaigns.values():
            c["State"], c["StatusPayment"] = "ON", "ALLOWED"
        fake.report_rows = [(day_add(today, -1), 0, 1000, 30, 450 * MICRO), (today, 1, 500, 10, 150 * MICRO)]
        result = sync_and_run(conn, biz(), today, transport=fake.transport)
        assert result["error"] == "" and result["synced"]["rows"] == 2
        assert {r["state"] for r in conn.execute("SELECT state FROM ad_units")} == {"active"}
        assert conn.execute("SELECT SUM(spend_micro) FROM stats_daily").fetchone()[0] == 600 * MICRO
        conn.close()

        ov = http.get(f"/api/businesses/{bid}/overview").json()
        assert ov["totals"]["spend"] == 600.0 and ov["sync"]["at"] and ov["sync"]["error"] == ""


def test_without_metrika_engine_only_guards_the_budget(isolated):
    """Обращений нет: тратить деньги слепо нельзя, но и останавливать хорошее по ошибке тоже."""
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        conn = connect(isolated)
        biz = lambda: conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        today = date.today().isoformat()
        fake.accept_all(state="ON", payment="ALLOWED")
        # на каждое объявление потрачено заметно, а обращений в данных нет
        fake.report_rows = [(day_add(today, -d), i, 1000, 40, 900 * MICRO) for i in range(3) for d in (1, 2, 3)]
        sync_and_run(conn, biz(), today, transport=fake.transport)
        assert conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 0
        assert ("campaigns", "suspend") not in fake.calls and ("ads", "suspend") not in fake.calls

        # бюджет недели исчерпан: защита срабатывает даже без Метрики и всё останавливается в Директе
        fake.report_rows = [(day_add(today, -1), i, 9000, 400, 8000 * MICRO) for i in range(3)]
        sync_and_run(conn, biz(), today, transport=fake.transport)
        assert ("campaigns", "suspend") in fake.calls
        assert {c["State"] for c in fake.campaigns.values()} == {"SUSPENDED"}
        assert {r["state"] for r in conn.execute("SELECT state FROM ad_units")} == {"paused"}
        conn.close()


def test_stop_all_on_real_ads_works_in_direct_and_holds_for_later_approvals(isolated, monkeypatch):
    import app.direct.client as client_module
    fake = FakeDirect()
    monkeypatch.setattr(client_module, "DEFAULT_TRANSPORT", fake.transport)
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        fake.accept_all(state="ON", payment="ALLOWED")
        conn = connect(isolated)
        biz = lambda: conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        sync_and_run(conn, biz(), transport=fake.transport)
        assert http.post(f"/api/businesses/{bid}/stop-all").json()["stopped"] == 3
        assert {c["State"] for c in fake.campaigns.values()} == {"SUSPENDED"} and all(a["suspended"] for a in fake.ads.values())

        # владелец пополнил счёт, Яндекс «включил» кампании сам: пока стоп-кран нажат, сервис снова их останавливает
        for c in fake.campaigns.values():
            c["State"] = "ON"
        sync_and_run(conn, biz(), transport=fake.transport)
        assert {c["State"] for c in fake.campaigns.values()} == {"SUSPENDED"}

        assert http.post(f"/api/businesses/{bid}/resume-all").json()["resumed"] == 3
        assert {c["State"] for c in fake.campaigns.values()} == {"ON"} and not any(a["suspended"] for a in fake.ads.values())
        assert biz()["status"] == "running"
        conn.close()


def test_direct_platform_pauses_single_ads_and_changes_budget_and_strategy_of_the_campaign(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        launched(http, fake, isolated)
        conn = connect(isolated)
        today = date.today().isoformat()
        camp = conn.execute("SELECT * FROM campaigns").fetchone()
        platform = DirectPlatform(conn, "tok", transport=fake.transport)
        assert platform.shared_budget

        platform.set_campaign_budget(camp["id"], 700 * MICRO, today)
        assert fake.campaigns[camp["external_id"]]["Limit"] == 700 * MICRO
        assert conn.execute("SELECT weekly_micro FROM campaigns").fetchone()[0] == 700 * MICRO

        conn.execute("UPDATE businesses SET metrika_goal_id = '55'")
        platform.set_strategy(camp["id"], "conversions", today)
        direct = fake.campaigns[camp["external_id"]]
        assert direct["Strategy"] == "WB_MAXIMUM_CONVERSION_RATE" and direct["GoalId"] == 55 and direct["Limit"] == 700 * MICRO   # лимит сохранился
        assert conn.execute("SELECT strategy FROM campaigns").fetchone()[0] == "conversions"

        fake.accept_all(state="ON", payment="ALLOWED")
        sync_and_run(conn, conn.execute("SELECT * FROM businesses").fetchone(), today, transport=fake.transport)   # объявления стали работающими
        a, b, c = [r["id"] for r in conn.execute("SELECT id FROM ad_units ORDER BY id")]
        platform.set_state(a, "paused", today)
        assert [ad["suspended"] for ad in fake.ads.values()] == [True, False, False] and direct["State"] == "ON"   # остановили одно объявление, кампания идёт
        suspends = fake.calls.count(("ads", "suspend"))
        platform.set_state(a, "paused", today)
        assert fake.calls.count(("ads", "suspend")) == suspends                       # повторная остановка Директу не отправляется
        platform.set_state(b, "paused", today)
        platform.set_state(c, "paused", today)
        assert direct["State"] == "SUSPENDED" and conn.execute("SELECT state FROM campaigns").fetchone()[0] == "paused"   # остановили последнее: кампания тоже
        platform.set_state(b, "active", today)
        assert direct["State"] == "ON" and [ad["suspended"] for ad in fake.ads.values()] == [True, False, True]
        assert conn.execute("SELECT state FROM campaigns").fetchone()[0] == "active"
        with pytest.raises(Exception, match="на всю кампанию"):
            platform.set_budget(a, 100 * MICRO, today)                                  # у объявления бюджета нет
        conn.close()


def test_worker_syncs_every_real_business_and_survives_errors(isolated):
    from app.worker import run_once
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        fake.accept_all(state="ON", payment="ALLOWED")
        fake.report_rows = [(date.today().isoformat(), 0, 1000, 30, 450 * MICRO)]
        assert run_once(transport=fake.transport) == 1
        conn = connect(isolated)
        assert conn.execute("SELECT SUM(spend_micro) FROM stats_daily").fetchone()[0] == 450 * MICRO
        assert conn.execute("SELECT last_sync_at FROM businesses WHERE id = ?", (bid,)).fetchone()[0]
        # у второго бизнеса сломан доступ: первый всё равно обработан, ошибка записана
        conn.execute("UPDATE ad_accounts SET expires_at = '2020-01-01T00:00:00+00:00'")
        conn.commit()
        assert run_once(transport=fake.transport) == 1
        assert "истёк" in conn.execute("SELECT last_sync_error FROM businesses WHERE id = ?", (bid,)).fetchone()[0]
        conn.close()


def test_sync_error_is_saved_and_shown(isolated):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched(http, fake, isolated)
        conn = connect(isolated)
        conn.execute("UPDATE ad_accounts SET expires_at = '2020-01-01T00:00:00+00:00'")
        conn.commit()
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        result = sync_and_run(conn, biz, transport=fake.transport)
        assert "истёк" in result["error"]
        conn.close()
        ov = http.get(f"/api/businesses/{bid}/overview").json()
        assert any("истёк" in n for n in ov["notices"])
        r = http.post(f"/api/businesses/{bid}/sync")
        assert r.status_code == 400 and "истёк" in r.json()["detail"]["message"]


# ---------- метки в ссылках ----------

def test_ad_links_get_utm_macros_that_direct_fills_in(isolated):
    from app.direct.launch import UTM
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        assert http.get(f"/api/businesses/{bid}/launch/preview").json()["tagged"] is True
        assert http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True}).status_code == 200
        assert {a["ad"]["Href"] for a in fake.ads.values()} == {f"https://salon.ru/?{UTM}"}
        assert "{ad_id}" in UTM and "{campaign_id}" in UTM and "{keyword}" in UTM


@pytest.mark.parametrize("url,expected", [
    ("https://x.ru/p?a=1#top", "https://x.ru/p?a=1&{utm}#top"),                       # свои параметры и якорь сохраняются
    ("https://x.ru", "https://x.ru/?{utm}"),
    ("https://x.ru/?utm_source=vk&utm_campaign=a", "https://x.ru/?utm_source=vk&utm_campaign=a"),   # свои метки владельца не трогаем
    ("salon.ru", "salon.ru"),                                                          # без схемы адрес не разобрать
    ("", ""),
])
def test_tagged_href(url, expected):
    from app.direct.launch import UTM, tagged_href
    assert tagged_href(url) == expected.replace("{utm}", UTM)