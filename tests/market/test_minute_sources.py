import json
import sqlite3
import subprocess
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from stock_god.market import MarketDataError
from stock_god.market.charts import proves_unadjusted
from stock_god.market.common import CN


def bar(at, source="tencent:none", price=10):
    return {
        "time": at.isoformat(),
        "open": 9.0,
        "high": 11.0,
        "low": 8.0,
        "close": price,
        "volume": 100.0,
        "amount": price * 100,
        "source": source,
    }


@pytest.mark.parametrize(
    "source",
    [
        "sina",
        "tencent",
        "diemeng",
        "diemeng_dump",
        "akshare:em",
        "akshare:sina:adjustment=none",
        "raw",
        "tencent:none",
        "sina:none",
        "eastmoney:none",
        "private-minute:none",
    ],
)
def test_original_and_python_sources_prove_unadjusted(source):
    assert proves_unadjusted(source)


@pytest.mark.parametrize(
    "source",
    [
        "",
        "unknown",
        "legacy",
        "akshare:sina",
        "eastmoney",
        "tencent:qfq",
        "sina:hfq",
        "private:adjustment=forward",
        "sina:adjustment=backward",
        "unadjusted:adjustment=forward",
    ],
)
def test_ambiguous_and_adjusted_sources_never_prove_raw(source):
    assert not proves_unadjusted(source)


def test_cache_reads_legacy_symbol_keys_but_rejects_unprovenance(make_market, config):
    at = datetime(2026, 9, 24, 10, tzinfo=CN)
    with sqlite3.connect(config.minute_db) as connection:
        for offset, (code, source) in enumerate(
            (
                ("SH600000", "tencent"),
                ("600000", "akshare:sina:adjustment=none"),
                ("600000.SH", "legacy"),
                ("sh600000", "private:adjustment=forward"),
            )
        ):
            connection.execute(
                "INSERT INTO minute_bar VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    code,
                    int((at + timedelta(minutes=offset)).timestamp() * 1000),
                    10,
                    11,
                    9,
                    10,
                    100,
                    1000,
                    source,
                    0,
                ),
            )
    rows = make_market().cached_bars("sh600000", at, at + timedelta(minutes=5))
    assert [row["source"] for row in rows] == ["tencent", "akshare:sina:adjustment=none"]


def test_public_chart_respects_disabled_sources_and_persisted_order(make_market, monkeypatch):
    at = datetime(2026, 9, 25, 10, tzinfo=CN)
    monkeypatch.setattr("stock_god.market.charts.now", lambda: at)
    service = make_market(
        settings={
            "tencentMinuteEnabled": False,
            "sinaMinuteEnabled": True,
            "akshareEnabled": True,
            "privateMinuteEnabled": True,
            "privateMinuteBaseUrl": "https://private.test",
            "privateMinuteApiKey": "fixture",
            "minuteProviderOrder": ["private", "sina", "akshare", "tencent"],
        }
    )
    calls = []

    def fail(name):
        def invoke(*args):
            calls.append(name)
            raise MarketDataError(name + " unavailable")

        return invoke

    monkeypatch.setattr(service, "_private_bars", fail("private"))
    monkeypatch.setattr(service, "_sina_bars", lambda *args: calls.append("sina") or [bar(at, "sina:none")])
    monkeypatch.setattr(service, "_tencent_bars", fail("disabled-tencent"))
    monkeypatch.setattr(service, "_akshare_bars", fail("akshare"))
    rows = service.bars("sh600000", at, at)
    assert rows[0]["source"] == "sina:none"
    assert calls == ["private", "sina"]


def test_private_minutes_use_canonical_symbol_zero_page_and_share_volume(make_market):
    at = datetime(2026, 9, 21, 10, tzinfo=CN)

    def respond(request):
        body = json.loads(request.content)
        assert request.url.host == "data.diemeng.chat"
        assert body["stock_code"] == "600000.SH"
        assert body["page"] == 0 and body["page_size"] == 10000
        return httpx.Response(200, json={
            "code": 200,
            "data": {"total": 1, "list": [{
                "trade_time": "2026-09-21 10:00:00", "open": 10, "high": 11,
                "low": 9, "close": 10, "vol": 2, "amount": 200,
            }]},
        })

    service = make_market(handler=respond, settings={
        "privateMinuteBaseUrl": "https://diemeng.chat/api",
        "privateMinuteApiKey": "fixture",
    })
    rows = service._private_bars("sh600000", at, at)
    assert len(rows) == 1
    assert rows[0]["source"] == "private-minute:none" and rows[0]["volume"] == 200


def test_recommendation_refresh_accepts_last_closed_minute_without_provider(make_market, monkeypatch):
    at = datetime(2026, 9, 29, 11, 12, 3, tzinfo=CN)
    monkeypatch.setattr("stock_god.market.charts.now", lambda: at)
    service = make_market()
    cached = [bar(at.replace(hour=9, minute=30, second=0) + timedelta(minutes=index)) for index in range(102)]
    monkeypatch.setattr(service, "_cached_minute_bars", lambda *args: cached)
    monkeypatch.setattr(service, "_public_minute_sources", lambda *args: pytest.fail("closed minutes are cached"))
    monkeypatch.setattr(service, "quote", lambda *args, **kwargs: {"price": 10, "asOf": at.isoformat()})
    result = service.refresh_recommendation_chart("sh600000", cached[0]["time"], at)
    assert len(result["bars"]) == 102 and not result["errors"]
    assert result["openedDates"] == {"2026-09-29"}


