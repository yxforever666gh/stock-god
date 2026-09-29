"""Queries and return calculations over the durable prediction ledger."""

from __future__ import annotations

from datetime import date
from typing import Any

from .core import DEFAULT_SLOT, SLOTS, PredictionError, dto, local, stamp, valid_slot
from .repository import many, one, overview, recommendation_dto

DAILY_SELECTION = """WITH selection_days AS (
 SELECT r.*,date(coalesce(buy_at,signal_at),'+8 hours') display_day,
 CASE WHEN r.buy_at IS NOT NULL THEN 0 WHEN r.status IN ('buy_pending','standby') THEN 1 ELSE 2 END priority,
 CASE WHEN a.status IN ('success','no_recommendation') THEN 1 ELSE 0 END valid_report
 FROM research2_recommendations r LEFT JOIN research2_analysis_runs a ON a.run_id=r.analysis_run_id
), unique_stocks AS (
 SELECT *,row_number() OVER (PARTITION BY slot,display_day,stock_code
 ORDER BY priority,final_score DESC,julianday(signal_at) DESC,id) stock_row
 FROM selection_days WHERE valid_report=1 OR priority=0
), ranked AS (
 SELECT id,row_number() OVER (PARTITION BY slot,display_day ORDER BY priority,final_score DESC,stock_code,id) daily_rank
 FROM unique_stocks WHERE stock_row=1
)
"""

RUN_SUMMARY_FIELDS = """scheduled_slot slot published archive_reason persisted_at run_id trading_date
attempt_no chain_id parent_run_id trigger_source requested_slots primary_count standby_count
scheduled_for started_at evidence_window_start_at evidence_cutoff_at evidence_coverage_pct
degraded generated_at status provider_name model_name strategy_version evidence_profile_version
evidence_set_id recommendation_count on_time failure_reason""".split()


def portfolio_query(slots=None, from_date="", to_date=""):
    selected = sorted(set(slots or SLOTS))
    for slot in selected:
        valid_slot(slot)
    for value in (from_date, to_date):
        if value:
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError()
            except ValueError:
                raise PredictionError("收益日期必须为 YYYY-MM-DD") from None
    if from_date and to_date and from_date > to_date:
        raise PredictionError("开始日期不能晚于结束日期")
    return selected, from_date, to_date


