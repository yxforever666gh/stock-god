import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from stock_god.market.common import CN, timestamp
from stock_god.market.meoz import MeozError, MeozProvider
from stock_god.market.meoz_stream import (
    FIELDS,
    StreamNormalizer,
    decode_rows,
    stream_records,
    subscription_ack,
    subscription_symbols,
    ticket_url,
)

NOW = datetime(2026, 10, 9, 9, 25, 1, tzinfo=CN)
CUTOFF = NOW.replace(minute=29, second=59)
SYMBOLS = ["sz000001", "sh600000"]
TICKET = {"url": "wss://meoz.cn/api/tick_stream_v1?ticket=never-log-this", "tick_sub_limit": 6000,
          "stream_version": "tick_v1"}


def provider(handler, **kwargs):
    return MeozProvider({"meozApiKey": "fixture-only-secret"},
                        httpx.Client(transport=httpx.MockTransport(handler)), clock=lambda: NOW, **kwargs)


def native(code="000001.SZ", *, when=None):
    when = when or NOW.replace(second=0)
    return {"symbol": code, "time": int(when.timestamp() * 1000), "lastPrice": 10,
            "volume": 10000, "amount": 100000, "bidPrice": [10, 9.99, 9.98, 9.97, 9.96],
            "askPrice": [10.01, 10.02, 10.03, 10.04, 10.05], "bidVol": [1000, 2000, 3000, 4000, 5000],
            "askVol": [2000, 3000, 4000, 5000, 6000], "send_ts_ms": int(when.timestamp() * 1000) + 100}


def frame(rows, fields=FIELDS):
    return {"type": "ticks", "i": [[r[k] for k in fields] for r in rows]}


def witness(row):
    result = {"symbol": row["symbol"][:6], "time": timestamp(row["time"]).isoformat()}
    for side, key in (("bid", "bidVol"), ("ask", "askVol")):
        for i in range(2):
            result[side + "_vol" + str(i + 1)] = row[key][i] / 100
            result[side + str(i + 1)] = row["bidPrice" if side == "bid" else "askPrice"][i]
    return result


def test_mainboard_nonst_universe_uses_authoritative_metadata():
    rows = [["000001", "平安银行", "主板", "SZSE", "L"], ["600000", "浦发银行", "主板", "SSE", "L"],
            ["000002", "*st样例", "主板", "SZSE", "L"], ["300001", "创业样例", "创业板", "SZSE", "L"],
            ["688001", "科创样例", "科创板", "SSE", "L"], ["920001", "北交样例", "北交所", "BSE", "L"],
            ["000003", "退市样例", "主板", "SZSE", "D"]]
    def handler(request):
        body = json.loads(request.content)
        assert body["params"] == {"market": "主板", "list_status": "L"}
        return httpx.Response(200, json={"code": 200, "data": {
            "fields": ["symbol", "name", "market", "exchange", "list_status"], "items": rows}})
    assert subscription_symbols(provider(handler), deadline=CUTOFF) == ["sh600000", "sz000001"]


@pytest.mark.parametrize("limit", [1, None, True, 0])
def test_ticket_rejects_insufficient_or_invalid_slot_limit(limit):
    with pytest.raises(MeozError):
        ticket_url({**TICKET, "tick_sub_limit": limit}, 2)


def test_ticket_request_is_bounded_and_credential_url_is_not_telemetry():
    calls = []
    def handler(request):
        calls.append(request)
        assert json.loads(request.content)["symbols"] == ["000001.SZ", "600000.SH"]
        return httpx.Response(200, json={"code": 200, "data": TICKET})
    p = provider(handler)
    result = p.stream_ticket(SYMBOLS, deadline=CUTOFF)
    assert result == TICKET
    assert str(calls[0].url) == "https://sz.meoz.cn:6688/api/v2/realtime/ws-ticket"
    assert "never-log-this" not in json.dumps(p.timing.snapshot())
    assert "fixture-only-secret" not in json.dumps(p.timing.snapshot())


@pytest.mark.parametrize("uri", ["ws://meoz.cn/api/tick_stream_v1?ticket=x", "wss://evil.example/api/tick_stream_v1",
                                  "wss://meoz.cn:6688/api/tick_stream_v1", "wss://meoz.cn/wrong"])
