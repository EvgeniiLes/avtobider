"""Заглушка Яндекс Директа с памятью: кампании, группы, объявления, проверка, отчёты."""
import json

import httpx

from app.direct.client import DirectClient


class FakeDirect:
    def __init__(self, regions=None, min_weekly=300_000_000, fail_add_after=None, fail_ads_after=None):
        self.fail_add_after = fail_add_after  # после стольких созданных кампаний Директ начинает отказывать
        self.fail_ads_after = fail_ads_after  # после стольких созданных объявлений отказывает при создании следующего
        self.regions = regions if regions is not None else {"москва": 213}
        self.min_weekly = min_weekly
        self.campaigns: dict[int, dict] = {}
        self.groups: dict[int, int] = {}      # группа → кампания
        self.ads: dict[int, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.report_rows: list[tuple] = []  # (день, номер кампании, показы, клики, расход в микро[, обращения])
        self.report_requests: list[dict] = []
        self.negatives: dict[int, list[str]] = {}
        self.images: list[str] = []
        self.advertiser = {"tin_type": None, "tin": None, "name": None, "phone": ""}  # данные рекламодателя в «кабинете»
        self.update_error = ""
        self._id = 1000
        self.transport = httpx.MockTransport(self._handle)

    def client(self, token="t", **kw) -> DirectClient:
        return DirectClient(token, transport=self.transport, **kw)

    def factory(self):
        return lambda: (lambda token, **kw: DirectClient(token, transport=self.transport, **kw))

    def _next(self) -> int:
        self._id += 1
        return self._id

    @staticmethod
    def _limit(search: dict) -> int:
        """Недельный лимит из описания стратегии (кликов или конверсий)."""
        return (search.get("WbMaximumClicks") or search["WbMaximumConversionRate"])["WeeklySpendLimit"]

    # управление состоянием из тестов
    def accept_all(self, state="OFF", payment="DISALLOWED"):
        for c in self.campaigns.values():
            c.update(Status="ACCEPTED", State=state, StatusPayment=payment)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        service = request.url.path.rsplit("/", 1)[1]
        body = json.loads(request.content)
        if service == "reports":
            return self._report(body)
        method, params = body["method"], body["params"]
        self.calls.append((service, method))
        ok = lambda result: httpx.Response(200, json={"result": result}, headers={"Units": "10/100/200"})
        ids = lambda: params.get("SelectionCriteria", {}).get("Ids", [])

        if service == "dictionaries":
            if params["DictionaryNames"] == ["GeoRegions"]:
                return ok({"GeoRegions": [{"GeoRegionId": i, "GeoRegionName": n.capitalize(), "GeoRegionType": "City"} for n, i in self.regions.items()]})
            return ok({"Currencies": [{"Currency": "RUB", "Properties": [{"Name": "MinimumWeeklySpendLimit", "Value": str(self.min_weekly)}]}]})
        if service == "clients":
            if method == "update":
                if self.update_error:
                    return ok({"UpdateResults": [{"Errors": [{"Code": 8000, "Message": self.update_error}]}]})
                item = params["Clients"][0]
                self.advertiser = {"tin_type": item["TinInfo"]["TinType"], "tin": item["TinInfo"]["Tin"],
                                   "name": item["ErirAttributes"]["Organization"]["Name"], "phone": item.get("Phone", self.advertiser["phone"])}
                return ok({"UpdateResults": [{"ClientId": 1}]})
            client = {"Login": "fake-login", "ClientInfo": "Иванов Иван", "Phone": self.advertiser["phone"]}
            if params.get("TinInfoFieldNames") and self.advertiser["tin"]:
                client["TinInfo"] = {"TinType": self.advertiser["tin_type"], "Tin": self.advertiser["tin"]}
                client["ErirAttributes"] = {"Organization": {"Name": self.advertiser["name"]}}
            return ok({"Clients": [client]})
        if service == "campaigns":
            if method == "add":
                item = params["Campaigns"][0]
                search = item["TextCampaign"]["BiddingStrategy"]["Search"]
                limit = self._limit(search)
                if self.fail_add_after is not None and len(self.campaigns) >= self.fail_add_after:
                    return ok({"AddResults": [{"Errors": [{"Code": 9000, "Message": "Достигнут лимит кампаний"}]}]})
                if limit < self.min_weekly:
                    return ok({"AddResults": [{"Errors": [{"Code": 4000, "Message": "Недельный бюджет меньше минимума"}]}]})
                cid = self._next()
                self.campaigns[cid] = {"Id": cid, "Name": item["Name"], "Limit": limit, "Strategy": search["BiddingStrategyType"], "Status": "DRAFT",
                                       "State": "OFF", "StatusPayment": "ALLOWED"}
                self.negatives[cid] = (item.get("NegativeKeywords") or {}).get("Items", [])
                return ok({"AddResults": [{"Id": cid}]})
            if method == "get":
                wanted = ids()
                return ok({"Campaigns": [{k: v for k, v in c.items() if k != "Limit"} | {"StatusClarification": c.get("Note", "")}
                                         for c in self.campaigns.values() if not wanted or c["Id"] in wanted]})
            if method in ("suspend", "resume"):
                for i in ids():
                    self.campaigns[i]["State"] = "SUSPENDED" if method == "suspend" else "ON"
                return ok({"SuspendResults" if method == "suspend" else "ResumeResults": [{"Id": i} for i in ids()]})
            if method == "update":
                item = params["Campaigns"][0]
                unified = item["UnifiedCampaign"]
                if "BiddingStrategy" in unified:
                    search = unified["BiddingStrategy"]["Search"]
                    self.campaigns[item["Id"]].update(Limit=self._limit(search), Strategy=search["BiddingStrategyType"],
                                                      GoalId=search.get("WbMaximumConversionRate", {}).get("GoalId"))
                if "CounterIds" in unified:
                    self.campaigns[item["Id"]]["CounterIds"] = unified["CounterIds"]["Items"]
                return ok({"UpdateResults": [{"Id": item["Id"]}]})
            if method == "delete":
                for i in ids():
                    self.campaigns.pop(i, None)
                return ok({"DeleteResults": [{"Id": i} for i in ids()]})
        if service == "adimages":
            item = params["AdImages"][0]
            self.images.append(item["Name"])
            return ok({"AddResults": [{"AdImageHash": f"hash{len(self.images)}"}]})
        if service == "adgroups":
            gid = self._next()
            self.groups[gid] = params["AdGroups"][0]["CampaignId"]
            return ok({"AddResults": [{"Id": gid}]})
        if service == "ads":
            if method == "add":
                item = params["Ads"][0]
                if self.fail_ads_after is not None and len(self.ads) >= self.fail_ads_after:
                    return ok({"AddResults": [{"Errors": [{"Code": 9001, "Message": "Не удалось создать объявление"}]}]})
                aid = self._next()
                self.ads[aid] = {"group": item["AdGroupId"], "campaign": self.groups[item["AdGroupId"]], "ad": item["TextAd"], "suspended": False}
                return ok({"AddResults": [{"Id": aid}]})
            if method == "get":
                wanted = params["SelectionCriteria"].get("CampaignIds") or list(self.campaigns)
                found = []
                for aid, ad in self.ads.items():
                    camp = self.campaigns.get(ad["campaign"])
                    if camp is None or camp["Id"] not in wanted:
                        continue
                    state = "SUSPENDED" if ad["suspended"] else "ON" if camp["State"] == "ON" else "OFF"   # у Директа State объявления учитывает кампанию
                    found.append({"Id": aid, "CampaignId": camp["Id"], "State": state, "Status": camp["Status"], "StatusClarification": camp.get("Note", "")})
                return ok({"Ads": found})
            if method in ("suspend", "resume"):
                for i in ids():
                    self.ads[i]["suspended"] = method == "suspend"
                return ok({"SuspendResults" if method == "suspend" else "ResumeResults": [{"Id": i} for i in ids()]})
            if method == "update":
                item = params["Ads"][0]
                self.ads[item["Id"]]["ad"].update(item["TextAd"])
                return ok({"UpdateResults": [{"Id": item["Id"]}]})
            if method == "moderate":
                for c in self.campaigns.values():
                    if c["Status"] == "DRAFT":
                        c["Status"] = "MODERATION"
                return ok({"ModerateResults": [{"Id": i} for i in ids()]})
        return httpx.Response(404)

    def _report(self, body) -> httpx.Response:
        params = body["params"]
        self.report_requests.append(params)
        goals = params.get("Goals") or []
        cols = ["Date", "CampaignId", "AdId", "Impressions", "Clicks", "Cost"]
        if "Conversions" in params["FieldNames"]:
            cols.append(f"Conversions_{goals[0]}_AUTO" if goals else "Conversions")
        lines = ["\t".join(cols)]
        for row in self.report_rows:
            day, ad_index, imp, clicks, cost = row[:5]   # ad_index: номер объявления по порядку создания
            leads = row[5] if len(row) > 5 else 0
            ad = list(self.ads)[ad_index]
            camp = self.ads[ad]["campaign"]
            values = [day, camp, ad, imp, clicks, cost]
            if len(cols) > 6:
                values.append(leads)
            lines.append("\t".join(map(str, values)))
        return httpx.Response(200, text="\n".join(lines))
