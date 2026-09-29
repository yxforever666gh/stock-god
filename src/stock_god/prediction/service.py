"""Prediction task orchestration. Every entry captures independent provider settings."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import logging
from contextlib import contextmanager
from datetime import timedelta

from .core import (
    DEFAULT_SLOT,
    SLOTS,
    Conflict,
    PredictionError,
    continuous,
    fresh_quote,
    json_text,
    local,
    next_session,
    positive,
    slot_at,
    slot_time,
    stamp,
    upper_limit,
)
from .email import EmailService, queue_published
from .evidence import PROMPT, build_prompt, parse_output, prepare, render_report, validate
from .repository import Repository, day_count, ensure_chain, ledger_snapshot, many, one, update
from .views import Views

log = logging.getLogger(__name__)


def as_record(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return dict(value) if isinstance(value, dict) else vars(value)


class PredictionService:
    def __init__(
        self, database, market, settings, ai_factory, audit, *, clock=None, mailer=None, evidence_store=None
    ):
        self.database, self.market, self.settings, self.ai_factory, self.audit = (
            database,
            market,
            settings,
            ai_factory,
            audit,
        )
        self.clock = clock or local
        self.repo = Repository(database, self.clock)
        self.views = Views(self.repo)
        self.email = EmailService(self.repo, settings, mailer)
        self._trade_lock = asyncio.Lock()
        self._metric_lock = asyncio.Lock()
        self._tasks = {}
        self._last_trade = None
        self._last_email = None
        self._last_metric = None
        self._chart_locks = {}
        self._chart_cache = {}
        self._non_trading_date = None
        if evidence_store is None:
            from .evidence_store import EvidenceStore

            evidence_store = EvidenceStore(database)
        self.evidence_store = evidence_store

    def _snapshot(self):
        return copy.deepcopy(self.settings.load())

    def _market(self, config):
        return self.market.with_settings(copy.deepcopy(config))

    @contextmanager
    def _provider(self, config):
        market = self._market(config)
        try:
            yield market
        finally:
            market.close()

    def on_settings_changed(self, before, after):
        before_config = before.config if hasattr(before, "config") else before
        after_config = after.config if hasattr(after, "config") else after
        if before_config.get("predictionAutoEnabled", True) and not after_config.get(
            "predictionAutoEnabled", True
        ):
            now = stamp(self.clock())
            with self.database.transaction() as connection:
                update(
                    connection,
                    "analysis_runs",
                    {"archive_reason": "自动策略已关闭，仅保留报告"},
                    "status='running'",
                )
                update(
                    connection,
                    "execution_chains",
                    {"status": "disabled", "completed_at": now, "stop_reason": "股票预测自动策略已关闭"},
                    "status='running'",
                )
                update(
                    connection,
                    "recommendations",
                    {"status": "analysis_only", "failure_reason": "自动策略已关闭，仅保留分析"},
                    "status IN ('buy_pending','standby') AND buy_at IS NULL",
                )
        if not after_config.get("predictionEmailEnabled", False):
            self.repo.set(
                "email_deliveries",
                {"status": "cancelled", "next_attempt_at": None, "last_error": "邮件开关已关闭"},
                "status IN ('pending','retry_wait')",
            )

    async def analyze(self, scheduled_for=None, *, diagnostic=False, parent_run_id=None):
        now = self.clock()
        scheduled = local(scheduled_for or now)
        snapshot = self._snapshot()
        with self._provider(snapshot.config) as market:
            if not diagnostic and (not slot_at(now) or now.date() != scheduled.date()):
                raise Conflict("不在允许启动分析的上午窗口")
            if not diagnostic and not snapshot.config.get("predictionAutoEnabled", True):
                raise Conflict("股票预测自动策略已关闭")
            if self._non_trading_date == scheduled.date() or not await asyncio.to_thread(
                market.is_trading_day, scheduled
            ):
                self._non_trading_date = scheduled.date()
                if parent_run_id:
                    raise Conflict("非交易日不执行分析")
                return None
            trigger = (
                "diagnostic"
                if diagnostic
                else "manual_rerun"
                if parent_run_id
                else "startup_recovery"
                if (now - scheduled).total_seconds() >= 60
                else "scheduled"
            )
            run, created = self.repo.claim_run(scheduled, trigger, parent_run_id or "")
            if not created:
                return self.views.run(run["run_id"])
            identity = run["run_id"]
            attempts = []
            try:
                self.audit.begin(identity)
                evidence_id = self.evidence_store.begin(identity, now)
                self.repo.set("analysis_runs", {"evidence_set_id": evidence_id}, "run_id=?", (identity,))
                try:
                    evidence = await asyncio.to_thread(
                        market.collect_prediction_evidence,
                        now,
                        set(),
                        float.fromhex("0x1.fffffffffffffp+1023"),
                    )
                except Exception as error:
                    failed = copy.deepcopy(
                        getattr(error, "evidence", None)
                        or {
                            "cutoffAt": stamp(now),
                            "freezeAt": stamp(self.clock()),
                            "documents": [],
                            "candidates": [],
                        }
                    )
                    failed["evidenceSetId"] = evidence_id
                    failed = self.evidence_store.capture(identity, failed, error=error)
                    self.repo.set(
                        "analysis_runs",
                        {
                            "evidence_set_id": evidence_id,
                            "evidence_cutoff_at": stamp(failed.get("cutoffAt") or now),
                            "evidence_profile_version": failed.get("evidenceProfileVersion", ""),
                            "source_status_json": failed.get("sourceStatusJson") or "[]",
                        },
                        "run_id=?",
                        (identity,),
                    )
                    raise
                evidence["evidenceSetId"] = evidence_id
                evidence = self.evidence_store.capture(identity, evidence)
                evidence = copy.deepcopy(evidence)
                evidence.setdefault("cutoffAt", stamp(now))
                evidence.setdefault("freezeAt", stamp(now))
                evidence.setdefault(
                    "windowStartAt",
                    stamp(
                        local(evidence["cutoffAt"]).replace(second=0, microsecond=0) - timedelta(minutes=5)
                    ),
                )
                evidence.setdefault("windowEndAt", evidence["cutoffAt"])
                evidence = await asyncio.to_thread(prepare, evidence, market)
                values = {
                    "evidence_cutoff_at": stamp(evidence["cutoffAt"]),
                    "evidence_window_start_at": stamp(evidence["windowStartAt"]),
                    "source_status_json": evidence.get("sourceStatusJson") or "[]",
                    "evidence_coverage_pct": evidence.get("coveragePct", 0),
                    "degraded": bool(evidence.get("degraded")),
                    "evidence_set_id": evidence.get("evidenceSetId", ""),
                    "evidence_profile_version": evidence.get("evidenceProfileVersion", ""),
                }
                run.update(values)
                self.repo.set("analysis_runs", values, "run_id=?", (identity,))
                output = {"conclusion": "没有满足当前数据约束的可评分标的。", "recommendations": []}
                items = []
                warnings = []
                prompt = build_prompt(evidence)
                client = self.ai_factory(copy.deepcopy(snapshot.models))
                for sequence in range(1, 3) if evidence.get("candidates") else ():
                    call_attempts = []

                    def on_attempt(record, call_attempts=call_attempts):
                        row = as_record(record)
                        key = row.get("id") or row.get("ID")
                        if key:
                            attempts[:] = [
                                item for item in attempts if (item.get("id") or item.get("ID")) != key
                            ]
                            call_attempts[:] = [
                                item for item in call_attempts if (item.get("id") or item.get("ID")) != key
                            ]
                        attempts.append(row)
                        call_attempts.append(row)
                        self.repo.set(
                            "analysis_runs",
                            {"model_attempt_log_json": json_text(attempts)},
                            "run_id=?",
                            (identity,),
                        )

                    phase = "prediction_overnight_strength" + ("_repair" if sequence > 1 else "")
                    result = None
                    call_error = None
                    prompt = self.audit.prepare_prompt(prompt)
                    try:
                        result = await client.complete(prompt=prompt, phase=phase, on_attempt=on_attempt)
                    except Exception as error:
                        call_error = error
                        raise
                    finally:
                        final_attempts = (
                            result.attempts if result is not None and result.attempts else call_attempts
                        )
                        self.audit.record(
                            identity,
                            phase,
                            sequence,
                            prompt,
                            evidence,
                            result,
                            final_attempts,
                            local(evidence["cutoffAt"]),
                            PROMPT,
                            repaired=sequence > 1,
                            error=call_error,
                        )
                    assert result is not None
                    run["provider_name"] = result.provider_name
                    run["model_name"] = result.model
                    if not call_attempts and result.attempts:
                        attempts.extend(as_record(row) for row in result.attempts)
                    run["model_attempt_log_json"] = json_text(attempts)
                    try:
                        output = parse_output(result.content)
                        items, warnings = validate(identity, evidence, output)
                    except (ValueError, TypeError) as error:
                        if sequence == 2:
                            raise PredictionError("大模型结构化输出无效：" + str(error)) from error
                        warnings = [str(error)]
                    if not warnings:
                        break
                    prompt = (
                        build_prompt(evidence)
                        + "\n# 上次输出纠正要求\n"
                        + "；".join(warnings)
                        + "\n请重新生成完整JSON结果，覆盖全部冻结候选。"
                    )
                run.update(
                    status="success" if items else "no_recommendation",
                    recommendation_count=len(items),
                    failure_reason="" if items else str(output.get("conclusion") or "无有效推荐"),
                )
                self.repo.publish(
                    run,
                    items,
                    lambda finalized, rows: render_report(finalized, rows, evidence, output, warnings),
                    queue_published,
                )
                self.audit.complete(identity)
            except asyncio.CancelledError:
                self._fail(identity, "分析被中断")
                raise
            except Exception as error:
                self._fail(identity, str(error))
                raise
            return self.views.run(identity)

    def _fail(self, identity, reason):
        self.repo.set(
            "analysis_runs",
            {"status": "failed", "generated_at": stamp(self.clock()), "failure_reason": reason},
            "run_id=? AND persisted_at IS NULL",
            (identity,),
        )
        self.audit.fail(identity, reason)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE research_evidence_sets SET status='failed',frozen_at=? WHERE owner_type='research2' AND owner_id=? AND status='collecting'",
                (stamp(self.clock()), identity),
            )

    async def rerun(self, identity):
        parent = self.repo.row("analysis_runs", "run_id=?", (identity,))
        result = await self.analyze(local(parent["scheduled_for"]), parent_run_id=identity)
        await self.process_trades()
        return result

    async def _sells(self, market, now):
        for slot in SLOTS:
            scheduled = slot_time(now, slot)
            if now < scheduled:
                continue
            with self.database.transaction() as connection:
                chain = ensure_chain(connection, slot, now)
            if chain["sell_completed_at"]:
                continue
            items = self.repo.rows(
                "recommendations",
                "slot=? AND status IN ('active','sell_pending') AND julianday(buy_at)<julianday(?) AND historical_sell_blocked=0",
                (slot, stamp(scheduled)),
            )
            for item in items:
                try:
                    quote = await asyncio.wait_for(
                        asyncio.to_thread(market.quote, item["stock_code"]), timeout=5
                    )
                except (OSError, ValueError, RuntimeError):
                    quote = {}
                checked = self.clock()
                if not continuous(checked):
                    return
                stale = not positive(quote.get("price")) or not fresh_quote(quote.get("asOf"), checked)
                if stale:
                    price = item["current_price"] or item["buy_market_price"] or item["buy_price"]
                    quote = {
                        "price": price,
                        "asOf": item["current_price_at"] or item["buy_at"],
                        "source": "stored_current_price"
                        if item["current_price"]
                        else "original_buy_market_price"
                        if item["buy_market_price"]
                        else "original_buy_execution_price",
                    }
                self.repo.sell(
                    item["recommendation_id"],
                    quote,
                    checked,
                    stale,
                    "recovered_slot_sell"
                    if (checked - scheduled).total_seconds() >= 60
                    else "scheduled_slot_sell",
                )
            with self.database.transaction() as connection:
                update(
                    connection,
                    "execution_chains",
                    {"sell_completed_at": stamp(self.clock())},
                    "chain_id=? AND sell_completed_at IS NULL",
                    (chain["chain_id"],),
                )
                ledger_snapshot(
                    connection, slot, self.clock(), "scheduled-sell-" + chain["chain_id"], "scheduled_sell"
                )

    def _expire(self, now, auto_enabled):
        with self.database.transaction() as connection:
            date = local(now).date().isoformat()
            update(
                connection,
                "execution_chains",
                {"status": "cutoff", "stop_reason": "原交易日执行窗口已截止", "completed_at": stamp(now)},
                "status='running' AND (trading_date<? OR (trading_date=? AND ?))",
                (date, date, local(now).hour * 60 + local(now).minute >= 690),
            )
            update(
                connection,
                "recommendations",
                {"status": "analysis_only", "failure_reason": "信号交易日或上午买入窗口已截止"},
                "status IN ('buy_pending','standby') AND (date(signal_at,'+8 hours')<? OR (date(signal_at,'+8 hours')=? AND ?))",
                (date, date, local(now).hour * 60 + local(now).minute >= 690),
            )
        if not auto_enabled:
            self.on_settings_changed(
                {"predictionAutoEnabled": True},
                {"predictionAutoEnabled": False, "predictionEmailEnabled": True},
            )

    @staticmethod
    def _buy_quote(market, item):
        if item["late"] or (not item["buy_lower"] and not item["buy_upper"]):
            return market.quote(item["stock_code"])
        from .history import bar_time, valid_bar

        target = local(item["target_buy_at"])
        rows = market.bars(
            item["stock_code"],
            target - timedelta(minutes=1),
            target + timedelta(minutes=2),
            period="1m",
            adjustment="none",
        )
        bar = next((row for row in rows if valid_bar(row) and bar_time(row) == target), None)
        if bar is None:
            raise PredictionError("历史目标分钟行情不可用")
        price = bar["close"]
        if bar.get("amount", 0) > 0 and bar.get("volume", 0) > 0:
            average = bar["amount"] / bar["volume"]
            if bar["low"] * 0.8 < average < bar["high"] * 1.2:
                price = average
        result = {"price": price, "asOf": stamp(target), "source": bar.get("source", ""), "preClose": 0}
        try:
            quote = market.quote(item["stock_code"])
            if quote.get("asOf") and local(quote["asOf"]).replace(second=0, microsecond=0) == target.replace(
                second=0, microsecond=0
            ):
                result.update(
                    {
                        key: quote[key]
                        for key in ("preClose", "suspended", "limitUp", "limitDown")
                        if key in quote
                    }
                )
        except (OSError, ValueError, RuntimeError):
            pass  # A legacy exact minute remains usable without a concurrent realtime quote.
        return result

    async def process_trades(self, now=None):
        if self._trade_lock.locked():
            return
        async with self._trade_lock:
            now = local(now or self.clock())
            snapshot = self._snapshot()
            with self._provider(snapshot.config) as market:
                self._expire(now, snapshot.config.get("predictionAutoEnabled", True))
                if not continuous(now) or not await asyncio.to_thread(market.is_trading_day, now):
                    return
                await self._sells(market, now)
                if not snapshot.config.get("predictionAutoEnabled", True) or not slot_at(self.clock()):
                    return
                with self.database.connection() as connection:
                    items = many(
                        connection,
                        "SELECT r.*,a.chain_id,a.generated_at run_generated FROM research2_recommendations r JOIN research2_analysis_runs a ON a.run_id=r.analysis_run_id WHERE a.status='success' AND r.status IN ('buy_pending','standby') AND julianday(r.target_buy_at)<=julianday(?) ORDER BY julianday(coalesce(a.generated_at,a.started_at)),r.final_score DESC,r.stock_code,r.id",
                        (stamp(now),),
                    )
                groups = {}
                for item in items:
                    groups.setdefault(item["analysis_run_id"], []).append(item)
                for group in groups.values():
                    slot = group[0]["slot"]
                    chain_id = group[0]["chain_id"]
                    quotes = {}
                    pending = set()
                    valid = []
                    for item in group:
                        try:
                            quote = await asyncio.to_thread(self._buy_quote, market, item)
                        except (OSError, ValueError, RuntimeError) as error:
                            self.repo.mark_pending(
                                item["recommendation_id"], "等待有效买入行情：" + str(error)
                            )
                            pending.add(item["recommendation_id"])
                            valid.append(item)
                            continue
                        checked = self.clock()
                        if not slot_at(checked) or local(item["signal_at"]).date() != checked.date():
                            self._expire(checked, True)
                            break
                        failure = (
                            "suspended"
                            if quote.get("suspended")
                            else "limit_up"
                            if quote.get("limitUp")
                            else "limit_down"
                            if quote.get("limitDown")
                            else "invalid_price"
                            if not positive(quote.get("price"))
                            else ""
                        )
                        limit = upper_limit(quote.get("preClose", 0))
                        distance = (limit - quote.get("price", 0)) / limit * 100 if limit else None
                        if not failure and distance is not None and distance + 1e-9 < 1:
                            failure = "near_limit_up"
                        if failure:
                            self.repo.set(
                                "recommendations",
                                {
                                    "status": "missed_untradable",
                                    "execution_failure_code": failure,
                                    "failure_reason": "本报告跳过不可验证成交：" + failure,
                                    "execution_quote_price": quote.get("price", 0),
                                    "execution_quote_at": stamp(quote.get("asOf")),
                                    "execution_limit_price": limit,
                                    "execution_limit_distance_pct": distance,
                                },
                                "recommendation_id=? AND status IN ('buy_pending','standby')",
                                (item["recommendation_id"],),
                            )
                            continue
                        not_before = max(local(item["signal_at"]), local(item["target_buy_at"])).replace(
                            microsecond=0
                        )
                        if (
                            (bool(chain_id) and not positive(quote.get("preClose")))
                            or not quote.get("asOf")
                            or (
                                (item["late"] or (not item["buy_lower"] and not item["buy_upper"]))
                                and not fresh_quote(quote.get("asOf"), checked)
                            )
                            or local(quote["asOf"]) < not_before
                        ):
                            self.repo.mark_pending(
                                item["recommendation_id"], "等待报告后的新行情与可验证前收盘价", quote
                            )
                            pending.add(item["recommendation_id"])
                        else:
                            quotes[item["recommendation_id"]] = quote
                            self.repo.set(
                                "recommendations",
                                {
                                    "execution_quote_price": quote["price"],
                                    "execution_quote_at": stamp(quote["asOf"]),
                                    "execution_limit_price": limit,
                                    "execution_limit_distance_pct": distance,
                                    "execution_failure_code": "",
                                    "failure_reason": "",
                                },
                                "recommendation_id=?",
                                (item["recommendation_id"],),
                            )
                        valid.append(item)
                    reserved = 0
                    for item in valid:
                        with self.database.connection() as connection:
                            remaining = 5 - day_count(connection, slot, now.date().isoformat())
                        if remaining <= reserved:
                            break
                        if item["recommendation_id"] in pending:
                            reserved += 1
                            continue
                        quote = quotes[item["recommendation_id"]]
                        sell_at = await asyncio.to_thread(
                            next_session, market, local(item["target_buy_at"]), slot
                        )
                        current = item["late"] or (not item["buy_lower"] and not item["buy_upper"])
                        if current and not fresh_quote(quote["asOf"], self.clock()):
                            self.repo.mark_pending(item["recommendation_id"], "其他候选采集期间行情已过期")
                            reserved += 1
                            continue
                        try:
                            self.repo.buy(item["recommendation_id"], quote, sell_at, current=bool(current))
                        except Conflict as error:
                            self.repo.set(
                                "recommendations",
                                {"status": "analysis_only", "failure_reason": str(error)},
                                "recommendation_id=? AND status IN ('buy_pending','standby')",
                                (item["recommendation_id"],),
                            )
                        except PredictionError as error:
                            self.repo.set(
                                "recommendations",
                                {"status": "missed_cash", "failure_reason": str(error)},
                                "recommendation_id=? AND status IN ('buy_pending','standby')",
                                (item["recommendation_id"],),
                            )
                    if chain_id:
                        self.repo.finish_chain(chain_id)

    async def refresh_quotes(self, items=None):
        snapshot = self._snapshot()
        with self._provider(snapshot.config) as market:
            items = (
                items
                if items is not None
                else self.repo.rows("recommendations", "status IN ('active','sell_pending')")
            )
            gate = asyncio.Semaphore(6)

            async def refresh(item):
                if item["status"] not in ("active", "sell_pending"):
                    return
                async with gate:
                    try:
                        quote = await asyncio.to_thread(market.quote, item["stock_code"])
                    except (OSError, ValueError, RuntimeError):
                        return
                    if not positive(quote.get("price")) or not quote.get("asOf") or not local(quote["asOf"]):
                        return
                    self.repo.set(
                        "recommendations",
                        {"current_price": quote["price"], "current_price_at": stamp(quote["asOf"])},
                        "recommendation_id=? AND status IN ('active','sell_pending') AND (current_price_at IS NULL OR julianday(current_price_at)<julianday(?))",
                        (item["recommendation_id"], stamp(quote["asOf"])),
                    )

            await asyncio.gather(*(refresh(item) for item in items))

    def list_runs(self, *args, **kwargs):
        return self.views.runs(*args, **kwargs)

    def browse_runs(self, *args, **kwargs):
        return self.views.browse_runs(*args, **kwargs)

    def get_run(self, identity):
        return self.views.run(identity)

    def list_recommendations(self, *args, **kwargs):
        return self.views.recommendations(*args, **kwargs)

    def get_recommendation(self, identity):
        return self.views.recommendation(identity)

    def account(self, slot=DEFAULT_SLOT):
        return self.views.account(slot)

    def performance(self, slot=DEFAULT_SLOT):
        return self.views.performance(slot)

    def portfolio_performance(self, *args, **kwargs):
        return self.views.portfolio(*args, **kwargs)

    def slots(self, now=None):
        return self.views.slots(now)

    async def deliver_emails(self, now=None):
        await self.email.process(self._snapshot().config)

    async def recover(self, now=None, *, resume=True):
        now = local(now or self.clock())
        self.repo.ready()
        interrupted = self.repo.rows("analysis_runs", "status='running'")
        for run in interrupted:
            reason = "服务重启时发现上次分析未完成，已恢复为新的分析轮次"
            self.repo.set(
                "analysis_runs",
                {"status": "failed", "generated_at": stamp(now), "failure_reason": reason},
                "run_id=?",
                (run["run_id"],),
            )
            with self.database.connection() as connection:
                audit_state = one(
                    connection,
                    "SELECT status FROM research_audit_run_states WHERE owner_type='research2' AND owner_id=?",
                    (run["run_id"],),
                )
            if audit_state and audit_state["status"] == "capturing":
                self.audit.fail(run["run_id"], reason)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE research_evidence_sets SET status='failed',frozen_at=? WHERE owner_type='research2' AND status='collecting' AND owner_id IN (SELECT run_id FROM research2_analysis_runs WHERE status='failed')",
                (stamp(now),),
            )
        self._expire(now, self._snapshot().config.get("predictionAutoEnabled", True))
        if resume:
            await self.tick(now)

    def _launch(self, key, coroutine):
        task = asyncio.create_task(coroutine, name="prediction:" + key)
        self._tasks[key] = task

        def done(completed):
            self._tasks.pop(key, None)
            if not completed.cancelled() and completed.exception():
                log.error("prediction task %s failed: %s", key, completed.exception())

        task.add_done_callback(done)

    async def _scheduled_analysis(self, scheduled):
        await self.analyze(scheduled)
        await self.process_trades()
        await self.deliver_emails()

    async def tick(self, now=None):
        now = local(now or self.clock())
        slot = slot_at(now)
        snapshot = self._snapshot()
        pulse = now.replace(second=0 if now.second < 5 else 5, microsecond=0)
        new_pulse = pulse != self._last_trade
        if (
            slot
            and now.weekday() < 5
            and self._non_trading_date != now.date()
            and snapshot.config.get("predictionAutoEnabled", True)
            and new_pulse
        ):
            key = now.date().isoformat() + ":" + slot
            if key not in self._tasks:
                self._launch(key, self._scheduled_analysis(slot_time(now, slot)))
        if new_pulse and now.weekday() < 5 and 9 <= now.hour <= 15 and "trades" not in self._tasks:
            self._last_trade = pulse
            self._launch("trades", self.process_trades(now))
        if self._last_email is None or (now - self._last_email).total_seconds() >= 30:
            self._last_email = now
            if "email" not in self._tasks:
                self._launch("email", self.deliver_emails(now))
        if now.weekday() < 5 and (now.hour, now.minute) >= (15, 5) and self._last_metric != now.date():
            self._last_metric = now.date()
            self._launch("metrics", self.finalize_metrics(now))

    async def close(self):
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def finalize_metrics(self, now=None):
        from .history import HistoryService

        async with self._metric_lock:
            with self._provider(self._snapshot().config) as market:
                return await asyncio.to_thread(
                    HistoryService(self.repo, market).backfill, local(now or self.clock()).date().isoformat()
                )

    async def backfill_performance(self):
        from .history import HistoryService

        async with self._metric_lock:
            with self._provider(self._snapshot().config) as market:
                return await asyncio.to_thread(HistoryService(self.repo, market).backfill)

    async def replay_allocations(self, dry_run=True):
        from .replay import AllocationReplay

        async with self._trade_lock:
            with self._provider(self._snapshot().config) as market:
                return await asyncio.to_thread(AllocationReplay(self.repo, market).run, dry_run)

    async def chart(self, identity, refresh=False):
        from .history import recommendation_chart

        lock = self._chart_locks.setdefault(identity, asyncio.Lock())
        if refresh and lock.locked():
            raise Conflict("图表刷新正在进行")
        async with lock:
            if not refresh and identity in self._chart_cache:
                return copy.deepcopy(self._chart_cache[identity])
            with self._provider(self._snapshot().config) as market:
                result = await asyncio.to_thread(recommendation_chart, self.repo, market, identity, refresh)
            self._chart_cache[identity] = copy.deepcopy(result)
            return result
