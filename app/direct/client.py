"""Тонкий клиент API Яндекс Директа (JSON).

Что делает: отправляет запрос, разбирает баллы из заголовка Units, превращает
ошибки в понятные тексты, не даёт слать больше 5 запросов одновременно и
повторяет запрос при сбоях на стороне Директа. Токен нигде не пишется в логи.
"""
import asyncio
import re
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


def bidding_strategy(strategy: str, weekly_limit_micro: int, goal_id: str | None = None) -> dict:
    """Стратегия показов. clicks: «Максимум кликов» с недельным лимитом. conversions: «Максимум конверсий» по цели Метрики
    (Яндекс сам подбирает ставки под обращения; ему нужно не меньше 10 конверсий в неделю). Показы в сетях всегда выключены."""
    if strategy == "conversions":
        try:
            goal = int(goal_id or 0)
        except ValueError:
            goal = 0
        if not goal:
            raise DirectError("Для стратегии по обращениям нужна цель Яндекс Метрики: выберите её на странице «Бизнес».")
        search = {"BiddingStrategyType": "WB_MAXIMUM_CONVERSION_RATE",
                  "WbMaximumConversionRate": {"WeeklySpendLimit": weekly_limit_micro, "GoalId": goal}}
    else:
        search = {"BiddingStrategyType": "WB_MAXIMUM_CLICKS", "WbMaximumClicks": {"WeeklySpendLimit": weekly_limit_micro}}
    return {"Search": search, "Network": {"BiddingStrategyType": "SERVING_OFF"}}


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


def _to_int(value: str) -> int:
    """В отчётах пустые значения записаны как «--»."""
    value = (value or "").strip()
    if value in ("", "--"):
        return 0
    return int(float(value))


