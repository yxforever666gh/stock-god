import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from stock_god.market.common import CN, MarketDataError
from stock_god.market.meoz import HISTORY, LIVE, MeozError, MeozProvider
from stock_god.market.meoz_timing import CaptureTiming
from stock_god.market.service import MarketServices

NOW = datetime(2026, 10, 8, 9, 24, 51, tzinfo=CN)
SETTINGS = {"meozApiKey": "transport-fixture-key", "crawlTimeOut": 30}


class Clock:
    def __init__(self):
        self.wall, self.elapsed = NOW, 0.0

    def now(self):
        return self.wall

    def mono(self):
        return self.elapsed

    def advance(self, seconds):
        self.elapsed += seconds
        self.wall += timedelta(seconds=seconds)


def success(fields=None, items=None):
    return httpx.Response(200, json={"code": 200, "data": {
        "fields": fields or ["symbol"], "items": items or []}})


def provider(handler, clock, settings=None):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return MeozProvider(settings or SETTINGS, client, clock=clock.now, monotonic=clock.mono,
                        sleep=clock.advance)


def test_task_clones_share_node_cache_and_timing_but_not_mutable_settings(config):
    urls = []

    def handler(request):
        urls.append(str(request.url))
        if len(urls) == 1:
            raise httpx.ConnectError("private transport-fixture-key", request=request)
        return success()

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        service = MarketServices(config, SETTINGS, client=client)
        first = service.with_settings({**SETTINGS, "nested": {"items": [1]}})
        first.meoz_request("stockbasic", {})
        first.http.cached("calendar:2026", 60, lambda: {"day": True})
        first.settings["nested"]["items"].append(2)
        first.close()
        second = service.with_settings(SETTINGS)
        second.meoz_request("pricelimit", {})
        assert urls == [LIVE[0], LIVE[1], LIVE[1]]
        assert second.http is first.http and second.meoz.timing is first.meoz.timing
        assert second.meoz.settings == SETTINGS and service.settings == SETTINGS
        with second.meoz.timing.phase("calendar"):
            assert second.http.cached("calendar:2026", 60, lambda: pytest.fail("cache lost")) == {"day": True}
        assert second.meoz.timing.snapshot()["cacheHits"] == 1
        other = service.with_settings({**SETTINGS, "meozApiKey": "another-fixture-key"})
        assert other.http is not second.http and other.meoz.node == 0
        assert other.meoz.timing.snapshot()["requestCount"] == 0
        other.close()
        second.close()
        service.close()
        assert not client.is_closed


def test_owned_sessions_close_only_at_root_and_reject_use_after_close(config, monkeypatch):
    real_client, clients = httpx.Client, []

    def make_client(**kwargs):
        client = real_client(transport=httpx.MockTransport(lambda request: success()))
        clients.append(client)
        return client

    monkeypatch.setattr("stock_god.market.service.httpx.Client", make_client)
    service = MarketServices(config, SETTINGS)
    first = service.with_settings(SETTINGS)
    other = service.with_settings({**SETTINGS, "meozApiKey": "different-fixture-key"})
    assert first.http.client is service.http.client and len(clients) == 2
    first.close()
    other.close()
    assert all(not client.is_closed for client in clients)
    service.close()
    service.close()
    assert all(client.is_closed for client in clients)
    with pytest.raises(RuntimeError, match="closed"):
        first.meoz_request("stockbasic", {})


