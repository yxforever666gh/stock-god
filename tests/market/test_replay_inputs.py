import hashlib
import json
import sqlite3

import pytest

from stock_god.market.common import MarketDataError
from stock_god.market.minute_local import minute_time
from stock_god.market.replay_inputs import hydrate_private_minutes, verified_replay_evidence

SCHEMA = (
    "CREATE TABLE minute_bar (stock_code TEXT, trade_time INTEGER, open REAL, high REAL, "
    "low REAL, close REAL, volume REAL, amount REAL, source TEXT, updated_at INTEGER, "
    "PRIMARY KEY(stock_code,trade_time))"
)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_replay_minute_import_requires_original_response_and_only_copies_private_rows(tmp_path):
    source = tmp_path / "chain-a" / "minute.db"
    source.parent.mkdir()
    target = tmp_path / "target.db"
    stamp = minute_time("2026-09-24 09:50:00") // 1_000_000
    for path in (source, target):
        with sqlite3.connect(path) as db:
            db.execute(SCHEMA)
    with sqlite3.connect(source) as db:
        db.execute(
            "INSERT INTO minute_bar VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("600001.SH", stamp, 10, 10, 10, 10, 100, 1000, "private-minute:none", 1),
        )
        db.execute(
            "INSERT INTO minute_bar VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("600002.SH", stamp, 20, 20, 20, 20, 100, 2000, "other", 1),
        )
    raw = tmp_path / "diemeng-raw"
    raw.mkdir()
    body = json.dumps({"code": 200, "data": {"total": 1, "list": [{
        "stock_code": "600001.SH", "trade_time": "2026-09-24 09:50:00",
        "open": 10, "high": 10, "low": 10, "close": 10, "vol": 1, "amount": 1000,
    }]}}).encode()
    raw_digest = hashlib.sha256(body).hexdigest()
    (raw / (raw_digest + ".json")).write_bytes(body)
    for name in ("trading-calendar.json", "diemeng-daily-manifest.json"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    (source.parent / "financial-result.json").write_text("{}", encoding="utf-8")
    plan = {
        "minuteDbSHA256": sha(source),
        "calendarSHA256": sha(tmp_path / "trading-calendar.json"),
        "dailyManifestSHA256": sha(tmp_path / "diemeng-daily-manifest.json"),
        "financialResultSHA256": sha(source.parent / "financial-result.json"),
        "rawFileCount": 1,
        "rawTreeSHA256": hashlib.sha256((raw_digest + "\n").encode()).hexdigest(),
        "privateBarCount": 1,
    }
    manifest = tmp_path / "correction-plan.json"
    manifest.write_text(json.dumps(plan), encoding="utf-8")
    verified_replay_evidence(manifest, sha(manifest))
    hydrate_private_minutes(target, source)
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT count(*) FROM minute_bar").fetchone()[0] == 1
        assert db.execute("SELECT close FROM minute_bar").fetchone()[0] == 10
    with sqlite3.connect(source) as db:
        db.execute("UPDATE minute_bar SET close=11 WHERE stock_code='600001.SH'")
    plan["minuteDbSHA256"] = sha(source)
    manifest.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(MarketDataError, match="lacks original response"):
        verified_replay_evidence(manifest, sha(manifest))
