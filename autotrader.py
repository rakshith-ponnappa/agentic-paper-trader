"""Daily paper-trading loop: screen a watchlist, run TradingAgents on the picks and current
holdings, then rebalance a capped USD budget on an Alpaca *paper* account."""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import signals as signals_mod

# Runtime data (runs/, state/, logs/, advice/, the KILL file) lives in $TA_HOME, default: this folder.
HOME = Path(os.environ.get("TA_HOME") or Path(__file__).resolve().parent)
RUNS_DIR = HOME / "runs"
KILL_FILE = HOME / "KILL"
STOPS_FILE = HOME / "state" / "stops.json"         # symbol -> {"stop": price, "took_profit": bool}
COOLDOWN_FILE = HOME / "state" / "cooldown.json"   # symbol -> ISO date it may be bought again
# Paper only. There is deliberately no live endpoint in this script.
ALPACA_URL = "https://paper-api.alpaca.markets"

WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "AVGO", "TSLA", "TSM", "LLY",
    "WMT", "JPM", "V", "MA", "MU", "AMD", "XOM", "JNJ", "COST", "NFLX",
]
BUDGET_USD = float(os.environ.get("AUTOTRADER_BUDGET_USD", "1000"))
MAX_TICKER_FRACTION = 0.10  # Buy = 10% of the budget, Overweight = 5%
MAX_PICKS = 3               # new candidates analysed per day
MAX_ORDERS = 25             # per run
MIN_ORDER_USD = 1.0
LOSS_STOP_FRACTION = 0.15   # no new buys once unrealised loss exceeds 15% of the budget
DEFAULT_STOP_FRACTION = 0.08  # stop 8% under entry when the trader gave no usable stop
TAKE_PROFIT_FRACTION = 0.15   # sell half once a position is up 15%
COOLDOWN_DAYS = 5             # no re-buy for 5 calendar days after a stop-out
MAX_BUY_PREMIUM = 0.01        # buy limit = analysed close + 1%; unfilled orders expire at the close
PARALLEL = 5                  # tickers analysed at once
VOTES = 3                     # TradingAgents runs per ticker before it is traded
ANALYSTS = ["market", "social", "news", "fundamentals"]


def load_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def notify(title: str, message: str, open_target: str | None = None) -> None:
    """macOS notification that opens `open_target` (URL or file) when clicked; never raises."""
    notifier = shutil.which("terminal-notifier") or "/opt/homebrew/bin/terminal-notifier"
    if Path(notifier).exists():
        cmd = [notifier, "-title", title, "-message", message[:240], "-group", f"ta-{title}"]
        if open_target:
            cmd += ["-open", open_target if "://" in open_target else Path(open_target).resolve().as_uri()]
        subprocess.run(cmd, check=False, capture_output=True)
        return
    script = f"display notification {json.dumps(message[:240])} with title {json.dumps(title)}"
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


def write_summary(record: dict, path: Path) -> None:
    """One-page HTML view of a daily run, linking each full agent report."""
    esc = html.escape
    rows = "".join(
        f"<tr><td>{esc(s)}</td><td>{esc(str(sig))}</td><td>"
        + (f'<a href="{Path(record["reports"][s]).with_suffix(".html").as_uri()}">report</a>'
           if s in record.get("reports", {}) else "")
        + "</td></tr>"
        for s, sig in record.get("signals", {}).items())
    orders = "".join(
        f"<li>{esc(o['side'])} {esc(o['symbol'])} ${o['notional']}"
        f"{' limit ' + str(o['limit_price']) if o.get('limit_price') else ''}: "
        f"{esc(str(o.get('result', {}).get('status', o.get('error', 'not submitted'))))}</li>"
        for o in record.get("orders", []))
    path.write_text(
        f"<html><body style='font-family:-apple-system;max-width:760px;margin:2em auto'>"
        f"<h2>Autotrader {esc(record['trade_date'])}</h2>"
        f"<p>Book ${sum(record.get('positions', {}).values()):.2f} &middot; unrealised P/L "
        f"${record.get('unrealised_pl', 0):+.2f} &middot; picks {esc(', '.join(record.get('picks', [])))}</p>"
        f"<table border=1 cellpadding=6><tr><th>Ticker</th><th>Rating</th><th></th></tr>{rows}</table>"
        f"<h3>Orders</h3><ul>{orders or '<li>none</li>'}</ul>"
        f"<h3>Errors</h3><pre>{esc(json.dumps(record.get('errors', {}), indent=2))}</pre>"
        f"<p><a href='https://app.alpaca.markets/paper/dashboard/overview'>Alpaca paper dashboard</a></p>"
        "</body></html>")


