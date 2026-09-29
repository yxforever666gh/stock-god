import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from stock_god.prediction.core import (
    SLOTS,
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
    assert len(SLOTS) == 24 and SLOTS[0] == "09:30" and SLOTS[-1] == "11:25"
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


@pytest.mark.asyncio
async def test_end_to_end_frozen_evidence_publish_five_buys_and_next_day_sell(env):
    run = await env.service.analyze()
    assert run["status"] == "success" and run["published"] and run["slot"] == "09:50"
    assert "股票预测" in run["reportMarkdown"] and "分项评分依据" in run["reportMarkdown"]
    assert env.market.last_cash > 1e100
    assert env.service.repo.rows("recommendations")[0]["reference_price"] == 10
    await env.service.process_trades()
    bought = env.service.repo.rows("recommendations", "buy_at IS NOT NULL")
    assert len(bought) == 5 and env.service.account()["cash"] >= 0
    assert all(row["stock_name"] != "model name" for row in bought)
    assert len(env.service.list_recommendations()) == 5
    env.clock.at = local("2026-09-25T09:50:00+08:00")
    env.market.price = 11.0
    await env.service.process_trades()
    assert len(env.service.repo.rows("trades", "side='sell'")) == 5
    assert env.service.performance()["closedTrades"] == 5
    assert env.service.performance()["winningTrades"] == 5
    again = len(env.service.repo.rows("trades"))
    await env.service.process_trades()
    assert len(env.service.repo.rows("trades")) == again


@pytest.mark.asyncio
async def test_non_trading_day_does_not_create_analysis_or_audit_rows(env, monkeypatch):
    env.clock.at = local("2026-09-25T09:50:10+08:00")
    checked = []

    def closed(at):
        checked.append(at)
        return False

    monkeypatch.setattr(env.market, "is_trading_day", closed)
    assert await env.service.analyze() is None
    assert await env.service.analyze() is None
    assert len(checked) == 1
    assert not env.service.repo.rows("analysis_runs")
    with env.db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM research_audit_run_states").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM research_evidence_sets").fetchone()[0] == 0
    assert not env.ai.calls


@pytest.mark.asyncio
async def test_publication_uses_start_slot_and_keeps_late_completion_visible(env):
    async def late():
        env.clock.at = local("2026-09-24T09:55:01+08:00")

    env.ai.hook = late
    run = await env.service.analyze(local("2026-09-24T09:50:00+08:00"))
    assert run["slot"] == "09:50" and run["scheduledSlot"] == "09:50" and not run["onTime"]
    env.ai.hook = None
    second = await env.service.analyze(local("2026-09-24T09:55:00+08:00"))
    assert second["published"] and second["slot"] == "09:55"
    assert len(env.service.repo.rows("recommendations")) == 14


@pytest.mark.asyncio
async def test_first_completed_run_wins_when_different_schedules_start_in_same_slot(env):
    env.clock.at = local("2026-09-24T09:55:01+08:00")
    first = await env.service.analyze(local("2026-09-24T09:50:00+08:00"))
    second = await env.service.analyze(local("2026-09-24T09:55:00+08:00"))
    assert first["published"] and first["slot"] == "09:55" and not first["onTime"]
    assert not second["published"] and "先落盘" in second["archiveReason"]


@pytest.mark.asyncio
async def test_diagnostic_and_late_reports_never_publish(env):
    result = await env.service.analyze(diagnostic=True)
    assert not result["published"] and not env.service.repo.rows("recommendations")
    env.clock.at = local("2026-09-24T11:25:00+08:00")

    async def late():
        env.clock.at = local("2026-09-24T11:30:00+08:00")

    env.ai.hook = late
    result = await env.service.analyze()
    assert result["status"] == "success" and not result["published"]
    assert not env.service.repo.rows("recommendations")


@pytest.mark.asyncio
async def test_disable_then_reenable_cannot_revoke_running_archive_restriction(env):
    async def toggle():
        before = env.settings.load()
        env.settings.config["predictionAutoEnabled"] = False
        env.settings.save()
        env.service.on_settings_changed(before, env.settings.load())
        env.settings.config["predictionAutoEnabled"] = True
        env.settings.save()

    env.ai.hook = toggle
    run = await env.service.analyze()
    assert not run["published"] and "关闭" in run["archiveReason"]
    assert not env.service.repo.rows("recommendations")


@pytest.mark.asyncio
async def test_repairs_once_preserves_attempts_and_never_accepts_unknown_source(env):
    env.ai.responses = ["invalid JSON"]
    run = await env.service.analyze()
    payloads = env.audit.detail(run["runId"])["payloads"]
    assert run["status"] == "success" and len(env.ai.calls) == 2 and len(payloads) == 2
    assert payloads[1]["repairedResponse"]
    assert len(json.loads(run["modelAttemptLogJson"])) == 2


