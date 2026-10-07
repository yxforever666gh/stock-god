"""Safety gates in the active orchestration; legacy reads never acquire providers."""

from types import SimpleNamespace
import asyncio
from threading import Event

import pytest

from stock_god.prediction.core import Conflict, local


class Source:
    def __init__(self, ready=False):
        self.ready = ready

    def with_settings(self, config):
        return self

    def status(self):
        return {"ready": self.ready, "configured": True, "status": "unverified"}

    def freeze(self, day, cutoff):
        return {"complete": False}


@pytest.mark.asyncio
async def test_incomplete_source_waits_without_report_or_cash_claim(env):
    env.clock.at = local("2026-09-24T09:26:00+08:00")
    env.service.auction_source = Source()
    assert await env.service.analyze() is None
    assert env.service.repo.rows("analysis_runs") == []
    assert env.service.repo.rows("execution_chains") == []
    assert not env.service._task_done("2026-09-24", "freeze")
    assert not env.service._freeze_due("2026-09-24", local("2026-09-24T09:26:02+08:00"))
    assert env.service._freeze_due("2026-09-24", local("2026-09-24T09:26:03+08:00"))


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


@pytest.fixture
def complete_inputs(env, monkeypatch):
    import numpy as np

    import stock_god.prediction.service as module


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

    env.service.auction_source = CompleteSource(False)
    records = []
    env.service.models = SimpleNamespace(
        snapshot=lambda day: SimpleNamespace(dates=("20260924",), identity="models"),
        predict=lambda vectors, snapshot: np.array([2.0, 1.0, -1.0]),
        record_candidate=lambda *args: records.append(args),
    )
    monkeypatch.setattr(module, "feature_candidate", lambda candidate: np.zeros(43))
    return env.service.auction_source, records


@pytest.mark.asyncio
@pytest.mark.parametrize("at", ["09:26:00", "09:29:55"])
async def test_complete_inputs_publish_once_with_actual_signal_and_open_buy_time(env, complete_inputs, at):
    _, records = complete_inputs
    env.clock.at = local("2026-09-24T" + at + "+08:00")
    await env.service.analyze()
    await env.service.analyze()
    rows = env.service.repo.rows("recommendations")
    assert len(rows) == 2 and len(records) == 3
    assert [r["allocation_base_cash"] for r in rows] == [15000.0, 15000.0]
    assert all(r["reference_price"] == 10 and r["execution_limit_price"] == 11 for r in rows)
    assert all(local(r["signal_at"]) == env.clock.at for r in rows)
    assert all(local(r["target_buy_at"]) == local("2026-09-24T09:30:00+08:00") for r in rows)
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


@pytest.mark.asyncio
async def test_wait_then_complete_only_claims_cash_once(env, complete_inputs):
    source, records = complete_inputs
    real_freeze = source.freeze
    source.freeze = lambda *a: {"complete": False}
    env.clock.at = local("2026-09-24T09:26:00+08:00")
    await env.service.analyze()
    assert not env.service.repo.rows("analysis_runs")
    assert not env.service.repo.rows("execution_chains")
    source.freeze = real_freeze
    env.clock.at = local("2026-09-24T09:26:03+08:00")
    await env.service.analyze()
    env.service.repo.set("accounts", {"cash": 50000}, "slot=?", ("base43",))
    env.clock.at = local("2026-09-24T09:29:55+08:00")
    await env.service.analyze()
    assert len(records) == 3 and len(env.service.repo.rows("analysis_runs")) == 1
    assert [r["allocation_base_cash"] for r in env.service.repo.rows("recommendations")] == [15000, 15000]


@pytest.mark.asyncio
async def test_incomplete_inputs_stop_at_deadline_without_buy_list(env):
    env.service.auction_source = Source()
    env.clock.at = local("2026-09-24T09:29:59+08:00")
    await env.service.analyze()
    assert env.service._task_done("2026-09-24", "freeze")
    assert not env.service.repo.rows("analysis_runs")
    assert not env.service.repo.rows("execution_chains")
    env.clock.at = local("2026-09-24T09:30:00+08:00")
    with pytest.raises(Conflict):
        await env.service.analyze()
    assert not env.service.repo.rows("recommendations")


