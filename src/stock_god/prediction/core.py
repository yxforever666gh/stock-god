"""Prediction time, exchange lot and fee rules; no storage or provider access."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
LEGACY_SLOTS = tuple(f"{minute // 60:02}:{minute % 60:02}" for minute in range(570, 690, 5))
SLOTS = ("base43",)
DEFAULT_SLOT = "base43"
TARGET_BUYS = 2
ALLOCATION_POLICY = "remaining_cash_by_open_slots"
STRATEGY_VERSION = "base43-v1"


class PredictionError(ValueError):
    pass


class NotFound(PredictionError):
    pass


class Conflict(PredictionError):
    pass


def local(value=None) -> datetime:
    if value is None:
        return datetime.now(SHANGHAI)
    if isinstance(value, datetime):
        return value.replace(tzinfo=SHANGHAI) if value.tzinfo is None else value.astimezone(SHANGHAI)
    text = str(value).strip()
    if not text or text.startswith("0001-"):
        raise PredictionError("缺少有效时间")
    # GORM stores a space before the UTC offset in older databases.
    text = re.sub(r" ([+-]\d{2}:\d{2})$", r"\1", text).replace("Z", "+00:00")
    try:
        if text.isdigit() and len(text) in (10, 13):
            return datetime.fromtimestamp(int(text) / (1000 if len(text) == 13 else 1), SHANGHAI)
        if text.isdigit() and len(text) == 8:
            return datetime.strptime(text, "%Y%m%d").replace(tzinfo=SHANGHAI)
        return local(datetime.fromisoformat(text))
    except (ValueError, OverflowError) as error:
        raise PredictionError("无效时间：" + text) from error


def parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        return local(value)
    except PredictionError:
        return None


def stamp(value):
    return local(value).isoformat(timespec="microseconds") if value else None


def valid_slot(slot=DEFAULT_SLOT):
    if slot not in SLOTS + LEGACY_SLOTS:
        raise PredictionError(f"无效股票预测时段：{slot}")
    return slot


def slot_at(at):
    at = local(at)
    minute = at.hour * 60 + at.minute
    return f"{minute // 60:02}:{minute // 5 * 5 % 60:02}" if 570 <= minute < 690 else ""


def slot_time(at, slot):
    hour, minute = (9, 30) if valid_slot(slot) == DEFAULT_SLOT else map(int, slot.split(":"))
    return local(at).replace(hour=hour, minute=minute, second=0, microsecond=0)


def continuous(at):
    minute = local(at).hour * 60 + local(at).minute
    # Preserve the existing simulator's sell/recovery window through midday.
    # New buying is independently stopped at 11:30 by slot_at/publication checks.
    return 570 <= minute < 900


def code(value):
    text = str(value or "").strip().lower()
    if len(text) == 9 and text[6] == ".":
        text = text[7:] + text[:6]
    if not re.fullmatch(r"(sh|sz)?(60|68|00|30)\d{4}", text):
        raise PredictionError("不支持的 A 股代码")
    digits = text[-6:]
    return ("sh" if digits.startswith(("60", "68")) else "sz") + digits


def positive(value):
    return isinstance(value, (int, float)) and math.isfinite(value) and 0 < value < 1e100


def lot_size(stock_code):
    return 200 if code(stock_code).startswith("sh68") else 100


def trade_cost(stock_code, price, quantity, side="buy") -> dict[str, float]:
    stock_code = code(stock_code)
    if not positive(price) or quantity <= 0:
        raise PredictionError("成交价格和数量必须为正数")
    notional = price * quantity
    commission = max(5.0, notional * 0.0002)
    transfer = notional * 0.00001 if stock_code.startswith("sh") else 0.0
    stamp_duty = notional * 0.0005 if side == "sell" else 0.0
    fees = commission + transfer + stamp_duty
    return {
        "execution_price": price,
        "commission": commission,
        "transfer_fee": transfer,
        "stamp_duty": stamp_duty,
        "slippage_amount": 0.0,
        "net_cash_flow": notional - fees if side == "sell" else -(notional + fees),
    }


def base43_trade_cost(stock_code, price, quantity, side="buy"):
    """Round each actually charged fee to cents; preserve archived fee semantics."""
    from decimal import Decimal, ROUND_HALF_UP

    result = trade_cost(stock_code, price, quantity, side)
    notional = Decimal(str(price)) * quantity
    cent = Decimal("0.01")
    commission = max(Decimal("5"), notional * Decimal("0.0002"))
    transfer = notional * Decimal("0.00001") if code(stock_code).startswith("sh") else Decimal(0)
    duty = notional * Decimal("0.0005") if side == "sell" else Decimal(0)
    fees = [fee.quantize(cent, rounding=ROUND_HALF_UP) for fee in (commission, transfer, duty)]
    for key, fee in zip(("commission", "transfer_fee", "stamp_duty"), fees):
        result[key] = float(fee)
    result["net_cash_flow"] = float(notional - sum(fees) if side == "sell" else -(notional + sum(fees)))
    return result


def size_buy(stock_code, price, cash, slots, policy=ALLOCATION_POLICY, base=None):
    if slots <= 0 or not positive(price) or not positive(cash):
        raise PredictionError("可用现金或买入名额不足")
    lot = lot_size(stock_code)
    one = -trade_cost(stock_code, price, lot)["net_cash_flow"]
    fixed = policy != ALLOCATION_POLICY and base is not None and math.isfinite(base) and base >= 0
    allocation_limit = base / 5 if fixed and base is not None else cash / slots
    cap = min(cash, allocation_limit)
    if one >= allocation_limit if fixed else one > allocation_limit:
        if one > cash + 1e-8:
            raise PredictionError("可用现金不足支付一手含费成本")
        return lot, trade_cost(stock_code, price, lot)
    quantity = math.floor(cap / price / lot) * lot
    while quantity >= lot:
        costs = trade_cost(stock_code, price, quantity)
        spent = -costs["net_cash_flow"]
        if spent < allocation_limit and spent <= cash + 1e-8 if fixed else spent <= cap + 1e-8:
            return quantity, costs
        quantity -= lot
    raise PredictionError("可用现金不足支付一手含费成本")


def upper_limit(previous_close, rate=0.1):
    if not positive(previous_close) or not positive(rate):
        return 0.0
    cents = math.floor(previous_close * 100 + 0.5)
    basis = math.floor(rate * 10000 + 0.5)
    return ((cents * (10000 + basis) + 5000) // 10000) / 100


def fresh_quote(at, now):
    if not at:
        return False
    at = parse_time(at)
    return at is not None and -5 <= (local(now) - at).total_seconds() <= 60


def next_session(market, at, slot):
    for offset in range(1, 21):
        day = local(at) + timedelta(days=offset)
        if market.is_trading_day(day):
            return slot_time(day, slot)
    raise PredictionError("20日内找不到下一A股交易日")


def json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str, allow_nan=False)


def dto(row) -> dict[str, Any]:
    names = {"net_pn_l": "netPnl", "period_pn_l": "periodPnl"}
    hidden = {
        "historical_replay_id",
        "historical_sell_blocked",
        "allocation_base_cash",
        "allocation_policy",
        "hit_five_before_sell",
        "hit_limit_up_full_day",
        "hit_minus_three",
        "metrics_finalized",
    }
    booleans = {
        "published",
        "on_time",
        "degraded",
        "late",
        "legacy_slot_exception",
        "price_stale",
        "external",
    }
    optional_text = {
        "chain_id",
        "parent_run_id",
        "strategy_version",
        "evidence_profile_version",
        "evidence_set_id",
        "replaces_recommendation_id",
        "promotion_reason",
    }
    omitted_nulls = optional_text | {
        "completed_at",
        "execution_quote_at",
        "current_price_at",
        "buy_day_limit_evaluated_at",
        "baseline_value",
        "period_pn_l",
    }
    result = {}
    for key, value in dict(row).items():
        if key in hidden:
            continue
        if value is None and key in omitted_nulls:
            continue  # These Go fields used omitempty; retain absence without inventing a value.
        if key == "buy_day_limit_outcome" and not value:
            continue
        if value is None and key in {"slot", "archive_reason", "winner_run_id"}:
            value = ""
        name = names.get(key, key.split("_")[0] + "".join(p.title() for p in key.split("_")[1:]))
        if key in booleans and value is not None:
            value = bool(value)
        if value and (key.endswith("_at") or key in {"scheduled_for"}):
            value = stamp(value)
        result[name] = value
    return result
