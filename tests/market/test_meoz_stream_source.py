"""Offline stream ingestion, causal deduplication and resumable full-Tick backfill."""

import asyncio
import json
from copy import deepcopy
from datetime import datetime, timedelta
from threading import get_ident
from types import SimpleNamespace

import pytest

from stock_god.market.common import CN
from stock_god.market.meoz import TICK_FIELDS, MeozError
from stock_god.market.meoz_source import MeozAuctionSource
from stock_god.storage.current import BASE43_DDL
from stock_god.storage.db import Database

DAY = "20261008"
START = datetime(2026, 10, 8, 9, 15, tzinfo=CN)
CUTOFF = START.replace(minute=29, second=59)


class FakeMarket:
    """No network/client or production database; each settings lease is independent."""

    def __init__(self, state, settings=None):
        self.state = state
        self.meoz = SimpleNamespace(settings=deepcopy(settings or {"meozApiKey": "offline-fixture-key"}),
                                    configured=True)
        self.meoz.status = lambda: {"configured": True, "ready": False, "status": "incomplete",
                                   "message": "offline fixture"}

    def with_settings(self, settings):
        clone = FakeMarket(self.state, settings)
        self.state.leases.append(clone)
        return clone

    def close(self):
        self.state.closed += 1

    def meoz_subscription_symbols(self, *, deadline=None):
        assert deadline == CUTOFF
        self.state.selected = list(self.state.universe)
        return list(self.state.universe)

    async def meoz_stream_records(self, symbols, *, deadline, on_status=None):
        assert symbols == sorted(self.state.universe) and deadline == CUTOFF
        try:
            if on_status:
                await on_status({"status": "connected", "selectedCount": len(symbols)})
                await on_status({"status": "subscribed", "selectedCount": len(symbols),
                                 "confirmedCount": self.state.confirmed, "slotLimit": self.state.slots,
                                 "counts": {"subscriptions": self.state.confirmed}})
            for action in self.state.actions:
                if callable(action):
                    result = action(self, on_status)
                    if asyncio.iscoroutine(result):
                        await result
                else:
                    yield action
            if self.state.block:
                self.state.waiting.set()
                await asyncio.Event().wait()
        finally:
            self.state.generator_closed += 1

    def meoz_tick_history(self, day, symbols, **kwargs):
        self.state.history_calls.append((day, list(symbols), deepcopy(kwargs)))
        assert len(symbols) <= 200
        return self.state.history(day, symbols, **kwargs)

    def meoz_ticks(self, day, symbols, **kwargs):
        self.state.rest_calls.append((list(symbols), kwargs.get("final", False)))
        return self.state.ticks(day, symbols, **kwargs)

    def meoz_request(self, api, params, fields=None, **kwargs):
        self.state.request_calls.append((api, deepcopy(params)))
        handler = getattr(self.state, "request", None)
        if handler is not None:
            return handler(api, params, fields, **kwargs)
        assert api == "limit_pool_yes"
        return {"fields": fields.split(","), "rows": [{"symbol": symbol, "tradedate": DAY,
                "pre_type": "u", "pre_limit_times": 1} for symbol in self.state.mother],
                "source": "offline-mother", "receivedAt": self.state.now.isoformat()}


@pytest.fixture
def source(tmp_path):
    database = Database(tmp_path / "stream-source.db")
    with database.transaction() as db:
        for statement in BASE43_DDL:
            db.execute(statement)
    state = SimpleNamespace(now=START + timedelta(seconds=2), universe=["sz000001", "sz000002", "sh600000", "sh600001"],
                            confirmed=4, slots=4000, actions=[], leases=[], selected=[], closed=0,
                            generator_closed=0, block=False, waiting=None, history_calls=[], rest_calls=[],
                            request_calls=[], mother=())
    market = FakeMarket(state)
    result = MeozAuctionSource(database, market, lambda: state.now)
    result.state = state
    yield result
    database.close()


def record(code="sz000001", second=0, *, received=None, transport="websocket"):
    at = START + timedelta(seconds=second)
    raw = {"tradedate": DAY, "symbol": code[2:], "time": at.isoformat(), "close": 10,
           "vol": 2, "amount": 2000, "transaction_num": 1, "bid1": 10, "bid2": 10,
           "ask1": 10, "ask2": 10, "bid_vol1": 2, "bid_vol2": 0, "ask_vol1": 2, "ask_vol2": 0}
    result = {"code": code, "raw": raw, "asOf": at.isoformat(),
              "availableAt": (received or at + timedelta(seconds=1)).isoformat(),
              "source": "offline-node", "transport": transport, "volumeUnit": "lot", "bookVolumeUnit": "lot"}
    if transport == "websocket":
        result["unitEvidence"] = {"via": "ack", "volumeUnit": "lot", "bookVolumeUnit": "lot"}
    return result


