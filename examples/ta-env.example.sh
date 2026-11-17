#!/usr/bin/env bash
# Example TA_ENV_FILE: copy it outside the repo (e.g. to $TA_HOME/ta-env.sh), fill it in, and
# point TA_ENV_FILE at it. scripts/run.sh and scripts/backtest.sh `source` it before every run.
# Any OpenAI-compatible endpoint works (a local gateway, LiteLLM, vLLM, Ollama, ...).

# "openai" with a custom base URL keeps TradingAgents on Chat Completions with tool-forced structured output.
export TRADINGAGENTS_LLM_PROVIDER="openai"
export TRADINGAGENTS_LLM_BACKEND_URL="http://127.0.0.1:8000/v1"
export OPENAI_API_KEY="replace-with-your-gateway-token"
export TRADINGAGENTS_QUICK_THINK_LLM="your-quick-model"
export TRADINGAGENTS_DEEP_THINK_LLM="your-deep-model"
export TRADINGAGENTS_MAX_DEBATE_ROUNDS=1
export TRADINGAGENTS_MAX_RISK_ROUNDS=1

# Alpaca PAPER keys. Leave these unset to read them from the macOS Keychain instead
# (generic passwords named alpaca-paper-key-id and alpaca-paper-secret).
# export ALPACA_KEY_ID="..."
# export ALPACA_SECRET_KEY="..."

# Optional: macOS app that hosts the gateway; run.sh starts it when the gateway is down.
# export TA_GATEWAY_APP="Visual Studio Code"
return 0 2>/dev/null || true
