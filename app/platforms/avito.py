"""Авито: клиент API и платформа для движка.

На Авито реклама устроена иначе, чем в Директе и VK: продвигаются уже размещённые объявления (товары и услуги), а платят за целевые
действия (просмотры контактов) по ставке. Поэтому «объявление» в нашем движке здесь это объявление на Авито, а «бюджет» это ставка
и дневной лимит продвижения.

Заготовка под ключи. Авторизация (client_credentials) и список объявлений стандартные. Пути статистики и ставок (cpxpromo) написаны
по справочнику API Авито и помечены «СВЕРИТЬ»: их нужно проверить на настоящем аккаунте, когда придут ключи.
"""
import asyncio
import sqlite3
import time

import httpx

from app.engine.util import EngineError

BASE = "https://api.avito.ru"


class AvitoError(Exception):
    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.message = message
        self.code = code


class AvitoClient:
    def __init__(self, client_id: str, client_secret: str, transport: httpx.AsyncBaseTransport | None = None):
        self._id, self._secret = client_id.strip(), client_secret.strip()
        self._http = httpx.AsyncClient(base_url=BASE, timeout=60, transport=transport)
        self._token = ""
        self._expires = 0.0
        self.user_id: int | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self._http.aclose()

    async def _auth(self) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        try:
            r = await self._http.post("/token", data={"grant_type": "client_credentials", "client_id": self._id, "client_secret": self._secret})
        except httpx.HTTPError as exc:
            raise AvitoError("Не удалось связаться с Авито. Попробуйте позже.", "network") from exc
        if r.status_code in (400, 401, 403):
            raise AvitoError("Авито не приняло ключи. Проверьте client_id и client_secret.", "auth")
        if r.status_code >= 400:
            raise AvitoError("Авито сейчас недоступно. Попробуйте позже.", "unavailable")
        data = r.json()
        self._token = data.get("access_token", "")
        if not self._token:
            raise AvitoError("Авито не выдало токен. Проверьте ключи.", "auth")
        self._expires = time.time() + int(data.get("expires_in", 3600))
        return self._token

    async def _call(self, method: str, path: str, retry: bool = True, **kw) -> dict:
        headers = {"Authorization": f"Bearer {await self._auth()}"}
        try:
            r = await self._http.request(method, path, headers=headers, **kw)
        except httpx.HTTPError as exc:
            raise AvitoError("Не удалось связаться с Авито. Попробуйте позже.", "network") from exc
        if r.status_code == 401 and retry:
            self._token = ""
            return await self._call(method, path, retry=False, **kw)
        if r.status_code == 429:
            raise AvitoError("Авито просит делать запросы реже. Повторите через минуту.", "rate")
        if r.status_code in (402, 403):
            raise AvitoError("У этого аккаунта Авито нет доступа к нужному разделу API (возможно, нужен тариф или подключение услуги).", "forbidden")
        if r.status_code >= 400:
            raise AvitoError(f"Авито не приняло запрос ({r.status_code}).", "request")
        return r.json() if r.content else {}

    async def check(self) -> dict:
        """Проверка ключей: токен получен, аккаунт отвечает. Запоминает номер пользователя для статистики."""
        data = await self._call("GET", "/core/v1/accounts/self")
        self.user_id = int(data["id"]) if data.get("id") is not None else None
        return data

    async def list_campaigns(self, limit: int = 50) -> list[dict]:
        """«Кампании» Авито это объявления аккаунта: именно их можно продвигать."""
        data = await self._call("GET", "/core/v1/items", params={"per_page": limit, "status": "active"})
        return [{"id": str(x["id"]), "name": x.get("title", ""), "status": x.get("status", ""), "kind": (x.get("category") or {}).get("name", ""),
                 "budget": x.get("price", "")} for x in data.get("resources", [])]

    async def day_stats(self, item_ids: list[str], date_from: str, date_to: str) -> list[dict]:
        """Просмотры и контакты по дням. СВЕРИТЬ: путь и поля статистики по справочнику на настоящем аккаунте."""
        if self.user_id is None:
            await self.check()
        data = await self._call("POST", f"/stats/v1/accounts/{self.user_id}/items", json={
            "dateFrom": date_from, "dateTo": date_to, "itemIds": [int(i) for i in item_ids], "periodGrouping": "day",
            "fields": ["uniqViews", "uniqContacts", "uniqFavorites"]})
        out = []
        for item in (data.get("result") or {}).get("items", []):
            for row in item.get("stats", []):
                out.append({"campaign_id": str(item.get("itemId")), "day": row.get("date"), "impressions": int(row.get("uniqViews", 0) or 0),
                            "clicks": int(row.get("uniqContacts", 0) or 0), "spent": 0.0})
        return out

    async def set_bid(self, item_id: str, bid_rub: float, limit_rub: float) -> None:
        """Ручная ставка за целевое действие и лимит. СВЕРИТЬ: тело запроса cpxpromo/1/setManual (ставка и лимит в копейках)."""
        await self._call("POST", "/cpxpromo/1/setManual", json={"itemID": int(item_id), "actionTypeID": 5,
                                                              "bidPenny": round(bid_rub * 100), "limitPenny": round(limit_rub * 100)})

    async def remove_promotion(self, item_id: str) -> None:
        """Остановить продвижение объявления. СВЕРИТЬ: cpxpromo/1/remove."""
        await self._call("POST", "/cpxpromo/1/remove", json={"itemID": int(item_id), "actionTypeID": 5})


