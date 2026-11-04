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


def test_base_url_and_version():
    assert make_client(lambda r: ok({})).base_url == "https://api.direct.yandex.com/json/v501"
    assert make_client(lambda r: ok({}), api_version="v5").base_url == "https://api.direct.yandex.com/json/v5"


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
    assert seen["url"] == "https://api.direct.yandex.com/json/v501/campaigns"
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


def test_server_error_keeps_http_status_in_detail(monkeypatch):
    async def no_sleep(_):
        pass

    monkeypatch.setattr("app.direct.client.asyncio.sleep", no_sleep)

    async def run():
        async with make_client(lambda r: httpx.Response(504)) as client:
            await client.list_campaigns()

    with pytest.raises(DirectError) as exc:
        asyncio.run(run())
    assert exc.value.code == 52
    assert exc.value.detail == "HTTP 504"


def run_client(handler, coro_fn):
    async def run():
        async with make_client(handler, api_version="v501") as client:
            return await coro_fn(client)

    return asyncio.run(run())


def test_create_campaign_group_ad_payloads():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append((request.url.path.rsplit("/", 1)[1], body["method"], body["params"]))
        key = "AddResults"
        return ok({key: [{"Id": 100 + len(seen)}]})

    async def go(client):
        cid = await client.create_text_campaign("Лотос 1", 600 * 1_000_000, "2026-11-01")
        gid = await client.create_ad_group(cid, "Группа", [213])
        aid = await client.create_text_ad(gid, "Маникюр в Москве", "Запишитесь онлайн", business_id=555, href="https://x.ru")
        return cid, gid, aid

    assert run_client(handler, go) == (101, 102, 103)
    campaign = seen[0][2]["Campaigns"][0]
    strategy = campaign["TextCampaign"]["BiddingStrategy"]
    assert strategy["Search"]["WbMaximumClicks"]["WeeklySpendLimit"] == 600_000_000
    assert strategy["Network"]["BiddingStrategyType"] == "NETWORK_DEFAULT"      # поиск, Карты и сайты-партнёры: сети идут следом за поиском
    assert seen[1][2]["AdGroups"][0]["RegionIds"] == [213]
    ad = seen[2][2]["Ads"][0]["TextAd"]
    assert ad["BusinessId"] == 555 and ad["Mobile"] == "NO" and ad["Href"] == "https://x.ru"


def test_ad_needs_site_or_business():
    async def go(client):
        await client.create_text_ad(1, "T", "X")

    with pytest.raises(DirectError) as e:
        run_client(lambda r: ok({}), go)
    assert "Яндекс Картах" in e.value.message


def test_add_error_is_raised_with_direct_message():
    def handler(request):
        return ok({"AddResults": [{"Errors": [{"Code": 5005, "Message": "Недопустимый недельный бюджет", "Details": "мин. 300"}]}]})

    async def go(client):
        await client.create_text_campaign("x", 1, "2026-11-01")

    with pytest.raises(DirectError) as e:
        run_client(handler, go)
    assert e.value.code == 5005 and "бюджет" in e.value.message


def test_suspend_resume_and_moderate():
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append((request.url.path.rsplit("/", 1)[1], body["method"], body["params"]["SelectionCriteria"]["Ids"]))
        name = {"suspend": "SuspendResults", "resume": "ResumeResults", "moderate": "ModerateResults"}[body["method"]]
        return ok({name: [{"Id": i} for i in body["params"]["SelectionCriteria"]["Ids"]]})

    async def go(client):
        await client.set_campaigns_state([1, 2], False)
        await client.set_campaigns_state([1], True)
        await client.set_ads_state([9], False)
        await client.moderate_ads([9])

    run_client(handler, go)
    assert calls == [("campaigns", "suspend", [1, 2]), ("campaigns", "resume", [1]), ("ads", "suspend", [9]), ("ads", "moderate", [9])]