def test_sessions_roll_over_by_day_transport_and_identity_with_bounded_idle_cache(config):
    clock = Clock()
    with httpx.Client(transport=httpx.MockTransport(lambda request: success())) as client:
        service = MarketServices(config, SETTINGS, client=client, _clock=clock.now)
        old = service.with_settings(SETTINGS)
        old.close()
        clock.advance(24 * 60 * 60)
        tomorrow = service.with_settings(SETTINGS)
        assert tomorrow.http is not old.http and tomorrow.meoz.timing is not old.meoz.timing
        changed = tomorrow.with_settings({**SETTINGS, "crawlTimeOut": 12})
        assert changed.http is not tomorrow.http
        changed.close()
        tomorrow.close()
        service.meoz_request("stockbasic", {})
        assert all(key[0] == "2026-10-09" for key in service._pool.entries)
        for number in range(15):
            task = service.with_settings({**SETTINGS, "meozApiKey": f"fixture-{number}"})
            task.close()
        assert len(service._pool.entries) <= 8
        service.close()


@pytest.mark.parametrize("configured,remaining,expected", [(5, 40, 5), (120, 40, 30),
    (None, 40, 30), (30, 4, 4)])
def test_material_request_timeout_uses_config_cap_and_deadline(configured, remaining, expected):
    clock, timeouts = Clock(), []

    def handler(request):
        timeouts.append(request.extensions["timeout"]["read"])
        return success()

    p = provider(handler, clock, {**SETTINGS, "crawlTimeOut": configured})
    try:
        p.request("stockbasic", {}, deadline=NOW + timedelta(seconds=remaining))
        assert timeouts == [expected]
    finally:
        p.client.close()


def test_wall_deadline_checked_before_request_and_after_receipt():
    clock, calls = Clock(), []

    def handler(request):
        calls.append(request)
        clock.advance(2)
        return success()

    p = provider(handler, clock)
    try:
        with pytest.raises(MeozError, match="截止"):
            p.request("stockbasic", {}, deadline=NOW)
        assert not calls and p.timing.snapshot()["requestCount"] == 0
        with pytest.raises(MeozError, match="截止后"):
            p.request("stockbasic", {}, deadline=NOW + timedelta(seconds=1))
        timing = p.timing.snapshot()
        assert timing["requestCount"] == 1 and timing["requests"][0]["outcome"] == "late"
    finally:
        p.client.close()


def test_ticks_failover_uses_one_round_budget_and_preserves_returns():
    clock, timeouts = Clock(), []

    def handler(request):
        timeouts.append(request.extensions["timeout"]["read"])
        clock.advance(1)
        if len(timeouts) == 1:
            raise httpx.ConnectError("private transport-fixture-key", request=request)
        return success(["symbol", "tradedate", "time"], [["000001", "20261008", "2026-10-08 09:24:50"]])

    p = provider(handler, clock)
    try:
        rows = p.ticks("20261008", ["sz000001"], deadline=NOW + timedelta(seconds=40))
        assert timeouts == [3, 2] and p.node == 1
        assert rows[0]["raw"] == {"symbol": "000001", "tradedate": "20261008", "time": "2026-10-08 09:24:50"}
        assert rows[0]["availableAt"] == (NOW + timedelta(seconds=2)).isoformat()
        timing = p.timing.snapshot()
        assert timing["requestCount"] == 2 and timing["requestElapsedSeconds"] == 2
        assert [item["outcome"] for item in timing["requests"]] == ["connection_failed", "ok"]
    finally:
        p.client.close()


def test_ticks_exhausted_budget_does_not_start_later_batch_or_accept_late_data():
    clock, timeouts = Clock(), []

    def handler(request):
        timeouts.append(request.extensions["timeout"]["read"])
        clock.advance(2)
        symbol = json.loads(request.content)["params"]["symbols"][0]
        return success(["symbol", "tradedate", "time"], [[symbol, "20261008", "2026-10-08 09:24:50"]])

    p = provider(handler, clock)
    try:
        with pytest.raises(MeozError, match="截止后"):
            p.ticks("20261008", [f"{number:06d}" for number in range(1, 402)])
        assert timeouts == [3, 1]
        assert p.timing.snapshot()["requests"][-1]["outcome"] == "late"
    finally:
        p.client.close()


