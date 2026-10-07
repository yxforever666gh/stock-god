import json


from stock_god.prediction.evidence import build_prompt


def full_market_evidence(evidence):
    # Same normalized shape as MarketServices.full_market, including thousands of
    # non-candidate securities that must stay in the durable evidence only.
    rows = [
        {
            "code": f"sh{600000 + index:06}",
            "name": f"全市场股票{index}",
            "price": 10.1,
            "preClose": 10.0,
            "changePct": 1.0,
            "open": 10.0,
            "high": 10.2,
            "low": 9.9,
            "volume": 100000,
            "amount": 1010000,
            "turnover": 1.5,
            "mainFlow": 50000,
            "listingDate": "20100101",
            "asOf": evidence["cutoffAt"],
            "vendorArchiveNote": "VENDOR_ARCHIVE_ONLY",
        }
        for index in range(6000)
    ]
    evidence["documents"][0]["content"] = json.dumps(
        {"rows": rows, "reported": 6000, "source": "eastmoney"}, ensure_ascii=False
    )
    evidence["prompt"] = json.dumps(
        {
            "version": "research2-slots-v8",
            "cutoffAt": evidence["cutoffAt"],
            "market": {"observed": 6000, "reported": 6000, "coveragePct": 100},
            "candidates": evidence["candidates"],
            "sources": [{"sourceId": "market", "summary": "全市场覆盖6000只，已按规则筛选候选"}],
        },
        ensure_ascii=False,
    )
    return evidence


def test_full_market_rows_are_not_repeated_in_the_model_prompt(env):
    evidence = full_market_evidence(env.market.collect_prediction_evidence(env.clock(), set(), 1e300))
    evidence["scoreEvidence"] = {"sh600001": {"sectorState": "available", "catalystState": "source_missing"}}
    prompt = build_prompt(evidence)
    assert len(evidence["documents"][0]["content"]) > 1_000_000
    assert len(prompt) < 20_000
    assert "VENDOR_ARCHIVE_ONLY" not in prompt
    assert evidence["prompt"] in prompt
    assert "sectorState" in prompt and evidence["cutoffAt"] in prompt
