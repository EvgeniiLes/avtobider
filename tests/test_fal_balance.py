import asyncio

import httpx

from app.creative import fal


def test_empty_fal_balance_is_explained_instead_of_a_generic_failure():
    locked = httpx.MockTransport(lambda request: httpx.Response(403, json={"detail": "User is locked. Reason: TOP_UP."}))
    assert fal.is_locked(403, '{"detail": "User is locked. Reason: TOP_UP."}') and not fal.is_locked(403, "forbidden") and not fal.is_locked(200, "TOP_UP")
    assert asyncio.run(fal.why_down("key", "model", locked)) == fal.TOPUP_MESSAGE
    fine = httpx.MockTransport(lambda request: httpx.Response(500, text="oops"))
    assert asyncio.run(fal.why_down("key", "model", fine)) == "" and asyncio.run(fal.why_down("", "model", locked)) == ""   # другая причина или нет ключа
    try:
        asyncio.run(fal.start_image("p", "key", "fal-ai/nano-banana-2", locked))
        raise AssertionError("ожидали ошибку генерации")
    except fal.FalError as e:
        assert "закончились деньги" in e.message                                                         # а не «ключ не подошёл»