def test_ticks_same_identity_cannot_overlap():
    clock, calls = Clock(), []
    p = provider(lambda request: calls.append(request) or success(), clock)
    try:
        assert p._session.poll_lock.acquire(blocking=False)
        with pytest.raises(MeozError, match="正在执行"):
            p.ticks("20261008", ["sz000001"])
        assert not calls
        p._session.poll_lock.release()
        assert p.ticks("20261008", ["sz000001"]) == []
    finally:
        p.client.close()


def test_retry_failure_and_wait_are_measured_separately_and_do_not_switch_node():
    clock, urls = Clock(), []

    def handler(request):
        urls.append(str(request.url))
        clock.advance(.5)
        return httpx.Response(429, headers={"Retry-After": "1"}) if len(urls) < 3 else success()

    p = provider(handler, clock)
    try:
        assert p.request("stockbasic", {})["rows"] == []
        timing = p.timing.snapshot()
        assert urls == [LIVE[0]] * 3 and timing["requestElapsedSeconds"] == 1.5
        assert timing["waits"]["retry"] == 2 and timing["wallSpanSeconds"] == 3.5
        assert [row["outcome"] for row in timing["requests"]] == ["rate_limited", "rate_limited", "ok"]
    finally:
        p.client.close()


def test_permission_and_history_errors_never_fail_over_or_expose_credentials(caplog):
    clock, urls = Clock(), []

    def forbidden(request):
        urls.append(str(request.url))
        return httpx.Response(403, json={"message": "transport-fixture-key"})

    p = provider(forbidden, clock)
    try:
        with caplog.at_level("INFO"), pytest.raises(MeozError) as error:
            p.request("stockbasic", {"private": "not-for-telemetry"})
        assert error.value.status == "no_permission" and urls == [LIVE[0]]
        text = json.dumps(p.timing.snapshot()) + caplog.text + str(error.value)
        assert "transport-fixture-key" not in text and "not-for-telemetry" not in text
    finally:
        p.client.close()

    def unavailable(request):
        urls.append(str(request.url))
        raise httpx.ConnectError("transport-fixture-key", request=request)

    p = provider(unavailable, clock)
    try:
        with pytest.raises(MeozError):
            p.request("tick_history", {}, history=True)
        assert urls[-1] == HISTORY and len(urls) == 2 and p.node == 0
    finally:
        p.client.close()


def test_http_server_errors_retry_same_node_and_connection_reset_can_switch():
    clock, urls = Clock(), []

    def handler(request):
        urls.append(str(request.url))
        if len(urls) == 1:
            raise httpx.ReadError("transport-fixture-key", request=request)
        return httpx.Response(503) if len(urls) < 4 else success()

    p = provider(handler, clock)
    try:
        with pytest.raises(MeozError):
            p.request("stockbasic", {})
        assert urls == [LIVE[0], LIVE[1], LIVE[1]]
        assert [row["outcome"] for row in p.timing.snapshot()["requests"]] == [
            "connection_failed", "server_error", "server_error"]
    finally:
        p.client.close()


def test_timing_restore_is_once_and_unknown_restart_gap_is_not_request_time():
    clock = Clock()
    previous = CaptureTiming(clock=clock.now, monotonic=clock.mono)
    with previous.phase("mother"):
        clock.advance(2)
    previous.record_request(api="stockbasic", node=LIVE[0], attempt=1, outcome="ok", rowCount=1,
        startedAt=NOW, endedAt=clock.now(), elapsedSeconds=2, apikey="must-be-ignored")
    recorded = previous.snapshot()
    previous.restore(recorded)
    assert previous.snapshot()["requestCount"] == 1
    clock.advance(10)
    restored = CaptureTiming(clock=clock.now, monotonic=clock.mono)
    restored.restore(recorded)
    restored.restore(recorded)
    restored.record_wait(-10, "scheduler")
    with restored.phase("mother"):
        clock.elapsed -= 10  # Wall-clock discontinuities cannot create negative elapsed records.
    timing = restored.snapshot()
    assert timing["requestCount"] == 1 and timing["requestElapsedSeconds"] == 2
    assert isinstance(timing["requestCount"], int) and isinstance(timing["cacheHits"], int)
    assert timing["phases"]["mother"]["elapsedSeconds"] == 2
    assert timing["waits"]["scheduler"] == 0 and len(timing["unknownIntervals"]) == 1
    assert "must-be-ignored" not in json.dumps(timing)
    assert timing["interfaces"]["stockbasic"]["elapsedSeconds"] == 2