def test_strategy_switch_to_max_conversions_keeps_the_limit_and_names_the_goal():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append((request.url.path.rsplit("/", 1)[1], body["method"], body["params"]))
        return ok({"UpdateResults": [{"Id": 77}]})

    async def go(client):
        await client.set_strategy(77, "conversions", 900 * 1_000_000, "12345")
        await client.set_strategy(77, "clicks", 900 * 1_000_000)

    run_client(handler, go)
    conversions = seen[0][2]["Campaigns"][0]
    assert conversions["Id"] == 77
    strategy = conversions["UnifiedCampaign"]["BiddingStrategy"]
    assert strategy["Search"] == {"BiddingStrategyType": "WB_MAXIMUM_CONVERSION_RATE",
                                  "WbMaximumConversionRate": {"WeeklySpendLimit": 900_000_000, "GoalId": 12345}}
    assert strategy["Network"]["BiddingStrategyType"] == "NETWORK_DEFAULT"      # сети остаются включёнными при смене стратегии
    assert seen[1][2]["Campaigns"][0]["UnifiedCampaign"]["BiddingStrategy"]["Search"]["BiddingStrategyType"] == "WB_MAXIMUM_CLICKS"


@pytest.mark.parametrize("goal", [None, "", "не число"])
def test_max_conversions_without_a_valid_goal_is_refused_before_calling_direct(goal):
    called = []

    async def go(client):
        await client.set_strategy(1, "conversions", 300_000_000, goal)

    with pytest.raises(DirectError) as e:
        run_client(lambda r: called.append(r) or ok({}), go)
    assert "цель" in e.value.message and called == []


def test_ads_and_campaign_state_are_read_with_the_fields_the_service_needs():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append((request.url.path.rsplit("/", 1)[1], body["params"]))
        if body["method"] == "get" and seen[-1][0] == "ads":
            return ok({"Ads": [{"Id": 5, "CampaignId": 9, "State": "SUSPENDED", "Status": "ACCEPTED"}]})
        return ok({"Campaigns": [{"Id": 9, "State": "ON", "Status": "ACCEPTED", "StatusPayment": "ALLOWED"}]})

    async def go(client):
        return await client.list_ads([9]), await client.get_campaign(9), await client.get_campaign(10)

    ads, camp, _ = run_client(handler, go)
    assert ads[0]["State"] == "SUSPENDED" and camp["State"] == "ON"
    assert seen[0][1]["SelectionCriteria"] == {"CampaignIds": [9]} and {"State", "Status", "StatusClarification"} <= set(seen[0][1]["FieldNames"])


def test_region_and_minimum_budget_lookup():
    def handler(request):
        names = json.loads(request.content)["params"]["DictionaryNames"]
        if names == ["GeoRegions"]:
            return ok({"GeoRegions": [
                {"GeoRegionId": 1, "GeoRegionName": "Москва и область", "GeoRegionType": "Region"},
                {"GeoRegionId": 213, "GeoRegionName": "Москва", "GeoRegionType": "City"},
            ]})
        return ok({"Currencies": [{"Currency": "RUB", "Properties": [{"Name": "MinimumWeeklySpendLimit", "Value": "300000000"}]}]})

    async def go(client):
        return await client.find_region_id(" москва "), await client.find_region_id("Нигде"), await client.min_weekly_budget()

    assert run_client(handler, go) == (213, None, 300_000_000)


TSV = "Date\tCampaignId\tAdId\tImpressions\tClicks\tCost\n2026-10-27\t10\t77\t1000\t30\t450000000\n2026-10-27\t10\t78\t--\t--\t--\n"


def test_ad_stats_parses_tsv_and_money_stays_in_micros():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, text=TSV, headers={"Units": "10/100/200"})

    async def run():
        async with make_client(handler) as client:
            return await client.ad_stats("2026-10-27", "2026-10-27")

    rows = asyncio.run(run())
    assert seen["url"].endswith("/json/v501/reports")
    params = seen["body"]["params"]
    assert params["ReportType"] == "AD_PERFORMANCE_REPORT" and params["DateRangeType"] == "CUSTOM_DATE"
    assert params["SelectionCriteria"] == {"DateFrom": "2026-10-27", "DateTo": "2026-10-27"}
    assert "returnMoneyInMicros" not in seen["headers"]
    assert seen["headers"]["skipReportHeader"] == "true"
    assert rows[0] == {"day": "2026-10-27", "campaign_id": "10", "ad_id": "77", "impressions": 1000, "clicks": 30,
                       "spend_micro": 450_000_000, "leads": 0, "goals": {}}
    assert rows[1]["clicks"] == 0 and rows[1]["spend_micro"] == 0  # «--» значит ноль


