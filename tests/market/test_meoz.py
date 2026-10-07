from datetime import datetime, timedelta

import httpx
import pytest

from stock_god.market.common import CN
from stock_god.market.meoz import MeozError, MeozProvider

NOW = datetime(2026, 10, 8, 9, 24, 51, tzinfo=CN)


def provider(handler, **kwargs):
    return MeozProvider({"meozApiKey": "secret-fixture"}, httpx.Client(transport=httpx.MockTransport(handler)),
                        clock=lambda: NOW, **kwargs)


def success(fields=None, items=None):
    return httpx.Response(200, json={"code": 200, "data": {
        "fields": fields or ["symbol", "tradedate"], "items": [] if items is None else items}})


def test_fields_order_and_symbol_string():
    p = provider(lambda r: success(["tradedate", "symbol"], [["20261008", "000001"]]))
    assert p.request("stockbasic", {})["rows"] == [{"symbol": "000001", "tradedate": "20261008"}]


@pytest.mark.parametrize("fields,items", [(["symbol", "symbol"], []), (["symbol"], [[1]]),
    (["symbol"], [["000001", 2]]), (["tradedate"], [["20260230"]])])
def test_rejects_corrupt_schema(fields, items):
    p = provider(lambda r: success(fields, items))
    with pytest.raises(MeozError):
        p.request("stockbasic", {})


def test_switches_only_connection_failure():
    urls = []
    def handler(request):
        urls.append(str(request.url))
        if len(urls) == 1:
            raise httpx.ConnectError("contains secret-fixture", request=request)
        return success()
    p = provider(handler)
    p.request("stockbasic", {})
    assert urls == ["https://sz.meoz.cn:6688/api", "https://sh.meoz.cn:6688/api"]
    p.request("stockbasic", {})
    assert urls[-1] == urls[-2]


def test_permission_never_fails_over_or_leaks_secret():
    calls = []
    def handler(r):
        calls.append(r)
        return httpx.Response(200, json={"code": 403, "message": "secret-fixture"})
    with pytest.raises(MeozError) as exc:
        provider(handler).request("stockbasic", {})
    assert exc.value.status == "no_permission"
    assert "secret-fixture" not in str(exc.value)
    assert len(calls) == 1


def test_history_never_switches_to_live():
    urls = []
    def handler(r):
        urls.append(str(r.url))
        raise httpx.ConnectError("secret-fixture", request=r)
    with pytest.raises(MeozError):
        provider(handler).request("tick_history", {}, history=True)
    assert urls == ["https://hist.meoz.cn:6688/api"]


def test_retry_finite_same_node_and_respects_cutoff():
    calls, delays = [], []
    def handler(r):
        calls.append(str(r.url))
        return httpx.Response(429, headers={"Retry-After": "20"})
    p = provider(handler, sleep=delays.append)
    with pytest.raises(MeozError):
        p.request("stockbasic", {})
    assert len(calls) == 3 and len(set(calls)) == 1 and delays == [20, 20]
    calls.clear()
    with pytest.raises(MeozError):
        p.request("stockbasic", {}, deadline=NOW + timedelta(seconds=5))
    assert len(calls) == 1


def test_ticks_batch_and_future_gate():
    sizes = []
    import json
    def handler(r):
        body = json.loads(r.content)
        sizes.append(len(body["params"]["symbols"]))
        symbol = body["params"]["symbols"][0]
        return success(["symbol", "tradedate", "time"], [[symbol, "20261008", "2026-10-08 09:24:50"]])
    p = provider(handler)
    codes = [f"{i:06d}" for i in range(1, 202)]
    result = p.ticks("20261008", codes)
    assert sizes == [200, 1] and len(result) == 2
    with pytest.raises(MeozError):
        p.ticks("20261008", codes[:1], final=True)


def test_unknown_volume_stays_nan_book_documented_lots():
    import math
    raw = {"vol": 2, "bid_vol1": 2, "ask_vol1": 3, "close": 10}
    x = MeozProvider.auction_values(raw, 9)
    assert math.isnan(x[2]) and x[6] == 200 and x[10] == 300
    assert MeozProvider.auction_values(raw, 9, volume_unit="share")[2] == 2
    assert MeozProvider.auction_values(raw, 9, volume_unit="lot")[2] == 200


