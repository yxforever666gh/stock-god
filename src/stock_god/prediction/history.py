"""Complete-minute outcome evaluation, cash-neutral daily returns and trade charts."""

from __future__ import annotations

import itertools
import math
from datetime import timedelta

from .core import (
    SLOTS,
    PredictionError,
    dto,
    json_text,
    local,
    parse_time,
    positive,
    stamp,
    trade_cost,
    upper_limit,
)
from .repository import insert, one, update


def bar_time(bar):
    return local(bar.get("time") or bar.get("at"))


def valid_bar(bar):
    return (
        parse_time(bar.get("time") or bar.get("at")) is not None
        and all(positive(bar.get(key)) for key in ("open", "high", "low", "close"))
        and bar["high"] >= bar["low"]
    )


def classify_outcome(item, data):
    bought = local(item.get("buy_at") or item.get("buyAt"))
    if not bought:
        raise PredictionError("缺少真实买入时间")
    if data.get("noLimitReason"):
        raise PredictionError(data["noLimitReason"])
    limit = upper_limit(data.get("previousClose", 0), data.get("limitRate", 0))
    if not limit:
        raise PredictionError("缺少已核验买入日涨停价")
    first = bought.replace(second=0, microsecond=0) + timedelta(
        minutes=1 + (bought.second != 0 or bought.microsecond != 0)
    )
    end = bought.replace(hour=15, minute=0, second=0, microsecond=0)
    if first > end:
        raise PredictionError("买入晚于最后一个完整分钟")
    bars = {
        bar_time(bar).replace(second=0, microsecond=0): bar for bar in data.get("bars", []) if valid_bar(bar)
    }
    touched = False
    at = first
    while at <= end:
        minute = at.hour * 60 + at.minute
        if not 690 < minute < 781:
            if at not in bars:
                raise PredictionError("买入日分钟覆盖不完整：" + at.strftime("%H:%M"))
            touched |= bars[at]["high"] >= limit - 0.001
        at += timedelta(minutes=1)
    return (
        "sealed" if touched and bars[end]["close"] >= limit - 0.001 else "broken" if touched else "untouched"
    )


