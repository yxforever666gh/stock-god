import csv
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "A股历史分钟线数据包" / "download_diemeng.py"
SPEC = importlib.util.spec_from_file_location("download_diemeng", SCRIPT)
DOWNLOADER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(DOWNLOADER)


def source_rows():
    return [
        ["09:30", 1, 1, 1, 1, 1, 10],
        ["09:31", 2, 3, 1, 2, 2, 20],
        ["09:35", 3, 4, 2, 3, 3, 30],
        ["13:01", 4, 5, 3, 4, 4, 40],
        ["13:05", 5, 6, 4, 5, 5, 50],
    ]


def stock_facts(codes):
    daily = {code: {"pre_close": 0.9} for code in codes}
    finance = {code: {"float_share": 1_000_000, "total_share": 2_000_000} for code in codes}
    return daily, finance


def test_only_minute_output_preserves_fields_and_timestamps(tmp_path):
    daily, finance = stock_facts(["600941.SH"])
    result = DOWNLOADER.materialize_stocks("2026-09-30", {"600941.SH": source_rows()}, daily, finance, tmp_path)
    assert result["rows"] == 5
    year = tmp_path / "A股个股/2026"
    assert [path.name for path in year.iterdir()] == ["1分钟"]
    with (year / "1分钟/sh600941.csv").open(encoding="utf-8") as source:
        records = list(csv.reader(source))
    assert records[0] == DOWNLOADER.STOCK_HEADER and all(len(row) == 12 for row in records)
    assert [row[0] for row in records[1:]] == [f"2026-09-30 {bar[0]}:00" for bar in source_rows()]
    assert records[1][1:8] == ["1", "1", "1", "1", "100", "10", "0.1"]


def test_materialization_checkpoint_resumes_and_append_deduplicates(tmp_path):
    rows = source_rows()
    daily, finance = stock_facts(["600941.SH", "000001.SZ"])
    checkpoint = {"days": {}}
    first = DOWNLOADER.materialize_stocks(
        "2026-09-30",
        {"600941.SH": rows},
        daily,
        finance,
        tmp_path,
        checkpoint=checkpoint,
        checkpoint_cache=tmp_path,
    )
    second = DOWNLOADER.materialize_stocks(
        "2026-09-30",
        {"600941.SH": rows, "000001.SZ": rows},
        daily,
        finance,
        tmp_path,
        checkpoint=checkpoint,
        checkpoint_cache=tmp_path,
    )
    assert first["rows"] == 5 and second["rows"] == 5
    assert checkpoint["days"]["2026-09-30"]["completed_count"] == 2
    target = tmp_path / "A股个股" / "2026" / "1分钟" / "sh600941.csv"
    DOWNLOADER.append_rows(target.parent, target.stem, DOWNLOADER.STOCK_HEADER, [["2026-10-01 09:30:00"] + ["x"] * 11])
    DOWNLOADER.append_rows(target.parent, target.stem, DOWNLOADER.STOCK_HEADER, [["2026-10-01 09:30:00"] + ["y"] * 11])
    values = list(csv.reader(target.open(encoding="utf-8")))
    assert len(values) == 7 and values[-1][1] == "y"


def test_index_pages_resume_from_cached_page(tmp_path):
    class FakeClient:
        def __init__(self, fail_page=None):
            self.calls = []
            self.fail_page = fail_page

        def call(self, name, args):
            self.calls.append(args["page"])
            if args["page"] == self.fail_page:
                raise RuntimeError("interrupted")
            row = {
                "index_code": "000001.SH",
                "trade_time": f"2026-09-30 09:{30 + args['page']:02d}:00",
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "volume": 1,
                "amount": 1,
            }
            return {"response": {"data": {"list": [row], "total": 2}}}

    checkpoint = {"indices": {}}
    first = FakeClient(fail_page=1)
    try:
        DOWNLOADER.download_indices(first, "2026-09-30", "2026-09-30", tmp_path, tmp_path, ["000001.SH"], 1, checkpoint=checkpoint, checkpoint_cache=tmp_path)
    except RuntimeError:
        pass
    second = FakeClient()
    result = DOWNLOADER.download_indices(second, "2026-09-30", "2026-09-30", tmp_path, tmp_path, ["000001.SH"], 1, checkpoint=checkpoint, checkpoint_cache=tmp_path)
    assert first.calls == [0, 1]
    assert second.calls == [1]
    assert result["pages"] == 1


