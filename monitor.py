"""Intraday guard, run every 15 minutes: stop-loss and take-profit on the autotrader's positions.
No LLM calls, so it works without the LLM gateway."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from autotrader import (
    COOLDOWN_DAYS,
    COOLDOWN_FILE,
    DEFAULT_STOP_FRACTION,
    KILL_FILE,
    MIN_ORDER_USD,
    STOPS_FILE,
    TAKE_PROFIT_FRACTION,
    WATCHLIST,
    Alpaca,
    _cents,
    load_json,
    notify,
    save_json,
)

ALPACA_DASHBOARD = "https://app.alpaca.markets/paper/dashboard/overview"


def decide(price: float, entry: float, market_value: float, saved: dict) -> dict | None:
    """Return the protective action for one position, or None."""
    stop = saved.get("stop") or entry * (1 - DEFAULT_STOP_FRACTION)
    if price <= stop:
        return {"action": "stop", "close": True, "reason": f"price {price:.2f} <= stop {stop:.2f}"}
    if not saved.get("took_profit") and price >= entry * (1 + TAKE_PROFIT_FRACTION):
        half = _cents(market_value / 2)
        if half >= MIN_ORDER_USD:
            return {"action": "take_profit", "close": False, "notional": half,
                    "reason": f"price {price:.2f} >= +{TAKE_PROFIT_FRACTION:.0%} of {entry:.2f}"}
    return None


def main() -> int:
    if KILL_FILE.exists():
        return 0
    alpaca = Alpaca(os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"])
    if not alpaca.clock().get("is_open"):
        return 0

    today = datetime.now(ZoneInfo("America/New_York")).date()
    stops, cooldown = load_json(STOPS_FILE), load_json(COOLDOWN_FILE)
    for p in alpaca.positions():
        symbol = p["symbol"]
        if symbol not in WATCHLIST:
            continue
        saved = stops.get(symbol, {})
        act = decide(float(p["current_price"]), float(p["avg_entry_price"]), float(p["market_value"]), saved)
        if act is None:
            continue
        order = {"symbol": symbol, "side": "sell", "close": act["close"], "notional": act.get("notional", 0)}
        try:
            alpaca.submit(order, f"{today.isoformat()}-{act['action']}")
        except Exception as err:
            notify("Autotrader FAILED", f"{act['action']} {symbol}: {err}", ALPACA_DASHBOARD)
            print(f"FAILED {act['action']} {symbol}: {err}", file=sys.stderr)
            continue
        if act["close"]:
            stops.pop(symbol, None)
            cooldown[symbol] = (today + timedelta(days=COOLDOWN_DAYS)).isoformat()
        else:
            stops[symbol] = {**saved, "took_profit": True}
        msg = f"{act['action']} {symbol}: {act['reason']}"
        notify("Autotrader", msg, ALPACA_DASHBOARD)
        print(f"{datetime.now():%H:%M} {msg}")
    save_json(STOPS_FILE, stops)
    save_json(COOLDOWN_FILE, cooldown)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as crash:
        notify("Autotrader monitor CRASHED", f"{type(crash).__name__}: {crash}", ALPACA_DASHBOARD)
        raise
