import asyncio
import json

import httpx

from app.creative import refs


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