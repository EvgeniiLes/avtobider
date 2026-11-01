"""Платформа «Яндекс Директ» для движка: те же действия, что у симулятора, но в настоящем кабинете.

Одна кампания на бизнес, объявления-варианты внутри неё делят недельный лимит (shared_budget). Остановить сервис может
отдельное объявление; когда не остаётся ни одного работающего, останавливается и кампания, а при запуске снова включается.
Перед каждым действием читаем состояние в кабинете и действуем только если оно отличается: повторная остановка или запуск
у Директа заканчивается ошибкой.

Методы синхронные (движок синхронный): каждое действие открывает короткое соединение с Директом.
Нельзя вызывать из async-кода внутри работающего цикла событий.
"""
import asyncio
import sqlite3

import httpx

from app.direct.client import DirectClient
from app.engine.util import EngineError


class DirectPlatform:
    shared_budget = True

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
        if row is None or row["external_campaign_id"] is None or row["external_ad_id"] is None:
            raise EngineError("Это объявление не создано в Яндекс Директе.", "not_found")
        return row

    def _campaign(self, campaign_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        if row is None or row["external_id"] is None:
            raise EngineError("Эта кампания не создана в Яндекс Директе.", "not_found")
        return row

    def set_state(self, unit_id: int, state: str, day: str) -> None:
        u = self._unit(unit_id)
        campaign_id, ad_id = int(u["external_campaign_id"]), int(u["external_ad_id"])
        remaining = self.conn.execute(
            "SELECT COUNT(*) FROM ad_units WHERE campaign_id = ? AND state = 'active' AND id != ?", (u["campaign_id"], unit_id)).fetchone()[0]

        async def go(c: DirectClient) -> str:
            ads = {int(a["Id"]): a for a in await c.list_ads([campaign_id])}
            suspended = ads.get(ad_id, {}).get("State") == "SUSPENDED"
            campaign = await c.get_campaign(campaign_id) or {}
            if state == "paused":
                if ads.get(ad_id) and not suspended:
                    await c.set_ads_state([ad_id], False)
                if remaining == 0 and campaign.get("State") == "ON":   # последнее работающее объявление: кампания тоже встаёт
                    await c.set_campaigns_state([campaign_id], False)
                return "paused"
            if campaign.get("State") == "SUSPENDED":
                await c.set_campaigns_state([campaign_id], True)
            if suspended:
                await c.set_ads_state([ad_id], True)
            return "active"

        result = self._run(go)
        self.conn.execute("UPDATE ad_units SET state = ?, last_changed_day = ? WHERE id = ?", (state, day, unit_id))
        if result == "active" or remaining == 0:
            self.conn.execute("UPDATE campaigns SET state = ? WHERE id = ?", ("active" if result == "active" else "paused", u["campaign_id"]))

    def set_budget(self, unit_id: int, budget_micro: int, day: str) -> None:
        raise EngineError("В Директе недельный лимит один на всю кампанию, у объявления своего бюджета нет.", "not_allowed")

    def touch(self, unit_id: int, day: str) -> None:
        self.conn.execute("UPDATE ad_units SET last_changed_day = ? WHERE id = ?", (day, unit_id))

    def _goal(self, campaign: sqlite3.Row) -> str | None:
        row = self.conn.execute("SELECT metrika_goal_id FROM businesses WHERE id = ?", (campaign["business_id"],)).fetchone()
        return (row["metrika_goal_id"] or None) if row else None

    def set_campaign_budget(self, campaign_id: int, weekly_micro: int, day: str) -> None:
        camp = self._campaign(campaign_id)
        self._run(lambda c: c.set_strategy(camp["external_id"], camp["strategy"], weekly_micro, self._goal(camp)))
        self.conn.execute("UPDATE campaigns SET weekly_micro = ?, last_changed_day = ? WHERE id = ?", (weekly_micro, day, campaign_id))

    def set_strategy(self, campaign_id: int, strategy: str, day: str) -> None:
        camp = self._campaign(campaign_id)
        self._run(lambda c: c.set_strategy(camp["external_id"], strategy, camp["weekly_micro"], self._goal(camp)))
        self.conn.execute("UPDATE campaigns SET strategy = ?, last_changed_day = ? WHERE id = ?", (strategy, day, campaign_id))
