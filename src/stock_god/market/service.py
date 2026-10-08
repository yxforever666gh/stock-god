"""Immutable task settings with day-and-identity-scoped connection reuse."""

import hashlib
import json
import re
import time
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timedelta
from threading import RLock
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from stock_god.config import AppConfig

from .charts import Charts, proves_unadjusted, valid_bars
from .common import CN, MarketDataError, Transport, instrument, number, timestamp
from .evidence import Evidence
from .funds import Funds
from .meoz import MeozProvider, MeozSession
from .meoz_timing import CaptureTiming
from .news import News
from .prediction_inputs import PredictionInputs
from .quotes import Quotes
from .text_analysis import TextAnalysis
from .themes import Themes


class _TimedTransport(Transport):
    def __init__(self, settings, client, timing):
        super().__init__(settings, client)
        self.timing = timing
        self._secrets = [str(value) for name, value in settings.items() if value
                         and name.lower().endswith(("apikey", "token"))]

    def _capturing(self):
        return self.timing.capturing()

    def _endpoint(self, url, body):
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname or "unknown"
            authority = hostname + (":" + str(parsed.port) if parsed.port else "")
            path = unquote(parsed.path or "/")
            secrets = self._secrets + [unquote(value) for value in (parsed.username, parsed.password) if value]
            if isinstance(body, dict):
                secrets += [str(value) for name, value in body.items() if value
                            and name.lower() in {"apikey", "api_key", "token", "password"}]
            for secret in secrets:
                path = path.replace(secret, "[redacted]").replace(unquote(secret), "[redacted]")
            node = urlunsplit((parsed.scheme, authority, "", "", ""))
            api = "tushare:trade_cal" if (hostname == "api.tushare.pro" and isinstance(body, dict)
                                         and body.get("api_name") == "trade_cal") else hostname + ":" + path
            return api, node
        except (TypeError, ValueError):
            return "provider:unknown", "unknown"

    @staticmethod
    def _row_count(raw):
        raw = raw.strip()
        if not raw.startswith(("[", "{")):
            match = re.search(r"^[\w.$]+\s*\((.*)\)\s*;?$", raw, re.DOTALL)
            raw = match.group(1) if match else raw
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if isinstance(payload, list):
            return len(payload)
        if isinstance(payload, dict):
            data = payload.get("data", payload)
            if isinstance(data, list):
                return len(data)
            if isinstance(data, dict):
                for key in ("items", "list", "klines", "trends"):
                    if isinstance(data.get(key), list):
                        return len(data[key])
                counts = [len(value[key]) for value in data.values() if isinstance(value, dict)
                          for key in ("m1", "day") if isinstance(value.get(key), list)]
                if counts:
                    return sum(counts)
        return None

    def text(self, url, params=None, **kwargs):
        if not self._capturing():
            return super().text(url, params, **kwargs)
        supplied = kwargs.get("timeout")
        configured = self.client.timeout.read if supplied is None else supplied
        limit = min(30.0, number(configured, 30.0))
        remaining = self.timing.remaining_budget()
        if remaining is not None:
            limit = min(limit, remaining)
        if limit <= 0:
            raise MarketDataError("auction capture receive deadline exhausted")
        kwargs["timeout"] = limit
        api, node = self._endpoint(url, kwargs.get("body"))
        started_at, started = self.timing.clock(), self.timing.monotonic()
        outcome, row_count = "ok", None
        try:
            raw = super().text(url, params, **kwargs)
            remaining = self.timing.remaining_budget()
            if remaining is not None and remaining < 0:
                outcome = "late"
                raise MarketDataError("auction capture response received after deadline")
            row_count = self._row_count(raw)
            return raw
        except MarketDataError as exc:
            cause = exc.__cause__
            if outcome != "late":
                outcome = ("timeout" if isinstance(cause, httpx.TimeoutException) else
                           "http_" + str(cause.response.status_code) if isinstance(cause, httpx.HTTPStatusError) else
                           "connection_failed" if isinstance(cause, httpx.NetworkError) else "transport_failed")
            raise
        finally:
            self.timing.record_request(api=api, node=node, attempt=1, outcome=outcome,
                startedAt=started_at, endedAt=self.timing.clock(),
                elapsedSeconds=self.timing.monotonic() - started, rowCount=row_count)

    def cached(self, key, ttl, load):
        with self._lock:
            cached = self._cache.get(key)
            if self._capturing() and cached and cached[0] > time.monotonic():
                self.timing.record_cache_hit()
        return super().cached(key, ttl, load)


class _SessionPool:
    """Idle entries are bounded; active task leases survive eviction and clone close."""

    def __init__(self, client=None, clock=None):
        self.client = client
        self.clock = clock or (lambda: datetime.now(CN))
        self.entries = OrderedDict()
        self.lock = RLock()
        self.closed = False

    def key(self, settings):
        identity = {name: settings.get(name) for name in ("meozApiKey", "tushareToken", "crawlTimeOut",
                    "httpProxyEnabled", "httpProxy", "forceNoProxyForFetch")}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        return self.clock().astimezone(CN).date().isoformat(), digest

    def acquire(self, settings):
        with self.lock:
            if self.closed:
                raise RuntimeError("market services are closed")
            key = self.key(settings)
            if key not in self.entries:
                timing = CaptureTiming(clock=self.clock)
                self.entries[key] = {"http": _TimedTransport(settings, self.client, timing),
                                     "meoz": MeozSession(timing=timing), "leases": 0}
            entry = self.entries[key]
            entry["leases"] += 1
            self.entries.move_to_end(key)
            self._prune(key[0])
            return key, entry

    def _prune(self, day):
        for key, entry in list(self.entries.items()):
            if not entry["leases"] and (key[0] != day or len(self.entries) > 8):
                self.entries.pop(key)["http"].close()

    def release(self, key):
        with self.lock:
            if key in self.entries:
                self.entries[key]["leases"] -= 1
            self._prune(self.clock().astimezone(CN).date().isoformat())

    def close(self):
        with self.lock:
            self.closed = True
            entries, self.entries = self.entries, OrderedDict()
        for entry in entries.values():
            entry["http"].close()