class Views:
    def __init__(self, repository):
        self.repo = repository

    def run(self, identity):
        row = self.repo.row("analysis_runs", "run_id=?", (identity,))
        result = dto(row)
        deliveries = self.repo.rows("email_deliveries", "analysis_run_id=?", (identity,))
        if deliveries:
            delivery = deliveries[0]
            result.update(
                emailDeliveryStatus=delivery["status"],
                emailSentAt=stamp(delivery["sent_at"]),
                emailAttemptCount=delivery["attempt_count"],
                emailLastError=delivery["last_error"],
            )
        chains = (
            self.repo.rows("execution_chains", "chain_id=?", (row["chain_id"],)) if row["chain_id"] else []
        )
        if chains:
            result["executionChain"] = dto(chains[0])
        return result

    def runs(self, limit=100, offset=0, slot=""):
        if slot:
            valid_slot(slot)
        rows = self.repo.rows(
            "analysis_runs",
            "status<>'skipped_non_trading_day' AND "
            "(slot=? OR (coalesce(slot,'')='' AND scheduled_slot=?))" if slot else "status<>'skipped_non_trading_day'",
            (slot, slot) if slot else (),
            "julianday(scheduled_for) DESC,id DESC",
        )
        result = []
        for row in rows[max(0, offset) : max(0, offset) + min(500, max(1, limit))]:
            value = self.run(row["run_id"])
            for field in ("reportMarkdown", "sourceStatusJson", "modelAttemptLogJson"):
                value.pop(field, None)
            result.append(value)
        return result

    def browse_runs(self, page=1, day="recent5", all_reports=True):
        if day not in {"recent5", "all"}:
            try:
                if date.fromisoformat(day).isoformat() != day:
                    raise ValueError()
            except ValueError:
                raise PredictionError("交易日必须为 YYYY-MM-DD") from None
        with self.repo.db.connection() as connection:
            dates = [
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT trading_date FROM research2_analysis_runs "
                    "WHERE trading_date<>'' AND status<>'skipped_non_trading_day' ORDER BY trading_date DESC"
                )
            ]
            where, params = ["r.status<>'skipped_non_trading_day'"], []
            if day == "recent5":
                recent = dates[:5]
                where.append("r.trading_date IN (" + ",".join("?" for _ in recent) + ")" if recent else "0")
                params.extend(recent)
            elif day != "all":
                where.append("r.trading_date=?")
                params.append(day)
            if not all_reports:
                where.append("r.published=1")
            condition = " AND ".join(where) if where else "1"
            total = connection.execute(
                f"SELECT count(*) FROM research2_analysis_runs r WHERE {condition}", params
            ).fetchone()[0]
            fields = ",".join("r." + field for field in RUN_SUMMARY_FIELDS)
            rows = many(
                connection,
                f"SELECT {fields},e.status email_delivery_status,e.sent_at email_sent_at,"
                "e.attempt_count email_attempt_count,e.last_error email_last_error "
                "FROM research2_analysis_runs r LEFT JOIN research2_email_deliveries e "
                f"ON e.analysis_run_id=r.run_id WHERE {condition} "
                "ORDER BY julianday(r.scheduled_for) DESC,r.id DESC LIMIT 100 OFFSET ?",
                (*params, (page - 1) * 100),
            )
        items = []
        for row in rows:
            value = dto(row)
            if row["email_delivery_status"] is None:
                for field in ("emailDeliveryStatus", "emailSentAt", "emailAttemptCount", "emailLastError"):
                    value.pop(field, None)
            items.append(value)
        return {"items": items, "total": total, "page": page, "pageSize": 100, "tradingDates": dates}

    def recommendations(
        self,
        limit=200,
        offset=0,
        slot=DEFAULT_SLOT,
        *,
        slots=None,
        from_date="",
        to_date="",
        bought_only=False,
    ):
        limit = min(500, max(1, limit))
        offset = max(0, offset)
        with self.repo.db.connection() as connection:
            if bought_only or slots:
                slots, from_date, to_date = portfolio_query(slots, from_date, to_date)
                where = "slot IN (" + ",".join("?" for _ in slots) + ") AND buy_at IS NOT NULL"
                args = list(slots)
                if from_date:
                    where += " AND date(buy_at,'+8 hours')>=?"
                    args.append(from_date)
                if to_date:
                    where += " AND date(buy_at,'+8 hours')<=?"
                    args.append(to_date)
                rows = many(
                    connection,
                    f"SELECT * FROM research2_recommendations WHERE {where} ORDER BY julianday(buy_at) DESC,id DESC LIMIT ? OFFSET ?",
                    (*args, limit, offset),
                )
            else:
                valid_slot(slot or DEFAULT_SLOT)
                rows = many(
                    connection,
                    DAILY_SELECTION
                    + "SELECT d.*,r.daily_rank display_selection_rank,'' display_selection_role FROM selection_days d JOIN ranked r ON r.id=d.id WHERE d.slot=? AND r.daily_rank<=5 ORDER BY d.display_day DESC,r.daily_rank,d.stock_code,d.id LIMIT ? OFFSET ?",
                    (slot or DEFAULT_SLOT, limit, offset),
                )
        for row in rows:
            for key in ("display_day", "priority", "valid_report"):
                row.pop(key, None)
        return [recommendation_dto(row) for row in rows]

    def recommendation(self, identity):
        with self.repo.db.connection() as connection:
            row = one(
                connection,
                DAILY_SELECTION
                + "SELECT r.*,CASE WHEN n.daily_rank<=5 THEN n.daily_rank ELSE 0 END display_selection_rank,'' display_selection_role FROM research2_recommendations r LEFT JOIN ranked n ON n.id=r.id WHERE r.recommendation_id=?",
                (identity,),
                True,
            )
        return {
            "recommendation": recommendation_dto(row),
            "analysis": self.run(row["analysis_run_id"]),
            "trades": [
                dto(trade)
                for trade in self.repo.rows(
                    "trades", "recommendation_id=?", (identity,), "julianday(traded_at),id"
                )
            ],
        }

    def account(self, slot=DEFAULT_SLOT):
        with self.repo.db.connection() as connection:
            return overview(connection, slot or DEFAULT_SLOT, self.repo.clock())

    def performance(self, slot=DEFAULT_SLOT):
        slot = slot or DEFAULT_SLOT
        result = self.account(slot)
        recommendations = self.repo.rows("recommendations", "slot=? AND status='closed'", (slot,))
        wins = sum((r["net_pn_l"] or 0) > 0 for r in recommendations)
        trades = self.repo.rows("trades", "slot=?", (slot,))
        reports = self.repo.rows(
            "analysis_runs", "slot=? AND published=1 AND status IN ('success','no_recommendation')", (slot,)
        )
        curve = [
            dto(r)
            for r in self.repo.rows(
                "account_ledger_snapshots",
                "slot=? AND snapshot_type IN ('trade','initial_external','top_up_external','legacy_pool_transfer')",
                (slot,),
                "julianday(valued_at),id",
            )
        ]
        curve.append(
            dict(
                snapshotId="current-" + slot,
                slot=slot,
                valuedAt=stamp(self.repo.clock()),
                tradingDate=self.repo.clock().date().isoformat(),
                snapshotType="current",
                **{
                    key: result[key]
                    for key in (
                        "cash",
                        "positionValue",
                        "netAssetValue",
                        "netProfit",
                        "cumulativeExternalCapital",
                        "netInternalTransfer",
                        "cumulativeCapitalReturn",
                        "valuationBasis",
                    )
                },
            )
        )
        wealth = peak = 1.0
        maximum = previous_nav = previous_funding = 0.0
        for point in curve:
            point["returnRate"] = point["cumulativeCapitalReturn"]
            funding = point["cumulativeExternalCapital"] + point["netInternalTransfer"]
            if previous_nav > 0:
                wealth *= max(0, (point["netAssetValue"] - (funding - previous_funding)) / previous_nav)
            peak = max(peak, wealth)
            maximum = max(maximum, (peak - wealth) / peak)
            previous_nav, previous_funding = point["netAssetValue"], funding
        result.update(
            closedTrades=len(recommendations),
            winningTrades=wins,
            winRate=wins / len(recommendations) if recommendations else None,
            totalFees=sum(
                (r["commission"] or 0) + (r["stamp_duty"] or 0) + (r["transfer_fee"] or 0) for r in trades
            ),
            maxDrawdown=maximum,
            onTimeReports=sum(bool(r["on_time"]) for r in reports),
            lateReports=sum(not r["on_time"] for r in reports),
            curve=curve,
        )
        return result

    def portfolio(self, slots=None, from_date="", to_date=""):
        slots, from_date, to_date = portfolio_query(slots, from_date, to_date)
        result: dict[str, Any] = dict(
            slots=slots,
            **{"from": from_date, "to": to_date},
            selectedAccountCount=len(slots),
            effectiveAccountCount=0,
            incompleteAccountCount=0,
            noActivityAccountCount=0,
            periodReturn=None,
            boughtTrades=0,
            closedTrades=0,
            winningTrades=0,
            winRate=None,
            classifiedTrades=0,
            pendingOutcomeCount=0,
            curve=[],
        )
        bought = self.repo.rows("recommendations", "buy_at IS NOT NULL")
        selected = [
            r
            for r in bought
            if r["slot"] in slots
            and (not from_date or local(r["buy_at"]).date().isoformat() >= from_date)
            and (not to_date or local(r["buy_at"]).date().isoformat() <= to_date)
        ]
        result["boughtTrades"] = len(selected)
        closed = [r for r in selected if r["status"] == "closed"]
        result["closedTrades"] = len(closed)
        result["winningTrades"] = sum((r["net_pn_l"] or 0) > 0 for r in closed)
        if closed:
            result["winRate"] = result["winningTrades"] / len(closed)
        for outcome in ("sealed", "broken", "untouched"):
            count = sum(
                r["buy_day_limit_status"] == "complete" and r["buy_day_limit_outcome"] == outcome
                for r in selected
            )
            result[outcome] = {"count": count, "rate": None}
            result["classifiedTrades"] += count
        for outcome in ("sealed", "broken", "untouched"):
            if result["classifiedTrades"]:
                result[outcome]["rate"] = result[outcome]["count"] / result["classifiedTrades"]
        result["pendingOutcomeCount"] = len(selected) - result["classifiedTrades"]
        accounts = {}
        dates = set()
        for slot in slots:
            active = any(
                r["slot"] == slot
                and (not to_date or local(r["buy_at"]).date().isoformat() <= to_date)
                and (not from_date or not r["sell_at"] or local(r["sell_at"]).date().isoformat() >= from_date)
                for r in bought
            )
            if not active:
                result["noActivityAccountCount"] += 1
                continue
            rows = self.repo.rows("account_daily_valuations", "slot=?", (slot,), "trading_date")
            rows = [
                r
                for r in rows
                if (not from_date or r["trading_date"] >= from_date)
                and (not to_date or r["trading_date"] <= to_date)
            ]
            if not rows or any(r["data_status"] != "complete" or r["daily_return"] is None for r in rows):
                result["incompleteAccountCount"] += 1
                continue
            result["effectiveAccountCount"] += 1
            wealth = 1.0
            values = {}
            for row in rows:
                wealth *= max(0, 1 + row["daily_return"])
                values[row["trading_date"]] = wealth - 1
                dates.add(row["trading_date"])
            accounts[slot] = (values, wealth - 1)
        if accounts:
            result["periodReturn"] = sum(value[1] for value in accounts.values()) / len(accounts)
            latest = {}
            for day in sorted(dates):
                for slot, (values, _) in accounts.items():
                    if day in values:
                        latest[slot] = values[day]
                result["curve"].append(
                    {
                        "tradingDate": day,
                        "returnRate": sum(latest.values()) / len(latest),
                        "effectiveAccountCount": len(latest),
                        "incompleteAccountCount": result["incompleteAccountCount"],
                    }
                )
        return result

    def slots(self, at=None):
        now = local(at or self.repo.clock())
        day = now.date().isoformat()
        chains = {r["slot"]: r for r in self.repo.rows("execution_chains", "trading_date=?", (day,))}
        runs = {r["run_id"]: r for r in self.repo.rows("analysis_runs", "trading_date=?", (day,))}
        recommendations = self.repo.rows("recommendations")
        result = []
        for slot in SLOTS:
            chain = chains.get(slot, {})
            run = runs.get(chain.get("winner_run_id"), {})
            report = (
                run.get("status")
                if run.get("status") in ("success", "no_recommendation", "failed")
                else chain.get("status")
                if chain.get("status") in ("failed", "disabled", "cutoff")
                else "awaiting"
            )
            belonging = {
                r["run_id"] for r in runs.values() if r["chain_id"] and r["chain_id"] == chain.get("chain_id")
            }
            pending = sum(
                r["analysis_run_id"] in belonging and r["status"] in ("buy_pending", "standby")
                for r in recommendations
            )
            opened = sum(
                r["analysis_run_id"] in belonging and r["status"] in ("active", "sell_pending")
                for r in recommendations
            )
            filled = chain.get("filled_slots", 0)
            target = min(5, run.get("recommendation_count") or 5)
            status = chain.get("status", "awaiting")
            buy = (
                status
                if status in ("cutoff", "disabled", "failed")
                else "no_recommendation"
                if report == "no_recommendation"
                else "awaiting_report"
                if report != "success"
                else "awaiting_quote"
                if pending
                else "bought_full"
                if filled >= target
                else "bought_partial"
                if filled
                else "processing"
                if status == "running"
                else "no_purchase"
            )
            result.append(
                {
                    "slot": slot,
                    "label": slot,
                    "tradingDate": day,
                    "winnerRunId": chain.get("winner_run_id", ""),
                    "sellCompletedAt": stamp(chain.get("sell_completed_at")),
                    "status": status,
                    "reportStatus": report,
                    "reportOnTime": bool(run["on_time"]) if run else None,
                    "buyStatus": buy,
                    "boughtCount": filled,
                    "buyTargetCount": target,
                    "pendingBuyCount": pending,
                    "openPositionCount": opened,
                    "stopReason": chain.get("stop_reason", ""),
                }
            )
        return result
