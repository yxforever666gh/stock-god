"""OHLC providers, session-aware aggregation and existing drawing storage."""

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from uuid import uuid4

from .common import (
    CN,
    MarketDataError,
    ProviderState,
    database_rows,
    envelope,
    evidence_instrument,
    instrument,
    now,
    number,
    remaining_seconds,
    security_id,
    timestamp,
)
from .news import array

PERIODS = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "60m": 60,
    "day": 1,
    "week": 6,
    "month": 23,
    "quarter": 66,
    "year": 260,
}

AKSHARE_SCRIPT = r"""
import contextlib, json, math, sys
options = json.load(sys.stdin)
with contextlib.redirect_stdout(sys.stderr):
    import akshare as ak
    if options["source"] == "sina":
        frame = ak.stock_zh_a_minute(symbol=options["code"], period="1", adjust="")
    else:
        frame = ak.stock_zh_a_hist_min_em(symbol=options["code"][2:], start_date=options["start"][:10].replace("-", ""), end_date=options["end"][:10].replace("-", ""), period="1", adjust="")
aliases = {"time": ["时间", "日期", "day", "time"], "open": ["开盘", "open"], "high": ["最高", "high"], "low": ["最低", "low"], "close": ["收盘", "close"], "volume": ["成交量", "volume", "vol"], "amount": ["成交额", "amount"]}
columns = {key: next((name for name in names if name in frame.columns), None) for key, names in aliases.items()}
if any(columns[key] is None for key in ("time", "open", "high", "low", "close")):
    raise ValueError("AKShare returned unexpected columns")
result = []
for row in frame.to_dict("records"):
    item = {"time": str(row[columns["time"]])}
    for key in ("open", "high", "low", "close", "volume", "amount"):
        value = row.get(columns[key], 0) if columns[key] else 0
        try:
            value = float(value)
        except (ValueError, TypeError):
            value = 0
        item[key] = value if math.isfinite(value) else 0
    result.append(item)
json.dump(result, sys.stdout, ensure_ascii=False, allow_nan=False)
"""


def proves_unadjusted(source):
    source = str(source or "").strip().lower()
    if not source or any(
        marker in source for marker in ("qfq", "hfq", "adjustment=forward", "adjustment=backward")
    ):
        return False
    if (
        "adjustment=none" in source
        or "unadjusted" in source
        or source == "raw"
        or source.endswith(":raw")
        or "_raw" in source
    ):
        return True
    if source in {
        "sina",
        "tencent",
        "diemeng",
        "diemeng_dump",
        "akshare:em",
        "tencent:none",
        "sina:none",
        "eastmoney:none",
        "private-minute:none",
    }:
        return True
    return source == "test" or source.startswith("test:") or source.endswith("-test")


def valid_bars(rows, start, end):
    clean = {}
    for row in rows:
        at = timestamp(row["time"])
        if not start <= at <= end:
            continue
        values = [number(row.get(key)) for key in ("open", "high", "low", "close")]
        if any(value is None or value <= 0 for value in values):
            continue
        opening, high, low, close = (float(value) for value in values if value is not None)
        if high < max(opening, low, close) or low > min(opening, high, close):
            continue
        row["time"] = at.isoformat()
        clean.setdefault(at, row)
    return [clean[key] for key in sorted(clean)]