def test_parallel_request_sum_does_not_claim_to_be_wall_span_and_snapshot_is_detached():
    timer = CaptureTiming(clock=lambda: NOW)
    for api in ("stockbasic", "pricelimit"):
        timer.record_request(api=api, node=LIVE[0], attempt=1, outcome="ok", startedAt=NOW,
                             endedAt=NOW + timedelta(seconds=2), elapsedSeconds=2)
    timer.record_wait(3, "scheduler")
    recorded = timer.snapshot()
    assert recorded["requestElapsedSeconds"] == 4 and recorded["wallSpanSeconds"] == 2
    assert recorded["waits"]["scheduler"] == 3
    assert recorded["requests"][0]["startedAt"].endswith("+08:00")
    recorded["requests"].clear()
    assert timer.snapshot()["requestCount"] == 2 and len(timer.snapshot()["requests"]) == 2


def test_request_detail_retention_does_not_truncate_cumulative_interface_costs():
    timer = CaptureTiming(clock=lambda: NOW)
    for number in range(1030):
        timer.record_request(api="suspend" if number == 0 else "stockbasic", node=LIVE[0],
            attempt=1, outcome="ok", startedAt=NOW, endedAt=NOW + timedelta(seconds=1),
            elapsedSeconds=2000 if number == 0 else 1)
    timing = timer.snapshot()
    assert timing["requestCount"] == 1030 and len(timing["requests"]) == 1024
    assert timing["requestsTruncated"] and timing["slowestInterface"] == "suspend"
    assert timing["requestElapsedSeconds"] == 3029


def test_out_of_order_parallel_completion_retains_outer_wall_bounds_and_old_phase_start():
    clock = Clock()
    prior = CaptureTiming(clock=clock.now, monotonic=clock.mono)
    with prior.phase("mother"):
        clock.advance(1)
    old = prior.snapshot()
    current = CaptureTiming(clock=clock.now, monotonic=clock.mono)
    with current.phase("mother"):
        clock.advance(1)
    current.restore(old)
    assert current.snapshot()["phases"]["mother"]["startedAt"] == NOW.isoformat()
    current.record_request(api="stockbasic", node=LIVE[0], attempt=1, outcome="ok", rowCount=1,
        startedAt=NOW + timedelta(seconds=1), endedAt=NOW + timedelta(seconds=5), elapsedSeconds=4)
    current.record_request(api="pricelimit", node=LIVE[0], attempt=1, outcome="ok", rowCount=1,
        startedAt=NOW, endedAt=NOW + timedelta(seconds=2), elapsedSeconds=2)
    timing = current.snapshot()
    assert timing["startedAt"] == NOW.isoformat()
    assert timing["endedAt"] == (NOW + timedelta(seconds=5)).isoformat()
    assert timing["wallSpanSeconds"] == 5 and timing["requestElapsedSeconds"] == 6


def test_scheduler_wait_is_channel_scoped_and_restart_endpoint_is_unknown():
    clock = Clock()
    timer = CaptureTiming(clock=clock.now, monotonic=clock.mono)
    with timer.scheduled("poll"):
        clock.advance(1)
    clock.advance(2)
    with timer.scheduled("poll"):
        clock.advance(1)
        with timer.scheduled("poll"):
            clock.advance(1)
    assert timer.snapshot()["waits"] == {"scheduler:poll": 2}
    clock.advance(10)
    restarted = CaptureTiming(clock=clock.now, monotonic=clock.mono)
    restarted.restore(timer.snapshot())
    with restarted.scheduled("poll"):
        clock.advance(1)
    assert restarted.snapshot()["waits"] == {"scheduler:poll": 2}


