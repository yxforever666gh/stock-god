"""Bounded, credential-free timings for one auction collection identity."""

import logging
import math
import time
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime
from threading import RLock
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from .common import CN, timestamp

logger = logging.getLogger(__name__)
COLLECTION_PHASES = {"calendar", "mother", "qualification", "previous-minute", "history", "poll", "backfill",
                     "validation", "prepare"}
_CAPTURE: ContextVar[tuple[str, str] | None] = ContextVar("auction_capture_timing", default=None)
_DEADLINE: ContextVar[datetime | None] = ContextVar("auction_capture_deadline", default=None)


class _CaptureHttpLogFilter(logging.Filter):
    def filter(self, record):
        if (_CAPTURE.get() is not None and str(record.msg).startswith("HTTP Request:")
                and isinstance(record.args, tuple) and len(record.args) >= 2):
            try:
                parsed = urlsplit(str(record.args[1]))
                authority = (parsed.hostname or "unknown") + (":" + str(parsed.port) if parsed.port else "")
                safe_url = urlunsplit((parsed.scheme, authority, "", "", ""))
            except ValueError:
                safe_url = "[provider]"
            record.args = (record.args[0], safe_url, *record.args[2:])
        return True


logging.getLogger("httpx").addFilter(_CaptureHttpLogFilter())


def _seconds(value):
    try:
        result = float(value)
        return max(0.0, result) if math.isfinite(result) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _at(value):
    try:
        return timestamp(value).astimezone(CN).isoformat()
    except (TypeError, ValueError):
        return None


def _count(value):
    return int(_seconds(value))


def _first(current, candidate):
    return min(current, candidate) if current and candidate else current or candidate


def _last(current, candidate):
    return max(current, candidate) if current and candidate else current or candidate


