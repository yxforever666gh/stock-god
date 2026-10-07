import copy
from datetime import timedelta

import pytest

from stock_god.prediction.core import Conflict, local, stamp, trade_cost
from stock_god.prediction.history import HistoryService, classify_outcome
from stock_god.prediction.replay import AllocationReplay, plan_hash
from stock_god.prediction.repository import insert
from stock_god.prediction.views import Views


def bar(at, price=10, high=None):
    return {
        "time": stamp(at),
        "open": price,
        "high": high or price,
        "low": price,
        "close": price,
        "volume": 100,
        "amount": price * 100,
        "source": "fixture",
    }


def complete_day(at, price=10):
    at = at.replace(hour=9, minute=30, second=0, microsecond=0)
    rows = []
    for minute in range(331):
        current = at + timedelta(minutes=minute)
        if 690 < current.hour * 60 + current.minute < 781:
            continue
        rows.append(bar(current, price))
    return {"previousClose": 10.0, "limitRate": 0.1, "bars": rows, "sourceStatusJson": "[]"}


class PortfolioLedger:
    def __init__(self, trades, now="2026-10-05T15:05:00+08:00"):
        self.trades = trades
        self.now = local(now)

    def clock(self):
        return self.now

    def row(self, table, where, params=()):
        assert table == "accounts"
        return {"archived_at": None}

    def rows(self, table, where="", params=(), order=""):
        if table == "trades":
            return self.trades
        return []


def trade(identity, side, at, quantity=100, slot="09:50", id=1):
    return {
        "id": id,
        "recommendation_id": identity,
        "side": side,
        "traded_at": stamp(local(at)),
        "quantity": quantity,
        "slot": slot,
    }


def test_trading_day_count_uses_trade_intervals_and_calendar_boundaries():
    trades = [
        trade("first", "buy", "2026-09-24T09:50:00+08:00", id=1),
        trade("first", "buy", "2026-09-24T09:51:00+08:00", id=2),
        trade("first", "sell", "2026-09-28T14:50:00+08:00", quantity=200, id=3),
        trade("second", "buy", "2026-09-25T09:50:00+08:00", id=4),
        trade("second", "sell", "2026-09-29T14:50:00+08:00", id=5),
    ]
    days = Views(PortfolioLedger(trades), lambda day: day.weekday() < 5)
    assert days.trading_day_count("09:50") == 4
    assert days.trading_day_count("09:50", "2026-09-25", "2026-09-28") == 2


def test_trading_day_count_handles_open_positions_holidays_and_calendar_failure():
    trades = [trade("open", "buy", "2026-09-30T09:50:00+08:00")]
    holiday = {"2026-10-01"}
    days = Views(PortfolioLedger(trades), lambda day: day.weekday() < 5 and day.date().isoformat() not in holiday)
    assert days.trading_day_count("09:50") == 3
    assert Views(PortfolioLedger([]), lambda day: True).trading_day_count("09:50") == 0

    def unavailable(_):
        raise RuntimeError("calendar unavailable")

    assert Views(PortfolioLedger(trades), unavailable).trading_day_count("09:50") is None


def test_archived_trading_day_count_stops_at_archive_timestamp():
    ledger = PortfolioLedger([trade("open", "buy", "2026-09-24T09:50:00+08:00")])
    ledger.row = lambda *args: {"archived_at": "2026-09-25T16:00:00+08:00"}
    days = Views(ledger, lambda day: day.weekday() < 5)
    assert days.trading_day_count("09:50") == 2


def test_integrated_portfolio_does_not_report_a_combined_trading_day_count():
    result = Views(PortfolioLedger([]), lambda day: True).portfolio(["09:50", "10:00"])
    assert result["tradingDayCount"] is None


