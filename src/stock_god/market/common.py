"""Provider transport, provenance and bounded in-process cache."""

import json
import math
import re
import sqlite3
import time
from collections.abc import Callable
from copy import deepcopy
from datetime import date, datetime
from threading import RLock
from typing import Any, overload
from zoneinfo import ZoneInfo

import httpx

from stock_god.config import AppConfig

CN = ZoneInfo("Asia/Shanghai")
ZERO_TIME = "0001-01-01T00:00:00Z"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


class MarketDataError(RuntimeError):
    """An input is unavailable; never interpreted as zero or a successful empty input."""

    def __init__(self, message: str, *, evidence: dict | None = None):
        super().__init__(message)
        self.evidence = evidence


def remaining_seconds(deadline):
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MarketDataError("provider time budget exhausted")
    return remaining


@overload
def number(value: Any, default: float) -> float: ...


@overload
def number(value: Any, default: None = None) -> float | None: ...


def number(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(str(value).strip().replace(",", ""))
        return parsed if math.isfinite(parsed) else default
    except (ValueError, TypeError):
        return default


def now() -> datetime:
    return datetime.now(CN)


def timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=CN) if value.tzinfo is None else value.astimezone(CN)
    if isinstance(value, (float, int)):
        return datetime.fromtimestamp(value / 1000 if value > 10**12 else value, CN)
    text = str(value).strip()
    if text.isdigit() and len(text) == 14:
        return datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=CN)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace("/", "-"))
    return parsed.replace(tzinfo=CN) if parsed.tzinfo is None else parsed.astimezone(CN)


def instrument(code: str, asset_type: str = "stock", market: str = "") -> dict:
    raw = code.strip().lower()
    if asset_type not in {"stock", "index", "etf"}:
        raise ValueError("assetType must be stock, index or etf")
    if re.fullmatch(r"\d{6}\.(sh|sz|bj)", raw):
        raw = raw[-2:] + raw[:6]
    if re.fullmatch(r"\d{6}", raw):
        prefix = market.lower() or (
            "sh"
            if raw[0] in "569" or asset_type == "index" and raw.startswith("000")
            else "bj"
            if raw[0] in "48"
            else "sz"
        )
        raw = prefix + raw
    if raw.startswith("us"):
        raw = "gb_" + raw[2:]
    if re.fullmatch(r"(sh|sz|bj)\d{6}", raw):
        actual = raw[:2].upper()
        if asset_type == "etf" and not (raw.startswith(("sh51", "sh56", "sh58", "sz15"))):
            raise ValueError("invalid ETF code")
        if asset_type == "index" and not raw.startswith(("sh000", "sz399")):
            raise ValueError("invalid index code")
    elif asset_type == "stock" and re.fullmatch(r"hk\d{5}|gb_[a-z0-9._-]+", raw):
        actual = "HK" if raw.startswith("hk") else "US"
    else:
        raise ValueError("invalid instrument code")
    if market and market.upper() not in {actual, "A" if actual in {"SH", "SZ", "BJ"} else actual}:
        raise ValueError("instrument and market do not match")
    return {"code": raw, "assetType": asset_type, "market": actual}


def evidence_instrument(code: str, asset_type: str = "stock", market: str = "") -> dict:
    identity = instrument(code, asset_type, market)
    if asset_type == "stock" and not identity["code"].startswith(("sh60", "sh68", "sz00", "sz30")):
        raise ValueError("code does not match assetType stock")
    if identity["market"] not in {"SH", "SZ"}:
        raise ValueError("chart and instrument evidence require Shanghai or Shenzhen instruments")
    return identity


def security_id(code: str) -> str:
    value = instrument(code)["code"]
    if value.startswith(("sh", "sz", "bj")):
        return ("1." if value.startswith("sh") else "0.") + value[2:]
    if value.startswith("hk"):
        return "116." + value[2:]
    raise ValueError("provider does not support this instrument")


def envelope(
    data: Any,
    source: str,
    *,
    status: str = "ok",
    as_of: Any = None,
    errors: list | None = None,
    warnings: list | None = None,
) -> dict:
    result = {
        "data": data,
        "source": source,
        "asOf": timestamp(as_of).isoformat() if as_of else ZERO_TIME,
        "fetchedAt": now().isoformat(),
        "status": status,
        "errors": errors or [],
        "evidenceProfile": "market-evidence-v1",
    }
    if source:
        result["sources"] = [
            {
                "provider": source,
                "status": status,
                "asOf": result["asOf"],
                "availableAt": result["asOf"] if as_of else None,
            }
        ]
    if warnings:
        result["warnings"] = warnings
    return result