def test_model_and_scoring_timings_do_not_extend_collection_span():
    clock = Clock()
    timer = CaptureTiming(clock=clock.now, monotonic=clock.mono)
    timer.record_request(api="stockbasic", node=LIVE[0], attempt=1, outcome="ok", startedAt=NOW,
        endedAt=NOW, elapsedSeconds=0)
    with timer.phase("validation"):
        clock.advance(2)
    for phase in ("model", "scoring"):
        with timer.phase(phase):
            clock.advance(3)
    timing = timer.snapshot()
    assert timing["wallSpanSeconds"] == 2
    assert timing["phases"]["model"]["elapsedSeconds"] == 3
    assert timing["phases"]["scoring"]["elapsedSeconds"] == 3


def minute_rows(start, end):
    return [{"time": when.isoformat(), "open": 9.0, "high": 11.0, "low": 8.0, "close": 10.0,
             "volume": 100.0, "amount": 1000.0, "source": "tencent:none"} for when in (start, end)]


def test_auction_minutes_use_fully_covered_raw_cache_without_provider_work(config, monkeypatch):
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("network forbidden"))) as client:
        service = MarketServices(config, SETTINGS, client=client)
        start = NOW.replace(day=7, hour=9, minute=30, second=0)
        end = start.replace(hour=15)
        service._save_minute_bars("sz000001", minute_rows(start, end))
        monkeypatch.setattr(service, "_public_minute_sources", lambda *args: pytest.fail("cached window"))
        with service.meoz.timing.phase("previous-minute"):
            rows = service._auction_bars("sz000001", start, end, budget_seconds=3)
        assert len(rows) == 2 and rows[0]["close"] == 10
        assert service.meoz.timing.snapshot()["cacheHits"] == 1
        service.close()


@pytest.mark.parametrize("late", [False, True])
def test_auction_minute_loaders_share_deadline_and_snapshot_without_late_cache_write(config, monkeypatch, late):
    clock, deadlines, snapshots = Clock(), [], []
    monkeypatch.setattr("stock_god.market.service.time", SimpleNamespace(monotonic=clock.mono))
    start, end = NOW.replace(day=7, hour=9, minute=30, second=0), NOW.replace(day=7, hour=15, minute=0, second=0)

    def sources(task, code, window_start, window_end, limit):
        assert code == "sz000001" and (window_start, window_end, limit) == (start, end, 5000)
        snapshots.append(task.settings.copy())
        task.settings["nested"]["items"].append(2)

        def first(deadline):
            deadlines.append(deadline)
            clock.advance(4 if late else 1)
            if late:
                return minute_rows(start, end)
            raise MarketDataError("fixture unavailable")

        def second(deadline):
            assert not late
            deadlines.append(deadline)
            return minute_rows(start, end)

        return [("first", first), ("second", second)]

    monkeypatch.setattr(MarketServices, "_public_minute_sources", sources)
    settings = {**SETTINGS, "crawlTimeOut": 120, "privateMinuteTimeoutSec": 60, "nested": {"items": [1]}}
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("network forbidden"))) as client:
        service = MarketServices(config, settings, client=client)
        if late:
            with pytest.raises(MarketDataError):
                service._auction_bars("sz000001", start, end, budget_seconds=3)
            assert service.cached_bars("sz000001", start, end) == [] and deadlines == [3]
        else:
            rows = service._auction_bars("sz000001", start, end, budget_seconds=3)
            assert len(rows) == 2 and deadlines == [3, 3]
            assert len(service.cached_bars("sz000001", start, end)) == 2
        assert snapshots[0]["crawlTimeOut"] == snapshots[0]["privateMinuteTimeoutSec"] == 3
        assert service.settings == settings and settings["nested"]["items"] == [1]
        service.close()