@pytest.mark.asyncio
async def test_waiting_state_survives_service_restart(env, complete_inputs):
    from stock_god.prediction.service import PredictionService
    source, _ = complete_inputs
    real_freeze = source.freeze
    source.freeze = lambda *a: {"complete": False}
    env.clock.at = local("2026-09-24T09:26:00+08:00")
    await env.service.analyze()
    resumed = PredictionService(env.db, env.market, env.settings, None, env.service.audit,
                                clock=env.clock, auction_source=source, models=env.service.models)
    source.freeze = real_freeze
    env.clock.at = local("2026-09-24T09:26:03+08:00")
    assert resumed._freeze_due("2026-09-24", env.clock.at)
    await resumed.analyze()
    assert len(resumed.repo.rows("analysis_runs")) == 1
    assert len(resumed.repo.rows("recommendations")) == 2


@pytest.mark.asyncio
async def test_permission_error_blocks_without_claim(env):
    source = Source()
    source.status = lambda: {"configured": True, "ready": False, "status": "unauthorized"}
    env.service.auction_source = source
    env.clock.at = local("2026-09-24T09:26:00+08:00")
    await env.service.analyze()
    assert env.service._task_done("2026-09-24", "freeze")
    assert not env.service.repo.rows("analysis_runs")


@pytest.mark.asyncio
async def test_before_0926_never_claims_and_no_positive_result_is_final(env, complete_inputs):
    import numpy as np
    env.clock.at = local("2026-09-24T09:25:59+08:00")
    with pytest.raises(Conflict):
        await env.service.analyze()
    assert not env.service.repo.rows("analysis_runs")
    env.clock.at = local("2026-09-24T09:26:00+08:00")
    env.service.models.predict = lambda *a: np.array([-1., -2., -3.])
    await env.service.analyze()
    assert env.service.repo.rows("analysis_runs")[0]["status"] == "no_recommendation"
    assert env.service._task_done("2026-09-24", "freeze")
    assert not env.service.repo.rows("recommendations")


@pytest.fixture
def preparing_inputs(env):
    calls = []
    controls = SimpleNamespace(result=lambda key: {"complete": True})

    class PreparingSource(Source):
        def __init__(self, config=None):
            self.key = (config or {}).get("meozApiKey", "")
            self.ready = False

        def with_settings(self, config):
            return PreparingSource(config)

        def prepare(self, day, previous):
            calls.append((self.key, day, previous))
            return controls.result(self.key)

        def poll(self, *args):
            return False

    env.service.auction_source = PreparingSource()
    env.service.models = SimpleNamespace(prepare=lambda day: None)
    env.settings.config["meozApiKey"] = "first-key"
    env.settings.save()
    return controls, calls


async def settle(service):
    if service._tasks:
        await asyncio.gather(*list(service._tasks.values()))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["incomplete", "exception"])
async def test_temporary_mother_failure_retries_across_0915(env, preparing_inputs, failure):
    controls, calls = preparing_inputs

    def prepare(key):
        if len(calls) == 1 and failure == "exception":
            raise ValueError("temporary provider failure")
        return {"complete": len(calls) > 1, "sourceStatusJson": {"status": "incomplete"}}

    controls.result = prepare
    env.clock.at = local("2026-09-24T09:14:57+08:00")
    await env.service.tick()
    await settle(env.service)
    assert not env.service._task_done("2026-09-24", "prepare")
    env.clock.at = local("2026-09-24T09:14:59+08:00")
    await env.service.tick()
    await settle(env.service)
    assert len(calls) == 1
    env.clock.at = local("2026-09-24T09:15:00+08:00")
    await env.service.tick()
    await settle(env.service)
    assert len(calls) == 2 and env.service._task_done("2026-09-24", "prepare")
    env.clock.at = local("2026-09-24T09:15:03+08:00")
    await env.service.tick()
    await settle(env.service)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_changed_key_reprepares_success_and_reopens_only_unclaimed_source_block(env, preparing_inputs):
    _, calls = preparing_inputs
    env.clock.at = local("2026-09-24T09:10:00+08:00")
    await env.service.tick()
    await settle(env.service)
    env.service._task("2026-09-24", "freeze", "blocked", error="竞价来源权限或配置错误")
    env.clock.at = local("2026-09-24T09:26:00+08:00")
    before = env.settings.load()
    env.settings.config["meozApiKey"] = "second-key"
    env.settings.save()
    env.service.on_settings_changed(before, env.settings.load())
    env.clock.at = local("2026-09-24T09:26:03+08:00")
    await env.service.tick()
    await settle(env.service)
    assert [call[0] for call in calls] == ["first-key", "second-key"]
    assert env.service._task_done("2026-09-24", "prepare")
    assert not env.service._task_done("2026-09-24", "freeze")
    assert not env.service.repo.rows("analysis_runs")


