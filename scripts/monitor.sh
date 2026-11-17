#!/usr/bin/env bash
# 15-minute stop-loss / take-profit guard (Alpaca PAPER). Makes no LLM calls, so no gateway is needed.
#   TA_PYTHON  Python with this repo's requirements installed (default: python3)
#   TA_HOME    where state/, logs/ and the KILL file live (default: the repo root)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
export TA_HOME="${TA_HOME:-$REPO}"
TA_PYTHON="${TA_PYTHON:-python3}"
mkdir -p "$TA_HOME/logs"
cd "$REPO"

keychain() { security find-generic-password -s "$1" -a "$USER" -w 2>/dev/null || true; }
# Alpaca PAPER keys: from the environment, else from the macOS Keychain.
ALPACA_KEY_ID="${ALPACA_KEY_ID:-$(keychain alpaca-paper-key-id)}"
ALPACA_SECRET_KEY="${ALPACA_SECRET_KEY:-$(keychain alpaca-paper-secret)}"
[[ -n "$ALPACA_KEY_ID" && -n "$ALPACA_SECRET_KEY" ]] || { echo "Alpaca paper keys not found" >&2; exit 1; }
export ALPACA_KEY_ID ALPACA_SECRET_KEY
"$TA_PYTHON" monitor.py 2>&1 | tee -a "$TA_HOME/logs/monitor-$(date +%F).log"
