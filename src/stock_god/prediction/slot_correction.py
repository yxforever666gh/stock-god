"""Explicit start-slot correction over frozen reports and verified recommendations."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import defaultdict

from stock_god.jsonutil import dumps

from .core import Conflict, PredictionError, local, slot_at
from .evidence import parse_output, validate

POLICY = "start-slot-ownership-v1"
OLD_COLUMNS = ("rank", "code", "name", "score", "price", "lot_cost", "parts", "risk", "execution")


def _started_slot(run):
    try:
        started = local(run["started_at"])
    except (TypeError, ValueError):
        raise PredictionError("历史报告缺少有效启动时间") from None
    return slot_at(started) if started.date().isoformat() == run["trading_date"] else ""


def _can_publish(run, slot):
    if run["status"] not in ("success", "no_recommendation") or not slot:
        return False
    if run["trigger_source"] == "diagnostic" or "自动策略已关闭" in (run["archive_reason"] or ""):
        return False
    if not run["persisted_at"]:
        return False
    completed = local(run["persisted_at"])
    return completed.date().isoformat() == run["trading_date"] and slot_at(completed) != ""


def _audit_result(audit, run):
    payloads = audit.detail(run["run_id"])["payloads"]
    selected = next(
        (
            payload
            for payload in reversed(payloads)
            if payload.get("repairedResponse") or payload.get("rawResponse")
        ),
        None,
    )
    if selected is None:
        raise PredictionError("归档报告缺少冻结模型响应")
    output = parse_output(selected.get("repairedResponse") or selected.get("rawResponse"))
    evidence = json.loads(selected["evidenceSnapshot"])
    return output, evidence, selected.get("repairedResponseSha256") or selected.get("rawResponseSha256")


def _old_report_items(run, output):
    raw = {
        str(item.get("code") or "").lower(): item
        for item in output.get("recommendations", [])
        if isinstance(item, dict)
    }
    rows = []
    for line in (run["report_markdown"] or "").splitlines():
        cells = [part.strip() for part in line.strip().strip("|").split("|")]
        if (
            len(cells) < len(OLD_COLUMNS)
            or not cells[0].isdigit()
            or not re.fullmatch(r"(?:sh|sz)\d{6}", cells[1], re.I)
        ):
            continue
        rank, code = int(cells[0]), cells[1].lower()
        source = raw.get(code)
        if source is None:
            raise PredictionError("归档报告入选股票不在冻结模型响应中")
        parts = [float(part.strip()) for part in cells[6].split("/")]
        if len(parts) != 4:
            raise PredictionError("归档报告缺少完整分项评分")
        risk = float(cells[7])
        score = float(cells[3])
        if abs(sum(parts) - risk - score) > 0.011:
            raise PredictionError("归档报告分项与总分不一致")
        raw_score = sum(
            float(source.get(field) or 0)
            for field in ("marketScore", "sectorScore", "stockScore", "catalystScore")
        ) - float(source.get("riskDeduction") or 0)
        if abs(raw_score - score) > 0.011:
            raise PredictionError("冻结模型分数与已接受报告不一致")
        if abs(float(source.get("referencePrice") or 0) - float(cells[4])) > 0.011:
            raise PredictionError("冻结模型参考价与已接受报告不一致")
        rows.append(
            dict(
                rank=rank,
                code=code,
                name=cells[2],
                score=score,
                price=float(cells[4]),
                lot_cost=float(cells[5]),
                parts=parts,
                risk=risk,
                source=source,
            )
        )
    if len(rows) != run["recommendation_count"] or [row["rank"] for row in rows] != list(
        range(1, len(rows) + 1)
    ):
        raise PredictionError("归档报告入选数量或排名与持久记录不一致")
    return rows


def _restored_items(run, slot, audit):
    if not run["recommendation_count"]:
        return [], ""
    output, evidence, response_hash = _audit_result(audit, run)
    version = run["strategy_version"]
    if version in {"research2-slots-v11", "research2-slots-v12", "research2-slots-v13"}:
        accepted = _old_report_items(run, output)
        items = []
        for row in accepted:
            source = row["source"]
            market, sector, stock, catalyst = row["parts"]
            items.append(
                dict(
                    recommendation_id=str(
                        uuid.uuid5(
                            uuid.NAMESPACE_OID,
                            f"go-stock:research2:start-slot-v1:{run['run_id']}:{row['code']}",
                        )
                    ),
                    analysis_run_id=run["run_id"],
                    slot=slot,
                    stock_code=row["code"],
                    stock_name=row["name"],
                    selection_rank=row["rank"],
                    final_score=row["score"],
                    reference_price=row["price"],
                    estimated_lot_cost=row["lot_cost"],
                    market_score=market,
                    sector_score=sector,
                    stock_score=stock,
                    catalyst_score=catalyst,
                    risk_deduction=row["risk"],
                    source_refs="\n".join(dict.fromkeys(source.get("sourceRefs") or [])),
                    summary=source.get("summary") or "",
                    quant_data=source.get("quantData") or "",
                    fresh_catalyst=source.get("freshCatalyst") or "",
                    old_background=source.get("oldBackground") or "",
                    main_risk=source.get("mainRisk") or "",
                    cancel_conditions=source.get("cancelConditions") or "",
                    buy_lower=0,
                    buy_upper=0,
                    status="buy_pending",
                    signal_at=run["persisted_at"],
                    target_buy_at=run["persisted_at"],
                )
            )
    elif version == "prediction-slots-v13":
        items, warnings = validate(run["run_id"], evidence, output)
        if warnings or len(items) != run["recommendation_count"]:
            raise PredictionError("归档 Python 报告与冻结证据校验不一致")
        for item in items:
            item["recommendation_id"] = str(
                uuid.uuid5(
                    uuid.NAMESPACE_OID,
                    f"go-stock:research2:start-slot-v1:{run['run_id']}:{item['stock_code']}",
                )
            )
            item.update(
                slot=slot,
                status="buy_pending",
                signal_at=run["persisted_at"],
                target_buy_at=run["persisted_at"],
            )
    else:
        raise PredictionError("归档报告缺少已核对的历史策略版本")
    return items, response_hash


class StartSlotCorrection:
    def __init__(self, repository, audit):
        self.repo, self.audit = repository, audit

    def plan(self):
        runs = self.repo.rows("analysis_runs", order="julianday(coalesce(persisted_at,started_at)),id")
        existing = self.repo.rows("recommendations")
        by_run = defaultdict(list)
        for item in existing:
            by_run[item["analysis_run_id"]].append(item)
        slots = {run["run_id"]: _started_slot(run) for run in runs}
        winners = {}
        for run in runs:
            slot = slots[run["run_id"]]
            if _can_publish(run, slot):
                winners.setdefault((run["trading_date"], slot), run["run_id"])
        winning_ids = set(winners.values())
        updates = []
        restored = []
        responses = {}
        for run in runs:
            identity = run["run_id"]
            slot = slots[identity]
            winner = identity in winning_ids
            completed = local(run["persisted_at"]) if run["persisted_at"] else None
            on_time = bool(
                completed
                and slot == run["scheduled_slot"]
                and slot_at(completed) == slot
                and completed.date().isoformat() == run["trading_date"]
            )
            values: dict[str, object] = {"slot": slot}
            if run["status"] in ("success", "no_recommendation"):
                values.update(
                    published=winner,
                    chain_id=(
                        str(
                            uuid.uuid5(
                                uuid.NAMESPACE_OID,
                                f"go-stock:research2:execution-chain:{run['trading_date']}:{slot}",
                            )
                        )
                        if winner
                        else ""
                    ),
                    requested_slots=5 if winner else 0,
                    archive_reason=""
                    if winner
                    else (
                        run["archive_reason"]
                        if not _can_publish(run, slot)
                        else "启动区间已有先完成报告，仅保留报告"
                    ),
                    on_time=on_time,
                )
                if winner and not by_run[identity]:
                    rows, digest = _restored_items(run, slot, self.audit)
                    restored.extend(rows)
                    responses[identity] = digest
            updates.append((identity, values))
        payload = {
            "policy": POLICY,
            "updates": updates,
            "winners": sorted((day, slot, identity) for (day, slot), identity in winners.items()),
            "restored": restored,
            "responseHashes": responses,
        }
        payload["planHash"] = hashlib.sha256(dumps(payload, sort_keys=True).encode()).hexdigest()
        return payload

    def apply(self, plan, *, expected_hash):
        raise Conflict("旧预测账户已归档，禁止应用启动区间校正")