def parse_stop(trader_plan: str, entry: float) -> float | None:
    """Trader's stop-loss if it is a sane long stop (1-20% under entry), else None."""
    found = re.search(r"Stop Loss\**:?\**\s*\$?([\d,]+(?:\.\d+)?)", trader_plan or "")
    if not found or entry <= 0:
        return None
    stop = float(found.group(1).replace(",", ""))
    return stop if entry * 0.80 <= stop <= entry * 0.99 else None


def in_cooldown(symbol: str, today: date, cooldown: dict) -> bool:
    until = cooldown.get(symbol)
    return bool(until) and today < date.fromisoformat(until)


def target_value(signal: str, current: float, cap: float) -> float:
    """Dollar value the position should hold after acting on a 5-tier rating."""
    if signal == "Sell":
        return 0.0
    if signal == "Underweight":
        return current * 0.5
    if signal == "Overweight":
        return max(current, cap * 0.5)
    if signal == "Buy":
        return max(current, cap)
    return current  # Hold, REVIEW or anything unparsed: leave it alone


def _cents(x: float) -> float:
    return math.floor(x * 100) / 100


def plan_orders(signals: dict[str, str], positions: dict[str, float], cash: float,
                budget: float, buys_allowed: bool, cap_scale: dict[str, float] | None = None) -> list[dict]:
    """Turn ratings into notional orders: sells first, buys scaled to fit cash and the budget."""
    sells, buys = [], []
    for symbol, signal in signals.items():
        cap = budget * MAX_TICKER_FRACTION * (cap_scale or {}).get(symbol, 1.0)
        current = positions.get(symbol, 0.0)
        target = target_value(signal, current, cap)
        delta = target - current
        if delta <= -MIN_ORDER_USD:
            sells.append({"symbol": symbol, "side": "sell", "notional": _cents(-delta),
                          "close": target == 0.0, "signal": signal})
        elif delta >= MIN_ORDER_USD and buys_allowed:
            buys.append({"symbol": symbol, "side": "buy", "notional": delta, "signal": signal})

    exposure_after_sells = sum(positions.values()) - sum(o["notional"] for o in sells)
    # Sell proceeds are not counted: they may not have filled when the buys go in.
    room = max(0.0, min(cash, budget - exposure_after_sells))
    wanted = sum(o["notional"] for o in buys)
    scale = min(1.0, room / wanted) if wanted else 0.0
    sized = []
    for o in buys:
        o["notional"] = _cents(o["notional"] * scale)
        if o["notional"] >= MIN_ORDER_USD:
            sized.append(o)
    return (sells + sized)[:MAX_ORDERS]


