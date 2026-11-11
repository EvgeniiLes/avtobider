"""Без выбора владельца в показ идёт одно объявление, остальные запасные; у уже запущенной рекламы число идущих объявлений сохраняется."""
from datetime import date

from fastapi.testclient import TestClient

from app.db import _add_missing_columns, connect
from app.main import app
from tests.fake_direct import FakeDirect
from tests.test_cohort import launched, run
from tests.test_launch import isolated, setup  # noqa: F401  (isolated: общая подготовка базы для каждого теста)

TODAY = date.today().isoformat()


def test_without_a_choice_one_ad_goes_live_and_the_rest_are_spare(isolated, monkeypatch):
    monkeypatch.setattr("app.direct.launch.DEFAULT_LIVE", 1)   # проверяем настоящее значение, а не то, что подставляет общая подготовка тестов
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        data = http.get(f"/api/businesses/{bid}/creatives/ads").json()
        assert data["count"] == 1
        assert [a["reserve"] for a in data["ads"]][:4] == [False, True, True, True]
        assert http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True}).status_code == 200
        assert len(fake.ads) == 1                                                                   # в Директ ушло одно объявление, запасные остались у нас
        assert len(fake.campaigns) == 1


def test_the_owner_can_still_take_more_ads_into_the_campaign(isolated, monkeypatch):
    monkeypatch.setattr("app.direct.launch.DEFAULT_LIVE", 1)
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        assert http.put(f"/api/businesses/{bid}/creatives/ads/live", json={"count": 3}).status_code == 200
        data = http.get(f"/api/businesses/{bid}/creatives/ads").json()
        assert data["count"] == 3 and [a["reserve"] for a in data["ads"]][:4] == [False, False, False, True]


def test_running_ads_keep_their_number_when_the_default_changes(isolated, monkeypatch):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid, conn = launched(http, fake, isolated, separate=False, ads=3)
        run(conn, bid, fake, TODAY)
        conn.execute("UPDATE businesses SET ads_live = NULL WHERE id = ?", (bid,))                  # так выглядел бизнес до того, как число стали записывать
        conn.commit()
        _add_missing_columns(conn)
        conn.commit()
        assert conn.execute("SELECT ads_live FROM businesses WHERE id = ?", (bid,)).fetchone()[0] == 3
        conn.execute("UPDATE businesses SET ads_live = NULL WHERE id = ?", (bid,))
        conn.execute("UPDATE ad_units SET state = 'paused'")                                        # нет ни одного идущего: выбора не навязываем
        conn.commit()
        _add_missing_columns(conn)
        assert conn.execute("SELECT ads_live FROM businesses WHERE id = ?", (bid,)).fetchone()[0] is None
        conn.close()
