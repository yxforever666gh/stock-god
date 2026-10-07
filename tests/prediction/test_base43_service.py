"""Safety gates in the active orchestration; legacy reads never acquire providers."""

from types import SimpleNamespace

import pytest

from stock_god.prediction.core import Conflict, local


class Source:
    def __init__(self, ready=False):
        self.ready = ready

    def with_settings(self, config):
        return self

    def status(self):
        return {"ready": self.ready, "configured": True, "status": "unverified"}


@pytest.mark.asyncio
async def test_unverified_source_does_not_make_fake_report(env):
    env.clock.at = local("2026-09-24T09:29:58+08:00")
    env.service.auction_source = Source()
    with pytest.raises(Conflict, match="竞价"):
        await env.service.analyze()
    assert env.service.repo.rows("analysis_runs") == []
    assert env.market.network_calls == 0
    assert env.service._task_done("2026-09-24", "freeze")


@pytest.mark.asyncio
async def test_old_valuation_does_not_create_provider(env):
    await env.service.refresh_quotes([{"slot": "09:30", "status": "active"}])
    assert env.market.settings == []


@pytest.mark.asyncio
async def test_rerun_and_replay_rejected_before_provider(env):
    with pytest.raises(Conflict):
        await env.service.rerun("archived")
    with pytest.raises(Conflict):
        await env.service.replay_allocations(False)
    assert env.market.settings == []


@pytest.mark.asyncio
async def test_late_start_cannot_freeze_budget(env):
    env.clock.at = local("2026-09-24T09:30:00+08:00")
    env.service.auction_source = Source(True)
    with pytest.raises(Conflict):
        await env.service.analyze()
    assert env.service.repo.rows("analysis_runs") == []
    assert env.service.repo.rows("execution_chains") == []


@pytest.mark.asyncio
async def test_recovery_never_changes_archived_run(env):
    from stock_god.prediction.repository import insert

    with env.db.transaction() as con:
        insert(
            con,
            "analysis_runs",
            {
                "run_id": "old",
                "scheduled_slot": "09:30",
                "slot": "09:30",
                "trading_date": "2026-09-24",
                "status": "running",
                "scheduled_for": "2026-09-24T09:30:00+08:00",
                "started_at": "2026-09-24T09:30:00+08:00",
            },
        )
    env.service.models = SimpleNamespace(bootstrap=lambda: None, health=lambda: {"ready": True})
    await env.service.recover(resume=False)
    assert env.service.repo.row("analysis_runs", "run_id=?", ("old",))["status"] == "running"


@pytest.mark.asyncio
async def test_unconfigured_source_blocks_trades_without_old_ai(env):
    env.clock.at = local("2026-09-24T09:30:01+08:00")
    await env.service.process_trades()
    assert env.market.network_calls == 0


@pytest.mark.asyncio
async def test_freeze_publishes_once_and_records_every_candidate(env, monkeypatch):
    import numpy as np

    import stock_god.prediction.service as module

    env.clock.at = local("2026-09-24T09:29:58+08:00")

    class CompleteSource(Source):
        def freeze(self, day, cutoff):
            return {
                "complete": True,
                "factsSha256": "facts",
                "candidates": [
                    {
                        "code": code,
                        "name": name,
                        "upper": 1100,
                        "auctionRows": [
                            {
                                "time": 33900,
                                "receivedAt": "2026-09-24T09:25:00+08:00",
                                "fields": [10, 10, 100000, 1000000] + [None] * 13,
                            }
                        ],
                    }
                    for code, name in [("sh600001", "A"), ("sh600002", "B"), ("sh600003", "C")]
                ],
            }

    env.service.auction_source = CompleteSource(True)
    records = []
    env.service.models = SimpleNamespace(
        snapshot=lambda day: SimpleNamespace(dates=("20260924",), identity="models"),
        predict=lambda vectors, snapshot: np.array([2.0, 1.0, -1.0]),
        record_candidate=lambda *args: records.append(args),
    )
    monkeypatch.setattr(module, "feature_candidate", lambda candidate: np.zeros(43))
    await env.service.analyze()
    await env.service.analyze()
    rows = env.service.repo.rows("recommendations")
    assert len(rows) == 2
    assert len(records) == 3
    assert [r["allocation_base_cash"] for r in rows] == [15000.0, 15000.0]
    assert all(r["reference_price"] == 10 and r["execution_limit_price"] == 11 for r in rows)
    assert len(env.service.repo.rows("analysis_runs")) == 1


@pytest.mark.asyncio
async def test_auto_off_does_not_stop_existing_position_exit(env):
    from stock_god.prediction.core import stamp
    from stock_god.prediction.repository import insert

    bought = local("2026-09-24T09:30:00+08:00")
    with env.db.transaction() as con:
        insert(
            con,
            "recommendations",
            {
                "recommendation_id": "holding",
                "slot": "base43",
                "stock_code": "sh600001",
                "status": "active",
                "buy_at": stamp(bought),
                "signal_at": stamp(bought),
                "target_buy_at": stamp(bought),
                "quantity": 100,
                "buy_price": 10,
                "buy_fees": 5,
            },
        )

    class ExitSource(Source):
        def rules(self, code, day):
            return {"known": True, "suspended": False, "upper": 1100, "lower": 900}

        def exit_state(self, code, day, bought):
            return {"complete": True, "auctionPrice": 10, "buyClose": 10}

    env.service.auction_source = ExitSource(True)
    env.settings.config["predictionAutoEnabled"] = False
    env.clock.at = local("2026-09-25T09:30:01+08:00")
    await env.service.process_trades()
    assert env.service.repo.row("recommendations", "recommendation_id=?", ("holding",))["status"] == "closed"
    assert len(env.service.repo.rows("trades")) == 1


@pytest.mark.asyncio
async def test_close_recovers_multiple_pending_dates_but_never_seed_unknown(env, monkeypatch):
    import json

    import stock_god.prediction.base43_reference as reference

    env.clock.at = local("2026-09-25T15:05:00+08:00")
    with env.db.transaction() as con:
        for day, evidence in [
            ("20260921", {"code": "sh600001"}),
            ("20260923", {"code": "sh600002"}),
            ("20260922", {"source": "frozen_raw_roi"}),
        ]:
            con.execute(
                "INSERT INTO research2_base43_samples VALUES (?,?,?,?,?,?,?,?)",
                (day, day, "sh600001", "[]", None, None, "pending", json.dumps(evidence)),
            )
    env.service.auction_source = Source(True)
    calls = []
    env.service.models = SimpleNamespace(
        mature_pending=lambda inputs, day, maturity, *args: calls.append((day, maturity)),
        prepare=lambda day: None,
    )
    monkeypatch.setattr(reference, "ReferenceInputs", lambda *args: object())
    await env.service.finalize_metrics()
    assert calls == [("20260921", "2026-09-22"), ("20260923", "2026-09-24")]
    assert env.service._task_done("2026-09-25", "close")
