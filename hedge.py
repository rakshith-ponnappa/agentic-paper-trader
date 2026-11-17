"""Hedge screen: which candidates have moved least with (or against) a given portfolio? Price data only.

Usage: python hedge.py holdings.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

CANDIDATES = {
    "Healthcare": ["LLY", "JNJ", "UNH", "ABBV", "MRK", "XLV"],
    "Staples": ["COST", "WMT", "PG", "KO", "PEP", "XLP"],
    "Utilities": ["NEE", "DUK", "SO", "XLU"],
    "Energy": ["XOM", "CVX", "XLE"],
    "Financials": ["JPM", "BRK-B", "V", "MA", "XLF"],
    "Industrials": ["CAT", "GE", "XLI"],
    "Gold": ["GLD", "IAU"],
    "Bonds": ["TLT", "IEF", "SHY", "BND"],
    "Low-vol / broad": ["USMV", "SPLV", "SPY", "VXUS"],
}


def main() -> int:
    book = json.loads(Path(sys.argv[1]).read_text())["positions"]
    held = [p["ticker"] for p in book]
    universe = sorted({t for ts in CANDIDATES.values() for t in ts} | set(held))
    closes = yf.download(universe, period="1y", auto_adjust=True, progress=False)["Close"].dropna(how="all")
    rets = closes.pct_change().dropna(how="all")

    last = closes.iloc[-1]
    value = {p["ticker"]: p["quantity"] * last[p["ticker"]] for p in book}
    total = sum(value.values())
    port = sum(rets[t] * (v / total) for t, v in value.items())
    bad = port <= port.quantile(0.10)  # the portfolio's worst 10% of days

    rows = []
    for sector, tickers in CANDIDATES.items():
        for t in tickers:
            if t in held or t not in rets:
                continue
            r = rets[t].reindex(port.index)
            rows.append({
                "sector": sector, "ticker": t,
                "corr_1y": r.corr(port),
                "beta_to_book": r.cov(port) / port.var(),
                "avg_on_book_worst_days_%": r[bad].mean() * 100,
                "ret_3m_%": (closes[t].iloc[-1] / closes[t].iloc[-64] - 1) * 100,
                "ret_12m_%": (closes[t].iloc[-1] / closes[t].dropna().iloc[0] - 1) * 100,
                "vol_ann_%": r.std() * (252 ** 0.5) * 100,
            })
    df = pd.DataFrame(rows).sort_values("corr_1y").round(2)
    print(f"Book ${total:,.0f}; worst-10% days avg {port[bad].mean() * 100:.2f}%, "
          f"book vol {port.std() * 252 ** 0.5 * 100:.1f}%/yr")
    print(df.to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
