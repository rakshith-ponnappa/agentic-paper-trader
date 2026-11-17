# ruff: noqa: E402
import pytest

pytest.importorskip("pandas")
pytest.importorskip("yfinance")

from datetime import date
from unittest.mock import patch

import signals
from autotrader import plan_orders
from signals import buy_gate, majority, size_factor


def test_majority_is_median_of_votes():
    assert majority(["Buy", "Buy", "Sell"]) == "Buy"
    assert majority(["Overweight", "Hold", "Overweight"]) == "Overweight"
    assert majority(["Buy", "Hold", "Sell"]) == "Hold"
    assert majority(["Overweight", "REVIEW", "Underweight"]) == "Hold"


def test_buy_gate_reasons():
    assert buy_gate({"regime_ok": True, "factor_percentile": 0.7, "sentiment": 0.1, "persona": "buy"}) == []
    blocked = buy_gate({"regime_ok": False, "earnings_soon": True, "factor_percentile": 0.2,
                        "sentiment": -0.6, "persona": "avoid"})
    assert len(blocked) == 5


def test_missing_data_does_not_block():
    assert buy_gate({"regime_ok": True, "factor_percentile": None, "sentiment": None}) == []


def test_size_factor_halves_only_proven_losers():
    card = {"Overweight": {"n": 45, "mean_alpha_pct": -0.4}, "Buy": {"n": 3, "mean_alpha_pct": -5.0}}
    assert size_factor("Overweight", card) == 0.5
    assert size_factor("Buy", card) == 1.0  # too few decisions to judge
    assert size_factor("Hold", card) == 1.0


def test_cap_scale_halves_buy_size():
    [o] = plan_orders({"AAPL": "Buy"}, {}, 1000, 1000, True, cap_scale={"AAPL": 0.5})
    assert o["notional"] == 50


def test_earnings_guard_window():
    cal = {"Earnings Date": [date(2026, 11, 16)]}
    with patch.object(signals.yf, "Ticker") as t:
        t.return_value.calendar = cal
        assert signals.earnings_soon("X", date(2026, 11, 13))[0] is True
        assert signals.earnings_soon("X", date(2026, 11, 1))[0] is False
