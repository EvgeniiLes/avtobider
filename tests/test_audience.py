"""Профиль аудитории: ИИ составляет, код проверяет, из него берутся фразы, минус-слова и автотаргетинг запуска."""
import asyncio
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from app.creative.texts_ai import build_prompt
from app.db import connect
from app.deps import client_factory, creative_transport
from app.direct import audience as aud
from app.direct.client import DirectError
from app.direct.runner import sync_and_run
from app.main import app
from app.routes import auth
from app.security import encrypt_token
from tests.fake_direct import FakeDirect

PROFILE = {
    "summary": "Женщины 25-45 лет, которые живут или работают в центре и хотят аккуратный маникюр в удобное время.",
    "gender": "female", "ages": ["25-34", "35-44", "70+"], "income": "above_average",
    "need": "записаться на маникюр или педикюр рядом",
    "pains": ["нет времени на долгие поиски", "боятся плохой обработки инструментов"],
    "keywords": ["записаться на маникюр", "маникюр с покрытием цена", "Педикюр аппаратный", "маникюр!!", "записаться на маникюр",
                 "очень длинная фраза из девяти слов подряд здесь вот так", "ногти", "+маникюр -дешево"],
    "negatives": ["бесплатно", "вакансия", "самостоятельно", "маникюр", "мастер-класс", "салон"],
    "confidence": "medium", "note": "Описание короткое: возраст и доход угаданы по типу услуги.",
}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.delenv("FAL_KEY", raising=False)
    auth._fails.clear()
    yield tmp_path / "test.db"
    app.dependency_overrides.clear()


def ai(content=None, status=200):
    """ИИ-заглушка: на любой запрос к чату отвечает профилем (или тем, что передали). Запросы запоминает."""
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(status, json={"choices": [{"message": {"content": json.dumps(PROFILE, ensure_ascii=False) if content is None else content}}]})

    app.dependency_overrides[creative_transport] = lambda: httpx.MockTransport(handler)
    return seen


def setup(http, fake, db_path, **business):
    http.post("/api/auth/register", json={"email": "o@example.com", "password": "password123"})
    app.dependency_overrides[client_factory] = fake.factory()
    body = {"name": "Лотос", "kind": "Маникюрный салон", "city": "Москва", "districts": "Хамовники", "weekly_budget": 20_000, "target_cpa": 800,
            "description": "Маникюр и педикюр в центре города.", "site_url": "salon.ru", "mode": "auto"}
    body.update(business)
    bid = http.post("/api/businesses", json=body).json()["id"]
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO ad_accounts (user_id, title, token_enc) VALUES (1, 'fake-login', ?)", (encrypt_token("tok", "test-secret-key"),))
    conn.commit()
    conn.close()
    return bid


# ---------- проверка профиля кодом ----------

def test_profile_keeps_only_what_direct_accepts():
    profile = aud.parse_profile("вот профиль: " + json.dumps(PROFILE, ensure_ascii=False))
    assert profile["keywords"] == ["записаться на маникюр", "маникюр с покрытием цена", "педикюр аппаратный"]   # без знаков, операторов, повторов и длинных фраз
    assert profile["ages"] == ["25-34", "35-44"] and profile["gender"] == "female" and profile["income"] == "above_average"
    assert profile["autotargeting"]["categories"]["exact"] is True and profile["edited"] is False


@pytest.mark.parametrize("raw", ["", "не json", "[]", json.dumps({"summary": "", "keywords": ["маникюр цена"]})])
def test_profile_without_a_client_description_is_refused(raw):
    assert aud.parse_profile(raw) is None


def test_unknown_values_fall_back_to_safe_ones():
    profile = aud.parse_profile(json.dumps({"summary": "Все", "gender": "robot", "income": "billions", "confidence": "sure"}))
    assert (profile["gender"], profile["income"], profile["confidence"], profile["keywords"]) == ("any", "any", "low", [])