class CaptureTiming:
    """Elapsed counters use a monotonic clock; wall times retain China timezone."""

    def __init__(self, *, clock=None, monotonic=None):
        self.clock = clock or (lambda: datetime.now(CN))
        self.monotonic = monotonic or time.monotonic
        self._lock = RLock()
        self._run_id = uuid4().hex
        self._created_at = self.clock().astimezone(CN).isoformat()
        self._restored = False
        self._scheduled_end = {}
        self._scheduled_active = {}
        self._data = {"startedAt": None, "endedAt": None, "phases": {}, "requests": [],
                      "requestCount": 0, "requestElapsedSeconds": 0.0, "waits": {},
                      "cacheHits": 0, "unknownIntervals": [], "interfaces": {}, "operations": {}}

    @property
    def run_id(self):
        return self._run_id

    @staticmethod
    def capture_active():
        return _CAPTURE.get()

    def capturing(self):
        marker = self.capture_active()
        return bool(marker and marker[0] == self._run_id)

    def capture_deadline(self):
        return _DEADLINE.get() if self.capturing() else None

    def remaining_budget(self):
        deadline = self.capture_deadline()
        return (deadline - self.clock()).total_seconds() if deadline is not None else None

    @contextmanager
    def phase(self, name, *, deadline=None):
        started_at, started = self.clock(), self.monotonic()
        parent_deadline = self.capture_deadline()
        deadline = timestamp(deadline) if deadline is not None else parent_deadline
        if parent_deadline is not None and deadline is not None:
            deadline = min(deadline, parent_deadline)
        token = _CAPTURE.set((self._run_id, name) if name in COLLECTION_PHASES else None)
        deadline_token = _DEADLINE.set(deadline if name in COLLECTION_PHASES else None)
        failed = False
        try:
            yield self
        except BaseException:
            failed = True
            raise
        finally:
            _CAPTURE.reset(token)
            _DEADLINE.reset(deadline_token)
            ended_at = self.clock()
            with self._lock:
                row = self._data["phases"].setdefault(name, {"startedAt": _at(started_at),
                    "endedAt": None, "elapsedSeconds": 0.0, "count": 0, "failures": 0})
                row["startedAt"] = _first(row["startedAt"], _at(started_at))
                row["endedAt"] = _last(row["endedAt"], _at(ended_at))
                row["elapsedSeconds"] += _seconds(self.monotonic() - started)
                row["count"] += 1
                row["failures"] += int(failed)
                if name in COLLECTION_PHASES and self._data["startedAt"] is not None:
                    self._data["endedAt"] = _last(self._data["endedAt"], _at(ended_at))

    def record_request(self, *, api, node, attempt, outcome, startedAt, endedAt, elapsedSeconds,
                       rowCount=0, **_ignored):
        record = {"api": api, "node": node, "attempt": attempt, "outcome": outcome,
                  "startedAt": _at(startedAt), "endedAt": _at(endedAt),
                  "elapsedSeconds": _seconds(elapsedSeconds),
                  "rowCount": None if rowCount is None else _count(rowCount)}
        with self._lock:
            self._data["startedAt"] = _first(self._data["startedAt"], record["startedAt"])
            self._data["endedAt"] = _last(self._data["endedAt"], record["endedAt"])
            self._data["requestCount"] += 1
            self._data["requestElapsedSeconds"] += record["elapsedSeconds"]
            self._data["requests"] = (self._data["requests"] + [record])[-1024:]
            summary = self._data["interfaces"].setdefault(api, {"requestCount": 0,
                "elapsedSeconds": 0.0, "maximumSeconds": 0.0, "rowCount": 0, "unknownRowCounts": 0})
            summary["requestCount"] += 1
            summary["elapsedSeconds"] += record["elapsedSeconds"]
            summary["maximumSeconds"] = max(summary["maximumSeconds"], record["elapsedSeconds"])
            summary["rowCount"] += _count(record["rowCount"])
            summary["unknownRowCounts"] += int(record["rowCount"] is None)
        logger.info("MeoZ api=%s node=%s attempt=%s outcome=%s elapsed=%.3fs rows=%s",
                    api, node, attempt, outcome, record["elapsedSeconds"], record["rowCount"])

    def record_wait(self, seconds, kind="retry"):
        with self._lock:
            self._data["waits"][kind] = self._data["waits"].get(kind, 0.0) + _seconds(seconds)

    @contextmanager
    def scheduled(self, name):
        """Measure known idle gaps between same-process attempts of one channel."""
        started = self.monotonic()
        with self._lock:
            previous = self._scheduled_end.get(name)
            active = self._scheduled_active.get(name, 0)
            if previous is not None and not active:
                self.record_wait(started - previous, "scheduler:" + name)
            self._scheduled_active[name] = active + 1
        try:
            yield self
        finally:
            with self._lock:
                self._scheduled_active[name] -= 1
                if not self._scheduled_active[name]:
                    self._scheduled_end[name] = self.monotonic()

    def record_cache_hit(self):
        with self._lock:
            self._data["cacheHits"] += 1

    @contextmanager
    def external_operation(self, name):
        """Opaque subprocess work is elapsed work, not a fabricated HTTP count."""
        started_at, started = self.clock(), self.monotonic()
        failed = False
        try:
            with self.phase("external:" + name):
                yield self
        except BaseException:
            failed = True
            raise
        finally:
            elapsed = _seconds(self.monotonic() - started)
            with self._lock:
                row = self._data["operations"].setdefault(name, {"kind": "subprocess", "count": 0,
                    "startedAt": None, "endedAt": None, "elapsedSeconds": 0.0, "failures": 0,
                    "httpRequestCount": None, "httpRequestCountKnown": False})
                row["startedAt"] = _first(row["startedAt"], _at(started_at))
                row["endedAt"] = _last(row["endedAt"], _at(self.clock()))
                row["elapsedSeconds"] += elapsed
                row["count"] += 1
                row["failures"] += int(failed)

    def restore(self, recorded):
        """Restore a prior process once; same-process snapshots are already counted."""
        if not isinstance(recorded, dict):
            return
        with self._lock:
            if self._restored:
                return
            self._restored = True
            if recorded.get("runId") == self._run_id:
                return
            self._data["requestElapsedSeconds"] += _seconds(recorded.get("requestElapsedSeconds"))
            for field in ("requestCount", "cacheHits"):
                self._data[field] += _count(recorded.get(field))
            for kind, seconds in (recorded.get("waits") or {}).items():
                self._data["waits"][kind] = self._data["waits"].get(kind, 0.0) + _seconds(seconds)
            for api, previous in (recorded.get("interfaces") or {}).items():
                if not isinstance(previous, dict):
                    continue
                row = self._data["interfaces"].setdefault(api, {"requestCount": 0,
                    "elapsedSeconds": 0.0, "maximumSeconds": 0.0, "rowCount": 0, "unknownRowCounts": 0})
                row["elapsedSeconds"] += _seconds(previous.get("elapsedSeconds"))
                for field in ("requestCount", "rowCount", "unknownRowCounts"):
                    row[field] += _count(previous.get(field))
                row["maximumSeconds"] = max(row["maximumSeconds"], _seconds(previous.get("maximumSeconds")))
            for name, previous in (recorded.get("operations") or {}).items():
                if not isinstance(previous, dict):
                    continue
                row = self._data["operations"].setdefault(name, {"kind": "subprocess", "count": 0,
                    "startedAt": None, "endedAt": None, "elapsedSeconds": 0.0, "failures": 0,
                    "httpRequestCount": None, "httpRequestCountKnown": False})
                for field in ("count", "failures"):
                    row[field] += _count(previous.get(field))
                row["elapsedSeconds"] += _seconds(previous.get("elapsedSeconds"))
                row["startedAt"] = _first(row["startedAt"], _at(previous.get("startedAt")))
                row["endedAt"] = _last(row["endedAt"], _at(previous.get("endedAt")))
            for name, previous in (recorded.get("phases") or {}).items():
                if not isinstance(previous, dict):
                    continue
                row = self._data["phases"].setdefault(name, {"startedAt": _at(previous.get("startedAt")),
                    "endedAt": None, "elapsedSeconds": 0.0, "count": 0, "failures": 0})
                row["elapsedSeconds"] += _seconds(previous.get("elapsedSeconds"))
                for field in ("count", "failures"):
                    row[field] += _count(previous.get(field))
                row["startedAt"] = _first(row["startedAt"], _at(previous.get("startedAt")))
                row["endedAt"] = _last(row["endedAt"], _at(previous.get("endedAt")))
            previous_start, previous_end = _at(recorded.get("startedAt")), _at(recorded.get("endedAt"))
            if previous_start:
                self._data["startedAt"] = min(previous_start, self._data["startedAt"] or previous_start)
            if previous_end:
                self._data["endedAt"] = max(previous_end, self._data["endedAt"] or previous_end)
                if previous_end < self._created_at:
                    self._data["unknownIntervals"].append({"startedAt": previous_end,
                                                          "endedAt": self._created_at})
            self._data["unknownIntervals"] = (recorded.get("unknownIntervals", [])
                                              + self._data["unknownIntervals"])[-32:]
            # Restore only known fields, even if an old file contains request bodies.
            fields = ("api", "node", "attempt", "outcome", "startedAt", "endedAt", "elapsedSeconds", "rowCount")
            records = [{key: value for key, value in row.items() if key in fields}
                       for row in recorded.get("requests", []) if isinstance(row, dict)]
            self._data["requests"] = (records + self._data["requests"])[-1024:]

    def snapshot(self):
        with self._lock:
            result = deepcopy(self._data)
        result["runId"] = self._run_id
        start = result["startedAt"]
        result["wallSpanSeconds"] = (_seconds((timestamp(result["endedAt"]) - timestamp(start)).total_seconds())
                                     if start and result["endedAt"] else 0.0)
        interfaces = result["interfaces"]
        result["slowestInterface"] = (max(interfaces, key=lambda api: interfaces[api]["elapsedSeconds"])
                                      if interfaces else None)
        result["requestsTruncated"] = result["requestCount"] > len(result["requests"])
        result["httpRequestsComplete"] = not any(row["count"] for row in result["operations"].values())
        return result