@pytest.mark.asyncio
async def test_old_preparation_finishes_without_overwriting_changed_key_receipt(env, preparing_inputs):
    controls, calls = preparing_inputs
    entered, release = Event(), Event()

    def prepare(key):
        if key == "first-key":
            entered.set()
            assert release.wait(10)
        return {"complete": True}

    controls.result = prepare
    env.clock.at = local("2026-09-24T09:14:57+08:00")
    await env.service.tick()
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        before = env.settings.load()
        env.settings.config["meozApiKey"] = "second-key"
        env.settings.save()
        env.service.on_settings_changed(before, env.settings.load())
        env.clock.at = local("2026-09-24T09:15:00+08:00")
        await env.service.tick()
        assert len(calls) == 1  # Drain the old worker before preparing the new snapshot.
    finally:
        release.set()
        await settle(env.service)
    assert not env.service._task_done("2026-09-24", "prepare")
    env.clock.at = local("2026-09-24T09:15:03+08:00")
    await env.service.tick()
    await settle(env.service)
    assert [call[0] for call in calls] == ["first-key", "second-key"]
    assert env.service._task_done("2026-09-24", "prepare")


@pytest.mark.asyncio
async def test_mother_retries_stop_at_preopen_deadline(env, preparing_inputs):
    controls, calls = preparing_inputs
    controls.result = lambda key: {"complete": False, "sourceStatusJson": {"status": "incomplete"}}
    env.clock.at = local("2026-09-24T09:29:59+08:00")
    await env.service.tick()
    await settle(env.service)
    assert env.service._task_done("2026-09-24", "prepare")
    env.clock.at = local("2026-09-24T09:30:00+08:00")
    await env.service.tick()
    await settle(env.service)
    assert len(calls) == 1 and not env.service.repo.rows("recommendations")


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["freeze", "predict"])
async def test_auto_off_then_on_permanently_revokes_inflight_scoring(env, complete_inputs, stage):
    source, _ = complete_inputs
    target = source if stage == "freeze" else env.service.models
    original = getattr(target, stage)
    entered, release = Event(), Event()

    def paused(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    setattr(target, stage, paused)
    env.clock.at = local("2026-09-24T09:26:00+08:00")
    task = asyncio.create_task(env.service.analyze())
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        before = env.settings.load()
        env.settings.config["predictionAutoEnabled"] = False
        env.settings.save()
        env.service.on_settings_changed(before, env.settings.load())
        before = env.settings.load()
        env.settings.config["predictionAutoEnabled"] = True
        env.settings.save()
        env.service.on_settings_changed(before, env.settings.load())
    finally:
        release.set()
        await task
    assert not env.service.repo.rows("recommendations")
    runs = env.service.repo.rows("analysis_runs")
    if stage == "freeze":
        assert not runs
    else:
        assert runs[0]["status"] == "failed"
    assert env.service._task_done("2026-09-24", "freeze")
    await env.service.analyze()
    assert len(env.service.repo.rows("analysis_runs")) == len(runs)


@pytest.mark.asyncio
async def test_auto_off_then_on_between_permission_check_and_claim_cannot_publish(env, complete_inputs, monkeypatch):
    _, records = complete_inputs
    claim = env.service.repo.claim_run

    def revoke_then_claim(scheduled):
        before = env.settings.load()
        env.settings.config["predictionAutoEnabled"] = False
        env.settings.save()
        env.service.on_settings_changed(before, env.settings.load())
        before = env.settings.load()
        env.settings.config["predictionAutoEnabled"] = True
        env.settings.save()
        env.service.on_settings_changed(before, env.settings.load())
        return claim(scheduled)

    monkeypatch.setattr(env.service.repo, "claim_run", revoke_then_claim)
    env.clock.at = local("2026-09-24T09:26:00+08:00")
    await env.service.analyze()
    assert not records and not env.service.repo.rows("recommendations")
    assert env.service.repo.rows("analysis_runs")[0]["status"] == "failed"
    assert env.service._task_done("2026-09-24", "freeze")