def test_negatives_never_cut_the_business_own_phrases():
    biz = {"kind": "Маникюрный салон"}
    words = aud.effective_negatives(biz, {"keywords": ["записаться на маникюр"], "negatives": ["бесплатно", "маникюр", "самостоятельно", "салон", "мастер-класс"]})
    assert "бесплатно" in words and "самостоятельно" in words and "лампа" in words   # общие, от ИИ и по нише
    assert "маникюр" not in words and "салон" not in words                           # режут собственную услугу
    assert words.count("мастер-класс") == 1                                          # «мастер-класс» не путается с «мастер»


def test_a_school_does_not_get_study_words_as_negatives():
    words = aud.effective_negatives({"kind": "Школа обучения ИИ"}, {"keywords": ["курсы по ии для бизнеса"], "negatives": []})
    assert "обучение" not in words and "курсы" not in words and "бесплатно" in words


def test_autotargeting_is_never_fully_off():
    wanted = {"categories": {k: False for k in aud.CATEGORIES}, "brands": {k: False for k in aud.BRANDS}}
    result = aud.autotargeting_for({"autotargeting": wanted})
    assert result["categories"]["exact"] is True and result["brands"]["without"] is True and result["brands"]["competitors"] is False
    assert aud.autotargeting_for(None)["categories"] == {"exact": True, "narrow": True, "alternative": False, "accessory": False, "broader": False}


# ---------- запросы группы в Директе ----------

def settings():
    return aud.autotargeting_for(None)


def add(fake, phrases, autotargeting=True):
    async def go():
        async with fake.client() as client:
            return await client.add_keywords(55, phrases, settings() if autotargeting else None)
    return asyncio.run(go())


def test_phrases_and_autotargeting_go_in_one_call_in_the_form_direct_wants():
    fake = FakeDirect()
    result = add(fake, ["записаться на маникюр", "педикюр цена"])
    assert result == {"added": 2, "rejected": [], "autotargeting": True, "autotargeting_error": ""}
    assert [k["keyword"] for k in fake.keywords] == ["записаться на маникюр", "педикюр цена", "---autotargeting"]
    assert fake.keywords[-1]["settings"] == {
        "Categories": {"Exact": "YES", "Narrow": "YES", "Alternative": "NO", "Accessory": "NO", "Broader": "NO"},
        "BrandOptions": {"WithoutBrands": "YES", "WithAdvertiserBrand": "YES", "WithCompetitorsBrand": "NO"}}
    assert fake.calls.count(("keywords", "add")) == 1


def test_a_refused_phrase_is_skipped_and_reported():
    fake = FakeDirect()
    fake.reject_keywords = {"педикюр цена"}
    result = add(fake, ["записаться на маникюр", "педикюр цена"])
    assert result["added"] == 1 and result["autotargeting"] is True
    assert result["rejected"] == [{"phrase": "педикюр цена", "reason": "Недопустимые символы во фразе"}]


def test_group_keeps_its_phrases_when_only_autotargeting_is_refused():
    fake = FakeDirect()
    fake.reject_autotargeting = True
    result = add(fake, ["записаться на маникюр"])
    assert result["added"] == 1 and result["autotargeting"] is False and "недоступен" in result["autotargeting_error"]


def test_group_with_no_queries_at_all_is_an_error():
    fake = FakeDirect()
    fake.reject_autotargeting = True
    with pytest.raises(DirectError) as e:
        add(fake, [])
    assert "Автотаргетинг недоступен" in e.value.message
    assert add(FakeDirect(), [], autotargeting=False)["added"] == 0   # вызывать нечего: Директу ничего не отправляем


# ---------- экран и запуск ----------