@pytest.fixture
def source(tmp_path):
    from types import SimpleNamespace

    from stock_god.market.meoz_source import MeozAuctionSource
    from stock_god.storage.current import BASE43_DDL
    from stock_god.storage.db import Database
    db = Database(tmp_path / "meoz.db")
    with db.transaction() as con:
        for sql in BASE43_DDL:
            con.execute(sql)
    market = SimpleNamespace(meoz=provider(lambda r: success()))
    return MeozAuctionSource(db, market, clock=lambda: NOW)


def test_optional_diagnostic_receipt_does_not_grant_daily_readiness(source):
    from stock_god.market.meoz_source import SIGNATURE
    assert source.status()["ready"] is False
    with pytest.raises(ValueError):
        source.certify("share", {"passed": True})
    source.certify("share", {"passed": True, "coverageVerified": True, "volumeVerified": True,
        "checkpointsVerified": True, "finalAuctionVerified": True, "sourceSignature": SIGNATURE,
        "tradingDate": "20261008", "evidenceSha256": "a" * 64})
    assert source.status()["ready"] is False
    source.market.meoz.settings["meozApiKey"] = "different-fixture-account"
    assert source.status()["ready"] is False


def test_source_freeze_does_not_allow_sparse_daily_data(source):
    c = {"code": "sz000001", "reference": 900, "auctionRows": []}
    source._write("20261008", "source_candidates", {"complete": True, "candidates": [c], "documents": []})
    raw = {"close": 10, "vol": 100, "amount": 1000}
    source._write("20261008", "source_snapshot", {"records": [{"code": c["code"], "raw": raw,
        "asOf": "2026-10-08T09:25:00+08:00", "availableAt": "2026-10-08T09:25:01+08:00"}], "documents": []})
    result = source.freeze("20261008", NOW.replace(minute=29, second=59))
    assert not result["complete"] and result["sourceStatusJson"]["status"] == "incomplete"
    assert result["candidates"][0]["auctionRows"][0]["fields"][2] == 10000


def test_source_freeze_rejects_conflicting_ticks(source):
    c = {"code": "sz000001", "reference": 900, "auctionRows": []}
    source._write("20261008", "source_candidates", {"complete": True, "candidates": [c], "documents": []})
    records = []
    start = NOW.replace(minute=15, second=0)
    for sec in range(0, 601, 3):
        t = start + timedelta(seconds=sec)
        records.append({"code": c["code"], "raw": {"close": 10, "vol": 1, "amount": 1000,
                        "bid1": 10, "ask1": 10, "bid_vol1": 1, "ask_vol1": 1},
                        "asOf": t.isoformat(), "availableAt": (t + timedelta(seconds=1)).isoformat()})
    documents = [{"receivedAt": "2026-10-08T09:25:02+08:00", "rows": [
        {"symbol": "000001", "m_price": 10, "auc_vol": 1, "auc_amt": 1000}]}]
    source._write("20261008", "source_snapshot", {"records": records, "documents": documents})
    cutoff = NOW.replace(minute=29, second=59)
    assert source.freeze("20261008", cutoff)["complete"]
    assert source._read("source", "source_certification") is None
    assert source.status()["ready"]
    conflict = {**records[1], "raw": {"close": 11, "vol": 1, "amount": 1000}}
    source._write("20261008", "source_snapshot", {"records": records + [conflict], "documents": documents})
    assert not source.freeze("20261008", cutoff)["complete"]
    for record in records:
        record["raw"]["ask1"] = None
    source._write("20261008", "source_snapshot", {"records": records, "documents": documents})
    invalid_book = source.inspect("20261008", cutoff, volume_unit="lot")
    assert not invalid_book["checks"]["checkpointsVerified"]
    assert not invalid_book["checks"]["coverageVerified"]
    for record in records:
        record["raw"]["ask1"] = 10
        record["raw"]["ask_vol1"] = 2
    source._write("20261008", "source_snapshot", {"records": records, "documents": documents})
    assert not source.freeze("20261008", cutoff)["complete"]


def test_historical_auction_dates_use_live_contract_and_split_months(source):
    requests = []
    def request(api, params, fields=None, **kwargs):
        requests.append((api, params, kwargs))
        return {"rows": []}
    source._request = request
    source.market.is_trading_day = lambda date: date.weekday() < 2
    source._history_volumes("000001", "20261008", NOW)
    requests = [r for r in requests if r[0] == "daily_auc_detail"]
    assert len(requests) >= 3
    assert all(not args.get("history") for _, _, args in requests)
    assert all(p["startdate"][:6] == p["enddate"][:6] for _, p, _ in requests)