def test_outcome_uses_only_complete_minutes_after_buy_and_requires_coverage():
    bought = local("2026-09-24T09:50:10+08:00")
    data = complete_day(bought)
    data["bars"][1]["high"] = 11
    assert classify_outcome({"buy_at": stamp(bought)}, data) == "untouched"
    data["bars"][-1]["high"] = 11
    data["bars"][-1]["close"] = 11
    assert classify_outcome({"buy_at": stamp(bought)}, data) == "sealed"
    data["bars"][-1]["close"] = 10
    assert classify_outcome({"buy_at": stamp(bought)}, data) == "broken"
    data["bars"].pop()
    with pytest.raises(ValueError, match="覆盖"):
        classify_outcome({"buy_at": stamp(bought)}, data)


def seed_chart_position(env):
    """Recorded ledger fixture; does not run any retired strategy or model."""
    at = env.clock()
    cost = trade_cost("sh600001", 10, 100)
    with env.db.transaction() as connection:
        insert(connection, "analysis_runs", {
            "run_id": "chart-run", "slot": "base43", "trading_date": at.date().isoformat(),
            "attempt_no": 1, "scheduled_for": stamp(at), "started_at": stamp(at),
            "evidence_cutoff_at": stamp(at), "status": "success", "published": True,
        })
        insert(connection, "recommendations", {
            "recommendation_id": "chart-position", "analysis_run_id": "chart-run",
            "slot": "base43", "stock_code": "sh600001", "stock_name": "fixture",
            "signal_at": stamp(at), "target_buy_at": stamp(at), "buy_at": stamp(at), "buy_market_price": 10,
            "buy_price": 10, "buy_fees": 5.01, "quantity": 100, "status": "active",
            "current_price": 10, "current_price_at": stamp(at),
        })
        insert(connection, "trades", {
            "trade_id": "chart-buy", "recommendation_id": "chart-position", "slot": "base43",
            "side": "buy", "traded_at": stamp(at), "market_price": 10, "quantity": 100,
            "price_source": "fixture", **cost,
        })
        connection.execute("UPDATE research2_accounts SET cash=cash+? WHERE slot='base43'", (cost["net_cash_flow"],))


def test_full_day_suspension_carries_verified_close_and_resume_uses_actual_close(env):
    seed_chart_position(env)
    days = [local(date) for date in ("2026-09-24", "2026-09-25", "2026-09-28")]
    closes = {"sh600001": {
        "2026-09-24": {"close": 10, "source": "verified-unadjusted"},
        "2026-09-28": {"close": 11, "source": "verified-unadjusted"},
    }}
    history = HistoryService(env.service.repo, env.market, lambda code, day: {
        "known": True, "fullDaySuspended": True, "reference": 1000,
    })
    result = {"valuationsUnavailable": 0, "valuationsCompleted": 0}
    history._valuations("base43", days, env.service.repo.rows("recommendations"), closes, result)
    rows = env.service.repo.rows("account_daily_valuations", order="trading_date")
    assert [row["data_status"] for row in rows] == ["unavailable", "complete", "complete"]
    assert [row["position_value"] for row in rows] == [1000, 1000, 1100]
    assert rows[1]["daily_return"] == 0
    assert rows[2]["net_asset_value"] - rows[1]["net_asset_value"] == 100


@pytest.mark.parametrize("rules", [
    {"known": True, "suspended": True, "fullDaySuspended": False, "reference": 1000},
    {"known": False, "fullDaySuspended": True, "reference": 1000},
    {"known": True, "fullDaySuspended": True, "reference": 950},
])
def test_missing_close_stays_unknown_for_partial_suspension_or_unverified_reference(env, rules):
    seed_chart_position(env)
    days = [local("2026-09-24"), local("2026-09-25")]
    history = HistoryService(env.service.repo, env.market, lambda code, day: rules)
    result = {"valuationsUnavailable": 0, "valuationsCompleted": 0}
    history._valuations("base43", days, env.service.repo.rows("recommendations"), {
        "sh600001": {"2026-09-24": {"close": 10, "source": "verified-unadjusted"}},
    }, result)
    row = env.service.repo.rows("account_daily_valuations", order="trading_date")[-1]
    assert row["data_status"] == "unavailable" and row["daily_return"] is None
    assert "missing unadjusted daily close" in row["failure_reason"]