def test_profile_is_built_edited_and_read_back(isolated, monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    seen = ai()
    with TestClient(app) as http:
        bid = setup(http, FakeDirect(), isolated)
        empty = http.get(f"/api/businesses/{bid}/audience").json()
        assert empty["profile"] is None and empty["keywords"] == [] and empty["ai_ready"] is True

        built = http.post(f"/api/businesses/{bid}/audience")
        assert built.status_code == 200
        data = built.json()
        assert data["profile"]["summary"].startswith("Женщины 25-45") and data["keywords"][0] == "записаться на маникюр"
        assert "маникюр" not in data["negatives"] and "бесплатно" in data["negatives"]
        assert "Маникюр и педикюр в центре города." in seen[0]["messages"][1]["content"]   # ИИ видит описание владельца
        assert http.get(f"/api/businesses/{bid}/audience").json()["profile"]["gender"] == "female"

        edited = http.put(f"/api/businesses/{bid}/audience", json={
            "keywords": ["маникюр на дом", "плохая!!! фраза", "гель лак цена"], "ages": ["35-44"], "gender": "any",
            "categories": {"alternative": True, "exact": False, "narrow": False}, "brands": {"competitors": True}}).json()
        assert edited["keywords"] == ["маникюр на дом", "гель лак цена"] and edited["ignored"]["keywords"] == ["плохая!!! фраза"]
        assert edited["profile"]["edited"] is True and edited["profile"]["ages"] == ["35-44"] and edited["profile"]["gender"] == "any"
        assert edited["autotargeting"]["categories"] == {"exact": False, "narrow": False, "alternative": True, "accessory": False, "broader": False}
        assert edited["autotargeting"]["brands"]["competitors"] is True


def test_profile_cannot_be_built_without_ai_key_or_description(isolated, monkeypatch):
    with TestClient(app) as http:
        bid = setup(http, FakeDirect(), isolated)
        assert http.post(f"/api/businesses/{bid}/audience").status_code == 503
        assert http.put(f"/api/businesses/{bid}/audience", json={"keywords": ["маникюр цена"]}).status_code == 400   # править нечего
        monkeypatch.setenv("FAL_KEY", "fal-test")
        empty = setup_empty_description(http, isolated)
        assert http.post(f"/api/businesses/{empty}/audience").status_code == 400


def setup_empty_description(http, db_path):
    return http.post("/api/businesses", json={"name": "Пусто", "kind": "Салон", "city": "Москва", "districts": "Хамовники", "weekly_budget": 20_000, "target_cpa": 800,
                                              "description": "", "site_url": "salon.ru", "mode": "auto"}).json()["id"]


def test_launch_sends_phrases_negatives_and_autotargeting_to_direct(isolated, monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    ai()
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        http.post(f"/api/businesses/{bid}/audience")
        plan = http.get(f"/api/businesses/{bid}/launch/preview").json()
        assert plan["keywords"][:3] == ["записаться на маникюр", "маникюр с покрытием цена", "педикюр аппаратный"]   # дальше фразы с районом
        assert plan["audience"]["summary"].startswith("Женщины") and plan["audience"]["confidence"] == "средняя"
        assert not any("Профиль аудитории не составлен" in w for w in plan["warnings"])

        assert http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True}).status_code == 200
        phrases = [k["keyword"] for k in fake.keywords]
        assert phrases == ["записаться на маникюр", "маникюр с покрытием цена", "педикюр аппаратный", "записаться на маникюр хамовники", "маникюр с покрытием цена хамовники",
                       "педикюр аппаратный хамовники", "---autotargeting"]   # свои фразы, затем те же с районом
        assert len({k["group"] for k in fake.keywords}) == 1 and list(fake.groups)[0] == fake.keywords[0]["group"]
        negatives = next(iter(fake.negatives.values()))
        assert "бесплатно" in negatives and "самостоятельно" in negatives and "маникюр" not in negatives
        launched = http.get(f"/api/businesses/{bid}/audience").json()["profile"]["launched"]
        assert launched == {"added": 6, "rejected": [], "autotargeting": True, "autotargeting_error": ""}   # три фразы профиля и три с районом


def test_launch_builds_the_profile_itself_when_it_is_missing(isolated, monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    seen = ai()
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        assert http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True}).status_code == 200
        assert len([x for x in seen if "аналитик целевой аудитории" in str(x)]) == 1 and fake.keywords and fake.keywords[-1]["keyword"] == "---autotargeting"   # запрос дополнений объявлений не в счёт
        assert http.get(f"/api/businesses/{bid}/audience").json()["profile"]["summary"]


