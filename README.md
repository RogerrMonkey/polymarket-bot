# Polymarket Trading Bot

> Autonomous prediction market trading system using chain-of-thought
> LLM analysis, Kelly criterion position sizing, and multi-provider AI
> fallback chains. Currently in paper-trading validation phase.

[![Tests](https://img.shields.io/badge/tests-207%20passing-brightgreen)]()
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)]()
[![Status](https://img.shields.io/badge/status-paper--trading-yellow)]()
[![Primary LLM](https://img.shields.io/badge/primary-DeepSeek%20R1%20%2F%20NVIDIA%20NIM-76b900)]()

## What It Does

Scans live Polymarket markets, routes survivors through DeepSeek R1's
chain-of-thought reasoning (via NVIDIA NIM) to estimate the true
probability of YES resolution, compares that to the market mid-price,
and — if the edge clears a deterministic risk engine — sizes a position
with fractional Kelly and paper-trades it. Resolved markets feed a
Brier score so the whole pipeline is self-validating; nothing goes live
until 30 paper days and Brier < 0.22 over 20+ resolved markets.

## Architecture

```
                  ┌──────────────────────────────────────────┐
                  │      APScheduler (daily 03:00 UTC)        │
                  └──────────────────┬───────────────────────┘
                                     │
                            ┌────────▼────────┐
                            │  Market Scanner │
                            │  Polymarket CLOB│
                            │  + Gamma API    │
                            └────────┬────────┘
                                     │
                            ┌────────▼────────┐
                            │  Market Filter  │
                            │  volume · time  │
                            │  price · quality│
                            └────────┬────────┘
                                     │
                       ┌─────────────▼─────────────┐
                       │       LLM Analyst          │
                       │ ① DeepSeek R1 / NVIDIA NIM │  ← chain-of-thought
                       │ ② Groq llama-3.3-70b      │  ← fallback
                       │ ③ Anthropic Claude         │  ← fallback
                       │ ④ Ollama (offline)        │  ← local fallback
                       │ ⑤ Deterministic stub      │  ← last resort
                       └─────────────┬─────────────┘
                                     │
                            ┌────────▼────────┐
                            │   Risk Engine   │
                            │  Kelly sizing   │
                            │  volume weight  │
                            │  time decay     │
                            └────────┬────────┘
                                     │
                            ┌────────▼────────┐
                            │    Executor     │
                            │  Paper / Live   │
                            └────────┬────────┘
                                     │
                            ┌────────▼────────┐
                            │ Outcome Resolver│
                            │  Brier scoring  │
                            │  P&L tracking   │
                            └─────────────────┘
```

A single paper-loop cycle runs **scan → filter → analyse → risk → trade
→ resolve** end-to-end. A daily Windows Task Scheduler job fires the
loop at 03:00 UTC, bracketed by `warp-cli connect`/`disconnect` tasks
to handle DNS-blocked regions like India.

## Key Technical Features

- **DeepSeek R1 chain-of-thought:** thinking tokens (`<think>...</think>`)
  expose the model's reasoning process; we strip the block before parsing
  the tool-call JSON and capture a 100-char preview into the analyses log.
- **Multi-provider fallback chain** with automatic fall-through on 429
  rate-limit and 401/403 auth errors — never crashes on API unavailability;
  India-region restrictions on `build.nvidia.com` are detected and logged.
- **Kelly criterion with volume-tier weighting** (0.5× / 1.0× / 1.2× by
  liquidity tier) and time-decay adjustment for near-expiry markets.
- **Consistency validation layer:** catches contradictory `BUY+Low
  confidence`, internally inconsistent probability/decision, extreme
  probability clamping, and `BUY` below market price.
- **WARP auto-connect:** detects Cloudflare WARP status, attempts
  `warp-cli connect` before scheduled runs for geo-restricted regions.
- **Crash recovery:** `data/run.lock` files with stale-PID detection and
  a `status=crashed` scheduler health row when a stale lock is cleared.
- **Production-grade resolver:** 3-retry exponential backoff against
  Gamma API, idempotent `data/resolved_markets.jsonl` skip-list,
  per-market entry price + computed P&L written on resolution.
- **207 tests, 22-point prelive checklist, 6-gate live-readiness CLI**
  with estimated completion date based on paper-day accumulation rate.

## Stack

| Component       | Technology                                       |
|-----------------|--------------------------------------------------|
| Language        | Python 3.11+                                     |
| Primary LLM     | DeepSeek R1 671B via NVIDIA NIM (free tier)      |
| Fallback LLM    | Groq `llama-3.3-70b-versatile` (free tier)       |
| Optional LLM    | Anthropic Claude (paid), Ollama (local)          |
| Scheduler       | APScheduler `AsyncIOScheduler` + `CronTrigger`   |
| Dashboard       | Flask + Jinja2 (dark theme, 6 panels)            |
| Auth            | `py-clob-client` EIP-712 + HMAC-SHA256           |
| News            | GDELT + RSS via `feedparser`                     |
| Persistence     | SQLite (predictions) + JSONL (analyses, trades)  |
| Testing         | pytest (207 tests across 25+ files)              |

## Setup

### Prerequisites
- Python 3.11+
- Cloudflare WARP (for Polymarket API access from India / restricted regions)
- NVIDIA NIM API key — free at [build.nvidia.com](https://build.nvidia.com) (no credit card)
- Groq API key — free fallback at [console.groq.com](https://console.groq.com)

### Installation
```bash
git clone https://github.com/YOUR_USERNAME/polymarket-bot
cd polymarket-bot
pip install -e .
cp .env.example .env
# Edit .env: at minimum set NVIDIA_API_KEY and GROQ_API_KEY
python -m prediction_bot health-check
```

### Configuration

Key `.env` variables, grouped by purpose:

**Analyst chain (priority order):**

| Variable             | Required | Default                          | Notes                                    |
|----------------------|----------|----------------------------------|------------------------------------------|
| `ANALYST_PROVIDER`   | no       | `nvidia`                         | Forces chosen provider to front of chain |
| `NVIDIA_API_KEY`     | primary  | *(empty)*                        | `nvapi-...` from build.nvidia.com        |
| `NVIDIA_MODEL`       | no       | `deepseek-ai/deepseek-r1`        | See `.env.example` for alternatives      |
| `NVIDIA_TEMPERATURE` | no       | `0.6`                            | DeepSeek R1 recommended sampling temp    |
| `NVIDIA_MAX_TOKENS`  | no       | `4096`                           | R1 needs headroom for `<think>` block    |
| `GROQ_API_KEY`       | fallback | *(empty)*                        | `gsk_...` from console.groq.com          |
| `GROQ_MODEL`         | no       | `llama-3.3-70b-versatile`        |                                          |
| `ANTHROPIC_API_KEY`  | optional | *(empty)*                        | Paid; used only if NVIDIA + Groq absent  |
| `OLLAMA_BASE_URL`    | optional | `http://localhost:11434`         | Local LLM fallback                       |

**Bot posture (paper-first):**

| Variable          | Safe default | Live-mode value                  |
|-------------------|--------------|----------------------------------|
| `DRY_RUN`         | `true`       | `false` (after live-readiness)   |
| `BOT_LIVE_MODE`   | `false`      | `true`                           |
| `KILL_SWITCH`     | `false`      | `true` aborts loop before cycle  |

**Risk + sizing:**

| Variable                | Default | Notes                                       |
|-------------------------|---------|---------------------------------------------|
| `BOT_PAPER_BANKROLL`    | `100`   | Starting paper bankroll (USDC)              |
| `BOT_MAX_POSITION_USDC` | `10`    | Hard cap per trade                          |
| `BOT_MIN_KELLY_FRACTION`| `0.05`  | Minimum edge for approval                   |
| `BOT_MIN_VOLUME_24H`    | `5000`  | Minimum 24h volume                          |

**Polygon / Polymarket auth (live mode only):**

| Variable                    | Notes                                  |
|-----------------------------|----------------------------------------|
| `POLYGON_RPC_URL`           | Default: `https://1rpc.io/matic`       |
| `POLYGON_WALLET_ADDRESS`    | EIP-55 checksum, used for balance      |
| `POLYMARKET_PRIVATE_KEY`    | CLOB signer key                        |
| `POLYMARKET_FUNDER_ADDRESS` | EIP-55 funder address                  |
| `SIGNATURE_TYPE`            | `1` for EIP-712 magic-link             |

### Running

```bash
python -m prediction_bot health-check              # one-page status snapshot
python -m prediction_bot paper-loop --cycles 10    # run the pipeline
python -m prediction_bot serve-dashboard           # http://localhost:8787
python -m prediction_bot live-readiness            # 6-gate go/no-go report
python -m prediction_bot prelive-checklist         # all 22+ gates
```

## Current Status

🟡 **Paper Trading — accumulating validation data.**

| Gate                 | Status | Detail                                       |
|----------------------|--------|----------------------------------------------|
| Auth verified        | ✅     | EIP-712 wallet connected                     |
| LLM analyst          | ✅     | DeepSeek R1 via NVIDIA NIM (Groq fallback)   |
| WARP connectivity    | ✅     | Polymarket APIs reachable                    |
| Scheduler registered | ✅     | Daily 03:00 UTC + WARP bracket               |
| Paper days           | 🔄     | 8 / 30                                       |
| Brier validation     | 🔄     | Awaiting market resolutions                  |
| USDC balance         | ⏳     | Pending wallet funding                       |

## Live Mode

Paper → live is a **configuration change, not a code change**. See
[docs/LIVE_MODE_RUNBOOK.md](docs/LIVE_MODE_RUNBOOK.md) for the full
prerequisites checklist, transition steps, position-sizing ramp, and
emergency-stop procedure.

## Project Structure

```
Polymarket_bot/
├── src/prediction_bot/
│   ├── __main__.py             # entry: python -m prediction_bot ...
│   ├── cli.py                  # argparse command router
│   ├── config.py               # .env-backed settings
│   ├── main_loop.py            # paper-loop + run.lock + heartbeats
│   ├── claude_analyst.py       # provider chain (NVIDIA/Groq/Anthropic/Ollama)
│   ├── risk_engine.py          # Kelly sizing + pre-trade checks
│   ├── executor.py             # order placement (paper + live stub)
│   ├── outcome_resolver.py     # Gamma API resolution + retry/backoff
│   ├── paper_pnl.py            # P&L ledger + bankroll trajectory
│   ├── checklist.py            # pre-live PASS/FAIL gates
│   ├── scheduler_health.py     # daily run + heartbeat log
│   ├── health_check.py         # one-page system snapshot CLI
│   ├── live_readiness.py       # live-mode gate evaluation CLI
│   ├── dashboard.py            # Flask dashboard
│   ├── research/               # news_feed, relevance, market_filter
│   ├── clients/                # HTTP + Polymarket clients
│   ├── pipeline/               # scan runner, compliance preflight
│   ├── storage/                # SQLite prediction store
│   └── utils/network.py        # check_warp_active
├── tests/                      # 207 tests
├── scripts/
│   ├── setup_scheduler_windows.ps1  # daily task + WARP bracket
│   ├── benchmark_providers.py       # NVIDIA vs Groq side-by-side
│   ├── audit_signals.py             # confidence/decision distribution
│   └── dev/                         # gitignored dev-only tools
├── docs/
│   ├── LIVE_MODE_RUNBOOK.md
│   └── signal_audit_v0.8.*.md
├── data/                       # (gitignored) JSONL + sqlite state
├── logs/                       # (gitignored) loguru output
├── tasks/{todo.md, lessons.md}
├── .env.example                # template; copy to .env
├── pyproject.toml
└── README.md
```

## Testing

```bash
python -m pytest tests/ -q       # 207 tests, ~3 min
```

Coverage: LLM providers (NVIDIA, Groq, Anthropic, Ollama, stub), risk
engine, market filter, paper P&L, outcome resolver, dashboard, scheduler
health, auth, checklist, WARP detection, run.lock recovery,
live-readiness gates.

## Disclaimer

This bot is for **educational and research purposes**. Prediction
markets involve real financial risk. Past paper-trading performance
does not guarantee live trading profitability. Not financial advice.
Read [docs/LIVE_MODE_RUNBOOK.md](docs/LIVE_MODE_RUNBOOK.md) before
attempting any live-mode transition.