@pytest.mark.asyncio
async def test_archived_valuation_and_recovery_do_not_mutate_history(env):
    legacy_replay(env)
    before = {table: env.service.repo.rows(table) for table in ("accounts", "recommendations", "trades")}
    await env.service.finalize_metrics()
    await env.service.process_trades()
    await env.service.recover(resume=False)
    assert {table: env.service.repo.rows(table) for table in before} == before
    assert not env.market.network_calls and not env.ai.calls


@pytest.mark.asyncio
async def test_chart_cached_get_never_calls_providers_and_returns_fee_net_pnl(env):
    seed_chart_position(env)
    item = env.service.repo.rows("recommendations", "buy_at IS NOT NULL")[0]
    env.market.bar_rows = [
        bar(env.clock().replace(hour=9, minute=30)),
        bar(env.clock().replace(second=0)),
        bar(env.clock() + timedelta(minutes=1), 11),
    ]
    env.clock.at += timedelta(minutes=2)
    env.market.network_calls = 0
    chart = await env.service.chart(item["recommendation_id"])
    assert env.market.network_calls == 0 and len(chart["bars"]) == 3
    assert (
        chart["bars"][0]["netPnl"] == 0 and chart["bars"][1]["netPnl"] < 0 and chart["bars"][2]["netPnl"] > 0
    )
    assert chart["trades"][0]["markerSnapped"] and chart["trades"][0]["markerAt"]
    await env.service.chart(item["recommendation_id"], True)
    assert env.market.network_calls == 2


@pytest.mark.asyncio
async def test_chart_refresh_requests_holding_range_and_keeps_cached_prior_close(env, monkeypatch):
    seed_chart_position(env)
    item = env.service.repo.rows("recommendations", "buy_at IS NOT NULL")[0]
    rows = [
        bar(local("2026-09-23T15:00:00+08:00"), 9.5),
        bar(local("2026-09-24T09:30:00+08:00"), 10),
        bar(local("2026-09-24T09:50:00+08:00"), 10.5),
    ]
    requested = []

    def select(code, start, end, **kwargs):
        return [row for row in rows if start <= local(row["time"]) <= end]

    def fresh(code, start, end, **kwargs):
        requested.append((start, end))
        return select(code, start, end, **kwargs)

    monkeypatch.setattr(env.market, "bars", fresh)
    monkeypatch.setattr(env.market, "cached_bars", select)
    chart = await env.service.chart(item["recommendation_id"], True)
    assert requested[0][0].date() == local(item["signal_at"]).date()
    assert chart["sessions"][0]["previousClose"] == 9.5
    assert len(chart["bars"]) == 2


async def test_failed_chart_refresh_preserves_real_partial_cache_and_trade_markers(env, monkeypatch):
    seed_chart_position(env)
    item = env.service.repo.rows("recommendations", "buy_at IS NOT NULL")[0]
    env.market.bar_rows = [bar(env.clock().replace(hour=9, minute=30)), bar(env.clock(), 10.5)]
    env.clock.at += timedelta(minutes=2)
    cached = await env.service.chart(item["recommendation_id"])

    def unavailable(*args, **kwargs):
        raise RuntimeError("fixture upstream unavailable")

    monkeypatch.setattr(env.market, "bars", unavailable)
    refreshed = await env.service.chart(item["recommendation_id"], True)
    assert refreshed["bars"] == cached["bars"] and len(refreshed["bars"]) == 2
    assert refreshed["trades"] == cached["trades"]
    assert refreshed["status"] == "partial"
    assert refreshed["providerErrors"][0] == {
        "provider": "minutes",
        "message": "fixture upstream unavailable",
    }
    assert (await env.service.chart(item["recommendation_id"]))["bars"] == cached["bars"]


