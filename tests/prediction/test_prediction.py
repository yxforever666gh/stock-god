import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from stock_god.prediction.core import (
    SLOTS,
    LEGACY_SLOTS,
    Conflict,
    PredictionError,
    continuous,
    fresh_quote,
    local,
    size_buy,
    slot_at,
    stamp,
    trade_cost,
    upper_limit,
)
from stock_god.prediction.evidence import prepare, validate
from stock_god.prediction.repository import Repository


def test_clock_exchange_fee_and_remaining_cash_rules():
    assert SLOTS == ("base43",)
    assert len(LEGACY_SLOTS) == 24 and LEGACY_SLOTS[0] == "09:30" and LEGACY_SLOTS[-1] == "11:25"
    assert slot_at(local("2026-09-24T11:29:59+08:00")) == "11:25"
    assert slot_at(local("2026-09-24T11:30:00+08:00")) == ""
    assert continuous(local("2026-09-24T12:00:00+08:00"))  # Preserves the old simulated sell window.
    assert not fresh_quote(None, local())
    assert upper_limit(3.45) == 3.8
    assert trade_cost("sh600001", 10, 100)["net_cash_flow"] == -1005.01
    assert trade_cost("sz000001", 10, 100)["net_cash_flow"] == -1005
    assert trade_cost("sh600001", 10, 100, "sell")["net_cash_flow"] == 994.49
    cash = 10000.0
    quantities = []
    for remaining in range(5, 0, -1):
        quantity, cost = size_buy("sh600001", 10, cash, remaining)
        quantities.append(quantity)
        cash += cost["net_cash_flow"]
        assert cash >= 0
    assert quantities == [100, 200, 200, 200, 200]
    assert size_buy("sh600001", 60, 10000, 5)[0] == 100
    assert size_buy("sz000001", 10, 2005, 5, "legacy_recorded", 20000)[0] == 200


def test_analysis_report_browse_uses_recorded_days_and_pages_without_report_bodies(env):
    dates = ["2026-09-29", "2026-09-28", "2026-09-25", "2026-09-24", "2026-09-23", "2026-09-22"]
    with env.db.transaction() as connection:
        for date in dates:
            for attempt in range(1, 103 if date == dates[0] else 2):
                identity = f"{date}-{attempt}"
                at = date + "T09:50:00+08:00"
                connection.execute(
                    "INSERT INTO research2_analysis_runs "
                    "(run_id,trading_date,attempt_no,scheduled_for,started_at,evidence_cutoff_at,"
                    "status,published,recommendation_count,on_time,report_markdown) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (identity, date, attempt, at, at, at, "success", attempt == 1, 0, 1, "large report"),
                )
        connection.execute(
            "INSERT INTO research2_analysis_runs "
            "(run_id,trading_date,attempt_no,scheduled_for,started_at,evidence_cutoff_at,status) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                "closed-day", "2026-09-26", 1, "2026-09-26T09:50:00+08:00",
                "2026-09-26T09:50:00+08:00", "2026-09-26T09:50:00+08:00",
                "skipped_non_trading_day",
            ),
        )
    recent = env.service.browse_runs()
    assert recent["tradingDates"] == dates
    assert recent["total"] == 106 and len(recent["items"]) == 100
    assert "reportMarkdown" not in recent["items"][0]
    assert len(env.service.browse_runs(2)["items"]) == 6
    assert env.service.browse_runs(1, dates[0])["total"] == 102
    assert env.service.browse_runs(1, "all")["total"] == 107
    assert env.service.browse_runs(1, "recent5", False)["total"] == 5
    assert len(env.service.list_runs(limit=500)) == 107
    with pytest.raises(PredictionError, match="交易日"):
        env.service.browse_runs(1, "2026-09-99")


def test_concurrent_run_claims_have_one_durable_attempt(env):
    env.clock.at = local("2026-09-24T09:29:59+08:00")
    def claim(_):
        return Repository(env.db, env.clock).claim_run(env.clock())[1]

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(claim, range(6)))
    assert sum(results) == 1 and len(env.service.repo.rows("analysis_runs")) == 1


def test_sources_require_verified_time_scope_and_fresh_catalyst(env):
    raw = env.market.collect_prediction_evidence(env.clock(), set(), 1e300)
    evidence = prepare(raw, env.market)
    model = {
        "recommendations": [
            {
                "code": "sh600001",
                "marketScore": 0,
                "sectorScore": 0,
                "stockScore": 1,
                "catalystScore": 1,
                "riskDeduction": 0,
                "sourceRefs": ["quote-sh600001"],
                "referencePrice": 10,
            }
        ]
    }
    rows, warnings = validate("run", evidence, model)
    assert not rows and any("catalyst" in message for message in warnings)
    model["recommendations"][0]["catalystScore"] = 0
    rows, _ = validate("run", evidence, model)
    assert len(rows) == 1 and rows[0]["final_score"] == 1  # No execution score threshold.
    model["recommendations"][0]["sourceRefs"] = ["quote-sh600002"]
    assert not validate("run", evidence, model)[0]
    model["recommendations"][0]["sourceRefs"] = ["quote-sh600001"]
    evidence["documents"][1]["availableAt"] = stamp(env.clock() + timedelta(seconds=1))
    assert not validate("run", evidence, model)[0]


@pytest.mark.asyncio
async def test_unconfigured_auction_blocks_selection_without_ai_or_fake_report(env):
    env.clock.at = local("2026-09-24T09:29:58+08:00")
    with pytest.raises(Conflict, match="竞价"):
        await env.service.analyze()
    assert not env.ai.calls and not env.market.network_calls
    assert not env.service.repo.rows("analysis_runs")
    assert not env.service.repo.rows("recommendations")
    assert not env.service.repo.rows("trades")
    assert env.service.account()["cash"] == 30000


@pytest.mark.asyncio
async def test_unconfigured_scheduler_never_falls_back_to_retired_ai(env):
    env.clock.at = local("2026-09-24T09:29:59+08:00")
    await env.service.tick()
    if env.service._tasks:
        await asyncio.gather(*list(env.service._tasks.values()), return_exceptions=True)
    assert not env.ai.calls
    assert not env.service.repo.rows("analysis_runs")
    assert not env.service.repo.rows("trades")
    await env.service.close()
