import copy
import json
from datetime import timedelta

import pytest

from stock_god.prediction.core import local
from stock_god.prediction.input_facts import compact_snapshot, daily_facts, project_source


CUTOFF = local("2026-09-30T09:50:00+08:00")


def bars(count=61):
    return [
        {
            "time": (CUTOFF - timedelta(days=count - index)).date().isoformat(),
            "open": 10 + index,
            "high": 11 + index,
            "low": 9 + index,
            "close": 10 + index,
            "volume": 100 + index,
            "amount": 0,
            "source": "tencent:none",
        }
        for index in range(count)
    ]


def unpack(value):
    return [dict(zip(value["columns"], row, strict=True)) for row in value["rows"]]


def document(kind, value):
    return {
        "sourceId": "research2:aux:sh603396:" + kind,
        "stockCode": "sh603396",
        "availableAt": CUTOFF.isoformat(),
        "content": json.dumps(value),
        "error": "",
    }


def test_daily_retains_recent_complete_records_and_all_history_statistics():
    raw = bars()
    original = copy.deepcopy(raw)
    facts = daily_facts(list(reversed(raw)), CUTOFF)
    recent = unpack(facts["recent"])
    assert raw == original
    assert facts["observations"] == 61 and facts["omittedRows"] == 41
    assert len(recent) == 20 and recent[0]["date"] == raw[-20]["time"]
    assert recent[-1]["date"] == "2026-09-29" and recent[-1]["amount"] is None
    assert facts["statistics"]["5"]["returnPct"] == pytest.approx((70 / 65 - 1) * 100, abs=0.0001)
    assert facts["statistics"]["61"]["returnPct"] is None


def test_daily_excludes_partial_future_invalid_and_conflicting_records():
    raw = bars(3)
    extra = [
        raw[-1] | {"close": 10, "open": 10, "high": 11, "low": 9},
        raw[-1] | {"time": "2026-09-30"},
        raw[-1] | {"time": "2026-10-01"},
        raw[-1] | {"time": "bad"},
        raw[-1] | {"time": "2026-09-01", "high": 1},
    ]
    facts = daily_facts(raw + extra, CUTOFF)
    assert facts["observations"] == 2
    assert facts["through"] == "2026-09-28"
    assert all(row["date"] < "2026-09-30" for row in unpack(facts["recent"]))


def test_missing_history_and_mixed_units_are_not_filled():
    raw = bars(5)
    raw[0]["source"] = "sina"
    facts = daily_facts(raw, CUTOFF)
    assert facts["statistics"]["5"]["sameSourceVolumeRatio"] is None
    assert facts["statistics"]["10"]["observations"] == 5
    assert facts["statistics"]["10"]["returnPct"] is None
    assert daily_facts([], CUTOFF)["status"] == "unavailable"


def test_percentage_does_not_prove_exchange_limit_without_verified_rule():
    raw = bars(4)
    raw[-2].update(open=12, high=12.1, low=12, close=12.1)
    raw[-1].update(open=12.1, high=13.31, low=12.1, close=13.31)
    facts = daily_facts(raw, CUTOFF)
    event = unpack(facts["recent"])[-1]
    assert event["returnPct"] == 10 and "limitUpClose" not in event
    raw[-1].update(preClose=12.1, limitRate=0.1)
    facts = daily_facts(raw, CUTOFF)
    assert unpack(facts["recent"])[-1]["limitUpClose"]


def test_notices_keep_full_title_disclosure_time_and_mark_missing_body():
    title = "risk announcement " + "complete title " * 70
    value = [
        {"title": "older", "notice_date": "2026-01-01"},
        {"title": title, "display_time": "2026-09-29 18:46:35:498", "notice_date": "2026-09-30"},
        {"title": "future", "display_time": "2026-09-30 10:00:00"},
    ]
    facts = project_source(document("notices", value), CUTOFF, set(), set(), set())
    selected = unpack(facts)
    assert selected[0]["title"] == title
    assert selected[0]["display_time"] == "2026-09-29 18:46:35:498"
    assert not facts["bodyAvailable"] and facts["excludedRows"] == 1
    assert "future" not in json.dumps(facts)


