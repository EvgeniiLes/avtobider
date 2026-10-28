"""Платформа «Яндекс Директ» для движка: те же действия, что у симулятора, но в настоящем кабинете.

Методы синхронные (движок синхронный): каждое действие открывает короткое соединение с Директом.
Нельзя вызывать из async-кода внутри работающего цикла событий.
"""
import asyncio
import sqlite3

import httpx

from app.direct.client import DirectClient
from app.direct.launch import Plan, create_unit, save_unit
from app.direct.texts import alternative_variant
from app.engine.util import EngineError, day_add


class DirectPlatform:
    def __init__(self, conn: sqlite3.Connection, token: str, api_version: str = "v501", login: str = "",
                 transport: httpx.AsyncBaseTransport | None = None):
        self.conn = conn
        self._args = dict(api_version=api_version, login=login, transport=transport)
        self._token = token

    def _run(self, fn):
        async def go():
            async with DirectClient(self._token, **self._args) as client:
                return await fn(client)
        return asyncio.run(go())

    def _unit(self, unit_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM ad_units WHERE id = ?", (unit_id,)).fetchone()
        if row is None or row["external_campaign_id"] is None:
            raise EngineError("Это объявление не создано в Яндекс Директе.", "not_found")
        return row

    def set_state(self, unit_id: int, state: str, day: str) -> None:
        u = self._unit(unit_id)
        self._run(lambda c: c.set_campaigns_state([u["external_campaign_id"]], state == "active"))
        self.conn.execute("UPDATE ad_units SET state = ?, last_changed_day = ? WHERE id = ?", (state, day, unit_id))

    def set_budget(self, unit_id: int, budget_micro: int, day: str) -> None:
        u = self._unit(unit_id)
        self._run(lambda c: c.set_weekly_limit(u["external_campaign_id"], budget_micro))
        self.conn.execute("UPDATE ad_units SET budget_micro = ?, last_changed_day = ? WHERE id = ?", (budget_micro, day, unit_id))

    def touch(self, unit_id: int, day: str) -> None:
        self.conn.execute("UPDATE ad_units SET last_changed_day = ? WHERE id = ?", (day, unit_id))

    def create_variant(self, unit_id: int, day: str) -> int:
        """Второй вариант для сравнения: новая кампания с другим акцентом, отправляется на проверку."""
        a = self._unit(unit_id)
        biz = self.conn.execute("SELECT * FROM businesses WHERE id = ?", (a["business_id"],)).fetchone()
        used = {r["title"] for r in self.conn.execute("SELECT title FROM ad_units WHERE business_id = ?", (biz["id"],))}
        variant = alternative_variant(biz, used)

        async def go(client: DirectClient):
            region = await client.find_region_id(biz["city"])
            if region is None:
                raise EngineError(f"Не нашёл город «{biz['city']}» в Яндексе.", "no_region")
            from app.direct.launch import _maps_id
            plan = Plan(1, a["budget_micro"], [variant], region, biz["city"], biz["site_url"], _maps_id(biz), [])
            ids = await create_unit(client, plan, variant, f"Автобидер: {biz['name']} (вариант)", a["budget_micro"], day_add(day, 1))
            await client.moderate_ads([ids["ad_id"]])
            return ids

        ids = self._run(go)
        campaign_row = self.conn.execute("SELECT campaign_id FROM ad_units WHERE id = ?", (unit_id,)).fetchone()["campaign_id"]
        return save_unit(self.conn, biz, 0, campaign_row, variant, ids, a["budget_micro"], day, variant_of=unit_id)
