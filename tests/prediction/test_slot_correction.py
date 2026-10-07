import json

import pytest

from stock_god.prediction.core import Conflict, PredictionError
from stock_god.prediction.repository import insert
from stock_god.prediction.slot_correction import StartSlotCorrection, _restored_items


def test_start_slot_correction_selects_first_completion_and_excludes_1130(env):
    day = "2026-09-24"
    samples = (
        ("slow", "09:50", "09:55:01", "09:57:00"),
        ("fast", "09:55", "09:55:03", "09:56:00"),
        ("late", "11:25", "11:25:01", "11:30:00"),
    )
    with env.db.transaction() as connection:
        for identity, scheduled, started, completed in samples:
            insert(connection, "analysis_runs", {
                "run_id": identity,
                "trading_date": day,
                "scheduled_slot": scheduled,
                "scheduled_for": f"{day}T{scheduled}:00+08:00",
                "started_at": f"{day}T{started}+08:00",
                "persisted_at": f"{day}T{completed}+08:00",
                "status": "no_recommendation",
                "recommendation_count": 0,
                "trigger_source": "scheduled",
            })
    correction = StartSlotCorrection(env.service.repo, env.audit)
    plan = correction.plan()
    assert plan["winners"] == [(day, "09:55", "fast")]
    before = env.service.repo.rows("analysis_runs")
    with pytest.raises(Conflict, match="归档"):
        correction.apply(plan, expected_hash=plan["planHash"])
    assert env.service.repo.rows("analysis_runs") == before


def test_old_strategy_restores_only_report_accepted_row_and_checks_price():
    raw_item = {
        "code": "sh600001", "name": "样本", "marketScore": 20, "sectorScore": 10,
        "stockScore": 40, "catalystScore": 0, "riskDeduction": 0,
        "finalScore": 70, "referencePrice": 10,
        "sourceRefs": ["frozen"], "summary": "原始结论",
    }
    payload = {
        "rawResponse": json.dumps({"recommendations": [raw_item]}, ensure_ascii=False),
        "rawResponseSha256": "frozen-response-hash",
        "evidenceSnapshot": "{}",
    }

    class Audit:
        def detail(self, identity):
            assert identity == "archived"
            return {"payloads": [payload]}

    run = {
        "run_id": "archived", "strategy_version": "research2-slots-v11",
        "recommendation_count": 1,
        "report_markdown": "| 1 | sh600001 | 样本 | 70 | 10.00 | 1005.01 | 20/10/40/0 | 0 | -- |",
        "persisted_at": "2026-09-24T09:57:00+08:00",
    }
    rows, digest = _restored_items(run, "09:55", Audit())
    assert len(rows) == 1 and rows[0]["reference_price"] == 10
    assert rows[0]["selection_rank"] == 1 and digest == "frozen-response-hash"
    run["report_markdown"] = run["report_markdown"].replace("10.00", "10.02")
    with pytest.raises(PredictionError, match="参考价"):
        _restored_items(run, "09:55", Audit())
