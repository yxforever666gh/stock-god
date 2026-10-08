"""Daily preparation keeps irreplaceable capture ahead of reusable inputs."""

import asyncio
from contextlib import contextmanager
from threading import Event
from types import SimpleNamespace

import pytest

from stock_god.prediction.core import local


class CaptureSource:
    def __init__(self):
        self.events = []
        self.block = None

    def with_settings(self, config):
        return self

    def status(self):
        return {"configured": True, "status": "incomplete", "ready": False}

    def collect_mother(self, day):
        self.events.append("mother")

    def prepare(self, day, previous):
        self.events.append("inputs")
        if self.block:
            self.block[0].set()
            assert self.block[1].wait(10)
        return {"complete": False, "sourceStatusJson": {"status": "incomplete"}}

    def poll(self, *args):
        assert "mother" in self.events
        self.events.append("poll")

    @contextmanager
    def stage(self, name):
        self.events.append(name + ":start")
        yield
        self.events.append(name + ":end")


def capture(env):
    source = CaptureSource()
    env.service.auction_source = source
    env.service.models = SimpleNamespace(prepare=lambda day: source.events.append("model"))
    env.settings.config["meozApiKey"] = "fixture-key"
    env.settings.save()
    return source


@pytest.mark.asyncio
async def test_mother_saved_before_calendar_and_model(env):
    source = capture(env)
    original = env.service._previous

    def previous(market, now):
        source.events.append("calendar")
        return original(market, now)

    env.service._previous = previous
    env.clock.at = local("2026-09-24T09:14:57+08:00")
    await env.service.prepare_day()
    assert source.events.index("mother") < source.events.index("calendar") < source.events.index("inputs")
    assert source.events[-3:] == ["model:start", "model", "model:end"]
    assert source.events.count("calendar:start") == source.events.count("calendar:end") == 2
    assert not env.service._task_done("2026-09-24", "prepare")
    assert not env.service.repo.rows("analysis_runs")


@pytest.mark.asyncio
async def test_slow_inputs_do_not_stop_separate_poll_task(env):
    source = capture(env)
    entered, release = Event(), Event()
    source.block = entered, release
    env.clock.at = local("2026-09-24T09:14:57+08:00")
    await env.service.tick()
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        env.clock.at = local("2026-09-24T09:15:00+08:00")
        await env.service.tick()
        await env.service._tasks["poll"]
        assert "poll" in source.events and "model" not in source.events
    finally:
        release.set()
        await asyncio.gather(*list(env.service._tasks.values()))


@pytest.mark.asyncio
async def test_interrupted_prepare_restores_waiting_with_identity(env):
    capture(env)
    env.clock.at = local("2026-09-24T09:20:00+08:00")
    key = env.service._source_key(env.settings.config)
    env.service._task("2026-09-24", "prepare", "running", {"keyFingerprint": key})
    env.service._task("2026-09-24", "freeze", "running")
    env.service.models = SimpleNamespace(bootstrap=lambda: None, health=lambda: {"ready": True})
    await env.service.recover(resume=False)
    with env.db.connection() as con:
        row = con.execute("SELECT status,payload_json FROM research2_base43_daily_tasks "
                          "WHERE task_type='prepare'").fetchone()
        frozen = con.execute("SELECT status FROM research2_base43_daily_tasks "
                             "WHERE task_type='freeze'").fetchone()
    assert row[0] == "waiting" and key in row[1]
    assert frozen[0] == "failed"
    assert not env.service._prepare_due("2026-09-24", env.clock.at, env.settings.config)
    env.clock.at = local("2026-09-24T09:20:03+08:00")
    assert env.service._prepare_due("2026-09-24", env.clock.at, env.settings.config)


@pytest.mark.asyncio
async def test_explicit_model_failure_remains_terminal(env):
    source = capture(env)

    def broken(day):
        raise ValueError("invalid model")

    env.service.models.prepare = broken
    env.clock.at = local("2026-09-24T09:14:57+08:00")
    await env.service.prepare_day()
    assert source.events.index("mother") < source.events.index("inputs") < source.events.index("model:start")
    assert env.service._task_done("2026-09-24", "prepare")
    assert not env.service.repo.rows("recommendations")