def test_launch_goes_on_with_autotargeting_alone_when_ai_is_down(isolated, monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    ai(status=500)
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        assert http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True}).status_code == 200
        assert [k["keyword"] for k in fake.keywords] == ["---autotargeting"]
        assert http.get(f"/api/businesses/{bid}/audience").json()["profile"] is None


def test_refused_phrases_do_not_stop_the_launch_and_are_shown_to_the_owner(isolated, monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    ai()
    fake = FakeDirect()
    fake.reject_keywords = {"педикюр аппаратный"}
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        http.post(f"/api/businesses/{bid}/audience")
        assert http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True}).status_code == 200
        launched = http.get(f"/api/businesses/{bid}/audience").json()["profile"]["launched"]
        assert launched["added"] == 5 and launched["rejected"][0]["phrase"] == "педикюр аппаратный"   # три своих без одной отказной и три с районом


def test_group_without_any_queries_is_not_left_in_the_cabinet(isolated):
    fake = FakeDirect()
    fake.reject_autotargeting = True    # ключа ИИ нет, фраз нет, автотаргетинг Директ не принял: показывать нечем
    with TestClient(app) as http:
        bid = setup(http, fake, isolated)
        r = http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True})
        assert r.status_code == 400 and "Автотаргетинг недоступен" in r.json()["detail"]["message"]
        assert fake.campaigns == {}


def test_ad_texts_are_written_for_the_described_client(isolated):
    with TestClient(app) as http:
        bid = setup(http, FakeDirect(), isolated)
        conn = connect(isolated)
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        assert "Кому рекламируем" not in build_prompt(biz, 3)
        aud.save(conn, bid, aud.parse_profile(json.dumps(PROFILE, ensure_ascii=False)))
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        prompt = build_prompt(biz, 3)
        conn.close()
        assert "Кому рекламируем: Женщины 25-45" in prompt and "Возраст: 25-34, 35-44" in prompt and "Пол: женщины" in prompt
        assert "боятся плохой обработки инструментов" in prompt


# ---------- корректировки ставок по полу и возрасту ----------

def test_bid_slices_follow_the_profile_and_never_overlap():
    from app.direct import bids
    both = bids.slices({"gender": "female", "ages": ["25-34", "35-44"]})
    by = {(s["gender"], s["age"]): s["percent"] for s in both}
    assert len(both) == 12 and len(by) == 12
    assert by[("GENDER_FEMALE", "AGE_25_34")] == 132 and by[("GENDER_FEMALE", "AGE_55")] == 86
    assert by[("GENDER_MALE", "AGE_35_44")] == 86 and by[("GENDER_FEMALE", "AGE_0_17")] == 0 and by[("GENDER_MALE", "AGE_18_24")] == 0   # моложе целевых выключены
    only_age = bids.slices({"gender": "any", "ages": ["25-34"]})
    assert len(only_age) == 6 and all(s["gender"] is None for s in only_age) and {s["percent"] for s in only_age} == {115, 75, 0}
    assert {s["age"] for s in only_age if s["percent"] == 0} == {"AGE_0_17", "AGE_18_24"} and bids.off_ages({"ages": ["18-24"]}) == {"AGE_0_17"}
    only_gender = bids.slices({"gender": "female", "ages": []})
    assert [(s["gender"], s["percent"]) for s in only_gender] == [("GENDER_MALE", 75), ("GENDER_FEMALE", 115)]
    assert bids.slices({"gender": "any", "ages": []}) == [] and bids.slices(None) == []


def launched_with_profile(http, fake, db_path, monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    ai()
    bid = setup(http, fake, db_path)
    assert http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True}).status_code == 200
    return bid


