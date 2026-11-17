"""Extra evidence for buy decisions, beside the TradingAgents rating.

Free (no LLM calls): market regime, earnings guard, factor score, FinBERT news sentiment, scorecard.
Uses the LLM gateway: the investor-persona check (one deep-model call per buy candidate).
"""

from __future__ import annotations

import contextlib
import glob
import json
import os
import re
from datetime import date
from functools import lru_cache
from pathlib import Path

import pandas as pd
import yfinance as yf

REGIME_TICKER = "SPY"
EARNINGS_GUARD_DAYS = 4          # no new buys when earnings fall within this many calendar days
MIN_FACTOR_PERCENTILE = 0.5      # buys only from the top half of the watchlist
MIN_SENTIMENT = -0.3             # FinBERT score in [-1, 1]; below this the news is clearly negative
SCORECARD_MIN_N = 10             # a rating needs this many scored decisions before it changes sizing
LOG_GLOBS = [str(Path.home() / ".tradingagents/logs/backtest/*/trading_memory.md"),
             str(Path.home() / ".tradingagents/memory/trading_memory.md")]


def regime_ok() -> tuple[bool, str]:
    """Bull regime when SPY closes above its 200-day average."""
    s = yf.download(REGIME_TICKER, period="1y", auto_adjust=True, progress=False)["Close"].squeeze().dropna()
    last, sma = float(s.iloc[-1]), float(s.tail(200).mean())
    return last > sma, f"{REGIME_TICKER} {last:.2f} vs 200d avg {sma:.2f}"


def earnings_soon(symbol: str, today: date) -> tuple[bool, str]:
    try:
        dates = (yf.Ticker(symbol).calendar or {}).get("Earnings Date") or []
    except Exception as err:  # missing calendar must not block a decision
        return False, f"calendar unavailable: {err}"
    upcoming = sorted(d for d in dates if d >= today)
    if upcoming and (upcoming[0] - today).days <= EARNINGS_GUARD_DAYS:
        return True, f"earnings {upcoming[0]}"
    return False, f"next earnings {upcoming[0] if upcoming else 'unknown'}"


def factor_scores(tickers: list[str]) -> dict[str, dict]:
    """Percentile (0-1) of an equal mix of 12-1 momentum, low volatility and quality (ROE, margin)."""
    closes = yf.download(tickers, period="2y", auto_adjust=True, progress=False)["Close"]
    rows = {}
    for t in tickers:
        s = closes[t].dropna() if t in closes else pd.Series(dtype=float)
        if len(s) < 260:
            continue
        info = {}
        with contextlib.suppress(Exception):
            info = yf.Ticker(t).info
        rows[t] = {
            "momentum_12_1": float(s.iloc[-21] / s.iloc[-252] - 1),
            "volatility_63d": float(s.pct_change().tail(63).std()),
            "roe": info.get("returnOnEquity"),
            "margin": info.get("profitMargins"),
        }
    df = pd.DataFrame(rows).T
    ranks = pd.concat([
        df["momentum_12_1"].rank(pct=True),
        (-df["volatility_63d"]).rank(pct=True),
        pd.to_numeric(df["roe"], errors="coerce").rank(pct=True),
        pd.to_numeric(df["margin"], errors="coerce").rank(pct=True),
    ], axis=1)
    df["percentile"] = ranks.mean(axis=1, skipna=True).rank(pct=True)
    return {t: {k: (None if pd.isna(v) else round(float(v), 4)) for k, v in r.items()}
            for t, r in df.to_dict("index").items()}


@lru_cache(maxsize=1)
def _finbert():
    from transformers import pipeline

    return pipeline("text-classification", model="ProsusAI/finbert", top_k=None, truncation=True)


def news_sentiment(symbol: str) -> tuple[float | None, int]:
    """Mean P(positive) - P(negative) over the latest Yahoo headlines; None when there is no news."""
    items = yf.Ticker(symbol).news or []
    texts = []
    for it in items[:15]:
        c = it.get("content", it)
        text = " ".join(x for x in (c.get("title"), c.get("summary")) if x)
        if text:
            texts.append(text[:512])
    if not texts:
        return None, 0
    scores = []
    for result in _finbert()(texts):
        p = {r["label"].lower(): r["score"] for r in result}
        scores.append(p.get("positive", 0.0) - p.get("negative", 0.0))
    return round(sum(scores) / len(scores), 3), len(texts)


