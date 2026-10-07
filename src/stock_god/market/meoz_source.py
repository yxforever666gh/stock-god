"""Persisted causal auction collection, separate from prediction decisions."""

import hashlib
import json
import math
import re
from copy import deepcopy
from datetime import datetime, timedelta
from threading import RLock

from .common import CN, instrument, number, timestamp
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
    """Daily facts and explicit verification receipts, never guessed readiness."""

    def __init__(self, database, market, clock=None):
        self.database, self.market = database, market
        self.clock = clock or (lambda: datetime.now(CN))
        self._lock = RLock()

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
        return json.loads(row[0]) if row else None

    def _write(self, day, kind, payload, status="complete"):
        now = self.clock().isoformat()
        with self.database.transaction() as db:
            db.execute("INSERT INTO research2_base43_daily_tasks VALUES (?,?,?,?,?,?,?) ON CONFLICT(trading_date,task_type) DO UPDATE SET status=excluded.status,completed_at=excluded.completed_at,payload_json=excluded.payload_json,error=excluded.error",
                       (_day(day), kind, status, now, now, _json(payload), payload.get("error")))

    def status(self):
        status = self._certificate_status()
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
        return status

    def _certificate_status(self):
        status = self.market.meoz.status()
        if not self.configured:
            return status
        cert = self._read("source", "source_certification")
        if cert and cert.get("signature") == SIGNATURE and cert.get("keyFingerprint") == self._key_fingerprint():
            status.update(status="ready", ready=True, message="竞价来源已核验")
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

    def prepare(self, day, previous_day=None):
        day = _day(day)
        with self._lock:
            existing = self._read(day, "source_candidates")
            if existing and existing.get("complete"):
                return existing
            if not self.configured:
                return self._bundle(False, [], [], self.status())
            if previous_day is None:
                return self._bundle(False, [], [], {**self.status(), "message": "缺少已核前交易日"})
            previous_day = _day(previous_day)
            deadline = datetime.strptime(day + "092959", "%Y%m%d%H%M%S").replace(tzinfo=CN)
            documents = []
            try:
                pool = self._request("limit_pool_yes", {"tradedate": day},
                                     "tradedate,symbol,name,pre_type,pre_limit_times", deadline=deadline)
                documents.append(pool)
                if any(r.get("tradedate") != day for r in pool["rows"]):
                    raise MeozError("incomplete", "竞价母体非请求交易日")
                # Qualification is the previous close, never today's dynamic pool type.
                mother = [r for r in pool["rows"] if r.get("pre_type") == "u"]
                symbols = [r["symbol"] for r in mother]
                if len(symbols) != len(set(symbols)):
                    raise MeozError("incomplete", "竞价候选母体重复或冲突")
                candidates = []
                for start in range(0, len(symbols), 200):
                    batch = symbols[start:start + 200]
                    basic = self._request("stockbasic", {"symbols": ",".join(batch), "list_status": "L"},
                                          "symbol,name,market,list_status", deadline=deadline)
                    limits = self._request("pricelimit", {"symbols": batch, "tradedate": day}, deadline=deadline)
                    previous = self._request("pricelimit", {"symbols": batch, "tradedate": previous_day}, deadline=deadline)
                    suspend = self._request("suspend", {"symbols": batch, "startdate": day, "enddate": day}, deadline=deadline)
                    documents.extend([basic, limits, previous, suspend])
                    if (any(r.get("tradedate") != day for r in limits["rows"] + suspend["rows"])
                            or any(r.get("tradedate") != previous_day for r in previous["rows"])):
                        raise MeozError("incomplete", "竞价价格资格非请求交易日")
                    by_symbol = []
                    for result in (basic, limits, previous):
                        mapped = {r["symbol"]: r for r in result["rows"]}
                        if len(mapped) != len(result["rows"]) or set(mapped) != set(batch):
                            raise MeozError("incomplete", "竞价资格资料覆盖不完整")
                        by_symbol.append(mapped)
                    halted = {r["symbol"] for r in suspend["rows"] if r.get("suspend_type") in {"S", "停牌"}}
                    for symbol in batch:
                        info, today, yesterday = [m[symbol] for m in by_symbol]
                        raw = next(r for r in mother if r["symbol"] == symbol)
                        streak = number(raw.get("pre_limit_times"))
                        name = info.get("name")
                        if not isinstance(name, str) or not info.get("market") or streak is None or streak < 1 or streak != int(streak):
                            raise MeozError("incomplete", "竞价资格字段缺失")
                        if (info["market"] != "主板" or info.get("list_status") != "L"
                                or "ST" in name.upper() or symbol in halted):
                            continue
                        prices = [_cents(today.get(k)) for k in ("pre_close", "up_limit", "down_limit")]
                        prior = [_cents(yesterday.get(k)) for k in ("pre_close", "up_limit")]
                        if not all(prices + prior) or not prices[2] < prices[0] < prices[1]:
                            raise MeozError("incomplete", "竞价涨跌停资格缺失")
                        code = instrument(symbol)["code"]
                        pday = datetime.strptime(previous_day, "%Y%m%d").replace(tzinfo=CN)
                        bars = self.market.bars(code, pday.replace(hour=9, minute=30).isoformat(),
                                                pday.replace(hour=15).isoformat(), period="1m", adjustment="none")
                        minute = []
                        for b in bars:
                            when = timestamp(b["time"])
                            if when.date() != pday.date():
                                raise MeozError("incomplete", "前交易日分钟日期冲突")
                            minute.append({"time": when.hour * 60 + when.minute,
                                "ohlc": [_cents(b.get(k)) for k in ("open", "high", "low", "close")],
                                "volume": number(b.get("volume"))})
                        if not minute:
                            raise MeozError("incomplete", "前交易日分钟输入缺失")
                        history = self._history_volumes(symbol, day, deadline)
                        candidates.append({"code": code, "name": name, "qualificationKnown": True, "eligible": True,
                            "priorStreak": int(streak), "reference": prices[0], "upper": prices[1], "lower": prices[2],
                            "previousReference": prior[0], "previousUpper": prior[1], "previousLabel": 1,
                            "previousBars": minute, "historicalAuctionVolumes": history, "auctionRows": []})
                result = self._bundle(True, candidates, documents, self.status())
                result["previousDay"] = previous_day
                self._write(day, "source_candidates", result)
                return result
            except MeozError as exc:
                result = self._bundle(False, [], documents, {"configured": True, "ready": False,
                    "status": exc.status, "message": str(exc)})
                self._write(day, "source_candidates", result, "failed")
                return result

    def _history_volumes(self, symbol, day, deadline):
        # Contract clamps ordinary-history ranges to one month: split explicitly.
        end = datetime.strptime(day, "%Y%m%d") - timedelta(days=1)
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
        first = datetime.strptime(dates[0], "%Y%m%d")
        references = self._request("pricelimit", {"symbols": symbol, "startdate": dates[0],
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
                result = self._request("daily_auc_detail", {"symbols": symbol,
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
        return [by_day.get(d) for d in dates]

    @staticmethod
    def _bundle(complete, candidates, documents, state):
        return {"complete": complete, "candidates": candidates, "documents": documents,
                "sourceStatusJson": deepcopy(state)}

    def poll(self, day, now=None, holding_symbols=()):
        with self._lock:
            now = timestamp(now or self.clock())
            day = _day(day)
            if now.strftime("%Y%m%d") != day or not "091500" <= now.strftime("%H%M%S") <= "092959":
                return False
            prepared = self._read(day, "source_candidates")
            if (not prepared or not prepared.get("complete")) and not holding_symbols:
                return False
            saved = self._read(day, "source_snapshot") or {"records": [], "documents": [], "lastPoll": None}
            if saved["lastPoll"] and (now - timestamp(saved["lastPoll"])).total_seconds() < 3:
                return False
            cutoff = now.replace(hour=9, minute=29, second=59, microsecond=0)
            candidates = prepared["candidates"] if prepared and prepared.get("complete") else []
            symbols = sorted({c["code"] for c in candidates} |
                             {instrument(s)["code"] for s in holding_symbols})
            try:
                saved["records"].extend(self.market.meoz_ticks(day, symbols, deadline=cutoff))
                if now.strftime("%H%M%S") >= "092500" and not saved.get("finalComplete"):
                    final = self.market.meoz_ticks(day, symbols, final=True, deadline=cutoff)
                    saved["records"].extend(final)
                    # Independent matched quantity/amount cross-check, not synthetic order books.
                    for start in range(0, len(symbols), 200):
                        saved["documents"].append(self._request("daily_auc_detail", {
                            "tradedate": day, "symbols": [instrument(s)["code"][2:] for s in symbols[start:start + 200]],
                            "trademin": "0925", "side": "after", "limit": 6000},
                            "tradedate,symbol,time,m_price,auc_vol,auc_amt,um_vol,um_side", deadline=cutoff))
                    saved["finalComplete"] = len({r["code"] for r in final}) == len(symbols)
                saved["lastPoll"] = now.isoformat()
                saved.pop("error", None)
                self._write(day, "source_snapshot", saved)
                return True
            except MeozError as exc:
                saved.update(error=str(exc), sourceStatus=exc.status, lastPoll=now.isoformat())
                self._write(day, "source_snapshot", saved, "failed")
                return False

    def freeze(self, day, cutoff):
        result = self._measure(day, cutoff)
        self._write(day, "source_freeze", {"complete": result["complete"],
                    "sourceStatusJson": result["sourceStatusJson"]}, "complete" if result["complete"] else "failed")
        return result

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
            cert = self._read("source", "source_certification")
            state = self._certificate_status()
            if not prepared or not prepared.get("complete") or not saved:
                return self._bundle(False, [], [], {**state, "ready": False, "status": "incomplete", "message": "当日竞价未完整采集"})
            unit = inspection_unit or (cert.get("volumeUnit") if cert and cert.get("signature") == SIGNATURE
                                       and cert.get("keyFingerprint") == self._key_fingerprint() else None)
            limit = timestamp(cutoff)
            candidates = deepcopy(prepared["candidates"])
            complete = state["ready"] and not saved.get("error")
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
                finals = [r for r in rows if 33900 <= r["time"] < 34200]
                checkpoints = all(any(0 <= target - t <= 6 for t in pre) for target in (33600, 33840, 33890))
                path = checkpoints
                path = path and bool(pre) and min(pre) <= 33306 and max(pre) >= 33890
                path = path and max((b - a for a, b in zip(pre, pre[1:], strict=False)), default=999) <= 6
                matched = False
                if finals:
                    final = finals[0]
                    for doc in saved["documents"]:
                        for r in doc["rows"]:
                            if r["symbol"] == c["code"][2:] and timestamp(doc["receivedAt"]) <= limit:
                                p, v, amt = number(r.get("m_price")), number(r.get("auc_vol")), number(r.get("auc_amt"))
                                f = final["fields"]
                                matched |= (p is not None and v is not None and amt is not None and f[1] is not None
                                    and f[2] is not None and f[3] is not None and abs(p - f[1]) < .005
                                    and abs(v * 100 - f[2]) <= 1 and abs(amt - f[3]) <= max(1, abs(amt) * .001))
                c["coverageComplete"] = not conflict and path and matched
                c["verification"] = {"checkpointsVerified": not conflict and checkpoints,
                    "coverageVerified": not conflict and path, "volumeVerified": not conflict and matched,
                    "finalAuctionVerified": not conflict and bool(finals) and matched}
                complete &= c["coverageComplete"]
            if not complete:
                state.update(ready=False, status="incomplete" if unit else "unverified",
                             message="当日竞价覆盖或单位核验未通过")
            result = self._bundle(bool(complete), candidates, prepared["documents"] + saved["documents"], state)
            result["sourceSignature"] = SIGNATURE
            result["factsSha256"] = hashlib.sha256(_json(result).encode()).hexdigest()
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
        cert = self._read("source", "source_certification")
        unit = cert.get("volumeUnit") if cert and cert.get("signature") == SIGNATURE and cert.get("keyFingerprint") == self._key_fingerprint() else None
        values = MeozProvider.auction_values(final["raw"], 0, volume_unit=unit)
        verified = False
        for doc in saved.get("documents", []):
            if timestamp(doc["receivedAt"]) > cutoff:
                continue
            for row in doc.get("rows", []):
                if row.get("symbol") != code[2:] or row.get("tradedate", day) != day:
                    continue
                p, v, amt = [number(row.get(k)) for k in ("m_price", "auc_vol", "auc_amt")]
                if (p is not None and p > 0 and v is not None and v > 0 and amt is not None and amt > 0
                        and math.isfinite(values[1]) and math.isfinite(values[2]) and math.isfinite(values[3])
                        and abs(p - values[1]) < .005 and abs(v * 100 - values[2]) <= 1
                        and abs(amt - values[3]) <= max(1, amt * .001)
                        and abs(p * v * 100 - amt) <= max(1, amt * .001)):
                    verified = True
        if not verified:
            return {"complete": False, "auctionPrice": None, "buyClose": None}
        buy = datetime.strptime(buy_date, "%Y%m%d").replace(tzinfo=CN)
        closes = self.market.daily_closes(code, buy.isoformat(), buy.isoformat())
        exact = [r for r in closes if _day(r["tradingDate"]) == buy_date]
        if len(exact) != 1:
            return {"complete": False, "auctionPrice": None, "buyClose": None}
        price, close = number(final["raw"].get("close")), number(exact[0].get("close"))
        return {"complete": price is not None and price > 0 and close is not None and close > 0,
                "auctionPrice": price, "buyClose": close}
