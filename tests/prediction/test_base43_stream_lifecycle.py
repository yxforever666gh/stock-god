"""Settings-scoped, bounded stream ownership using only disposable resources and fakes."""

import asyncio
import copy
from datetime import timedelta
from threading import Event, get_ident
from types import SimpleNamespace

import pytest

import stock_god.prediction.service as service_module
from stock_god.prediction.core import DEFAULT_SLOT, local, stamp
from stock_god.prediction.repository import insert

DAY = "2026-09-24"


class StreamSource:
    def __init__(self, root=None, config=None):
        self.root = root or self
        self.config = copy.deepcopy(config or {})
        self.started, self.cancelled = asyncio.Event(), asyncio.Event()
        self.revoked, self.close_count = False, 0
        if root is None:
            self.clones, self.captures, self.merges, self.events = [], [], [], []
            self.configured, self.fail = True, False
            self.finish, self.cleanup_release = asyncio.Event(), asyncio.Event()
            self.cleanup_release.set()
            self.calendar_entered, self.calendar_release = Event(), Event()
            self.calendar_release.set()
            self.prepare_entered, self.prepare_release = Event(), Event()
            self.prepare_release.set()
            self.model_entered, self.model_release = Event(), Event()
            self.model_release.set()
            self.calendar_calls, self.trading = [], True
            self.frozen = {"complete": False}

    def with_settings(self, config):
        clone = StreamSource(self.root, config)
        self.root.clones.append(clone)
        return clone

    def status(self):
        return {"configured": self.root.configured and bool(self.config.get("meozApiKey")),
                "ready": False, "status": "incomplete"}

    def revoke(self):
        self.revoked = True

    def close(self):
        self.revoke()
        self.close_count += 1

    def merge(self, value):
        if not self.revoked:
            self.root.merges.append((self.config["meozApiKey"], value))

    async def capture_stream(self, day, holding_symbols=()):
        self.root.captures.append((self, day, tuple(holding_symbols)))
        self.root.events.append("stream")
        self.started.set()
        try:
            if self.root.fail:
                raise RuntimeError("private fixture credential must not reach logs")
            await self.root.finish.wait()
            self.merge("complete")
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.root.cleanup_release.wait()
            self.merge("buffered stale result")
            raise

    def collect_mother(self, day):
        self.root.events.append("mother")

    def prepare(self, day, previous):
        self.root.events.append("qualification")
        self.root.prepare_entered.set()
        assert self.root.prepare_release.wait(5)
        return {"complete": False, "sourceStatusJson": self.status()}

    def freeze(self, day, cutoff):
        return copy.deepcopy(self.root.frozen)


async def spin_until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0)


async def start(env):
    previous = len(env.source.captures)
    await env.service.tick()
    await spin_until(lambda: len(env.source.captures) > previous)
    return env.source.captures[-1][0]


async def drain_stream(env):
    task = env.service._tasks.get("auction-stream")
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)


def change_settings(env, **changes):
    before = env.settings.load()
    env.settings.config.update(changes)
    env.settings.save()
    env.service.on_settings_changed(before, env.settings.load())


@pytest.fixture
async def stream_env(env, monkeypatch):
    env.clock.at = local(DAY + "T09:14:30+08:00")
    env.settings.config.update(meozApiKey="first-key", crawlTimeOut=30, fixtureNested={"items": [1]})
    env.settings.save()
    env.source = StreamSource()
    env.service.auction_source = env.source
    env.real_prepare_due, env.real_freeze_due = env.service._prepare_due, env.service._freeze_due
    monkeypatch.setattr(env.service, "_prepare_due", lambda *args: False)
    monkeypatch.setattr(env.service, "_freeze_due", lambda *args: False)
    env.service._last_email = env.clock()

    def calendar(at):
        env.source.calendar_calls.append((at, get_ident()))
        env.source.calendar_entered.set()
        assert env.source.calendar_release.wait(5)
        return env.source.trading

    def prepare_model(day):
        env.source.events.append("model")
        env.source.model_entered.set()
        assert env.source.model_release.wait(5)

    async def poll(now):
        env.source.events.append("poll")

    monkeypatch.setattr(env.market, "is_trading_day", calendar)
    monkeypatch.setattr(env.service, "_poll", poll)
    env.service.models = SimpleNamespace(prepare=prepare_model)
    try:
        yield env
    finally:
        env.source.calendar_release.set()
        env.source.prepare_release.set()
        env.source.model_release.set()
        env.source.cleanup_release.set()
        await env.service.close()
        env.db.close()


