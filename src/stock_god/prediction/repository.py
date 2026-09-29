"""SQLite account ownership and atomic publication/trade boundaries."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any, Literal, overload

from .core import (
    ALLOCATION_POLICY,
    DEFAULT_SLOT,
    SLOTS,
    STRATEGY_VERSION,
    Conflict,
    NotFound,
    PredictionError,
    dto,
    local,
    slot_at,
    slot_time,
    stamp,
    trade_cost,
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
    valid_slot(slot)
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
            "target_slots": 5,
            "filled_slots": day_count(connection, slot, day),
            "allocation_policy": ALLOCATION_POLICY,
        },
        ignore=True,
    )
    return one(
        connection,
        "SELECT * FROM research2_execution_chains WHERE trading_date=? AND slot=?",
        (day, slot),
        True,
    )


def live_value(item):
    price = item.get("current_price") or item.get("buy_market_price") or item.get("buy_price") or 0
    if price <= 0 or not item.get("quantity"):
        return 0.0
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
        "lastValuedAt": stamp(at),
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
        with self.db.connection() as connection:
            slots = {r[0] for r in connection.execute("SELECT slot FROM research2_accounts")}
        if slots != set(SLOTS):
            raise PredictionError("股票预测必须由迁移初始化24个独立账户")

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
        slot, day = slot_at(scheduled) or DEFAULT_SLOT, local(scheduled).date().isoformat()
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
            run = {
                "run_id": str(uuid.uuid4()),
                "scheduled_slot": slot,
                "slot": slot_at(now),
                "trading_date": day,
                "attempt_no": (runs[0]["attempt_no"] + 1) if runs else 1,
                "scheduled_for": stamp(scheduled),
                "started_at": stamp(now),
                "evidence_cutoff_at": stamp(now),
                "evidence_window_start_at": stamp(
                    now.replace(second=0, microsecond=0) - timedelta(minutes=5)
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
        with self.db.transaction() as connection:
            stored = one(
                connection, "SELECT * FROM research2_analysis_runs WHERE run_id=?", (run["run_id"],), True
            )
            if stored["persisted_at"]:
                return stored
            now = self.clock()  # Sample only after BEGIN IMMEDIATE owns the publication lock.
            started = local(stored["started_at"])
            slot = slot_at(started)
            completed_slot = slot_at(now)
            run.update(
                persisted_at=stamp(now),
                generated_at=stamp(now),
                slot=slot,
                on_time=(
                    slot == run["scheduled_slot"]
                    and completed_slot == slot
                    and started.date() == now.date()
                ),
                published=False,
                chain_id="",
                archive_reason="",
            )
            if run["trigger_source"] == "diagnostic":
                run["archive_reason"] = "链路诊断，仅保留报告，不发布推荐或交易"
            elif (
                not slot
                or not completed_slot
                or started.date().isoformat() != run["trading_date"]
                or local(now).date().isoformat() != run["trading_date"]
            ):
                run["archive_reason"] = "上午窗口外完成，仅保留报告"
            else:
                chain = ensure_chain(connection, slot, now)
                reason = stored["archive_reason"] or (
                    "本区间已有先落盘报告，仅保留报告" if chain["winner_run_id"] else ""
                )
                if not enabled(connection):
                    reason = reason or "自动策略已关闭，仅保留报告"
                run["archive_reason"] = reason
                if not reason:
                    changed = update(
                        connection,
                        "execution_chains",
                        {
                            "winner_run_id": run["run_id"],
                            "latest_run_id": run["run_id"],
                            "root_run_id": run["run_id"],
                            "status": "running",
                            "target_slots": 5,
                            "allocation_policy": ALLOCATION_POLICY,
                        },
                        "chain_id=? AND coalesce(winner_run_id,'')=''",
                        (chain["chain_id"],),
                    ).rowcount
                    if changed != 1:
                        raise Conflict("时段报告已被其他任务发布")
                    run.update(published=True, chain_id=chain["chain_id"], requested_slots=5)
            for item in items:
                item.update(
                    slot=slot,
                    signal_at=stamp(now),
                    target_buy_at=stamp(now),
                    status="buy_pending" if run["published"] else "analysis_only",
                    failure_reason=run["archive_reason"],
                )
            run["report_markdown"] = render(run, items)
            update(
                connection,
                "analysis_runs",
                {k: v for k, v in run.items() if k != "id"},
                "run_id=?",
                (run["run_id"],),
            )
            if run["published"]:
                for item in items:
                    insert(connection, "recommendations", item)
                queue_email(connection, run)
            return dict(run)

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
        from .core import fresh_quote, size_buy

        with self.db.transaction() as connection:
            if not enabled(connection):
                raise Conflict("自动策略已关闭")
            item = one(
                connection,
                "SELECT * FROM research2_recommendations WHERE recommendation_id=? AND status IN ('buy_pending','standby')",
                (identity,),
                True,
            )
            now = self.clock()
            if not slot_at(now) or local(item["signal_at"]).date() != local(now).date():
                raise Conflict("上午买入窗口已截止")
            if current and not fresh_quote(quote.get("asOf"), now):
                raise Conflict("成交行情已过期")
            day = local(now).date().isoformat()
            slot = item["slot"]
            filled = day_count(connection, slot, day)
            if filled >= 5:
                raise Conflict("已完成当日五笔买入")
            duplicate = connection.execute(
                "SELECT 1 FROM research2_recommendations WHERE slot=? AND stock_code=? AND (status IN ('active','sell_pending') OR date(buy_at,'+8 hours')=?)",
                (slot, item["stock_code"], day),
            ).fetchone()
            if duplicate:
                raise Conflict("该股票当日已买入或仍持仓")
            run = one(
                connection,
                "SELECT * FROM research2_analysis_runs WHERE run_id=?",
                (item["analysis_run_id"],),
                True,
            )
            chain = (
                one(
                    connection,
                    "SELECT * FROM research2_execution_chains WHERE chain_id=?",
                    (run["chain_id"],),
                )
                if run["chain_id"]
                else None
            )
            if chain and (
                chain["status"] != "running"
                or not chain["sell_completed_at"]
                or chain["filled_slots"] >= chain["target_slots"]
            ):
                raise Conflict("执行链尚未完成卖出或已结束")
            account = one(connection, "SELECT * FROM research2_accounts WHERE slot=?", (slot,), True)
            quantity, cost = size_buy(
                item["stock_code"],
                quote["price"],
                account["cash"],
                5 - filled,
                chain["allocation_policy"] if chain else ALLOCATION_POLICY,
                chain["allocation_base_cash"] if chain else None,
            )
            trade: dict[str, Any] = dict(
                trade_id=str(uuid.uuid4()),
                recommendation_id=identity,
                slot=slot,
                side="buy",
                traded_at=stamp(quote["asOf"]),
                quote_at=stamp(quote["asOf"]),
                market_price=quote["price"],
                quantity=quantity,
                price_source=quote.get("source", ""),
                execution_mode="live_after_signal",
                **cost,
            )
            update(
                connection,
                "recommendations",
                {
                    "status": "active",
                    "buy_at": trade["traded_at"],
                    "buy_market_price": quote["price"],
                    "buy_price": cost["execution_price"],
                    "quantity": quantity,
                    "buy_fees": cost["commission"] + cost["transfer_fee"],
                    "current_price": quote["price"],
                    "current_price_at": trade["traded_at"],
                    "target_sell_at": stamp(sell_at),
                    "failure_reason": "",
                    "execution_failure_code": "",
                },
                "recommendation_id=?",
                (identity,),
            )
            insert(connection, "trades", trade)
            update(
                connection, "accounts", {"cash": account["cash"] + cost["net_cash_flow"]}, "slot=?", (slot,)
            )
            if chain:
                values = {"filled_slots": filled + 1}
                if filled + 1 >= 5:
                    values.update(
                        status="completed", stop_reason="已完成本区间当日五笔买入", completed_at=stamp(now)
                    )
                update(connection, "execution_chains", values, "chain_id=?", (chain["chain_id"],))
            ledger_snapshot(connection, slot, local(trade["traded_at"]), "trade-" + trade["trade_id"])
            return trade

    def sell(self, identity, quote, at, stale=False, mode="scheduled_slot_sell"):
        with self.db.transaction() as connection:
            item = one(
                connection,
                "SELECT * FROM research2_recommendations WHERE recommendation_id=? AND status IN ('active','sell_pending')",
                (identity,),
            )
            if item is None:
                return None
            cost = trade_cost(item["stock_code"], quote["price"], item["quantity"], "sell")
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
