"""VK Реклама (ads.vk.com): клиент API v2 и платформа для движка.

Заготовка под ключи: подключение, список кампаний, загрузка картинки, создание кампании, пауза, дневной бюджет, статистика.
Пути и поля сверены с официальным Quick start (токен, ad_plans, content/static). Остальные методы (смена статуса, статистика) описаны
по справочнику VK Рекламы и должны быть проверены на настоящем кабинете, когда придут ключи: места помечены «СВЕРИТЬ».
"""
import asyncio
import sqlite3
import time

import httpx

from app.engine.util import EngineError

BASE = "https://ads.vk.com"


class VkError(Exception):
    """Понятная пользователю причина, почему VK Реклама не ответила как нужно."""

    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.message = message
        self.code = code


class VkAdsClient:
    def __init__(self, client_id: str, client_secret: str, transport: httpx.AsyncBaseTransport | None = None):
        self._id, self._secret = client_id.strip(), client_secret.strip()
        self._http = httpx.AsyncClient(base_url=BASE, timeout=60, transport=transport)
        self._token = ""
        self._expires = 0.0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self._http.aclose()

    async def _auth(self) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        try:
            r = await self._http.post("/api/v2/oauth2/token.json", data={
                "grant_type": "client_credentials", "client_id": self._id, "client_secret": self._secret})
        except httpx.HTTPError:
            raise VkError("Не удалось связаться с VK Рекламой. Попробуйте позже.", "network")
        if r.status_code in (400, 401, 403):
            raise VkError("VK Реклама не приняла ключи. Проверьте client_id и client_secret.", "auth")
        if r.status_code >= 400:
            raise VkError("VK Реклама сейчас недоступна. Попробуйте позже.", "unavailable")
        data = r.json()
        self._token = data.get("access_token", "")
        if not self._token:
            raise VkError("VK Реклама не выдала токен. Проверьте ключи.", "auth")
        self._expires = time.time() + int(data.get("expires_in", 86400))
        return self._token

    async def _call(self, method: str, path: str, retry: bool = True, **kw) -> dict:
        headers = {"Authorization": f"Bearer {await self._auth()}"}
        try:
            r = await self._http.request(method, path, headers=headers, **kw)
        except httpx.HTTPError:
            raise VkError("Не удалось связаться с VK Рекламой. Попробуйте позже.", "network")
        if r.status_code == 401 and retry:   # токен мог протухнуть раньше срока
            self._token = ""
            return await self._call(method, path, retry=False, **kw)
        if r.status_code == 429:
            raise VkError("VK Реклама просит делать запросы реже. Повторите через минуту.", "rate")
        if r.status_code in (402, 403):
            raise VkError("У этого кабинета VK Рекламы нет доступа к API или не хватает прав.", "forbidden")
        if r.status_code >= 400:
            raise VkError(f"VK Реклама не приняла запрос ({r.status_code}). {_why(r)}".strip(), "request")
        return r.json() if r.content else {}

    # ---- чтение ----
    async def check(self) -> dict:
        """Проверка ключей: токен получен, кабинет отвечает. Возвращает данные пользователя."""
        return await self._call("GET", "/api/v2/user.json")

    async def list_campaigns(self, limit: int = 50) -> list[dict]:
        data = await self._call("GET", "/api/v2/ad_plans.json", params={"limit": limit, "fields": "id,name,status,objective,budget_limit_day"})
        return [{"id": str(x["id"]), "name": x.get("name", ""), "status": x.get("status", ""), "kind": x.get("objective", ""),
                 "budget": x.get("budget_limit_day", "")} for x in data.get("items", [])]

    async def day_stats(self, campaign_ids: list[str], date_from: str, date_to: str) -> list[dict]:
        """Показы, клики и расход по дням. СВЕРИТЬ: путь и названия метрик по справочнику статистики на настоящем кабинете."""
        data = await self._call("GET", "/api/v2/statistics/ad_plans/day.json", params={
            "id": ",".join(campaign_ids), "date_from": date_from, "date_to": date_to, "metrics": "base"})
        out = []
        for item in data.get("items", []):
            for row in item.get("rows", []):
                base = row.get("base", {})
                out.append({"campaign_id": str(item.get("id")), "day": row.get("date"), "impressions": int(float(base.get("shows", 0) or 0)),
                            "clicks": int(float(base.get("clicks", 0) or 0)), "spent": float(base.get("spent", 0) or 0)})
        return out

    # ---- запись ----
    async def upload_image(self, data: bytes, name: str = "banner.jpg") -> int:
        """Загрузка картинки для баннера (jpg/png): возвращает id, который указывается в content баннера."""
        r = await self._call("POST", "/api/v2/content/static.json", files={"file": (name, data, "image/jpeg")})
        if "id" not in r:
            raise VkError("VK Реклама не приняла картинку.", "request")
        return int(r["id"])

    async def create_campaign(self, payload: dict) -> dict:
        """Кампания с группами и баннерами одним запросом (как в Quick start). Поля payload собирает вызывающий код."""
        return await self._call("POST", "/api/v2/ad_plans.json", json=payload)

    async def set_status(self, campaign_id: str, active: bool) -> None:
        """СВЕРИТЬ: у VK Рекламы статусы active / blocked (пауза от владельца) / deleted."""
        await self._call("POST", f"/api/v2/ad_plans/{campaign_id}.json", json={"status": "active" if active else "blocked"})

    async def set_daily_budget(self, campaign_id: str, rubles: int) -> None:
        await self._call("POST", f"/api/v2/ad_plans/{campaign_id}.json", json={"budget_limit_day": str(int(rubles))})