class MarketServices(Quotes, Charts, Evidence, News, PredictionInputs, TextAnalysis, Themes, Funds):
    def __init__(self, config: AppConfig, settings: dict | None = None, *, client: httpx.Client | None = None,
                 _pool=None, _clock=None):
        self.config = config
        self.settings = deepcopy(settings or {})
        self._pool = _pool or _SessionPool(client, _clock)
        self._is_root = _pool is None
        self._closed = False
        self._session_key, entry = self._pool.acquire(self.settings)
        self.http = entry["http"]
        self.meoz = MeozProvider(self.settings, self.http.client, clock=self._pool.clock, session=entry["meoz"])

    def _refresh_session(self):
        if self._closed or self._pool.closed:
            raise RuntimeError("market services are closed")
        if self._session_key != self._pool.key(self.settings):
            previous = self._session_key
            self._session_key, entry = self._pool.acquire(self.settings)
            self.http = entry["http"]
            self.meoz = MeozProvider(self.settings, self.http.client, clock=self._pool.clock, session=entry["meoz"])
            self._pool.release(previous)

    def meoz_request(self, apiname, params, fields=None, *, history=False, deadline=None, budget_seconds=None):
        self._refresh_session()
        return self.meoz.request(apiname, params, fields, history=history, deadline=deadline,
                                 budget_seconds=budget_seconds)

    def meoz_ticks(self, day, symbols, *, final=False, deadline=None, budget_seconds=3.0):
        self._refresh_session()
        return self.meoz.ticks(day, symbols, final=final, deadline=deadline, budget_seconds=budget_seconds)

    def meoz_subscription_symbols(self, *, deadline=None):
        from .meoz_stream import subscription_symbols
        self._refresh_session()
        return subscription_symbols(self.meoz, deadline=deadline)

    def meoz_stream_records(self, symbols, *, deadline, on_status=None):
        from .meoz_stream import stream_records
        self._refresh_session()
        return stream_records(self.meoz, symbols, deadline=deadline, on_status=on_status)

    def meoz_tick_history(self, day, symbols, *, offset=0, start_time="09:15:00", end_time="09:26:00",
                          deadline=None, budget_seconds=None):
        self._refresh_session()
        return self.meoz.tick_history(day, symbols, offset=offset, start_time=start_time, end_time=end_time,
                                      deadline=deadline, budget_seconds=budget_seconds)

    def _auction_bars(self, code, start, end, budget_seconds):
        """Keep raw-minute provenance and cache coverage within one provider budget."""
        code, start, end = instrument(code)["code"], timestamp(start), timestamp(end)
        if start > end:
            raise ValueError("invalid auction minute range")
        budget = min(30.0, max(0.0, number(budget_seconds, 0.0)))
        deadline = time.monotonic() + budget
        try:
            cached = self.cached_bars(code, start, end)
            effective_start = max(start, start.replace(hour=9, minute=30, second=0, microsecond=0))
            effective_end = min(end, end.replace(hour=15, minute=0, second=0, microsecond=0))
            if (cached and timestamp(cached[0]["time"]) <= effective_start + timedelta(minutes=1)
                    and timestamp(cached[-1]["time"]) >= effective_end - timedelta(minutes=1)):
                if self.meoz.timing.capturing():
                    self.meoz.timing.record_cache_hit()
                return cached[-5000:]
        except (MarketDataError, ValueError, KeyError, TypeError):
            pass
        if time.monotonic() >= deadline:
            raise MarketDataError("auction minute time budget exhausted")
        settings = deepcopy(self.settings)
        for name in ("crawlTimeOut", "privateMinuteTimeoutSec"):
            configured = number(settings.get(name), budget)
            settings[name] = min(budget, configured) if configured > 0 else budget
        task = self.with_settings(settings)
        capturing = self.meoz.timing.capturing()
        if capturing:
            # This task-only adapter keeps the capture identity without mutating a pooled transport.
            task.http = _TimedTransport(settings, task.http.client, self.meoz.timing)
        failures = []
        try:
            for name, load in task._public_minute_sources(code, start, end, 5000):
                if time.monotonic() >= deadline:
                    break
                try:
                    if name == "akshare" and capturing:
                        with self.meoz.timing.external_operation(name):
                            fetched = load(deadline)
                    else:
                        fetched = load(deadline)
                    rows = valid_bars([deepcopy(row) for row in fetched
                                       if proves_unadjusted(row.get("source"))], start, end)
                    if time.monotonic() >= deadline:
                        raise MarketDataError("auction minute data arrived after time budget")
                    if not rows:
                        raise MarketDataError("no verified minute bars in requested range")
                    try:
                        task._save_minute_bars(code, rows)
                    except MarketDataError:
                        pass  # Preserve the established nonfatal chart-cache write behavior.
                    return rows[-5000:]
                except (MarketDataError, ValueError, KeyError, TypeError) as exc:
                    failures.append(name + ": " + str(exc))
            raise MarketDataError("auction minute providers incomplete: " + "; ".join(failures))
        finally:
            task.close()

    def with_settings(self, settings: dict):
        if self._closed:
            raise RuntimeError("market services are closed")
        return MarketServices(self.config, settings, _pool=self._pool)

    def close(self):
        if not self._closed:
            self._closed = True
            self._pool.release(self._session_key)
            if self._is_root:
                self._pool.close()