def access_denied(path):
    error = PermissionError(13, "Access denied", str(path))
    error.winerror = 5
    return error


def test_csv_replace_retries_transient_access_denied(tmp_path, monkeypatch):
    header = ["time", "close"]
    target = tmp_path / "stock.csv"
    target.write_text("time,close\n2026-09-14 09:30:00,1\n", encoding="utf-8")
    replace = DOWNLOADER.os.replace
    attempts, delays = [], []

    def occupied(source, destination):
        attempts.append(destination)
        if len(attempts) <= 2:
            raise access_denied(destination)
        replace(source, destination)

    monkeypatch.setattr(DOWNLOADER.os, "replace", occupied)
    monkeypatch.setattr(DOWNLOADER.time, "sleep", delays.append)
    DOWNLOADER.append_rows(tmp_path, "stock", header, [["2026-09-14 09:30:00", 2]])
    assert len(attempts) == 3 and delays == [0.2, 0.5]
    assert target.read_text(encoding="utf-8").endswith(",2\n")
    assert not list(tmp_path.glob("*.tmp"))


def test_csv_replace_persistent_denial_keeps_original_and_cleans_temp(tmp_path, monkeypatch):
    target = tmp_path / "stock.csv"
    original = b"time,close\n2026-09-14 09:30:00,1\n"
    target.write_bytes(original)
    attempts, delays = [], []

    def occupied(source, destination):
        attempts.append(destination)
        raise access_denied(destination)

    monkeypatch.setattr(DOWNLOADER.os, "replace", occupied)
    monkeypatch.setattr(DOWNLOADER.time, "sleep", delays.append)
    with pytest.raises(PermissionError):
        DOWNLOADER.append_rows(tmp_path, "stock", ["time", "close"], [["2026-09-14 09:30:00", 2]])
    assert len(attempts) == 5 and delays == list(DOWNLOADER.FILE_ACCESS_RETRY_DELAYS)
    assert target.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows delete-sharing handle")
def test_real_windows_file_lock_retries_after_handle_release(tmp_path, monkeypatch):
    import ctypes
    from ctypes import wintypes

    target = tmp_path / "stock.csv"
    target.write_text("time,close\n2026-09-14 09:30:00,1\n", encoding="utf-8")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    # Sharing permits readers/writers but denies replacing/deleting this file.
    handle = kernel.CreateFileW(str(target), 0x80000000, 3, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value
    released = []

    def release_on_retry(delay):
        assert kernel.CloseHandle(handle)
        released.append(delay)

    monkeypatch.setattr(DOWNLOADER.time, "sleep", release_on_retry)
    try:
        DOWNLOADER.append_rows(tmp_path, "stock", ["time", "close"], [["2026-09-14 09:30:00", 2]])
        assert released == [0.2]
        assert target.read_text(encoding="utf-8").endswith(",2\n")
    finally:
        if not released:
            kernel.CloseHandle(handle)


def test_pending_stock_file_does_not_block_following_stock_and_resumes(tmp_path, monkeypatch):
    codes = ["600941.SH", "000001.SZ"]
    daily, finance = stock_facts(codes)
    minute_map = dict.fromkeys(codes, source_rows())
    checkpoint = {"days": {}}
    write_rows = DOWNLOADER.append_rows
    calls = []

    def occupied(directory, filename, header, rows):
        if filename == "sh600941":
            raise access_denied(directory / (filename + ".csv"))
        write_rows(directory, filename, header, rows)

    monkeypatch.setattr(DOWNLOADER, "append_rows", occupied)
    result = DOWNLOADER.materialize_stocks(
        "2026-09-14", minute_map, daily, finance, tmp_path,
        checkpoint=checkpoint, checkpoint_cache=tmp_path,
    )
    state = checkpoint["days"]["2026-09-14"]
    assert state["completed_count"] == 2 and state["stocks"]["last_code"] == "sz000001"
    assert not state["complete"] and len(result["pending_files"]) == 1
    assert (tmp_path / "A股个股/2026/1分钟/sz000001.csv").exists()
    # Use the saved state, as a new process would after an interruption.
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))

    def observed(directory, filename, header, rows):
        calls.append((directory.name, filename))
        write_rows(directory, filename, header, rows)

    monkeypatch.setattr(DOWNLOADER, "append_rows", observed)
    result = DOWNLOADER.materialize_stocks(
        "2026-09-14", minute_map, daily, finance, tmp_path,
        checkpoint=checkpoint, checkpoint_cache=tmp_path,
    )
    assert calls == [("1分钟", "sh600941")]
    state = checkpoint["days"]["2026-09-14"]
    assert state["complete"] and not result["pending_files"]
    assert state["completed_count"] == 2 and state["stocks"]["last_code"] == "sz000001"


