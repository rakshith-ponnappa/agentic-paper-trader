"""Would the free buy checks have helped? Replays the point-in-time ones over past decisions.

Testable as of each decision date: market trend (SPY vs 200d), earnings guard, and the price
factors (12-1 momentum, 63d volatility) ranked across the watchlist. Not testable: FinBERT and
the persona check (no news archive), ROE/margin (only today's values), and the vote (needs reruns).
"""

from __future__ import annotations

import glob
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import yfinance as yf

from autotrader import WATCHLIST
from rescore import BENCHMARK, decisions, forward_alpha
from signals import EARNINGS_GUARD_DAYS, MIN_FACTOR_PERCENTILE

WINDOWS = (5, 10, 20)


def price_factor_percentile(closes: pd.DataFrame, day: pd.Timestamp) -> dict[str, float]:
    hist = closes.loc[:day]
    rows = {}
    for t in WATCHLIST:
        s = hist[t].dropna() if t in hist else pd.Series(dtype=float)
        if len(s) >= 260:
            rows[t] = {"mom": s.iloc[-21] / s.iloc[-252] - 1, "vol": s.pct_change().tail(63).std()}
    df = pd.DataFrame(rows).T
    score = pd.concat([df["mom"].rank(pct=True), (-df["vol"]).rank(pct=True)], axis=1).mean(axis=1)
    return score.rank(pct=True).to_dict()


def earnings_dates(tickers: set[str]) -> dict[str, list[date]]:
    out = {}
    for t in tickers:
        try:
            out[t] = sorted({d.date() for d in yf.Ticker(t).get_earnings_dates(limit=12).index})
        except Exception:
            out[t] = []
    return out


def main() -> int:
    paths = sys.argv[1:] or glob.glob(str(Path.home() / ".tradingagents/logs/backtest/*/trading_memory.md"))
    rows = [r for r in decisions(paths) if r[2] in ("Buy", "Overweight")]
    start = pd.Timestamp(min(d for d, _, _ in rows)) - pd.Timedelta(days=420)
    closes = yf.download(sorted(set(WATCHLIST) | {BENCHMARK}), start=start.date().isoformat(),
                         auto_adjust=True, progress=False)["Close"]
    spy_sma = closes[BENCHMARK].rolling(200).mean()
    earnings = earnings_dates({t for _, t, _ in rows})

    results = []
    for day, ticker, rating in rows:
        ts = closes.index[closes.index <= pd.Timestamp(day)][-1]
        d = date.fromisoformat(day)
        upcoming = [e for e in earnings.get(ticker, []) if e >= d]
        checks = {
            "regime": closes.at[ts, BENCHMARK] > spy_sma.at[ts],
            "earnings": not (upcoming and (upcoming[0] - d).days <= EARNINGS_GUARD_DAYS),
            "factor": price_factor_percentile(closes, ts).get(ticker, 1.0) >= MIN_FACTOR_PERCENTILE,
        }
        alpha = {w: forward_alpha(closes, ticker, day, w) for w in WINDOWS}
        results.append({"day": day, "ticker": ticker, "rating": rating, **checks,
                        "passed": all(checks.values()), **{f"a{w}": alpha[w] for w in WINDOWS}})
    df = pd.DataFrame(results)

    def line(label: str, part: pd.DataFrame) -> str:
        cells = []
        for w in WINDOWS:
            v = part[f"a{w}"].dropna()
            if not len(v):
                cells.append(f"{'n/a':>8}" + " " * 10)
                continue
            cells.append(f"{v.mean():+6.2f}% {(v > 0).mean():4.0%} n={len(v):<3}")
        return f"{label:<26}" + "  ".join(cells)

    print(f"{len(df)} Buy/Overweight decisions; alpha vs {BENCHMARK} (mean, % beating SPY, n)")
    print(" " * 26 + "  ".join(f"{w:>2}d" + " " * 17 for w in WINDOWS))
    print(line("all (no checks)", df))
    print(line("passed all checks", df[df.passed]))
    print(line("blocked by any check", df[~df.passed]))
    for c in ("regime", "earnings", "factor"):
        print(line(f"  blocked by {c}", df[~df[c]]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
