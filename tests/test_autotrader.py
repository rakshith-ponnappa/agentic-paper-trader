# ruff: noqa: E402
import pytest

pytest.importorskip("pandas")
pytest.importorskip("yfinance")

from autotrader import MIN_ORDER_USD, order_payload, plan_orders


def test_buys_capped_per_ticker_and_by_budget():
    orders = plan_orders({"AAPL": "Buy", "MSFT": "Buy", "NVDA": "Buy"}, {}, cash=1000, budget=1000,
                         buys_allowed=True)
    assert [o["notional"] for o in orders] == [100, 100, 100]


def test_many_buys_scaled_to_budget():
    signals = {f"T{i}": "Buy" for i in range(12)}
    orders = plan_orders(signals, {}, cash=5000, budget=1000, buys_allowed=True)
    assert len(orders) == 12 and sum(o["notional"] for o in orders) <= 1000


def test_overweight_targets_half_cap():
    [order] = plan_orders({"AAPL": "Overweight"}, {}, cash=1000, budget=1000, buys_allowed=True)
    assert order["notional"] == 50


def test_sell_closes_and_underweight_halves():
    orders = plan_orders({"AAPL": "Sell", "MSFT": "Underweight"}, {"AAPL": 50, "MSFT": 60},
                         cash=0, budget=200, buys_allowed=True)
    by_symbol = {o["symbol"]: o for o in orders}
    assert by_symbol["AAPL"]["close"] is True
    assert by_symbol["MSFT"]["notional"] == 30 and by_symbol["MSFT"]["close"] is False


def test_hold_and_review_do_nothing():
    assert plan_orders({"AAPL": "Hold", "MSFT": "REVIEW"}, {"AAPL": 50}, 150, 200, True) == []


def test_loss_stop_blocks_buys_but_not_sells():
    orders = plan_orders({"AAPL": "Buy", "MSFT": "Sell"}, {"MSFT": 40}, 160, 200, buys_allowed=False)
    assert [o["side"] for o in orders] == ["sell"]


def test_buys_fit_remaining_budget_and_cash():
    orders = plan_orders({"NVDA": "Buy"}, {"AAPL": 480, "MSFT": 480}, cash=5000, budget=1000,
                         buys_allowed=True)
    assert orders[0]["notional"] == 40


def test_tiny_orders_dropped():
    assert plan_orders({"AAPL": "Buy"}, {"AAPL": 99.5}, 1000, 1000, True) == []
    assert MIN_ORDER_USD == 1.0


def test_buy_with_reference_becomes_capped_limit():
    p = order_payload({"symbol": "TSM", "side": "buy", "notional": 40, "ref_price": 396.48}, "2026-11-12")
    assert p["type"] == "limit" and p["limit_price"] == "400.44"
    assert float(p["qty"]) * 400.44 <= 40
    assert "notional" not in p and p["client_order_id"] == "ta-2026-11-12-TSM-buy"


def test_sell_and_unpriced_buy_stay_market_notional():
    assert order_payload({"symbol": "TSM", "side": "sell", "notional": 20}, "d")["type"] == "market"
    p = order_payload({"symbol": "TSM", "side": "buy", "notional": 20, "ref_price": None}, "d")
    assert p["type"] == "market" and p["notional"] == "20.00"