@pytest.mark.asyncio
async def test_quote_pending_reserves_high_rank_seat_without_blocking_other_buys(env):
    await env.service.analyze()
    env.market.fail_quotes.add("sh600001")
    await env.service.process_trades()
    bought = env.service.repo.rows("recommendations", "buy_at IS NOT NULL")
    assert len(bought) == 4
    assert env.service.repo.row("recommendations", "stock_code=?", ("sh600001",))["status"] == "buy_pending"
    env.market.fail_quotes.clear()
    await env.service.process_trades()
    bought = env.service.repo.rows("recommendations", "buy_at IS NOT NULL")
    assert len(bought) == 5 and any(row["stock_code"] == "sh600001" for row in bought)


@pytest.mark.asyncio
async def test_disable_during_quote_rejects_buy_but_selling_uses_stored_mark(env):
    await env.service.analyze()
    changed = False

    def disable(_):
        nonlocal changed
        if changed:
            return
        changed = True
        before = env.settings.load()
        env.settings.config["predictionAutoEnabled"] = False
        env.settings.save()
        env.service.on_settings_changed(before, env.settings.load())

    env.market.quote_hook = disable
    await env.service.process_trades()
    assert not env.service.repo.rows("trades")


def test_concurrent_run_claims_have_one_durable_attempt(env):
    def claim(_):
        return Repository(env.db, env.clock).claim_run(env.clock())[1]

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(claim, range(6)))
    assert sum(results) == 1 and len(env.service.repo.rows("analysis_runs")) == 1


@pytest.mark.asyncio
async def test_missing_quote_timestamp_and_lunch_crossing_cannot_buy(env):
    await env.service.analyze()
    original = env.market.quote
    env.market.quote = lambda code: {**original(code), "asOf": None}
    await env.service.process_trades()
    assert not env.service.repo.rows("trades")
    env.market.quote = original
    env.market.quote_hook = lambda _: setattr(env.clock, "at", local("2026-09-24T11:30:00+08:00"))
    await env.service.process_trades()
    assert not env.service.repo.rows("trades")
    assert all(row["status"] == "analysis_only" for row in env.service.repo.rows("recommendations"))


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
async def test_publication_rolls_back_on_recommendation_insert_failure(env):
    with env.db.transaction() as con:
        con.execute(
            "CREATE TRIGGER fail_recommendation BEFORE INSERT ON research2_recommendations BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
    with pytest.raises(Exception, match="fixture"):
        await env.service.analyze()
    assert not env.service.repo.rows("recommendations")
    assert not env.service.repo.rows("execution_chains")
    assert env.service.repo.rows("analysis_runs")[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_scheduler_retry_pulse_preserves_failed_attempt_history(env):
    env.ai.responses = ["invalid", "invalid"]
    await env.service.tick()
    await asyncio.gather(*list(env.service._tasks.values()), return_exceptions=True)
    first = env.service.repo.rows("analysis_runs")[0]
    assert first["status"] == "failed" and first["attempt_no"] == 1
    env.clock.at = local("2026-09-24T09:51:00+08:00")
    await env.service.tick()
    await asyncio.gather(*list(env.service._tasks.values()), return_exceptions=True)
    rows = env.service.repo.rows("analysis_runs")
    assert len(rows) == 2 and rows[0]["status"] == "failed" and rows[1]["status"] == "success"
    assert rows[1]["attempt_no"] == 2
    await env.service.close()


@pytest.mark.asyncio
async def test_failed_collection_keeps_evidence_and_recovery_closes_orphan_batch(env):
    def failure(cutoff, exclusions, cash):
        error = RuntimeError("fixture collection failed")
        error.evidence = {
            "cutoffAt": stamp(cutoff),
            "freezeAt": stamp(cutoff),
            "documents": [{"sourceId": "failed", "category": "market", "content": "", "error": "timeout"}],
            "candidates": [],
        }
        raise error

    env.market.collect_prediction_evidence = failure
    with pytest.raises(RuntimeError, match="collection"):
        await env.service.analyze()
    failed = env.service.repo.rows("analysis_runs")[0]
    assert failed["evidence_set_id"]
    with env.db.connection() as con:
        assert con.execute("SELECT status FROM research_evidence_sets").fetchone()[0] == "frozen"
        assert con.execute("SELECT status FROM research_evidence_items").fetchone()[0] == "unavailable"
    run, created = env.service.repo.claim_run(env.clock())
    assert created
    batch = env.service.evidence_store.begin(run["run_id"], env.clock())
    env.service.repo.set("analysis_runs", {"evidence_set_id": batch}, "run_id=?", (run["run_id"],))
    env.clock.at = local("2026-09-24T16:00:00+08:00")
    await env.service.recover()
    await env.service.close()
    with env.db.connection() as con:
        assert (
            con.execute(
                "SELECT status FROM research_evidence_sets WHERE evidence_set_id=?", (batch,)
            ).fetchone()[0]
            == "failed"
        )
