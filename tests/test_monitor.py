# ruff: noqa: E402
import pytest

pytest.importorskip("pandas")
pytest.importorskip("yfinance")

from datetime import date

from autotrader import in_cooldown, parse_stop
from monitor import decide


def test_parse_stop_accepts_sane_long_stop():
    assert parse_stop("**Entry Price**: 100\n**Stop Loss**: 92.5", 100) == 92.5
    assert parse_stop("Stop Loss: $1,850.00", 2000) == 1850.0


def test_parse_stop_rejects_stop_above_entry_or_too_wide():
    assert parse_stop("**Stop Loss**: 194.7", 168.9) is None
    assert parse_stop("**Stop Loss**: 50", 100) is None
    assert parse_stop("no stop here", 100) is None


def test_decide_uses_saved_stop_then_default():
    assert decide(91, 100, 50, {"stop": 92})["action"] == "stop"
    assert decide(93, 100, 50, {"stop": 92}) is None
    assert decide(91.9, 100, 50, {})["action"] == "stop"  # default -8%


def test_decide_takes_half_profit_once():
    act = decide(116, 100, 80, {})
    assert act["action"] == "take_profit" and act["notional"] == 40 and act["close"] is False
    assert decide(116, 100, 80, {"took_profit": True}) is None


def test_cooldown_window():
    cooldown = {"NVDA": "2026-11-16"}
    assert in_cooldown("NVDA", date(2026, 11, 15), cooldown)
    assert not in_cooldown("NVDA", date(2026, 11, 16), cooldown)
    assert not in_cooldown("AAPL", date(2026, 11, 15), cooldown)