def test_history_is_not_inferred_from_date():
    p = provider(lambda r: success())
    with pytest.raises(ValueError):
        p.request("daily_auc_detail", {"tradedate": "20260930"}, history=True)


def test_inspection_never_grants_production_readiness(source):
    source._write("20261008", "source_candidates", {"complete": True, "candidates": [], "documents": []})
    source._write("20261008", "source_snapshot", {"records": [], "documents": []})
    inspected = source.inspect("20261008", NOW.replace(minute=29, second=59))
    assert inspected["inspectionOnly"] and not inspected["complete"]
    assert not inspected["sourceStatusJson"]["ready"] and not inspected["inspectionPassed"]
    assert not source.status()["ready"]


def test_daily_permission_failure_survives_source_recreation(source):
    from stock_god.market.meoz_source import SIGNATURE, MeozAuctionSource
    source.certify("lot", {"passed": True, "coverageVerified": True, "volumeVerified": True,
        "checkpointsVerified": True, "finalAuctionVerified": True, "sourceSignature": SIGNATURE,
        "tradingDate": "20261008", "evidenceSha256": "a" * 64})
    source._write("20261008", "source_candidates", {"complete": True, "candidates": [], "documents": []})
    source._write("20261008", "source_snapshot", {"error": "MeoZ 接口无权限", "sourceStatus": "no_permission"})
    clone = MeozAuctionSource(source.database, source.market, source.clock)
    assert clone.status()["status"] == "unauthorized" and not clone.status()["ready"]
    assert clone._read("source", "source_certification") is not None
    tomorrow = MeozAuctionSource(source.database, source.market, lambda: NOW + timedelta(days=1))
    assert not tomorrow.status()["ready"]


def test_daily_freeze_failure_remains_blocked_even_with_old_receipt(source):
    from stock_god.market.meoz_source import SIGNATURE
    source.certify("lot", {"passed": True, "coverageVerified": True, "volumeVerified": True,
        "checkpointsVerified": True, "finalAuctionVerified": True, "sourceSignature": SIGNATURE,
        "tradingDate": "20261008", "evidenceSha256": "a" * 64})
    source.freeze("20261008", NOW.replace(minute=29, second=59))
    assert source.status()["status"] == "incomplete" and not source.status()["ready"]
    assert source.market.meoz.configured


def test_holding_exit_rejects_conflicting_final_and_requires_corroboration(source):
    from stock_god.market.meoz_source import SIGNATURE
    source.certify("lot", {"passed": True, "coverageVerified": True, "volumeVerified": True,
        "checkpointsVerified": True, "finalAuctionVerified": True, "sourceSignature": SIGNATURE,
        "tradingDate": "20261008", "evidenceSha256": "a" * 64})
    source.market.daily_closes = lambda *args: [{"tradingDate": "2026-10-07", "close": 10}]
    record = {"code": "sz000001", "asOf": "2026-10-08T09:25:00+08:00",
              "availableAt": "2026-10-08T09:25:01+08:00", "raw": {"close": 9, "vol": 1, "amount": 900}}
    documents = [{"receivedAt": "2026-10-08T09:25:02+08:00", "rows": [
        {"tradedate": "20261008", "symbol": "000001", "m_price": 9, "auc_vol": 1, "auc_amt": 900}]}]
    source._write("20261008", "source_snapshot", {"records": [record], "documents": documents})
    assert source.exit_state("sz000001", "20261008", "20261007")["complete"]
    conflict = {**record, "raw": {**record["raw"], "close": 11}}
    source._write("20261008", "source_snapshot", {"records": [record, conflict], "documents": documents})
    assert not source.exit_state("sz000001", "20261008", "20261007")["complete"]
    source._write("20261008", "source_snapshot", {"records": [record], "documents": []})
    assert not source.exit_state("sz000001", "20261008", "20261007")["complete"]


@pytest.mark.parametrize("timing,expected", [(None, True), ("09:30-15:00", True),
    ("09:30-11:30,13:00-15:00", True), ("09:30-10:15", False), ("", False)])
def test_rules_distinguish_full_day_suspension(source, timing, expected):
    def request(api, params, fields=None, **kwargs):
        if api == "pricelimit":
            return {"rows": [{"symbol": "000001", "tradedate": "20261008",
                "pre_close": 10, "up_limit": 11, "down_limit": 9}]}
        return {"rows": [{"symbol": "000001", "tradedate": "20261008",
            "suspend_type": "S", "suspend_timing": timing}]}
    source._request = request
    rules = source.rules("sz000001", "20261008")
    assert rules["known"] and rules["suspended"]
    assert rules["fullDaySuspended"] is expected