def test_only_official_wss_ticket_url_is_accepted(uri):
    with pytest.raises(MeozError):
        ticket_url({**TICKET, "url": uri}, 2)


def test_subscription_ack_must_confirm_every_symbol_and_fields():
    fields = list(reversed(FIELDS))
    assert subscription_ack({"fields": fields, "symbols": ["000001.SZ", "600000.SH"]}, SYMBOLS) == fields
    assert subscription_ack({"fields": fields, "subscribed_count": 2}, SYMBOLS) == fields
    for ack in ({"fields": fields, "count": 1}, {"fields": fields},
                {"fields": fields[:-1], "count": 2}, {"fields": fields, "symbols": ["000001.SZ"]}):
        with pytest.raises(MeozError):
            subscription_ack(ack, SYMBOLS)
    assert subscription_ack({"type": "connected"}, SYMBOLS) is None


def test_reordered_fields_and_share_to_lot_conversion_are_evidence_based():
    row = native()
    fields = list(reversed(FIELDS))
    decoded = decode_rows(frame([row], fields), fields, set(SYMBOLS), NOW, CUTOFF)[0]
    normalizer = StreamNormalizer()
    with pytest.raises(MeozError):
        normalizer.normalize(decoded, "wss://meoz.cn/api/tick_stream_v1")
    normalizer.certify_books(decoded, witness(row))
    result = normalizer.normalize(decoded, "wss://meoz.cn/api/tick_stream_v1")
    assert result["raw"]["vol"] == 100
    assert result["raw"]["bid_vol1"] == 10
    assert result["volumeUnit"] == result["bookVolumeUnit"] == "lot"
    assert result["asOf"] == timestamp(row["time"]).isoformat()
    assert result["availableAt"] == NOW.isoformat()
    assert "ticket" not in result["source"]


def test_unknown_cumulative_units_are_not_guessed_from_preauction_data():
    row = native(when=NOW.replace(minute=24, second=0))
    decoded = decode_rows(frame([row]), list(FIELDS), set(SYMBOLS), NOW, CUTOFF)[0]
    n = StreamNormalizer()
    n.certify_books(decoded, witness(row))
    result = n.normalize(decoded, "source")
    assert result["raw"]["vol"] is None
    assert n.volume_divisor is None
    row["volume"] = 0
    decoded = decode_rows(frame([row]), list(FIELDS), set(SYMBOLS), NOW, CUTOFF)[0]
    assert n.normalize(decoded, "source")["raw"]["vol"] == 0


@pytest.mark.parametrize("change", [{"symbol": "300001.SZ"}, {"time": None}, {"bidVol": [1, 2]},
                                  {"amount": float("inf")}, {"bidPrice": [-1] * 5}, {"send_ts_ms": 9999999999999}])
def test_invalid_frames_are_not_silently_normalized(change):
    with pytest.raises(MeozError):
        decode_rows(frame([{**native(), **change}]), list(FIELDS), set(SYMBOLS), NOW, CUTOFF)


def test_wrong_day_and_late_receipt_are_rejected():
    with pytest.raises(MeozError):
        decode_rows(frame([native(when=NOW - timedelta(days=1))]), list(FIELDS), set(SYMBOLS), NOW, CUTOFF)
    with pytest.raises(MeozError):
        decode_rows(frame([native()]), list(FIELDS), set(SYMBOLS), CUTOFF + timedelta(seconds=1), CUTOFF)