def parse_tsv(text: str) -> list[dict]:
    """Разбор TSV-отчёта: первая строка названия колонок, дальше данные."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    columns = lines[0].split("\t")
    return [dict(zip(columns, line.split("\t"))) for line in lines[1:]]


DEFAULT_TRANSPORT: httpx.AsyncBaseTransport | None = None  # в тестах сюда подставляют заглушку Директа


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
        self._http = httpx.AsyncClient(transport=transport or DEFAULT_TRANSPORT, timeout=timeout)
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

    async def report(
        self,
        report_type: str,
        fields: list[str],
        date_from: str,
        date_to: str,
        name: str = "autobider",
        *,
        goals: list[str] | None = None,
        poll_seconds: float = 5.0,
        max_polls: int = 24,
    ) -> list[dict]:
        """Отчёт Директа (TSV) за период в виде списка строк.

        Деньги приходят целыми числами в микрорублях (заголовок returnMoneyInMicros не ставим).
        Если отчёт строится офлайн (коды 201 и 202), ждём и спрашиваем повторно.
        """
        body = {"params": {
            "SelectionCriteria": {"DateFrom": date_from, "DateTo": date_to},
            "FieldNames": fields,
            "ReportName": f"{name} {date_from} {date_to}",
            "ReportType": report_type,
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
            "IncludeDiscount": "NO",
        }}
        if goals:  # обращения по цели Метрики: поле Conversions станет Conversions_<цель>_<модель>
            body["params"]["Goals"] = [str(g) for g in goals]
            body["params"]["AttributionModels"] = ["AUTO"]
        headers = {
            **self._headers,
            "processingMode": "auto",
            "skipReportHeader": "true",
            "skipReportSummary": "true",
        }
        url = f"{self.base_url}/reports"
        for _ in range(max_polls + 1):
            try:
                async with self._slots:
                    response = await self._http.post(url, json=body, headers=headers)
            except httpx.TransportError:
                raise DirectError("Не удалось связаться с Яндекс Директом. Проверьте интернет и попробуйте снова.")
            self.last_units = parse_units(response.headers.get("Units")) or self.last_units
            if response.status_code in (201, 202):
                await asyncio.sleep(float(response.headers.get("retryIn", poll_seconds)))
                continue
            if response.status_code == 200:
                return parse_tsv(response.text)
            try:
                err = response.json().get("error", {})
            except ValueError:
                err = {}
            code = err.get("error_code")
            raise DirectError(
                ERROR_HINTS.get(code) or err.get("error_string") or "Не получилось получить отчёт из Яндекс Директа.",
                code=code,
                detail=err.get("error_detail", f"HTTP {response.status_code}"),
                request_id=err.get("request_id") or response.headers.get("RequestId", ""),
            )
        raise DirectError("Яндекс Директ долго собирает отчёт. Попробуйте ещё раз чуть позже.")

    # ---------- создание и управление (запись в кабинет) ----------

    @staticmethod
    def _result_item(result: dict, key: str, index: int = 0) -> dict:
        """Результат одной операции пакета. Если Директ вернул ошибку, поднимает DirectError."""
        item = result.get(key, [{}])[index]
        errors = item.get("Errors") or []
        if errors:
            first = errors[0]
            raise DirectError(
                first.get("Message") or "Яндекс Директ не принял данные.",
                code=first.get("Code"),
                detail=first.get("Details", ""),
            )
        return item

    async def create_text_campaign(self, name: str, weekly_limit_micro: int, start_date: str,
                                   counter_ids: list[int] | None = None, negative_keywords: list[str] | None = None) -> int:
        """Кампания на поиске с автоматической стратегией и жёстким недельным бюджетом.

        Показы в сетях выключены (SERVING_OFF), чтобы бюджет шёл только на поиск."""
        text_campaign = {"BiddingStrategy": bidding_strategy("clicks", weekly_limit_micro)}
        if counter_ids:
            text_campaign["CounterIds"] = {"Items": counter_ids}
        item = {"Name": name, "StartDate": start_date, "TextCampaign": text_campaign}
        if negative_keywords:
            item["NegativeKeywords"] = {"Items": negative_keywords}
        result = await self.call("campaigns", "add", {"Campaigns": [item]})
        return self._result_item(result, "AddResults")["Id"]

    async def create_ad_group(self, campaign_id: int, name: str, region_ids: list[int]) -> int:
        """Группа объявлений. Автотаргетинг на поиске включается сам: ключевые фразы не нужны."""
        result = await self.call("adgroups", "add", {"AdGroups": [{
            "Name": name, "CampaignId": campaign_id, "RegionIds": region_ids,
        }]})
        return self._result_item(result, "AddResults")["Id"]

    async def create_text_ad(
        self, group_id: int, title: str, text: str, *, title2: str = "", href: str = "", business_id: int | None = None,
    ) -> int:
        ad = {"Title": title, "Text": text, "Mobile": "NO"}
        if title2:
            ad["Title2"] = title2
        if href:
            ad["Href"] = href
        if business_id:
            ad["BusinessId"] = business_id
        if not (href or business_id):
            raise DirectError("Для объявления нужен сайт или профиль организации в Яндекс Картах.")
        result = await self.call("ads", "add", {"Ads": [{"AdGroupId": group_id, "TextAd": ad}]})
        return self._result_item(result, "AddResults")["Id"]

    async def upload_image(self, data: bytes, name: str) -> str:
        """Загружает картинку в библиотеку изображений кабинета и возвращает её хеш."""
        import base64
        result = await self.call("adimages", "add", {"AdImages": [{
            "ImageData": base64.b64encode(data).decode(), "Name": name[:255], "Type": "AUTO",
        }]})
        return self._result_item(result, "AddResults")["AdImageHash"]

    async def attach_image(self, ad_id: int, image_hash: str) -> None:
        """Привязывает загруженную картинку к объявлению (у объявления может быть одна)."""
        result = await self.call("ads", "update", {"Ads": [{"Id": ad_id, "TextAd": {"AdImageHash": image_hash}}]})
        self._result_item(result, "UpdateResults")

    async def moderate_ads(self, ad_ids: list[int]) -> None:
        result = await self.call("ads", "moderate", {"SelectionCriteria": {"Ids": ad_ids}})
        for i in range(len(ad_ids)):
            self._result_item(result, "ModerateResults", i)

    async def set_strategy(self, campaign_id: int, strategy: str, weekly_limit_micro: int, goal_id: str | None = None) -> None:
        """Меняет стратегию и недельный лимит кампании (Директ хранит её как единую перфоманс-кампанию)."""
        result = await self.call("campaigns", "update", {"Campaigns": [{
            "Id": campaign_id,
            "UnifiedCampaign": {"BiddingStrategy": bidding_strategy(strategy, weekly_limit_micro, goal_id)},
        }]})
        self._result_item(result, "UpdateResults")

    async def get_campaign(self, campaign_id: int) -> dict | None:
        """Состояние одной кампании (State, Status, StatusPayment). None, если её нет в кабинете."""
        result = await self.call("campaigns", "get", {
            "SelectionCriteria": {"Ids": [campaign_id]},
            "FieldNames": ["Id", "State", "Status", "StatusPayment", "StatusClarification"],
        })
        found = result.get("Campaigns", [])
        return found[0] if found else None

    async def list_ads(self, campaign_ids: list[int]) -> list[dict]:
        """Объявления кампаний с состоянием показа и результатом проверки. State объявления уже учитывает состояние кампании."""
        result = await self.call("ads", "get", {
            "SelectionCriteria": {"CampaignIds": campaign_ids},
            "FieldNames": ["Id", "CampaignId", "State", "Status", "StatusClarification"],
            "Page": {"Limit": PAGE_SIZE},
        })
        return result.get("Ads", [])

    async def delete_campaigns(self, campaign_ids: list[int]) -> None:
        result = await self.call("campaigns", "delete", {"SelectionCriteria": {"Ids": campaign_ids}})
        for i in range(len(campaign_ids)):
            self._result_item(result, "DeleteResults", i)

    async def set_campaigns_state(self, campaign_ids: list[int], active: bool) -> None:
        """Остановить или возобновить показы кампаний."""
        method = "resume" if active else "suspend"
        result = await self.call("campaigns", method, {"SelectionCriteria": {"Ids": campaign_ids}})
        for i in range(len(campaign_ids)):
            self._result_item(result, "ResumeResults" if active else "SuspendResults", i)

    async def set_ads_state(self, ad_ids: list[int], active: bool) -> None:
        method = "resume" if active else "suspend"
        result = await self.call("ads", method, {"SelectionCriteria": {"Ids": ad_ids}})
        for i in range(len(ad_ids)):
            self._result_item(result, "ResumeResults" if active else "SuspendResults", i)

    async def whoami(self) -> str:
        """Логин рекламодателя, на которого выдан токен. Заодно проверяет, что доступ к API есть."""
        result = await self.call("clients", "get", {"FieldNames": ["Login"]})
        clients = result.get("Clients") or []
        if not clients:
            raise DirectError("Не удалось определить ваш рекламный кабинет.")
        return clients[0]["Login"]

    async def advertiser_data(self) -> dict:
        """Что уже внесено в «Данные рекламодателя»: вид плательщика, ИНН (только факт), название или имя, телефон."""
        result = await self.call("clients", "get", {
            "FieldNames": ["Login", "Phone", "ClientInfo"],
            "TinInfoFieldNames": ["TinType", "Tin"],
            "OrganizationFieldNames": ["Name"],
        })
        c = (result.get("Clients") or [{}])[0]
        tin = c.get("TinInfo") or {}
        org = (c.get("ErirAttributes") or {}).get("Organization") or {}
        return {"login": c.get("Login", ""), "tin_type": tin.get("TinType") or "", "has_tin": bool(tin.get("Tin")),
                "org_name": org.get("Name") or "", "has_phone": bool(c.get("Phone")), "full_name": c.get("ClientInfo") or ""}

    async def set_advertiser_data(self, tin_type: str, tin: str, name: str, phone: str = "") -> None:
        """Записывает данные рекламодателя в кабинет (то же, что форма «Данные рекламодателя» в Директе)."""
        item: dict = {"TinInfo": {"TinType": tin_type, "Tin": tin}, "ErirAttributes": {"Organization": {"Name": name}}}
        if phone:
            item["Phone"] = phone
        result = await self.call("clients", "update", {"Clients": [item]})
        self._result_item(result, "UpdateResults", 0)

    async def find_region_id(self, city: str) -> int | None:
        """Номер региона Директа по названию города (Москва = 213). None, если не нашли."""
        result = await self.call("dictionaries", "get", {"DictionaryNames": ["GeoRegions"]})
        wanted = re.sub(r"^(?:г\.?|город)\s+", "", city.split(",")[0].strip().lower())   # «Москва, ул. Тверская» и «г. Москва» → «москва»
        for region in result.get("GeoRegions", []):
            if region.get("GeoRegionName", "").strip().lower() == wanted and region.get("GeoRegionType") in ("City", "Town", "Village", "Region"):
                return int(region["GeoRegionId"])
        return None

    async def min_weekly_budget(self, currency: str = "RUB") -> int | None:
        """Минимальный недельный бюджет кампании в микрорублях из справочника валют."""
        result = await self.call("dictionaries", "get", {"DictionaryNames": ["Currencies"]})
        for cur in result.get("Currencies", []):
            if cur.get("Currency") == currency:
                for prop in cur.get("Properties", []):
                    if prop.get("Name") == "MinimumWeeklySpendLimit":
                        return int(prop["Value"])
        return None

    async def ad_stats(self, date_from: str, date_to: str, goal_id: str | None = None) -> list[dict]:
        """Статистика по объявлениям и дням: показы, клики, расход (микрорубли) и обращения по цели Метрики."""
        fields = ["Date", "CampaignId", "AdId", "Impressions", "Clicks", "Cost"]
        if goal_id:
            fields.append("Conversions")
        rows = await self.report("AD_PERFORMANCE_REPORT", fields, date_from, date_to, goals=[goal_id] if goal_id else None)
        return [
            {
                "day": r["Date"],
                "campaign_id": r["CampaignId"],
                "ad_id": r["AdId"],
                "impressions": _to_int(r["Impressions"]),
                "clicks": _to_int(r["Clicks"]),
                "spend_micro": _to_int(r["Cost"]),
                "leads": sum(_to_int(v) for k, v in r.items() if k.startswith("Conversions")),
            }
            for r in rows
        ]

    async def set_counters(self, campaign_id: int, counter_ids: list[int]) -> None:
        """Привязывает счётчики Метрики к кампании (нужно, чтобы Директ считал обращения)."""
        result = await self.call("campaigns", "update", {"Campaigns": [{
            "Id": campaign_id, "UnifiedCampaign": {"CounterIds": {"Items": counter_ids}},
        }]})
        self._result_item(result, "UpdateResults")

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
