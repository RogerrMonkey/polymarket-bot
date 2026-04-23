# Polymarket Trading Bot

> Autonomous prediction market trading bot for Polymarket.
> Paper-trading validated · Groq LLM analyst · Kelly position sizing

## What It Does

Scans live Polymarket markets, routes survivors through an LLM analyst
(Groq `llama-3.3-70b-versatile`) that estimates a calibrated probability,
compares that to the market's mid-price, and — if the edge clears a
deterministic risk engine — sizes a position with fractional Kelly and
paper-trades it. Resolved markets feed a Brier score so the whole loop
is self-validating; nothing goes live until 30 paper days and
Brier < 0.22 over 20+ resolved markets.

## Architecture

```
                         ┌──────────────┐
                  ┌─────▶│  News Feed   │───┐
                  │      │ (GDELT+RSS)  │   │
                  │      └──────────────┘   ▼
  ┌─────────┐   ┌─────────────┐   ┌─────────────┐   ┌────────────┐   ┌──────────┐
  │ Scanner │──▶│  Market     │──▶│    Groq     │──▶│   Risk     │──▶│ Executor │
  │ (Gamma) │   │  Filter     │   │   Analyst   │   │   Engine   │   │ (paper)  │
  └─────────┘   └─────────────┘   └─────────────┘   └────────────┘   └─────┬────┘
                                                                          │
                                                                          ▼
                                             ┌────────────────┐   ┌──────────────┐
                                             │  Outcome       │◀──│ Paper P&L    │
                                             │  Resolver      │   │ Tracker      │
                                             └────────────────┘   └──────────────┘
```

A single paper-loop cycle is: **scan → filter → analyse → risk → trade
→ resolve**. A daily Windows Task Scheduler job fires the loop at
03:00 UTC, bracketed by `warp-cli connect`/`disconnect` tasks.

## Stack

- Python 3.11+
- **Groq API** (`llama-3.3-70b-versatile`) as analyst brain
- Polymarket **CLOB API** + **Gamma API**
- Flask dashboard (dark theme, 6 panels)
- **APScheduler** for daily runs
- `py-clob-client` for Polymarket authentication

## Current Status

🟡 **Paper trading — accumulating validation data.**
Pre-live checklist: **17+/22 gates** · 196 tests passing · paper day 6/14.

## Setup

```bash
git clone <repo> polymarket-bot && cd polymarket-bot
python -m pip install -e .
cp .env.example .env   # then fill in your keys
python -m prediction_bot health-check
```

## Configuration

Key `.env` variables (all loaded via `python-dotenv`):

| Variable                   | What it does                                              | Safe default          |
|----------------------------|-----------------------------------------------------------|-----------------------|
| `GROQ_API_KEY`             | Analyst LLM credential                                    | *(required)*          |
| `POLYMARKET_PRIVATE_KEY`   | CLOB signer key (funding wallet)                          | *(required for auth)* |
| `POLYMARKET_FUNDER_ADDRESS`| Polygon funder address (EIP-55 checksum)                  | *(required for auth)* |
| `POLYGON_RPC_URL`          | Polygon JSON-RPC endpoint                                 | `https://1rpc.io/matic` |
| `DRY_RUN`                  | If `true`, skip all order placement                       | `true`                |
| `BOT_LIVE_MODE`            | Must be `true` to place real orders                       | `false`               |
| `KILL_SWITCH`              | Abort the loop before the first cycle                     | `false`               |
| `BOT_PAPER_BANKROLL`       | Starting paper bankroll (USDC)                            | `100`                 |
| `BOT_MAX_POSITION_USDC`    | Hard cap per trade (USDC)                                 | `10`                  |
| `BOT_MIN_KELLY_FRACTION`   | Minimum edge for approval                                 | `0.05`                |
| `BOT_MIN_VOLUME_24H`       | Minimum 24h market volume                                 | `5000`                |

## Running

The four commands you'll use day-to-day:

```bash
python -m prediction_bot health-check                  # one-page status
python -m prediction_bot paper-loop --cycles 10        # run the pipeline
python -m prediction_bot serve-dashboard               # local ops dashboard
python -m prediction_bot live-readiness                # are we ready for live?
```

## Paper Trading Gates (before live mode)

All five must be green before flipping `BOT_LIVE_MODE=true`:

1. **Paper days ≥ 30** (distinct UTC dates with analyses) — currently **6/30**
2. **Brier score < 0.22** over **n ≥ 20** resolved markets
3. **Win rate > 52%** over **n ≥ 10** resolved paper trades
4. **Scheduler success rate ≥ 80%** over the trailing 14 days
5. **Prelive checklist ≥ 20/22 PASS**

Follow `docs/LIVE_MODE_RUNBOOK.md` the first time you flip to live.

## Project Structure

```
Polymarket_bot/
├── src/prediction_bot/
│   ├── __main__.py            # entry: python -m prediction_bot …
│   ├── cli.py                 # argparse command router
│   ├── config.py              # .env-backed settings
│   ├── main_loop.py           # paper-loop + run.lock + heartbeats
│   ├── claude_analyst.py      # Groq/Anthropic LLM analyst
│   ├── risk_engine.py         # Kelly sizing + pre-trade checks
│   ├── executor.py            # order placement (paper + live stub)
│   ├── outcome_resolver.py    # settle resolved markets
│   ├── paper_pnl.py           # P&L ledger + bankroll trajectory
│   ├── checklist.py           # pre-live PASS/FAIL gates
│   ├── scheduler_health.py    # daily run + heartbeat log
│   ├── health_check.py        # one-page system snapshot
│   ├── live_readiness.py      # live-mode gate evaluation
│   ├── dashboard.py           # Flask dashboard
│   ├── research/              # news_feed, relevance, market_filter
│   ├── clients/               # HTTP + Polymarket clients
│   ├── pipeline/              # scan runner, compliance preflight
│   ├── storage/               # sqlite prediction store
│   └── utils/network.py       # check_warp_active
├── tests/                     # 196 tests
├── scripts/
│   ├── setup_scheduler_windows.ps1     # daily task + WARP bracket
│   ├── audit_signals.py
│   └── dev/force_paper_trade.py        # dev-only smoke test
├── docs/
│   ├── LIVE_MODE_RUNBOOK.md
│   └── signal_audit_v0.8.*.md
├── data/                      # (gitignored) JSONL + sqlite state
├── logs/                      # (gitignored) loguru output
├── tasks/
│   ├── todo.md
│   └── lessons.md
├── .env.example               # (gitignored – contains real key)
├── CLAUDE.md
├── pyproject.toml
└── README.md
```

## Development

```bash
python -m pytest tests/ -q      # 196 tests, ~3 min
```

The codebase defaults to paper mode: `DRY_RUN=true` and
`BOT_LIVE_MODE=false`. No real orders can be placed without both
flags being explicitly flipped **and** the pre-live checklist
passing.

## Disclaimer

This is experimental software. Prediction markets involve real
financial risk. Nothing here is financial advice. Use at your own
risk, and read `docs/LIVE_MODE_RUNBOOK.md` before attempting any
live-mode transition.