def prepare(source, mother=("000001",), candidates=("sz000001",), complete=True):
    source._write(DAY, "source_candidates", {"complete": complete, "motherSymbols": list(mother),
                  "candidates": [{"code": c, "reference": 900} for c in candidates], "documents": []})


def final_document(received=None):
    return {"receivedAt": (received or START.replace(minute=26)).isoformat(), "fields": [],
            "rows": [{"symbol": "000001", "tradedate": DAY, "m_price": 10, "auc_vol": 2, "auc_amt": 2000}]}


def sequence(step=6, transport="websocket"):
    return [record(second=second, transport=transport) for second in [*range(0, 600, step), 600]]


def history_result(source, rows, fields=None):
    return {"fields": TICK_FIELDS.split(",") if fields is None else fields,
            "rows": deepcopy(rows), "source": "offline-full-tick", "receivedAt": source.clock().isoformat()}


async def test_stream_subscribes_full_universe_but_persists_only_model_scope(source):
    prepare(source, mother=("000001",), candidates=("sz000002",))
    source.state.actions = [[record(code) for code in source.state.universe]]
    before = deepcopy(source._read(DAY, "source_candidates"))
    summary = await source.capture_stream(DAY, holding_symbols=("sh600000",))
    saved = source._read(DAY, "source_snapshot")
    assert set(source.state.selected) == set(source.state.universe)
    assert {row["code"] for row in saved["records"]} == {"sz000001", "sz000002", "sh600000"}
    assert source._read(DAY, "source_candidates") == before
    assert summary["selectedCount"] == summary["confirmedCount"] == 4
    assert summary["persistedScopeCount"] == 3 and summary["counts"]["outsideScopeRecords"] == 1
    assert source.state.closed == source.state.generator_closed == 1
    assert all(row["volumeUnit"] == row["bookVolumeUnit"] == "lot" for row in saved["records"])


async def test_stream_settings_are_frozen_even_if_root_settings_mutate(source):
    prepare(source)

    def mutate_root(market, callback):
        source.market.meoz.settings["meozApiKey"] = "new-root-key"
        assert market.meoz.settings["meozApiKey"] == "offline-fixture-key"

    source.state.actions = [mutate_root, [record()]]
    await source.capture_stream(DAY)
    frozen = source.with_settings({"meozApiKey": "offline-fixture-key"})
    assert len(frozen._read(DAY, "source_snapshot")["records"]) == 1
    assert source._read(DAY, "source_snapshot") is None


