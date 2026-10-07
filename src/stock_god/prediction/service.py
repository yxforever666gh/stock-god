"""BASE43 orchestration. Providers and settings are immutable task snapshots."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from contextlib import contextmanager
from datetime import timedelta

from .base43 import Base43Models, feature_candidate
from .core import DEFAULT_SLOT, Conflict, PredictionError, json_text, local, next_session, positive, stamp
from .email import EmailService, queue_published
from .repository import Repository
from .views import Views

log = logging.getLogger(__name__)


class PredictionService:
    def __init__(
        self,
        database,
        market,
        settings,
        ai_factory,
        audit,
        *,
        clock=None,
        mailer=None,
        evidence_store=None,
        auction_source=None,
        models=None,
    ):
        self.database, self.market, self.settings, self.audit = database, market, settings, audit
        self.clock = clock or local
        self.repo = Repository(database, self.clock)
        self.views = Views(self.repo, lambda day: self.market.is_trading_day(day))
        self.email = EmailService(self.repo, settings, mailer)
        self.models = models or Base43Models(database)
        self.auction_source = auction_source
        self._trade_lock, self._metric_lock = asyncio.Lock(), asyncio.Lock()
        self._tasks, self._chart_locks, self._chart_cache = {}, {}, {}
        self._last_email = None
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

    @contextmanager
    def _source(self, config):
        source = self.auction_source.with_settings(copy.deepcopy(config)) if self.auction_source else None
        try:
            yield source
        finally:
            if source is not None and hasattr(source, "close"):
                source.close()

    def on_settings_changed(self, before, after):
        config = after.config if hasattr(after, "config") else after
        if not config.get("predictionAutoEnabled", True):
            self._expire(local(self.clock()), False)

    def _task(self, day, kind, status, payload=None, error=None):
        with self.database.transaction() as con:
            con.execute(
                "INSERT INTO research2_base43_daily_tasks VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(trading_date,task_type) DO UPDATE SET status=excluded.status, "
                "completed_at=excluded.completed_at,payload_json=excluded.payload_json,error=excluded.error",
                (
                    day,
                    kind,
                    status,
                    stamp(self.clock()),
                    stamp(self.clock()) if status != "running" else None,
                    json_text(payload or {}),
                    error,
                ),
            )

    def _task_done(self, day, kind):
        with self.database.connection() as con:
            row = con.execute(
                "SELECT status FROM research2_base43_daily_tasks WHERE trading_date=? AND task_type=?",
                (day, kind),
            ).fetchone()
        return row is not None and row[0] in ("success", "blocked", "failed")

    @staticmethod
    def _previous(market, now):
        for delta in range(1, 21):
            at = now - timedelta(days=delta)
            if market.is_trading_day(at):
                return at.date().isoformat()
        raise PredictionError("缺少前交易日")

    async def prepare_day(self, now=None):
        now = local(now or self.clock())
        day = now.date().isoformat()
        if self._task_done(day, "prepare"):
            return
        self._task(day, "prepare", "running")
        try:
            snapshot = self._snapshot()
            with self._provider(snapshot.config) as market:
                if not await asyncio.to_thread(market.is_trading_day, now):
                    self._task(day, "prepare", "blocked", error="非交易日")
                    return
                await asyncio.to_thread(self.models.prepare, day)
                with self._source(snapshot.config) as source:
                    if source:
                        previous = await asyncio.to_thread(self._previous, market, now)
                        await asyncio.to_thread(source.prepare, day, previous)
            self._task(day, "prepare", "success")
        except Exception:
            self._task(day, "prepare", "failed", error="BASE43盘前准备失败")
            log.error("BASE43 preparation failed; provider details suppressed")

    async def analyze(self, scheduled_for=None, *, diagnostic=False, parent_run_id=None):
        now = local(self.clock())
        scheduled = local(scheduled_for or now)
        if diagnostic or parent_run_id:
            raise Conflict("BASE43不支持旧AI重跑或诊断交易入口")
        if not (9, 29, 55) <= (now.hour, now.minute, now.second) <= (9, 29, 59):
            raise Conflict("不在BASE43盘前冻结窗口")
        day = scheduled.date().isoformat()
        if self._task_done(day, "freeze"):
            return None
        snapshot = self._snapshot()
        if not snapshot.config.get("predictionAutoEnabled", True):
            raise Conflict("股票预测自动策略已关闭")
        with self._source(snapshot.config) as source:
            if source is None or not source.status().get("ready"):
                self._task(day, "freeze", "blocked", error="竞价来源未配置或待核验，未执行选股")
                raise Conflict("竞价来源未配置或待核验，未执行选股")
            # Claim and freeze cash synchronously, before provider awaits.
            run, created = self.repo.claim_run(scheduled)
            if not created:
                return self.views.run(run["run_id"])
            self._task(day, "freeze", "running")
            try:
                with self._provider(snapshot.config) as market:
                    if not await asyncio.to_thread(market.is_trading_day, now):
                        raise Conflict("非交易日")
                cutoff = min(now, now.replace(hour=9, minute=29, second=59, microsecond=0))
                frozen = await asyncio.to_thread(source.freeze, day, cutoff)
                if not frozen.get("complete"):
                    raise Conflict("当日竞价母体或关键输入不完整，未执行选股")
                model_snapshot = await asyncio.to_thread(self.models.snapshot, day)
                if str(model_snapshot.dates[-1]) != day.replace("-", ""):
                    raise Conflict("当日滚动模型未准备完成")
                candidates, vectors = frozen.get("candidates", []), []
                for candidate in candidates:
                    for row in candidate.get("auctionRows", []):
                        received = local(row["receivedAt"])
                        if received > cutoff or received > now:
                            raise Conflict("竞价证据越过冻结接收时点")
                    vector = feature_candidate(candidate)
                    if vector is None:
                        raise Conflict("候选关键竞价证据不完整")
                    vectors.append(vector)
                    await asyncio.to_thread(
                        self.models.record_candidate, day, candidate["code"], vector, candidate
                    )
                scores = (
                    await asyncio.to_thread(self.models.predict, vectors, model_snapshot) if vectors else []
                )
                items = [
                    dict(
                        stock_code=c["code"],
                        stock_name=c.get("name", c["code"]),
                        final_score=float(score),
                        reference_price=next(
                            row["fields"][1]
                            for row in c["auctionRows"]
                            if row["time"] >= 33900
                            and all(positive(row["fields"][i]) for i in (1, 2, 3))
                            and abs(row["fields"][3] / row["fields"][2] - row["fields"][1]) <= 0.011
                        ),
                        execution_limit_price=c["upper"] / 100,
                    )
                    for c, score in zip(candidates, scores, strict=True)
                ]
                self.repo.set(
                    "analysis_runs",
                    {
                        "evidence_cutoff_at": stamp(now),
                        "provider_name": "BASE43",
                        "model_name": "HGB 五模型均值",
                    },
                    "run_id=?",
                    (run["run_id"],),
                )
                run["evidence_cutoff_at"] = stamp(now)

                def render(finalized, selected):
                    lines = [
                        "# BASE43 股票预测",
                        f"交易日：{day}；冻结时点：{stamp(cutoff)}",
                        "原43特征、五模型均值；正分前两名按盘前现金等分。",
                        "模型日期：" + "、".join(model_snapshot.dates),
                        "实时报价模拟成交；当前账户收益由成交账本计算，研究历史收益仅供参考。",
                        "",
                        "| 股票 | 预测净收益分数 | 席位预算 |",
                        "|---|---:|---:|",
                    ]
                    lines.extend(
                        f"| {row['stock_name']} {row['stock_code']} | {row['final_score']:.4f}% | {row['allocation_base_cash']:.2f}元 |"
                        for row in selected
                    )
                    if not selected:
                        lines.append("本日没有正分候选，保留现金。")
                    return "\n".join(lines)

                result = self.repo.publish_base43(run, items, render=render, queue_email=queue_published)
                self._task(
                    day,
                    "freeze",
                    "success",
                    {
                        "modelIdentity": model_snapshot.identity,
                        "factsSha256": frozen.get("factsSha256"),
                        "candidateCount": len(items),
                    },
                )
                return self.views.run(result["run_id"])
            except Exception:
                self.repo.set(
                    "analysis_runs",
                    {
                        "status": "failed",
                        "failure_reason": "BASE43证据或模型未通过冻结校验",
                        "generated_at": stamp(self.clock()),
                    },
                    "run_id=? AND persisted_at IS NULL",
                    (run["run_id"],),
                )
                self._task(day, "freeze", "failed", error="BASE43证据或模型未通过冻结校验")
                log.error("BASE43 freeze failed; provider details suppressed")
                return None

    async def rerun(self, identity):
        raise Conflict("归档报告只读；BASE43不支持旧AI重跑")

    def _expire(self, now, auto_enabled=True):
        day = now.date().isoformat()
        if (now.hour, now.minute) >= (9, 31) or not auto_enabled:
            self.repo.set(
                "recommendations",
                {"status": "analysis_only", "failure_reason": "买入窗口已截止或自动策略已关闭"},
                "slot=? AND status='buy_pending'",
                (DEFAULT_SLOT,),
            )
        else:
            self.repo.set(
                "recommendations",
                {"status": "analysis_only", "failure_reason": "买入席位已过期"},
                "slot=? AND status='buy_pending' AND date(target_buy_at,'+8 hours')<>?",
                (DEFAULT_SLOT, day),
            )

    async def process_trades(self, now=None):
        now = local(now or self.clock())
        snapshot = self._snapshot()
        self._expire(now, snapshot.config.get("predictionAutoEnabled", True))
        if not (9, 30) <= (now.hour, now.minute) < (15, 0):
            return
        async with self._trade_lock:
            with self._provider(snapshot.config) as market:
                if not await asyncio.to_thread(market.is_trading_day, now):
                    return
                with self._source(snapshot.config) as source:
                    if source is None:
                        return
                    day = now.date().isoformat()
                    for item in self.repo.rows(
                        "recommendations",
                        "slot=? AND status IN ('active','sell_pending','buy_pending')",
                        (DEFAULT_SLOT,),
                    ):
                        try:
                            buying = item["status"] == "buy_pending"
                            if buying and (
                                not snapshot.config.get("predictionAutoEnabled", True)
                                or (now.hour, now.minute) != (9, 30)
                            ):
                                continue
                            if not buying:
                                bought = local(item["buy_at"])
                                if now.date() <= bought.date():
                                    continue
                                first = await asyncio.to_thread(next_session, market, bought, DEFAULT_SLOT)
                                due = first
                                if now.date() == first.date():
                                    state = await asyncio.to_thread(
                                        source.exit_state, item["stock_code"], day, bought.date().isoformat()
                                    )
                                    if (
                                        not state.get("complete")
                                        or not positive(state.get("buyClose"))
                                        or not positive(state.get("auctionPrice"))
                                    ):
                                        continue
                                    if state["auctionPrice"] / state["buyClose"] - 1 < -0.03:
                                        due = first.replace(hour=10, minute=30)
                                else:
                                    due = now.replace(hour=9, minute=30, second=0, microsecond=0)
                                self.repo.set(
                                    "recommendations",
                                    {"target_sell_at": stamp(due)},
                                    "recommendation_id=? AND slot=?",
                                    (item["recommendation_id"], DEFAULT_SLOT),
                                )
                                if now < due:
                                    continue
                            rules = await asyncio.to_thread(source.rules, item["stock_code"], day)
                            if not rules.get("known") or rules.get("suspended") is not False:
                                continue
                            quote = dict(await asyncio.to_thread(market.quote, item["stock_code"]))
                            actual = local(self.clock())
                            quote.setdefault("receivedAt", stamp(actual))
                            quote.update(
                                suspended=False,
                                upperLimit=rules.get("upper", 0) / 100,
                                lowerLimit=rules.get("lower", 0) / 100,
                            )
                            if buying:
                                sell_at = await asyncio.to_thread(next_session, market, actual, DEFAULT_SLOT)
                                self.repo.buy_base43(item["recommendation_id"], quote, sell_at, actual)
                            else:
                                self.repo.sell_base43(item["recommendation_id"], quote, actual)
                        except (OSError, ValueError, RuntimeError):
                            continue

    async def refresh_quotes(self, items=None):
        items = (
            items
            if items is not None
            else self.repo.rows(
                "recommendations", "slot=? AND status IN ('active','sell_pending')", (DEFAULT_SLOT,)
            )
        )
        items = [
            i for i in items if i.get("slot") == DEFAULT_SLOT and i["status"] in ("active", "sell_pending")
        ]
        if not items:
            return
        with self._provider(self._snapshot().config) as market:
            for item in items:
                try:
                    quote = await asyncio.to_thread(market.quote, item["stock_code"])
                    now = local(self.clock())
                    at = local(quote["asOf"])
                    if (
                        not positive(quote.get("price"))
                        or at.date() != now.date()
                        or not 0 <= (now - at).total_seconds() <= 60
                    ):
                        continue
                    self.repo.set(
                        "recommendations",
                        {"current_price": quote["price"], "current_price_at": stamp(at)},
                        "recommendation_id=? AND slot=? AND status IN ('active','sell_pending') AND (current_price_at IS NULL OR julianday(current_price_at)<julianday(?))",
                        (item["recommendation_id"], DEFAULT_SLOT, stamp(at)),
                    )
                except (OSError, ValueError, RuntimeError):
                    continue

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
        with self._provider(self._snapshot().config) as market:
            return Views(self.repo, market.is_trading_day).portfolio(*args, **kwargs)

    def slots(self, now=None):
        result = self.views.slots(now)
        with self._source(self._snapshot().config) as source:
            status = (
                source.status()
                if source
                else {
                    "configured": False,
                    "ready": False,
                    "status": "unconfigured",
                    "message": "竞价API未配置，未执行选股",
                }
            )
            health = self.models.health()
            at = local(now or self.clock())
            day = at.date().isoformat()
            current_ready = bool(health.get("ready")) and day.replace("-", "") in health.get("modelDates", [])
            with self.database.connection() as con:
                prepare = con.execute(
                    "SELECT status FROM research2_base43_daily_tasks WHERE trading_date=? AND task_type='prepare'",
                    (day,),
                ).fetchone()
            if prepare and prepare[0] == "failed":
                current_ready = False
            for row in result:
                if row["slot"] == DEFAULT_SLOT:
                    row.update(
                        auctionSourceConfigured=status.get("configured", False),
                        modelReady=current_ready,
                        auctionSourceStatus=status.get("status", "unconfigured"),
                        auctionSourceMessage=status.get("message", ""),
                    )
            return result

    async def deliver_emails(self, now=None):
        await self.email.process(self._snapshot().config)

    async def recover(self, now=None, *, resume=True):
        now = local(now or self.clock())
        self.repo.ready()
        await asyncio.to_thread(self.models.bootstrap)
        if not self.models.health().get("ready"):
            raise RuntimeError("BASE43模型身份检查失败")
        self.repo.set(
            "analysis_runs",
            {
                "status": "failed",
                "failure_reason": "BASE43冻结任务中断，不补发买单",
                "generated_at": stamp(now),
            },
            "scheduled_slot=? AND status='running'",
            (DEFAULT_SLOT,),
        )
        with self.database.transaction() as con:
            con.execute(
                "UPDATE research2_base43_daily_tasks SET status='failed',error='服务重启时任务中断' WHERE status='running' AND task_type NOT LIKE 'source%'"
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
                log.error("prediction task %s failed", key, exc_info=completed.exception())

        task.add_done_callback(done)

    async def _scheduled_freeze(self, now):
        try:
            await self.analyze(now)
        except Conflict:
            pass  # The durable blocked receipt is the business state, not a fake report.

    async def _poll(self, now):
        with self._source(self._snapshot().config) as source:
            if source:
                holdings = self.repo.rows(
                    "recommendations", "slot=? AND status IN ('active','sell_pending')", (DEFAULT_SLOT,)
                )
                await asyncio.to_thread(
                    source.poll, now.date().isoformat(), now, [r["stock_code"] for r in holdings]
                )

    async def tick(self, now=None):
        now = local(now or self.clock())
        day = now.date().isoformat()
        enabled = self._snapshot().config.get("predictionAutoEnabled", True)
        if now.weekday() < 5:
            if (
                (9, 0) <= (now.hour, now.minute) < (9, 15)
                and not self._task_done(day, "prepare")
                and "prepare" not in self._tasks
            ):
                self._launch("prepare", self.prepare_day(now))
            if (9, 15) <= (now.hour, now.minute) < (9, 30) and "poll" not in self._tasks:
                self._launch("poll", self._poll(now))
            if (
                enabled
                and (9, 29, 55) <= (now.hour, now.minute, now.second) <= (9, 29, 59)
                and "freeze" not in self._tasks
                and not self._task_done(day, "freeze")
            ):
                self._launch("freeze", self._scheduled_freeze(now))

            if (
                (now.hour, now.minute) >= (15, 5)
                and "metrics" not in self._tasks
                and not self._task_done(day, "close")
            ):
                self._launch("metrics", self.finalize_metrics(now))
        if now.weekday() < 5 and (9, 30) <= (now.hour, now.minute) < (15, 0) and "trades" not in self._tasks:
            self._launch("trades", self.process_trades(now))
        self._expire(now, enabled)
        if self._last_email is None or (now - self._last_email).total_seconds() >= 30:
            self._last_email = now
            if "email" not in self._tasks:
                self._launch("email", self.deliver_emails(now))

    async def close(self):
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def finalize_metrics(self, now=None):
        from .base43_reference import (
            ReferenceInputs,
            reference_buy_terms,
            reference_costs,
            reference_sell_terms,
        )
        from .history import HistoryService

        now = local(now or self.clock())
        day = now.date().isoformat()
        self._task(day, "close", "running")
        try:
            async with self._metric_lock:
                with self._provider(self._snapshot().config) as market:
                    if not await asyncio.to_thread(market.is_trading_day, now):
                        self._task(day, "close", "blocked")
                        return
                    with self._source(self._snapshot().config) as source:
                        if source:
                            with self.database.connection() as con:
                                samples = con.execute(
                                    "SELECT trade_date,evidence_json FROM research2_base43_samples WHERE label_status IN ('pending','unknown') AND trade_date<? ORDER BY trade_date,code",
                                    (day.replace("-", ""),),
                                ).fetchall()
                            groups = {}
                            for entry_day, raw in samples:
                                candidate = json.loads(raw)
                                if candidate.get("source") == "frozen_raw_roi" or not candidate.get("code"):
                                    continue
                                groups.setdefault(entry_day, []).append(candidate)
                            for entry_day, candidates in groups.items():
                                maturity = await asyncio.to_thread(
                                    next_session, market, local(entry_day), DEFAULT_SLOT
                                )
                                if maturity.date() > now.date():
                                    continue
                                inputs = ReferenceInputs(
                                    market, source, entry_day, maturity.date().isoformat(), candidates
                                )
                                await asyncio.to_thread(
                                    self.models.mature_pending,
                                    inputs,
                                    entry_day,
                                    maturity.date().isoformat(),
                                    reference_costs,
                                    reference_buy_terms,
                                    reference_sell_terms,
                                )
                        following = await asyncio.to_thread(next_session, market, now, DEFAULT_SLOT)
                        await asyncio.to_thread(self.models.prepare, following.date().isoformat())
                        await asyncio.to_thread(
                            HistoryService(
                                self.repo, market, rules_provider=source.rules if source else None
                            ).backfill,
                            day,
                        )
            self._task(day, "close", "success")
        except Exception:
            self._task(day, "close", "failed", error="BASE43收盘训练或收益结算失败")
            log.error("BASE43 close task failed; provider details suppressed")

    async def backfill_performance(self):
        from .history import HistoryService

        async with self._metric_lock:
            with self._provider(self._snapshot().config) as market:
                with self._source(self._snapshot().config) as source:
                    return await asyncio.to_thread(
                        HistoryService(
                            self.repo, market, rules_provider=source.rules if source else None
                        ).backfill
                    )

    async def replay_allocations(self, dry_run=True):
        raise Conflict("旧账户已归档，禁止交易重放")

    async def chart(self, identity, refresh=False):
        item = self.repo.row("recommendations", "recommendation_id=?", (identity,))
        if refresh and item["slot"] != DEFAULT_SLOT:
            raise Conflict("归档图表仅支持只读查询")
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