def aggregate(rows, period):
    if period in {"1m", "day"}:
        return rows
    groups = {}
    for row in rows:
        at = timestamp(row["time"])
        if period.endswith("m"):
            minute = at.hour * 60 + at.minute
            session = 570 if 570 <= minute <= 690 else 780 if 780 <= minute <= 900 else None
            if session is None:
                continue
            bucket = min((minute - session) // PERIODS[period], 119 // PERIODS[period])
            key = (at.date(), session, bucket)
        elif period == "week":
            key = at.isocalendar()[:2]
        elif period == "month":
            key = (at.year, at.month)
        elif period == "quarter":
            key = (at.year, (at.month - 1) // 3)
        else:
            key = at.year
        if key not in groups:
            groups[key] = dict(row)
        else:
            result = groups[key]
            result.update(
                high=max(result["high"], row["high"]),
                low=min(result["low"], row["low"]),
                close=row["close"],
                volume=result["volume"] + row["volume"],
                amount=result["amount"] + row["amount"],
            )
    return list(groups.values())


class Charts(ProviderState):
    def cached_bars(self, code, start, end, period="1m", adjustment="none"):
        if not period.endswith("m") or adjustment != "none":
            raise MarketDataError("historical cache only proves unadjusted minute bars")
        return aggregate(
            self._cached_minute_bars(instrument(code)["code"], timestamp(start), timestamp(end)), period
        )

    def refresh_recommendation_chart(self, code, start, end):
        """Bound chart-only provider work without changing execution price sources."""
        code, start, end = instrument(code)["code"], timestamp(start), timestamp(end)
        deadline = time.monotonic() + 13
        errors = []
        try:
            cached = self._cached_minute_bars(code, start, end)
        except MarketDataError as exc:
            cached = []
            errors.append({"provider": "minute-cache", "message": str(exc)})
        by_time = {timestamp(bar["time"]): bar for bar in cached}
        opened_dates = set()
        day = end.replace(hour=12, minute=0, second=0, microsecond=0)
        today = now().date()
        while day.date() >= start.date():
            date = day.date().isoformat()
            day_rows = sorted(
                (bar for at, bar in by_time.items() if at.date() == day.date()),
                key=lambda bar: timestamp(bar["time"]),
            )
            if day.weekday() >= 5:
                day -= timedelta(days=1)
                continue
            if not day_rows:
                try:
                    calendar_deadline = min(deadline, time.monotonic() + 1)
                    if not self.is_trading_day(day, deadline=calendar_deadline):
                        day -= timedelta(days=1)
                        continue
                except (MarketDataError, ValueError) as exc:
                    errors.append({"provider": "calendar", "message": str(exc)})
            opened_dates.add(date)
            window_start = max(start, day.replace(hour=9, minute=30))
            window_end = min(end, day.replace(hour=15, minute=0))
            covered = self._chart_window_covered(day_rows, window_start, window_end, day.date() == today)
            if window_start <= window_end and not covered:
                sources = self._public_minute_sources(code, window_start, window_end, 5000)
                if day.date() == today:
                    sources.sort(key=lambda source: source[0] != "tencent")
                if not sources:
                    errors.append({"provider": "minutes", "message": date + ": no enabled minute provider"})
                for name, load in sources:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    cap = {"tencent": 3, "private": 4, "sina": 2, "akshare": 2}[name]
                    if name == "private":
                        cap = min(cap, float(self.settings.get("privateMinuteTimeoutSec") or 60))
                    try:
                        fetched = [
                            bar for bar in load(min(deadline, time.monotonic() + cap))
                            if proves_unadjusted(bar.get("source"))
                        ]
                        if not fetched:
                            raise MarketDataError("no verified minute bars in requested session")
                        for bar in fetched:
                            by_time[timestamp(bar["time"])] = bar
                        try:
                            self._save_minute_bars(code, fetched)
                        except MarketDataError as exc:
                            errors.append({"provider": "minute-cache", "message": str(exc)})
                        day_rows = sorted(
                            (bar for at, bar in by_time.items() if at.date() == day.date()),
                            key=lambda bar: timestamp(bar["time"]),
                        )
                        if self._chart_window_covered(day_rows, window_start, window_end, day.date() == today):
                            break
                    except (MarketDataError, ValueError, KeyError, TypeError) as exc:
                        errors.append({"provider": name, "message": date + ": " + str(exc)})
            day -= timedelta(days=1)
        quote = {}
        if time.monotonic() < deadline:
            try:
                quote = self.quote(code, deadline=min(deadline, time.monotonic() + 2))
            except (MarketDataError, ValueError) as exc:
                errors.append({"provider": "quote", "message": str(exc)})
        if time.monotonic() >= deadline:
            errors.append({"provider": "chart", "message": "refresh time budget exhausted; verified cache retained"})
        return {
            "bars": [by_time[at] for at in sorted(by_time)],
            "quote": quote,
            "openedDates": opened_dates,
            "errors": errors,
        }

    @staticmethod
    def _chart_window_covered(rows, start, end, current_day):
        target = (
            end.replace(second=0, microsecond=0) - timedelta(minutes=1)
            if current_day else end - timedelta(minutes=5)
        )
        if 690 <= target.hour * 60 + target.minute < 780:
            target = target.replace(hour=11, minute=29)
        if target < start:
            return True
        if (
            not rows
            or timestamp(rows[0]["time"]) > start + timedelta(minutes=6)
            or timestamp(rows[-1]["time"]) < target
        ):
            return False
        for left, right in zip(rows, rows[1:], strict=False):
            a, b = timestamp(left["time"]), timestamp(right["time"])
            if (a.hour < 12) == (b.hour < 12) and (b - a).total_seconds() > 300:
                return False
        return True

    def minute_line(self, code, name=""):
        code = instrument(code)["code"]
        endpoint = (
            "https://web.ifzq.gtimg.cn/appstock/app/UsMinute/query"
            if code.startswith("gb_")
            else "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
        )
        data = self.http.json(endpoint, {"code": code})
        if data.get("code", 0) != 0:
            raise MarketDataError("minute-line source rejected request")
        payload = data.get("data", {}).get(code, {}).get("data", {})
        if not payload.get("date") or not isinstance(payload.get("data"), list):
            raise MarketDataError("minute-line source missing date or data")
        rows = []
        for line in payload["data"]:
            fields = line.split()
            if len(fields) < 3 or len(fields[0]) < 4:
                raise MarketDataError("malformed minute-line row")
            rows.append(
                {
                    "time": fields[0][:2] + ":" + fields[0][2:4],
                    "price": number(fields[1]),
                    "volume": number(fields[2]),
                    "amount": number(fields[3]) if len(fields) > 3 else 0,
                }
            )
        return {"priceData": rows, "date": payload["date"], "stockName": name, "stockCode": code}

    def _tencent_bars(self, code, start, end, period, adjustment, limit, deadline=None):
        minute = period.endswith("m")
        if minute and (now() - end > timedelta(days=7) or end > now() + timedelta(minutes=2)):
            raise MarketDataError("Tencent minute source only covers recent windows")
        url = (
            "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
            if minute
            else "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        )
        unit = "m1" if minute else "day"
        count = min(1200 if minute else 12000, limit * PERIODS[period])
        params = {"param": f"{code},{unit},,,{count}" + ("" if minute else f",{adjustment}")}
        response = self.http.json(url, params, headers={"Referer": "https://gu.qq.com/"}, timeout=remaining_seconds(deadline))
        if response.get("code", 0) != 0:
            raise MarketDataError("Tencent bar source rejected request")
        payload = response.get("data", {}).get(code, {})
        key = unit if minute or adjustment == "none" else adjustment + "day"
        rows = array(payload, key)
        result = []
        for row in rows:
            if len(row) < 6:
                continue
            at = (
                datetime.strptime(str(row[0]), "%Y%m%d%H%M").replace(tzinfo=CN)
                if minute
                else timestamp(row[0])
            )
            result.append(
                {
                    "time": at.isoformat(),
                    **{
                        field: number(row[index], 0)
                        for field, index in {"open": 1, "close": 2, "high": 3, "low": 4, "volume": 5}.items()
                    },
                    "amount": 0.0,
                    "source": "tencent:" + ("none" if minute else adjustment),
                }
            )
        return valid_bars(result, start, end)

    def _eastmoney_bars(self, code, start, end, period, adjustment, limit):
        minute = period.endswith("m")
        payload = self.http.json(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            {
                "secid": security_id(code),
                "klt": "1" if minute else "101",
                "fqt": {"none": 0, "qfq": 1, "hfq": 2}[adjustment],
                "beg": start.strftime("%Y%m%d"),
                "end": end.strftime("%Y%m%d"),
                "lmt": min(limit * PERIODS[period], 350000),
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57",
            },
        )
        result = []
        for value in array(payload, "data", "klines"):
            row = value.split(",")
            if len(row) < 7:
                continue
            result.append(
                {
                    "time": timestamp(row[0]).isoformat(),
                    **{
                        field: number(row[index], 0)
                        for field, index in {
                            "open": 1,
                            "close": 2,
                            "high": 3,
                            "low": 4,
                            "volume": 5,
                            "amount": 6,
                        }.items()
                    },
                    "source": "eastmoney:" + adjustment,
                }
            )
        return valid_bars(result, start, end)

    def _sina_bars(self, code, start, end, period, adjustment, limit, deadline=None):
        if adjustment != "none":
            raise MarketDataError("Sina bars do not prove adjusted prices")
        rows = array(
            self.http.json(
                "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData",
                {
                    "symbol": code,
                    "scale": 1 if period.endswith("m") else 240,
                    "ma": "no",
                    "datalen": min(limit * PERIODS[period], 12000),
                },
                timeout=remaining_seconds(deadline),
            )
        )
        result = [
            {
                "time": timestamp(row["day"]).isoformat(),
                **{
                    key: number(row.get(key), 0)
                    for key in ("open", "high", "low", "close", "volume", "amount")
                },
                "source": "sina:none",
            }
            for row in rows
        ]
        return valid_bars(result, start, end)

    def _cached_minute_bars(self, code, start, end):
        symbol = code[2:] + "." + code[:2].upper()
        rows = database_rows(
            self.config.minute_db,
            "SELECT * FROM minute_bar WHERE stock_code IN (?,?,?,?) AND trade_time>=? AND trade_time<=? ORDER BY trade_time,CASE stock_code WHEN ? THEN 0 WHEN ? THEN 1 WHEN ? THEN 2 ELSE 3 END",
            (
                symbol,
                code.upper(),
                code[2:],
                code,
                int(start.timestamp() * 1000),
                int(end.timestamp() * 1000),
                symbol,
                code.upper(),
                code[2:],
            ),
        )
        result = [
            {
                "time": timestamp(row["trade_time"]).isoformat(),
                **{key: float(row[key] or 0) for key in ("open", "high", "low", "close", "volume", "amount")},
                "source": row["source"],
            }
            for row in rows
            if proves_unadjusted(row.get("source"))
        ]
        return valid_bars(result, start, end)

    def _akshare_bars(self, code, start, end, deadline=None):
        preference = self.settings.get("akshareMinuteSourceMode") or "auto"
        if preference not in {"auto", "sina", "em"}:
            raise ValueError("invalid AKShare minute source mode")
        sources = ["sina", "em"] if preference == "auto" else [preference]
        if not self.settings.get("sinaMinuteEnabled", True):
            sources = [source for source in sources if source != "sina"]
        if not sources:
            raise MarketDataError("AKShare configured source is disabled")
        environment = os.environ.copy()
        for key in tuple(environment):
            if key.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}:
                environment.pop(key)
        if self.settings.get("httpProxyEnabled") and not self.settings.get("forceNoProxyForFetch", True):
            environment["HTTP_PROXY"] = environment["HTTPS_PROXY"] = str(self.settings.get("httpProxy") or "")
        environment["PYTHONIOENCODING"] = "utf-8"
        partial, failures = [], []
        for source in sources:
            try:
                output = subprocess.run(
                    [sys.executable, "-c", AKSHARE_SCRIPT],
                    input=json.dumps(
                        {"source": source, "code": code, "start": start.isoformat(), "end": end.isoformat()}
                    ),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    env=environment,
                    timeout=min(90, remaining_seconds(deadline) or 90),
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                if output.returncode != 0:
                    raise MarketDataError(f"AKShare {source} subprocess failed (exit {output.returncode})")
                rows = json.loads(output.stdout)
                label = f"akshare:{source}:adjustment=none"
                rows = valid_bars([row | {"source": label} for row in rows], start, end)
                if not rows:
                    raise MarketDataError(f"AKShare {source} has no bars in the requested range")
                partial = rows
                if timestamp(rows[0]["time"]) <= start + timedelta(minutes=1) and timestamp(
                    rows[-1]["time"]
                ) >= end - timedelta(minutes=1):
                    return rows
            except (subprocess.TimeoutExpired, OSError, ValueError, MarketDataError) as exc:
                failures.append(
                    type(exc).__name__ + ": " + str(exc)[:200]
                    if isinstance(exc, MarketDataError)
                    else type(exc).__name__
                )
        if partial:
            return partial
        raise MarketDataError("AKShare minute sources failed: " + "; ".join(failures))

    def _public_minute_sources(self, code, start, end, limit):
        default = ["tencent", "sina", "akshare", "private"]
        order = self.settings.get("minuteProviderOrder")
        if not order:
            order = (
                ["private", "tencent", "sina", "akshare"]
                if self.settings.get("minuteProviderMode") == "private"
                else default
            )
        if isinstance(order, str):
            order = order.split(",")
        order = list(dict.fromkeys(str(value).strip().lower() for value in order))
        if any(value not in default for value in order):
            raise ValueError("unknown minute provider in configured order")
        order += [value for value in default if value not in order]
        available = {}
        today = now()
        if self.settings.get("tencentMinuteEnabled", True) and (
            end.date() == today.date() or timedelta(0) <= today - end <= timedelta(days=7)
        ):
            available["tencent"] = lambda deadline=None: self._tencent_bars(code, start, end, "1m", "none", limit, deadline)
        if self.settings.get("sinaMinuteEnabled", True) and end.date() == today.date():
            available["sina"] = lambda deadline=None: self._sina_bars(code, start, end, "1m", "none", limit, deadline)
        if self.settings.get("akshareEnabled", True):
            available["akshare"] = lambda deadline=None: self._akshare_bars(code, start, end, deadline)
        if (
            self.settings.get("privateMinuteEnabled")
            and self.settings.get("privateMinuteLevel", "1min") == "1min"
            and self.settings.get("privateMinuteBaseUrl")
            and self.settings.get("privateMinuteApiKey")
        ):
            available["private"] = lambda deadline=None: self._private_bars(code, start, end, deadline)
        return [(name, available[name]) for name in order if name in available]

    def _prediction_eastmoney_bars(self, code, start, end):
        payload = self.http.json(
            "https://push2his.eastmoney.com/api/qt/stock/trends2/get",
            {
                "secid": security_id(code),
                "fields1": "f1,f2,f3,f4,f5,f6,f7,f8",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                "ndays": 2 if start.date() == end.date() else 5,
                "iscr": 0,
            },
        )
        result = []
        for line in array(payload, "data", "trends"):
            fields = line.split(",")
            if len(fields) < 8:
                continue
            result.append(
                {
                    "time": timestamp(fields[0]).isoformat(),
                    **{
                        key: number(fields[index], 0)
                        for key, index in {
                            "open": 1,
                            "close": 2,
                            "high": 3,
                            "low": 4,
                            "volume": 5,
                            "amount": 6,
                        }.items()
                    },
                    "source": "eastmoney:none",
                }
            )
        return valid_bars(result, start, end)

    def _prediction_minutes(self, code, start, end, usable):
        failures = []
        for name, load in (
            ("tencent", lambda: self._tencent_bars(code, start, end, "1m", "none", 1200)),
            ("eastmoney", lambda: self._prediction_eastmoney_bars(code, start, end)),
            ("local-minute-cache", lambda: self._cached_minute_bars(code, start, end)),
        ):
            try:
                rows = [row for row in load() if proves_unadjusted(row.get("source"))]
                if not usable(rows):
                    raise MarketDataError("source does not provide the required prediction minutes")
                return rows
            except (MarketDataError, ValueError, KeyError) as exc:
                failures.append(name + ": " + str(exc))
        raise MarketDataError("prediction minute sources unavailable: " + "; ".join(failures))

    def prediction_window(self, code, start, end, minimum=4):
        code, start, end = instrument(code)["code"], timestamp(start), timestamp(end)
        if start == end and minimum == 0:
            return []
        rows = self._prediction_minutes(
            code,
            start,
            end,
            lambda rows: len([row for row in rows if timestamp(row["time"]) < end]) >= minimum,
        )
        return [row for row in rows if timestamp(row["time"]) < end]

    def _save_minute_bars(self, code, rows):
        if not self.config.minute_db.is_file():
            raise MarketDataError("minute cache database unavailable")
        symbol = code[2:] + "." + code[:2].upper()
        try:
            with sqlite3.connect(self.config.minute_db, timeout=10) as connection:
                connection.executemany(
                    "INSERT INTO minute_bar(stock_code,trade_time,open,high,low,close,volume,amount,source,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(stock_code,trade_time) DO UPDATE SET open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume,amount=excluded.amount,source=excluded.source,updated_at=excluded.updated_at",
                    [
                        (
                            symbol,
                            int(timestamp(row["time"]).timestamp() * 1000),
                            *(row[key] for key in ("open", "high", "low", "close", "volume", "amount")),
                            row["source"],
                            int(now().timestamp() * 1000),
                        )
                        for row in rows
                    ],
                )
        except sqlite3.Error as exc:
            raise MarketDataError("minute cache write failed") from exc

    def _private_bars(self, code, start, end, deadline=None):
        base = str(self.settings.get("privateMinuteBaseUrl") or "https://mg.diemeng.chat/api").rstrip("/")
        result = []
        private_timeout = float(self.settings.get("privateMinuteTimeoutSec") or 60)
        for page in range(1, 51):
            remaining = remaining_seconds(deadline)
            payload = self.http.json(
                base + "/stock/history",
                method="POST",
                headers={"apiKey": self.settings.get("privateMinuteApiKey", "")},
                body={
                    "stock_code": code[2:],
                    "level": "1min",
                    "start_time": start.strftime("%Y-%m-%d %H:%M:%S"),
                    "end_time": end.strftime("%Y-%m-%d %H:%M:%S"),
                    "page": page,
                    "page_size": 5000,
                },
                timeout=min(private_timeout, remaining) if remaining is not None else private_timeout,
            )
            if payload.get("code", 0) not in (0, 200):
                raise MarketDataError("private minute provider rejected request")
            data = payload.get("data") or {}
            items = data.get("items", data.get("list"))
            if not isinstance(items, list):
                raise MarketDataError("private minute data has no items")
            for row in items:
                result.append(
                    {
                        "time": timestamp(row["trade_time"]).isoformat(),
                        **{
                            key: number(row.get(key), 0) for key in ("open", "high", "low", "close", "amount")
                        },
                        "volume": number(row.get("vol"), 0),
                        "source": "private-minute:none",
                    }
                )
            if len(result) >= int(data.get("total", len(result))) or not items:
                return valid_bars(result, start, end)
        raise MarketDataError("private minute pagination exceeded 50 pages")

    def bars(self, code, start, end, period="1m", adjustment="none", limit=5000):
        return self._bars_with_source(code, start, end, period, adjustment, limit)[0]

    def _bars_with_source(self, code, start, end, period, adjustment, limit):
        start, end = timestamp(start), timestamp(end)
        if period not in PERIODS or adjustment not in {"none", "qfq", "hfq"} or start > end:
            raise ValueError("invalid bar period, adjustment or range")
        code = instrument(code)["code"]
        if period.endswith("m") and adjustment != "none":
            raw, source, failures = self._bars_with_source(code, start, end, period, "none", limit)
            daily_start = start.replace(hour=0, minute=0, second=0, microsecond=0)
            daily_end = end.replace(hour=23, minute=59, second=59, microsecond=0)
            daily_count = max(300, (daily_end - daily_start).days + 20)
            unadjusted, _, raw_failures = self._bars_with_source(
                code, daily_start, daily_end, "day", "none", daily_count
            )
            adjusted, _, adjusted_failures = self._bars_with_source(
                code, daily_start, daily_end, "day", adjustment, daily_count
            )
            failures.extend(raw_failures + adjusted_failures)
            raw_closes = {row["time"][:10]: row["close"] for row in unadjusted}
            adjusted_closes = {row["time"][:10]: row["close"] for row in adjusted}
            result, missing = [], set()
            for bar in raw:
                day = bar["time"][:10]
                if not raw_closes.get(day) or not adjusted_closes.get(day):
                    missing.add(day)
                    continue
                ratio = adjusted_closes[day] / raw_closes[day]
                result.append(
                    bar
                    | {
                        **{key: bar[key] * ratio for key in ("open", "high", "low", "close")},
                        "source": bar["source"] + "|adjustment=" + adjustment,
                    }
                )
            for day in sorted(missing):
                failures.append(
                    {
                        "provider": "adjustment",
                        "code": "factor_unavailable",
                        "message": day + " has no verified daily adjustment ratio",
                    }
                )
            if not result:
                raise MarketDataError("minute adjustment factors unavailable")
            return result, source, failures
        failures = []
        sources = []
        minute = period.endswith("m")
        if minute and adjustment == "none":
            sources.append(("local-minute-cache", lambda: self._cached_minute_bars(code, start, end)))
            sources.extend(
                self._public_minute_sources(code, start, end, min(limit * PERIODS[period], 350000))
            )
        # Tencent minute payloads are always unadjusted. Never relabel them qfq/hfq.
        if not minute:
            sources.append(
                ("tencent", lambda: self._tencent_bars(code, start, end, period, adjustment, limit))
            )
        if not minute and adjustment == "none":
            sources.append(("sina", lambda: self._sina_bars(code, start, end, period, adjustment, limit)))
        if not minute:
            sources.append(
                ("eastmoney", lambda: self._eastmoney_bars(code, start, end, period, adjustment, limit))
            )
        for source, load in sources:
            try:
                rows = load()
                if not rows:
                    raise MarketDataError("no bars in requested range")
                if source == "local-minute-cache":
                    effective_start = max(start, start.replace(hour=9, minute=30, second=0, microsecond=0))
                    effective_end = min(end, end.replace(hour=15, minute=0, second=0, microsecond=0))
                    if timestamp(rows[0]["time"]) > effective_start + timedelta(minutes=1) or timestamp(
                        rows[-1]["time"]
                    ) < effective_end - timedelta(minutes=1):
                        raise MarketDataError("minute cache does not cover requested window")
                elif minute and adjustment == "none":
                    try:
                        self._save_minute_bars(code, rows)
                    except MarketDataError as exc:
                        failures.append(
                            {
                                "provider": "local-minute-cache",
                                "code": "cache_write_failed",
                                "message": str(exc),
                            }
                        )
                return aggregate(rows, period)[-limit:], source, failures
            except (MarketDataError, ValueError, KeyError, TypeError) as exc:
                failures.append({"provider": source, "code": "unavailable", "message": str(exc)})
        raise MarketDataError("all bar providers failed: " + "; ".join(item["message"] for item in failures))

    def price_at(self, code, at, sell=False):
        at = timestamp(at)
        current = now()
        if abs((current - at).total_seconds()) <= 180:
            quote = self.quote(code)
            quoted = timestamp(quote["asOf"])
            if (
                quoted < at
                or quoted > current + timedelta(seconds=5)
                or current - quoted > timedelta(minutes=3)
            ):
                raise MarketDataError("live quote is outside execution window")
            return quote | {"quoteAt": quote["asOf"], "mode": "live_after_signal"}
        target = at.replace(second=0, microsecond=0)
        code = instrument(code)["code"]
        bars = self._prediction_minutes(
            code,
            at - timedelta(minutes=1),
            at + timedelta(minutes=2),
            lambda rows: any(
                timestamp(row["time"]).replace(second=0, microsecond=0) == target for row in rows
            ),
        )
        bar = next(row for row in bars if timestamp(row["time"]).replace(second=0, microsecond=0) == target)
        price = bar["close"]
        if bar["amount"] > 0 and bar["volume"] > 0:
            value = bar["amount"] / bar["volume"]
            if bar["low"] * 0.8 < value < bar["high"] * 1.2:
                price = value
        return {
            "code": instrument(code)["code"],
            "price": price,
            "asOf": bar["time"],
            "quoteAt": bar["time"],
            "source": bar["source"],
            "status": "ok",
            "mode": "historical_minute",
        }

    def daily_closes(self, code, start, end):
        start = timestamp(start).replace(hour=0, minute=0, second=0, microsecond=0)
        end = timestamp(end).replace(hour=23, minute=59, second=59, microsecond=0)
        rows = self.bars(code, start, end, "day", "none", max(30, (end - start).days + 20))
        return [
            {"tradingDate": row["time"][:10], "close": row["close"], "source": row["source"]} for row in rows
        ]

    def buy_day_data(self, recommendation):
        if not recommendation.get("buyAt"):
            raise MarketDataError("buy-day data requires buyAt")
        day = timestamp(recommendation["buyAt"])
        previous = None
        for offset in range(1, 21):
            candidate = day - timedelta(days=offset)
            if self.is_trading_day(candidate):
                previous = candidate.date().isoformat()
                break
        if previous is None:
            raise MarketDataError("previous trading day unavailable")
        code = recommendation["stockCode"]
        daily = self.daily_closes(code, day - timedelta(days=40), day)
        close = next((row["close"] for row in daily if row["tradingDate"] == previous), None)
        if not close:
            raise MarketDataError("verified previous trading-day close unavailable")
        bars = self.bars(
            code,
            day.replace(hour=9, minute=30, second=0, microsecond=0),
            day.replace(hour=15, minute=0, second=0, microsecond=0),
            "1m",
            "none",
        )
        result = {
            "previousClose": close,
            "bars": bars,
            "limitRate": 0.0,
            "noLimitReason": "",
            "sourceStatusJson": json.dumps(
                {"sources": sorted({row["source"] for row in bars}), "daily": daily, "errors": []}
            ),
        }
        if len(daily) < 6:
            result["noLimitReason"] = (
                "fewer than six verified pre-buy daily sessions; price-limit rule is unavailable"
            )
        else:
            normalized = instrument(code)["code"]
            name = recommendation.get("stockName", "").strip().upper()
            result["limitRate"] = (
                0.05
                if name.startswith(("ST", "*ST"))
                else 0.2
                if normalized.startswith(("sh68", "sz30"))
                else 0.3
                if normalized.startswith("bj")
                else 0.1
            )
        return result

    def chart(
        self,
        code,
        asset_type="stock",
        market="",
        period="day",
        adjustment="",
        start=None,
        end=None,
        limit=500,
    ):
        identity = evidence_instrument(code, asset_type, market)
        if period not in PERIODS or not 1 <= limit <= 5000:
            raise ValueError("invalid chart period or limit")
        adjustment = adjustment or ("qfq" if asset_type == "stock" else "none")
        if adjustment not in {"none", "qfq", "hfq"} or asset_type == "index" and adjustment != "none":
            raise ValueError("invalid chart adjustment")
        end = timestamp(end or now())
        start = (
            timestamp(start)
            if start
            else end - timedelta(minutes=limit * PERIODS[period] * 3)
            if period.endswith("m")
            else end - timedelta(days=limit * PERIODS[period] * 2)
        )
        if start > end:
            raise ValueError("from must not be after to")
        data = {
            "instrument": identity,
            "period": period,
            "adjustment": adjustment,
            "timezone": "Asia/Shanghai",
            "rangeFrom": start.isoformat(),
            "rangeTo": end.isoformat(),
            "bars": [],
            "missingIntervals": [],
        }
        try:
            rows, source, errors = self._bars_with_source(code, start, end, period, adjustment, limit)
            data["bars"] = rows
            return envelope(
                data, source, as_of=rows[-1]["time"], errors=errors, status="partial" if errors else "ok"
            )
        except MarketDataError as exc:
            data["missingIntervals"] = [
                {"from": start.isoformat(), "to": end.isoformat(), "reason": str(exc)}
            ]
            return envelope(
                data,
                "",
                status="unavailable",
                errors=[{"provider": "chart", "code": "unavailable", "message": str(exc)}],
            )

    def drawings(
        self,
        code,
        asset_type,
        market,
        period,
        adjustment,
        *,
        expected_revision=None,
        drawings=None,
        delete=False,
    ):
        identity = evidence_instrument(code, asset_type, market)
        if period not in PERIODS or adjustment not in {"none", "qfq", "hfq"}:
            raise ValueError("invalid drawing scope")
        key = ("user", "local", asset_type, identity["market"], identity["code"], period, adjustment)
        where = "scope_type=? AND scope_id=? AND asset_type=? AND market=? AND code=? AND period=? AND adjustment=?"
        if expected_revision is None:
            rows = database_rows(
                self.config.main_db, "SELECT * FROM chart_drawing_documents WHERE " + where, key
            )
            row = rows[0] if rows else None
        else:
            if expected_revision < 0:
                raise ValueError("expectedRevision must be non-negative")
            if not delete and (not isinstance(drawings, list) or len(drawings) > 500):
                raise ValueError("drawings must be an array of up to 500 entries")
            if not delete:
                assert isinstance(drawings, list)
                ids = set()
                for item in drawings:
                    if (
                        not isinstance(item, dict)
                        or not item.get("id")
                        or len(item["id"].encode()) > 128
                        or item["id"] in ids
                        or item.get("type")
                        not in {
                            "measure",
                            "trend_line",
                            "ray",
                            "fibonacci_retracement",
                            "horizontal_line",
                            "wave",
                        }
                    ):
                        raise ValueError("invalid or duplicate drawing")
                    ids.add(item["id"])
                    minimum, maximum = (
                        (1, 1)
                        if item["type"] == "horizontal_line"
                        else (3, 64)
                        if item["type"] == "wave"
                        else (2, 2)
                    )
                    if not minimum <= len(item.get("points", [])) <= maximum:
                        raise ValueError("invalid drawing point count")
                    for point in item.get("points", []):
                        timestamp(point["time"])
                        if number(point.get("value")) is None:
                            raise ValueError("invalid drawing point")
            payload = json.dumps([] if delete else drawings, ensure_ascii=False)
            if len(payload.encode()) > 256 * 1024:
                raise ValueError("drawing payload exceeds 256 KiB")
            with sqlite3.connect(self.config.main_db, timeout=10) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT * FROM chart_drawing_documents WHERE " + where, key
                ).fetchone()
                if (current["revision"] if current else 0) != expected_revision:
                    raise ValueError("chart drawing revision conflict")
                if delete and not current:
                    raise LookupError("chart drawing document not found")
                doc_id = current["drawing_document_id"] if current else str(uuid4())
                at = now().isoformat()
                revision = expected_revision + 1
                deleted = at if delete else None
                if current:
                    connection.execute(
                        "UPDATE chart_drawing_documents SET revision=?,drawings_json=?,deleted_at=?,updated_at=? WHERE "
                        + where,
                        (revision, payload, deleted, at) + key,
                    )
                else:
                    connection.execute(
                        "INSERT INTO chart_drawing_documents(drawing_document_id,scope_type,scope_id,asset_type,market,code,period,adjustment,revision,drawings_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (doc_id,) + key + (revision, payload, at, at),
                    )
                connection.execute(
                    "INSERT INTO chart_drawing_revisions(document_id,revision,drawings_json,deleted_at,created_at) VALUES (?,?,?,?,?)",
                    (doc_id, revision, payload, deleted, at),
                )
                row = dict(
                    connection.execute("SELECT * FROM chart_drawing_documents WHERE " + where, key).fetchone()
                )
        return {
            "instrument": identity,
            "period": period,
            "adjustment": adjustment,
            "revision": row["revision"] if row else 0,
            "drawings": json.loads(row["drawings_json"]) if row else [],
            "updatedAt": row["updated_at"] if row else "0001-01-01T00:00:00Z",
            **({"deletedAt": row["deleted_at"]} if row and row["deleted_at"] else {}),
        }