def legacy_replay(env):
    env.clock.at = local("2026-09-22T16:00:00+08:00")
    signal = local("2026-09-18T10:00:30+08:00")
    buy = signal.replace(minute=1, second=0)
    sell = local("2026-09-21T10:00:00+08:00")
    with env.db.transaction() as con:
        insert(
            con,
            "analysis_runs",
            {
                "run_id": "legacy",
                "trading_date": "2026-09-18",
                "scheduled_slot": "10:00",
                "slot": "10:00",
                "published": True,
                "attempt_no": 1,
                "scheduled_for": stamp(signal),
                "started_at": stamp(signal),
                "generated_at": stamp(signal),
                "evidence_cutoff_at": stamp(signal),
                "status": "success",
                "strategy_version": "research2-trailing5-v10",
            },
        )
        insert(
            con,
            "execution_chains",
            {
                "chain_id": "chain",
                "slot": "10:00",
                "trading_date": "2026-09-18",
                "winner_run_id": "legacy",
                "scheduled_for": stamp(signal),
                "started_at": stamp(signal),
                "status": "completed",
                "target_slots": 3,
            },
        )
        for index, price in enumerate([60.0, 10.0, 100.0, 5.0], 1):
            stock_code = f"sh60000{index}"
            insert(
                con,
                "recommendations",
                {
                    "recommendation_id": f"legacy-{index}",
                    "analysis_run_id": "legacy",
                    "slot": "10:00",
                    "stock_code": stock_code,
                    "stock_name": stock_code,
                    "signal_at": stamp(signal),
                    "target_buy_at": stamp(signal),
                    "final_score": 90 - index,
                    "selection_rank": index,
                    "status": "analysis_only",
                },
            )
            env.market.history[(stock_code, "2026-09-18")] = {
                "previousClose": price,
                "limitRate": 0.1,
                "bars": [bar(buy, price)],
            }
            if index != 2:
                env.market.history[(stock_code, "2026-09-21")] = {
                    "previousClose": price,
                    "limitRate": 0.1,
                    "bars": [bar(sell, price * 1.02)],
                }
    return buy, sell


def test_replay_retains_legacy_three_buys_blocks_missing_sell_and_rebuilds_ledger(env):
    legacy_replay(env)
    replay = AllocationReplay(env.service.repo, env.market)
    dry = replay.run(True)
    assert (dry["buyCount"], dry["sellCount"], dry["missingSellCount"]) == (3, 2, 1)
    assert not env.service.repo.rows("trades")
    before = {table: env.service.repo.rows(table) for table in ("accounts", "recommendations", "trades")}
    with pytest.raises(Conflict, match="归档"):
        replay.run(False)
    assert {table: env.service.repo.rows(table) for table in before} == before


def test_replay_rejects_capital_timeline_drift_and_fee_hash_changes(env):
    legacy_replay(env)
    replay = AllocationReplay(env.service.repo, env.market)
    plan = replay.build()
    changed = copy.deepcopy(plan["trades"])
    changed[0]["commission"] += 1
    assert plan_hash(plan["states"], plan["capital"], changed) != plan["hash"]
    with env.db.transaction() as con:
        con.execute("UPDATE research2_account_capital_events SET amount=10001 WHERE slot='10:00'")
    with pytest.raises(ValueError, match="时间线"):
        replay.build()


def test_allocation_hash_matches_temporary_go_encoding_oracle():
    # Captured from a standalone Go encoding/json oracle using the released struct field order.
    trade = {
        "trade_id": "t",
        "side": "buy",
        "traded_at": "2026-09-24T09:51:00.001000+08:00",
        "execution_price": 10.0,
        "market_price": 10.0,
        "quantity": 100,
        "price_source": "fixture<&>",
        "commission": 5.0,
        "stamp_duty": 0.0,
        "transfer_fee": 0.01,
        "slippage_amount": 0.0,
        "net_cash_flow": -1005.01,
    }
    states = [
        {
            "item": {"recommendation_id": "r"},
            "status": "active",
            "failure": "",
            "blocked": False,
            "buy": trade,
        }
    ]
    capital = [
        {"event_id": "initial", "slot": "09:50", "effective_at": "2026-08-27T01:00:00Z", "amount": 10000.0}
    ]
    assert (
        plan_hash(states, capital, [trade])
        == "78ce0c31752687f8590f3547dda97f0b75a4e29a56a77828c13a8b287604000d"
    )