def checkpoint_identity(periods):
    return {"start_date": "2026-09-30", "end_date": "2026-09-30", "scope": "stocks",
            "periods": periods, "page_size": 10000}


def test_migrate_legacy_checkpoint_keeps_cursor_raw_and_index_pages(tmp_path):
    identity = checkpoint_identity(["1min"])
    old = {"version": 1, "identity": checkpoint_identity(["1min", "5min", "15min", "30min", "60min"]),
           "days": {"2026-09-30": {"complete": False, "completed_count": 2,
                    "stocks": {"last_code": "sz000001"}, "raw": {"sha256": "unchanged", "bytes": 100},
                    "pending_files": {"sh600941": {"1min": {"path": "keep.csv"}, "5min": {"path": "drop.csv"}},
                                      "sz000001": {"60min": {"path": "drop-too.csv"}}}}},
           "indices": {"request": {"1": {"pages": {"0": {"done": True}}}}}}
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(old), encoding="utf-8")
    original = path.read_bytes()
    state = DOWNLOADER.load_checkpoint(tmp_path, identity)
    assert state["identity"] == identity and state["indices"] == old["indices"]
    day = state["days"]["2026-09-30"]
    assert day["raw"] == old["days"]["2026-09-30"]["raw"]
    assert day["completed_count"] == 2 and day["stocks"]["last_code"] == "sz000001"
    assert day["pending_files"] == {"sh600941": {"1min": {"path": "keep.csv"}}}
    assert path.read_bytes() == original
    DOWNLOADER.save_checkpoint(tmp_path, state)
    assert DOWNLOADER.load_checkpoint(tmp_path, identity) == state


def test_missing_completed_minute_file_is_written_individually(tmp_path):
    codes = ["600941.SH", "000001.SZ"]
    daily, finance = stock_facts(codes)
    minute_map = dict.fromkeys(codes, source_rows())
    checkpoint = {"days": {}}
    DOWNLOADER.materialize_stocks("2026-09-30", minute_map, daily, finance, tmp_path,
                                  checkpoint=checkpoint, checkpoint_cache=tmp_path)
    keep = tmp_path / "A股个股/2026/1分钟/sh600941.csv"
    before = (keep.read_bytes(), keep.stat().st_mtime_ns)
    missing = tmp_path / "A股个股/2026/1分钟/sz000001.csv"
    missing.unlink()
    result = DOWNLOADER.materialize_stocks("2026-09-30", minute_map, daily, finance, tmp_path,
                                         checkpoint=checkpoint, checkpoint_cache=tmp_path)
    assert result["rows"] == 5 and missing.exists()
    assert (keep.read_bytes(), keep.stat().st_mtime_ns) == before