def test_recommendation_refresh_tries_tencent_first_only_for_today(make_market, monkeypatch):
    at = datetime(2026, 9, 29, 11, 12, 3, tzinfo=CN)
    monkeypatch.setattr("stock_god.market.charts.now", lambda: at)
    service = make_market(settings={"minuteProviderOrder": ["private", "tencent", "sina", "akshare"]})
    cached = [bar(at.replace(hour=9, minute=30, second=0) + timedelta(minutes=index)) for index in range(100)]
    calls = []
    monkeypatch.setattr(service, "_cached_minute_bars", lambda *args: cached)
    monkeypatch.setattr(service, "_save_minute_bars", lambda *args: None)
    monkeypatch.setattr(service, "quote", lambda *args, **kwargs: {"price": 10, "asOf": at.isoformat()})
    monkeypatch.setattr(service, "_public_minute_sources", lambda *args: [
        ("private", lambda deadline: calls.append("private") or []),
        ("tencent", lambda deadline: calls.append("tencent") or [bar(at.replace(hour=11, minute=10, second=0)), bar(at.replace(hour=11, minute=11, second=0))]),
    ])
    result = service.refresh_recommendation_chart("sh600000", cached[0]["time"], at)
    assert calls == ["tencent"] and len(result["bars"]) == 102


def test_disabled_public_and_private_sources_make_no_request(make_market, monkeypatch):
    at = datetime(2026, 9, 25, 10, tzinfo=CN)
    service = make_market(
        settings={
            "tencentMinuteEnabled": False,
            "sinaMinuteEnabled": False,
            "akshareEnabled": False,
            "privateMinuteEnabled": False,
        }
    )
    with pytest.raises(MarketDataError, match="providers failed"):
        service.bars("sh600000", at, at)


def test_prediction_closed_window_uses_fixed_chain_independent_of_chart_settings(make_market, monkeypatch):
    end = datetime(2026, 9, 25, 10, tzinfo=CN)
    service = make_market(
        settings={
            "tencentMinuteEnabled": False,
            "akshareEnabled": False,
            "minuteProviderOrder": ["private", "sina", "akshare", "tencent"],
        }
    )
    calls = []
    monkeypatch.setattr(service, "_tencent_bars", lambda *args: calls.append("tencent") or [bar(end)])
    monkeypatch.setattr(
        service,
        "_prediction_eastmoney_bars",
        lambda *args: (
            calls.append("eastmoney")
            or [bar(end - timedelta(minutes=index), "eastmoney:none") for index in (5, 4, 3, 2, 1)]
            + [bar(end)]
        ),
    )
    monkeypatch.setattr(service, "_cached_minute_bars", lambda *args: calls.append("cache") or [])
    rows = service.prediction_window("sh600000", end - timedelta(minutes=5), end)
    assert calls == ["tencent", "eastmoney"]
    assert len(rows) == 5 and all(row["time"] < end.isoformat() for row in rows)


def test_historical_price_requires_exact_minute_and_uses_vwap(make_market, monkeypatch):
    target = datetime(2026, 9, 24, 10, tzinfo=CN)
    service = make_market()
    calls = []
    monkeypatch.setattr(
        service,
        "_tencent_bars",
        lambda *args: calls.append("tencent") or [bar(target + timedelta(minutes=1))],
    )
    monkeypatch.setattr(
        service,
        "_prediction_eastmoney_bars",
        lambda *args: calls.append("eastmoney") or [bar(target, "eastmoney:none", 10.5)],
    )
    result = service.price_at("sh600000", target)
    assert calls == ["tencent", "eastmoney"]
    assert result["price"] == 10.5 and result["asOf"] == target.isoformat()


def test_akshare_runs_real_sdk_entrypoint_with_hard_timeout_and_isolated_proxy(make_market, monkeypatch):
    service = make_market(
        settings={
            "akshareMinuteSourceMode": "em",
            "httpProxyEnabled": True,
            "httpProxy": "http://127.0.0.1:7890",
            "forceNoProxyForFetch": False,
        }
    )
    at = datetime(2026, 9, 24, 10, tzinfo=CN)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert "ak.stock_zh_a_hist_min_em" in argv[2]
        assert kwargs["timeout"] == 90
        assert kwargs["env"]["HTTPS_PROXY"] == "http://127.0.0.1:7890"
        assert json.loads(kwargs["input"])["source"] == "em"
        return SimpleNamespace(returncode=0, stdout=json.dumps([bar(at)]), stderr="")

    monkeypatch.setattr("stock_god.market.charts.subprocess.run", run)
    rows = service._akshare_bars("sh600000", at, at)
    assert rows[0]["source"] == "akshare:em:adjustment=none"
    assert len(calls) == 1


def test_akshare_timeout_is_explicit_unavailable(make_market, monkeypatch):
    service = make_market(settings={"akshareMinuteSourceMode": "em"})

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("fixture", 90)

    monkeypatch.setattr("stock_god.market.charts.subprocess.run", timeout)
    at = datetime(2026, 9, 24, 10, tzinfo=CN)
    with pytest.raises(MarketDataError, match="TimeoutExpired"):
        service._akshare_bars("sh600000", at, at)


def test_packaged_user_dictionary_loaded_before_runtime_override(make_market, config):
    custom = config.root / "data/dict/user.txt"
    custom.parent.mkdir(parents=True)
    custom.write_text("新能源汽车 900 n\n自定义量化词 700 n\n", encoding="utf-8")
    tokenizer, _, _ = make_market()._analyzers()
    assert tokenizer.FREQ["新能源汽车"] == 900
    assert tokenizer.FREQ["自定义量化词"] == 700
    assert "新能源汽车" in list(tokenizer.cut("新能源汽车产业", HMM=True))
