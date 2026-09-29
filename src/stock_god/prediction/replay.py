"""Deterministic full-history allocation replay, applied only by explicit callers."""

from __future__ import annotations

import hashlib
import math
import uuid
from datetime import datetime, timedelta
from typing import Any

from stock_god.jsonutil import dumps

from .core import (
    ALLOCATION_POLICY,
    SLOTS,
    PredictionError,
    dto,
    json_text,
    local,
    next_session,
    positive,
    size_buy,
    stamp,
    trade_cost,
    upper_limit,
)
from .history import HistoryService, bar_time, valid_bar
from .repository import insert, one, update

POLICY = "remaining_cash_by_open_slots_ashare_cost_v2"
MODE = "historical_allocation_replay_v1"


def rfc(value):
    if not value:
        return ""
    at = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00").replace(" +", "+"))
    )
    text = at.strftime("%Y-%m-%dT%H:%M:%S")
    if at.microsecond:
        text += "." + f"{at.microsecond:06}".rstrip("0")
    offset = at.strftime("%z")
    return text + ("Z" if offset in ("", "+0000") else offset[:3] + ":" + offset[3:])


def plan_hash(states, capital, trades):
    state_rows = []
    for state in states:
        buy, sell = state.get("buy"), state.get("sell")
        state_rows.append(
            {
                "ID": state["item"]["recommendation_id"],
                "Status": state["status"],
                "Failure": state.get("failure", ""),
                "Source": buy["price_source"] if buy else "",
                "BuyAt": rfc(buy["traded_at"]) if buy else "",
                "SellAt": rfc(sell["traded_at"]) if sell else "",
                "BuyPrice": buy["market_price"] if buy else 0.0,
                "SellPrice": sell["market_price"] if sell else 0.0,
                "Quantity": buy["quantity"] if buy else 0,
                "Blocked": bool(state.get("blocked")),
            }
        )
    capitals = [
        {"ID": row["event_id"], "Slot": row["slot"], "At": rfc(row["effective_at"]), "Amount": row["amount"]}
        for row in capital
    ]
    trade_rows = [
        {
            "ID": row["trade_id"],
            "Side": row["side"],
            "ExecutionPrice": row["execution_price"],
            "Commission": row["commission"],
            "StampDuty": row["stamp_duty"],
            "TransferFee": row["transfer_fee"],
            "SlippageAmount": row["slippage_amount"],
            "NetCashFlow": row["net_cash_flow"],
        }
        for row in trades
    ]
    value = {
        "Policy": POLICY,
        "States": state_rows or None,
        "Capital": capitals or None,
        "Trades": trade_rows or None,
    }
    return hashlib.sha256(dumps(value, sort_keys=False).encode()).hexdigest()


class QuoteGap(PredictionError):
    def __init__(self, code, reason):
        self.code = code
        super().__init__(reason)


