import asyncio
import json

import httpx

from app.creative import refs


def test_layout_words_drop_small_print_phones_and_icons_so_they_do_not_appear_on_the_banner():
    raw = ("The background is saturated blue. The headline is huge and cream. Small white text blocks sit in the top right. "
           "Contact information appears in small white print at the bottom, with phone icons on the left and a location pin icon on the right. "
           "A small yellow accent sits below the headline.")
    cleaned = refs.clean_layout(raw)
    assert "blue" in cleaned and "headline" in cleaned and "yellow accent" in cleaned
    assert not any(w in cleaned.lower() for w in ("phone", "icon", "contact", "print", "text blocks"))


def test_reference_layout_is_described_once_and_then_remembered(tmp_path, monkeypatch):
    monkeypatch.setattr(refs, "_cache_file", lambda: tmp_path / "ref_layouts.json")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "A bright orange background; the photo sits on the right in a rounded frame; the headline is huge, white and heavy on the left."}}]})

    transport = httpx.MockTransport(handler)
    first = asyncio.run(refs.layout_text(91, "key", "model", transport))
    assert "headline" in first and len(calls) == 1 and calls[0]["image_urls"][0].startswith("data:image/jpeg;base64,")
    assert asyncio.run(refs.layout_text(91, "key", "model", transport)) == first and len(calls) == 1   # второй раз из памяти, без запроса
    assert asyncio.run(refs.layout_text(91, "", "model", transport)) == ""                             # без ключа ИИ недоступен: описания нет, баннер идёт по картинке
    assert asyncio.run(refs.layout_text(9999, "key", "model", transport)) == ""                        # нет такого референса: пусто