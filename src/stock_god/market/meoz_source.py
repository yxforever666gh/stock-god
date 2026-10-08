"""Persisted causal auction collection, separate from prediction decisions."""

import hashlib
import inspect
import json
import math
import re
import time
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timedelta
from threading import RLock
from typing import Any

from .common import CN, MarketDataError, instrument, number, timestamp
from .meoz import MeozError, MeozProvider

SIGNATURE = "meoz-last-tick-1.0.4-book-lot-auction-lot-v1"


def _day(day):
    return str(day).replace("-", "")


def _cents(value):
    n = number(value)
    return int(math.floor(n * 100 + .500001)) if n is not None and n > 0 else 0


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def _full_day_suspension(rows):
    halted = [r for r in rows if r.get("suspend_type") in {"S", "停牌"}]
    if not halted or any(r.get("suspend_type") in {"R", "复牌"} for r in rows):
        return False
    if any(r.get("suspend_timing") is None for r in halted):
        return True
    ranges = []
    for row in halted:
        timing = str(row.get("suspend_timing", ""))
        for start, end in re.findall(r"(\d{2}:\d{2})\s*[-~～—]\s*(\d{2}:\d{2})", timing):
            a, b = [int(value[:2]) * 60 + int(value[3:]) for value in (start, end)]
            if 0 <= a < b <= 1440:
                ranges.append((a, b))
    return all(any(a <= start and end <= b for a, b in ranges)
               for start, end in ((570, 690), (780, 900)))


