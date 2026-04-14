# Polymarket Bot — Project Constitution

## Overview
Autonomous prediction market trading bot targeting Polymarket (CLOB API).
Late Phase 8 — core pipeline complete, actively refining strategies.

## Project Root
`C:\Users\ADMIN\Saved Games\Polymarket_bot`

## Stack
- **Language:** Python 3.11+
- **Modules:** auth, data_feed, scanning, risk, claude_analyst, news_feed,
  execution, paper_metrics, telemetry, alerting, flask_dashboard, watchlist,
  synthetic_replay, outcome_resolution
- **AI:** Claude API (claude-sonnet-4-20250514) as analyst brain
- **Infra:** Flask dashboard, SQLite/local storage, Redis/BullMQ optional
- **APIs:** Polymarket CLOB API, Gamma API for market data, news feeds

## Active Strategies
1. **Maker liquidity provisioning** — post limit orders, capture spread
2. **News-to-price latency arb** — detect news before price moves
3. **CEX/oracle price lag** — exploit delay between CEX price and Polymarket

## India-Specific Constraints
- Platform access restrictions — paper trading mode FIRST always
- No real funds until paper_metrics validate strategy over 30+ days
- API funding workarounds in progress — do not suggest Polymarket direct deposit

## Absolute Rules (NEVER BREAK)
- NEVER execute real trades without explicit `LIVE_MODE=true` env flag
- NEVER hardcode API keys — always load from `.env` via `python-dotenv`
- ALWAYS run paper_metrics validation before any strategy parameter change
- ALWAYS log every decision through the telemetry module
- ALWAYS check risk module limits before execution module calls
- NEVER delete `tasks/lessons.md` — it contains hard-learned mistakes

## Code Style
- Python: snake_case everywhere, type hints on ALL functions
- Every new module needs a corresponding `tests/test_<module>.py`
- Use `loguru` for logging — no bare `print()` statements
- Prefer `aiohttp` over `requests` for async-compatible code
- Config via `config.py` reading `.env` — never inline constants

## Current Focus
- Perfecting maker liquidity provisioning (spread capture, order book depth)
- Improving `claude_analyst.py` prompt quality and response parsing
- Reducing scanner false positives
- Flask dashboard polish

## Key Files
- `main.py` — entry point
- `config.py` — all settings, reads from .env
- `modules/claude_analyst.py` — core AI analyst brain
- `modules/risk.py` — position sizing, exposure limits
- `modules/execution.py` — order placement (paper + live)
- `modules/scanning.py` — market opportunity scanner
- `modules/news_feed.py` — news ingestion & signal extraction
- `dashboard/app.py` — Flask dashboard
- `tasks/todo.md` — current task list
- `tasks/lessons.md` — hard-learned lessons, NEVER repeat these

## When Asked to Add a Feature
1. Read `tasks/todo.md` and `tasks/lessons.md` first
2. Check `config.py` for existing settings before adding new ones
3. Verify the risk module won't block the new flow
4. Write test file alongside implementation
5. Update `tasks/todo.md` on completion
