#!/usr/bin/env bash
# Daily autotrader run (Alpaca PAPER). Args go to autotrader.py, e.g. --dry-run.
#   TA_PYTHON    Python with TradingAgents installed (default: python3)
#   TA_ENV_FILE  shell file exporting the LLM gateway settings (default: $TA_HOME/ta-env.sh)
#   TA_HOME      where runs/, state/, logs/ and the KILL file live (default: the repo root)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
export TA_HOME="${TA_HOME:-$REPO}"
TA_PYTHON="${TA_PYTHON:-python3}"
TA_ENV_FILE="${TA_ENV_FILE:-$TA_HOME/ta-env.sh}"
LOG="$TA_HOME/logs/$(date +%F).log"
mkdir -p "$TA_HOME/logs"
cd "$REPO"

alert() {
  if command -v terminal-notifier >/dev/null; then
    terminal-notifier -title "Autotrader FAILED" -message "$1" -open "file://$LOG" >/dev/null 2>&1 || true
  else
    osascript -e "display notification \"$1\" with title \"Autotrader FAILED\"" >/dev/null 2>&1 || true
  fi
  echo "$1" >&2
}
keychain() { security find-generic-password -s "$1" -a "$USER" -w 2>/dev/null || true; }

export TRADINGAGENTS_LLM_MAX_RETRIES="${TRADINGAGENTS_LLM_MAX_RETRIES:-6}"
# shellcheck source=/dev/null
source "$TA_ENV_FILE" || { alert "LLM settings missing: could not source $TA_ENV_FILE"; exit 1; }
gateway_up() { [[ "$(curl -s -o /dev/null -w '%{http_code}' -m 10 -H "Authorization: Bearer $OPENAI_API_KEY" "$TRADINGAGENTS_LLM_BACKEND_URL/models" || true)" == "200" ]]; }
if ! gateway_up; then
  # Optionally start the app that hosts the gateway (e.g. TA_GATEWAY_APP="Visual Studio Code"),
  # then give it up to 3 minutes.
  if [[ -n "${TA_GATEWAY_APP:-}" ]]; then open -ga "$TA_GATEWAY_APP" || true; fi
  for _ in $(seq 18); do sleep 10; gateway_up && break; done
fi
gateway_up || { alert "LLM gateway not reachable at $TRADINGAGENTS_LLM_BACKEND_URL"; exit 1; }

# Alpaca PAPER keys: from the environment, else from the macOS Keychain.
ALPACA_KEY_ID="${ALPACA_KEY_ID:-$(keychain alpaca-paper-key-id)}"
ALPACA_SECRET_KEY="${ALPACA_SECRET_KEY:-$(keychain alpaca-paper-secret)}"
export ALPACA_KEY_ID ALPACA_SECRET_KEY
# caffeinate (macOS) stops idle sleep for the length of the run.
cmd=("$TA_PYTHON" autotrader.py "$@")
if command -v caffeinate >/dev/null; then cmd=(caffeinate -i "${cmd[@]}"); fi
"${cmd[@]}" 2>&1 | tee -a "$LOG"
