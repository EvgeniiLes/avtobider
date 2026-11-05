import json

from fastapi.testclient import TestClient

from app.direct import audience as aud
from app.direct.groups import build_groups, group_sizes, score
from app.main import app
from tests.fake_direct import FakeDirect
from tests.test_audience import ai, isolated, setup  # noqa: F401  (фикстура isolated нужна этому файлу)
from tests.test_launch import rows

INTENTS_ANSWER = {
    "summary": "Женщины 25-45 лет, которые ищут аккуратный уход за руками и ногами.", "gender": "female", "ages": ["25-34"], "income": "any",
    "need": "записаться на уход", "pains": ["нет времени"], "confidence": "medium", "note": "",
    "keywords": ["записаться на маникюр", "маникюр с покрытием цена", "педикюр аппаратный", "записаться на педикюр"],
    "intents": [{"name": "Маникюр", "keywords": ["записаться на маникюр", "маникюр с покрытием цена"]},
                {"name": "Педикюр", "keywords": ["педикюр аппаратный", "записаться на педикюр"]}],
    "negatives": ["бесплатно", "вакансия"],
}


def test_profile_keeps_intents_and_follows_the_owners_edits():
    profile = aud.parse_profile(json.dumps(INTENTS_ANSWER, ensure_ascii=False))
    assert [i["name"] for i in profile["intents"]] == ["Маникюр", "Педикюр"] and len(profile["keywords"]) == 4
    assert [g["name"] for g in aud.intents_for(profile)] == ["Маникюр", "Педикюр"]
    edited = aud.apply_edits(profile, {"keywords": ["записаться на маникюр", "маникюр с покрытием цена", "гель лак цена"]})   # педикюр убрали, добавили новую фразу
    groups = aud.intents_for(edited)
    assert len(groups) == 1 and "гель лак цена" in groups[0]["keywords"]                           # осталась одна группа: все фразы вместе, как раньше
    assert aud.intents_for(None) == [] and aud.intents_for({"keywords": ["маникюр цена"]})[0]["keywords"] == ["маникюр цена"]


def test_ads_go_to_the_group_of_their_service_with_at_least_two_ads_each():
    intents = [{"name": "Стрижка собак", "keywords": ["стрижка собак цена"]}, {"name": "Груминг кошек", "keywords": ["груминг кошек запись"]},
               {"name": "Тримминг", "keywords": ["тримминг собак"]}]
    ads = [{"title": "Стрижка собак в Адлере", "text": "Запись онлайн"}, {"title": "Груминг кошек без стресса", "text": "Бережно"},
           {"title": "Стрижка собак любых пород", "text": "Опытные мастера"}, {"title": "Груминг кошек рядом", "text": "Запишитесь"}]
    groups = build_groups(ads, intents)
    assert [g["intent"] for g in groups] == ["Стрижка собак", "Груминг кошек"]                        # на четыре объявления две группы: по два
    assert [len(g["variants"]) for g in groups] == [2, 2] and groups[0]["indexes"] == [0, 2] and groups[1]["indexes"] == [1, 3]
    assert score(ads[0], intents[0]) > score(ads[0], intents[1])
    assert len(build_groups(ads[:3], intents)) == 1 and build_groups(ads[:3], intents)[0]["intent"] == ""   # три объявления: одна общая группа
    assert len(build_groups(ads, [intents[0]])) == 2 and group_sizes(5) == [3, 2]                      # одно намерение: прежняя схема


def test_launch_creates_a_campaign_per_service_with_its_own_phrases(isolated, monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    ai(json.dumps(INTENTS_ANSWER, ensure_ascii=False))
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        assert http.post(f"/api/businesses/{bid}/audience").status_code == 200                       # профиль с намерениями составлен
        assert http.put(f"/api/businesses/{bid}/creatives/ads/live", json={"count": 4}).status_code == 200
        preview = http.get(f"/api/businesses/{bid}/launch/preview").json()
        assert preview["groups"] == 2 and preview["group_names"] == ["Маникюр", "Педикюр"] and preview["total"] == preview["weekly"] * 2
        assert http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True}).status_code == 200
        names = sorted(c["Name"] for c in fake.campaigns.values())
        assert len(names) == 2 and all("—" in n for n in names) and any("Маникюр" in n for n in names) and any("Педикюр" in n for n in names)
        phrases = {}
        for k in fake.keywords:
            if k["keyword"] != "---autotargeting":
                phrases.setdefault(k["group"], set()).add(k["keyword"])
        sets = list(phrases.values())
        assert len(sets) == 2 and any("записаться на маникюр" in s for s in sets) and any("педикюр аппаратный" in s for s in sets)
        assert not (sets[0] & sets[1] - {k for k in sets[0] if "хамовники" in k})                    # у групп разные фразы (общих нет)
        assert {r["intent"] for r in rows(isolated, "SELECT intent FROM campaigns")} == {"Маникюр", "Педикюр"}

def test_a_new_ad_joins_the_group_with_room_instead_of_opening_a_new_campaign(isolated, monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    ai(json.dumps(INTENTS_ANSWER, ensure_ascii=False))
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        assert http.post(f"/api/businesses/{bid}/audience").status_code == 200
        assert http.put(f"/api/businesses/{bid}/creatives/ads/live", json={"count": 4}).status_code == 200
        assert http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True}).status_code == 200
        assert http.put(f"/api/businesses/{bid}/creatives/ads/live", json={"count": 5}).status_code == 200
        plan = http.get(f"/api/businesses/{bid}/launch/preview").json()
        assert plan["adding"] and plan["groups"] == 0                                              # в группах по два объявления, место есть: новая кампания не нужна
        assert http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True}).status_code == 200
        assert len(fake.campaigns) == 2 and len(fake.ads) == 5