@pytest.mark.asyncio
async def test_091429_does_not_start_but_091430_starts_one_thread_gated_connection(stream_env):
    env = stream_env
    env.clock.at -= timedelta(seconds=1)
    await env.service.tick()
    assert "auction-stream" not in env.service._tasks
    assert not env.source.clones and not env.source.calendar_calls
    env.clock.at += timedelta(seconds=1)
    clone = await start(env)
    task = env.service._tasks["auction-stream"]
    assert task.get_name() == "prediction:auction-stream"
    assert env.source.captures == [(clone, DAY, ())]
    assert len(env.source.calendar_calls) == 1
    assert env.source.calendar_calls[0][1] != get_ident()
    assert clone.close_count == 0  # The source context owns the entire capture, not just startup.
    for seconds in (0, 1, 30, 300):
        env.clock.at = local(DAY + "T09:14:30+08:00") + timedelta(seconds=seconds)
        await asyncio.gather(*(env.service.tick() for _ in range(5)))
    assert env.service._tasks["auction-stream"] is task
    assert len(env.source.captures) == 1
    assert not env.service.repo.rows("analysis_runs")


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["nontrading", "weekend", "no-key", "disabled", "unconfigured", "no-source"])
async def test_only_configured_enabled_trading_days_connect(stream_env, condition):
    env = stream_env
    if condition in {"nontrading", "weekend"}:
        env.source.trading = False
    if condition == "weekend":
        env.clock.at = local("2026-09-26T09:14:30+08:00")
    if condition == "no-key":
        env.settings.config["meozApiKey"] = "  "
    if condition == "disabled":
        env.settings.config["predictionAutoEnabled"] = False
    if condition == "unconfigured":
        env.source.configured = False
    if condition == "no-source":
        env.service.auction_source = None
    await env.service.tick()
    await drain_stream(env)
    assert not env.source.captures
    if condition in {"nontrading", "weekend"}:
        assert len(env.source.calendar_calls) == 1
        assert env.source.calendar_calls[0][1] != get_ident()
        env.clock.at += timedelta(seconds=31)
        await env.service.tick()
        assert len(env.source.calendar_calls) == 1
    else:
        assert not env.source.calendar_calls
    assert "auction-stream" not in env.service._tasks


@pytest.mark.asyncio
async def test_snapshot_taken_once_and_holdings_read_at_capture_start(stream_env):
    env = stream_env
    env.source.calendar_release.clear()
    await env.service.tick()
    await spin_until(env.source.calendar_entered.is_set)
    env.settings.config["fixtureNested"]["items"].append(2)
    with env.db.transaction() as con:
        for index, (code, status, slot) in enumerate([
            ("sz000002", "active", DEFAULT_SLOT), ("sh600001", "sell_pending", DEFAULT_SLOT),
            ("sz000002", "active", DEFAULT_SLOT), ("sh600003", "buy_pending", DEFAULT_SLOT),
            ("sh600004", "active", "09:30"), ("sh600005", "sold", DEFAULT_SLOT),
        ]):
            insert(con, "recommendations", {
                "recommendation_id": f"holding-{index}", "slot": slot, "stock_code": code,
                "status": status, "signal_at": stamp(env.clock()), "target_buy_at": stamp(env.clock()),
            })
    env.source.calendar_release.set()
    await spin_until(lambda: bool(env.source.captures))
    clone, day, holdings = env.source.captures[0]
    assert day == DAY and holdings == ("sh600001", "sz000002")
    assert clone.config["fixtureNested"] == {"items": [1]}
    clone.config["fixtureNested"]["items"].append(3)
    assert env.settings.config["fixtureNested"] == {"items": [1, 2]}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"meozApiKey": "second-key"}, {"crawlTimeOut": 12}, {"httpProxyEnabled": True},
    {"httpProxy": "http://fixture.invalid:7890"}, {"forceNoProxyForFetch": False},
    {"tushareToken": "second-calendar-key"},
])
async def test_config_changes_revoke_promptly_and_drain_before_restart(stream_env, change):
    env = stream_env
    old = await start(env)
    old_task = env.service._tasks["auction-stream"]
    env.source.cleanup_release.clear()
    change_settings(env, **change)
    assert old.revoked and old_task.cancelling()
    old.merge("late buffered merge")
    assert not env.source.merges
    await spin_until(old.cancelled.is_set)
    await env.service.tick()
    assert env.service._tasks["auction-stream"] is old_task
    assert not old_task.done() and len(env.source.captures) == 1
    env.source.cleanup_release.set()
    await drain_stream(env)
    await env.service.tick()
    await spin_until(lambda: len(env.source.captures) == 2)
    new = env.source.captures[-1][0]
    assert new is not old and old.close_count == 1
    assert all(new.config[key] == value for key, value in change.items())
    assert not env.source.merges


