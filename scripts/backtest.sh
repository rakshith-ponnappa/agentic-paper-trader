#!/usr/bin/env bash
# Point-in-time backtest with the TradingAgents CLI, one process per ticker in parallel.
# Usage: scripts/backtest.sh NVDA,AAPL 2026-09-14 2026-11-02 [every-N-days, default 7]
#   TA_PYTHON    Python with TradingAgents installed; its `tradingagents` CLI is used (default: python3)
#   TA_ENV_FILE  shell file exporting the LLM gateway settings (default: $TA_HOME/ta-env.sh)
#   TA_HOME      logs go to $TA_HOME/backtests/<timestamp>/ (default: the repo root)
set -euo pipefail
[[ $# -ge 3 ]] || { echo "usage: $0 TICKER[,TICKER...] START END [EVERY]" >&2; exit 2; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
export TA_HOME="${TA_HOME:-$REPO}"
TA_PYTHON="${TA_PYTHON:-python3}"
TA_ENV_FILE="${TA_ENV_FILE:-$TA_HOME/ta-env.sh}"
ta_cli="$(dirname "$(command -v "$TA_PYTHON")")/tradingagents"
# shellcheck source=/dev/null
source "$TA_ENV_FILE"
out="$TA_HOME/backtests/$(date +%Y%m%d-%H%M)"; mkdir -p "$out"
keep_awake=""; if command -v caffeinate >/dev/null; then keep_awake="caffeinate -i"; fi
IFS=, read -ra tickers <<< "$1"
for t in "${tickers[@]}"; do
  $keep_awake "$ta_cli" backtest "$t" --start "$2" --end "$3" --every "${4:-7}" > "$out/$t.log" 2>&1 &
done
wait
echo "Backtest logs: $out"