@pytest.mark.asyncio
async def test_stream_confirms_collects_normalizes_and_closes_at_deadline():
    clock = [NOW]
    row = native()
    messages = [{"type": "connected"}, {"type": "ack", "op": "sub", "count": 2, "fields": list(FIELDS)}, frame([row])]
    closed, statuses = [], []
    class Socket:
        async def recv(self):
            if messages:
                return json.dumps(messages.pop(0))
            clock[0] = CUTOFF
            return "{}"
    class Connection:
        async def __aenter__(self): return Socket()
        async def __aexit__(self, *args): closed.append(True)
    def connector(uri, **kwargs):
        assert uri == TICKET["url"] and kwargs["proxy"] is None
        assert kwargs["logger"].disabled
        return Connection()
    p = SimpleNamespace(clock=lambda: clock[0], monotonic=lambda: 100, settings={},
                        stream_ticket=lambda *a, **k: TICKET, tick_history=lambda *a, **k: {"rows": [witness(row)]})
    captured = [r async for batch in stream_records(p, SYMBOLS, deadline=CUTOFF, on_status=statuses.append, connector=connector) for r in batch]
    assert len(captured) == 1 and captured[0]["raw"]["vol"] == 100
    assert closed == [True]
    assert any(s["status"] == "subscribed" and s["confirmedCount"] == 2 for s in statuses)
    assert statuses[-1]["status"] == "complete"
    assert "never-log-this" not in json.dumps(statuses)


@pytest.mark.asyncio
async def test_stream_retries_are_bounded_and_do_not_log_websocket_exception():
    attempts, statuses = [], []
    class Connection:
        async def __aenter__(self):
            attempts.append(1)
            raise OSError("wss://meoz.cn/api/tick_stream_v1?ticket=never-log-this")
        async def __aexit__(self, *args): pass
    async def no_wait(seconds): pass
    p = SimpleNamespace(clock=lambda: NOW, monotonic=lambda: 100, settings={}, stream_ticket=lambda *a, **k: TICKET)
    with pytest.raises(MeozError, match="订阅无法继续"):
        async for _ in stream_records(p, SYMBOLS, deadline=CUTOFF, on_status=statuses.append,
                                      connector=lambda *a, **k: Connection(), sleep=no_wait):
            pass
    assert len(attempts) == 5
    assert "never-log-this" not in json.dumps(statuses)


@pytest.mark.asyncio
async def test_cancelling_stream_closes_connection_without_reconnect():
    active, closed = asyncio.Event(), []
    class Socket:
        async def recv(self):
            active.set()
            await asyncio.Future()
    class Connection:
        async def __aenter__(self): return Socket()
        async def __aexit__(self, *args): closed.append(True)
    p = SimpleNamespace(clock=lambda: NOW, monotonic=lambda: 100, settings={}, stream_ticket=lambda *a, **k: TICKET)
    async def consume():
        async for _ in stream_records(p, SYMBOLS, deadline=CUTOFF, connector=lambda *a, **k: Connection()):
            pass
    task = asyncio.create_task(consume())
    await active.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True]


def test_nullable_server_send_timestamp_is_preserved_without_inventing_it():
    row = {**native(), "send_ts_ms": None}
    decoded = decode_rows(frame([row]), list(FIELDS), set(SYMBOLS), NOW, CUTOFF)[0]
    normalizer = StreamNormalizer()
    normalizer.certify_books(decoded, witness(row))
    result = normalizer.normalize(decoded, "source")
    assert result["sendAt"] is None and result["availableAt"] == NOW.isoformat()


def test_mainboard_null_exchange_uses_official_suffixed_code_not_guessing():
    def handler(request):
        return httpx.Response(200, json={"code": 200, "data": {
            "fields": ["code", "symbol", "name", "market", "exchange", "list_status"],
            "items": [["000001.SZ", "000001", "平安银行", "主板", None, "L"],
                      ["600000.SH", "600000", "浦发银行", "主板", None, "L"],
                      ["200011.SZ", "200011", "深物业B", "主板", None, "L"],
                      ["900901.SH", "900901", "上电B股", "主板", None, "L"]]}})
    assert subscription_symbols(provider(handler), deadline=CUTOFF) == ["sh600000", "sz000001"]


@pytest.mark.parametrize("code,exchange", [(None, None), ("000001.SH", "SZSE"), ("600000.SH", None)])
def test_missing_or_conflicting_exchange_proof_blocks_subscription(code, exchange):
    def handler(request):
        return httpx.Response(200, json={"code": 200, "data": {
            "fields": ["code", "symbol", "name", "market", "exchange", "list_status"],
            "items": [[code, "000001", "样例", "主板", exchange, "L"]]}})
    with pytest.raises(MeozError):
        subscription_symbols(provider(handler), deadline=CUTOFF)
