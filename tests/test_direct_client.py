import asyncio
import json

import httpx
import pytest

from app.direct.client import DirectClient, DirectError, parse_units


def make_client(handler, **kwargs):
    return DirectClient("secret-token", transport=httpx.MockTransport(handler), **kwargs)


def ok(result, units="10/20000/64000"):
    return httpx.Response(200, json={"result": result}, headers={"Units": units, "RequestId": "r1"})


def test_parse_units():
    u = parse_units("10/20828/64000")
    assert (u.spent, u.remaining, u.daily_limit) == (10, 20828, 64000)
    assert parse_units(None) is None
    assert parse_units("мусор") is None


def test_sandbox_and_prod_urls():
    assert make_client(lambda r: ok({}), sandbox=True).base_url == "https://api-sandbox.direct.yandex.com/json/v5"
    assert make_client(lambda r: ok({}), sandbox=False).base_url == "https://api.direct.yandex.com/json/v5"


def test_request_shape_and_units():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["login"] = request.headers.get("Client-Login")
        seen["body"] = json.loads(request.content)
        return ok({"Campaigns": [{"Id": 1, "Name": "Маникюр"}]})

    async def run():
        async with make_client(handler, login="salon") as client:
            return await client.list_campaigns(), client.last_units

    campaigns, units = asyncio.run(run())
    assert seen["url"] == "https://api-sandbox.direct.yandex.com/json/v5/campaigns"
    assert seen["auth"] == "Bearer secret-token"
    assert seen["login"] == "salon"
    assert seen["body"]["method"] == "get"
    assert "Id" in seen["body"]["params"]["FieldNames"]
    assert campaigns == [{"Id": 1, "Name": "Маникюр"}]
    assert units.remaining == 20000


def test_pagination():
    offsets = []

    def handler(request):
        offset = json.loads(request.content)["params"]["Page"]["Offset"]
        offsets.append(offset)
        if offset == 0:
            return ok({"Campaigns": [{"Id": 1}], "LimitedBy": 1})
        return ok({"Campaigns": [{"Id": 2}]})

    async def run():
        async with make_client(handler) as client:
            return await client.list_campaigns()

    assert [c["Id"] for c in asyncio.run(run())] == [1, 2]
    assert offsets == [0, 1]


def test_empty_cabinet():
    async def run():
        async with make_client(lambda r: ok({})) as client:
            return await client.list_campaigns()

    assert asyncio.run(run()) == []


def test_auth_error_is_human_and_does_not_leak_token():
    def handler(request):
        return httpx.Response(
            200,
            json={"error": {"error_code": 53, "error_string": "Authorization error", "request_id": "abc"}},
        )

    async def run():
        async with make_client(handler) as client:
            await client.list_campaigns()

    with pytest.raises(DirectError) as exc:
        asyncio.run(run())
    assert exc.value.code == 53
    assert "токен" in exc.value.message.lower()
    assert "secret-token" not in exc.value.message
    assert exc.value.request_id == "abc"


def test_retries_on_server_error(monkeypatch):
    calls = []

    async def no_sleep(_):
        pass

    monkeypatch.setattr("app.direct.client.asyncio.sleep", no_sleep)

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(502)
        return ok({"Campaigns": []})

    async def run():
        async with make_client(handler) as client:
            return await client.list_campaigns()

    assert asyncio.run(run()) == []
    assert len(calls) == 3


def test_gives_up_after_retries(monkeypatch):
    async def no_sleep(_):
        pass

    monkeypatch.setattr("app.direct.client.asyncio.sleep", no_sleep)

    async def run():
        async with make_client(lambda r: httpx.Response(503)) as client:
            await client.list_campaigns()

    with pytest.raises(DirectError):
        asyncio.run(run())


def test_sandbox_down_says_so(monkeypatch):
    async def no_sleep(_):
        pass

    monkeypatch.setattr("app.direct.client.asyncio.sleep", no_sleep)

    async def run(sandbox):
        async with make_client(lambda r: httpx.Response(504), sandbox=sandbox) as client:
            await client.list_campaigns()

    with pytest.raises(DirectError) as exc:
        asyncio.run(run(True))
    assert "Песочница" in exc.value.message
    assert exc.value.detail == "HTTP 504"
    with pytest.raises(DirectError) as exc:
        asyncio.run(run(False))
    assert "Песочница" not in exc.value.message


def test_empty_token_rejected():
    with pytest.raises(DirectError):
        DirectClient("")
