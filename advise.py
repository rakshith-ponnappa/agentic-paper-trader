"""Advice only, never trades: analyse your own portfolio (e.g. a broker export) with the same pipeline.

Usage: python advise.py holdings.json [--date YYYY-MM-DD] [--votes 3]
holdings.json: {"cash": null, "positions": [{"ticker": "AAPL", "quantity": 10, "average_price": 150.0}]}
See examples/holdings.example.json. The file is only read; results go to $TA_HOME/advice/ (git-ignored).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

import signals as sig
from autotrader import HOME, PARALLEL, WATCHLIST


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("holdings")
    p.add_argument("--date", default=None, help="analysis date (default: last weekday)")
    p.add_argument("--votes", type=int, default=3)
    p.add_argument("--tickers", help="comma-separated candidates to analyse instead of the holdings")
    args = p.parse_args()

    book_raw = json.loads(Path(args.holdings).read_text())
    avg_price = {x["ticker"]: x.get("average_price") for x in book_raw["positions"]}
    tickers = args.tickers.split(",") if args.tickers else list(avg_price)
    trade_date = args.date or str(sig.yf.download("SPY", period="10d", progress=False).index[-1].date())

    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.portfolio import PortfolioContext

    book = PortfolioContext.model_validate({"currency": "USD", **book_raw})

    def analyse(symbol: str):
        ta = TradingAgentsGraph(selected_analysts=["market", "social", "news", "fundamentals"],
                                config=DEFAULT_CONFIG.copy())
        state, signal = ta.propagate(symbol, trade_date, portfolio=book)
        return signal, str(ta.save_reports(state, symbol))

    votes, reports = defaultdict(list), defaultdict(list)
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        futures = {pool.submit(analyse, t): t for t in tickers for _ in range(args.votes)}
        for f in as_completed(futures):
            t = futures[f]
            try:
                s, r = f.result()
                votes[t].append(s)
                reports[t].append(r)
            except Exception as err:
                votes[t].append("Hold")
                print(f"  {t}: vote ERROR {err}", file=sys.stderr)
            print(f"  {t}: {votes[t]}", flush=True)

    regime = sig.regime_ok()
    factors = sig.factor_scores(sorted(set(WATCHLIST) | set(tickers)))
    today = date.fromisoformat(trade_date)
    out = {"trade_date": trade_date, "regime": regime, "positions": {}}
    for t in tickers:
        row = {"votes": votes[t], "rating": sig.majority(votes[t]), "reports": reports[t],
               "factor": factors.get(t), "earnings": sig.earnings_soon(t, today)[1]}
        row["sentiment"], row["headlines"] = sig.news_sentiment(t)
        try:
            row["persona"] = sig.persona_verdict(t, {"factors": factors.get(t), "rating": row["rating"],
                                                     "average_price": avg_price.get(t)})
        except Exception as err:
            row["persona"] = {"error": str(err)}
        out["positions"][t] = row
        pct = (row["factor"] or {}).get("percentile")
        print(f"{t}: votes {row['votes']} -> {row['rating']} | factor {pct} "
              f"| sentiment {row['sentiment']} | {row['earnings']} | persona {row['persona'].get('verdict')}")
    dest = HOME / "advice" / f"advice-{datetime.now():%Y%m%d-%H%M}.json"
    dest.parent.mkdir(exist_ok=True)
    dest.write_text(json.dumps(out, indent=2, default=str))
    print(f"Saved: {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
