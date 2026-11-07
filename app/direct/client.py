"""Тонкий клиент API Яндекс Директа (JSON).

Что делает: отправляет запрос, разбирает баллы из заголовка Units, превращает
ошибки в понятные тексты, не даёт слать больше 5 запросов одновременно и
повторяет запрос при сбоях на стороне Директа. Токен нигде не пишется в логи.
"""
import asyncio
import hashlib
import re
from dataclasses import dataclass

import httpx

HOST = "https://api.direct.yandex.com"
MAX_PARALLEL = 5
RETRIES = 2
PAGE_SIZE = 1000
AUTOTARGETING_KEYWORD = "---autotargeting"   # так в API задаётся автотаргетинг группы
RUSSIA_REGION_ID = 225  # «Россия» в справочнике регионов Директа (Москва 213): для онлайн-услуг без города

# Понятные подсказки к частым кодам ошибок Директа; исходный текст ошибки сохраняется.
ERROR_HINTS = {
    52: "Яндекс Директ временно не отвечает. Попробуйте ещё раз через минуту.",
    53: "Токен не подошёл: он неверный или устарел. Получите новый токен и вставьте его заново.",
    54: "У этого токена нет прав на работу с рекламным кабинетом.",
    58: "Этот аккаунт ещё не подключён к Яндекс Директу.",
    152: "Закончились баллы Директа на сегодня. Они обновятся завтра.",
    506: "Слишком много запросов одновременно. Подождите немного.",
    7001: "Директ не принимает больше объектов такого вида, например больше трёх объявлений в одной группе. Сервис раскладывает объявления по группам сам: повторите запуск, а если ошибка осталась, покажите её в поддержке.",
    8800: "Директ не нашёл нужный объект: его, скорее всего, удалили в кабинете. Обновите данные кнопкой «Обновить данные» на главной.",
}


def bidding_strategy(strategy: str, weekly_limit_micro: int, goal_id: str | None = None) -> dict:
    """Стратегия показов. clicks: «Максимум кликов» с недельным лимитом. conversions: «Максимум конверсий» по цели Метрики
    (Яндекс сам подбирает ставки под обращения; ему нужно не меньше 10 конверсий в неделю). Сети (сайты-партнёры, Карты и др.) идут
    следом за поиском (NETWORK_DEFAULT): недельный лимит один на все площадки."""
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
    return {"Search": search, "Network": {"BiddingStrategyType": "NETWORK_DEFAULT"}}


class DirectError(Exception):
    """Ошибка при обращении к Директу. message можно показывать пользователю."""

    def __init__(self, message: str, *, code: int | None = None, detail: str = "", request_id: str = ""):
        super().__init__(message)
        self.message = message
        self.code = code
        self.detail = detail
        self.request_id = request_id

    def full_message(self) -> str:
        """Текст для экрана: наше объяснение и то, что ответил сам Директ (подробности, код, номер запроса), чтобы ошибку можно было разобрать или показать в поддержке."""
        extra = []
        if self.detail and self.detail not in self.message:
            extra.append(self.detail)
        if self.code:
            extra.append(f"код ошибки {self.code}")
        if self.request_id:
            extra.append(f"запрос {self.request_id}")
        return f"{self.message} Ответ Директа: {'; '.join(extra)}." if extra else self.message


@dataclass
class Units:
    spent: int
    remaining: int
    daily_limit: int


