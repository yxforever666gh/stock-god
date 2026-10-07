"""Reference targets keep unknown minutes and never trigger retired writes."""

import numpy as np
import pytest

from stock_god.cli import start_slot_command
from stock_god.prediction.base43 import reference_label
from stock_god.prediction.base43_reference import (
    ReferenceInputs,
    reference_buy_terms,
    reference_costs,
    reference_sell_terms,
)


def test_conflicting_first_minute_never_becomes_a_reference_fill():
    class Market:
        def bars(self, code, start, end, **kwargs):
            at = start.replace(minute=31).isoformat()
            return [
                dict(time=at, open=10, high=10, low=10, close=10, volume=100000),
                dict(time=at, open=10.01, high=10.01, low=10.01, close=10.01, volume=100000),
            ]

    class Source:
        def rules(self, symbol, day):
            return dict(known=True, eligible=True, suspended=False, reference=1000, lower=900, upper=1100)

    inputs = ReferenceInputs(Market(), Source(), "20261008", "20261009", [{"code": "sz000001"}])
    row = reference_label(
        inputs, "sz000001", 20261008, 20261009, reference_costs, reference_buy_terms, reference_sell_terms
    )
    assert not row["bought"] and not row["sold"] and np.isnan(row["net_pct"])


def test_reference_ticket_charges_each_real_side_to_integer_fen():
    assert reference_buy_terms(999, 100, symbol="sh600000")[0] == 100401
    assert reference_sell_terms(999, 100, symbol="sh600000")[0] == 99349
    assert reference_buy_terms(999, 100, symbol="sz000001")[0] == 100400


def test_retired_cli_apply_refuses_before_opening_any_plan_or_database():
    with pytest.raises(ValueError, match="归档"):
        start_slot_command(None, "nonexistent-plan", "unused", True)
