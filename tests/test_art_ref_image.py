"""Арт-директор смотрит на референс картинкой: тип кадра берёт из него, а не из «обязательного сюжета» по номеру объявления (так появлялись люди там, где их нет)."""
import asyncio
import json

import httpx

from app.creative.art import art_direction

BIZ = {"kind": "Автомойка", "name": "Тритон", "description": "Мойка и детейлинг.", "visual_hint": ""}
VARIANT = {"title": "Запись на мойку", "text": "Онлайн", "angle": ""}


def run(ref_image):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        scene = "A clean hatchback in a white bathtub full of foam on a pale floor, no people, calm empty wall above for text."
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"audience": "автовладельцы", "scene": scene, "logo": False, "logo_reason": "нет"})}}]})

    art = asyncio.run(art_direction(BIZ, VARIANT, 0, [], None, False, "key", "model", httpx.MockTransport(handler), "машина в ванне с пеной", ref_image))
    return art, seen["body"]


def test_with_a_reference_image_the_art_director_sees_it_and_has_no_forced_people_scene():
    _art, body = run(b"\xff\xd8fake-jpeg")
    user = body["messages"][1]["content"]
    assert body["image_urls"][0].startswith("data:image/jpeg;base64,")
    assert "Обязательный сюжет" not in user and "Компания людей" not in user
    assert "Если в референсе нет людей, людей в сцене быть не должно" in user


def test_without_an_image_the_old_forced_plot_is_kept():
    _art, body = run(None)
    assert "image_urls" not in body and "Обязательный сюжет: Компания людей" in body["messages"][1]["content"]
