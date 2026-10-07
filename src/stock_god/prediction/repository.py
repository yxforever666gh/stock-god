"""SQLite account ownership and atomic publication/trade boundaries."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any, Literal, overload

from .core import (
    DEFAULT_SLOT,
    STRATEGY_VERSION,
    Conflict,
    NotFound,
    PredictionError,
    dto,
    local,
    slot_time,
    stamp,
    trade_cost,
    base43_trade_cost,
    valid_slot,
)

TABLES = {
    name: "research2_" + name
    for name in (
        "analysis_runs",
        "recommendations",
        "trades",
        "accounts",
        "execution_chains",
        "email_deliveries",
        "account_snapshots",
        "account_capital_events",
        "account_ledger_snapshots",
        "account_daily_valuations",
        "allocation_replays",
    )
}
NULL_FIELDS = {
    "baseline_value",
    "period_pn_l",
    "evidence_coverage_pct",
    "degraded",
    "execution_limit_distance_pct",
    "allocation_base_cash",
    "daily_return",
}


@overload
def one(connection, sql, params, required: Literal[True]) -> dict[str, Any]: ...
@overload
def one(connection, sql, params=(), required: Literal[False] = False) -> dict[str, Any] | None: ...
def one(connection, sql, params=(), required=False) -> dict[str, Any] | None:
    row = connection.execute(sql, params).fetchone()
    if row is None and required:
        raise NotFound("记录不存在")
    return dict(row) if row is not None else None


def many(connection, sql, params=()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, params)]


def insert(connection, table, values, *, ignore=False):
    table = TABLES[table]
    columns = connection.execute(f"PRAGMA table_info({table})").fetchall()
    if not columns:
        raise PredictionError(f"预测数据库缺少 {table}，请先运行迁移")
    row = {}
    for column in columns:
        key, kind, default = column["name"], column["type"].upper(), column["dflt_value"]
        if key in values:
            row[key] = values[key]
        elif key == "id" or default is not None:
            continue
        elif key in ("created_at", "updated_at"):
            row[key] = stamp(local())
        elif key in NULL_FIELDS or kind == "DATETIME":
            row[key] = None
        else:
            row[key] = "" if kind == "TEXT" else 0
    keys = list(row)
    return connection.execute(
        f"INSERT {'OR IGNORE ' if ignore else ''}INTO {table} ({','.join(keys)}) "
        f"VALUES ({','.join('?' for _ in keys)})",
        list(row.values()),
    )


def update(connection, table, values, where, params=()):
    if table in {"recommendations", "accounts", "execution_chains", "account_ledger_snapshots", "account_daily_valuations"}:
        for item in many(connection, f"SELECT slot FROM {TABLES[table]} WHERE {where}", params):
            assert_active(connection, item["slot"])
    return connection.execute(
        f"UPDATE {TABLES[table]} SET {','.join(k + '=?' for k in values)} WHERE {where}",
        [*values.values(), *params],
    )


def enabled(connection, fallback=True):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='research_settings'").fetchone():
        return fallback
    row = connection.execute("SELECT config_json FROM research_settings WHERE center='research2'").fetchone()
    if row is None:
        return fallback
    config = json.loads(row[0])
    return config.get("predictionAutoEnabled", config.get("research2AutoEnabled", True)) is not False


def day_count(connection, slot, day):
    return connection.execute(
        "SELECT count(*) FROM research2_recommendations WHERE slot=? AND date(buy_at,'+8 hours')=?",
        (slot, day),
    ).fetchone()[0]


def ensure_chain(connection, slot, now):
    assert_active(connection, valid_slot(slot))
    day = local(now).date().isoformat()
    chain_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"go-stock:research2:execution-chain:{day}:{slot}"))
    insert(
        connection,
        "execution_chains",
        {
            "chain_id": chain_id,
            "slot": slot,
            "trading_date": day,
            "scheduled_for": stamp(slot_time(now, slot)),
            "started_at": stamp(now),
            "status": "running",
            "target_slots": 2,
            "filled_slots": day_count(connection, slot, day),
            "allocation_policy": "base43_frozen_equal",
        },
        ignore=True,
    )
    return one(
        connection,
        "SELECT * FROM research2_execution_chains WHERE trading_date=? AND slot=?",
        (day, slot),
        True,
    )


def assert_active(connection, slot):
    account = one(connection, "SELECT * FROM research2_accounts WHERE slot=?", (slot,), True)
    if slot != DEFAULT_SLOT or account.get("archived_at"):
        raise Conflict("旧预测账户已归档，只读")


def validate_quote(quote, now, *, buy):
    from .core import parse_time, positive
    now = local(now)
    at = parse_time(quote.get("asOf"))
    received = parse_time(quote.get("receivedAt")) or now
    if (at is None or at.date() != now.date() or at > received or received > now
            or not 0 <= (now - at).total_seconds() <= 60 or not quote.get("source")
            or not positive(quote.get("price")) or quote.get("suspended") is not False):
        raise Conflict("缺少合格实时报价或停牌资格证据")
    if buy:
        cutoff = now.replace(hour=9, minute=31, second=0, microsecond=0)
        opening = cutoff.replace(minute=30)
        if not opening <= at <= now < cutoff:
            raise Conflict("买入窗口已截止")
        limit = quote.get("upperLimit")
        if not positive(limit) or quote["price"] >= limit:
            raise Conflict("涨停价未知或不可买入")
    else:
        limit = quote.get("lowerLimit")
        minute = now.hour * 60 + now.minute
        if not (570 <= minute < 690 or 780 <= minute < 900):
            raise Conflict("连续交易窗口外")
        if not positive(limit) or quote["price"] <= limit:
            raise Conflict("跌停价未知或不可卖出")


def live_value(item):
    price = item.get("current_price") or item.get("buy_market_price") or item.get("buy_price") or 0
    if price <= 0 or not item.get("quantity"):
        return 0.0
    if item.get("slot") == DEFAULT_SLOT:
        return price * item["quantity"]
    return trade_cost(item["stock_code"], price, item["quantity"], "sell")["net_cash_flow"]


def recommendation_dto(item):
    item = dict(item)
    if item["status"] in ("active", "sell_pending"):
        cost = (item.get("buy_price") or 0) * (item.get("quantity") or 0) + (item.get("buy_fees") or 0)
        if cost > 0:
            item["net_pn_l"] = live_value(item) - cost
            item["net_yield_rate"] = item["net_pn_l"] / cost
    return dto(item)


def overview(connection, slot, at):
    account = one(connection, "SELECT * FROM research2_accounts WHERE slot=?", (valid_slot(slot),), True)
    active = many(
        connection,
        "SELECT * FROM research2_recommendations WHERE slot=? AND status IN ('active','sell_pending')",
        (slot,),
    )
    pending = connection.execute(
        "SELECT count(*) FROM research2_recommendations WHERE slot=? AND status='buy_pending'", (slot,)
    ).fetchone()[0]
    capital = many(
        connection,
        "SELECT * FROM research2_account_capital_events WHERE slot=? ORDER BY julianday(effective_at),id",
        (slot,),
    )
    external = sum(r["amount"] for r in capital if r["external"])
    transfer = sum(r["amount"] for r in capital if not r["external"])
    if not capital or external <= 0:
        raise PredictionError("股票预测账户缺少完整本金账本")
    position = sum(live_value(item) for item in active)
    nav = account["cash"] + position
    profit = nav - external - transfer
    return {
        "slot": slot,
        "baselineAt": stamp(account["baseline_at"]),
        "baselineNetAssetValue": external,
        "initialCash": account["initial_cash"],
        "cash": account["cash"],
        "positionValue": position,
        "netAssetValue": nav,
        "netProfit": profit,
        "returnRate": profit / external,
        "openPositions": len(active),
        "pendingBuys": pending,
        "lastValuedAt": stamp(account.get("archived_at") or at),
        "archivedAt": stamp(account.get("archived_at")),
        "initialContribution": sum(
            r["amount"] for r in capital if r["external"] and r["event_type"] == "initial_external"
        ),
        "topUpContribution": sum(
            r["amount"] for r in capital if r["external"] and r["event_type"] == "top_up_external"
        ),
        "cumulativeExternalCapital": external,
        "netInternalTransfer": transfer,
        "cumulativeCapitalReturn": profit / external,
        "valuationBasis": "capital_ledger_v1",
    }


def ledger_snapshot(connection, slot, at, identity, kind="trade"):
    assert_active(connection, slot)
    value = overview(connection, slot, at)
    row = {
        "snapshot_id": identity,
        "slot": slot,
        "valued_at": stamp(at),
        "trading_date": local(at).date().isoformat(),
        "snapshot_type": kind,
        "cash": value["cash"],
        "position_value": value["positionValue"],
        "net_asset_value": value["netAssetValue"],
        "cumulative_external_capital": value["cumulativeExternalCapital"],
        "net_internal_transfer": value["netInternalTransfer"],
        "net_profit": value["netProfit"],
        "cumulative_capital_return": value["cumulativeCapitalReturn"],
        "valuation_basis": value["valuationBasis"],
    }
    insert(connection, "account_ledger_snapshots", row, ignore=True)


class Repository:
    def __init__(self, database, clock=local):
        self.db, self.clock = database, clock

    def ready(self):
        now = self.clock()
        with self.db.transaction() as connection:
            insert(connection, "accounts", {
                "slot": DEFAULT_SLOT, "initial_cash": 30000.0, "cash": 30000.0,
                "baseline_at": stamp(now), "baseline_net_asset_value": 30000.0, "seed_cash": 30000.0,
            }, ignore=True)
            account = one(connection, "SELECT * FROM research2_accounts WHERE slot=?", (DEFAULT_SLOT,), True)
            insert(connection, "account_capital_events", {
                "event_id": "base43-initial-external", "slot": DEFAULT_SLOT,
                "event_type": "initial_external", "amount": 30000.0, "external": True,
                "source": "base43_initial_capital", "effective_at": account["baseline_at"] or stamp(now),
                "trading_date": local(account["baseline_at"] or now).date().isoformat(),
            }, ignore=True)
            ledger_snapshot(connection, DEFAULT_SLOT, account["baseline_at"] or now,
                            "base43-initial-external", "initial_external")

    def rows(self, table, where="1", params=(), order="id ASC"):
        with self.db.connection() as connection:
            return many(connection, f"SELECT * FROM {TABLES[table]} WHERE {where} ORDER BY {order}", params)

    def row(self, table, where, params=()):
        rows = self.rows(table, where, params)
        if not rows:
            raise NotFound("记录不存在")
        return rows[0]

    def set(self, table, values, where, params=()):
        with self.db.transaction() as connection:
            return update(connection, table, values, where, params).rowcount

    def claim_run(self, scheduled, trigger="scheduled", parent=""):
        now = self.clock()
        slot, day = DEFAULT_SLOT, local(scheduled).date().isoformat()
        with self.db.transaction() as connection:
            runs = many(
                connection,
                "SELECT * FROM research2_analysis_runs WHERE trading_date=? AND scheduled_slot=? ORDER BY attempt_no DESC,id DESC",
                (day, slot),
            )
            effective = next(
                (r for r in runs if r["status"] in ("success", "no_recommendation", "running")), None
            )
            if effective:
                if parent:
                    raise Conflict("已有完成或运行中的分析，不能重跑")
                return effective, False
            if parent and (not runs or runs[0]["run_id"] != parent or runs[0]["status"] != "failed"):
                raise Conflict("只允许重试最后一份失败报告")
            if runs and runs[0]["status"] != "failed":
                return runs[0], False
            now = local(self.clock())
            cutoff = local(scheduled).replace(hour=9, minute=29, second=59, microsecond=0)
            if now > cutoff or now.date().isoformat() != day:
                raise Conflict("错过盘前冻结时点，禁止创建当日买入名单")
            chain = ensure_chain(connection, DEFAULT_SLOT, now)
            if chain["allocation_base_cash"] is None:
                account = one(connection, "SELECT * FROM research2_accounts WHERE slot=?", (DEFAULT_SLOT,), True)
                update(connection, "execution_chains", {"allocation_base_cash": account["cash"],
                    "allocation_policy": "base43_frozen_equal"}, "chain_id=?", (chain["chain_id"],))
            run = {
                "run_id": str(uuid.uuid4()),
                "scheduled_slot": slot,
                "slot": DEFAULT_SLOT,
                "trading_date": day,
                "attempt_no": (runs[0]["attempt_no"] + 1) if runs else 1,
                "scheduled_for": stamp(scheduled),
                "started_at": stamp(now),
                "evidence_cutoff_at": stamp(now),
                "evidence_window_start_at": stamp(
                    now.replace(hour=9, minute=15, second=0, microsecond=0)
                ),
                "status": "running",
                "strategy_version": STRATEGY_VERSION,
                "trigger_source": trigger,
                "parent_run_id": parent,
                "created_at": stamp(now),
                "updated_at": stamp(now),
            }
            insert(connection, "analysis_runs", run)
            return one(
                connection, "SELECT * FROM research2_analysis_runs WHERE run_id=?", (run["run_id"],), True
            ), True

    def publish(self, run, items, render, queue_email):
        return self.publish_base43(run, items, render, queue_email)

    def mark_pending(self, identity, reason, quote=None):
        values = {"failure_reason": reason, "execution_failure_code": "quote_retry"}
        if quote:
            values.update(
                execution_quote_price=quote.get("price", 0), execution_quote_at=stamp(quote.get("asOf"))
            )
        self.set(
            "recommendations",
            values,
            "recommendation_id=? AND status IN ('buy_pending','standby')",
            (identity,),
        )

    def buy(self, identity, quote, sell_at, *, current=True):
        return self.buy_base43(identity, quote, sell_at)

    def sell(self, identity, quote, at, stale=False, mode="scheduled_slot_sell"):
        with self.db.transaction() as connection:
            item = one(
                connection,
                "SELECT * FROM research2_recommendations WHERE recommendation_id=? AND status IN ('active','sell_pending')",
                (identity,),
            )
            if item is None:
                return None
            assert_active(connection, item["slot"])
            validate_quote(quote, at, buy=False)
            if local(item["buy_at"]).date() >= local(at).date():
                raise Conflict("T+1 未到")
            if item.get("target_sell_at") and local(at) < local(item["target_sell_at"]):
                raise Conflict("尚未到退出时间")
            cost = base43_trade_cost(item["stock_code"], quote["price"], item["quantity"], "sell")
            paid = item["buy_price"] * item["quantity"] + item["buy_fees"]
            profit = cost["net_cash_flow"] - paid
            values = {
                "status": "closed",
                "sell_at": stamp(at),
                "sell_market_price": quote["price"],
                "sell_price": cost["execution_price"],
                "sell_fees": cost["commission"] + cost["stamp_duty"] + cost["transfer_fee"],
                "current_price": quote["price"],
                "current_price_at": stamp(at),
                "net_pn_l": profit,
                "net_yield_rate": profit / paid if paid else 0,
                "failure_reason": "",
            }
            if item["baseline_value"] is not None:
                values["period_pn_l"] = cost["net_cash_flow"] - item["baseline_value"]
            update(connection, "recommendations", values, "recommendation_id=?", (identity,))
            trade: dict[str, Any] = dict(
                trade_id=str(uuid.uuid4()),
                recommendation_id=identity,
                slot=item["slot"],
                side="sell",
                traded_at=stamp(at),
                quote_at=stamp(quote.get("asOf")),
                price_stale=stale,
                market_price=quote["price"],
                quantity=item["quantity"],
                price_source=quote.get("source", ""),
                execution_mode=mode,
                **cost,
            )
            insert(connection, "trades", trade)
            connection.execute(
                "UPDATE research2_accounts SET cash=cash+? WHERE slot=?",
                (cost["net_cash_flow"], item["slot"]),
            )
            ledger_snapshot(connection, item["slot"], at, "trade-" + trade["trade_id"])
            return trade

    def finish_chain(self, chain_id):
        with self.db.transaction() as connection:
            chain = one(
                connection, "SELECT * FROM research2_execution_chains WHERE chain_id=?", (chain_id,), True
            )
            assert_active(connection, chain["slot"])
            count = day_count(connection, chain["slot"], chain["trading_date"])
            pending = connection.execute(
                "SELECT count(*) FROM research2_recommendations r JOIN research2_analysis_runs a ON a.run_id=r.analysis_run_id WHERE a.chain_id=? AND r.status IN ('buy_pending','standby')",
                (chain_id,),
            ).fetchone()[0]
            values = {"filled_slots": count}
            if (
                chain["status"] == "running"
                and chain["winner_run_id"]
                and (count >= chain["target_slots"] or pending == 0)
            ):
                values.update(
                    status="completed",
                    completed_at=stamp(self.clock()),
                    stop_reason="当日有效报告的执行名单已处理完毕",
                )
                connection.execute(
                    "UPDATE research2_recommendations SET status='analysis_only',failure_reason='当日执行已结束，剩余评分仅保留分析' WHERE analysis_run_id=? AND status IN ('buy_pending','standby')",
                    (chain["winner_run_id"],),
                )
            update(connection, "execution_chains", values, "chain_id=?", (chain_id,))

    def publish_base43(self, run, candidates, render=None, queue_email=None):
        from .core import code, positive
        with self.db.transaction() as connection:
            assert_active(connection, DEFAULT_SLOT)
            stored = one(connection, "SELECT * FROM research2_analysis_runs WHERE run_id=?", (run["run_id"],), True)
            if stored["scheduled_slot"] != DEFAULT_SLOT:
                raise Conflict("旧预测报告已归档，只读")
            if stored["persisted_at"]:
                return stored
            now = local(self.clock())
            cutoff = now.replace(hour=9, minute=29, second=59, microsecond=0)
            if (now >= cutoff.replace(hour=9, minute=31, second=0)
                    or now.date().isoformat() != stored["trading_date"]
                    or local(stored["started_at"]) > cutoff
                    or not stored["evidence_cutoff_at"]
                    or local(stored["evidence_cutoff_at"]) > min(cutoff, now)):
                raise Conflict("错过盘前冻结时点，禁止补发买单")
            if not enabled(connection):
                raise Conflict("自动策略已关闭")
            # Select first, freeze equal budgets once; failed seats are never replaced.
            chosen = sorted((dict(c) for c in candidates if positive(c.get("final_score", c.get("score")))),
                            key=lambda c: (-c.get("final_score", c.get("score")), code(c["stock_code"])))[:2]
            if len({code(c["stock_code"]) for c in chosen}) != len(chosen):
                raise Conflict("候选代码重复")
            chain = one(connection, "SELECT * FROM research2_execution_chains WHERE slot=? AND trading_date=?",
                        (DEFAULT_SLOT, stored["trading_date"]), True)
            if chain["allocation_base_cash"] is None or local(chain["started_at"]) > cutoff:
                raise Conflict("没有盘前冻结预算，禁止借用当日卖款")
            if chain["winner_run_id"]:
                raise Conflict("当日已有冻结名单")
            update(connection, "execution_chains", {
                "winner_run_id": run["run_id"], "latest_run_id": run["run_id"], "root_run_id": run["run_id"],
                "target_slots": len(chosen),
                "allocation_policy": "base43_frozen_equal", "status": "running" if chosen else "completed",
            }, "chain_id=?", (chain["chain_id"],))
            budget = chain["allocation_base_cash"] / len(chosen) if chosen else 0
            for rank, item in enumerate(chosen, 1):
                item.update(recommendation_id=item.get("recommendation_id") or str(uuid.uuid4()),
                    analysis_run_id=run["run_id"], stock_code=code(item["stock_code"]), slot=DEFAULT_SLOT,
                    final_score=item.get("final_score", item.get("score")), status="buy_pending",
                    signal_at=stamp(local(stored["evidence_cutoff_at"])), target_buy_at=stamp(now.replace(hour=9, minute=30, second=0, microsecond=0)),
                    allocation_base_cash=budget, allocation_policy="base43_frozen_equal", selection_rank=rank, selection_role="primary")
                item.pop("score", None)
                insert(connection, "recommendations", item)
            values = {"status": "success" if chosen else "no_recommendation", "slot": DEFAULT_SLOT,
                "published": True, "on_time": True, "persisted_at": stamp(now), "generated_at": stamp(now),
                "chain_id": chain["chain_id"], "recommendation_count": len(chosen), "requested_slots": len(chosen),
                "strategy_version": STRATEGY_VERSION}
            run.update(values)
            values["report_markdown"] = render(run, chosen) if render else "BASE43 固定前两名；模拟账户收益以实际报价成交账本为准。"
            run["report_markdown"] = values["report_markdown"]
            update(connection, "analysis_runs", values, "run_id=?", (run["run_id"],))
            if queue_email:
                queue_email(connection, run)
            return one(connection, "SELECT * FROM research2_analysis_runs WHERE run_id=?", (run["run_id"],), True)

    def buy_base43(self, identity, quote, sell_at, now=None):
        import math
        with self.db.transaction() as connection:
            item = one(connection, "SELECT * FROM research2_recommendations WHERE recommendation_id=?", (identity,), True)
            assert_active(connection, item["slot"])
            if item["status"] == "active":
                return one(connection, "SELECT * FROM research2_trades WHERE recommendation_id=? AND side='buy'", (identity,), True)
            if item["status"] != "buy_pending" or not enabled(connection):
                raise Conflict("买入席位不可执行")
            now = local(now or self.clock())
            if local(item["signal_at"]).date() != now.date():
                raise Conflict("买入席位已过期")
            validate_quote(quote, now, buy=True)
            duplicate = connection.execute(
                "SELECT 1 FROM research2_recommendations WHERE slot=? AND stock_code=? "
                "AND recommendation_id<>? AND (status IN ('active','sell_pending') OR date(buy_at,'+8 hours')=?)",
                (DEFAULT_SLOT, item["stock_code"], identity, now.date().isoformat()),
            ).fetchone()
            if duplicate:
                raise Conflict("该股票当日已买入或仍持仓；冻结席位不补排")
            account = one(connection, "SELECT * FROM research2_accounts WHERE slot=?", (DEFAULT_SLOT,), True)
            cap = min(account["cash"], item["allocation_base_cash"] or 0)
            quantity = math.floor(cap / quote["price"] / 100) * 100
            while quantity > 0:
                cost = base43_trade_cost(item["stock_code"], quote["price"], quantity)
                if -cost["net_cash_flow"] <= cap + 1e-8:
                    break
                quantity -= 100
            if quantity <= 0:
                raise Conflict("冻结席位预算不足支付一手含费成本")
            trade = dict(trade_id=str(uuid.uuid4()), recommendation_id=identity, slot=DEFAULT_SLOT, side="buy",
                traded_at=stamp(now), quote_at=stamp(quote["asOf"]), market_price=quote["price"], quantity=quantity,
                price_source=quote["source"], execution_mode="base43_live_quote", **cost)
            update(connection, "recommendations", {"status": "active", "buy_at": stamp(now),
                "buy_market_price": quote["price"], "buy_price": quote["price"], "quantity": quantity,
                "buy_fees": cost["commission"] + cost["transfer_fee"], "current_price": quote["price"],
                "current_price_at": stamp(quote["asOf"]), "target_sell_at": stamp(sell_at),
                "failure_reason": "", "execution_failure_code": ""}, "recommendation_id=?", (identity,))
            insert(connection, "trades", trade)
            update(connection, "accounts", {"cash": account["cash"] + cost["net_cash_flow"]}, "slot=?", (DEFAULT_SLOT,))
            run = one(connection, "SELECT * FROM research2_analysis_runs WHERE run_id=?", (item["analysis_run_id"],), True)
            update(connection, "execution_chains", {"filled_slots": day_count(connection, DEFAULT_SLOT, now.date().isoformat())}, "chain_id=?", (run["chain_id"],))
            ledger_snapshot(connection, DEFAULT_SLOT, now, "trade-" + trade["trade_id"])
            return trade

    def sell_base43(self, identity, quote, at):
        return self.sell(identity, quote, at, mode="base43_live_quote")