def report_name(name: str, report_type: str, fields: list[str], date_from: str, date_to: str, goals: list[str] | None) -> str:
    """Название отчёта для Директа. Он не принимает второй отчёт с тем же названием, но другими параметрами (ошибка 4000, пока первый в очереди),
    поэтому в название входит отпечаток типа, полей и целей: разные запросы за одни даты не мешают друг другу."""
    stamp = hashlib.md5("|".join([report_type, ",".join(fields), ",".join(map(str, goals or []))]).encode()).hexdigest()[:8]
    return f"{name} {date_from} {date_to} {stamp}"


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
            "ReportName": report_name(name, report_type, fields, date_from, date_to, goals),
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
                ERROR_HINTS.get(first.get("Code")) or first.get("Message") or "Яндекс Директ не принял данные.",   # сначала что делать, исходный текст Директа остаётся в подробностях
                code=first.get("Code"),
                detail=first.get("Details") or (first.get("Message", "") if first.get("Code") in ERROR_HINTS else ""),
            )
        return item

    async def create_text_campaign(self, name: str, weekly_limit_micro: int, start_date: str,
                                   counter_ids: list[int] | None = None, negative_keywords: list[str] | None = None) -> int:
        """Кампания с автоматической стратегией и жёстким недельным бюджетом: поиск, Карты и сайты-партнёры.

        Сети идут следом за поиском (NETWORK_DEFAULT), недельный лимит один на все площадки."""
        text_campaign = {"BiddingStrategy": bidding_strategy("clicks", weekly_limit_micro)}
        if counter_ids:
            text_campaign["CounterIds"] = {"Items": counter_ids}
        item = {"Name": name, "StartDate": start_date, "TextCampaign": text_campaign}
        if negative_keywords:
            item["NegativeKeywords"] = {"Items": negative_keywords}
        result = await self.call("campaigns", "add", {"Campaigns": [item]})
        return self._result_item(result, "AddResults")["Id"]

    async def create_ad_group(self, campaign_id: int, name: str, region_ids: list[int]) -> int:
        """Группа объявлений. Запросы для неё задаём отдельно (add_keywords): фразами и явным автотаргетингом."""
        result = await self.call("adgroups", "add", {"AdGroups": [{
            "Name": name, "CampaignId": campaign_id, "RegionIds": region_ids,
        }]})
        return self._result_item(result, "AddResults")["Id"]

    @staticmethod
    def _auto_settings(autotargeting: dict) -> dict:
        yes = lambda value: "YES" if value else "NO"
        categories, brands = autotargeting["categories"], autotargeting["brands"]
        return {"Categories": {"Exact": yes(categories["exact"]), "Narrow": yes(categories["narrow"]), "Alternative": yes(categories["alternative"]),
                               "Accessory": yes(categories["accessory"]), "Broader": yes(categories["broader"])},
                "BrandOptions": {"WithoutBrands": yes(brands["without"]), "WithAdvertiserBrand": yes(brands["own"]),
                                 "WithCompetitorsBrand": yes(brands["competitors"])}}

    async def group_keywords(self, group_ids: list[int]) -> dict[int, set[str]]:
        """Фразы, которые уже стоят в группах: {группа: набор}. Автотаргетинг в список не входит."""
        found: dict[int, set[str]] = {g: set() for g in group_ids}
        if not group_ids:
            return found
        result = await self.call("keywords", "get", {"SelectionCriteria": {"AdGroupIds": group_ids}, "FieldNames": ["Id", "AdGroupId", "Keyword"],
                                                     "Page": {"Limit": PAGE_SIZE}})
        for k in result.get("Keywords", []):
            if k.get("Keyword") and k["Keyword"] != AUTOTARGETING_KEYWORD:
                found.setdefault(k["AdGroupId"], set()).add(k["Keyword"].lower())
        return found

    async def keyword_serving(self, group_ids: list[int]) -> tuple[int, int]:
        """(фраз в группах, из них с малым числом показов). Статус показа: ELIGIBLE (показывается) и RARELY_SERVED («мало показов»: фраза не участвует в аукционе)."""
        if not group_ids:
            return 0, 0
        result = await self.call("keywords", "get", {"SelectionCriteria": {"AdGroupIds": group_ids}, "FieldNames": ["Id", "AdGroupId", "Keyword", "ServingStatus"],
                                                     "Page": {"Limit": PAGE_SIZE}})
        items = [k for k in result.get("Keywords", []) if k.get("Keyword") and k["Keyword"] != AUTOTARGETING_KEYWORD]
        return len(items), sum(1 for k in items if k.get("ServingStatus") == "RARELY_SERVED")

    async def add_ad_extras(self, ad_id: int, title2: str = "", display_path: str = "", callout_ids: list[int] | None = None) -> None:
        """Второй заголовок, отображаемая ссылка и уточнения готовому объявлению (уточнения добавляются к тем, что уже есть)."""
        text_ad: dict = {}
        if title2:
            text_ad["Title2"] = title2
        if display_path:
            text_ad["DisplayUrlPath"] = display_path
        if callout_ids:
            text_ad["CalloutSetting"] = {"AdExtensions": [{"AdExtensionId": i, "Operation": "ADD"} for i in callout_ids]}
        if not text_ad:
            return
        result = await self.call("ads", "update", {"Ads": [{"Id": ad_id, "TextAd": text_ad}]})
        self._result_item(result, "UpdateResults")

    async def autotargeting_ids(self, group_ids: list[int]) -> dict[int, int]:
        """Номер «ключевой фразы» автотаргетинга в каждой группе: {группа: номер}. Нужен, чтобы менять его настройки."""
        if not group_ids:
            return {}
        result = await self.call("keywords", "get", {"SelectionCriteria": {"AdGroupIds": group_ids}, "FieldNames": ["Id", "AdGroupId", "Keyword"],
                                                     "Page": {"Limit": PAGE_SIZE}})
        return {k["AdGroupId"]: k["Id"] for k in result.get("Keywords", []) if k.get("Keyword") == AUTOTARGETING_KEYWORD}

    async def update_autotargeting(self, keyword_id: int, autotargeting: dict) -> None:
        """Меняет категории и бренды автотаргетинга у готовой группы."""
        result = await self.call("keywords", "update", {"Keywords": [{"Id": keyword_id, "AutotargetingSettings": self._auto_settings(autotargeting)}]})
        self._result_item(result, "UpdateResults")

    async def add_keywords(self, group_id: int, phrases: list[str], autotargeting: dict | None = None) -> dict:
        """Ключевые фразы группы и автотаргетинг одним вызовом. Автотаргетинг в API это особая фраза `---autotargeting`
        с настройками категорий. Фразу, которую Директ не принял, пропускаем и возвращаем в rejected. Если не принято
        вообще ничего (ни фраз, ни автотаргетинга), группа осталась бы без запросов, и это ошибка."""
        items = [{"AdGroupId": group_id, "Keyword": p} for p in phrases]
        if autotargeting is not None:
            items.append({"AdGroupId": group_id, "Keyword": AUTOTARGETING_KEYWORD, "AutotargetingSettings": self._auto_settings(autotargeting)})
        if not items:
            return {"added": 0, "rejected": [], "autotargeting": False, "autotargeting_error": ""}
        result = await self.call("keywords", "add", {"Keywords": items})
        results = result.get("AddResults", [])
        added, rejected, auto, auto_error = 0, [], False, ""
        for index, item in enumerate(items):
            errors = (results[index] if index < len(results) else {}).get("Errors") or []
            message = (errors[0].get("Message") or "Яндекс Директ не принял данные.") if errors else ""
            if item["Keyword"] == AUTOTARGETING_KEYWORD:
                auto, auto_error = not errors, message
            elif errors:
                rejected.append({"phrase": item["Keyword"], "reason": message})
            else:
                added += 1
        if autotargeting is not None and not added and not auto:   # группа осталась бы совсем без запросов (при добавлении в готовую группу это не так)
            raise DirectError(auto_error or (rejected[0]["reason"] if rejected else "Яндекс Директ не принял ключевые фразы."))
        return {"added": added, "rejected": rejected, "autotargeting": auto, "autotargeting_error": auto_error}

    async def add_callouts(self, texts: list[str]) -> list[int]:
        """Уточнения («Работаем без выходных») в кабинет. Возвращает номера принятых; что Директ не принял, пропускаем: уточнения не обязательны."""
        if not texts:
            return []
        result = await self.call("adextensions", "add", {"AdExtensions": [{"Callout": {"CalloutText": t}} for t in texts]})
        return [item["Id"] for item in result.get("AddResults", []) if item.get("Id") and not item.get("Errors")]

    async def create_text_ad(
        self, group_id: int, title: str, text: str, *, title2: str = "", href: str = "", business_id: int | None = None, erir_description: str = "",
        display_path: str = "", extension_ids: list[int] | None = None,
    ) -> int:
        ad = {"Title": title, "Text": text, "Mobile": "NO"}
        if erir_description:
            ad["ErirAdDescription"] = erir_description   # описание объекта продвижения: без него Директ ограничивает показы по закону «О рекламе»
        if title2:
            ad["Title2"] = title2
        if display_path and href:
            ad["DisplayUrlPath"] = display_path   # только вместе со ссылкой на сайт
        if extension_ids:
            ad["AdExtensionIds"] = extension_ids
        if href:
            ad["Href"] = href
        if business_id:
            ad["BusinessId"] = business_id
        if not (href or business_id):
            raise DirectError("Для объявления нужен сайт или профиль организации в Яндекс Картах.")
        result = await self.call("ads", "add", {"Ads": [{"AdGroupId": group_id, "TextAd": ad}]})
        return self._result_item(result, "AddResults")["Id"]

    async def create_responsive_ad(self, group_id: int, titles: list[str], texts: list[str], *, href: str = "", business_id: int | None = None,
                                   erir_description: str = "", display_path: str = "", extension_ids: list[int] | None = None) -> int:
        """Комбинаторное объявление: несколько заголовков (до 7) и текстов (до 3) в одном, Директ сам подбирает лучшую комбинацию для каждого показа."""
        ad: dict = {"Titles": titles, "Texts": texts}
        if href:
            ad["Href"] = href
        if business_id:
            ad["BusinessId"] = business_id
        if erir_description:
            ad["ErirAdDescription"] = erir_description
        if display_path and href:
            ad["DisplayUrlPath"] = display_path
        if extension_ids:
            ad["AdExtensionIds"] = extension_ids
        if not (href or business_id):
            raise DirectError("Для объявления нужен сайт или профиль организации в Яндекс Картах.")
        result = await self.call("ads", "add", {"Ads": [{"AdGroupId": group_id, "ResponsiveAd": ad}]})
        return self._result_item(result, "AddResults")["Id"]

    async def attach_responsive_images(self, ad_id: int, hashes: list[str]) -> None:
        """Картинки комбинаторного объявления (до пяти)."""
        result = await self.call("ads", "update", {"Ads": [{"Id": ad_id, "ResponsiveAd": {"AdImageHashes": hashes[:5]}}]})
        self._result_item(result, "UpdateResults")

    async def upload_image(self, data: bytes, name: str) -> str:
        """Загружает картинку в библиотеку изображений кабинета и возвращает её хеш."""
        import base64
        result = await self.call("adimages", "add", {"AdImages": [{
            "ImageData": base64.b64encode(data).decode(), "Name": name[:255], "Type": "AUTO",
        }]})
        return self._result_item(result, "AddResults")["AdImageHash"]

    async def update_text_ad(self, ad_id: int, title: str, text: str, title2: str | None = None) -> None:
        """Меняет заголовок и текст готового объявления (после правки Директ снова проверяет его). title2: None оставляет второй заголовок как есть."""
        ad: dict = {"Title": title, "Text": text}
        if title2:
            ad["Title2"] = title2
        result = await self.call("ads", "update", {"Ads": [{"Id": ad_id, "TextAd": ad}]})
        self._result_item(result, "UpdateResults")

    async def detach_image(self, ad_id: int) -> None:
        """Убирает картинку у объявления (у нового текста старый баннер с прежней надписью не нужен)."""
        result = await self.call("ads", "update", {"Ads": [{"Id": ad_id, "TextAd": {"AdImageHash": None}}]})
        self._result_item(result, "UpdateResults")

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

    async def ensure_network(self, campaign_id: int) -> str:
        """Включает показы в сетях (сайты-партнёры, Карты) кампаниям, созданным раньше с выключенными сетями. Стратегию поиска и лимит не меняет.
        Возвращает ошибку Директа текстом (пусто, если всё в порядке или сети уже включены)."""
        result = await self.call("campaigns", "get", {
            "SelectionCriteria": {"Ids": [campaign_id]}, "FieldNames": ["Id"], "UnifiedCampaignFieldNames": ["BiddingStrategy"]})
        found = result.get("Campaigns") or []
        strategy = ((found[0].get("UnifiedCampaign") or {}).get("BiddingStrategy") if found else None) or {}
        if (strategy.get("Network") or {}).get("BiddingStrategyType") != "SERVING_OFF":
            return ""
        search = strategy.get("Search") or {}
        if search.get("BiddingStrategyType") == "WB_MAXIMUM_CLICKS":
            keep = bidding_strategy("clicks", search["WbMaximumClicks"]["WeeklySpendLimit"])
        elif search.get("BiddingStrategyType") == "WB_MAXIMUM_CONVERSION_RATE":
            conversion = search["WbMaximumConversionRate"]
            keep = bidding_strategy("conversions", conversion["WeeklySpendLimit"], str(conversion["GoalId"]))
        else:
            return ""   # стратегию, которую сервис не создавал, не трогаем
        try:
            answer = await self.call("campaigns", "update", {"Campaigns": [{"Id": campaign_id, "UnifiedCampaign": {"BiddingStrategy": keep}}]})
            self._result_item(answer, "UpdateResults")
        except DirectError as e:
            return f"Не удалось включить показы в сетях: {e.full_message()}"
        return ""

    async def ensure_alerts(self, campaign_id: int) -> list[str]:
        """Включает в кампании мониторинг сайта (Директ остановит показы, если сайт недоступен). Что уже включено, не трогает.
        Возвращает ошибки Директа, а не ломает остальное. Почтовые предупреждения (SendWarnings) Директ через API больше не принимает:
        на обновление он отвечает «настройка не будет изменена», поэтому их не трогаем."""
        result = await self.call("campaigns", "get", {
            "SelectionCriteria": {"Ids": [campaign_id]}, "FieldNames": ["Id"], "UnifiedCampaignFieldNames": ["Settings"]})
        found = result.get("Campaigns") or []
        if not found:
            return []
        settings = {s.get("Option"): s.get("Value") for s in (found[0].get("UnifiedCampaign") or {}).get("Settings") or []}
        updates: list[tuple[str, dict]] = []
        if settings.get("ENABLE_SITE_MONITORING") != "YES":
            updates.append(("Мониторинг сайта", {"UnifiedCampaign": {"Settings": [{"Option": "ENABLE_SITE_MONITORING", "Value": "YES"}]}}))
        errors = []
        for title, change in updates:
            try:
                answer = await self.call("campaigns", "update", {"Campaigns": [{"Id": campaign_id, **change}]})
                self._result_item(answer, "UpdateResults")
            except DirectError as e:
                errors.append(f"{title}: {e.full_message()}")
        return errors

    async def list_ads(self, campaign_ids: list[int]) -> list[dict]:
        """Объявления кампаний с состоянием показа и результатом проверки. State объявления уже учитывает состояние кампании."""
        result = await self.call("ads", "get", {
            "SelectionCriteria": {"CampaignIds": campaign_ids},
            "FieldNames": ["Id", "CampaignId", "State", "Status", "StatusClarification"],
            "TextAdFieldNames": ["ErirAdDescription", "AutogeneratedErirAdDescription"],
            "Page": {"Limit": PAGE_SIZE},
        })
        return result.get("Ads", [])

    async def set_ad_descriptions(self, ad_ids: list[int], description: str) -> None:
        """Дописывает описание объекта продвижения объявлениям, у которых его нет."""
        result = await self.call("ads", "update", {"Ads": [{"Id": i, "TextAd": {"ErirAdDescription": description}} for i in ad_ids]})
        for index in range(len(ad_ids)):
            self._result_item(result, "UpdateResults", index)

    async def delete_ads(self, ad_ids: list[int]) -> None:
        result = await self.call("ads", "delete", {"SelectionCriteria": {"Ids": ad_ids}})
        for i in range(len(ad_ids)):
            self._result_item(result, "DeleteResults", i)

    async def delete_ad_groups(self, group_ids: list[int]) -> None:
        result = await self.call("adgroups", "delete", {"SelectionCriteria": {"Ids": group_ids}})
        for i in range(len(group_ids)):
            self._result_item(result, "DeleteResults", i)

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

    async def find_district_ids(self, city_id: int, districts: list[str]) -> list[int]:
        """Номера регионов Директа для районов города. Район берётся, только если он лежит внутри города (по цепочке родителей справочника):
        одноимённый район другого города не подходит. Не найденные районы пропускаются."""
        result = await self.call("dictionaries", "get", {"DictionaryNames": ["GeoRegions"]})
        regions = {int(r["GeoRegionId"]): r for r in result.get("GeoRegions", []) if r.get("GeoRegionId") is not None}

        def inside(region: dict) -> bool:
            seen, parent = set(), region.get("ParentId")
            while parent and parent not in seen:
                if int(parent) == city_id:
                    return True
                seen.add(parent)
                parent = (regions.get(int(parent)) or {}).get("ParentId")
            return False

        def norm(name: str) -> str:
            return re.sub(r"\s*(?:район|р-н|округ|микрорайон)\s*", " ", name.lower().replace("ё", "е")).strip()

        found = []
        for wanted in districts:
            for rid, region in regions.items():
                if rid != city_id and norm(region.get("GeoRegionName", "")) == norm(wanted) and inside(region):
                    found.append(rid)
                    break
        return found

    async def min_weekly_budget(self, currency: str = "RUB") -> int | None:
        """Минимальный недельный бюджет кампании в микрорублях из справочника валют."""
        result = await self.call("dictionaries", "get", {"DictionaryNames": ["Currencies"]})
        for cur in result.get("Currencies", []):
            if cur.get("Currency") == currency:
                for prop in cur.get("Properties", []):
                    if prop.get("Name") == "MinimumWeeklySpendLimit":
                        return int(prop["Value"])
        return None

    async def ad_stats(self, date_from: str, date_to: str, goal_id: str | None = None, goal_ids: list[str] | None = None) -> list[dict]:
        """Статистика по объявлениям и дням: показы, клики, расход (микрорубли), обращения по цели «обращение» (goal_id)
        и достижения каждой цели из goal_ids (goals: номер цели → сколько раз). Директ принимает в отчёте до 10 целей."""
        goals = list(dict.fromkeys(([str(goal_id)] if goal_id else []) + [str(g) for g in (goal_ids or [])]))[:10]
        fields = ["Date", "CampaignId", "AdId", "Impressions", "Clicks", "Cost"]
        if goals:
            fields.append("Conversions")
        rows = await self.report("AD_PERFORMANCE_REPORT", fields, date_from, date_to, goals=goals or None)
        out = []
        for r in rows:
            by_goal: dict[str, int] = {}
            for key, value in r.items():
                if key.startswith("Conversions"):   # колонки Conversions_<цель>_<модель>; без номера цели это единственная цель отчёта
                    parts = key.split("_")
                    gid = parts[1] if len(parts) > 2 else (goals[0] if len(goals) == 1 else "")
                    by_goal[gid] = by_goal.get(gid, 0) + _to_int(value)
            out.append({
                "day": r["Date"],
                "campaign_id": r["CampaignId"],
                "ad_id": r["AdId"],
                "impressions": _to_int(r["Impressions"]),
                "clicks": _to_int(r["Clicks"]),
                "spend_micro": _to_int(r["Cost"]),
                "leads": by_goal.get(str(goal_id), 0) if goal_id else sum(by_goal.values()),
                "goals": by_goal,
            })
        return out

    async def search_queries(self, date_from: str, date_to: str, goal_id: str | None = None, campaign_ids: list | None = None) -> list[dict]:
        """Реальные поисковые запросы, по которым показывались объявления: за период, по запросам (без разбивки по дням).
        Показы, клики, расход (микрорубли) и обращения по цели «обращение» (если она задана). Отчёт охватывает весь кабинет,
        поэтому campaign_ids оставляет только запросы наших кампаний."""
        fields = ["CampaignId", "Query", "Impressions", "Clicks", "Cost"]
        if goal_id:
            fields.append("Conversions")
        rows = await self.report("SEARCH_QUERY_PERFORMANCE_REPORT", fields, date_from, date_to, name="autobider-queries", goals=[goal_id] if goal_id else None)
        mine = {str(c) for c in campaign_ids} if campaign_ids else None
        totals: dict[str, dict] = {}
        for r in rows:
            query = (r.get("Query") or "").strip()
            if not query or query == "--" or (mine is not None and r.get("CampaignId") not in mine):
                continue
            item = totals.setdefault(query, {"query": query, "impressions": 0, "clicks": 0, "spend_micro": 0, "leads": 0})
            item["impressions"] += _to_int(r["Impressions"])
            item["clicks"] += _to_int(r["Clicks"])
            item["spend_micro"] += _to_int(r["Cost"])
            item["leads"] += sum(_to_int(v) for k, v in r.items() if k.startswith("Conversions"))
        return list(totals.values())

    async def slice_report(self, dimension: str, date_from: str, date_to: str, goal_id: str | None = None, campaign_ids: list | None = None) -> list[dict]:
        """Показатели по одному измерению (устройство, пол, возраст, площадка…) и дням: показы, клики, расход (микрорубли), обращения по цели.
        Отчёт охватывает весь кабинет, поэтому campaign_ids оставляет только наши кампании; строки разных кампаний за день складываются."""
        fields = ["Date", "CampaignId", dimension, "Impressions", "Clicks", "Cost"]
        if goal_id:
            fields.append("Conversions")
        rows = await self.report("CUSTOM_REPORT", fields, date_from, date_to, name=f"autobider-{dimension.lower()}", goals=[goal_id] if goal_id else None)
        mine = {str(c) for c in campaign_ids} if campaign_ids else None
        totals: dict[tuple, dict] = {}
        for r in rows:
            if mine is not None and r.get("CampaignId") not in mine:
                continue
            key = (r["Date"], (r.get(dimension) or "--").strip() or "--")
            item = totals.setdefault(key, {"day": key[0], "key": key[1], "impressions": 0, "clicks": 0, "spend_micro": 0, "leads": 0})
            item["impressions"] += _to_int(r["Impressions"])
            item["clicks"] += _to_int(r["Clicks"])
            item["spend_micro"] += _to_int(r["Cost"])
            item["leads"] += sum(_to_int(v) for k, v in r.items() if k.startswith("Conversions"))
        return list(totals.values())

    async def demographic_adjustments(self, campaign_id: int) -> list[dict]:
        """Корректировки ставок по полу и возрасту, которые сейчас стоят на кампании."""
        result = await self.call("bidmodifiers", "get", {
            "SelectionCriteria": {"CampaignIds": [campaign_id], "Types": ["DEMOGRAPHICS_ADJUSTMENT"], "Levels": ["CAMPAIGN"]},
            "FieldNames": ["Id", "CampaignId", "Level", "Type"],
            "DemographicsAdjustmentFieldNames": ["Gender", "Age", "BidModifier", "Enabled"],
        })
        found = []
        for item in result.get("BidModifiers", []):
            d = item.get("DemographicsAdjustment") or {}
            found.append({"id": item["Id"], "gender": d.get("Gender"), "age": d.get("Age"), "percent": d.get("BidModifier")})
        return found

    async def delete_adjustments(self, ids: list[int]) -> None:
        if not ids:
            return
        result = await self.call("bidmodifiers", "delete", {"SelectionCriteria": {"Ids": ids}})
        for index in range(len(ids)):
            self._result_item(result, "DeleteResults", index)

    async def add_demographic_adjustments(self, campaign_id: int, slices: list[dict]) -> list[int]:
        """Корректировки по полу и возрасту одним набором (Директ создаёт набор целиком или не создаёт ничего).
        slices: {gender: GENDER_*|None, age: AGE_*|None, percent: 0..1300}; срезы не должны пересекаться."""
        items = []
        for s in slices:
            item = {"BidModifier": int(s["percent"])}
            if s.get("gender"):
                item["Gender"] = s["gender"]
            if s.get("age"):
                item["Age"] = s["age"]
            items.append(item)
        result = await self.call("bidmodifiers", "add", {"BidModifiers": [{"CampaignId": campaign_id, "DemographicsAdjustments": items}]})
        return self._result_item(result, "AddResults").get("Ids", [])

    async def set_negative_keywords(self, campaign_id: int, words: list[str]) -> None:
        """Минус-слова кампании целиком (Директ заменяет весь список, поэтому присылаем его полностью)."""
        result = await self.call("campaigns", "update", {"Campaigns": [{"Id": campaign_id, "NegativeKeywords": {"Items": words}}]})
        self._result_item(result, "UpdateResults")

    async def excluded_sites(self, campaign_id: int) -> list[str]:
        """Площадки и приложения, на которых показы кампании уже отключены."""
        result = await self.call("campaigns", "get", {"SelectionCriteria": {"Ids": [campaign_id]}, "FieldNames": ["Id", "ExcludedSites"]})
        found = result.get("Campaigns", [])
        return list(((found[0].get("ExcludedSites") or {}).get("Items") or [])) if found else []

    async def set_excluded_sites(self, campaign_id: int, sites: list[str]) -> None:
        """Список отключённых площадок целиком (Директ заменяет весь список, поэтому присылаем его полностью)."""
        result = await self.call("campaigns", "update", {"Campaigns": [{"Id": campaign_id, "ExcludedSites": {"Items": sites}}]})
        self._result_item(result, "UpdateResults")

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