class MeozAuctionSource:
    """Daily causal facts; readiness follows complete collection, not manual certification."""

    def __init__(self, database, market, clock=None):
        self.database, self.market = database, market
        self.clock = clock or (lambda: datetime.now(CN))
        self._lock = RLock()
        self._timing_day = None

    def with_settings(self, settings):
        return MeozAuctionSource(self.database, self.market.with_settings(deepcopy(settings)), self.clock)

    def close(self):
        self.market.close()

    @property
    def configured(self):
        return self.market.meoz.configured

    def _read(self, day, kind):
        with self.database.connection() as db:
            row = db.execute("SELECT payload_json FROM research2_base43_daily_tasks WHERE trading_date=? AND task_type=?",
                             (_day(day), kind)).fetchone()
        payload = json.loads(row[0]) if row else None
        if payload and _day(day) != "source" and (
                payload.get("keyFingerprint") != self._key_fingerprint()
                or payload.get("sourceSignature") != SIGNATURE):
            return None
        return payload

    def _write(self, day, kind, payload, status="complete"):
        return self._change(day, kind, lambda _: deepcopy(payload), status)

    def _change(self, day, kind, change, status: str | None = "complete"):
        """Serialize only merging facts, and revoke stale credential writes atomically."""
        with self.database.transaction() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='research_settings'").fetchone():
                row = db.execute("SELECT config_json FROM research_settings WHERE center='research2'").fetchone()
                key = json.loads(row[0]).get("meozApiKey", "").strip() if row else ""
                if hashlib.sha256(key.encode()).hexdigest() != self._key_fingerprint():
                    raise MeozError("incomplete", "API Key 已更换，旧采集写入已撤销")
            row = db.execute("SELECT payload_json FROM research2_base43_daily_tasks WHERE trading_date=? AND task_type=?",
                             (_day(day), kind)).fetchone()
            old = json.loads(row[0]) if row else {}
            if old.get("keyFingerprint") != self._key_fingerprint() or old.get("sourceSignature") != SIGNATURE:
                old = {}
            payload = change(old)
            payload.update(keyFingerprint=self._key_fingerprint(), sourceSignature=SIGNATURE)
            status = status or ("complete" if payload.get("complete") else "waiting")
            now = self.clock().isoformat()
            db.execute("INSERT INTO research2_base43_daily_tasks VALUES (?,?,?,?,?,?,?) ON CONFLICT(trading_date,task_type) DO UPDATE SET status=excluded.status,completed_at=excluded.completed_at,payload_json=excluded.payload_json,error=excluded.error",
                       (_day(day), kind, status, now, now, _json(payload), payload.get("error")))
        return payload

    def _progress(self, day, change):
        def merge(saved):
            saved.setdefault("complete", False)
            saved.setdefault("candidates", [])
            saved.setdefault("documents", [])
            change(saved.setdefault("progress", {}), saved)
            return saved
        return self._change(day, "source_candidates", merge, None)

    def _restore_timing(self, day):
        day = _day(day)
        timer = getattr(self.market.meoz, "timing", None)
        if timer is not None and self._timing_day != day:
            timer.restore(self._read(day, "source_timing") or {})
        self._timing_day = day

    @contextmanager
    def stage(self, name, *, day=None):
        """Internal phase timer shared by preparation, polling and model scoring."""
        phase_day = _day(day or self.clock().strftime("%Y%m%d"))
        self._restore_timing(phase_day)
        timer = getattr(self.market.meoz, "timing", None)
        try:
            if timer is None:
                yield
            else:
                scheduled = timer.scheduled(name) if name in {"prepare", "poll"} else nullcontext()
                with scheduled, timer.phase(name, deadline=self._deadline(phase_day)):
                    yield
        finally:
            if timer is not None:
                try:
                    measured = self._read(phase_day, "source_freeze") or {}
                    self._write(phase_day, "source_timing", {**timer.snapshot(),
                        "completeInputs": measured.get("complete", False)})
                except MeozError:
                    pass  # A replaced key must not publish telemetry from its old task.

    def status(self):
        status = self.market.meoz.status()
        if not self.configured:
            return status
        day = self.clock().strftime("%Y%m%d")
        prepared = self._read(day, "source_candidates")
        snapshots = self._read(day, "source_snapshot")
        frozen = self._read(day, "source_freeze")
        if prepared and not prepared.get("complete"):
            daily = prepared.get("sourceStatusJson", {})
            status.update(ready=False, status="unauthorized" if daily.get("status") == "no_permission"
                          else daily.get("status", "incomplete"), message=daily.get("message", "当日资格资料不完整"))
        elif snapshots and snapshots.get("error"):
            status.update(ready=False, status="unauthorized" if snapshots.get("sourceStatus") == "no_permission"
                          else snapshots.get("sourceStatus", "incomplete"), message=snapshots["error"])
        elif frozen and not frozen.get("complete"):
            daily = frozen.get("sourceStatusJson", {})
            status.update(ready=False, status=daily.get("status", "incomplete"),
                          message=daily.get("message", "当日竞价输入不完整"))
        elif frozen and frozen.get("complete"):
            status.update(status="ready", ready=True, message="当日竞价数据已就绪")
        else:
            status.update(message="已配置，等待当日完整竞价数据")
        if prepared and "motherSymbols" in prepared:
            mother_count = len(prepared["motherSymbols"])
            eligible = prepared.get("progress", {}).get("eligibleSymbols")
            if eligible is None:
                status["message"] += f"；母体已存{mother_count}只，资格待核"
            else:
                status["message"] += (f"；母体{mother_count}只，合格{len(eligible)}只"
                                      f"，历史完成{len(prepared.get('candidates', []))}/{len(eligible)}只")
        timing = self._read(day, "source_timing") or {}
        if timing.get("requestCount"):
            status["message"] += f"；请求累计{timing.get('requestElapsedSeconds', 0):.2f}秒"
        if self.clock().strftime("%H%M%S") > "092959" and not status["ready"]:
            status["message"] += "；已截止，缺口不补发买单"
        return status

    def _key_fingerprint(self):
        key = self.market.meoz.settings.get("meozApiKey", "").strip()
        return hashlib.sha256(key.encode()).hexdigest()

    def certify(self, volume_unit, evidence):
        """Only an independently verified complete opening-session receipt qualifies."""
        required = ("passed", "coverageVerified", "volumeVerified", "checkpointsVerified", "finalAuctionVerified")
        if (volume_unit not in {"share", "lot"} or not all(evidence.get(x) is True for x in required)
                or evidence.get("sourceSignature") != SIGNATURE or not evidence.get("tradingDate")
                or not evidence.get("evidenceSha256")):
            raise ValueError("complete live MeoZ verification receipt required")
        self._write("source", "source_certification", {"signature": SIGNATURE,
                    "keyFingerprint": self._key_fingerprint(), "volumeUnit": volume_unit, "evidence": deepcopy(evidence)})

    def _request(self, api, params, fields=None, **kwargs):
        return self.market.meoz_request(api, params, fields, **kwargs)

    @staticmethod
    def _deadline(day):
        return datetime.strptime(_day(day) + "092959", "%Y%m%d%H%M%S").replace(tzinfo=CN)

    @staticmethod
    def _request_key(api, params, fields, history=False):
        return hashlib.sha256(_json([api, params, fields, bool(history)]).encode()).hexdigest()

    def _cached_request(self, day, api, params, fields=None, **kwargs):
        key = self._request_key(api, params, fields, kwargs.get("history", False))
        saved = self._read(day, "source_candidates") or {}
        cached = saved.get("progress", {}).get("requests", {}).get(key)
        if cached is not None:
            timer = getattr(self.market.meoz, "timing", None)
            if timer is not None:
                timer.record_cache_hit()
            return deepcopy(cached)
        result = {**self._request(api, params, fields, **kwargs),
                  "request": {"api": api, "params": deepcopy(params), "fields": fields,
                              "history": bool(kwargs.get("history", False))}}
        def remember(progress, payload):
            progress.setdefault("requests", {})[key] = deepcopy(result)
            payload["documents"].append(deepcopy(result))
        self._progress(day, remember)
        return result

    def _discard_request(self, day, api, params, fields=None):
        key = self._request_key(api, params, fields)
        self._progress(day, lambda progress, _: progress.get("requests", {}).pop(key, None))

    def collect_mother(self, day):
        """Make raw mother symbols available before slow qualification or model work."""
        day = _day(day)
        self._restore_timing(day)
        saved = self._read(day, "source_candidates") or {}
        if "motherSymbols" in saved:
            return saved
        deadline = self._deadline(day)
        params, fields = {"tradedate": day}, "tradedate,symbol,name,pre_type,pre_limit_times"
        # Upgrade old partial bundles without re-fetching their already saved mother.
        for document in saved.get("documents", []):
            names = set(document.get("fields", []))
            if not {"tradedate", "symbol", "pre_type", "pre_limit_times"} <= names:
                continue
            if any(row.get("tradedate") != day for row in document["rows"]):
                continue
            key = self._request_key("limit_pool_yes", params, fields)
            self._progress(day, lambda progress, _, doc=document, key=key:
                           progress.setdefault("requests", {}).setdefault(key, doc))
            break
        try:
            with self.stage("mother", day=day):
                pool = self._cached_request(day, "limit_pool_yes", params, fields, deadline=deadline)
                if any(r.get("tradedate") != day for r in pool["rows"]):
                    raise MeozError("incomplete", "竞价母体非请求交易日")
                mother = [r for r in pool["rows"] if r.get("pre_type") == "u"]
                symbols = [r["symbol"] for r in mother]
                if len(symbols) != len(set(symbols)):
                    raise MeozError("incomplete", "竞价候选母体重复或冲突")
                return self._progress(day, lambda progress, payload: payload.update(
                    motherSymbols=symbols, motherRows=mother,
                    sourceStatusJson={"configured": True, "ready": False, "status": "incomplete",
                                      "message": "母体已保存，等待资格与历史资料"}))
        except MeozError as exc:
            self._discard_request(day, "limit_pool_yes", params, fields)
            self._failure(day, "mother", exc)
            raise

    def _failure(self, day, stage: str, exc):
        phase, _, symbol = stage.partition(":")
        label = {"mother": "母体", "qualification": "资格", "previous-minute": "前日分钟",
                 "history": "历史竞价", "cutoff": "截止"}.get(phase, phase)
        label += "（" + symbol + "）" if symbol else ""
        def merge(progress, payload):
            progress["stage"] = stage
            progress.setdefault("gaps", {})[stage] = str(exc)
            payload.update(complete=False, sourceStatusJson={"configured": self.configured,
                "ready": False, "status": exc.status, "message": label + "：" + str(exc)})
        try:
            return self._progress(day, merge)
        except MeozError:
            return self._bundle(False, [], [], {"configured": self.configured, "ready": False,
                                               "status": "incomplete", "message": str(exc)})

    def _qualified_rows(self, day, kind, api, symbols, params, fields, deadline, expected=None):
        """Keep usable rows of a partial batch, then query only uncovered symbols."""
        saved = (self._read(day, "source_candidates") or {}).get("progress", {}).get("qualification", {}).get(kind, {})
        rows, done = deepcopy(saved.get("rows", {})), set(saved.get("done", []))
        missing = [s for s in symbols if s not in done]
        if missing:
            params = {**params, "symbols": ",".join(missing) if api == "stockbasic" else missing}
            result = self._cached_request(day, api, params, fields, deadline=deadline)
            if any(r.get("symbol") not in missing or expected and r.get("tradedate") != expected for r in result["rows"]):
                self._discard_request(day, api, params, fields)
                raise MeozError("incomplete", "竞价价格资格非请求股票或日期")
            grouped = {}
            for row in result["rows"]:
                symbol = row.get("symbol")
                if symbol not in missing or expected and row.get("tradedate") != expected:
                    continue
                grouped.setdefault(symbol, []).append(row)
            for symbol in missing:
                group = grouped.get(symbol, [])
                valid = api == "suspend" or len(group) == 1
                if valid and api == "stockbasic":
                    valid = isinstance(group[0].get("name"), str) and bool(group[0].get("market"))
                if valid and api == "pricelimit":
                    valid = all(_cents(group[0].get(k)) for k in ("pre_close", "up_limit", "down_limit"))
                if valid:
                    rows[symbol], done = group, done | {symbol}
            self._progress(day, lambda progress, _: progress.setdefault("qualification", {}).update(
                {kind: {"rows": rows, "done": sorted(done)}}))
            if not set(missing) <= done:
                self._discard_request(day, api, params, fields)
                raise MeozError("incomplete", "竞价资格资料覆盖不完整")
        return {s: rows[s] for s in symbols}

    def prepare(self, day, previous_day=None):
        with self.stage("prepare", day=day):
            return self._prepare(day, previous_day)

    def _prepare(self, day, previous_day=None):
        day = _day(day)
        self._restore_timing(day)
        existing = self._read(day, "source_candidates")
        if existing and existing.get("complete"):
            return existing
        if not self.configured:
            return self._bundle(False, [], [], self.status())
        if previous_day is None:
            return self._bundle(False, [], [], {**self.status(), "message": "缺少已核前交易日"})
        previous_day = _day(previous_day)
        deadline = self._deadline(day)
        phase = "mother"
        try:
            prepared = self.collect_mother(day)
            self._progress(day, lambda progress, payload: payload.update(previousDay=previous_day))
            mother = {r["symbol"]: r for r in prepared["motherRows"]}
            symbols = prepared["motherSymbols"]
            phase = "qualification"
            qualified = {}
            with self.stage(phase, day=day):
                for start in range(0, len(symbols), 200):
                    batch = symbols[start:start + 200]
                    basic = self._qualified_rows(day, "basic", "stockbasic", batch, {"list_status": "L"},
                                                 "symbol,name,market,list_status", deadline)
                    limits = self._qualified_rows(day, "limits", "pricelimit", batch, {"tradedate": day}, None, deadline, day)
                    previous = self._qualified_rows(day, "previous", "pricelimit", batch,
                                                    {"tradedate": previous_day}, None, deadline, previous_day)
                    suspend = self._qualified_rows(day, "suspend", "suspend", batch,
                                                   {"startdate": day, "enddate": day}, None, deadline, day)
                    for symbol in batch:
                        info, today, yesterday = (d[symbol][0] for d in (basic, limits, previous))
                        streak = number(mother[symbol].get("pre_limit_times"))
                        if streak is None or streak < 1 or streak != int(streak):
                            raise MeozError("incomplete", "竞价资格字段缺失")
                        halted = any(r.get("suspend_type") in {"S", "停牌"} for r in suspend[symbol])
                        if info["market"] != "主板" or info.get("list_status") != "L" or "ST" in info["name"].upper() or halted:
                            continue
                        prices = [_cents(today.get(k)) for k in ("pre_close", "up_limit", "down_limit")]
                        prior = [_cents(yesterday.get(k)) for k in ("pre_close", "up_limit")]
                        if not prices[2] < prices[0] < prices[1]:
                            raise MeozError("incomplete", "竞价涨跌停资格缺失")
                        qualified[symbol] = {"code": instrument(symbol)["code"], "name": info["name"],
                            "qualificationKnown": True, "eligible": True, "priorStreak": int(streak),
                            "reference": prices[0], "upper": prices[1], "lower": prices[2],
                            "previousReference": prior[0], "previousUpper": prior[1], "previousLabel": 1, "auctionRows": []}
            self._progress(day, lambda progress, _: progress.update(eligibleSymbols=list(qualified)))
            completed = {c["code"] for c in (self._read(day, "source_candidates") or {}).get("candidates", [])}
            failures = False
            for symbol, candidate in qualified.items():
                if candidate["code"] in completed:
                    continue
                if self.clock() >= deadline:
                    self._failure(day, "cutoff", MeozError("incomplete", "资料准备已截止"))
                    failures = True
                    break
                try:
                    phase = "previous-minute"
                    with self.stage(phase, day=day):
                        candidate["previousBars"] = self._previous_minute(symbol, day, previous_day)
                    phase = "history"
                    if self.clock() >= deadline:
                        raise MeozError("incomplete", "资料准备已截止")
                    with self.stage(phase, day=day):
                        candidate["historicalAuctionVolumes"] = self._history_volumes(symbol, day, deadline)
                except (MeozError, MarketDataError) as exc:
                    if not isinstance(exc, MeozError):
                        exc = MeozError("incomplete", "前交易日分钟暂不可用")
                    self._failure(day, phase + ":" + symbol, exc)
                    failures = True
                    if exc.status in {"unconfigured", "no_permission", "error"}:
                        return self._read(day, "source_candidates") or self._bundle(False, [], [], self.status())
                    continue
                def remember(progress, payload, item=candidate, symbol=symbol):
                    payload["candidates"] = [c for c in payload["candidates"] if c["code"] != item["code"]] + [item]
                    for key in ("previous-minute:" + symbol, "history:" + symbol):
                        progress.get("gaps", {}).pop(key, None)
                self._progress(day, remember)
            if failures:
                return self._read(day, "source_candidates") or self._bundle(False, [], [], self.status())
            def finish(progress, payload):
                progress.update(stage="complete", gaps={})
                payload["candidates"] = [c for c in payload["candidates"] if c["code"][2:] in qualified]
                payload.update(complete=True, sourceStatusJson={"configured": True, "ready": False,
                    "status": "incomplete", "message": "资格与历史资料已齐，等待完整盘口"})
            return self._progress(day, finish)
        except (MeozError, MarketDataError) as exc:
            if not isinstance(exc, MeozError):
                exc = MeozError("incomplete", "前交易日分钟暂不可用")
            return self._failure(day, phase, exc)

    def _previous_minute(self, symbol, day, previous_day):
        saved = (self._read(day, "source_candidates") or {}).get("progress", {}).get("minutes", {}).get(symbol)
        if saved is not None:
            timer = getattr(self.market.meoz, "timing", None)
            if timer is not None:
                timer.record_cache_hit()
            return saved
        pday = datetime.strptime(previous_day, "%Y%m%d").replace(tzinfo=CN)
        code, start, end = instrument(symbol)["code"], pday.replace(hour=9, minute=30), pday.replace(hour=15)
        if hasattr(self.market, "_auction_bars"):
            cutoff = self._deadline(day)
            bars = self.market._auction_bars(code, start, end,
                                            budget_seconds=min(30, (cutoff - self.clock()).total_seconds()))
        else:
            bars = self.market.bars(code, start.isoformat(), end.isoformat(), period="1m", adjustment="none")
        minute = []
        for bar in bars:
            when = timestamp(bar["time"])
            if when.date() != pday.date():
                raise MeozError("incomplete", "前交易日分钟日期冲突")
            minute.append({"time": when.hour * 60 + when.minute,
                          "ohlc": [_cents(bar.get(k)) for k in ("open", "high", "low", "close")],
                          "volume": number(bar.get("volume"))})
        if not minute:
            raise MeozError("incomplete", "前交易日分钟输入缺失")
        self._progress(day, lambda progress, _: progress.setdefault("minutes", {}).update({symbol: minute}))
        return minute

    def _history_volumes(self, symbol, day, deadline):
        # Contract clamps ordinary-history ranges to one month: split explicitly.
        end = datetime.strptime(day, "%Y%m%d") - timedelta(days=1)
        progress = (self._read(day, "source_candidates") or {}).get("progress", {})
        if symbol in progress.get("history", {}):
            return progress["history"][symbol]
        dates = progress.get("historyDates")
        if dates is None:
            dates = []
            for ago in range(90):
                date = (end - timedelta(days=ago)).replace(tzinfo=CN)
                if self.market.is_trading_day(date):
                    dates.append(date.strftime("%Y%m%d"))
                    if len(dates) == 20:
                        break
            if len(dates) != 20:
                raise MeozError("incomplete", "历史竞价最近20交易日无法核验")
            dates.reverse()
            self._progress(day, lambda progress, _: progress.update(historyDates=dates))
        end = datetime.strptime(dates[-1], "%Y%m%d")
        first = datetime.strptime(dates[0], "%Y%m%d")
        references = self._cached_request(day, "pricelimit", {"symbols": symbol, "startdate": dates[0],
                                          "enddate": dates[-1]}, "tradedate,symbol,pre_close", deadline=deadline)
        refs = {}
        for row in references["rows"]:
            d = row["tradedate"]
            if row.get("symbol") != symbol or not dates[0] <= d <= dates[-1]:
                raise MeozError("incomplete", "历史竞价价格资格非请求股票或日期")
            if d in refs and refs[d] != row:
                refs[d] = None
            else:
                refs.setdefault(d, row)
        current = first.replace(day=1)
        rows = []
        while current <= end:
            following = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
            last = min(end, following - timedelta(days=1))
            offset = 0
            for _ in range(100):
                result = self._cached_request(day, "daily_auc_detail", {"symbols": symbol,
                    "startdate": max(current, first).strftime("%Y%m%d"), "enddate": last.strftime("%Y%m%d"),
                    "start_time": "09:25:00", "end_time": "09:26:00", "limit": 6000, "offset": offset},
                    "tradedate,symbol,time,m_price,auc_pct_chg,auc_vol,auc_amt", deadline=deadline)
                rows.extend(result["rows"])
                if len(result["rows"]) < 6000:
                    break
                offset += 6000
            else:
                raise MeozError("incomplete", "历史竞价分页未完成")
            current = following
        by_day, selected = {}, {}
        for row in sorted(rows, key=lambda r: str(r.get("time", ""))):
            if row["tradedate"] >= day or row["symbol"] != symbol:
                raise MeozError("incomplete", "历史竞价日期或股票冲突")
            d = row["tradedate"]
            if d not in dates:
                continue
            if d in selected:
                if selected[d].get("time") == row.get("time") and selected[d] != row:
                    by_day[d] = None
                continue
            selected[d] = row
            reference = number((refs.get(d) or {}).get("pre_close"))
            p, gain, volume, amount = [number(row.get(k)) for k in ("m_price", "auc_pct_chg", "auc_vol", "auc_amt")]
            if (reference is not None and reference > 0 and p is not None and p > 0
                     and gain is not None and gain > -100 and volume is not None and volume > 0
                     and amount is not None and amount > 0
                     and abs(p / (1 + gain / 100) - reference) <= .005):
                by_day[d] = volume * 100 if abs(p * volume * 100 - amount) <= max(1, amount * .001) else None
            else:
                by_day[d] = None
        values = [by_day.get(d) for d in dates]
        self._progress(day, lambda progress, _: progress.setdefault("history", {}).update({symbol: values}))
        return values

    @staticmethod
    def _bundle(complete, candidates, documents, state):
        return {"complete": complete, "candidates": candidates, "documents": documents,
                "sourceStatusJson": deepcopy(state)}

    def poll(self, day, now=None, holding_symbols=()):
        now, day = timestamp(now or self.clock()), _day(day)
        if now.strftime("%Y%m%d") != day or not "091500" <= now.strftime("%H%M%S") <= "092959":
            return False
        self._restore_timing(day)
        prepared = self._read(day, "source_candidates") or {}
        mother = prepared.get("progress", {}).get("eligibleSymbols")
        if mother is None:
            mother = [] if prepared.get("complete") else prepared.get("motherSymbols", [])
        symbols = sorted({instrument(s)["code"] for s in mother} |
                         {c["code"] for c in prepared.get("candidates", [])} |
                         {instrument(s)["code"] for s in holding_symbols})
        if not symbols:
            if prepared.get("complete"):
                self._snapshot_merge(day, lastPoll=now.isoformat(), finalComplete=True)
                return True
            return False
        saved = self._read(day, "source_snapshot") or {}
        if saved.get("lastPoll") and (now - timestamp(saved["lastPoll"])).total_seconds() < 3:
            return False
        cutoff = now.replace(hour=9, minute=29, second=59, microsecond=0)
        started = time.monotonic()
        def budget():
            remaining = min(3 - (time.monotonic() - started), (cutoff - self.clock()).total_seconds())
            if remaining <= 0:
                raise MeozError("incomplete", "盘口本轮预算耗尽，等待下一轮")
            return remaining
        try:
            with self.stage("poll", day=day):
                for start in range(0, len(symbols), 200):
                    batch = symbols[start:start + 200]
                    kwargs: dict[str, Any] = {"deadline": cutoff}
                    if "budget_seconds" in inspect.signature(self.market.meoz_ticks).parameters:
                        kwargs["budget_seconds"] = budget()
                    records = self.market.meoz_ticks(day, batch, **kwargs)
                    saved = self._snapshot_merge(day, records=records, lastPoll=now.isoformat())
                if now.strftime("%H%M%S") >= "092500" and not saved.get("finalComplete"):
                    missing = [code for code in symbols if not self._fetched_final(
                        code, day, saved, min(self.clock(), cutoff))]
                    for start in range(0, len(missing), 200):
                        batch = missing[start:start + 200]
                        kwargs = {"final": True, "deadline": cutoff}
                        if "budget_seconds" in inspect.signature(self.market.meoz_ticks).parameters:
                            kwargs["budget_seconds"] = budget()
                        boundary = [{**row, "boundaryFinal": True}
                                    for row in self.market.meoz_ticks(day, batch, **kwargs)]
                        saved = self._snapshot_merge(day, records=boundary)
                    fetched = [code for code in symbols if self._fetched_final(
                        code, day, saved, min(self.clock(), cutoff))]
                    saved = self._snapshot_merge(day, finalFetchedSymbols=fetched)
                    missing = [code for code in fetched if not self._collected_final(
                        code, day, saved, min(self.clock(), cutoff))]
                    for start in range(0, len(missing), 200):
                        batch = missing[start:start + 200]
                        document = self._request("daily_auc_detail", {"tradedate": day,
                            "symbols": [s[2:] for s in batch], "trademin": "0925", "side": "after", "limit": 6000},
                            "tradedate,symbol,time,m_price,auc_vol,auc_amt,um_vol,um_side", deadline=cutoff,
                            budget_seconds=budget())
                        saved = self._snapshot_merge(day, documents=[document])
                    complete = len(fetched) == len(symbols) and all(
                        self._collected_final(code, day, saved, min(self.clock(), cutoff)) for code in symbols)
                    saved = self._snapshot_merge(day, finalComplete=complete)
                self._snapshot_merge(day, error=None, sourceStatus=None)
            if now.strftime("%H%M%S") >= "092600":
                self._backfill(day, symbols, cutoff, budget)
            return True
        except MeozError as exc:
            try:
                self._snapshot_merge(day, error=str(exc), sourceStatus=exc.status, lastPoll=now.isoformat())
            except MeozError:
                pass
            return False

    def _snapshot_merge(self, day, *, records=(), documents=(), **updates):
        def merge(saved):
            saved.setdefault("records", []).extend(deepcopy(records))
            saved.setdefault("documents", []).extend(deepcopy(documents))
            saved.setdefault("lastPoll", None)
            saved.update(updates)
            return saved
        return self._change(day, "source_snapshot", merge, "failed" if updates.get("error") else "complete")

    def _backfill(self, day, symbols, cutoff, budget):
        """One ordinary detail page per poll, saved separately from actual order books."""
        saved = self._read(day, "source_details") or {"batch": 0, "offset": 0, "documents": []}
        if saved.get("symbols") != symbols:
            saved.update(symbols=list(symbols), batch=0, offset=0, complete=False)
        if saved.get("complete"):
            return
        batch = symbols[saved["batch"]:saved["batch"] + 200]
        try:
            with self.stage("backfill", day=day):
                result = self._request("daily_auc_detail", {"tradedate": day, "symbols": [s[2:] for s in batch],
                    "start_time": "09:15:00", "end_time": "09:26:00", "order_dir": "asc",
                    "limit": 6000, "offset": saved["offset"]},
                    "tradedate,symbol,time,m_price,auc_pct_chg,auc_vol,auc_amt,um_vol,um_side",
                    deadline=cutoff, budget_seconds=budget())
                if any(r.get("symbol") not in {s[2:] for s in batch} or r.get("tradedate") != day for r in result["rows"]):
                    raise MeozError("incomplete", "竞价明细非请求股票或日期")
                saved["documents"].append(result)
                if len(result["rows"]) < 6000:
                    saved.update(batch=saved["batch"] + len(batch), offset=0)
                else:
                    saved["offset"] += 6000
                if saved["offset"] >= 600000:
                    raise MeozError("incomplete", "竞价明细分页未完成")
                saved.update(complete=saved["batch"] >= len(symbols), error=None)
                self._write(day, "source_details", saved, "complete" if saved["complete"] else "waiting")
        except MeozError as exc:
            saved.update(error=str(exc))
            try:
                self._write(day, "source_details", saved, "waiting")
            except MeozError:
                pass

    def freeze(self, day, cutoff):
        self._restore_timing(day)
        with self.stage("validation", day=day):
            result = self._measure(day, cutoff)
        self._write(day, "source_freeze", {"complete": result["complete"],
                    "sourceStatusJson": result["sourceStatusJson"], "gaps": result.get("gaps", [])},
                    "complete" if result["complete"] else "failed")
        timer = getattr(self.market.meoz, "timing", None)
        if timer is not None:
            self._write(day, "source_timing", {**timer.snapshot(), "completeInputs": result["complete"]})
        return result

    @staticmethod
    def _valid_final(fields):
        return (all(fields[i] is not None and math.isfinite(fields[i]) and fields[i] > 0 for i in (1, 2, 3))
                and abs(fields[3] / fields[2] - fields[1]) <= .011)

    @classmethod
    def _fetched_final(cls, code, day, saved, limit):
        """Only proven after-09:25 requests can satisfy the final-fetch checkpoint."""
        seen, boundary = {}, []
        for row in saved.get("records", []):
            if row["code"] != code:
                continue
            at = timestamp(row["asOf"])
            if (at.strftime("%Y%m%d") != _day(day) or not "092500" <= at.strftime("%H%M%S") < "093000"
                    or not at <= timestamp(row["availableAt"]) <= limit):
                continue
            if row["asOf"] in seen and seen[row["asOf"]] != row["raw"]:
                return False
            seen.setdefault(row["asOf"], row["raw"])
            if row.get("boundaryFinal"):
                boundary.append(row)
        return any(cls._valid_final(MeozProvider.auction_values(row["raw"], 0, volume_unit="lot"))
                   for row in boundary)

    @classmethod
    def _matched_final(cls, fields, code, day, documents, limit):
        if not cls._valid_final(fields):
            return False
        for doc in documents:
            if timestamp(doc["receivedAt"]) > limit:
                continue
            for row in doc["rows"]:
                if row.get("symbol") != code[2:] or row.get("tradedate", _day(day)) != _day(day):
                    continue
                p, v, amt = [number(row.get(k)) for k in ("m_price", "auc_vol", "auc_amt")]
                if (p is not None and p > 0 and v is not None and v > 0 and amt is not None and amt > 0
                        and abs(p - fields[1]) < .005 and abs(v * 100 - fields[2]) <= 1
                        and abs(amt - fields[3]) <= max(1, amt * .001)
                        and abs(p * v * 100 - amt) <= max(1, amt * .001)):
                    return True
        return False

    @classmethod
    def _collected_final(cls, code, day, saved, limit):
        seen = {}
        for row in saved["records"]:
            at = timestamp(row["asOf"])
            if (row["code"] != code or at.strftime("%Y%m%d") != _day(day)
                    or not "092500" <= at.strftime("%H%M%S") < "093000"
                    or not at <= timestamp(row["availableAt"]) <= limit):
                continue
            if row["asOf"] in seen and seen[row["asOf"]]["raw"] != row["raw"]:
                return False
            seen.setdefault(row["asOf"], row)
        for row in sorted(seen.values(), key=lambda r: r["asOf"]):
            values = MeozProvider.auction_values(row["raw"], 0, volume_unit="lot")
            if cls._valid_final(values):
                return cls._matched_final(values, code, day, saved["documents"], limit)
        return False

    def inspect(self, day, cutoff, volume_unit="lot"):
        """Measure actual collection for offline approval; never grant trading readiness."""
        if volume_unit not in {"share", "lot"}:
            raise ValueError("unsupported volume unit")
        result = self._measure(day, cutoff, inspection_unit=volume_unit)
        flags = ("checkpointsVerified", "coverageVerified", "volumeVerified", "finalAuctionVerified")
        result["checks"] = {flag: bool(result["candidates"]) and all(
            c.get("verification", {}).get(flag, False) for c in result["candidates"]) for flag in flags}
        result["inspectionPassed"] = all(result["checks"].values())
        result["complete"] = False
        result["sourceStatusJson"]["ready"] = False
        result["inspectionOnly"] = True
        return result

    def _measure(self, day, cutoff, inspection_unit=None):
        with self._lock:
            prepared = self._read(day, "source_candidates")
            saved = self._read(day, "source_snapshot")
            state = {"configured": self.configured, "ready": self.configured,
                     "status": "ready" if self.configured else "unconfigured",
                     "message": "当日竞价数据已就绪" if self.configured else "竞价 API 未配置"}
            if not prepared or not prepared.get("complete") or not saved:
                result = self._bundle(False, [], [], {**state, "ready": False, "status": "incomplete", "message": "当日竞价未完整采集"})
                result["gaps"] = [{"kind": "preparation" if not prepared or not prepared.get("complete") else "bookCoverage",
                                  "recoverable": bool(prepared and not prepared.get("complete")),
                                  "details": (prepared or {}).get("progress", {}).get("gaps", {})}]
                return result
            unit = inspection_unit or "lot"
            limit = timestamp(cutoff)
            candidates = deepcopy(prepared["candidates"])
            complete = state["ready"] and not saved.get("error") and bool(candidates or prepared.get("documents"))
            gaps = []
            for c in candidates:
                records = [r for r in saved["records"] if r["code"] == c["code"]
                           and timestamp(r["asOf"]) <= limit and timestamp(r["availableAt"]) <= limit]
                seen = {}
                conflict = False
                for r in records:
                    key = r["asOf"]
                    if key in seen and seen[key]["raw"] != r["raw"]:
                        conflict = True
                    else:
                        seen.setdefault(key, r)
                rows = []
                for r in sorted(seen.values(), key=lambda r: r["asOf"]):
                    t = timestamp(r["asOf"])
                    values = MeozProvider.auction_values(r["raw"], c["reference"] / 100, volume_unit=unit)
                    rows.append({"time": t.hour * 3600 + t.minute * 60 + t.second,
                        "fields": [v if math.isfinite(v) else None for v in values], "receivedAt": r["availableAt"]})
                c["auctionRows"] = rows
                # Contract for virtual matched books, before checkpoint coverage:
                # snapshots are valid only when both matched sides agree.
                pre = []
                for row in rows:
                    if not 33300 <= row["time"] < 33900:
                        continue
                    bid, bid_qty, ask, ask_qty = [row["fields"][i] for i in (5, 6, 9, 10)]
                    if (bid is not None and bid > 0 and ask is not None and abs(bid - ask) < .00001
                            and bid_qty is not None and bid_qty >= 0 and ask_qty is not None
                            and abs(bid_qty - ask_qty) < .001):
                        pre.append(row["time"])
                finals = [r for r in rows if 33900 <= r["time"] < 34200 and self._valid_final(r["fields"])]
                checkpoints = all(any(0 <= target - t <= 6 for t in pre) for target in (33600, 33840, 33890))
                path = checkpoints
                path = path and bool(pre) and min(pre) <= 33306 and max(pre) >= 33890
                path = path and max((b - a for a, b in zip(pre, pre[1:], strict=False)), default=999) <= 6
                matched = bool(finals) and self._matched_final(
                    finals[0]["fields"], c["code"], day, saved["documents"], limit)
                c["coverageComplete"] = not conflict and path and matched
                c["verification"] = {"checkpointsVerified": not conflict and checkpoints,
                    "coverageVerified": not conflict and path, "volumeVerified": not conflict and matched,
                    "finalAuctionVerified": not conflict and bool(finals) and matched}
                if not c["coverageComplete"]:
                    gaps.append({"code": c["code"], "kind": "bookCoverage", "recoverable": False,
                        "conflict": conflict, "startCovered": bool(pre) and min(pre) <= 33306,
                        "endCovered": bool(pre) and max(pre) >= 33890, "finalMatched": matched,
                        "checkpointsMissing": [target for target in (33600, 33840, 33890)
                                               if not any(0 <= target - t <= 6 for t in pre)],
                        "intervals": [{"from": a, "to": b, "seconds": b - a}
                                      for a, b in zip(pre, pre[1:], strict=False) if b - a > 6]})
                complete &= c["coverageComplete"]
            if not complete:
                state.update(ready=False, status="incomplete" if unit else "unverified",
                             message="当日竞价覆盖或单位核验未通过")
            result = self._bundle(bool(complete), candidates, prepared["documents"] + saved["documents"], state)
            result["sourceSignature"] = SIGNATURE
            result["factsSha256"] = hashlib.sha256(_json(result).encode()).hexdigest()
            result["gaps"] = gaps
            return result

    def rules(self, symbol, day):
        day = _day(day)
        code = instrument(symbol)["code"]
        prepared = self._read(day, "source_candidates")
        if prepared and prepared.get("complete"):
            c = next((c for c in prepared["candidates"] if c["code"] == code), None)
            if c:
                return {"known": True, "eligible": True, "suspended": False,
                        "fullDaySuspended": False,
                        "reference": c["reference"], "ref": c["reference"], "lower": c["lower"], "upper": c["upper"]}
        cached = self._read(day, "source_rules:" + code)
        if cached and "fullDaySuspended" in cached:
            return cached
        try:
            result = self._request("pricelimit", {"symbols": code[2:], "tradedate": day})
            halted = self._request("suspend", {"symbols": code[2:], "startdate": day, "enddate": day})
            if any(r.get("tradedate") != day for r in result["rows"] + halted["rows"]):
                raise MeozError("incomplete", "持仓价格资格非请求交易日")
            rows = [r for r in result["rows"] if r["symbol"] == code[2:]]
            if len(rows) != 1:
                raise MeozError("incomplete", "持仓涨跌停资料不完整")
            r = rows[0]
            ref, upper, lower = [_cents(r.get(k)) for k in ("pre_close", "up_limit", "down_limit")]
            if not 0 < lower < ref < upper:
                raise MeozError("incomplete", "持仓价格资格无效")
            halt_rows = [r for r in halted["rows"] if r.get("symbol") == code[2:] and r.get("tradedate") == day]
            value = {"known": True, "eligible": True,
                     "suspended": any(r.get("suspend_type") in {"S", "停牌"} for r in halt_rows),
                     "fullDaySuspended": _full_day_suspension(halt_rows),
                     "suspensionEvidence": halt_rows,
                     "reference": ref, "ref": ref, "lower": lower, "upper": upper}
            self._write(day, "source_rules:" + code, value)
            return value
        except MeozError:
            return {"known": False, "eligible": False, "suspended": None, "fullDaySuspended": False}

    def exit_state(self, symbol, day, buy_date):
        day, buy_date = _day(day), _day(buy_date)
        code = instrument(symbol)["code"]
        cutoff = datetime.strptime(day + "092959", "%Y%m%d%H%M%S").replace(tzinfo=CN)
        # Only locally received pre-open facts may drive the overnight waiting rule.
        saved = self._read(day, "source_snapshot") or {}
        rows = [r for r in saved.get("records", []) if r["code"] == code
                and timestamp(r["asOf"]).strftime("%Y%m%d") == day
                and timestamp(r["asOf"]).strftime("%H%M%S") >= "092500"
                and timestamp(r["asOf"]) <= timestamp(r["availableAt"]) <= cutoff]
        if not rows:
            return {"complete": False, "auctionPrice": None, "buyClose": None}
        seen = {}
        for row in rows:
            if row["asOf"] in seen and seen[row["asOf"]]["raw"] != row["raw"]:
                return {"complete": False, "auctionPrice": None, "buyClose": None}
            seen.setdefault(row["asOf"], row)
        final = min(rows, key=lambda r: r["asOf"])
        values = MeozProvider.auction_values(final["raw"], 0, volume_unit="lot")
        if not self._matched_final(values, code, day, saved.get("documents", []), cutoff):
            return {"complete": False, "auctionPrice": None, "buyClose": None}
        buy = datetime.strptime(buy_date, "%Y%m%d").replace(tzinfo=CN)
        closes = self.market.daily_closes(code, buy.isoformat(), buy.isoformat())
        exact = [r for r in closes if _day(r["tradingDate"]) == buy_date]
        if len(exact) != 1:
            return {"complete": False, "auctionPrice": None, "buyClose": None}
        price, close = number(final["raw"].get("close")), number(exact[0].get("close"))
        return {"complete": price is not None and price > 0 and close is not None and close > 0,
                "auctionPrice": price, "buyClose": close}