def test_capture_calendar_and_http_json_count_once_and_background_does_not_leak(config, caplog):
    clock = Clock()

    def handler(request):
        if request.url.host == "api.tushare.pro":
            clock.advance(2)
            return httpx.Response(200, json={"code": 0, "data": {"fields": ["exchange", "cal_date", "is_open"],
                "items": [["SSE", "20261008", 1]]}})
        clock.advance(3)
        return httpx.Response(200, json={"data": {"list": [{"close": 10}, {"close": 11}]}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        service = MarketServices(config, {**SETTINGS, "tushareToken": "fixture-calendar-token"},
                                 client=client, _clock=clock.now)
        timer = service.meoz.timing
        timer.monotonic = clock.mono
        with caplog.at_level("INFO"), timer.phase("calendar"):
            assert service.is_trading_day(NOW)
            assert service.is_trading_day(NOW)
        private_url = "https://fixture-user:fixture-password@provider.example/minutes?apikey=fixture-url-key"
        with caplog.at_level("INFO"), timer.phase("previous-minute"):
            result = service.http.json(private_url, method="POST", body={
                "token": "fixture-body-token", "api_name": "fixture-body-api-secret"})
        assert result == {"data": {"list": [{"close": 10}, {"close": 11}]}}
        recorded = timer.snapshot()
        assert recorded["requestCount"] == 2 and recorded["requestElapsedSeconds"] == 5
        assert recorded["wallSpanSeconds"] == 5 and recorded["cacheHits"] == 1
        assert [row["rowCount"] for row in recorded["requests"]] == [1, 2]
        assert [row["api"] for row in recorded["requests"]] == ["tushare:trade_cal", "provider.example:/minutes"]
        assert recorded["requests"][1]["node"] == "https://provider.example"
        logged = json.dumps(recorded) + caplog.text
        for value in ("fixture-user", "fixture-password", "fixture-url-key", "fixture-body-token",
                      "fixture-body-api-secret", "fixture-calendar-token"):
            assert value not in logged
        assert CaptureTiming.capture_active() is None
        service.http.json("https://provider.example/news?token=fixture-news-key")
        service.is_trading_day(NOW)
        assert timer.snapshot()["requestCount"] == 2 and timer.snapshot()["cacheHits"] == 1
        service.close()


@pytest.mark.asyncio
async def test_capture_context_propagates_to_thread_and_other_key_session_is_excluded(config):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[]))) as client:
        service = MarketServices(config, SETTINGS, client=client)
        other = service.with_settings({**SETTINGS, "meozApiKey": "other-capture-fixture"})
        with service.meoz.timing.phase("calendar"):
            marker = await asyncio.to_thread(CaptureTiming.capture_active)
            assert marker == (service.meoz.timing.run_id, "calendar")
            assert await asyncio.to_thread(service.http.json, "https://calendar.example/query") == []
            other.http.json("https://calendar.example/other")
        assert service.meoz.timing.snapshot()["requestCount"] == 1
        assert other.meoz.timing.snapshot()["requestCount"] == 0
        other.close()
        service.close()