class HistoryService:
    def __init__(self, repository, market):
        self.repo, self.market = repository, market

    def backfill(self, trading_date=""):
        result = {
            "outcomeCandidates": 0,
            "outcomesCompleted": 0,
            "outcomesUnavailable": 0,
            "valuationsCompleted": 0,
            "valuationsUnavailable": 0,
        }
        items = self.repo.rows("recommendations", "buy_at IS NOT NULL", order="julianday(buy_at),id")
        now = self.repo.clock()
        for item in items:
            bought = local(item["buy_at"])
            if trading_date and bought.date().isoformat() != trading_date:
                continue
            if item["buy_day_limit_status"] == "complete" or now < bought.replace(
                hour=15, minute=0, second=0, microsecond=0
            ):
                continue
            result["outcomeCandidates"] += 1
            data = {}
            try:
                data = self.market.buy_day_data(dto(item))
                outcome = classify_outcome(item, data)
                status, reason = "complete", ""
                result["outcomesCompleted"] += 1
            except (OSError, ValueError, RuntimeError) as error:
                status, reason, outcome = "unavailable", str(error), ""
                result["outcomesUnavailable"] += 1
            self.repo.set(
                "recommendations",
                {
                    "buy_day_limit_status": status,
                    "buy_day_limit_outcome": outcome,
                    "buy_day_limit_evaluated_at": stamp(now),
                    "buy_day_limit_attempt_count": (item["buy_day_limit_attempt_count"] or 0) + 1,
                    "buy_day_limit_source_json": data.get("sourceStatusJson") or "[]",
                    "buy_day_limit_failure_reason": reason,
                },
                "recommendation_id=?",
                (item["recommendation_id"],),
            )
        if not items:
            return result
        first = (
            local(trading_date)
            if trading_date
            else local(items[0]["buy_at"]).replace(hour=0, minute=0, second=0, microsecond=0)
        )
        end = (
            local(trading_date)
            if trading_date
            else now - (timedelta(days=1) if now.hour < 15 else timedelta())
        )
        days = []
        day = first
        while day.date() <= end.date():
            if self.market.is_trading_day(day):
                days.append(day.replace(hour=0, minute=0, second=0, microsecond=0))
            day += timedelta(days=1)
        if not days:
            return result
        closes = {}
        for stock_code in sorted({item["stock_code"] for item in items}):
            try:
                rows = self.market.daily_closes(stock_code, days[0], days[-1])
            except (OSError, ValueError, RuntimeError):
                rows = []
            closes[stock_code] = {row["tradingDate"]: row for row in rows if positive(row.get("close"))}
        for slot in SLOTS:
            self._valuations(slot, days, items, closes, result)
        return result

    def _valuations(self, slot, days, items, closes, result):
        items = {item["recommendation_id"]: item for item in items if item["slot"] == slot}
        if not items:
            return
        capital = self.repo.rows(
            "account_capital_events", "slot=?", (slot,), "julianday(effective_at),event_id"
        )
        trades = self.repo.rows("trades", "slot=?", (slot,), "julianday(traded_at),trade_id")
        events = sorted(
            [(local(row["effective_at"]), 0, row) for row in capital]
            + [(local(row["traded_at"]), 1, row) for row in trades],
            key=lambda event: (event[0], event[1]),
        )
        cash = 0.0
        positions = {}
        cursor = 0

        def consume(until, day_start):
            nonlocal cash, cursor
            funding = 0.0
            while cursor < len(events) and events[cursor][0] <= until:
                at, kind, row = events[cursor]
                cursor += 1
                if kind == 0:
                    cash += row["amount"]
                    if at >= day_start:
                        funding += row["amount"]
                else:
                    cash += row["net_cash_flow"]
                    if row["side"] == "buy":
                        positions[row["recommendation_id"]] = row
                    else:
                        positions.pop(row["recommendation_id"], None)
            return funding

        consume(days[0] - timedelta(microseconds=1), days[0])
        previous = cash
        ready = previous > 0 and not positions
        if positions:
            baseline = self.repo.rows(
                "account_daily_valuations",
                "slot=? AND trading_date<? AND data_status='complete'",
                (slot, days[0].date().isoformat()),
                "trading_date DESC",
            )
            if baseline:
                previous = baseline[0]["net_asset_value"]
                ready = previous > 0
        for day in days:
            close_at = day.replace(hour=15)
            funding = consume(close_at, day)
            value = 0.0
            sources = {}
            reason = ""
            for identity, trade in positions.items():
                item = items[identity]
                row = closes.get(item["stock_code"], {}).get(day.date().isoformat())
                if not row:
                    reason = "missing unadjusted daily close for " + item["stock_code"]
                    break
                value += trade_cost(item["stock_code"], row["close"], trade["quantity"], "sell")[
                    "net_cash_flow"
                ]
                sources[item["stock_code"]] = row["source"]
            nav = cash + value
            daily_return = None
            if reason:
                ready = False
            elif not ready or previous <= 0:
                reason = "previous complete daily valuation is unavailable"
                previous = nav
                ready = nav > 0
            else:
                daily_return = (nav - funding) / previous - 1
                previous = nav
                ready = nav > 0
            status = "unavailable" if reason else "complete"
            result["valuationsUnavailable" if reason else "valuationsCompleted"] += 1

            def rounded(x):
                return math.floor(x * 100 + 0.5) / 100 if x >= 0 else -math.floor(-x * 100 + 0.5) / 100

            row = {
                "valuation_id": "daily-v2-" + slot + "-" + day.strftime("%Y%m%d"),
                "slot": slot,
                "trading_date": day.date().isoformat(),
                "valued_at": stamp(close_at),
                "cash": rounded(cash),
                "position_value": rounded(value),
                "net_asset_value": rounded(nav),
                "neutral_funding": rounded(funding),
                "daily_return": daily_return,
                "data_status": status,
                "source_status_json": json_text(sources) if sources else "[]",
                "failure_reason": reason,
                "updated_at": stamp(self.repo.clock()),
            }
            with self.repo.db.transaction() as connection:
                existing = one(
                    connection,
                    "SELECT id FROM research2_account_daily_valuations WHERE valuation_id=?",
                    (row["valuation_id"],),
                )
                if existing:
                    update(connection, "account_daily_valuations", row, "id=?", (existing["id"],))
                else:
                    insert(connection, "account_daily_valuations", row)


