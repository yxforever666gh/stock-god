"""Collection progress survives partial facts without weakening causal book coverage."""

import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Event
from types import SimpleNamespace

import httpx
import pytest

from stock_god.market.common import CN, MarketDataError
from stock_god.market.meoz import MeozError, MeozProvider
from stock_god.market.meoz_source import MeozAuctionSource
from stock_god.storage.current import BASE43_DDL
from stock_god.storage.db import Database

DAY, PREVIOUS = "20261008", "20261007"


@pytest.fixture
def source(tmp_path):
    db = Database(tmp_path / "auction.db")
    with db.transaction() as con:
        for sql in BASE43_DDL:
            con.execute(sql)
    now = [datetime(2026, 10, 8, 9, 15, tzinfo=CN)]
    provider = MeozProvider({"meozApiKey": "fixture-only-key"},
                            httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500))),
                            clock=lambda: now[0])
    market = SimpleNamespace(meoz=provider)
    result = MeozAuctionSource(db, market, lambda: now[0])
    result.now = now
    yield result
    provider.client.close()
    db.close()


def request_facts(source, symbols=("000001", "000002")):
    calls = []

    def request(api, params, fields=None, **kwargs):
        calls.append((api, params.copy()))
        requested = params.get("symbols", symbols)
        if isinstance(requested, str):
            requested = requested.split(",")
        if api == "limit_pool_yes":
            rows = [{"tradedate": DAY, "symbol": symbol, "pre_type": "u", "pre_limit_times": 1}
                    for symbol in symbols]
        elif api == "stockbasic":
            rows = [{"symbol": symbol, "name": "普通股票", "market": "主板", "list_status": "L"}
                    for symbol in requested]
        elif api == "pricelimit":
            rows = [{"symbol": symbol, "tradedate": params["tradedate"],
                     "pre_close": 9, "up_limit": 9.9, "down_limit": 8.1} for symbol in requested]
        else:
            rows = []
        return {"rows": rows, "fields": [], "source": "fixture-node",
                "receivedAt": source.clock().isoformat()}

    source._request = request
    source.market.bars = lambda *args, **kwargs: [{"time": "2026-10-07T09:31:00+08:00",
        "open": 9.9, "high": 9.9, "low": 9.9, "close": 9.9, "volume": 100}]
    source._history_volumes = lambda *args: [100] * 20
    return calls, request


def records(source, symbols):
    return [{"code": code, "raw": {"close": 9.9, "vol": 1, "amount": 990,
             "bid1": 9.9, "ask1": 9.9, "bid_vol1": 1, "ask_vol1": 1},
             "asOf": source.clock().isoformat(), "availableAt": source.clock().isoformat()}
            for code in symbols]