def test_auction_minute_http_capture_keeps_original_identity_through_budget_clone(config, monkeypatch):
    clock = Clock()
    monkeypatch.setattr("stock_god.market.charts.now", clock.now)

    def handler(request):
        assert request.url.host == "ifzq.gtimg.cn"
        clock.advance(1)
        return httpx.Response(200, json={"code": 0, "data": {"sz000001": {"m1": [
            ["202610070930", 9, 10, 11, 8, 1], ["202610071500", 9, 10, 11, 8, 1]]}}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        service = MarketServices(config, {**SETTINGS, "crawlTimeOut": 120, "akshareEnabled": False,
            "sinaMinuteEnabled": False, "privateMinuteEnabled": False}, client=client, _clock=clock.now)
        service.meoz.timing.monotonic = clock.mono
        start = NOW.replace(day=7, hour=9, minute=30, second=0)
        with service.meoz.timing.phase("previous-minute"):
            rows = service._auction_bars("sz000001", start, start.replace(hour=15), 3)
        timing = service.meoz.timing.snapshot()
        assert len(rows) == 2 and timing["requestCount"] == 1 and timing["requestElapsedSeconds"] == 1
        assert timing["requests"][0]["rowCount"] == 2 and not timing["operations"]
        assert service.settings["crawlTimeOut"] == 120
        service.close()


def test_opaque_akshare_operation_is_separate_and_child_http_count_remains_unknown(config, monkeypatch):
    clock = Clock()
    start, end = NOW.replace(day=7, hour=9, minute=30, second=0), NOW.replace(day=7, hour=15, minute=0, second=0)

    def sources(task, *args):
        def load(deadline):
            clock.advance(2)
            return minute_rows(start, end)
        return [("akshare", load)]

    monkeypatch.setattr(MarketServices, "_public_minute_sources", sources)
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("no parent HTTP"))) as client:
        service = MarketServices(config, SETTINGS, client=client, _clock=clock.now)
        service.meoz.timing.monotonic = clock.mono
        with service.meoz.timing.phase("previous-minute"):
            assert len(service._auction_bars("sz000001", start, end, 3)) == 2
        timing = service.meoz.timing.snapshot()
        assert timing["requestCount"] == 0 and not timing["httpRequestsComplete"]
        assert timing["operations"]["akshare"]["count"] == 1
        assert timing["operations"]["akshare"]["elapsedSeconds"] == 2
        assert timing["operations"]["akshare"]["httpRequestCount"] is None
        assert timing["phases"]["external:akshare"]["elapsedSeconds"] == 2
        service.close()


def test_capture_http_default_read_timeout_is_capped_without_changing_background(config):
    timeouts = []

    def handler(request):
        timeouts.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json=[])

    with httpx.Client(transport=httpx.MockTransport(handler), timeout=300) as client:
        service = MarketServices(config, {**SETTINGS, "crawlTimeOut": 300}, client=client)
        with service.meoz.timing.phase("calendar"):
            assert service.http.json("https://calendar.example/query") == []
            service.http.json("https://calendar.example/short", timeout=2)
        service.http.json("https://background.example/news")
        assert timeouts == [30, 2, 300] and client.timeout.read == 300
        assert service.settings["crawlTimeOut"] == 300 and service.meoz.timing.snapshot()["requestCount"] == 2
        service.close()


def test_capture_http_deadline_is_checked_before_and_after_receipt_and_restored(config):
    clock, timeouts = Clock(), []

    def handler(request):
        timeouts.append(request.extensions["timeout"]["read"])
        clock.advance(2)
        return httpx.Response(200, json={"data": {"list": [{"close": 10}]}})

    with httpx.Client(transport=httpx.MockTransport(handler), timeout=300) as client:
        service = MarketServices(config, SETTINGS, client=client, _clock=clock.now)
        timer = service.meoz.timing
        timer.monotonic = clock.mono
        with timer.phase("prepare", deadline=NOW + timedelta(seconds=1)):
            with timer.phase("calendar", deadline=NOW + timedelta(seconds=10)):
                assert timer.remaining_budget() == 1
                with pytest.raises(MarketDataError, match="after deadline"):
                    service.http.json("https://calendar.example/query")
            with timer.phase("calendar"):
                with pytest.raises(MarketDataError, match="deadline exhausted"):
                    service.http.json("https://calendar.example/never")
        assert timeouts == [1] and timer.capture_deadline() is None
        recorded = timer.snapshot()
        assert recorded["requestCount"] == 1 and recorded["requests"][0]["outcome"] == "late"
        assert recorded["requests"][0]["rowCount"] is None
        service.close()