@pytest.mark.asyncio
async def test_auto_off_on_cannot_resurrect_old_same_key_capture(stream_env):
    env = stream_env
    old = await start(env)
    env.source.cleanup_release.clear()
    change_settings(env, predictionAutoEnabled=False)
    assert old.revoked
    change_settings(env, predictionAutoEnabled=True)
    await spin_until(old.cancelled.is_set)
    await env.service.tick()
    assert len(env.source.captures) == 1
    old.merge("late same-key result after reenable")
    assert not env.source.merges
    env.source.cleanup_release.set()
    await drain_stream(env)
    new = await start(env)
    assert new is not old and new.config["meozApiKey"] == old.config["meozApiKey"]
    assert not env.source.merges


@pytest.mark.asyncio
async def test_changed_key_before_calendar_returns_never_connects_old_snapshot(stream_env):
    env = stream_env
    env.source.calendar_release.clear()
    await env.service.tick()
    await spin_until(env.source.calendar_entered.is_set)
    old = env.source.clones[0]
    change_settings(env, meozApiKey="second-key")
    assert old.revoked
    env.source.calendar_release.set()
    await drain_stream(env)
    new = await start(env)
    assert [clone.config["meozApiKey"] for clone, _, _ in env.source.captures] == ["second-key"]
    assert new is not old and old.close_count == 1


@pytest.mark.asyncio
async def test_unrelated_settings_keep_connection_and_snapshot(stream_env):
    env = stream_env
    clone = await start(env)
    task = env.service._tasks["auction-stream"]
    change_settings(env, predictionEmailTo="another@example.invalid", fixtureNested={"items": [99]})
    await env.service.tick()
    assert env.service._tasks["auction-stream"] is task
    assert not clone.revoked and clone.config["fixtureNested"] == {"items": [1]}
    assert len(env.source.captures) == 1


@pytest.mark.asyncio
async def test_shutdown_waits_for_revoked_stream_cleanup_without_double_cancel(stream_env):
    env = stream_env
    clone = await start(env)
    env.source.cleanup_release.clear()
    closing = asyncio.create_task(env.service.close())
    try:
        await spin_until(clone.cancelled.is_set)
        assert clone.revoked and not closing.done()
        await env.service.tick()
        assert len(env.source.captures) == 1
        assert env.service._tasks["auction-stream"].cancelling() == 1
    finally:
        env.source.cleanup_release.set()
        await closing
    assert clone.close_count == 1 and not env.source.merges
    assert "auction-stream" not in env.service._tasks
    await env.service.tick()
    assert len(env.source.captures) == 1