async def test_cancellation_flushes_batch_closes_generator_and_releases_lease(source):
    prepare(source)
    source.state.actions = [[record()]]
    source.state.block, source.state.waiting = True, asyncio.Event()
    task = asyncio.create_task(source.capture_stream(DAY))
    await asyncio.wait_for(source.state.waiting.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert source.state.closed == source.state.generator_closed == 1
    assert source._read(DAY, "source_stream")["state"] == "cancelled"
    assert len(source._read(DAY, "source_snapshot")["records"]) == 1


async def test_quiet_stream_callback_surfaces_full_subscription_health_without_readiness(source):
    prepare(source)
    source.state.confirmed, source.state.slots = 2, 2
    source.state.block, source.state.waiting = True, asyncio.Event()
    task = asyncio.create_task(source.capture_stream(DAY))
    await asyncio.wait_for(source.state.waiting.wait(), timeout=2)
    status = source.status()
    assert "WS subscribed" in status["message"] and "2/4" in status["message"]
    assert "槽位2" in status["message"] and not status["ready"]
    assert source._read(DAY, "source_snapshot") is None
    assert source._read(DAY, "source_stream")["transportCounts"]["subscriptions"] == 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_stream_scope_refreshes_as_mother_becomes_available(source):
    source._write(DAY, "source_candidates", {"complete": False, "candidates": [], "documents": []})

    async def discover(market, callback):
        prepare(source)
        await callback({"status": "retrying", "selectedCount": 4})

    source.state.actions = [[record()], discover, [record(second=1)]]
    summary = await source.capture_stream(DAY)
    assert summary["counts"]["outsideScopeRecords"] == 1
    saved = source._read(DAY, "source_snapshot")
    assert len(saved["records"]) == 1 and saved["records"][0]["asOf"] == record(second=1)["asOf"]


async def test_stream_cutoff_keeps_real_receive_times_and_rejects_late_frame(source):
    prepare(source)
    source.state.now = CUTOFF - timedelta(seconds=1)
    earlier = record(second=600, received=source.clock())

    async def cross_cutoff(market, callback):
        await callback({"status": "receiving", "stockCount": 4})  # persist the last valid batch before fencing
        source.state.now = CUTOFF + timedelta(seconds=1)

    late = record(second=601, received=CUTOFF + timedelta(seconds=1))
    source.state.actions = [[earlier], cross_cutoff, [late]]
    summary = await source.capture_stream(DAY)
    saved = source._read(DAY, "source_snapshot")
    assert summary["state"] == "failed" and not summary["complete"]
    assert "截止" in summary["error"]
    assert summary["counts"]["rejectedRecords"] == 1
    assert saved["records"] == [earlier]
    assert source.state.generator_closed == source.state.closed == 1


@pytest.mark.parametrize("bad", ["wrong-day", "future-quote", "future-receive", "pre-window", "future-send"])
async def test_stream_rejects_noncausal_timestamps(source, bad):
    prepare(source)
    invalid = record()
    if bad == "wrong-day":
        invalid["asOf"] = (START - timedelta(days=1)).isoformat()
    elif bad == "future-quote":
        invalid["asOf"] = (source.clock() + timedelta(seconds=2)).isoformat()
    elif bad == "future-receive":
        invalid["availableAt"] = (source.clock() + timedelta(seconds=2)).isoformat()
    elif bad == "pre-window":
        invalid["asOf"] = (START - timedelta(seconds=1)).isoformat()
    else:
        invalid["sendAt"] = (source.clock() + timedelta(seconds=2)).isoformat()
    source.state.actions = [[invalid, record()]]
    summary = await source.capture_stream(DAY)
    assert summary["counts"]["rejectedRecords"] == 1
    assert len(source._read(DAY, "source_snapshot")["records"]) == 1


@pytest.mark.parametrize("unit,evidence", [("share", True), (None, True), ("lot", False)])
async def test_stream_never_assumes_ambiguous_units(source, unit, evidence):
    prepare(source)
    row = record()
    row["bookVolumeUnit"] = unit
    if not evidence:
        row.pop("unitEvidence")
    source.state.actions = [[row]]
    summary = await source.capture_stream(DAY)
    assert summary["counts"]["unsupportedUnitRecords"] == 1
    assert source._read(DAY, "source_snapshot") is None
    assert source._read("source", "source_certification") is None


async def test_unit_evidence_in_meta_is_preserved(source):
    prepare(source)
    row = record()
    row["meta"] = {"unitEvidence": row.pop("unitEvidence")}
    source.state.actions = [[row]]
    await source.capture_stream(DAY)
    assert source._read(DAY, "source_snapshot")["records"][0]["meta"] == row["meta"]


async def test_stream_batches_before_persistence_and_bounds_memory_duplicates(source, monkeypatch):
    import stock_god.market.meoz_source as source_module

    prepare(source)
    monkeypatch.setattr(source_module, "_STREAM_BATCH", 4)
    monkeypatch.setattr(source_module, "_STREAM_SEEN", 2)
    source.state.now = START + timedelta(seconds=10)
    rows = [record(second=second) for second in range(8)]
    source.state.actions = [[*rows, *rows, *([rows[0]] * 100)]]
    original, batches = MeozAuctionSource._snapshot_merge, []

    def merge(self, day, *, records=(), documents=(), **updates):
        if records:
            batches.append(len(records))
        return original(self, day, records=records, documents=documents, **updates)

    monkeypatch.setattr(MeozAuctionSource, "_snapshot_merge", merge)
    summary = await source.capture_stream(DAY)
    assert batches and max(batches) <= 4 and len(batches) < len(rows)
    assert len(source._read(DAY, "source_snapshot")["records"]) == 8
    assert summary["counts"]["duplicateRecords"] >= 99


def test_storage_dedup_is_bounded_but_keeps_real_conflicts_and_boundary_proof(source):
    first = record(transport="rest")
    boundary = {**first, "boundaryFinal": True}
    extra = deepcopy(first)
    extra["raw"]["unsupported_column"] = "different transport diagnostic"
    source._snapshot_merge(DAY, records=[first, extra, boundary] * 100)
    assert len(source._read(DAY, "source_snapshot")["records"]) == 2
    for price in range(11, 111):
        changed = deepcopy(first)
        changed["raw"]["close"] = price
        source._snapshot_merge(DAY, records=[changed])
    saved = source._read(DAY, "source_snapshot")
    assert len(saved["records"]) == 3 and len(saved["conflicts"]) == 1
    assert "close" in saved["conflicts"][0]["fields"]
    assert source._observations("sz000001", DAY, saved, CUTOFF)[1]


def test_snapshot_resource_limit_fails_closed_without_deleting_existing_facts(source, monkeypatch):
    import stock_god.market.meoz_source as source_module

    prepare(source)
    source.state.now = START.replace(minute=26)
    monkeypatch.setattr(source_module, "_SNAPSHOT_RECORDS", 2)
    source._snapshot_merge(DAY, records=sequence(), documents=[final_document()])
    saved = source._read(DAY, "source_snapshot")
    assert len(saved["records"]) == 2 and "sz000001" in saved["overflowSymbols"]
    result = source.freeze(DAY, CUTOFF)
    assert not result["complete"] and result["gaps"][0]["overflow"]


def test_record_units_are_used_by_measure_and_final_checks_not_inspection_override(source):
    prepare(source)
    source.state.now = START.replace(minute=26)
    rows = sequence()
    for row in rows:
        row.update(volumeUnit="share", bookVolumeUnit="share")
        for field in ("vol", "bid_vol1", "bid_vol2", "ask_vol1", "ask_vol2"):
            row["raw"][field] *= 100
    rows[-1]["boundaryFinal"] = True
    source._snapshot_merge(DAY, records=rows, documents=[final_document()])
    saved = source._read(DAY, "source_snapshot")
    result = source.freeze(DAY, CUTOFF)
    assert result["complete"]
    assert result["candidates"][0]["auctionRows"][0]["fields"][2] == 200
    assert result["candidates"][0]["auctionRows"][0]["fields"][6] == 200
    assert source._fetched_final("sz000001", DAY, saved, CUTOFF)
    assert source._collected_final("sz000001", DAY, saved, CUTOFF)
    rest = record(second=600, transport="rest")
    source._snapshot_merge(DAY, records=[rest])
    assert source.freeze(DAY, CUTOFF)["complete"]  # same shares, not a data conflict


def test_unsupported_fields_and_unknown_units_are_not_data_conflicts(source):
    prepare(source)
    source.state.now = START.replace(minute=26)
    rows = sequence()
    for row in rows:
        row["bookVolumeUnit"] = "unknown"
        row["raw"]["unused_extension"] = "ignored"
    source._snapshot_merge(DAY, records=rows, documents=[final_document()])
    result = source.freeze(DAY, CUTOFF)
    assert not result["complete"]
    assert result["gaps"][0]["kind"] == "unsupportedFields"
    assert "bid_vol1" in result["gaps"][0]["unsupportedFields"]
    assert not result["gaps"][0]["conflict"]
    assert source._read(DAY, "source_snapshot")["conflicts"] == []


@pytest.mark.parametrize("step,complete", [(6, True), (9, False)])
def test_full_tick_backfill_preserves_six_second_gate_without_interpolation(source, step, complete):
    prepare(source)
    source.state.now = START.replace(minute=26)
    raw_rows = [row["raw"] for row in sequence(step, transport="rest")]
    source.state.history = lambda *args, **kwargs: history_result(source, raw_rows)
    source._snapshot_merge(DAY, documents=[final_document()])
    source._backfill(DAY, ["sz000001"], CUTOFF, lambda: 3)
    task = source._read(DAY, "source_tick_backfill")
    saved = source._read(DAY, "source_snapshot")
    assert task["complete"] and task["pages"] == 1
    assert len(saved["records"]) == len(raw_rows)
    assert [row["asOf"] for row in saved["records"]] == [row["time"] for row in raw_rows]
    assert all(row["availableAt"] == source.clock().isoformat() for row in saved["records"])
    assert source.freeze(DAY, CUTOFF)["complete"] is complete
    if not complete:
        assert any(gap["seconds"] == 9 for gap in source.freeze(DAY, CUTOFF)["gaps"][0]["intervals"])
    assert not source.state.request_calls  # no daily_auc_detail reconstruction


def test_tick_history_backfills_only_missing_scope_and_merges_ws_duplicates(source):
    prepare(source, mother=("000001", "000002"), candidates=("sz000001", "sz000002"))
    source.state.now = START.replace(minute=26)
    complete_rows = sequence()
    sparse = [record("sz000002")]
    source._snapshot_merge(DAY, records=complete_rows + sparse, documents=[final_document()])

    def history(day, symbols, **kwargs):
        assert symbols == ["sz000002"]
        rows = [record("sz000002", second=second, transport="rest")["raw"] for second in (0, 6, 12, 600)]
        return history_result(source, rows)

    source.state.history = history
    source._backfill(DAY, ["sz000001", "sz000002"], CUTOFF, lambda: 3)
    saved = source._read(DAY, "source_snapshot")
    actual = [row for row in saved["records"] if row["code"] == "sz000002"]
    assert len(actual) == 4 and actual[0]["availableAt"] == sparse[0]["availableAt"]
    assert not saved["conflicts"]


def test_tick_history_pages_resume_after_failure_without_double_storage(source):
    prepare(source)
    source.state.now = START.replace(minute=26)
    calls, fail = [], [True]

    def history(day, symbols, *, offset, **kwargs):
        calls.append(offset)
        if offset == 6000 and fail[0]:
            fail[0] = False
            raise MeozError("incomplete", "offline page timeout")
        raw = record(transport="rest")["raw"]
        return history_result(source, [raw] * (6000 if offset == 0 else 1))

    source.state.history = history
    source._backfill(DAY, ["sz000001"], CUTOFF, lambda: 3)
    saved = source._read(DAY, "source_tick_backfill")
    assert not saved["complete"] and saved["offset"] == 6000 and saved["pages"] == 1
    source._backfill(DAY, ["sz000001"], CUTOFF, lambda: 3)
    assert source._read(DAY, "source_tick_backfill")["offset"] == 6000
    restarted = MeozAuctionSource(source.database, source.market.with_settings(source.market.meoz.settings), source.clock)
    restarted._backfill(DAY, ["sz000001"], CUTOFF, lambda: 3)
    assert source._read(DAY, "source_tick_backfill")["complete"]
    assert calls == [0, 6000, 6000]
    assert len(source._read(DAY, "source_snapshot")["records"]) == 1


def test_tick_history_batches_never_exceed_200_symbols(source):
    source.state.now = START.replace(minute=26)
    symbols = [f"sz{index:06d}" for index in range(1, 202)]
    source.state.history = lambda *args, **kwargs: history_result(source, [])
    source._backfill(DAY, symbols, CUTOFF, lambda: 3)
    source._backfill(DAY, symbols, CUTOFF, lambda: 3)
    assert [len(batch) for _, batch, _ in source.state.history_calls] == [200, 1]
    assert source._read(DAY, "source_tick_backfill")["complete"]
    assert all(args["start_time"] == "09:15:00" and args["end_time"] == "09:26:00"
               and args["deadline"] == CUTOFF and args["budget_seconds"] <= 3
               for _, _, args in source.state.history_calls)


@pytest.mark.parametrize("bad", ["detail-only", "missing-receive", "wrong-day", "late-receive", "future-time"])
def test_tick_history_rejects_unsupported_or_noncausal_pages_atomically(source, bad):
    source.state.now = START.replace(minute=26)
    result = history_result(source, [record(transport="rest")["raw"]])
    if bad == "detail-only":
        result["fields"] = ["symbol", "tradedate", "time", "m_price", "auc_vol"]
    elif bad == "missing-receive":
        result.pop("receivedAt")
    elif bad == "wrong-day":
        result["rows"][0]["tradedate"] = "20261007"
    elif bad == "late-receive":
        result["receivedAt"] = (CUTOFF + timedelta(seconds=1)).isoformat()
    else:
        result["rows"][0]["time"] = START.replace(minute=27).isoformat()
    source.state.history = lambda *args, **kwargs: deepcopy(result)
    source._backfill(DAY, ["sz000001"], CUTOFF, lambda: 3)
    assert source._read(DAY, "source_snapshot") is None
    task = source._read(DAY, "source_tick_backfill")
    assert task["offset"] == task["pages"] == 0 and not task["complete"] and task["error"]
    if bad == "detail-only":
        assert task["sourceStatus"] == "unsupported_fields"


def test_tick_history_budget_page_limit_and_cutoff_bound_attempts(source, monkeypatch):
    import stock_god.market.meoz_source as source_module

    source.state.now = START.replace(minute=26)
    source.state.history = lambda *args, **kwargs: history_result(source, [record(transport="rest")["raw"]] * 6000)
    source._backfill(DAY, ["sz000001"], CUTOFF, lambda: (_ for _ in ()).throw(MeozError("incomplete", "budget exhausted")))
    assert not source.state.history_calls
    monkeypatch.setattr(source_module, "_BACKFILL_PAGES", 1)
    source._backfill(DAY, ["sz000001"], CUTOFF, lambda: 3)
    source._backfill(DAY, ["sz000001"], CUTOFF, lambda: 3)
    assert len(source.state.history_calls) == 1
    task = source._read(DAY, "source_tick_backfill")
    assert not task["complete"] and "分页上限" in task["error"]
    source.state.now = CUTOFF + timedelta(seconds=1)
    source._backfill(DAY, ["sz000001"], CUTOFF, lambda: 3)
    assert len(source.state.history_calls) == 1


def test_rest_poll_still_crosschecks_final_auction_even_with_full_ws_coverage(source):
    prepare(source)
    source.state.now = START.replace(minute=26)
    source._snapshot_merge(DAY, records=sequence())

    def ticks(day, symbols, *, final=False, **kwargs):
        return [{**record(second=600, received=source.clock(), transport="rest")}]

    source.state.ticks = ticks
    source.state.request = lambda *args, **kwargs: final_document(source.clock())
    source.state.history = lambda *args, **kwargs: history_result(source, [])
    assert source.poll(DAY)
    assert source.state.rest_calls == [(["sz000001"], False), (["sz000001"], True)]
    assert source.state.request_calls[0][0] == "daily_auc_detail"
    assert source._read(DAY, "source_snapshot")["finalComplete"]
    assert source.freeze(DAY, CUTOFF)["complete"]
    assert not source.state.history_calls  # WS already supplies continuous book coverage


async def test_replaced_key_revokes_stream_writes_but_closes_lease(source):
    prepare(source)
    source._snapshot_merge(DAY, records=[record(transport="rest")])

    def replace_key(market, callback):
        with source.database.transaction() as db:
            db.execute("CREATE TABLE research_settings(center TEXT PRIMARY KEY,config_json TEXT)")
            db.execute("INSERT INTO research_settings VALUES('research2',?)",
                       (json.dumps({"meozApiKey": "replacement-key"}),))

    source.state.actions = [replace_key, [record(second=1)]]
    summary = await source.capture_stream(DAY)
    assert summary["state"] == "failed"
    assert "旧采集写入已撤销" in summary["error"]
    assert source.state.closed == source.state.generator_closed == 1
    assert len(source._read(DAY, "source_snapshot")["records"]) == 1


async def test_missing_mother_is_loaded_before_first_stream_frame(source):
    source.state.mother = ("000001",)
    source.state.actions = [[record()]]
    summary = await source.capture_stream(DAY)
    assert source.state.request_calls[0][0] == "limit_pool_yes"
    assert source._read(DAY, "source_candidates")["motherSymbols"] == ["000001"]
    assert summary["counts"]["acceptedRecords"] == 1
    assert len(source._read(DAY, "source_snapshot")["records"]) == 1


async def test_capture_and_status_callback_database_work_stays_off_event_loop(source, monkeypatch):
    prepare(source)
    main_thread, reads, writes = get_ident(), [], []
    original_read, original_change = MeozAuctionSource._read, MeozAuctionSource._change

    def read(self, *args, **kwargs):
        if self is not source:
            reads.append(get_ident())
            assert get_ident() != main_thread
        return original_read(self, *args, **kwargs)

    def change(self, *args, **kwargs):
        writes.append(get_ident())
        assert get_ident() != main_thread
        return original_change(self, *args, **kwargs)

    monkeypatch.setattr(MeozAuctionSource, "_read", read)
    monkeypatch.setattr(MeozAuctionSource, "_change", change)
    source.state.actions = [[record()]]
    await source.capture_stream(DAY)
    assert reads and writes


async def test_receiving_health_keeps_full_subscription_not_candidate_subset(source):
    prepare(source)
    health = {code: {"count": 3, "firstAsOf": START.isoformat(), "lastAsOf": START.isoformat(),
                     "maxGapSeconds": 3} for code in source.state.universe}

    async def receiving(market, callback):
        await callback({"status": "receiving", "receivedCount": 12, "stockCount": 4,
                        "stockHealth": health, "bookDivisor": 100, "volumeDivisor": None, "attempts": 1})

    source.state.actions = [receiving, []]
    summary = await source.capture_stream(DAY)
    assert set(summary["fullstockHealth"]) == set(source.state.universe)
    assert summary["stockCount"] == 4 and summary["persistedScopeCount"] == 1
    assert summary["bookDivisor"] == 100 and summary["volumeDivisor"] is None
    assert summary["transportCounts"]["receivedCount"] == 12
    assert "收到行情4只" in source.status()["message"]
    assert source._read(DAY, "source_snapshot") is None


def test_same_millisecond_rest_iso_source_and_unsupported_count_do_not_create_conflicts(source):
    at = START + timedelta(milliseconds=123)
    ws = record()
    ws.update(asOf=at.isoformat(), availableAt=(at + timedelta(seconds=1)).isoformat())
    ws["raw"].update(time=at.isoformat(), transaction_num=None)
    rest = deepcopy(ws)
    rest.update(asOf="2026-10-08T01:15:00.123Z", availableAt=source.clock().isoformat(),
                transport="rest", source="other-offline-node")
    rest["raw"].update(time="2026-10-08 09:15:00.123+08:00", transaction_num=3)
    source._snapshot_merge(DAY, records=[ws, rest] * 50)
    saved = source._read(DAY, "source_snapshot")
    observations, conflict = source._observations("sz000001", DAY, saved, CUTOFF)
    assert not conflict and not saved["conflicts"]
    assert len(saved["records"]) <= 2 and len(observations) == 1
    assert observations[0]["asOf"] == at.isoformat()
    assert observations[0]["availableAt"] == source.clock().isoformat()
    assert observations[0]["raw"]["transaction_num"] == 3


def test_unknown_ws_volume_is_enriched_by_real_rest_receipt_without_false_conflict(source):
    prepare(source)
    source.state.now = START.replace(minute=26)
    early = record()
    early["raw"].update(vol=None, transaction_num=None)
    later = record(transport="rest", received=source.clock())
    source._snapshot_merge(DAY, records=[early, later])
    saved = source._read(DAY, "source_snapshot")
    assert not saved["conflicts"]
    early_rows, early_conflict = source._observations("sz000001", DAY, saved, START + timedelta(seconds=2))
    later_rows, later_conflict = source._observations("sz000001", DAY, saved, CUTOFF)
    assert not early_conflict and not later_conflict
    assert early_rows[0]["raw"]["vol"] is None
    assert later_rows[0]["raw"]["vol"] == 2
    assert later_rows[0]["availableAt"] == source.clock().isoformat()


def test_millisecond_gaps_above_six_seconds_do_not_round_down_to_readiness(source):
    prepare(source)
    source.state.now = START.replace(minute=26)
    rows = sequence()
    at = START + timedelta(seconds=6, milliseconds=1)
    rows[1].update(asOf=at.isoformat(), availableAt=(at + timedelta(seconds=1)).isoformat())
    rows[1]["raw"]["time"] = at.isoformat()
    source._snapshot_merge(DAY, records=rows, documents=[final_document()])
    result = source.freeze(DAY, CUTOFF)
    assert not result["complete"]
    assert any(interval["seconds"] > 6 for interval in result["gaps"][0]["intervals"])


async def test_transport_complete_before_deadline_does_not_certify_stream_or_inputs(source):
    prepare(source)

    async def premature(market, callback):
        await callback({"status": "complete", "stockCount": 4, "receivedCount": 4})

    source.state.actions = [premature]
    summary = await source.capture_stream(DAY)
    assert summary["transportState"] == "complete" and summary["state"] == "disconnected"
    assert not summary["complete"] and not source.status()["ready"]


def test_snapshot_limit_does_not_prune_preexisting_distinct_facts(source, monkeypatch):
    import stock_god.market.meoz_source as source_module

    source.state.now = START + timedelta(seconds=10)
    old = [record(second=second) for second in range(3)]
    source._write(DAY, "source_snapshot", {"records": old, "documents": []})
    monkeypatch.setattr(source_module, "_SNAPSHOT_RECORDS", 2)
    source._snapshot_merge(DAY, records=[record(second=3)])
    saved = source._read(DAY, "source_snapshot")
    assert saved["records"] == old
    assert "sz000001" in saved["overflowSymbols"]


def test_backfill_stops_when_stream_has_filled_pending_coverage(source):
    prepare(source)
    source.state.now = START.replace(minute=26)
    source.state.history = lambda *args, **kwargs: history_result(source, [record(transport="rest")["raw"]] * 6000)
    source._backfill(DAY, ["sz000001"], CUTOFF, lambda: 3)
    assert len(source.state.history_calls) == 1
    source._snapshot_merge(DAY, records=sequence(), documents=[final_document()])
    source._backfill(DAY, ["sz000001"], CUTOFF, lambda: 3)
    assert len(source.state.history_calls) == 1
    assert source._read(DAY, "source_tick_backfill")["complete"]
    assert source.freeze(DAY, CUTOFF)["complete"]


async def test_revoke_fences_same_key_cancel_flush_without_revoking_ordinary_poll(source):
    prepare(source)
    source.state.actions = [[record()]]
    source.state.block, source.state.waiting = True, asyncio.Event()
    task = asyncio.create_task(source.capture_stream(DAY))
    await asyncio.wait_for(source.state.waiting.wait(), timeout=2)
    before = deepcopy(source._read(DAY, "source_stream"))
    source.revoke()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert source._read(DAY, "source_snapshot") is None
    terminal = source._read(DAY, "source_stream")
    assert terminal["state"] in {"cancelled", "closed"} and terminal["endedAt"] == source.clock().isoformat()
    for key, value in before.items():
        if key != "state":
            assert terminal[key] == value
    assert source.state.closed == source.state.generator_closed == 1
    source._snapshot_merge(DAY, records=[record(transport="rest")])
    assert len(source._read(DAY, "source_snapshot")["records"]) == 1


async def test_current_auto_disable_same_key_rejects_cancel_cleanup_without_explicit_revoke(source):
    prepare(source)
    source.state.actions = [[record()]]
    source.state.block, source.state.waiting = True, asyncio.Event()
    task = asyncio.create_task(source.capture_stream(DAY))
    await asyncio.wait_for(source.state.waiting.wait(), timeout=2)
    with source.database.transaction() as db:
        db.execute("CREATE TABLE research_settings(center TEXT PRIMARY KEY,config_json TEXT)")
        db.execute("INSERT INTO research_settings VALUES('research2',?)", (json.dumps({
            "meozApiKey": "offline-fixture-key", "predictionAutoEnabled": False}),))
    before = deepcopy(source._read(DAY, "source_stream"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert source._read(DAY, "source_snapshot") is None
    terminal = source._read(DAY, "source_stream")
    assert terminal["state"] in {"cancelled", "closed"} and terminal["endedAt"] == source.clock().isoformat()
    for key, value in before.items():
        if key != "state":
            assert terminal[key] == value
    source._write(DAY, "source_rules:sz000001", {"eligible": False})
    assert source._read(DAY, "source_rules:sz000001") is not None


async def test_deadline_revoke_cannot_flush_buffer_after_cutoff(source):
    prepare(source)
    source.state.actions = [[record()]]
    source.state.block, source.state.waiting = True, asyncio.Event()
    task = asyncio.create_task(source.capture_stream(DAY))
    await asyncio.wait_for(source.state.waiting.wait(), timeout=2)
    before = deepcopy(source._read(DAY, "source_stream"))
    source.state.now = CUTOFF
    source.revoke()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert source._read(DAY, "source_snapshot") is None
    terminal = source._read(DAY, "source_stream")
    assert terminal["state"] in {"cancelled", "closed"} and terminal["endedAt"] == source.clock().isoformat()
    for key, value in before.items():
        if key != "state":
            assert terminal[key] == value
    assert source.state.closed == source.state.generator_closed == 1


async def test_revoke_during_change_rolls_back_inflight_stream_transaction(source, monkeypatch):
    prepare(source)
    original = MeozAuctionSource._snapshot_merge

    def interrupted(self, day, *, records=(), documents=(), **updates):
        def change(old):
            source.revoke()
            return {"records": list(records), "documents": []}
        return self._change(day, "source_snapshot", change)

    monkeypatch.setattr(MeozAuctionSource, "_snapshot_merge", interrupted)
    source.state.actions = [[record()]]
    summary = await source.capture_stream(DAY)
    assert summary["state"] == "failed" and "撤销" in summary["error"]
    assert source._read(DAY, "source_snapshot") is None
    assert source.state.closed == source.state.generator_closed == 1
    monkeypatch.setattr(MeozAuctionSource, "_snapshot_merge", original)
    source._snapshot_merge(DAY, records=[record(transport="rest")])
    assert len(source._read(DAY, "source_snapshot")["records"]) == 1


async def test_revoke_during_nested_clone_creation_fences_that_stream_not_root(source, monkeypatch):
    prepare(source)
    original = source.market.with_settings

    def interrupted(settings):
        clone = original(settings)
        source.revoke()
        return clone

    monkeypatch.setattr(source.market, "with_settings", interrupted)
    summary = await source.capture_stream(DAY)
    assert summary["state"] == "failed" and "撤销" in summary["error"]
    assert source.state.selected == [] and source.state.closed == 1
    assert source._read(DAY, "source_stream") is None
    source._snapshot_merge(DAY, records=[record(transport="rest")])
    assert len(source._read(DAY, "source_snapshot")["records"]) == 1

async def test_cutoff_records_closed_health_without_flushing_late_quotes(source):
    prepare(source)
    source.state.block, source.state.waiting = True, asyncio.Event()
    task = asyncio.create_task(source.capture_stream(DAY))
    await asyncio.wait_for(source.state.waiting.wait(), timeout=2)
    before = source._read(DAY, "source_stream")
    source.state.now = CUTOFF
    source.revoke()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    after = source._read(DAY, "source_stream")
    assert after["captureId"] == before["captureId"]
    assert after["state"] == "closed" and after["endedAt"] == CUTOFF.isoformat()
    assert not after["complete"] and source._read(DAY, "source_snapshot") is None


def test_terminal_health_does_not_overwrite_a_new_stream_capture(source):
    source._write(DAY, "source_stream", {"captureId": "new-capture", "state": "subscribed", "confirmedCount": 4})
    source._stream_terminal(DAY, "old-capture", {"state": "failed", "error": "old error"})
    saved = source._read(DAY, "source_stream")
    assert saved["state"] == "subscribed" and saved["confirmedCount"] == 4
    assert "error" not in saved and "endedAt" not in saved