def persona_verdict(symbol: str, facts: dict) -> dict:
    """Three investor lenses (value, growth at a reasonable price, risk) in one deep-model call."""
    from openai import OpenAI

    info = {}
    with contextlib.suppress(Exception):
        info = yf.Ticker(symbol).info
    keys = ["trailingPE", "forwardPE", "pegRatio", "priceToBook", "returnOnEquity", "profitMargins",
            "debtToEquity", "freeCashflow", "marketCap", "revenueGrowth", "earningsGrowth",
            "recommendationMean", "targetMeanPrice", "currentPrice", "beta"]
    data = {k: info.get(k) for k in keys} | facts
    prompt = (
        f"Judge {symbol} for a 1-3 month long position through three lenses: a value investor "
        "(Buffett: moat, price vs intrinsic value), a growth-at-a-reasonable-price investor (Lynch: "
        "growth vs PEG), and a risk manager (drawdown, leverage, event risk). Use only the data below; "
        "say so when data is missing. Reply with JSON only: "
        '{"value": "buy|hold|avoid", "garp": "buy|hold|avoid", "risk": "buy|hold|avoid", '
        '"verdict": "buy|hold|avoid", "reason": "one sentence"}\n\n' + json.dumps(data, default=str)
    )
    client = OpenAI(base_url=os.environ["TRADINGAGENTS_LLM_BACKEND_URL"],
                    api_key=os.environ["OPENAI_API_KEY"], timeout=600)
    reply = client.chat.completions.create(
        model=os.environ["TRADINGAGENTS_DEEP_THINK_LLM"],
        messages=[{"role": "user", "content": prompt}],
    ).choices[0].message.content or ""
    found = re.search(r"\{.*\}", reply, re.S)
    out = json.loads(found.group()) if found else {}
    out.setdefault("verdict", "hold")
    return out


def scorecard(window: int = 10) -> dict[str, dict]:
    """Mean `window`-day alpha vs SPY per rating, from every backtest and live decision so far."""
    from rescore import decisions, forward_alpha

    paths = [p for g in LOG_GLOBS for p in glob.glob(g)]
    rows = decisions(paths) if paths else []
    if not rows:
        return {}
    tickers = sorted({t for _, t, _ in rows} | {"SPY"})
    closes = yf.download(tickers, start=min(d for d, _, _ in rows), auto_adjust=True, progress=False)["Close"]
    by: dict[str, list[float]] = {}
    for day, ticker, rating in rows:
        a = forward_alpha(closes, ticker, day, window)
        if a is not None:
            by.setdefault(rating, []).append(a)
    return {r: {"n": len(v), "mean_alpha_pct": round(float(sum(v) / len(v)), 2)} for r, v in by.items()}


def size_factor(rating: str, card: dict) -> float:
    """Halve buy sizes for a rating whose measured alpha is negative; full size otherwise."""
    stats = card.get(rating)
    if stats and stats["n"] >= SCORECARD_MIN_N and stats["mean_alpha_pct"] < 0:
        return 0.5
    return 1.0


RATING_ORDER = ["Sell", "Underweight", "Hold", "Overweight", "Buy"]


def majority(votes: list[str]) -> str:
    """Median of the votes on the Sell..Buy scale: 2-of-3 agreement wins, a 3-way split gives the middle."""
    ranks = sorted(RATING_ORDER.index(v) if v in RATING_ORDER else 2 for v in votes)
    return RATING_ORDER[ranks[len(ranks) // 2]]


def buy_gate(checks: dict) -> list[str]:
    """Reasons a buy is blocked; empty means every check passed."""
    blocked = []
    if not checks.get("regime_ok", True):
        blocked.append("market below 200d average")
    if checks.get("earnings_soon"):
        blocked.append("earnings within guard window")
    pct = checks.get("factor_percentile")
    if pct is not None and pct < MIN_FACTOR_PERCENTILE:
        blocked.append(f"factor score {pct:.0%} < {MIN_FACTOR_PERCENTILE:.0%}")
    sent = checks.get("sentiment")
    if sent is not None and sent < MIN_SENTIMENT:
        blocked.append(f"news sentiment {sent:+.2f}")
    if checks.get("persona") == "avoid":
        blocked.append("persona check: avoid")
    return blocked
