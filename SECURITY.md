# Security Policy

## Supported versions

Only the latest commit on `main` is supported.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's
[private vulnerability reporting](../../security/advisories/new) for this
repository rather than opening a public issue. Include steps to reproduce and
the affected script or module. You can expect an initial response within a
few days.

## Deployment notes

- **Paper trading only.** The code talks to `https://paper-api.alpaca.markets`
  and contains no live-trading endpoint. Do not change that to trade real money.
- Never commit API keys. Alpaca keys come from `ALPACA_KEY_ID` /
  `ALPACA_SECRET_KEY` or the macOS Keychain; the LLM gateway token comes from
  the file named by `TA_ENV_FILE`, which should live outside the repository.
- Runtime data (`runs/`, `state/`, `logs/`, `advice/`, `backtests/`) can contain
  positions and holdings. Keep `TA_HOME` outside the repository, or rely on
  `.gitignore`, and never publish it.
- Point `TRADINGAGENTS_LLM_BACKEND_URL` only at an LLM endpoint you trust:
  prompts include tickers, positions and portfolio context.