@pytest.mark.asyncio
async def test_deadline_timer_revokes_without_scheduler_tick(stream_env, monkeypatch):
    env = stream_env
    loop, scheduled = asyncio.get_running_loop(), []
    call_later = loop.call_later

    def record_timer(delay, callback, *args, **kwargs):
        handle = call_later(delay, callback, *args, **kwargs)
        if callback == env.service._stop_stream:
            scheduled.append((delay, callback, handle))
        return handle

    monkeypatch.setattr(loop, "call_later", record_timer)
    clone = await start(env)
    assert len(scheduled) == 1 and scheduled[0][0] == 929
    env.clock.at = local(DAY + "T09:29:59+08:00")
    scheduled[0][1]()  # Fire the captured deadline, without real sleeps or another tick.
    assert clone.revoked
    await drain_stream(env)
    assert scheduled[0][2].cancelled() and clone.close_count == 1
    assert not env.source.merges
    for at in ("09:29:59", "09:30:00", "09:30:59"):
        env.clock.at = local(DAY + "T" + at + "+08:00")
        await env.service.tick()
        assert "auction-stream" not in env.service._tasks
    assert len(env.source.captures) == 1


@pytest.mark.asyncio
async def test_tick_at_deadline_cancels_and_late_calendar_cannot_start(stream_env):
    env = stream_env
    env.source.calendar_release.clear()
    await env.service.tick()
    await spin_until(env.source.calendar_entered.is_set)
    old = env.source.clones[0]
    env.clock.at = local(DAY + "T09:29:59+08:00")
    await env.service.tick()
    assert old.revoked
    env.source.calendar_release.set()
    await drain_stream(env)
    assert not env.source.captures and old.close_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["qualification", "model"])
async def test_stream_first_survives_slow_preparation_and_ordinary_poll(stream_env, monkeypatch, stage):
    env = stream_env
    monkeypatch.setattr(env.service, "_prepare_due", env.real_prepare_due)
    entered, release = (env.source.prepare_entered, env.source.prepare_release) if stage == "qualification" else (
        env.source.model_entered, env.source.model_release
    )
    release.clear()
    launch, launched = env.service._launch, []

    def record_launch(key, coroutine):
        launched.append(key)
        launch(key, coroutine)

    monkeypatch.setattr(env.service, "_launch", record_launch)
    await env.service.tick()
    try:
        await spin_until(entered.is_set)
        await spin_until(lambda: bool(env.source.captures))
        assert launched[:2] == ["auction-stream", "prepare"]
        stream = env.service._tasks["auction-stream"]
        preparation = env.service._tasks["prepare"]
        assert not stream.done() and not preparation.done()
        env.clock.at = local(DAY + "T09:15:00+08:00")
        await env.service.tick()
        await env.service._tasks["poll"]
        assert "poll" in env.source.events and env.service._tasks["auction-stream"] is stream
        assert len(env.source.captures) == 1 and not stream.done()
    finally:
        release.set()
        await drain_non_stream_tasks(env)


async def drain_non_stream_tasks(env):
    await asyncio.gather(*(task for key, task in list(env.service._tasks.items()) if key != "auction-stream"))
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_failures_have_cooldown_and_three_attempt_bound(stream_env, caplog):
    env = stream_env
    env.source.fail = True
    origin = env.clock()
    for seconds, expected in [(0, 1), (1, 1), (29, 1), (30, 2), (59, 2), (60, 3), (90, 3), (600, 3)]:
        env.clock.at = origin + timedelta(seconds=seconds)
        await env.service.tick()
        await drain_stream(env)
        assert len(env.source.captures) == expected
    assert all(clone.close_count == 1 for clone, _, _ in env.source.captures)
    assert "private fixture credential" not in caplog.text
    env.clock.at = local("2026-09-25T09:14:30+08:00")
    await env.service.tick()
    await drain_stream(env)
    assert len(env.source.captures) == 4


@pytest.mark.asyncio
async def test_normal_completion_never_spins_a_second_connection(stream_env):
    env = stream_env
    clone = await start(env)
    env.source.finish.set()
    await drain_stream(env)
    assert clone.close_count == 1
    env.clock.at += timedelta(seconds=31)
    await env.service.tick()
    assert len(env.source.captures) == 1 and "auction-stream" not in env.service._tasks