def test_a_new_campaign_for_a_new_group_gets_the_same_bids(isolated, monkeypatch):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched_with_profile(http, fake, isolated, monkeypatch)
        assert len(fake.campaigns) == 1 and len(fake.bid_modifiers) == 12
        assert http.put(f"/api/businesses/{bid}/creatives/ads/live", json={"count": 4}).status_code == 200
        assert http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True}).status_code == 200   # четвёртое объявление: новая группа, своя кампания
        assert len(fake.campaigns) == 2
        assert {m["campaign"] for m in fake.bid_modifiers.values()} == set(fake.campaigns) and len(fake.bid_modifiers) == 24   # корректировки стоят на обеих кампаниях
        assert http.get(f"/api/businesses/{bid}/audience").json()["bids"]["stale"] is False


def test_bids_are_put_replaced_and_removed_in_the_cabinet(isolated, monkeypatch):
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = launched_with_profile(http, fake, isolated, monkeypatch)
        # сервис ставит корректировки сам при запуске, без кнопки
        data = http.get(f"/api/businesses/{bid}/audience").json()
        info = data["bids"]
        assert info["needed"] and info["launched"] and info["applied"] is not None and info["up"] == 15 and info["down"] == 25
        assert len(info["plan"]) == 12 and len(fake.bid_modifiers) == 12 and info["applied"]["slices"] == info["plan"] and info["stale"] is False
        assert {m["campaign"] for m in fake.bid_modifiers.values()} == {next(iter(fake.campaigns))}
        assert {"label": "женщины 25–34", "percent": 132} in info["applied"]["slices"]

        # владелец поправил возраст: применённое устарело, а при ближайшей синхронизации сервис заменяет набор целиком (пересечений нет)
        edited = http.put(f"/api/businesses/{bid}/audience", json={"ages": ["45-54"], "gender": "any"}).json()
        assert edited["bids"]["stale"] is True and len(edited["bids"]["plan"]) == 6
        conn = connect(isolated)
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        assert not sync_and_run(conn, biz, transport=fake.transport)["error"]
        conn.close()
        assert len(fake.bid_modifiers) == 6 and {m["gender"] for m in fake.bid_modifiers.values()} == {None}
        assert http.get(f"/api/businesses/{bid}/audience").json()["bids"]["stale"] is False

        # новый профиль не стирает то, что уже стоит в кабинете
        assert http.post(f"/api/businesses/{bid}/audience").json()["bids"]["applied"] is not None
        removed = http.delete(f"/api/businesses/{bid}/audience/bids").json()
        assert fake.bid_modifiers == {} and removed["bids"]["applied"] is None and removed["bids"]["off"] is True

        # владелец убрал сам: синхронизация их не возвращает; кнопка включает снова
        conn = connect(isolated)
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
        assert not sync_and_run(conn, biz, transport=fake.transport)["error"]
        conn.close()
        assert fake.bid_modifiers == {}
        back = http.post(f"/api/businesses/{bid}/audience/bids").json()["bids"]
        assert back["off"] is False and len(fake.bid_modifiers) == len(back["plan"]) == 12   # профиль пересоздан выше: снова 12 срезов


def test_bids_need_a_launch_a_profile_that_names_someone_and_a_cabinet_that_agrees(isolated, monkeypatch):
    fake = FakeDirect()
    with TestClient(app) as http:
        monkeypatch.setenv("FAL_KEY", "fal-test")
        ai()
        bid = setup(http, fake, isolated)
        http.post(f"/api/businesses/{bid}/audience")
        before = http.get(f"/api/businesses/{bid}/audience").json()["bids"]
        assert before["launched"] is False and before["needed"] is True
        r = http.post(f"/api/businesses/{bid}/audience/bids")
        assert r.status_code == 400 and "после запуска" in r.json()["detail"]["message"]
        http.put(f"/api/businesses/{bid}/audience", json={"gender": "any", "ages": []})
        r = http.post(f"/api/businesses/{bid}/audience/bids")
        assert r.status_code == 400 and "ни пол, ни возраст" in r.json()["detail"]["message"]
        assert http.get(f"/api/businesses/{bid}/audience").json()["bids"]["needed"] is False
        assert fake.bid_modifiers == {}