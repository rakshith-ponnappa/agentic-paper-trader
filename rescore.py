"""Re-score backtest decisions over several holding windows, from prices only (no LLM calls).

Entry is the first close after the analysis date, as in TradingAgents' own scoring.
Usage: python rescore.py [glob of trading_memory.md files]
"""

from __future__ import annotations

import glob
import re
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd
import yfinance as yf

WINDOWS = (5, 10, 20)
BENCHMARK = "SPY"
TAG = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) \| ([A-Z.\-]+) \| ([A-Za-z]+) \|", re.M)


def decisions(paths: list[str]) -> list[tuple[str, str, str]]:
    found = set()
    for p in paths:
        found.update(TAG.findall(Path(p).read_text(encoding="utf-8")))
    return sorted(found)


def forward_alpha(closes: pd.DataFrame, ticker: str, day: str, window: int) -> float | None:
    after = closes.index[closes.index > pd.Timestamp(day)]
    if len(after) <= window:
        return None
    entry, exit_ = after[0], after[window]
    stock = closes.at[exit_, ticker] / closes.at[entry, ticker] - 1
    bench = closes.at[exit_, BENCHMARK] / closes.at[entry, BENCHMARK] - 1
    return (stock - bench) * 100


def summarise(rows: list[tuple[str, str, str]], closes: pd.DataFrame) -> dict:
    out: dict = defaultdict(lambda: defaultdict(list))
    for day, ticker, rating in rows:
        for w in WINDOWS:
            a = forward_alpha(closes, ticker, day, w)
            if a is not None:
                out[rating][w].append(a)
    return out


def main() -> int:
    paths = sys.argv[1:] or glob.glob(str(Path.home() / ".tradingagents/logs/backtest/*/trading_memory.md"))
    rows = decisions(paths)
    tickers = sorted({t for _, t, _ in rows} | {BENCHMARK})
    start = min(d for d, _, _ in rows)
    closes = yf.download(tickers, start=start, auto_adjust=True, progress=False)["Close"]
    stats = summarise(rows, closes)
    print(f"{len(rows)} decisions, {len(tickers) - 1} tickers, alpha vs {BENCHMARK} (mean / % positive)")
    print("rating        n   " + "   ".join(f"{w:>2}d mean  %pos" for w in WINDOWS))
    for rating in ("Buy", "Overweight", "Hold", "Underweight", "Sell"):
        if rating not in stats:
            continue
        n = len(stats[rating][WINDOWS[0]])
        cells = []
        for w in WINDOWS:
            v = stats[rating][w]
            if not v:
                cells.append("   n/a      ")
                continue
            cells.append(f"{sum(v) / len(v):+6.2f}% {sum(x > 0 for x in v) / len(v):4.0%}")
        print(f"{rating:<12}{n:>3}   " + "   ".join(cells))
    return 0


if __name__ == "__main__":
    sys.exit(main())
