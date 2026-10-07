from datetime import datetime

import pytest

from stock_god.market.common import CN
from stock_god.market.normalization import normalize_bar, normalize_quote
from stock_god.market.prediction_inputs import metrics
from stock_god.market.quotes import parse_sina, parse_tencent


def tencent_payload(turnover="8.10"):
    fields = ["0"] * 50
    for index, value in {
        1: "测试股",
        3: "10",
        4: "9.8",
        5: "9.9",
        30: "20260930150000",
        33: "10.2",
        34: "9.7",
        35: "10/1000/1000000",
        38: turnover,
    }.items():
        fields[index] = value
    return 'v_sz001201="' + "~".join(fields) + '";'


def sina_payload():
    fields = ["0"] * 33
    for index, value in {
        0: "测试股",
        1: "9.9",
        2: "9.8",
        3: "10",
        4: "10.2",
        5: "9.7",
        8: "100000",
        9: "1000000",
        30: "2026-09-30",
        31: "15:00:00",
    }.items():
        fields[index] = value
    return 'var hq_str_sz001201="' + ",".join(fields) + '";'


def test_quote_adapters_use_canonical_units_and_field_status():
    tencent = parse_tencent(tencent_payload())[0]
    assert tencent["volume"] == 100000
    assert tencent["amount"] == 1000000
    assert tencent["turnoverPct"] == 8.1
    assert tencent["fieldStatus"]["turnoverPct"] == "observed"

    sina = parse_sina(sina_payload())[0]
    assert sina["volume"] == 100000
    assert sina["amount"] == 1000000
    assert sina["turnoverPct"] is None
    assert sina["fieldStatus"]["turnoverPct"] == "missing"


def test_eastmoney_lots_are_normalized_to_shares_without_changing_amount():
    quote = normalize_quote(
        {
            "price": 10,
            "preClose": 9,
            "open": 9.5,
            "high": 10.2,
            "low": 9.4,
            "volume": 1234,
            "amount": 2185140,
            "turnoverPct": 4.2,
            "mainFlowCny": -1000,
        },
        source="quote:eastmoney",
        volume_unit="lots",
    )
    assert quote["volume"] == 123400
    assert quote["amount"] == 2185140
    assert quote["fieldUnits"]["volume"] == "shares"
    assert quote["sourceFieldUnits"]["volume"] == "lots"
    assert quote["mainFlowCny"] == -1000


def test_minute_amount_missing_is_not_zero_and_proxy_is_explicit():
    bars = [
        normalize_bar(
            {
                "time": "2026-09-30T09:45:00+08:00",
                "open": 10,
                "high": 10.2,
                "low": 9.9,
                "close": 10.1,
                "volume": 1000,
                "amount": None,
            },
            source="tencent:none",
            volume_unit="lots",
        ),
        normalize_bar(
            {
                "time": "2026-09-30T09:46:00+08:00",
                "open": 10.1,
                "high": 10.3,
                "low": 10,
                "close": 10.2,
                "volume": 1000,
                "amount": None,
            },
            source="tencent:none",
            volume_unit="lots",
        ),
    ]
    result = metrics(bars, {"price": 10.2, "preClose": 9.8, "open": 9.9, "high": 10.3})
    assert result["windowAmount"] is None
    assert result["amountStatus"] == "missing"
    assert result["vwapMethod"] == "volume_weighted_minute_close_proxy"
    assert result["vwap"] == pytest.approx(10.15)
    assert all(bar["volume"] == 100000 for bar in bars)


def test_invalid_or_unsupported_fields_are_explicit():
    quote = normalize_quote({"price": "bad", "volume": 10}, source="fixture", volume_unit="unknown")
    assert quote["volume"] is None
    assert quote["fieldStatus"]["price"] == "invalid"
    assert quote["fieldStatus"]["volume"] == "unsupported"


def test_full_market_eastmoney_fields_are_normalized(make_market, monkeypatch):
    row = {
        "f2": "10",
        "f3": "2",
        "f5": "123",
        "f6": "1000000",
        "f8": "4.5",
        "f12": "001201",
        "f14": "测试股",
        "f15": "10.2",
        "f16": "9.7",
        "f17": "9.9",
        "f18": "9.8",
        "f26": "20100101",
        "f62": "-3000",
        "f124": "20260930150000",
    }
    service = make_market()
    monkeypatch.setattr(service.http, "json", lambda *args, **kwargs: {"data": {"diff": [row], "total": 1}})
    snapshot = service.full_market()
    quote = snapshot["rows"][0]
    assert quote["volume"] == 12300
    assert quote["amount"] == 1000000
    assert quote["turnoverPct"] == 4.5
    assert quote["mainFlowCny"] == -3000
    assert quote["fieldStatus"]["volume"] == "observed"


def test_minute_provider_units_are_normalized(make_market, monkeypatch):
    service = make_market()
    # This test checks units, independently of the provider's live seven-day window.
    monkeypatch.setattr("stock_god.market.charts.now", lambda: datetime(2026, 9, 30, 15, 1, tzinfo=CN))
    monkeypatch.setattr(
        service.http,
        "json",
        lambda *args, **kwargs: {
            "code": 0,
            "data": {"sh600000": {"m1": [["202609301500", "10", "10", "10", "10", "14913", "0", "0.45"]]}},
        },
    )
    rows = service._tencent_bars(
        "sh600000",
        datetime(2026, 9, 30, 15, 0, tzinfo=CN),
        datetime(2026, 9, 30, 15, 0, tzinfo=CN),
        "1m",
        "none",
        1200,
    )
    assert rows[0]["volume"] == 1491300 and rows[0]["amount"] is None
    monkeypatch.setattr(
        service.http,
        "json",
        lambda *args, **kwargs: {"data": {"klines": ["2026-09-30,10,10,10,10,2370,2185140"]}},
    )
    rows = service._eastmoney_bars(
        "sh600000",
        datetime(2026, 9, 30, 0, 0, tzinfo=CN),
        datetime(2026, 9, 30, 23, 59, tzinfo=CN),
        "1m",
        "none",
        1200,
    )
    assert rows[0]["volume"] == 237000 and rows[0]["amount"] == 2185140