def test_rules_and_mother_reject_latest_instead_of_requested_day(source):
    source._request = lambda *args, **kwargs: {"rows": [{"symbol": "000001", "tradedate": "20261007",
        "pre_close": 10, "up_limit": 11, "down_limit": 9, "pre_type": "u", "pre_limit_times": 1}]}
    assert source.rules("sz000001", "20261008")["known"] is False
    prepared = source.prepare("20261008", "20261007")
    assert prepared["complete"] is False
    assert prepared["sourceStatusJson"]["status"] == "incomplete"


def test_suspend_no_records_is_an_empty_set_without_retry():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={
            "code": 1002, "message": "未找到停牌复牌数据", "data": None})
    p = provider(handler)
    result = p.request("suspend", {"symbols": ["000001"], "startdate": "20261008",
                                 "enddate": "20261008"}, "symbol,tradedate,suspend_type")
    assert result["rows"] == [] and result["fields"] == ["symbol", "tradedate", "suspend_type"]
    assert result["receivedAt"] == NOW.isoformat() and len(calls) == 1


@pytest.mark.parametrize("api,message,data,http_status", [
    ("stockbasic", "未找到停牌复牌数据", None, 200),
    ("suspend", "参数或数据异常", None, 200),
    ("suspend", "未找到停牌复牌数据", {"fields": [], "items": []}, 200),
    ("suspend", "未找到停牌复牌数据", None, 400),
])
def test_no_records_does_not_hide_other_provider_failures(api, message, data, http_status):
    p = provider(lambda r: httpx.Response(http_status, json={
        "code": 1002, "message": message, "data": data}))
    with pytest.raises(MeozError):
        p.request(api, {})


def test_stockbasic_and_empty_suspension_preserve_candidate_eligibility(source):
    import json
    calls = []
    def handler(request):
        body = json.loads(request.content)
        api, params = body["apiname"], body["params"]
        calls.append(api)
        if api == "limit_pool_yes":
            return success(["tradedate", "symbol", "pre_type", "pre_limit_times"],
                           [["20261008", "000001", "u", 1]])
        if api == "stockbasic":
            return success(["symbol", "name", "market", "list_status"],
                           [["000001", "普通股票", "主板", "L"]])
        if api == "pricelimit":
            return success(["symbol", "tradedate", "pre_close", "up_limit", "down_limit"],
                           [["000001", params["tradedate"], 9, 9.9, 8.1]])
        if api == "suspend":
            return httpx.Response(200, json={
                "code": 1002, "message": "未找到停牌复牌数据", "data": None})
        raise AssertionError("unsupported production request")
    p = provider(handler)
    source.market.meoz = p
    source.market.meoz_request = p.request
    source.market.bars = lambda *a, **k: [{
        "time": "2026-10-07T09:31:00+08:00", "open": 9.9, "high": 9.9,
        "low": 9.9, "close": 9.9, "volume": 100}]
    source._history_volumes = lambda *a: [100] * 20
    result = source.prepare("20261008", "20261007")
    assert result["complete"] and len(result["candidates"]) == 1
    assert result["candidates"][0]["code"] == "sz000001"
    assert "stockbasic" in calls and "basic" not in calls
    assert not source.status()["ready"]


def test_changed_or_cleared_key_invalidates_daily_facts_without_deleting_them(source):
    payload = {"complete": True, "candidates": [], "documents": [{"rows": []}]}
    source._write("20261008", "source_candidates", payload)
    assert "keyFingerprint" not in payload
    assert source._read("20261008", "source_candidates") is not None
    old = source.market.meoz.settings["meozApiKey"]
    source.market.meoz.settings["meozApiKey"] = "changed-key"
    assert source._read("20261008", "source_candidates") is None
    assert not source.status()["ready"]
    source.market.meoz.settings["meozApiKey"] = ""
    assert source.status()["status"] == "unconfigured"
    assert not source.status()["ready"]
    source.market.meoz.settings["meozApiKey"] = old
    assert source._read("20261008", "source_candidates") is not None


def test_data_signature_change_invalidates_daily_snapshot(source):
    source._write("20261008", "source_snapshot", {"records": [], "documents": []})
    with source.database.transaction() as con:
        con.execute("UPDATE research2_base43_daily_tasks SET payload_json=json_set(payload_json, '$.sourceSignature', 'old-format')")
    assert source._read("20261008", "source_snapshot") is None