def test_ad_stats_with_goal_reads_conversions_column():
    seen = {}

    def handler(request):
        seen["params"] = json.loads(request.content)["params"]
        return httpx.Response(200, text="Date\tCampaignId\tAdId\tImpressions\tClicks\tCost\tConversions_9001_AUTO\n"
                                        "2026-10-27\t10\t77\t1000\t30\t450000000\t4\n2026-10-27\t10\t78\t1\t1\t1000000\t--\n")

    async def go(client):
        return await client.ad_stats("2026-10-27", "2026-10-27", goal_id="9001")

    rows = run_client(handler, go)
    assert seen["params"]["Goals"] == ["9001"] and seen["params"]["AttributionModels"] == ["AUTO"]
    assert "Conversions" in seen["params"]["FieldNames"]
    assert [r["leads"] for r in rows] == [4, 0]


def test_ad_stats_reads_every_goal_and_counts_only_the_main_one_as_leads():
    seen = {}

    def handler(request):
        seen["params"] = json.loads(request.content)["params"]
        return httpx.Response(200, text="Date\tCampaignId\tAdId\tImpressions\tClicks\tCost\tConversions_9001_AUTO\tConversions_9002_AUTO\tConversions_9003_AUTO\n"
                                        "2026-10-27\t10\t77\t1000\t30\t450000000\t4\t11\t--\n2026-10-27\t10\t78\t1\t1\t1000000\t--\t2\t1\n")

    async def go(client):
        return await client.ad_stats("2026-10-27", "2026-10-27", goal_id="9001", goal_ids=["9002", "9001", "9003"])

    rows = run_client(handler, go)
    assert seen["params"]["Goals"] == ["9001", "9002", "9003"]   # «обращение» первым, повторов нет
    assert [r["leads"] for r in rows] == [4, 0]
    assert rows[0]["goals"] == {"9001": 4, "9002": 11, "9003": 0}
    assert rows[1]["goals"] == {"9001": 0, "9002": 2, "9003": 1}


def test_ad_stats_asks_for_at_most_ten_goals():
    seen = {}

    def handler(request):
        seen["params"] = json.loads(request.content)["params"]
        return httpx.Response(200, text="Date\tCampaignId\tAdId\tImpressions\tClicks\tCost\n")

    async def go(client):
        return await client.ad_stats("2026-10-27", "2026-10-27", goal_id="1", goal_ids=[str(i) for i in range(2, 30)])

    run_client(handler, go)
    assert len(seen["params"]["Goals"]) == 10 and seen["params"]["Goals"][0] == "1"


def test_offline_report_is_awaited():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(201 if len(calls) == 1 else 202, headers={"retryIn": "0"})
        return httpx.Response(200, text=TSV)

    async def run():
        async with make_client(handler) as client:
            return await client.ad_stats("2026-10-01", "2026-10-27")

    assert len(asyncio.run(run())) == 2
    assert len(calls) == 3


def test_report_error_is_human():
    def handler(request):
        return httpx.Response(400, json={"error": {"error_code": 8000, "error_string": "Неверные параметры", "error_detail": "x", "request_id": "r9"}})

    async def run():
        async with make_client(handler) as client:
            await client.ad_stats("2026-10-01", "2026-10-27")

    with pytest.raises(DirectError) as e:
        asyncio.run(run())
    assert e.value.code == 8000 and e.value.request_id == "r9"


def test_report_gives_up_waiting():
    async def run():
        async with make_client(lambda r: httpx.Response(202, headers={"retryIn": "0"})) as client:
            await client.report("AD_PERFORMANCE_REPORT", ["Date"], "2026-10-01", "2026-10-02", max_polls=3)

    with pytest.raises(DirectError) as e:
        asyncio.run(run())
    assert "долго" in e.value.message


def test_empty_token_rejected():
    with pytest.raises(DirectError):
        DirectClient("")