class AvitoPlatform:
    """Действия движка над продвижением объявлений на Авито. Номер объявления хранится в ad_units.external_campaign_id."""

    def __init__(self, conn: sqlite3.Connection, client_id: str, client_secret: str, transport: httpx.AsyncBaseTransport | None = None):
        self.conn = conn
        self._creds = (client_id, client_secret, transport)

    def _run(self, fn):
        async def go():
            async with AvitoClient(*self._creds) as client:
                return await fn(client)
        try:
            return asyncio.run(go())
        except AvitoError as e:
            raise EngineError(e.message, e.code) from e

    def _unit(self, unit_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM ad_units WHERE id = ?", (unit_id,)).fetchone()
        if row is None or row["external_campaign_id"] is None:
            raise EngineError("Это объявление не найдено на Авито.", "not_found")
        return row

    def set_state(self, unit_id: int, state: str, day: str) -> None:
        u = self._unit(unit_id)
        if state == "paused":
            self._run(lambda c: c.remove_promotion(str(u["external_campaign_id"])))
        # включение на Авито = выставить ставку: это делает set_budget
        self.conn.execute("UPDATE ad_units SET state = ?, last_changed_day = ? WHERE id = ?", (state, day, unit_id))

    def set_budget(self, unit_id: int, budget_micro: int, day: str) -> None:
        u = self._unit(unit_id)
        week_rub = budget_micro / 1_000_000
        daily = max(1.0, week_rub / 7)
        bid = max(1.0, min(daily, 50.0))   # простая стартовая ставка: не больше дневного лимита; тонкая настройка появится после проверки на аккаунте
        self._run(lambda c: c.set_bid(str(u["external_campaign_id"]), bid, daily))
        self.conn.execute("UPDATE ad_units SET budget_micro = ?, state = 'active', last_changed_day = ? WHERE id = ?", (budget_micro, day, unit_id))

    def touch(self, unit_id: int, day: str) -> None:
        self.conn.execute("UPDATE ad_units SET last_changed_day = ? WHERE id = ?", (day, unit_id))

    def create_variant(self, unit_id: int, day: str) -> int:
        raise EngineError("На Авито второй вариант делается заменой текста самого объявления: эта функция появится после проверки на аккаунте.", "not_ready")
