import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from stock_god.market.common import CN
from stock_god.meoz_validation import capture, receipt, validate_acceptance

DAY = datetime(2026, 10, 8, tzinfo=CN)


class Market:
    def __init__(self, open_day=True):
        self.open_day = open_day

    def is_trading_day(self, day):
        return self.open_day


class Source:
    def __init__(self, passed=False):
        self.calls = []
        self.passed = passed

    def prepare(self, day, previous):
        self.calls.append(("prepare", day, previous))
        return {"complete": True, "candidates": [{"code": "sz000001"}]}

    def poll(self, day, at):
        self.calls.append(("poll", at))

    def inspect(self, day, cutoff, volume_unit):
        self.calls.append(("inspect", cutoff, volume_unit))
        candidate = {"code": "sz000001", "qualificationKnown": True, "eligible": True,
            "priorStreak": 1, "reference": 900, "lower": 810, "upper": 1100,
            "auctionRows": [{"time": 33900, "fields": [9, 10, 100, 1000] + [None] * 13}]}
        return {"sourceSignature": "fixture", "candidates": [candidate], "checks": {key: self.passed for key in (
            "coverageVerified", "volumeVerified", "checkpointsVerified", "finalAuctionVerified")}}


@pytest.mark.parametrize("now", [DAY - timedelta(days=1), DAY.replace(hour=9, minute=15)])
def test_late_or_wrong_day_never_queries(now):
    source = Source()
    with pytest.raises(ValueError):
        capture(source, Market(), DAY, clock=lambda: now)
    assert source.calls == []


def test_holiday_never_collects():
    source = Source()
    with pytest.raises(ValueError, match="trading day"):
        capture(source, Market(False), DAY, clock=lambda: DAY.replace(hour=9))
    assert source.calls == []


def test_missing_feed_cannot_be_certified_and_polls_with_causal_clock():
    now = DAY.replace(hour=9)

    def sleep(seconds):
        nonlocal now
        now += timedelta(seconds=seconds)

    source = Source(False)
    result = capture(source, Market(), DAY, clock=lambda: now, sleep=sleep)
    polls = [call[1] for call in source.calls if call[0] == "poll"]
    assert polls[0] == DAY.replace(hour=9, minute=15)
    assert polls[-1] <= DAY.replace(hour=9, minute=29, second=59)
    assert all((b - a).total_seconds() == 3 for a, b in zip(polls, polls[1:], strict=False))
    assert result["passed"] is False
    assert len(result["evidenceSha256"]) == 64


def test_receipt_hash_changes_when_measured_flags_change():
    failed = receipt(Source(False), DAY)
    passed = receipt(Source(True), DAY)
    assert failed["evidenceSha256"] != passed["evidenceSha256"]
    assert passed["passed"] is True


def test_receipt_missing_source_state_is_negative_not_exception():
    from stock_god.market.meoz_source import SIGNATURE
    source = SimpleNamespace(inspect=lambda *a, **k: {"complete": False, "candidates": []})
    result = receipt(source, DAY)
    assert result["passed"] is False
    assert result["sourceSignature"] == SIGNATURE
    assert not any(result[flag] for flag in (
        "coverageVerified", "volumeVerified", "checkpointsVerified", "finalAuctionVerified"))


@pytest.fixture
def accepted_capture(tmp_path):
    from stock_god.market.meoz import MeozProvider
    from stock_god.market.meoz_source import MeozAuctionSource
    from stock_god.storage import Database
    from stock_god.storage.current import BASE43_DDL

    database = Database(tmp_path / "facts.db")
    with database.transaction() as connection:
        for sql in BASE43_DDL:
            connection.execute(sql)
    source = MeozAuctionSource(database, SimpleNamespace(meoz=MeozProvider({"meozApiKey": "fixture-key"}, None)))
    source._write("20261008", "source_candidates", {"complete": True, "documents": [],
        "candidates": [{"code": "sz000001", "reference": 900, "lower": 810, "upper": 1100,
                        "qualificationKnown": True, "eligible": True, "priorStreak": 1, "auctionRows": []}]})
    records = []
    for second in range(0, 601, 3):
        at = DAY.replace(hour=9, minute=15) + timedelta(seconds=second)
        records.append({"code": "sz000001", "asOf": at.isoformat(),
            "availableAt": (at + timedelta(seconds=1)).isoformat(),
            "raw": {"close": 10, "vol": 1, "amount": 1000,
                    "bid1": 10, "ask1": 10, "bid_vol1": 1, "ask_vol1": 1}})
    source._write("20261008", "source_snapshot", {"records": records, "documents": [{
        "receivedAt": "2026-10-08T09:25:02+08:00", "rows": [{
            "symbol": "000001", "m_price": 10, "auc_vol": 1, "auc_amt": 1000}]}]})
    evidence = receipt(source, DAY)
    assert evidence["passed"]
    source.certify("lot", evidence)
    (tmp_path / "receipt.json").write_text(json.dumps(evidence), encoding="utf-8")
    return tmp_path, source


def test_acceptance_recomputes_without_network_or_writes(accepted_capture, monkeypatch):
    directory, _source = accepted_capture
    import httpx
    monkeypatch.setattr(httpx.Client, "post", lambda *a, **k: pytest.fail("validation made network request"))
    before = (directory / "facts.db").read_bytes()
    assert validate_acceptance(directory, "fixture-key", now=DAY.replace(hour=10))["passed"]
    assert (directory / "facts.db").read_bytes() == before


def test_acceptance_rejects_wrong_credential_and_future(accepted_capture):
    directory, _source = accepted_capture
    with pytest.raises(ValueError, match="credential"):
        validate_acceptance(directory, "other-key", now=DAY.replace(hour=10))
    with pytest.raises(ValueError, match="future"):
        validate_acceptance(directory, "fixture-key", now=DAY.replace(hour=9))


def test_acceptance_rejects_changed_raw_facts(accepted_capture):
    directory, source = accepted_capture
    saved = source._read("20261008", "source_snapshot")
    saved["records"][10]["raw"]["bid1"] = 123
    source._write("20261008", "source_snapshot", saved)
    with pytest.raises(ValueError, match="facts changed"):
        validate_acceptance(directory, "fixture-key", now=DAY.replace(hour=10))


def test_acceptance_rejects_failed_or_forged_receipt(tmp_path):
    from stock_god.market.meoz_source import SIGNATURE
    (tmp_path / "receipt.json").write_text(json.dumps({"passed": True, "sourceSignature": SIGNATURE}), encoding="utf-8")
    with pytest.raises(ValueError, match="did not pass"):
        validate_acceptance(tmp_path, "fixture-key")


def test_source_checks_cannot_bypass_unbuildable_43_features():
    source = Source(True)
    inspected = source.inspect("20261008", DAY, "lot")
    inspected["candidates"][0]["auctionRows"][0]["fields"][3] = 0
    source.inspect = lambda *args, **kwargs: inspected
    result = receipt(source, DAY)
    assert result["finalAuctionVerified"] is True
    assert result["featuresVerified"] is False
    assert result["passed"] is False