class AllocationReplay:
    def __init__(self, repository, market):
        self.repo, self.market = repository, market
        self.sessions = {}

    def quote(self, item, at, sell=False):
        key = (item["stock_code"], at.date())
        if key not in self.sessions:
            try:
                self.sessions[key] = self.market.buy_day_data({**dto(item), "buyAt": stamp(at)})
            except (OSError, ValueError, RuntimeError) as error:
                self.sessions[key] = error
        data = self.sessions[key]
        if isinstance(data, Exception):
            raise QuoteGap("historical_quote_unavailable", "历史行情不可用：" + str(data))
        bar = next(
            (
                row
                for row in data["bars"]
                if bar_time(row).replace(second=0, microsecond=0) == at.replace(second=0, microsecond=0)
            ),
            None,
        )
        if bar is None:
            raise QuoteGap("historical_minute_missing", "目标分钟线缺失")
        if not valid_bar(bar):
            raise QuoteGap("historical_minute_invalid", "目标分钟价格无效")
        if bar.get("volume", 0) <= 0 and bar.get("amount", 0) <= 0:
            raise QuoteGap("historical_suspended", "目标分钟无可验证成交量")
        previous, rate = data.get("previousClose", 0), data.get("limitRate", 0)
        if not positive(previous) or not positive(rate) or data.get("noLimitReason"):
            raise QuoteGap(
                "historical_limit_unavailable", data.get("noLimitReason") or "缺少前收盘价或适用涨跌停规则"
            )
        price = bar["close"]
        if bar.get("amount", 0) > 0 and bar.get("volume", 0) > 0:
            average = bar["amount"] / bar["volume"]
            if bar["low"] * 0.8 < average < bar["high"] * 1.2:
                price = average
        upper = upper_limit(previous, rate)
        cents = math.floor(previous * 100 + 0.5)
        basis = math.floor(rate * 10000 + 0.5)
        lower = ((cents * (10000 - basis) + 5000) // 10000) / 100
        if sell and (price <= lower + 1e-6 or bar["high"] <= lower + 1e-6):
            raise QuoteGap("historical_limit_down", "目标分钟封死跌停")
        distance = (upper - price) / upper * 100
        if not sell:
            if price >= upper - 1e-6:
                raise QuoteGap("limit_up", "历史目标分钟已涨停")
            if price <= lower + 1e-6:
                raise QuoteGap("limit_down", "历史目标分钟已跌停")
            if distance + 1e-9 < 1:
                raise QuoteGap("near_limit_up", "历史目标分钟距涨停不足1%")
        return {
            "price": price,
            "at": at.replace(second=0, microsecond=0),
            "quoteAt": at.replace(second=0, microsecond=0),
            "source": bar.get("source", ""),
            "limit": upper,
            "distance": None if sell else distance,
        }

    def build(self):
        now = self.repo.clock()
        successful = {
            row["run_id"]: row
            for row in self.repo.rows(
                "analysis_runs", "status='success'", order="trading_date,julianday(generated_at),id"
            )
        }
        runs = {identity: row for identity, row in successful.items() if row["published"]}
        if not runs:
            raise PredictionError("没有成功报告可供历史回放")
        historical_items = [r for r in self.repo.rows("recommendations") if r["analysis_run_id"] in successful]
        items = [r for r in historical_items if r["analysis_run_id"] in runs]
        if not items:
            raise PredictionError("没有推荐记录可供历史回放")
        known = {r["recommendation_id"] for r in historical_items}
        active = {r["recommendation_id"] for r in items}
        stored = {}
        for trade in self.repo.rows("trades"):
            if trade["recommendation_id"] not in known:
                raise PredictionError("存在成功报告历史之外的交易")
            if trade["side"] == "sell" and trade["recommendation_id"] in active:
                if trade["recommendation_id"] in stored:
                    raise PredictionError("同一推荐有多笔历史卖出")
                stored[trade["recommendation_id"]] = trade
        chains = self.repo.rows("execution_chains", order="trading_date,slot")
        if any(row["target_slots"] not in (3, 5) for row in chains):
            raise PredictionError("不支持的历史账户买入上限")
        targets = {(row["slot"], row["trading_date"]): row["target_slots"] for row in chains}
        capital = self.repo.rows(
            "account_capital_events", "external=1", order="julianday(effective_at),event_id"
        )
        by_slot = {slot: [row for row in capital if row["slot"] == slot] for slot in SLOTS}
        template = None
        for slot, events in by_slot.items():
            if not events:
                raise PredictionError("账户缺少外部本金事件：" + slot)
            for event in events:
                if (
                    not positive(event["amount"])
                    or not event["effective_at"]
                    or local(event["effective_at"]).date().isoformat() != event["trading_date"]
                ):
                    raise PredictionError("外部本金事件无效")
            timeline = [
                (r["event_type"], r["amount"], r["source"], local(r["effective_at"]), r["trading_date"])
                for r in events
            ]
            if template is not None and timeline != template:
                raise PredictionError("24账户外部本金时间线不一致")
            template = timeline
        if len(capital) != sum(len(rows) for rows in by_slot.values()):
            raise PredictionError("外部本金事件具有无效时段")
        states = []
        trades = []
        cashes = {}
        filled = {}
        gaps = []
        for slot in SLOTS:
            candidates = []
            for item in items:
                if item["slot"] not in SLOTS:
                    raise PredictionError("历史推荐具有无效时段")
                if item["slot"] != slot:
                    continue
                run = runs[item["analysis_run_id"]]
                at = max(local(item["signal_at"]), local(item["target_buy_at"]))
                minute = at.replace(second=0, microsecond=0) + (
                    timedelta(minutes=1) if at.second or at.microsecond else timedelta()
                )
                state = {
                    "item": item,
                    "run": run,
                    "at": minute,
                    "status": "analysis_only",
                    "reason": "历史仓位重放后未成交",
                    "failure": "",
                    "blocked": False,
                }
                states.append(state)
                candidates.append(state)
            candidates.sort(
                key=lambda s: (
                    s["at"],
                    local(s["run"]["generated_at"] or s["run"]["persisted_at"] or s["run"]["started_at"]),
                    -s["item"]["final_score"],
                    s["item"]["stock_code"],
                    s["item"]["recommendation_id"],
                )
            )
            cash = 0.0
            capital_index = 0
            ordinal = 0
            positions = []
            active = set()
            bought = set()

            def funding(until, slot=slot):
                nonlocal cash, capital_index
                while (
                    capital_index < len(by_slot[slot])
                    and local(by_slot[slot][capital_index]["effective_at"]) <= until
                ):
                    cash += by_slot[slot][capital_index]["amount"]
                    capital_index += 1

            def make_trade(state, quote, side, quantity, slot=slot) -> dict[str, Any]:
                nonlocal ordinal
                ordinal += 1
                item = state["item"]
                costs = trade_cost(item["stock_code"], quote["price"], quantity, side)
                return dict(
                    trade_id=str(
                        uuid.uuid5(
                            uuid.NAMESPACE_OID,
                            "go-stock:research2:allocation-replay:" + item["recommendation_id"] + ":" + side,
                        )
                    ),
                    slot=slot,
                    recommendation_id=item["recommendation_id"],
                    side=side,
                    traded_at=stamp(quote["at"] + timedelta(milliseconds=ordinal)),
                    quote_at=stamp(quote["quoteAt"]),
                    market_price=quote["price"],
                    quantity=quantity,
                    price_source=quote["source"],
                    execution_mode=MODE,
                    **costs,
                )

            def sells(until, positions=positions, slot=slot, active=active):
                nonlocal cash
                for state in sorted(positions, key=lambda s: (s["sellAt"], s["item"]["recommendation_id"])):
                    if state.get("attempted") or state["sellAt"] > until:
                        continue
                    item = state["item"]
                    target = state["sellAt"]
                    try:
                        quote = self.quote(item, target, True)
                    except QuoteGap as error:
                        old = stored.get(item["recommendation_id"])
                        old_at = local(old["traded_at"]) if old else None
                        if (
                            error.code in ("historical_minute_missing", "historical_quote_unavailable")
                            and old
                            and positive(old["market_price"])
                            and positive(old["execution_price"])
                            and old["quantity"] > 0
                            and old_at >= target
                        ):
                            if old_at > until:
                                continue
                            quote = {
                                "price": old["market_price"],
                                "at": old_at,
                                "quoteAt": local(old["quote_at"] or old["traded_at"]),
                                "source": "stored_trade"
                                + (":" + old["price_source"] if old["price_source"] else ""),
                            }
                        else:
                            state["attempted"] = True
                            state.update(
                                status="sell_pending",
                                reason="历史目标卖出未重放：" + str(error),
                                blocked=True,
                            )
                            gaps.append(
                                {
                                    "recommendationId": item["recommendation_id"],
                                    "slot": slot,
                                    "stockCode": item["stock_code"],
                                    "phase": "sell",
                                    "at": stamp(target),
                                    "reason": str(error),
                                }
                            )
                            continue
                    state["attempted"] = True
                    trade = make_trade(state, quote, "sell", state["buy"]["quantity"])
                    state.update(sell=trade, status="closed", reason="", failure="")
                    trades.append(trade)
                    cash += trade["net_cash_flow"]
                    active.discard(item["stock_code"])

            for state in candidates:
                at = state["at"]
                item = state["item"]
                run = state["run"]
                funding(at)
                sells(at)
                key = (slot, run["trading_date"])
                target = targets.get(key, 3)
                quote = None
                gap = None
                try:
                    quote = self.quote(item, at)
                    state["quote"] = quote
                except QuoteGap as error:
                    gap = error
                if filled.get(key, 0) >= target:
                    state.update(status="standby_not_used", reason="历史重放已完成当日买入上限")
                    continue
                if gap:
                    state.update(status="missed_untradable", reason=str(gap), failure=gap.code)
                    gaps.append(
                        {
                            "recommendationId": item["recommendation_id"],
                            "slot": slot,
                            "stockCode": item["stock_code"],
                            "phase": "buy",
                            "at": stamp(at),
                            "reason": str(gap),
                        }
                    )
                    continue
                if item["stock_code"] in active or (run["trading_date"], item["stock_code"]) in bought:
                    state["reason"] = "历史重放跳过同账户重复股票"
                    continue
                assert quote is not None
                try:
                    quantity, _ = size_buy(
                        item["stock_code"], quote["price"], cash, target - filled.get(key, 0)
                    )
                except PredictionError as error:
                    state.update(status="missed_cash", reason=str(error))
                    continue
                if item["buy_at"] and item["target_sell_at"]:
                    target_sell = local(item["target_sell_at"])
                else:
                    target_sell = next_session(
                        self.market,
                        at,
                        slot
                        if str(run["strategy_version"]).startswith(("research2-slots-", "prediction-slots-"))
                        else "10:00",
                    )
                trade = make_trade(state, quote, "buy", quantity)
                state.update(buy=trade, sellAt=target_sell, status="active", reason="", failure="")
                trades.append(trade)
                cash += trade["net_cash_flow"]
                filled[key] = filled.get(key, 0) + 1
                active.add(item["stock_code"])
                bought.add((run["trading_date"], item["stock_code"]))
                positions.append(state)
            funding(now)
            sells(now)
            if cash < -1e-7:
                raise PredictionError("历史回放账户透支")
            cashes[slot] = cash
        states.sort(key=lambda s: s["item"]["recommendation_id"])
        trades.sort(
            key=lambda t: (local(t["traded_at"]), 0 if t["side"] == "sell" else 1, t["recommendation_id"])
        )
        digest = plan_hash(states, capital, trades)
        return {
            "states": states,
            "capital": capital,
            "trades": trades,
            "cash": cashes,
            "filled": filled,
            "gaps": gaps,
            "hash": digest,
            "replayId": "allocation-" + digest[:40],
            "started": now,
        }

    def run(self, dry_run=True):
        plan = self.build()
        result = {
            "replayId": plan["replayId"],
            "planHash": plan["hash"],
            "policyVersion": POLICY,
            "dryRun": dry_run,
            "reused": False,
            "candidateCount": len(plan["states"]),
            "buyCount": sum(t["side"] == "buy" for t in plan["trades"]),
            "sellCount": sum(t["side"] == "sell" for t in plan["trades"]),
            "missingBuyCount": sum(g["phase"] == "buy" for g in plan["gaps"]),
            "missingSellCount": sum(g["phase"] == "sell" for g in plan["gaps"]),
            "missing": plan["gaps"],
            "accountCash": plan["cash"],
        }
        if dry_run:
            return result
        with self.repo.db.transaction() as connection:
            if one(
                connection,
                "SELECT id FROM research2_allocation_replays WHERE plan_hash=? AND status='complete'",
                (plan["hash"],),
            ):
                result["reused"] = True
            else:
                connection.execute("DELETE FROM research2_trades")
                connection.execute(
                    "DELETE FROM research2_account_capital_events WHERE event_type='legacy_pool_transfer' AND external=0"
                )
                connection.execute(
                    "DELETE FROM research2_account_ledger_snapshots WHERE valuation_basis='capital_ledger_v1'"
                )
                connection.execute("DELETE FROM research2_account_daily_valuations")
                for item in self.repo.rows(
                    "recommendations",
                    "analysis_run_id IN (SELECT run_id FROM research2_analysis_runs "
                    "WHERE status='success' AND published=0)",
                ):
                    self._apply_state(
                        connection,
                        {
                            "item": item,
                            "status": "analysis_only",
                            "reason": "启动区间重归属后仅保留分析",
                            "failure": "",
                            "blocked": False,
                        },
                        plan["replayId"],
                    )
                for state in plan["states"]:
                    self._apply_state(connection, state, plan["replayId"])
                for trade in plan["trades"]:
                    insert(connection, "trades", trade)
                for slot, cash in plan["cash"].items():
                    if (
                        update(
                            connection, "accounts", {"initial_cash": 10000.0, "cash": cash}, "slot=?", (slot,)
                        ).rowcount
                        != 1
                    ):
                        raise PredictionError("回放账户缺失")
                for chain in self.repo.rows("execution_chains"):
                    values = {
                        "allocation_policy": ALLOCATION_POLICY,
                        "filled_slots": plan["filled"].get((chain["slot"], chain["trading_date"]), 0),
                    }
                    if chain["winner_run_id"]:
                        values.update(
                            status="completed",
                            stop_reason="历史动态仓位重放完成",
                            completed_at=chain["completed_at"] or stamp(plan["started"]),
                        )
                    update(connection, "execution_chains", values, "chain_id=?", (chain["chain_id"],))
                self._ledger(connection, plan)
                insert(
                    connection,
                    "allocation_replays",
                    {
                        "replay_id": plan["replayId"],
                        "policy_version": POLICY,
                        "plan_hash": plan["hash"],
                        "status": "complete",
                        "candidate_count": result["candidateCount"],
                        "buy_count": result["buyCount"],
                        "sell_count": result["sellCount"],
                        "missing_buy_count": result["missingBuyCount"],
                        "missing_sell_count": result["missingSellCount"],
                        "summary_json": json_text(result),
                        "started_at": stamp(plan["started"]),
                        "completed_at": stamp(plan["started"]),
                    },
                )
        result["performance"] = HistoryService(self.repo, self.market).backfill()
        return result

    def _apply_state(self, connection, state, replay_id):
        values = {
            "status": state["status"],
            "failure_reason": state["reason"],
            "historical_replay_id": replay_id,
            "historical_sell_blocked": state["blocked"],
            "baseline_value": None,
            "period_pn_l": None,
            "buy_at": None,
            "buy_market_price": 0.0,
            "buy_price": 0.0,
            "quantity": 0,
            "buy_fees": 0.0,
            "current_price": 0.0,
            "current_price_at": None,
            "target_sell_at": None,
            "sell_at": None,
            "sell_market_price": 0.0,
            "sell_price": 0.0,
            "sell_fees": 0.0,
            "net_pn_l": 0.0,
            "net_yield_rate": 0.0,
            "execution_failure_code": state["failure"],
            "execution_quote_price": 0.0,
            "execution_quote_at": None,
            "execution_limit_price": 0.0,
            "execution_limit_distance_pct": None,
            "buy_day_limit_outcome": "",
            "buy_day_limit_status": "pending",
            "buy_day_limit_evaluated_at": None,
            "buy_day_limit_attempt_count": 0,
            "buy_day_limit_source_json": "[]",
            "buy_day_limit_failure_reason": "",
        }
        if quote := state.get("quote"):
            values.update(
                execution_quote_price=quote["price"],
                execution_quote_at=stamp(quote["at"]),
                execution_limit_price=quote["limit"],
                execution_limit_distance_pct=quote["distance"],
            )
        if buy := state.get("buy"):
            values.update(
                buy_at=buy["traded_at"],
                buy_market_price=buy["market_price"],
                buy_price=buy["execution_price"],
                quantity=buy["quantity"],
                buy_fees=buy["commission"] + buy["transfer_fee"],
                current_price=buy["market_price"],
                current_price_at=buy["traded_at"],
                target_sell_at=stamp(state["sellAt"]),
            )
            if sell := state.get("sell"):
                pnl = sell["net_cash_flow"] + buy["net_cash_flow"]
                values.update(
                    sell_at=sell["traded_at"],
                    sell_market_price=sell["market_price"],
                    sell_price=sell["execution_price"],
                    sell_fees=sell["commission"] + sell["stamp_duty"] + sell["transfer_fee"],
                    current_price=sell["market_price"],
                    current_price_at=sell["traded_at"],
                    net_pn_l=pnl,
                    net_yield_rate=pnl / (-buy["net_cash_flow"]),
                )
        update(
            connection,
            "recommendations",
            values,
            "recommendation_id=?",
            (state["item"]["recommendation_id"],),
        )

    def _ledger(self, connection, plan):
        events = [(local(row["effective_at"]), 0, row["event_id"], row) for row in plan["capital"]]
        events.extend(
            (local(row["traded_at"]), 1 if row["side"] == "sell" else 2, row["trade_id"], row)
            for row in plan["trades"]
        )
        events.sort(key=lambda e: (e[0], e[1], e[2]))
        cash = {slot: 0.0 for slot in SLOTS}
        capital = dict(cash)
        positions = {slot: {} for slot in SLOTS}
        stock_codes = {s["item"]["recommendation_id"]: s["item"]["stock_code"] for s in plan["states"]}
        for at, kind, identity, row in events:
            slot = row["slot"]
            if kind == 0:
                cash[slot] += row["amount"]
                capital[slot] += row["amount"]
            else:
                cash[slot] += row["net_cash_flow"]
                if kind == 2:
                    positions[slot][row["recommendation_id"]] = row
                else:
                    positions[slot].pop(row["recommendation_id"], None)
            if cash[slot] < -1e-7:
                raise PredictionError("回放账本发生现金透支")
            value = sum(
                trade_cost(stock_codes[key], trade["market_price"], trade["quantity"], "sell")[
                    "net_cash_flow"
                ]
                for key, trade in positions[slot].items()
            )
            nav = cash[slot] + value
            profit = nav - capital[slot]
            insert(
                connection,
                "account_ledger_snapshots",
                {
                    "snapshot_id": ("capital-" if kind == 0 else "trade-") + identity,
                    "slot": slot,
                    "valued_at": stamp(at),
                    "trading_date": at.date().isoformat(),
                    "snapshot_type": row["event_type"] if kind == 0 else "trade",
                    "cash": cash[slot],
                    "position_value": value,
                    "net_asset_value": nav,
                    "cumulative_external_capital": capital[slot],
                    "net_internal_transfer": 0.0,
                    "net_profit": profit,
                    "cumulative_capital_return": profit / capital[slot] if capital[slot] else 0.0,
                    "valuation_basis": "capital_ledger_v1",
                },
            )
