"""Historical reference tickets, independent of the realtime simulation account."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import numpy as np

from .core import code, local

reference_costs = {"commission": "0.0002", "minimum": 500, "shTransfer": "0.00001", "stamp": "0.0005"}


def _terms(price, quantity, symbol, side):
    notional = int(price) * int(quantity)

    def round_fen(rate):
        return int((Decimal(notional) * Decimal(rate)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    commission = max(500, round_fen(reference_costs["commission"]))
    transfer = round_fen(reference_costs["shTransfer"]) if code(symbol).startswith("sh") else 0
    stamp = round_fen(reference_costs["stamp"]) if side == "sell" else 0
    fees = commission + transfer + stamp
    return ((notional + fees) if side == "buy" else (notional - fees), commission, transfer, stamp)


def reference_buy_terms(price, quantity, *, symbol, config=None):
    return _terms(price, quantity, symbol, "buy")


def reference_sell_terms(price, quantity, *, symbol, config=None):
    return _terms(price, quantity, symbol, "sell")


class ReferenceInputs:
    """Freeze explicit dated rules and unadjusted completed-minute facts only."""

    def __init__(self, market, source, day, nextday, candidates):
        self.market, self.source = market, source
        self.records, self._minutes = {}, {}
        self.days = (int(str(day).replace("-", "")), int(str(nextday).replace("-", "")))
        for candidate in candidates:
            symbol = candidate["code"]
            for date in self.days:
                rule = dict(source.rules(symbol, str(date)))
                self.records[(date, symbol)] = rule

    def minute_arrays(self, symbol, day):
        key = (int(day), symbol)
        if key in self._minutes:
            return self._minutes[key]
        date = local(str(day))
        rows = self.market.bars(
            symbol, date.replace(hour=9, minute=30), date.replace(hour=15), period="1m", adjustment="none"
        )
        prices = np.zeros((241, 4), np.int32)
        volume = np.full(241, np.nan)
        seen = {}
        for row in rows:
            at = local(row.get("time") or row.get("at"))
            if at.date() != date.date() or at.second or at.microsecond:
                continue
            minute = at.hour * 60 + at.minute
            index = (
                0
                if minute == 570
                else minute - 570
                if 571 <= minute <= 690
                else minute - 660
                if 781 <= minute <= 900
                else -1
            )
            if index < 0:
                continue
            values = tuple(row.get(k) for k in ("open", "high", "low", "close", "volume"))
            if index in seen and seen[index] != values:
                prices[index] = 0
                volume[index] = np.nan
                seen[index] = None
                continue
            if index in seen and seen[index] is None:
                continue
            seen[index] = values
            if (
                all(isinstance(v, (int, float)) and np.isfinite(v) and v > 0 for v in values[:4])
                and isinstance(values[4], (int, float))
                and np.isfinite(values[4])
                and values[4] >= 0
                and values[1] >= max(values[0], values[3])
                and values[2] <= min(values[0], values[3])
            ):
                prices[index] = [
                    int((Decimal(str(v)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                    for v in values[:4]
                ]
                volume[index] = values[4]
        self._minutes[key] = prices, volume
        return self._minutes[key]
