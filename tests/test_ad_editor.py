"""Новая страница «Объявления»: редактор баннера (фото и подача для объявления), снятие идеи с рекламы, прежняя версия сайта."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.creative import refs  # noqa: E402
from app.main import app  # noqa: E402
from test_creatives import Fakes, isolated, png, setup  # noqa: E402,F401


def upload(http, base, tag="work"):
    r = http.post(f"{base}/assets", data={"kind": "photo", "tag": tag}, files={"file": ("a.png", png(), "image/png")})
    assert r.status_code == 200
    return r.json()["id"]


def test_the_previous_version_of_the_site_stays_available():
    with TestClient(app) as http:
        page = http.get("/old")
        assert page.status_code == 200 and "Новая версия сайта" in page.text and "/static-v1/app.js" in page.text
        assert http.get("/static-v1/app.js").status_code == 200 and http.get("/static-v1/style.css").status_code == 200
        assert "/static/app.js" in http.get("/").text and "Новая версия сайта" not in http.get("/").text


def test_ads_carry_everything_the_editor_needs(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    Fakes()
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}"
        first, second = upload(http, base, "work"), upload(http, base, "interior")
        data = http.get(f"{base}/creatives/ads").json()
        assert [p["id"] for p in data["photos"]] == [first, second] and all(p["url"] for p in data["photos"])
        assert [x["no"] for x in data["styles"]["looks"]] == [0, 1, 2, 3, 4] and data["styles"]["refs"]            # подачи есть всегда
        assert data["ref_extra"] == 0.02
        ad = data["ads"][0]
        assert ad["photo_id"] in (first, second) and ad["look_no"] == 0 and ad["ref_id"] == data["styles"]["refs"][0]["id"]   # подача по умолчанию: первый приём ниши


def test_photo_and_style_are_saved_per_ad_and_change_the_prompt(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    fakes.keep = 10 ** 8
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}"
        first, second = upload(http, base, "work"), upload(http, base, "interior")
        put = lambda body: http.put(f"{base}/creatives/ads/0/settings", json=body)
        ads = lambda: http.get(f"{base}/creatives/ads").json()
        ref = ads()["styles"]["refs"][0]["id"]

        assert put({"asset_id": 99999}).status_code == 400 and put({"ref_id": 9999}).status_code == 400 and put({"look": 7}).status_code == 422
        assert put({"asset_id": second, "look": 3}).status_code == 200
        ad = ads()["ads"][0]
        assert (ad["photo_id"], ad["look_no"], ad["ref_id"]) == (second, 3, None)
        assert "pastel" in ad["ai"]["prompt"] and "SECOND attached image" not in ad["ai"]["prompt"]           # выбрано обычное оформление

        assert put({"ref_id": ref}).status_code == 200                                                        # переключили на подачу по референсу
        ad = ads()["ads"][0]
        assert ad["ref_id"] == ref and ad["photo_id"] == second and "SECOND attached image" in ad["ai"]["prompt"]
        assert put({"look": 1}).status_code == 200                                                            # обратно к обычному оформлению: референс снят
        assert ads()["ads"][0]["ref_id"] is None

        put({"ref_id": ref})
        fakes.claude_text = json.dumps([ads()["ads"][0]["title"], "Записаться"], ensure_ascii=False)
        assert http.post(f"{base}/creatives/ads/0/ai-banner").status_code == 200
        _h, path, body = next(r for r in fakes.requests if r[0] == "queue.fal.run")
        assert path.endswith("gpt-image-2/edit") and body.count(b"data:image/jpeg;base64") == 2               # фото и референс
        assert http.get(f"{base}/creatives").json()["spend"]["spent_usd"] == 0.09                             # 7 центов баннер и 2 за референс
        http.get(f"{base}/creatives")
        assert put({"look": 4}).status_code == 200                                                            # после отрисовки смена подачи сразу видна в промпте
        assert "SECOND attached image" not in ads()["ads"][0]["ai"]["prompt"] and "photo-hero" in ads()["ads"][0]["ai"]["prompt"]


def test_redrawing_without_changes_takes_the_next_style_and_keeps_the_old_banner_visible(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    fakes.keep = 10 ** 8
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}"
        upload(http, base, "work")
        put = lambda body: http.put(f"{base}/creatives/ads/0/settings", json=body)
        ads = lambda: http.get(f"{base}/creatives/ads").json()
        first_ref = ads()["styles"]["refs"][0]["id"]
        assert put({"ref_id": first_ref}).status_code == 200
        fakes.claude_text = json.dumps([ads()["ads"][0]["title"], "Записаться"], ensure_ascii=False)

        started = http.post(f"{base}/creatives/ads/0/ai-banner").json()
        assert started["started"] and not started.get("varied")                                      # первая отрисовка: подача как выбрана
        http.get(f"{base}/creatives")                                                                # баннер дорисовался
        ready_picture = http.get(f"{base}/creatives/ads/0/banner.jpg").content

        again = http.post(f"{base}/creatives/ads/0/ai-banner").json()                               # «Нарисовать заново» без правок
        assert again["varied"]
        ad = ads()["ads"][0]
        assert ad["ai"]["status"] == "working" and ad["ref_id"] != first_ref                         # подача сменилась и сохранилась на карточке
        assert http.get(f"{base}/creatives/ads/0/banner.jpg").content == ready_picture               # пока рисуется, виден прежний баннер, а не шаблон

        http.get(f"{base}/creatives")
        assert put({"look": 2}).status_code == 200                                                   # владелец сам выбрал оформление
        chosen = http.post(f"{base}/creatives/ads/0/ai-banner").json()
        assert not chosen.get("varied") and ads()["ads"][0]["look_no"] == 2                            # его выбор не трогаем


def test_ad_without_a_photo_is_drawn_from_scratch_and_the_reference_works_in_words(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    fakes.keep = 10 ** 8
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}"
        photo = upload(http, base, "work")
        put = lambda body: http.put(f"{base}/creatives/ads/0/settings", json=body)
        ads = lambda: http.get(f"{base}/creatives/ads").json()
        ref = ads()["styles"]["refs"][0]["id"]

        assert put({"no_photo": True}).status_code == 200
        ad = ads()["ads"][0]
        assert ad["no_photo"] is True and ad["photo_id"] is None and "prompt" not in ad["ai"]        # промпт появится после заказа
        assert put({"ref_id": ref}).status_code == 200 and ads()["ads"][0]["no_photo"] is True       # референс не возвращает фото

        fakes.claude_text = json.dumps([ad["title"], "Записаться"], ensure_ascii=False)
        assert http.post(f"{base}/creatives/ads/0/ai-banner").status_code == 200
        _h, path, body = next(r for r in fakes.requests if r[0] == "queue.fal.run")
        assert path.endswith("/gpt-image-2/edit") and body.count(b"data:image/jpeg;base64") == 1       # фото владельца не уходит, уходит только картинка референса
        assert b"design template" in body                                                            # референс задан шаблоном дизайна, чужой текст и логотип копировать нельзя
        asked = [json.loads(b)["messages"][-1]["content"] for _h, _p, b in fakes.requests if b.startswith(b"{") and b"messages" in b]
        assert any("Подача по референсу" in a and refs.note(ref) in a for a in asked)               # арт-директору приём назван: сцена строится вокруг него
        http.get(f"{base}/creatives")
        ad = ads()["ads"][0]
        assert ad["ai"]["status"] == "ready" and ad["ai"]["prompt"] and ad["ref_id"] == ref and ad["no_photo"] is True

        again = http.post(f"{base}/creatives/ads/0/ai-banner").json()                               # повтор без правок: следующий референс
        assert again["varied"] and ads()["ads"][0]["ref_id"] != ref and ads()["ads"][0]["no_photo"] is True
        http.get(f"{base}/creatives")

        assert put({"asset_id": photo}).status_code == 200                                           # выбрали фото: снова баннер из фото
        ad = ads()["ads"][0]
        assert ad["no_photo"] is False and ad["photo_id"] == photo


def test_first_order_takes_a_different_niche_reference_for_each_ad(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    Fakes(fal_done=False)
    with TestClient(app) as http:
        bid = setup(http, kind="Барбершоп", description="Мужские стрижки и оформление бороды. Работаем ежедневно с 10 до 22.")
        base = f"/api/businesses/{bid}/creatives"
        shown = [a["ref_id"] for a in http.get(f"{base}/ads").json()["ads"]]
        assert all(shown[:3]) and len(set(shown[:3])) == 3 and shown[3:] == [None] * 3   # приёмы ниши только у трёх первых объявлений, у остальных обычное оформление
        assert 29 in shown and 36 not in shown                                     # кресло барбера берём, приём про зачёркнутую цену нет: скидки не разрешены

        assert http.post(f"{base}/ads/ai-banners").json() == {"started": 6}            # заказаны все шесть объявлений (запасные тоже)
        assert [a["ref_id"] for a in http.get(f"{base}/ads").json()["ads"]] == shown   # выбранные подачи сохранились на карточках


def test_a_niche_with_two_references_gives_the_other_ads_references_of_neighbouring_niches(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    Fakes(fal_done=False)
    with TestClient(app) as http:
        bid = setup(http)    # маникюр: в каталоге два приёма, остальные берутся у соседних ниш (косметика, парикмахерские)
        base = f"/api/businesses/{bid}/creatives"
        shown = [a["ref_id"] for a in http.get(f"{base}/ads").json()["ads"]]
        assert all(shown[:3]) and len(set(shown[:3])) == 3 and set(shown[:2]) == {17, 18} and shown[3:] == [None] * 3
        assert refs.REFS[shown[2]][0] in {"cosmetics", "hair"}
        assert http.post(f"{base}/ads/ai-banners").json() == {"started": 6}
        assert [a["ref_id"] for a in http.get(f"{base}/ads").json()["ads"]] == shown


def _draw(http, base, fakes, ad):
    """Заказывает баннер и забирает готовый: подставное чтение текста возвращает только надписи этого объявления."""
    fakes.claude_text = json.dumps([ad["title"], "Записаться"], ensure_ascii=False)
    started = http.post(f"{base}/ads/{ad['index']}/ai-banner").json()
    http.get(base)
    return started


def test_redrawing_never_drops_the_reference_when_neighbours_use_all_of_them(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    monkeypatch.setattr(refs, "NEIGHBOR_NICHES", {})      # только своя ниша: шесть приёмов барбершопа на шесть объявлений, свободных нет
    monkeypatch.setattr(refs, "GENERIC_REFS", ())
    monkeypatch.setattr("app.routes.creatives.REF_ADS", 6)   # в этом тесте приёмы получают все шесть объявлений
    fakes = Fakes(fal_done=True)
    with TestClient(app) as http:
        bid = setup(http, kind="Барбершоп", description="Мужские стрижки и оформление бороды. Работаем ежедневно с 10 до 22.")
        base = f"/api/businesses/{bid}/creatives"
        ads = lambda: http.get(f"{base}/ads").json()["ads"]
        for ad in ads():
            _draw(http, base, fakes, ad)
        assert all(a["ai"]["status"] == "ready" for a in ads())
        taken = {a["ref_id"] for a in ads()}
        assert len(taken) == 6
        before = ads()[0]["ref_id"]

        first = _draw(http, base, fakes, ads()[0])                              # свободных приёмов нет, соседи заняли все: лучше повтор приёма соседа, чем рисунок без референса
        assert first["varied"] and first["varied"] != "без референса" and ads()[0]["ref_id"] in taken - {before}
        second = _draw(http, base, fakes, ads()[0])                             # и дальше идёт по кругу, а не теряет приём
        assert second["varied"] != "без референса" and ads()[0]["ref_id"] in taken
        assert {a["ref_id"] for a in ads()[1:]} == taken - {before}              # у соседей приёмы остались свои


def test_redrawing_prefers_a_free_reference_over_one_a_neighbour_uses(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    with TestClient(app) as http:
        bid = setup(http, kind="Барбершоп", description="Мужские стрижки и оформление бороды. Работаем ежедневно с 10 до 22.")
        base = f"/api/businesses/{bid}/creatives"
        ads = lambda: http.get(f"{base}/ads").json()["ads"]
        for i in (0, 1):                                                          # заказаны только два объявления: остальные приёмы свободны
            _draw(http, base, fakes, ads()[i])
        mine, neighbour = ads()[0]["ref_id"], ads()[1]["ref_id"]
        assert mine and neighbour and mine != neighbour
        assert _draw(http, base, fakes, ads()[0])["varied"]
        assert ads()[0]["ref_id"] not in (mine, neighbour)                        # новый приём: не прежний и не занятый соседом


def test_renaming_the_text_keeps_the_photo_and_style_with_the_ad(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    Fakes()
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}"
        photo = upload(http, base, "work")
        added = http.post(f"{base}/creatives/texts", json={"title": "Маникюр в центре города", "body": "Запишитесь онлайн"}).json()
        http.put(f"{base}/creatives/{added['id']}", json={"status": "approved"})
        index = next(a["index"] for a in http.get(f"{base}/creatives/ads").json()["ads"] if a["title"] == "Маникюр в центре города")
        assert http.put(f"{base}/creatives/ads/{index}/settings", json={"asset_id": photo, "look": 2}).status_code == 200
        assert http.put(f"{base}/creatives/{added['id']}", json={"title": "Маникюр рядом с метро", "body": "Запишитесь онлайн"}).status_code == 200
        ad = next(a for a in http.get(f"{base}/creatives/ads").json()["ads"] if a["title"] == "Маникюр рядом с метро")
        assert ad["photo_id"] == photo and ad["look_no"] == 2                                                 # подача осталась
        assert "Маникюр рядом с метро" in ad["ai"]["prompt"] and "Маникюр в центре города" not in ad["ai"]["prompt"]


def test_a_drawn_banner_is_redrawn_with_the_renamed_title_not_the_old_one(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}"
        upload(http, base, "work")
        added = http.post(f"{base}/creatives/texts", json={"title": "Маникюр в центре города", "body": "Запишитесь онлайн"}).json()
        http.put(f"{base}/creatives/{added['id']}", json={"status": "approved"})
        ads = lambda: http.get(f"{base}/creatives/ads").json()["ads"]
        first = next(a for a in ads() if a["title"] == "Маникюр в центре города")
        _draw(http, f"{base}/creatives", fakes, first)                                                 # баннер со старым заголовком уже нарисован
        assert http.put(f"{base}/creatives/{added['id']}", json={"title": "Маникюр рядом с метро", "body": "Запишитесь онлайн"}).status_code == 200
        renamed = next(a for a in ads() if a["title"] == "Маникюр рядом с метро")                      # номер объявления мог сдвинуться: рисуем по новому
        before = len(fakes.requests)
        _draw(http, f"{base}/creatives", fakes, renamed)
        sent = [b.decode(errors="ignore") for h, _, b in fakes.requests[before:] if h == "queue.fal.run"]
        assert sent and all("Маникюр рядом с метро" in p and "Маникюр в центре города" not in p for p in sent)


def test_idea_can_be_taken_off_the_ads(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}"
        upload(http, base, "work")
        fakes.claude_text = json.dumps([{"hypothesis": "Проверяем запись", "audience": "Занятые", "angle": "booking", "title": "Маникюр без очередей",
                                         "text": "Запишитесь онлайн", "photo": 1, "look": 1, "mood": "calm", "ref": None, "direction": ""}], ensure_ascii=False)
        idea = http.post(f"{base}/briefs/generate", json={"count": 2}).json()["briefs"][0]
        assert http.post(f"{base}/briefs/{idea['id']}/use").json()["status"] == "used"
        assert any(a["title"] == "Маникюр без очередей" and a["own"] for a in http.get(f"{base}/creatives/ads").json()["ads"])
        off = http.post(f"{base}/briefs/{idea['id']}/unuse").json()
        assert off["status"] == "idea"
        assert not any(a["title"] == "Маникюр без очередей" and a["own"] for a in http.get(f"{base}/creatives/ads").json()["ads"])
        assert http.post(f"{base}/briefs/{idea['id']}/use").json()["status"] == "used"                        # и снова можно взять


def test_a_writer_waits_for_the_database_instead_of_failing_after_five_seconds(isolated):
    from app.db import connect
    conn = connect(isolated)
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000   # пока идёт долгий запрос к ИИ, другие сохранения ждут, а не падают ошибкой
    conn.close()


def test_redraw_all_redraws_every_live_ad_with_a_new_style_and_skips_the_ones_still_drawing(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    fakes.keep = 10 ** 8
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}/creatives"
        ads = lambda: http.get(f"{base}/ads").json()["ads"]
        for ad in ads()[:3]:
            _draw(http, base, fakes, ad)                                                  # у каждого объявления свой текст на баннере
        assert [a["ai"]["status"] for a in ads()] == ["ready"] * 3 + ["none"] * 3
        assert http.post(f"{base}/ads/ai-banners").json() == {"started": 3}               # без redo заказаны только те, у кого баннера нет (запасные тоже)
        refs_before = [a["ref_id"] for a in ads()[:3]]
        assert http.post(f"{base}/ads/ai-banners?redo=true").json() == {"started": 3}     # три готовых заново, остальные три уже рисуются и пропущены
        assert [a["ai"]["status"] for a in ads()] == ["working"] * 6
        assert all(a["ref_id"] != before for a, before in zip(ads()[:3], refs_before))      # каждое взяло другую подачу
        assert http.post(f"{base}/ads/ai-banners?redo=true").json() == {"started": 0}     # пока рисуются, второй раз не заказываем
        versions = [c for c in http.get(f"{base}").json()["creatives"] if c["kind"] == "ai_banner" and c["status"] in ("draft", "approved")]
        assert len(versions) >= 3                                                           # прежние версии остались, их можно вернуть


def test_ads_report_which_reference_really_went_to_the_model(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    fakes.keep = 10 ** 8
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}/creatives"
        ads = lambda: http.get(f"{base}/ads").json()["ads"]
        assert all("ref_sent" not in a["ai"] for a in ads())                                # ничего не заказывали: сказать нечего
        _draw(http, base, fakes, ads()[0])
        first = ads()[0]["ai"]
        assert first["ref_sent"] == ads()[0]["ref_id"] and first["ref_note"] == refs.note(first["ref_sent"])   # номер и приём, которые ушли модели


def test_a_niche_without_promo_still_has_a_reference_for_every_ad():
    from app.routes.creatives import _niche_refs
    biz = {"name": "Лапа", "kind": "груминг", "description": "Стрижка собак и кошек", "allow_promo": 0}
    biz["id"] = 0
    found = _niche_refs(type("B", (), {"__getitem__": lambda self, k: biz.get(k, "")})())
    assert len(found) >= 3 and len(set(found)) == len(found)


def test_an_idea_from_the_strategist_without_a_reference_gets_a_niche_reference_unless_the_owner_chose_a_look(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    fakes.keep = 10 ** 8
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}"
        upload(http, base, "work")
        fakes.claude_text = json.dumps([{"hypothesis": "Проверяем запись", "audience": "Занятые", "angle": "booking", "title": "Маникюр без очередей",
                                         "text": "Запишитесь онлайн", "photo": 1, "look": 1, "mood": "calm", "ref": None, "direction": ""}], ensure_ascii=False)
        idea = http.post(f"{base}/briefs/generate", json={"count": 2}).json()["briefs"][0]
        assert http.post(f"{base}/briefs/{idea['id']}/use").json()["status"] == "used"
        ads = lambda: http.get(f"{base}/creatives/ads").json()["ads"]
        mine = next(a for a in ads() if a["title"] == "Маникюр без очередей")
        assert mine["own"] and mine["ref_id"] and mine["ai"]["hypothesis"] == "Проверяем запись"   # идея стратега без референса: приём ниши подобран, гипотеза осталась
        drawn = _draw(http, base + "/creatives", fakes, mine)
        mine = next(a for a in ads() if a["title"] == "Маникюр без очередей")
        assert mine["ai"]["ref_sent"] == mine["ref_id"] and mine["ai"]["hypothesis"] == "Проверяем запись"

        other = next(a for a in ads() if not a["own"] and a["index"] < 3)
        assert http.put(f"{base}/creatives/ads/{other['index']}/settings", json={"look": 2}).status_code == 200   # владелец сам выбрал оформление без референса
        assert next(a for a in ads() if a["index"] == other["index"])["ref_id"] is None


def test_the_owner_can_reset_the_reference_and_it_does_not_come_back_on_redraw(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    fakes = Fakes(fal_done=True)
    fakes.keep = 10 ** 8
    with TestClient(app) as http:
        bid = setup(http)
        base = f"/api/businesses/{bid}"
        put = lambda body: http.put(f"{base}/creatives/ads/0/settings", json=body)
        ads = lambda: http.get(f"{base}/creatives/ads").json()["ads"]
        assert ads()[0]["ref_id"]                                                                    # по умолчанию у первого объявления приём ниши
        assert put({"no_photo": True}).status_code == 200 and put({"ref_id": 0}).status_code == 200   # сброс: рисовать без образца
        assert ads()[0]["ref_id"] is None and ads()[0]["no_photo"] is True
        fakes.claude_text = json.dumps([ads()[0]["title"], "Записаться"], ensure_ascii=False)
        _draw(http, f"{base}/creatives", fakes, ads()[0])
        assert ads()[0]["ai"]["ref_sent"] is None and ads()[0]["ref_id"] is None                       # баннер нарисован без референса
        _draw(http, f"{base}/creatives", fakes, ads()[0])
        assert ads()[0]["ai"]["ref_sent"] is None and ads()[0]["ref_id"] is None                       # и «Нарисовать заново» референс не возвращает
        assert put({"ref_id": 9999}).status_code == 400