@pytest.mark.asyncio
async def test_old_done_callback_cannot_pop_new_same_key_task(stream_env):
    env = stream_env
    release = asyncio.Event()

    async def finished():
        return None

    env.service._launch("auction-stream", finished())
    old = env.service._tasks["auction-stream"]
    env.service._launch("auction-stream", release.wait())
    new = env.service._tasks["auction-stream"]
    await old
    await asyncio.sleep(0)
    assert env.service._tasks["auction-stream"] is new and not new.done()
    release.set()
    await new


@pytest.mark.asyncio
@pytest.mark.parametrize("ready_at", ["09:26:00", "09:26:03"])
async def test_earliest_ready_publication_and_0930_buy_target_unchanged(stream_env, monkeypatch, ready_at):
    import numpy as np

    env = stream_env
    await start(env)
    monkeypatch.setattr(env.service, "_freeze_due", env.real_freeze_due)
    monkeypatch.setattr(service_module, "feature_candidate", lambda candidate: np.zeros(43))
    records = []
    env.service.models = SimpleNamespace(
        snapshot=lambda day: SimpleNamespace(dates=(DAY.replace("-", ""),), identity="fixture-models"),
        predict=lambda vectors, snapshot: np.array([2.0, 1.0, -1.0]),
        record_candidate=lambda *args: records.append(args),
    )
    frozen = {"complete": True, "factsSha256": "fixture-facts", "candidates": [
        {"code": code, "name": code, "upper": 1100, "auctionRows": [
            {"time": 33900, "receivedAt": DAY + "T09:25:00+08:00",
             "fields": [10, 10, 100000, 1000000] + [None] * 13}
        ]}
        for code in ("sh600001", "sh600002", "sh600003")
    ]}
    env.clock.at = local(DAY + "T09:25:59+08:00")
    await env.service.tick()
    await drain_non_stream_tasks(env)
    assert not env.service.repo.rows("analysis_runs")
    if ready_at == "09:26:03":
        env.clock.at = local(DAY + "T09:26:00+08:00")
        await env.service.tick()
        await drain_non_stream_tasks(env)
        assert not env.service.repo.rows("analysis_runs")
        assert not env.service._task_done(DAY, "freeze")
    env.source.frozen = frozen
    env.clock.at = local(DAY + "T" + ready_at + "+08:00")
    await env.service.tick()
    await drain_non_stream_tasks(env)
    recommendations = env.service.repo.rows("recommendations")
    assert len(recommendations) == 2 and len(records) == 3
    assert all(row["status"] == "buy_pending" for row in recommendations)
    assert all(local(row["signal_at"]) == env.clock() for row in recommendations)
    assert all(local(row["target_buy_at"]) == local(DAY + "T09:30:00+08:00") for row in recommendations)
    assert [row["allocation_base_cash"] for row in recommendations] == [15000.0, 15000.0]
    assert env.service._task_done(DAY, "freeze") and not env.service._tasks["auction-stream"].done()
    env.clock.at += timedelta(seconds=3)
    await env.service.tick()
    await drain_non_stream_tasks(env)
    assert len(env.service.repo.rows("analysis_runs")) == 1 and len(records) == 3
    assert len(env.source.captures) == 1 and not env.service.repo.rows("trades")


@pytest.mark.asyncio
async def test_returned_transient_failure_retries_but_permission_failure_is_terminal(stream_env, monkeypatch):
    env = stream_env
    status = {"state": "failed", "sourceStatus": "incomplete"}
    async def fail(source, day, holding_symbols=()):
        source.root.captures.append((source, day, tuple(holding_symbols)))
        return dict(status)
    monkeypatch.setattr(StreamSource, "capture_stream", fail)
    await env.service.tick()
    await drain_stream(env)
    assert len(env.source.captures) == 1
    env.clock.at += timedelta(seconds=31)
    status["sourceStatus"] = "no_permission"
    await env.service.tick()
    await drain_stream(env)
    assert len(env.source.captures) == 2
    env.clock.at += timedelta(seconds=31)
    await env.service.tick()
    assert len(env.source.captures) == 2
