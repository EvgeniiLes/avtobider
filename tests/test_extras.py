import json

import httpx
from fastapi.testclient import TestClient

from app.deps import creative_transport
from app.direct import extras
from app.main import app
from tests.fake_direct import FakeDirect
from tests.test_launch import isolated, setup  # noqa: F401  (фикстура isolated нужна этому файлу)


class Owner(dict):
    """Минимальная строка бизнеса для проверки дополнений."""


def biz(description="Груминг собак. Работаем 7 дней в неделю с 9 до 21. Бережно, без седативных."):
    return {"name": "Пушистик", "kind": "Груминг", "city": "Москва", "nationwide": 0, "description": description, "address": "", "districts": "",
            "allow_promo": 0, "avg_check_micro": 0, "visual_hint": "", "tone": ""}


def test_extras_keep_only_what_direct_accepts_and_the_owner_said():
    raw = {"title2": ["Запись онлайн", "ЗАПИСЬ ОНЛАЙН СЕГОДНЯ", "Скидка 50% всем!", "Этот заголовок слишком длинный для второго заголовка"],
           "callouts": ["Работаем 7 дней", "Без седативных", "Скидка 20%", "Гарантия результата", "Очень длинное уточнение сверх лимита", "Работаем 7 дней"],
           "display_path": "Груминг собак!"}
    out = extras.clean(raw, biz(), ["Груминг в Адлере", "Груминг собак", "Груминг кошек", "Груминг и стрижка"])
    assert out["title2"] == {"груминг в адлере": "Запись онлайн"}                       # верхний регистр, скидка и длинное отброшены
    assert out["callouts"] == ["Работаем 7 дней", "Без седативных"]                       # скидка и гарантия не из описания, повтор и длинное убраны
    assert out["display_path"] == "груминг-собак"
    assert extras.covers(out, ["Груминг в Адлере", "Груминг собак"]) and not extras.covers(out, ["Новый заголовок"])


def test_launch_puts_second_title_callouts_and_display_path_into_the_ads(isolated, monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    answer = json.dumps({"title2": ["Запись онлайн", "Работаем без выходных", "Бережно к питомцу"], "callouts": ["Запись онлайн", "Мастера с опытом"],
                         "display_path": "запись"}, ensure_ascii=False)
    app.dependency_overrides[creative_transport] = lambda: httpx.MockTransport(
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": answer}}]}))
    fake = FakeDirect()
    with TestClient(app) as http:
        bid = setup(http, fake, isolated, description="Маникюр и педикюр в центре города. Запись онлайн. Мастера с опытом.")
        preview = http.get(f"/api/businesses/{bid}/launch/preview").json()
        assert preview["extras"]["display_path"] == "запись" and "Мастера с опытом" in preview["extras"]["callouts"]
        assert http.post(f"/api/businesses/{bid}/launch", json={"marking_confirmed": True}).status_code == 200
        assert {c["text"] for c in fake.adextensions.values()} == {"Запись онлайн", "Мастера с опытом"}
        ads = [a["ad"] for a in fake.ads.values()]
        assert len(ads) == 3 and all(a.get("DisplayUrlPath") == "запись" and len(a["AdExtensionIds"]) == 2 for a in ads)
        assert sum(1 for a in ads if a.get("Title2")) >= 1 and all(len(a.get("Title2", "")) <= 30 for a in ads)