class Transport:
    def __init__(self, settings: dict, client: httpx.Client | None = None):
        proxy = settings.get("httpProxy") if settings.get("httpProxyEnabled") else None
        if settings.get("forceNoProxyForFetch", True):
            proxy = None
        self.client = client or httpx.Client(
            timeout=float(settings.get("crawlTimeOut") or 12),
            proxy=proxy,
            trust_env=False,
            follow_redirects=True,
        )
        self.owns_client = client is None
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = RLock()

    def close(self):
        if self.owns_client:
            self.client.close()

    def text(
        self,
        url: str,
        params: dict | None = None,
        *,
        encoding: str | None = None,
        headers: dict | None = None,
        method: str = "GET",
        body: dict | None = None,
        form: dict | None = None,
        timeout: float | None = None,
    ) -> str:
        values = {"User-Agent": USER_AGENT, "Referer": "https://finance.sina.com.cn/"}
        values.update(headers or {})
        try:
            if timeout is None:
                response = self.client.request(method, url, params=params, headers=values, json=body, data=form)
            else:
                response = self.client.request(method, url, params=params, headers=values, json=body, data=form, timeout=timeout)
            response.raise_for_status()
            return response.content.decode(encoding or response.encoding or "utf-8")
        except (httpx.HTTPError, UnicodeError) as exc:
            # Provider URLs may contain credentials; exceptions deliberately omit URL/body.
            raise MarketDataError(f"provider request failed ({type(exc).__name__})") from exc

    def json(self, url: str, params: dict | None = None, **kwargs) -> Any:
        raw = self.text(url, params, **kwargs).strip()
        if not raw.startswith(("[", "{")):
            match = re.search(r"^[\w.$]+\s*\((.*)\)\s*;?$", raw, re.DOTALL)
            if match:
                raw = match.group(1)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MarketDataError("provider returned invalid JSON") from exc

    def cached(self, key: str, ttl: float, load: Callable):
        with self._lock:
            item = self._cache.get(key)
            if item and item[0] > time.monotonic():
                return deepcopy(item[1])
        value = load()
        with self._lock:
            if len(self._cache) > 512:
                self._cache = {k: v for k, v in self._cache.items() if v[0] > time.monotonic()}
            self._cache[key] = (time.monotonic() + ttl, deepcopy(value))
        return value

    def chain(self, providers: list[tuple[str, Callable]], empty: Any) -> dict:
        failures = []
        for name, call in providers:
            try:
                data, as_of = call()
                if data is None:
                    raise MarketDataError("provider returned no data")
                result = envelope(
                    data, name, status="partial" if failures else "ok", as_of=as_of, errors=failures
                )
                result["sources"] = [
                    {
                        "provider": failure["provider"],
                        "status": "unavailable",
                        "message": failure["message"],
                        "asOf": ZERO_TIME,
                    }
                    for failure in failures
                ] + result["sources"]
                return result
            except (MarketDataError, ValueError, KeyError, IndexError, TypeError) as exc:
                failures.append({"provider": name, "code": "unavailable", "message": str(exc)})
        return envelope(empty, "", status="unavailable", errors=failures)


def database_rows(path, query: str, params: tuple = ()) -> list[dict]:
    """Never create a database, run migrations, or write through data reads."""
    if not path.is_file():
        raise MarketDataError("market database is unavailable")
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(query, params)]
    except sqlite3.Error as exc:
        raise MarketDataError(f"market database read failed ({type(exc).__name__})") from exc


def require_date(value: str) -> str:
    if value and date.fromisoformat(value).isoformat() != value:
        raise ValueError("date must be YYYY-MM-DD")
    return value


class ProviderState:
    """Type contract for the concrete MarketServices provider mixins."""

    config: AppConfig
    settings: dict[str, Any]
    http: Transport
    quote: Callable[..., dict[str, Any]]
    quotes: Callable[..., list[dict[str, Any]]]
    stock_master: Callable[..., list[dict[str, Any]]]
    bars: Callable[..., list[dict[str, Any]]]
    prediction_window: Callable[..., list[dict[str, Any]]]
    is_trading_day: Callable[..., bool]
    full_market: Callable[..., dict[str, Any]]
    fallback_full_market: Callable[..., dict[str, Any]]
    fund_flows: Callable[..., dict[str, Any]]
    hot_topics: Callable[..., list[dict[str, Any]]]
    hot_events: Callable[..., list[dict[str, Any]]]
    telegraphs: Callable[..., list[dict[str, Any]]]
    notices: Callable[..., list[dict[str, Any]]]
    stock_concepts: Callable[..., list[dict[str, Any]]]
    stock_financials: Callable[..., list[dict[str, Any]]]
    macro_evidence: Callable[..., dict[str, Any]]
    reuters_news: Callable[..., dict[str, Any]]
    interactive_answers: Callable[..., dict[str, Any]]
    global_indexes: Callable[..., dict[str, Any]]
    industry_rank: Callable[..., list[Any]]
    money_rank: Callable[..., list[dict[str, Any]]]
    money_trend: Callable[..., list[dict[str, Any]]]
    hot_stocks: Callable[..., list[dict[str, Any]]]
    long_tiger: Callable[..., list[dict[str, Any]]]
    investment_calendar: Callable[..., list[dict[str, Any]]]
    cls_calendar: Callable[..., list[dict[str, Any]]]
    minute_line: Callable[..., dict[str, Any]]
    research_reports: Callable[..., list[dict[str, Any]]]
    _live_news: Callable[..., list[dict[str, Any]]]
    prediction_theme_documents: Callable[..., list[dict[str, Any]]]
    sentiment_weighted: Callable[..., dict[str, Any]]