def test_interactive_uses_reply_time_and_financials_keep_latest_period():
    replies = {
        "results": [
            {"mainContent": "question", "attachedContent": "old reply", "attachedPubDate": "2026-01-01"},
            {"mainContent": "question", "attachedContent": "new reply", "attachedPubDate": "2026-09-29"},
        ]
    }
    facts = project_source(document("interactive", replies), CUTOFF, set(), set(), set())
    assert unpack(facts)[0]["attachedContent"] == "new reply"
    assert facts["timeField"] == "attachedPubDate"
    reports = [
        {"REPORT_DATE": "2025-12-31", "NOTICE_DATE": "2026-03-01", "NETPROFIT": 100},
        {"REPORT_DATE": "2026-06-30", "NOTICE_DATE": "2026-08-29", "NETPROFIT": -100},
    ]
    facts = project_source(document("financials", reports), CUTOFF, set(), set(), set())
    assert unpack(facts)[0]["NETPROFIT"] == -100


def test_indexes_retain_domestic_entries_even_after_foreign_entries():
    value = {
        "common": [{"qtcode": "s_usDJI", "name": "Dow", "zxj": 1, "zdf": 1, "img": "metadata"}],
        "asia": [{"qtcode": "sh000001", "name": "Shanghai", "zxj": 2, "zdf": 2, "state": "open"}],
    }
    facts = project_source(document("global-indexes", value), CUTOFF, set(), set(), set())
    assert {row["qtcode"] for row in unpack(facts)} == {"s_usDJI", "sh000001"}
    assert "img" not in facts["columns"]


def test_ranked_sources_prioritize_candidate_outside_first_ten_and_count_omissions():
    value = [{"symbol": f"sh600{index:03}", "name": "other", "netamount": index} for index in range(20)]
    value[-1]["symbol"] = "sh603396"
    facts = project_source(document("stock-money", value), CUTOFF, {"sh603396"}, set(), set())
    assert unpack(facts)[0]["symbol"] == "sh603396"
    assert len(facts["rows"]) == 10 and facts["omittedRows"] == 10


@pytest.mark.parametrize("kind", ["sector-flows", "concept-flows"])
def test_provider_normalized_fund_flows_keep_real_amount_fields(kind):
    value = {
        "status": "ok",
        "data": [
            {
                "code": "BK1175",
                "name": "glass",
                "changePct": 1.5,
                "netAmount": 1234,
                "inAmount": None,
                "outAmount": None,
                "mainNetRatio": 2.5,
                "superLargeNetAmount": 100,
                "largeNetAmount": 1134,
            }
        ],
    }
    facts = project_source(document(kind, value), CUTOFF, set(), set(), {"bk1175"})
    row = unpack(facts)[0]
    assert row["netAmount"] == 1234 and row["mainNetRatio"] == 2.5
    assert row["inAmount"] is None and row["largeNetAmount"] == 1134


def test_global_indexes_have_a_record_limit_and_explicit_omission_count():
    value = {"asia": [{"qtcode": f"index-{index}", "name": "index", "zxj": index} for index in range(45)]}
    facts = project_source(document("global-indexes", value), CUTOFF, set(), set(), set())
    assert len(facts["rows"]) == 40 and facts["omittedRows"] == 5


def test_null_topic_associations_do_not_fail_the_analysis():
    value = [{"htid": 1, "nickname": "topic", "desc": "known text", "stock_list": None}]
    facts = project_source(document("hot-topics", value), CUTOFF, set(), set(), set())
    assert facts["status"] == "ok" and unpack(facts)[0]["stock_list"] is None


def test_compact_snapshot_keeps_raw_documents_and_never_falls_back_to_prefix():
    docs = [
        document("daily", bars()),
        document("unknown", {"vendorSecretShape": "prefix"}),
        document("notices", [{"title": "after freeze"}]) | {"availableAt": "2026-09-30T11:00:00+08:00"},
    ]
    evidence = {
        "prompt": json.dumps({"sources": [{"summary": "old broken JSON prefix"}], "market": {"advances": 1}}),
        "cutoffAt": CUTOFF.isoformat(),
        "freezeAt": CUTOFF.isoformat(),
        "documents": docs,
        "candidates": [{"code": "sh603396", "name": "test"}],
        "scoreEvidence": {},
    }
    before = copy.deepcopy(evidence)
    snapshot = json.loads(compact_snapshot(evidence))
    assert evidence == before and snapshot["market"] == {"advances": 1}
    assert snapshot["sources"][1]["facts"]["status"] == "unrecognized_structure"
    assert snapshot["sources"][2]["status"] == "after_freeze"
    assert "facts" not in snapshot["sources"][2]
    assert "summary" not in snapshot["sources"][0]
    assert "prefix" not in json.dumps(snapshot)