def _why(r: httpx.Response) -> str:
    try:
        e = r.json().get("error", {})
        return str(e.get("message") or e.get("code") or "")[:200]
    except (ValueError, AttributeError):
        return ""


class VkPlatform:
    """Действия движка над кампаниями VK Рекламы. Идентификатор кампании хранится в ad_units.external_campaign_id."""

    def __init__(self, conn: sqlite3.Connection, client_id: str, client_secret: str, transport: httpx.AsyncBaseTransport | None = None):
        self.conn = conn
        self._creds = (client_id, client_secret, transport)

    def _run(self, fn):
        async def go():
            async with VkAdsClient(*self._creds) as client:
                return await fn(client)
        try:
            return asyncio.run(go())
        except VkError as e:
            raise EngineError(e.message, e.code)

    def _unit(self, unit_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM ad_units WHERE id = ?", (unit_id,)).fetchone()
        if row is None or row["external_campaign_id"] is None:
            raise EngineError("Это объявление не создано в VK Рекламе.", "not_found")
        return row

    def set_state(self, unit_id: int, state: str, day: str) -> None:
        u = self._unit(unit_id)
        self._run(lambda c: c.set_status(str(u["external_campaign_id"]), state == "active"))
        self.conn.execute("UPDATE ad_units SET state = ?, last_changed_day = ? WHERE id = ?", (state, day, unit_id))

    def set_budget(self, unit_id: int, budget_micro: int, day: str) -> None:
        u = self._unit(unit_id)
        daily = max(1, round(budget_micro / 1_000_000 / 7))   # у VK лимит на день, у нас бюджет на неделю
        self._run(lambda c: c.set_daily_budget(str(u["external_campaign_id"]), daily))
        self.conn.execute("UPDATE ad_units SET budget_micro = ?, last_changed_day = ? WHERE id = ?", (budget_micro, day, unit_id))

    def touch(self, unit_id: int, day: str) -> None:
        self.conn.execute("UPDATE ad_units SET last_changed_day = ? WHERE id = ?", (day, unit_id))

    def create_variant(self, unit_id: int, day: str) -> int:
        raise EngineError("Второй вариант в VK Рекламе появится после проверки на настоящем кабинете.", "not_ready")