def test_period_parameter_is_removed():
    parser = DOWNLOADER.build_parser()
    assert not hasattr(parser.parse_args([]), "periods")
    with pytest.raises(SystemExit):
        parser.parse_args(["--periods", "5min"])


def test_main_migrates_completed_day_without_requesting_raw_data(tmp_path, monkeypatch):
    output = tmp_path / "output"
    cache = tmp_path / "cache"
    day_dir = cache / "days/2026-09-30"
    day_dir.mkdir(parents=True)
    daily, finance = stock_facts(["600941.SH"])
    for name, values in [("daily", daily), ("finance", finance)]:
        records = [{"stock_code": code, **fields} for code, fields in values.items()]
        (day_dir / f"{name}.json").write_text(json.dumps(records), encoding="utf-8")
    minute_map = {"600941.SH": source_rows()}
    raw_path = day_dir / "1min.json.gz"
    raw_path.write_bytes(gzip.compress(json.dumps({"code": 200, "data": minute_map}).encode()))
    DOWNLOADER.materialize_stocks("2026-09-30", minute_map, daily, finance, output)
    target = output / "A股个股/2026/1分钟/sh600941.csv"
    before = (target.read_bytes(), target.stat().st_mtime_ns)
    checkpoint = {"version": 1, "identity": checkpoint_identity(["1min", "5min"]), "indices": {},
                  "days": {"2026-09-30": {"complete": True, "completed_count": 1,
                           "stocks": {"last_code": "sh600941"}, "raw": DOWNLOADER.file_digest(raw_path)}}}
    DOWNLOADER.save_checkpoint(cache, checkpoint)

    class FakeClient:
        def call(self, *args):
            raise AssertionError("cached day must not request provider data")

        def close(self):
            pass

    monkeypatch.setattr(DOWNLOADER, "SCRIPT_DIR", output)
    monkeypatch.setattr(DOWNLOADER, "make_client", lambda *args: FakeClient())
    monkeypatch.setattr(DOWNLOADER, "call_rows", lambda *args: [{"date": "2026-09-30", "is_open": 1}])
    monkeypatch.setattr(DOWNLOADER.sys, "argv", [str(SCRIPT), "--start-date", "2026-09-30", "--end-date",
                        "2026-09-30", "--scope", "stocks", "--no-progress", "--cache", str(cache)])
    assert DOWNLOADER.main() == 0
    state = json.loads((cache / "checkpoint.json").read_text(encoding="utf-8"))
    assert state["identity"]["periods"] == ["1min"] and state["days"]["2026-09-30"]["complete"]
    assert (target.read_bytes(), target.stat().st_mtime_ns) == before