def test_slow_previous_minute_does_not_block_mother_ticks(source):
    request_facts(source)
    started, release = Event(), Event()
    original = source.market.bars

    def slow_bars(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    source.market.bars = slow_bars
    source.market.meoz_ticks = lambda day, symbols, **kwargs: records(source, symbols)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(source.prepare, DAY, PREVIOUS)
        try:
            assert started.wait(5)
            saved = source._read(DAY, "source_candidates")
            assert not saved["complete"] and saved["motherSymbols"] == ["000001", "000002"]
            assert source.poll(DAY)
            assert len(source._read(DAY, "source_snapshot")["records"]) == 2
        finally:
            release.set()
        assert future.result(timeout=5)["complete"]


def test_failed_fifth_document_preserves_first_four_and_retries_only_gap(source):
    calls, original = request_facts(source)
    failed = [False]

    def flaky(api, params, fields=None, **kwargs):
        if api == "suspend" and not failed[0]:
            failed[0] = True
            calls.append((api, params.copy()))
            raise MeozError("incomplete", "fixture timeout")
        return original(api, params, fields, **kwargs)

    source._request = flaky
    first = source.prepare(DAY, PREVIOUS)
    assert not first["complete"] and len(first["documents"]) == 4
    assert first["motherSymbols"] == ["000001", "000002"]
    assert first["progress"]["gaps"]["qualification"] == "fixture timeout"
    second = source.prepare(DAY, PREVIOUS)
    assert second["complete"] and len(second["candidates"]) == 2
    assert Counter(api for api, _ in calls) == {"limit_pool_yes": 1, "stockbasic": 1,
                                             "pricelimit": 2, "suspend": 2}


def test_partial_qualification_queries_only_uncovered_stock(source):
    calls, original = request_facts(source)
    first = [True]

    def partial(api, params, fields=None, **kwargs):
        result = original(api, params, fields, **kwargs)
        if api == "stockbasic" and first[0]:
            first[0] = False
            result["rows"] = result["rows"][:1]
        return result

    source._request = partial
    assert not source.prepare(DAY, PREVIOUS)["complete"]
    assert source.prepare(DAY, PREVIOUS)["complete"]
    assert [p["symbols"] for api, p in calls if api == "stockbasic"] == ["000001,000002", "000002"]


def test_history_failure_keeps_completed_candidate_and_minute(source):
    request_facts(source)
    minutes, history = [], []
    original = source.market.bars
    source.market.bars = lambda code, *args, **kwargs: (minutes.append(code) or original(code, *args, **kwargs))
    fail = [True]

    def volumes(symbol, *args):
        history.append(symbol)
        if symbol == "000002" and fail[0]:
            fail[0] = False
            raise MeozError("incomplete", "history gap")
        return [100] * 20

    source._history_volumes = volumes
    result = source.prepare(DAY, PREVIOUS)
    assert not result["complete"] and [c["code"] for c in result["candidates"]] == ["sz000001"]
    assert source.prepare(DAY, PREVIOUS)["complete"]
    assert minutes == ["sz000001", "sz000002"]


def test_history_pages_resume_and_calendar_is_computed_once(source):
    calendar, requests, fail = [], [], [True]
    source.market.is_trading_day = lambda at: (calendar.append(at) or at.weekday() < 5)

    def request(api, params, fields=None, **kwargs):
        requests.append((api, params.copy()))
        if api == "pricelimit":
            rows = []
        elif params["offset"] == 0 and params["startdate"][:6] == "202609":
            rows = [{"symbol": "000001", "tradedate": "20260901", "time": "09:25:00",
                     "m_price": 10, "auc_pct_chg": 0, "auc_vol": 1, "auc_amt": 1000}] * 6000
        elif params["offset"] == 6000 and fail[0]:
            fail[0] = False
            raise MeozError("incomplete", "page gap")
        else:
            rows = []
        return {"rows": rows, "receivedAt": source.clock().isoformat()}

    source._request = request
    with pytest.raises(MeozError, match="page gap"):
        source._history_volumes("000001", DAY, source.clock().replace(minute=29, second=59))
    count = len(calendar)
    source._history_volumes("000001", DAY, source.clock().replace(minute=29, second=59))
    assert len(calendar) == count
    pages = [p for api, p in requests if api == "daily_auc_detail" and p["startdate"][:6] == "202609"]
    assert [p["offset"] for p in pages] == [0, 6000, 6000]
    assert all(p["startdate"][:6] == p["enddate"][:6] for api, p in requests if api == "daily_auc_detail")


def test_first_tick_batch_survives_second_batch_failure(source):
    codes = [f"sz{i:06d}" for i in range(1, 202)]
    source._write(DAY, "source_candidates", {"complete": False, "candidates": [], "documents": [],
                                          "motherSymbols": [c[2:] for c in codes]})

    def ticks(day, symbols, **kwargs):
        if symbols == codes[200:]:
            raise MeozError("incomplete", "second batch timeout")
        return records(source, symbols)

    source.market.meoz_ticks = ticks
    assert not source.poll(DAY)
    saved = source._read(DAY, "source_snapshot")
    assert len(saved["records"]) == 200 and saved["error"] == "second batch timeout"


def test_final_document_failure_keeps_both_successful_tick_batches(source):
    source.now[0] = source.now[0].replace(minute=25)
    source._write(DAY, "source_candidates", {"complete": False, "candidates": [], "documents": [],
                                          "motherSymbols": ["000001"]})
    source.market.meoz_ticks = lambda day, symbols, **kwargs: records(source, symbols)
    source._request = lambda *args, **kwargs: (_ for _ in ()).throw(MeozError("incomplete", "detail timeout"))
    assert not source.poll(DAY)
    assert len(source._read(DAY, "source_snapshot")["records"]) == 2


@pytest.mark.parametrize("replacement", ["replacement-fixture-key", ""])
def test_changed_production_key_revokes_old_write_without_losing_old_facts(source, replacement):
    source._write(DAY, "source_snapshot", {"records": ["old-fact"], "documents": []})
    with source.database.transaction() as con:
        con.execute("CREATE TABLE research_settings(center TEXT PRIMARY KEY,config_json TEXT)")
        con.execute("INSERT INTO research_settings VALUES ('research2',?)",
                    (json.dumps({"meozApiKey": replacement}),))
    with pytest.raises(MeozError, match="旧采集写入已撤销"):
        source._write(DAY, "source_snapshot", {"records": ["stale-replacement"], "documents": []})
    assert source._read(DAY, "source_snapshot")["records"] == ["old-fact"]


def test_ordinary_detail_pages_are_diagnostic_and_do_not_create_order_books(source):
    source.now[0] = source.now[0].replace(minute=26)
    source._write(DAY, "source_candidates", {"complete": True, "candidates": [
        {"code": "sz000001", "reference": 900}], "documents": []})
    source._write(DAY, "source_snapshot", {"records": [], "documents": [], "finalComplete": True})
    source.market.meoz_ticks = lambda *args, **kwargs: []
    calls = []

    def detail(api, params, fields=None, **kwargs):
        calls.append(params)
        assert "trademin" not in params and params["order_dir"] == "asc"
        rows = [{"symbol": "000001", "tradedate": DAY, "time": "2026-10-08T09:20:00+08:00",
                 "m_price": 9.9, "auc_vol": 1, "auc_amt": 990}] * (6000 if params["offset"] == 0 else 1)
        return {"rows": rows, "receivedAt": source.clock().isoformat()}

    source._request = detail
    assert source.poll(DAY)
    assert not source._read(DAY, "source_details")["complete"]
    source.now[0] += timedelta(seconds=3)
    assert source.poll(DAY)
    assert source._read(DAY, "source_details")["complete"]
    assert [p["offset"] for p in calls] == [0, 6000]
    assert source._read(DAY, "source_snapshot")["records"] == []
    assert not source.freeze(DAY, source.clock())["complete"]


def test_timing_is_persisted_and_stage_does_not_change_daily_candidates(source):
    request_facts(source)
    prepared = source.prepare(DAY, PREVIOUS)
    with source.stage("model"):
        pass
    timing = source._read(DAY, "source_timing")
    assert all(name in timing["phases"] for name in ("mother", "qualification", "previous-minute", "history", "model"))
    assert source._read(DAY, "source_candidates") == prepared
    assert "fixture-only-key" not in json.dumps(timing)


def test_permanently_missing_first_minute_does_not_starve_second_stock(source):
    request_facts(source)
    original = source.market.bars
    calls = []

    def minute(code, *args, **kwargs):
        calls.append(code)
        if code == "sz000001":
            raise MarketDataError("fixture missing")
        return original(code, *args, **kwargs)

    source.market.bars = minute
    first = source.prepare(DAY, PREVIOUS)
    assert not first["complete"]
    assert [c["code"] for c in first["candidates"]] == ["sz000002"]
    assert "previous-minute:000001" in first["progress"]["gaps"]
    second = source.prepare(DAY, PREVIOUS)
    assert not second["complete"] and len(second["candidates"]) == 1
    assert calls == ["sz000001", "sz000002", "sz000001"]


def test_preparation_stops_further_io_at_real_deadline(source):
    request_facts(source)
    original = source.market.bars
    calls = []

    def minute(code, *args, **kwargs):
        calls.append(code)
        source.now[0] = source.clock().replace(minute=30)
        return original(code, *args, **kwargs)

    source.market.bars = minute
    source._history_volumes = lambda *args: pytest.fail("must not start history after deadline")
    result = source.prepare(DAY, PREVIOUS)
    assert not result["complete"] and calls == ["sz000001"]
    assert "000001" in result["progress"]["minutes"] and "cutoff" in result["progress"]["gaps"]


def test_strict_book_gap_is_explicit_and_not_repairable_from_details(source):
    source.now[0] = source.clock().replace(minute=26)
    source._write(DAY, "source_candidates", {"complete": True, "candidates": [
        {"code": "sz000001", "reference": 900}], "documents": []})
    source._write(DAY, "source_snapshot", {"records": records(source, ["sz000001"]), "documents": []})
    result = source.freeze(DAY, source.clock())
    assert not result["complete"]
    gap = result["gaps"][0]
    assert not gap["recoverable"] and not gap["startCovered"]
    assert gap["checkpointsMissing"] == [33600, 33840, 33890]
    assert source._read(DAY, "source_freeze")["gaps"] == result["gaps"]
    assert source._read(DAY, "source_timing")["completeInputs"] is False


def test_upgrade_reuses_legacy_mother_document(source):
    calls, request = request_facts(source)
    document = request("limit_pool_yes", {"tradedate": DAY})
    document["fields"] = ["tradedate", "symbol", "pre_type", "pre_limit_times"]
    calls.clear()
    source._write(DAY, "source_candidates", {"complete": False, "candidates": [], "documents": [document]})
    result = source.collect_mother(DAY)
    assert result["motherSymbols"] == ["000001", "000002"] and calls == []
    assert len(result["documents"]) == 1


def test_cache_keeps_live_and_history_contracts_separate(source):
    calls = []

    def request(api, params, fields=None, **kwargs):
        calls.append(kwargs.get("history", False))
        return {"rows": [], "source": "history" if kwargs.get("history") else "live"}

    source._request = request
    assert source._cached_request(DAY, "tick_history", {}, history=False)["source"] == "live"
    assert source._cached_request(DAY, "tick_history", {}, history=True)["source"] == "history"
    assert source._cached_request(DAY, "tick_history", {}, history=False)["source"] == "live"
    assert calls == [False, True]


def test_status_reports_qualified_total_instead_of_unqualified_mother_total(source):
    _, original = request_facts(source)

    def qualified(api, params, fields=None, **kwargs):
        result = original(api, params, fields, **kwargs)
        if api == "stockbasic":
            result["rows"][0]["name"] = "ST测试"
        return result

    source._request = qualified
    assert source.prepare(DAY, PREVIOUS)["complete"]
    assert "母体2只，合格1只，历史完成1/1只" in source.status()["message"]


def test_known_eligible_symbols_exclude_other_mother_before_history_finishes(source):
    source.now[0] = source.clock().replace(minute=25)
    source._write(DAY, "source_candidates", {"complete": False, "candidates": [], "documents": [],
        "motherSymbols": ["000001", "000002"], "progress": {"eligibleSymbols": ["000002"]}})
    requested = []

    def ticks(day, symbols, **kwargs):
        requested.append(list(symbols))
        return records(source, symbols)

    source.market.meoz_ticks = ticks
    source._request = lambda *args, **kwargs: {"rows": [{"symbol": "000002", "tradedate": DAY,
        "m_price": 9.9, "auc_vol": 1, "auc_amt": 990}], "receivedAt": source.clock().isoformat()}
    assert source.poll(DAY)
    assert requested == [["sz000002"], ["sz000002"]]
    assert source._read(DAY, "source_snapshot")["finalComplete"]
    assert not source._read(DAY, "source_candidates")["complete"]


def test_complete_empty_qualification_can_finish_without_network_poll(source):
    source._write(DAY, "source_candidates", {"complete": True, "candidates": [],
        "documents": [{"rows": []}], "motherSymbols": [], "progress": {"eligibleSymbols": []}})
    assert source.poll(DAY)
    assert source.freeze(DAY, source.clock())["complete"]


def test_final_snapshot_resumes_detail_in_next_real_clock_budget(source, monkeypatch):
    import stock_god.market.meoz_source as source_module

    source.now[0] = source.clock().replace(minute=25)
    first = source.clock()
    elapsed, calls = [0.0], []
    monkeypatch.setattr(source_module.time, "monotonic", lambda: elapsed[0])
    source._write(DAY, "source_candidates", {"complete": False, "candidates": [], "documents": [],
                                          "motherSymbols": ["000001"]})

    def advance(seconds):
        elapsed[0] += seconds
        source.now[0] += timedelta(seconds=seconds)

    def ticks(day, symbols, *, final=False, deadline=None, budget_seconds=None):
        calls.append("final" if final else "ordinary")
        assert budget_seconds >= 1.1
        advance(1.1)
        volume = 1 if final else 2
        return [{"code": code, "raw": {"close": 9.9, "vol": volume, "amount": 990 * volume},
                 "asOf": (first if final else source.clock()).isoformat(),
                 "availableAt": source.clock().isoformat()} for code in symbols]

    def detail(api, params, fields=None, **kwargs):
        calls.append("detail")
        budget = kwargs["budget_seconds"]
        if budget < 1.1:
            advance(budget)
            raise MeozError("incomplete", "fixture read timeout")
        advance(1.1)
        return {"rows": [{"symbol": symbol, "tradedate": DAY,
                          "m_price": 9.9, "auc_vol": 1, "auc_amt": 990}
                         for symbol in params["symbols"]], "receivedAt": source.clock().isoformat()}

    source.market.meoz_ticks, source._request = ticks, detail
    assert not source.poll(DAY)
    saved = source._read(DAY, "source_snapshot")
    assert saved["finalFetchedSymbols"] == ["sz000001"]
    assert not saved.get("finalComplete")
    assert source.clock() == first + timedelta(seconds=3)
    assert source.poll(DAY)
    assert source._read(DAY, "source_snapshot")["finalComplete"]
    assert calls == ["ordinary", "final", "detail", "ordinary", "detail"]


def test_ordinary_later_valid_quote_does_not_satisfy_after_0925_fetch(source):
    source.now[0] = source.clock().replace(minute=25, second=15)
    source._write(DAY, "source_candidates", {"complete": False, "candidates": [], "documents": [],
                                          "motherSymbols": ["000001"]})
    source._write(DAY, "source_snapshot", {"records": records(source, ["sz000001"]), "documents": []})
    calls = []

    def ticks(day, symbols, *, final=False, **kwargs):
        calls.append(final)
        result = records(source, symbols)
        if final:
            result[0]["asOf"] = source.clock().replace(second=0).isoformat()
        return result

    source.market.meoz_ticks = ticks
    source._request = lambda *args, **kwargs: {"rows": [{"symbol": "000001", "tradedate": DAY,
        "m_price": 9.9, "auc_vol": 1, "auc_amt": 990}], "receivedAt": source.clock().isoformat()}
    assert source.poll(DAY)
    saved = source._read(DAY, "source_snapshot")
    assert calls == [False, True] and saved["finalFetchedSymbols"] == ["sz000001"]
    assert any(row.get("boundaryFinal") and "09:25:00" in row["asOf"] for row in saved["records"])


def test_final_retry_only_queries_unfinished_symbols(source):
    source.now[0] = source.clock().replace(minute=25, second=5)
    source._write(DAY, "source_candidates", {"complete": False, "candidates": [], "documents": [],
                                          "motherSymbols": ["000001", "000002"]})
    first = records(source, ["sz000001"])[0]
    first.update(boundaryFinal=True, asOf=source.clock().replace(second=0).isoformat())
    source._write(DAY, "source_snapshot", {"records": [first], "documents": [{
        "rows": [{"symbol": "000001", "tradedate": DAY, "m_price": 9.9, "auc_vol": 1, "auc_amt": 990}],
        "receivedAt": source.clock().isoformat()}]})
    requests = []

    def ticks(day, symbols, *, final=False, **kwargs):
        if final:
            requests.append(("tick", list(symbols)))
        result = records(source, symbols)
        if final:
            for row in result:
                row["asOf"] = source.clock().replace(second=0).isoformat()
        return result

    def detail(api, params, fields=None, **kwargs):
        requests.append(("detail", params["symbols"]))
        return {"rows": [{"symbol": symbol, "tradedate": DAY, "m_price": 9.9, "auc_vol": 1, "auc_amt": 990}
                         for symbol in params["symbols"]], "receivedAt": source.clock().isoformat()}

    source.market.meoz_ticks, source._request = ticks, detail
    assert source.poll(DAY)
    assert requests == [("tick", ["sz000002"]), ("detail", ["000002"])]
    assert source._read(DAY, "source_snapshot")["finalComplete"]


def test_history_scan_ends_at_last_open_day_before_cross_month_holiday(source):
    source.market.is_trading_day = lambda at: at.weekday() < 5 and at.month != 10
    requests = []

    def request(api, params, fields=None, **kwargs):
        requests.append((api, params.copy()))
        return {"rows": [], "receivedAt": source.clock().isoformat()}

    source._request = request
    values = source._history_volumes("000001", DAY, source.clock().replace(minute=29, second=59))
    pages = [params for api, params in requests if api == "daily_auc_detail"]
    assert len(values) == 20 and len(pages) == 1
    assert pages[0]["startdate"].startswith("202609") and pages[0]["enddate"] == "20260930"
    assert source._read(DAY, "source_candidates")["progress"]["historyDates"][-1] == "20260930"


def test_stage_uses_explicit_day_deadline_and_model_has_no_capture_budget(source):
    timer = source.market.meoz.timing
    with source.stage("calendar", day="20261009"):
        assert timer.capture_deadline() == source._deadline("20261009")
        assert timer.remaining_budget() > 24 * 60 * 60
    assert source._read("20261009", "source_timing") is not None
    assert source._read(DAY, "source_timing") is None
    with source.stage("model"):
        assert timer.capture_active() is None and timer.remaining_budget() is None
    assert "model" in source._read(DAY, "source_timing")["phases"]