class Alpaca:
    def __init__(self, key_id: str, secret: str):
        self.session = requests.Session()
        self.session.headers.update({"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret})
        # POSTs are retried too: the client_order_id turns a repeat into a rejected duplicate.
        retry = Retry(total=4, connect=4, read=4, backoff_factor=2,
                      status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=None, raise_on_status=False)
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def _call(self, method: str, path: str, **kwargs):
        r = self.session.request(method, ALPACA_URL + path, timeout=30, **kwargs)
        if r.status_code == 422 and "client_order_id" in r.text and method == "POST":
            # A retried POST whose first attempt went through: return the order that exists.
            cid = kwargs["json"]["client_order_id"]
            return self._call("GET", f"/v2/orders:by_client_order_id?client_order_id={cid}")
        if r.status_code >= 400:
            raise RuntimeError(f"Alpaca {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    def account(self):
        return self._call("GET", "/v2/account")

    def positions(self):
        return self._call("GET", "/v2/positions")

    def clock(self):
        return self._call("GET", "/v2/clock")

    def submit(self, order: dict, trade_date: str):
        if order.get("close"):
            return self._call("DELETE", f"/v2/positions/{order['symbol']}")
        return self._call("POST", "/v2/orders", json=order_payload(order, trade_date))


def order_payload(order: dict, trade_date: str) -> dict:
    """Buys with a reference price become fractional-qty limit orders capped just above it,
    so a gap up at the open is not chased; everything else is a notional market order."""
    payload = {"symbol": order["symbol"], "side": order["side"], "time_in_force": "day",
               # Same id on a re-run the same day makes Alpaca reject the duplicate.
               "client_order_id": f"ta-{trade_date}-{order['symbol']}-{order['side']}"}
    ref = order.get("ref_price")
    if order["side"] == "buy" and ref:
        limit = round(ref * (1 + MAX_BUY_PREMIUM), 2)
        qty = math.floor(order["notional"] / limit * 1e9) / 1e9
        return {**payload, "type": "limit", "limit_price": f"{limit:.2f}", "qty": f"{qty:.9f}"}
    return {**payload, "type": "market", "notional": f"{order['notional']:.2f}"}


def screen(tickers: list[str]) -> list[dict]:
    import yfinance as yf

    closes = yf.download(tickers, period="1y", interval="1d", auto_adjust=True,
                         progress=False)["Close"]
    rows = []
    for t in tickers:
        s = closes[t].dropna() if t in closes else None
        if s is None or len(s) < 200:
            continue
        diff = s.diff().tail(14)
        gain, loss = diff.clip(lower=0).mean(), -diff.clip(upper=0).mean()
        rsi = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
        last = float(s.iloc[-1])
        rows.append({
            "ticker": t, "close": round(last, 2),
            "ret_1m_pct": round((last / s.iloc[-22] - 1) * 100, 1),
            "ret_3m_pct": round((last / s.iloc[-64] - 1) * 100, 1),
            "vs_sma200_pct": round((last / s.tail(200).mean() - 1) * 100, 1),
            "rsi14": round(float(rsi), 1),
        })
    return rows


def choose_picks(metrics: list[dict], held: list[str], n: int) -> tuple[list[str], str]:
    """Ask the deep model for the n most promising candidates; fall back to 3-month momentum."""
    from openai import OpenAI

    allowed = {m["ticker"] for m in metrics}
    prompt = (
        f"You manage a ${BUDGET_USD:.0f} long-only US equity paper portfolio. Currently held: "
        f"{', '.join(held) or 'nothing'}. From the table below pick up to {n} tickers NOT already "
        "held that deserve a full research run today for a possible buy. Prefer a sound "
        "risk/reward over pure momentum. Reply with JSON only: "
        '{"picks": [{"ticker": "...", "reason": "..."}]}\n\n' + json.dumps(metrics)
    )
    try:
        client = OpenAI(base_url=os.environ["TRADINGAGENTS_LLM_BACKEND_URL"],
                        api_key=os.environ["OPENAI_API_KEY"], timeout=600)
        reply = client.chat.completions.create(
            model=os.environ["TRADINGAGENTS_DEEP_THINK_LLM"],
            messages=[{"role": "user", "content": prompt}],
        ).choices[0].message.content or ""
        found = re.search(r"\{.*\}", reply, re.S)
        picks = [p["ticker"] for p in json.loads(found.group())["picks"]] if found else []
        picks = [t for t in picks if t in allowed and t not in held][:n]
        if picks:
            return picks, reply
    except Exception as err:  # the run continues on the fallback, and says so
        reply = f"LLM screen failed: {err}"
    ranked = sorted((m for m in metrics if m["ticker"] not in held),
                    key=lambda m: m["ret_3m_pct"], reverse=True)
    return [m["ticker"] for m in ranked[:n]], reply + "\n(fallback: 3-month momentum)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="plan orders but never submit")
    parser.add_argument("--date", help="analysis date (default: today in New York)")
    parser.add_argument("--max-picks", type=int, default=MAX_PICKS)
    parser.add_argument("--all", action="store_true", help="analyse every watchlist ticker, skip the screen")
    parser.add_argument("--slot", default="open", help="run slot name; one real run per slot per day")
    parser.add_argument("--parallel", type=int, default=PARALLEL, help="tickers analysed at once")
    args = parser.parse_args()

    if KILL_FILE.exists():
        print(f"Kill switch present ({KILL_FILE}); not running.")
        return 0

    trade_date = args.date or datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    if not args.dry_run and any(
            not (r := json.loads(p.read_text())).get("dry_run") and r.get("slot", "open") == args.slot
            for p in RUNS_DIR.glob(f"{trade_date}-*.json")):
        print(f"A real '{args.slot}' run for {trade_date} already exists in {RUNS_DIR}.")
        return 0
    record: dict = {"started": datetime.now().isoformat(timespec="seconds"), "trade_date": trade_date,
                    "slot": args.slot, "dry_run": args.dry_run, "budget_usd": BUDGET_USD}

    key_id, secret = os.environ.get("ALPACA_KEY_ID"), os.environ.get("ALPACA_SECRET_KEY")
    alpaca = Alpaca(key_id, secret) if key_id and secret else None
    if alpaca is None and not args.dry_run:
        print("Alpaca paper keys not found (ALPACA_KEY_ID / ALPACA_SECRET_KEY from env or Keychain); "
              "use --dry-run or add them.", file=sys.stderr)
        return 1
    # launchd replays missed slots after a sleep; a closed market makes that run pure cost.
    if not args.dry_run and not alpaca.clock().get("is_open"):
        print(f"Market closed; skipping the '{args.slot}' run (no LLM requests used).")
        notify("Autotrader skipped", f"'{args.slot}' run started while the market was closed "
               "(Mac asleep at the scheduled time?).", str(HOME / "logs"))
        return 0

    raw_positions = [p for p in (alpaca.positions() if alpaca else []) if p["symbol"] in WATCHLIST]
    positions = {p["symbol"]: float(p["market_value"]) for p in raw_positions}
    unrealised = sum(float(p["unrealized_pl"]) for p in raw_positions)
    cash = float(alpaca.account()["cash"]) if alpaca else BUDGET_USD
    buys_allowed = unrealised > -LOSS_STOP_FRACTION * BUDGET_USD
    record.update(positions=positions, cash=cash, unrealised_pl=unrealised, buys_allowed=buys_allowed)

    metrics = screen(WATCHLIST)
    closes = {m["ticker"]: m["close"] for m in metrics}
    cooldown = load_json(COOLDOWN_FILE)
    today = date.fromisoformat(trade_date)
    held = sorted(positions)
    candidates = [m for m in metrics if not in_cooldown(m["ticker"], today, cooldown)]
    if args.all:
        picks = [m["ticker"] for m in candidates if m["ticker"] not in held]
        screen_reply = "--all: whole watchlist analysed, no LLM screen"
    else:
        picks, screen_reply = choose_picks(candidates, held, args.max_picks)
    record.update(screen=metrics, picks=picks, screen_reply=screen_reply)
    print(f"[{trade_date}] held={held} picks={picks} buys_allowed={buys_allowed}")

    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.portfolio import PortfolioContext

    book = PortfolioContext.model_validate({
        "cash": round(min(cash, max(0.0, BUDGET_USD - sum(positions.values()))), 2),
        "currency": "USD",
        "positions": [{"ticker": p["symbol"], "quantity": float(p["qty"]),
                       "average_price": float(p["avg_entry_price"])} for p in raw_positions],
    })

    def analyse(symbol: str):
        # One graph per ticker: graphs hold per-run state; the memory log locks its own writes.
        ta = TradingAgentsGraph(selected_analysts=ANALYSTS, config=DEFAULT_CONFIG.copy())
        state, signal = ta.propagate(symbol, trade_date, portfolio=book)
        return signal, state.get("trader_investment_plan", ""), str(ta.save_reports(state, symbol))

    signals, reports, errors, plans = {}, {}, {}, {}
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        futures = {pool.submit(analyse, s): s for s in dict.fromkeys(picks + held)}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                signals[symbol], plans[symbol], reports[symbol] = future.result()
                print(f"  {symbol}: {signals[symbol]}")
            except Exception as err:  # one ticker failing must not stop the others; no signal = no trade
                errors[symbol] = str(err)
                print(f"  {symbol}: ERROR {err}", file=sys.stderr)
    record.update(signals=signals, reports=reports, errors=errors)

    # Majority vote: every ticker the first pass would trade is analysed twice more.
    first_orders = plan_orders(signals, positions, cash, BUDGET_USD, buys_allowed)
    to_vote = sorted({o["symbol"] for o in first_orders})
    votes = {s: [signals[s]] for s in to_vote}
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        futures = {pool.submit(analyse, s): s for s in to_vote for _ in range(VOTES - 1)}
        for future in as_completed(futures):
            try:
                votes[futures[future]].append(future.result()[0])
            except Exception as err:  # a failed vote counts as Hold
                votes[futures[future]].append("Hold")
                print(f"  vote {futures[future]}: ERROR {err}", file=sys.stderr)
    for s, v in votes.items():
        signals[s] = signals_mod.majority(v)
        print(f"  {s}: votes {v} -> {signals[s]}")
    record["votes"] = votes

    # Buy gate: free checks first, the LLM persona call only for buys that pass them.
    regime, regime_note = signals_mod.regime_ok()
    factors = signals_mod.factor_scores(WATCHLIST)
    card = signals_mod.scorecard()
    gate = {}
    for s, rating in list(signals.items()):
        if rating not in ("Buy", "Overweight"):
            continue
        soon, earnings_note = signals_mod.earnings_soon(s, today)
        checks = {"regime_ok": regime, "regime": regime_note, "earnings_soon": soon,
                  "earnings": earnings_note, "factor_percentile": factors.get(s, {}).get("percentile")}
        try:
            checks["sentiment"], checks["headlines"] = signals_mod.news_sentiment(s)
        except Exception as err:
            checks["sentiment_error"] = str(err)
        if not signals_mod.buy_gate(checks):
            try:
                p = signals_mod.persona_verdict(s, {"factors": factors.get(s), "rating": rating})
                checks["persona"], checks["persona_detail"] = p["verdict"], p
            except Exception as err:
                checks["persona_error"] = str(err)
        checks["blocked"] = signals_mod.buy_gate(checks)
        if checks["blocked"]:
            signals[s] = "Hold"  # keeps an existing position, opens nothing
        gate[s] = checks
        print(f"  {s}: gate {'BLOCKED ' + str(checks['blocked']) if checks['blocked'] else 'passed'}")
    cap_scale = {s: signals_mod.size_factor(r, card) for s, r in signals.items()}
    record.update(final_signals=signals, gate=gate, factors=factors, scorecard=card, cap_scale=cap_scale)

    orders = plan_orders(signals, positions, cash, BUDGET_USD, buys_allowed, cap_scale)
    orders = [o for o in orders if o["side"] == "sell" or not in_cooldown(o["symbol"], today, cooldown)]
    record["orders"] = orders
    market_open = bool(alpaca and alpaca.clock().get("is_open"))
    if args.dry_run or not market_open:
        print(f"Not submitting ({'dry run' if args.dry_run else 'market closed'}). Planned: {orders}")
    else:
        stops = load_json(STOPS_FILE)
        for order in orders:
            symbol = order["symbol"]
            if order["side"] == "buy":
                order["ref_price"] = closes.get(symbol)
            try:
                order["result"] = alpaca.submit(order, f"{trade_date}-{args.slot}")
                print(f"  submitted {order['side']} {symbol} ${order['notional']}")
            except Exception as err:
                order["error"] = str(err)
                print(f"  FAILED {order['side']} {symbol}: {err}", file=sys.stderr)
                continue
            if order["side"] == "buy":
                previous = stops.get(symbol, {})
                stops[symbol] = {"stop": parse_stop(plans.get(symbol, ""), closes.get(symbol, 0.0)),
                                 "took_profit": previous.get("took_profit", False)}
            elif order.get("close"):
                stops.pop(symbol, None)
        save_json(STOPS_FILE, stops)
        record["stops"] = stops

    traded = [f"{o['side']} {o['symbol']} ${o['notional']}" for o in orders if "result" in o]
    failed = [o["symbol"] for o in orders if "error" in o] + list(errors)

    RUNS_DIR.mkdir(exist_ok=True)
    out = RUNS_DIR / f"{trade_date}-{args.slot}-{datetime.now():%H%M%S}.json"
    out.write_text(json.dumps(record, indent=2, default=str))
    summary = out.with_suffix(".html")
    write_summary(record, summary)
    notify("TradingAgents daily run",
           f"Book ${sum(positions.values()):.2f}, P/L ${unrealised:+.2f}. "
           f"Ratings: {signals or 'none'}. Orders: {traded or 'none'}."
           + (f" FAILED: {failed}" if failed else ""), str(summary))
    print(f"Run record: {out}")
    return 1 if errors and not signals else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as crash:
        notify("Autotrader CRASHED", f"{type(crash).__name__}: {crash}", str(HOME / "logs"))
        raise
