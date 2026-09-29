"""Offline market facts for an explicit prediction-ledger replay."""

import hashlib
import json
import math
import sqlite3
from pathlib import Path

import httpx

from .common import MarketDataError, instrument, timestamp
from .minute_local import minute_time
from .service import MarketServices


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _forbid_network(request):
    raise MarketDataError("offline replay must not request a provider")


class OfflineReplayMarket(MarketServices):
    def __init__(self, config, settings, calendar_path=None, daily_manifest_path=None):
        self._offline_client = httpx.Client(transport=httpx.MockTransport(_forbid_network))
        super().__init__(config, settings, client=self._offline_client)
        with sqlite3.connect(config.minute_db.as_uri() + "?mode=ro", uri=True) as connection:
            self.trading_dates = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT date(trade_time/1000,'unixepoch','+8 hours') FROM minute_bar"
                )
                if row[0]
            }
        self.pre_closes = {}
        if calendar_path is not None:
            manifest = json.loads(Path(calendar_path).read_text(encoding="utf-8"))
            raw = Path(calendar_path).parent / "diemeng-raw" / (manifest["rawSHA256"] + ".json")
            if hashlib.sha256(raw.read_bytes()).hexdigest() != manifest["rawSHA256"]:
                raise MarketDataError("offline trade calendar evidence hash mismatch")
            response = json.loads(raw.read_bytes())
            calendar_days = {
                row["date"] for row in response.get("data", [])
                if isinstance(row, dict) and row.get("is_open") == 1 and row.get("date")
            }
            if calendar_days != set(manifest["openDays"]):
                raise MarketDataError("offline trade calendar differs from original response")
            self.trading_dates.update(calendar_days)
            if daily_manifest_path:
                for day, digest in json.loads(Path(daily_manifest_path).read_text(encoding="utf-8")).items():
                    daily = Path(daily_manifest_path).parent / "diemeng-raw" / (digest + ".json")
                    body = daily.read_bytes()
                    if hashlib.sha256(body).hexdigest() != digest:
                        raise MarketDataError("offline daily evidence hash mismatch")
                    response = json.loads(body)["data"]
                    rows = response["list"]
                    if response["total"] != len(rows):
                        raise MarketDataError("offline daily evidence is incomplete")
                    for row in rows:
                        if row["trade_date"] != day:
                            raise MarketDataError("offline daily evidence date mismatch")
                        if row.get("pre_close", 0) > 0:
                            key = (instrument(row["stock_code"])["code"], day)
                            if key in self.pre_closes:
                                raise MarketDataError("duplicate offline daily pre-close")
                            self.pre_closes[key] = row["pre_close"]

    def close(self):
        self._offline_client.close()

    def is_trading_day(self, at, *, deadline=None):
        return timestamp(at).date().isoformat() in self.trading_dates

    def bars(self, code, start, end, period="1m", adjustment="none", limit=5000):
        if period != "1m" or adjustment != "none":
            raise MarketDataError("offline replay only accepts raw one-minute bars")
        return self.cached_bars(code, start, end, period, adjustment)[-limit:]

    def daily_closes(self, code, start, end):
        latest = {}
        for bar in self.cached_bars(code, start, end, "1m", "none"):
            date = bar["time"][:10]
            latest[date] = {"tradingDate": date, "close": bar["close"], "source": bar["source"]}
        target = timestamp(end).date().isoformat()
        prior = max((day for day in self.trading_dates if day < target), default="")
        pre_close = self.pre_closes.get((instrument(code)["code"], target))
        if pre_close and prior and prior not in latest:
            latest[prior] = {
                "tradingDate": prior,
                "close": pre_close,
                "source": "private-daily:pre_close",
            }
        return [latest[date] for date in sorted(latest)]

    def quote(self, code, **kwargs):
        raise MarketDataError("offline replay does not use current quotes")


def verified_replay_evidence(plan_path, expected_sha256):
    plan_path = Path(plan_path).resolve()
    if _file_sha256(plan_path) != expected_sha256:
        raise MarketDataError("correction plan file changed")
    root = plan_path.parent
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    files = {
        "minuteDbSHA256": root / "chain-a" / "minute.db",
        "calendarSHA256": root / "trading-calendar.json",
        "dailyManifestSHA256": root / "diemeng-daily-manifest.json",
        "financialResultSHA256": root / "chain-a" / "financial-result.json",
    }
    for field, path in files.items():
        if _file_sha256(path) != plan[field]:
            raise MarketDataError("correction evidence changed: " + field)
    raw_files = sorted((root / "diemeng-raw").glob("*.json"))
    if len(raw_files) != plan["rawFileCount"]:
        raise MarketDataError("correction raw response count changed")
    raw_bars = {}
    for path in raw_files:
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != path.stem:
            raise MarketDataError("correction raw response hash mismatch")
        response = json.loads(body)
        data = response.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            continue
        for row in data["list"]:
            if "trade_time" not in row:
                continue
            key = row["stock_code"], minute_time(row["trade_time"]) // 1_000_000
            value = tuple(float(row[name]) for name in ("open", "high", "low", "close")) + (
                float(row["vol"]) * 100, float(row["amount"]),
            )
            old = raw_bars.setdefault(key, value)
            if not all(math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-8) for a, b in zip(old, value, strict=True)):
                raise MarketDataError("conflicting original minute evidence")
    tree = hashlib.sha256(("\n".join(path.stem for path in raw_files) + "\n").encode()).hexdigest()
    if tree != plan["rawTreeSHA256"]:
        raise MarketDataError("correction raw response set changed")
    with sqlite3.connect(files["minuteDbSHA256"].as_uri() + "?mode=ro", uri=True) as db:
        total = 0
        for row in db.execute(
            "SELECT stock_code,trade_time,open,high,low,close,volume,amount "
            "FROM minute_bar WHERE source='private-minute:none'"
        ):
            expected = raw_bars.get(row[:2])
            if expected is None or not all(
                math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-8)
                for a, b in zip(row[2:], expected, strict=True)
            ):
                raise MarketDataError("private minute cache lacks original response evidence")
            total += 1
    if total != plan["privateBarCount"]:
        raise MarketDataError("private minute evidence row count changed")
    return plan, files


def hydrate_private_minutes(target, source):
    with sqlite3.connect(target, timeout=30, uri=True) as db:
        db.execute("ATTACH DATABASE ? AS source", (Path(source).resolve().as_uri() + "?mode=ro",))
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT OR REPLACE INTO minute_bar "
            "(stock_code,trade_time,open,high,low,close,volume,amount,source,updated_at) "
            "SELECT stock_code,trade_time,open,high,low,close,volume,amount,source,updated_at "
            "FROM source.minute_bar WHERE source='private-minute:none'"
        )
        db.commit()
