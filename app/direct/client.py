"""Тонкий клиент API Яндекс Директа (JSON).

Что делает: отправляет запрос, разбирает баллы из заголовка Units, превращает
ошибки в понятные тексты, не даёт слать больше 5 запросов одновременно и
повторяет запрос при сбоях на стороне Директа. Токен нигде не пишется в логи.
"""
import asyncio
from dataclasses import dataclass

import httpx

HOST = "https://api.direct.yandex.com"
MAX_PARALLEL = 5
RETRIES = 2
PAGE_SIZE = 1000

# Понятные подсказки к частым кодам ошибок Директа; исходный текст ошибки сохраняется.
ERROR_HINTS = {
    52: "Яндекс Директ временно не отвечает. Попробуйте ещё раз через минуту.",
    53: "Токен не подошёл: он неверный или устарел. Получите новый токен и вставьте его заново.",
    54: "У этого токена нет прав на работу с рекламным кабинетом.",
    58: "Этот аккаунт ещё не подключён к Яндекс Директу.",
    152: "Закончились баллы Директа на сегодня. Они обновятся завтра.",
    506: "Слишком много запросов одновременно. Подождите немного.",
}


class DirectError(Exception):
    """Ошибка при обращении к Директу. message можно показывать пользователю."""

    def __init__(self, message: str, *, code: int | None = None, detail: str = "", request_id: str = ""):
        super().__init__(message)
        self.message = message
        self.code = code
        self.detail = detail
        self.request_id = request_id


@dataclass
class Units:
    spent: int
    remaining: int
    daily_limit: int


def parse_units(header: str | None) -> Units | None:
    """Заголовок Units выглядит так: «10/20828/64000» (потрачено/осталось/лимит в сутки)."""
    if not header:
        return None
    try:
        spent, remaining, limit = (int(x) for x in header.split("/"))
    except ValueError:
        return None
    return Units(spent, remaining, limit)


class DirectClient:
    def __init__(
        self,
        token: str,
        *,
        api_version: str = "v501",
        login: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ):
        if not token:
            raise DirectError("Не указан токен Яндекса.")
        self.base_url = f"{HOST}/json/{api_version}"
        self.last_units: Units | None = None
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept-Language": "ru",
            "Content-Type": "application/json; charset=utf-8",
        }
        if login:
            self._headers["Client-Login"] = login
        self._http = httpx.AsyncClient(transport=transport, timeout=timeout)
        self._slots = asyncio.Semaphore(MAX_PARALLEL)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "DirectClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def call(self, service: str, method: str, params: dict) -> dict:
        """Вызывает метод сервиса и возвращает поле result из ответа."""
        url = f"{self.base_url}/{service.lower()}"
        body = {"method": method, "params": params}
        last_error: DirectError | None = None
        for attempt in range(RETRIES + 1):
            try:
                async with self._slots:
                    response = await self._http.post(url, json=body, headers=self._headers)
            except httpx.TransportError:
                last_error = DirectError("Не удалось связаться с Яндекс Директом. Проверьте интернет и попробуйте снова.")
                await asyncio.sleep(0.5 * (attempt + 1))
                continue

            self.last_units = parse_units(response.headers.get("Units")) or self.last_units
            request_id = response.headers.get("RequestId", "")

            if response.status_code >= 500:
                last_error = DirectError(
                    ERROR_HINTS[52], code=52, detail=f"HTTP {response.status_code}", request_id=request_id
                )
                await asyncio.sleep(0.5 * (attempt + 1))
                continue

            try:
                data = response.json()
            except ValueError:
                raise DirectError("Яндекс Директ ответил непонятно. Попробуйте позже.", request_id=request_id)

            if "error" in data:
                err = data["error"]
                code = err.get("error_code")
                if code == 52 and attempt < RETRIES:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise DirectError(
                    ERROR_HINTS.get(code) or err.get("error_string") or "Яндекс Директ вернул ошибку.",
                    code=code,
                    detail=err.get("error_detail", ""),
                    request_id=err.get("request_id") or request_id,
                )
            return data.get("result", {})
        assert last_error is not None
        raise last_error

    async def list_campaigns(self) -> list[dict]:
        """Все кампании кабинета (постранично)."""
        campaigns: list[dict] = []
        offset = 0
        while True:
            result = await self.call(
                "campaigns",
                "get",
                {
                    "SelectionCriteria": {},
                    "FieldNames": ["Id", "Name", "Type", "State", "Status", "StatusPayment", "Currency", "StartDate"],
                    "Page": {"Limit": PAGE_SIZE, "Offset": offset},
                },
            )
            campaigns.extend(result.get("Campaigns", []))
            if "LimitedBy" not in result:
                return campaigns
            offset = result["LimitedBy"]