def test_pending_index_files_resume_from_cached_pages(tmp_path, monkeypatch):
    class FakeClient:
        def __init__(self):
            self.calls = []

        def call(self, name, args):
            self.calls.append(args["page"])
            rows = [{"index_code": code, "trade_time": f"2026-09-14 09:{30 + args['page']}:00",
                     "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "amount": 1}
                    for code in ["000001.SH", "399001.SZ"]]
            return {"response": {"data": {"list": rows, "total": 4}}}

    client, checkpoint = FakeClient(), {"indices": {}}
    write_rows = DOWNLOADER.append_rows

    def occupied(directory, filename, header, rows):
        if filename == "sz399001":
            raise access_denied(directory / (filename + ".csv"))
        write_rows(directory, filename, header, rows)

    monkeypatch.setattr(DOWNLOADER, "append_rows", occupied)
    result = DOWNLOADER.download_indices(
        client, "2026-09-14", "2026-09-14", tmp_path, tmp_path,
        ["000001.SH", "399001.SZ"], 2, checkpoint=checkpoint, checkpoint_cache=tmp_path,
    )
    assert client.calls == [0, 1] and len(result["pending_files"]) == 2
    assert not checkpoint["indices"]["2026-09-14:2026-09-14"]["1"]["pages"]["0"]["done"]
    calls = []

    def observed(directory, filename, header, rows):
        calls.append(filename)
        write_rows(directory, filename, header, rows)

    monkeypatch.setattr(DOWNLOADER, "append_rows", observed)
    result = DOWNLOADER.download_indices(
        client, "2026-09-14", "2026-09-14", tmp_path, tmp_path,
        ["000001.SH", "399001.SZ"], 2, checkpoint=checkpoint, checkpoint_cache=tmp_path,
    )
    assert client.calls == [0, 1] and calls == ["sz399001", "sz399001"]
    assert not result["pending_files"]
    with (tmp_path / "分钟K线-指数/2026/1分钟/sz399001.csv").open(encoding="utf-8") as source:
        records = list(csv.reader(source))
    assert [record[1] for record in records[1:]] == ["09:30", "09:31"]


def test_main_returns_incomplete_status_for_deferred_files(tmp_path, monkeypatch, capsys):
    class FakeClient:
        def close(self):
            pass

    monkeypatch.setattr(DOWNLOADER, "make_client", lambda args, cache: FakeClient())
    monkeypatch.setattr(DOWNLOADER, "call_rows", lambda *args: [{"date": "2026-09-14", "is_open": 1}])
    record = {"path": str(tmp_path / "locked.csv"), "error": "Access denied"}
    monkeypatch.setattr(DOWNLOADER, "download_stock_day", lambda *args: {"date": "2026-09-14", "pending_files": [record]})
    monkeypatch.setattr(DOWNLOADER.sys, "argv", [str(SCRIPT), "--start-date", "2026-09-14",
                        "--end-date", "2026-09-14", "--scope", "stocks", "--no-progress", "--cache", str(tmp_path)])
    assert DOWNLOADER.main() == 2
    manifest = json.loads((tmp_path / "last-run.json").read_text(encoding="utf-8"))
    assert not manifest["complete"] and manifest["pending_files"] == [record]
    assert '"status": "incomplete"' in capsys.readouterr().out


def index_row(day, minute):
    return {"index_code": "000001.SH", "trade_time": f"{day} 09:{minute:02d}:00",
            "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "amount": 1}


def test_index_range_is_split_into_calendar_days(tmp_path):
    calls = []

    class Client:
        def call(self, name, args):
            assert args["start_time"] == args["end_time"]
            assert args["page"] * args["page_size"] <= 100000
            calls.append((args["start_time"], args["page"]))
            return {"response": {"data": {"total": 2, "list": [index_row(args['start_time'], 30 + args['page'])]}}}

    cp = {"indices": {}}
    result = DOWNLOADER.download_indices(Client(), "2026-09-11", "2026-09-30", tmp_path, tmp_path,
                                         ["000001.SH"], 1, checkpoint=cp, checkpoint_cache=tmp_path,
                                         trading_days=["2026-09-11", "2026-09-14"])
    assert calls == [("2026-09-11", 0), ("2026-09-11", 1), ("2026-09-14", 0), ("2026-09-14", 1)]
    assert result["rows"] == 4 and not result["pending_files"]
    assert set(cp['indices']) == {"2026-09-11:2026-09-11", "2026-09-14:2026-09-14"}


def test_complete_legacy_day_is_seeded_after_live_total_check(tmp_path):
    old = tmp_path / "indices/2026-09-11_2026-09-14/batch-001"
    old.mkdir(parents=True)
    records = [index_row("2026-09-11", 30), index_row("2026-09-11", 31), index_row("2026-09-14", 30)]
    for page, row in enumerate(records):
        (old / f"page-{page:04d}.json").write_text(json.dumps({"total": 4, "list": [row]}), encoding="utf-8")
    calls = []

    class Client:
        def call(self, name, args):
            calls.append((args['start_time'], args['page']))
            return {"response": {"data": {"total": 2, "list": [index_row(args['start_time'], 30 + args['page'])]}}}

    result = DOWNLOADER.download_indices(Client(), "2026-09-11", "2026-09-14", tmp_path, tmp_path,
                                         ["000001.SH"], 1, trading_days=["2026-09-11", "2026-09-14"])
    assert calls == [("2026-09-11", 0), ("2026-09-14", 0), ("2026-09-14", 1)]
    assert result['rows'] == 4 and len(list(old.glob('*.json'))) == 3


def test_truncated_completed_stock_dump_is_refetched(tmp_path, monkeypatch):
    day, codes = "2026-09-18", ["600941.SH", "000001.SZ"]
    day_dir = tmp_path / 'cache' / 'days' / day
    day_dir.mkdir(parents=True)
    daily, finance = stock_facts(codes)
    for name, values in [('daily', daily), ('finance', finance)]:
        (day_dir / f'{name}.json').write_text(json.dumps([{'stock_code':code, **facts} for code, facts in values.items()]), encoding='utf-8')
    raw = day_dir / '1min.json.gz'
    raw.write_bytes(gzip.compress(json.dumps({'code':200, 'data':{codes[0]:source_rows()}}).encode()))
    cp = {'days':{day:{'complete':True, 'completed_count':1, 'stocks':{'last_code':'sh600941'}, 'raw':DOWNLOADER.file_digest(raw)}}}
    output = tmp_path / 'output'
    monkeypatch.setattr(DOWNLOADER, 'SCRIPT_DIR', output)
    calls = []

    class Client:
        def call(self, name, args):
            calls.append(name)
            downloaded = tmp_path / 'complete.json.gz'
            downloaded.write_bytes(gzip.compress(json.dumps({'code':200, 'data':dict.fromkeys(codes,source_rows())}).encode()))
            return {'download':{'path':str(downloaded)}}

    result = DOWNLOADER.download_stock_day(Client(), day, tmp_path/'cache', checkpoint=cp, checkpoint_cache=tmp_path/'cache')
    assert calls == ['diemeng_stock_daily_dump'] and result['rows'] == 10
    assert cp['days'][day]['complete'] and cp['days'][day]['coverage_verified']
    assert set(DOWNLOADER.read_minute_dump(raw)) == set(codes)


def test_resume_progress_does_not_double_count_or_predict_zero(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(DOWNLOADER.time, 'perf_counter', lambda:clock[0])
    stream = io.StringIO()
    progress = DOWNLOADER.ProgressReporter(True, stream=stream, min_interval=0)
    progress.set_total(4)
    progress.begin_unit('cached', stage_count=1)
    progress.complete_unit(reused=True)
    assert '25.00%' in stream.getvalue().splitlines()[-1]
    assert '估算中' in stream.getvalue().splitlines()[-1]
    progress.begin_unit('new', stage_count=1)
    clock[0] += 10
    progress.complete_unit()
    assert '50.00%' in stream.getvalue().splitlines()[-1]
    assert '剩余 20秒' in stream.getvalue().splitlines()[-1]


def test_checkpoint_is_retained_when_default_end_date_advances(tmp_path):
    old = checkpoint_identity(['1min'])
    cp = {'version':1,'identity':old,'days':{'2026-09-30':{'completed_count':2}}, 'indices':{'saved':{}}}
    DOWNLOADER.save_checkpoint(tmp_path, cp)
    new = dict(old,end_date='2026-10-01')
    state = DOWNLOADER.load_checkpoint(tmp_path, new)
    assert state['identity'] == new and state['days']['2026-09-30']['completed_count'] == 2
    assert state['indices'] == {'saved':{}}


def test_empty_index_page_with_remaining_total_is_not_completed(tmp_path):
    class Client:
        def call(self, name, args):
            return {'response':{'data':{'total':2,'list':[]}}}

    cp = {'indices':{}}
    with pytest.raises(ValueError, match='行数与 total 不符'):
        DOWNLOADER.download_indices(Client(), '2026-09-30', '2026-09-30', tmp_path, tmp_path,
                                     ['000001.SH'], 2, checkpoint=cp, checkpoint_cache=tmp_path)
    assert cp['indices']['2026-09-30:2026-09-30']['1']['pages'] == {}