def _returns(trades, stock_code, price, before=None):
    buys = sells = 0.0
    quantity = 0
    for trade in trades:
        if before and local(trade["traded_at"]) >= before:
            continue
        if trade["side"] == "buy":
            buys += abs(trade["net_cash_flow"])
            quantity += trade["quantity"]
        else:
            sells += trade["net_cash_flow"]
            quantity -= trade["quantity"]
    if buys <= 0 or (quantity > 0 and not positive(price)):
        return 0.0, 0.0
    value = sells + (trade_cost(stock_code, price, quantity, "sell")["net_cash_flow"] if quantity > 0 else 0)
    return value - buys, (value - buys) / buys


def recommendation_chart(repository, market, identity, refresh):
    item = repository.row("recommendations", "recommendation_id=?", (identity,))
    trades = repository.rows("trades", "recommendation_id=?", (identity,), "julianday(traded_at),id")
    now = repository.clock()
    signal = local(item["signal_at"])
    anchor = min((local(trade["traded_at"]) for trade in trades if trade["side"] == "buy"), default=signal)
    start = anchor.replace(hour=9, minute=30, second=0, microsecond=0)
    sold = max((local(trade["traded_at"]) for trade in trades if trade["side"] == "sell"), default=None)
    end_anchor = sold or (anchor if not trades else now)
    end = (
        end_anchor
        if end_anchor.date() == now.date()
        else end_anchor.replace(hour=15, minute=0, second=0, microsecond=0)
    )
    if end.hour >= 15:
        end = end.replace(hour=15, minute=0, second=0, microsecond=0)
    elif 690 < end.hour * 60 + end.minute < 780:
        end = end.replace(hour=11, minute=30, second=0, microsecond=0)
    end = max(start, end)
    errors = []
    raw = []
    quote = {}
    try:
        loader = market.bars if refresh else market.cached_bars
        raw = loader(
            item["stock_code"],
            start if refresh else start - timedelta(days=10),
            end,
            period="1m",
            adjustment="none",
        )
        if refresh:
            try:
                raw = market.cached_bars(
                    item["stock_code"],
                    start - timedelta(days=10),
                    start - timedelta(minutes=1),
                    period="1m",
                    adjustment="none",
                ) + raw
            except (OSError, ValueError, RuntimeError):
                pass  # Prior close is optional; refreshed holding-period bars remain usable.
    except (OSError, ValueError, RuntimeError) as error:
        errors.append({"provider": "minutes", "message": str(error)})
        if refresh:
            try:
                raw = market.cached_bars(
                    item["stock_code"], start - timedelta(days=10), end, period="1m", adjustment="none"
                )
            except (OSError, ValueError, RuntimeError) as cache_error:
                errors.append({"provider": "minute-cache", "message": str(cache_error)})
    if refresh:
        try:
            quote = market.quote(item["stock_code"])
        except (OSError, ValueError, RuntimeError) as error:
            errors.append({"provider": "quote", "message": str(error)})
    all_bars = sorted(
        {bar_time(bar): bar for bar in raw if valid_bar(bar)}.values(), key=bar_time
    )
    bars = []
    for bar in all_bars:
        at = bar_time(bar)
        if not start <= at <= end:
            continue
        pnl, rate = _returns(
            trades,
            item["stock_code"],
            bar["close"],
            at.replace(second=0, microsecond=0) + timedelta(minutes=1),
        )
        bars.append(
            dict(
                at=stamp(at),
                **{key: bar.get(key, 0) for key in ("open", "high", "low", "close", "volume", "amount")},
                source=bar.get("source", ""),
                netPnl=pnl,
                netYieldRate=rate,
            )
        )
    sessions = []
    missing = []
    day = start.replace(hour=12, minute=0)
    previous = 0.0
    while day.date() <= end.date():
        try:
            opened = market.is_trading_day(day) if refresh else day.weekday() < 5
        except (OSError, ValueError, RuntimeError) as error:
            opened = day.weekday() < 5
            errors.append({"provider": "calendar", "message": str(error)})
        if opened and not (day.date() == end.date() and end.hour * 60 + end.minute < 570):
            date = day.date().isoformat()
            rows = [bar for bar in all_bars if bar_time(bar).date() == day.date()]
            if not previous:
                prior = [bar for bar in all_bars if bar_time(bar).date() < day.date()]
                if prior:
                    previous = prior[-1]["close"]
            if quote.get("asOf") and local(quote["asOf"]).date() == day.date() and not previous:
                previous = quote.get("preClose", 0)
            status = "missing"
            if rows:
                target = min(day.replace(hour=15), end)
                gaps = False
                for left, right in itertools.pairwise(rows):
                    a, b = bar_time(left), bar_time(right)
                    if (a.hour < 12) == (b.hour < 12) and (b - a).total_seconds() > 300:
                        gaps = True
                status = (
                    "complete"
                    if bar_time(rows[0]) <= day.replace(hour=9, minute=36)
                    and bar_time(rows[-1]) >= target - timedelta(minutes=5)
                    and not gaps
                    else "partial"
                )
            else:
                missing.append(date)
            sessions.append({"date": date, "previousClose": previous, "status": status})
            if rows:
                previous = rows[-1]["close"]
        day += timedelta(days=1)
    markers = []
    for trade in trades:
        at = local(trade["traded_at"])
        candidates = [
            bar
            for bar in bars
            if local(bar["at"]).date() == at.date()
            and (local(bar["at"]).hour < 12) == (at.hour < 12)
            and abs((local(bar["at"]) - at).total_seconds()) <= 300
        ]
        marker = (
            min(candidates, key=lambda bar: abs((local(bar["at"]) - at).total_seconds()))["at"]
            if candidates
            else None
        )
        markers.append(
            {
                "side": trade["side"],
                "tradedAt": stamp(at),
                "marketPrice": trade["market_price"],
                "executionPrice": trade["execution_price"],
                "quantity": trade["quantity"],
                "totalFees": (trade["commission"] or 0)
                + (trade["stamp_duty"] or 0)
                + (trade["transfer_fee"] or 0),
                "netCashFlow": trade["net_cash_flow"],
                "markerAt": marker,
                "markerSnapped": bool(marker and local(marker) != at),
            }
        )
    price = quote.get("price") or (bars[-1]["close"] if bars else item["current_price"] or 0)
    quoted = quote.get("asOf") or (bars[-1]["at"] if bars else item["current_price_at"])
    pnl, rate = _returns(trades, item["stock_code"], price)
    return {
        "recommendationId": identity,
        "stockCode": item["stock_code"],
        "stockName": item["stock_name"],
        "status": "empty"
        if not bars
        else "complete"
        if all(s["status"] == "complete" for s in sessions)
        else "partial",
        "rangeFrom": stamp(start),
        "rangeTo": stamp(end),
        "refreshedAt": stamp(repository.clock()),
        "quoteAt": stamp(quoted),
        "currentPrice": price,
        "currentNetPnl": pnl,
        "currentNetYieldRate": rate,
        "missingSessions": missing,
        "providerErrors": errors,
        "sessions": sessions,
        "bars": bars,
        "trades": markers,
    }
