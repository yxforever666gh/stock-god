"""Canonical market facts and explicit field provenance."""

from __future__ import annotations

import math

OBSERVED = "observed"
MISSING = "missing"
INVALID = "invalid"
UNSUPPORTED = "unsupported"
STALE = "stale"

QUOTE_FIELDS = {
    "price": ("CNY/share",),
    "preClose": ("CNY/share",),
    "open": ("CNY/share",),
    "high": ("CNY/share",),
    "low": ("CNY/share",),
    "volume": ("shares",),
    "amount": ("CNY",),
    "turnoverPct": ("percent",),
    "mainFlowCny": ("CNY",),
    "changePct": ("percent",),
}


def _valid(value) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def _field_state(value, *, supported=True, valid=True) -> str:
    if not supported:
        return UNSUPPORTED
    if value is None:
        return MISSING
    return OBSERVED if valid and _valid(value) else INVALID


def field_metadata(values: dict, *, supported: set[str] | None = None, source: str = "", as_of=None) -> dict:
    supported = set(values) if supported is None else set(supported)
    states = {field: _field_state(values.get(field), supported=field in supported) for field in QUOTE_FIELDS}
    return {
        "fieldStatus": states,
        "fieldUnits": {field: units[0] for field, units in QUOTE_FIELDS.items()},
        "sourceId": source,
        "asOf": as_of,
    }


def normalize_quote(raw: dict, *, source: str, volume_unit: str = "shares", amount_unit: str = "CNY") -> dict:
    """Return the stable quote shape used by market and prediction code."""
    result = dict(raw)
    supported = set(raw) | {"volume", "amount", "turnoverPct", "mainFlowCny"}
    original_status = {
        field: _field_state(raw.get(field), supported=field in supported) for field in QUOTE_FIELDS
    }
    volume = raw.get("volume")
    if _valid(volume) and volume_unit == "lots":
        volume *= 100
    elif volume_unit not in {"shares", "lots"}:
        volume = None
    result["volume"] = volume if _valid(volume) else None
    result["amount"] = raw.get("amount") if amount_unit == "CNY" and _valid(raw.get("amount")) else None
    result["turnoverPct"] = raw.get("turnoverPct", raw.get("turnover"))
    result["turnover"] = result["turnoverPct"]  # Compatibility with the existing candidate formula.
    result["mainFlowCny"] = raw.get("mainFlowCny", raw.get("mainFlow"))
    for field in ("price", "preClose", "open", "high", "low", "turnoverPct", "mainFlowCny", "changePct"):
        if not _valid(result.get(field)):
            result[field] = None
    result["mainFlow"] = result["mainFlowCny"]
    result["volumeUnit"] = "shares"
    result["amountUnit"] = "CNY"
    result.update(field_metadata(result, supported=supported, source=source, as_of=result.get("asOf")))
    result["fieldStatus"].update(original_status)
    result["sourceFieldUnits"] = {"volume": volume_unit, "amount": amount_unit}
    if volume_unit not in {"shares", "lots"}:
        result["fieldStatus"]["volume"] = UNSUPPORTED
    if amount_unit not in {"CNY"}:
        result["fieldStatus"]["amount"] = UNSUPPORTED
    return result


def normalize_bar(raw: dict, *, source: str, volume_unit: str = "shares", amount_unit: str = "CNY") -> dict:
    result = dict(raw)
    original_status = {
        field: _field_state(raw.get(field)) for field in ("open", "high", "low", "close", "volume", "amount")
    }
    volume = raw.get("volume")
    if _valid(volume) and volume_unit == "lots":
        volume *= 100
    elif volume_unit not in {"shares", "lots"}:
        volume = None
    result["volume"] = volume if _valid(volume) else None
    result["amount"] = raw.get("amount") if amount_unit == "CNY" and _valid(raw.get("amount")) else None
    for field in ("open", "high", "low", "close"):
        if not _valid(result.get(field)):
            result[field] = None
    result["volumeUnit"] = "shares"
    result["amountUnit"] = "CNY"
    result["fieldStatus"] = original_status
    result["fieldUnits"] = {"volume": "shares", "amount": "CNY"}
    result["sourceId"] = source
    result["sourceFieldUnits"] = {"volume": volume_unit, "amount": amount_unit}
    if volume_unit not in {"shares", "lots"}:
        result["fieldStatus"]["volume"] = UNSUPPORTED
    if amount_unit not in {"CNY"}:
        result["fieldStatus"]["amount"] = UNSUPPORTED
    return result
