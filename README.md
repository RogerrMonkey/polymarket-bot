# Polymarket Trading Bot

> Autonomous prediction market trading system using LLM analysis, Kelly criterion position sizing, and proper risk management. Currently in paper-trading validation phase.

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)]()
[![Status](https://img.shields.io/badge/status-paper--trading-yellow)]()
[![Primary LLM](https://img.shields.io/badge/primary-NVIDIA%20NIM-76b900)]()

## What It Does

Scans live Polymarket markets, routes survivors through an LLM reasoning engine (via NVIDIA NIM) to estimate the true probability of YES resolution, compares that to the market mid-price, and — if the edge clears a deterministic risk engine — sizes a position with fractional Kelly and paper-trades it. Resolved markets feed a Brier score so the whole pipeline is self-validating; nothing goes live until 30 paper days and sufficient Brier scoring validation is achieved.

## Architecture

```text
                  ┌──────────────────────────────────────────┐
                  │      APScheduler (daily 03:00 UTC)       │
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
                       │       LLM Analyst         │
                       │ ① NVIDIA NIM (minimax)    │  ← probability inference
                       │ ② Deterministic stub      │  ← fallback logic
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

A single paper-loop cycle runs **scan → filter → analyse → risk → trade → resolve** end-to-end. A daily task scheduler fires the loop alongside regional WARP DNS workarounds (for restricted geographic regions).

## Key Technical Features

- **LLM Contextual Analysis:** Connects automatically to NVIDIA NIM endpoints (specifically default models like `minimaxai/minimax-m2.7`) to derive probabilistic evaluations from market data.
- **Kelly criterion with volume-tier weighting** (0.5× / 1.0× / 1.2× by liquidity tier) and time-decay adjustment for near-expiry markets.
- **Consistency validation layer:** catches contradictory `BUY+Low confidence`, internally inconsistent probability/decision, extreme probability clamping, and `BUY` below market price.
- **WARP auto-connect:** detects Cloudflare WARP status, attempts `warp-cli connect` before scheduled runs for geo-restricted regions.
- **Crash recovery:** `data/run.lock` files with stale-PID detection and a `status=crashed` scheduler health row when a stale lock is cleared.
- **Production-grade resolver:** 3-retry exponential backoff against Gamma API, idempotent skip-lists, and per-market entry price tracking.

## Stack

| Component       | Technology                                       |
|-----------------|--------------------------------------------------|
| Language        | Python 3.11+                                     |
| AI Provider     | NVIDIA NIM (LLM)                                 |
| Scheduler       | APScheduler `AsyncIOScheduler` + `CronTrigger`   |
| Dashboard       | Flask + Jinja2 (dark theme, 6 panels)            |
| Auth            | `py-clob-client` EIP-712 + HMAC-SHA256           |
| Persistence     | SQLite (predictions) + JSONL (analyses, trades)  |

## Setup

### Prerequisites
- Python 3.11+
- Cloudflare WARP (for Polymarket API access from restricted regions)
- NVIDIA NIM API key — free at [build.nvidia.com](https://build.nvidia.com) (no credit card required)

### Installation
```bash
git clone https://github.com/YOUR_USERNAME/polymarket-bot
cd polymarket-bot
pip install -e .
cp .env.example .env
# Edit .env: at minimum set NVIDIA_API_KEY
python -m prediction_bot health-check
```

### Configuration

Key `.env` variables, grouped by purpose:

**Analyst config:**

| Variable             | Required | Default                          | Notes                                    |
|----------------------|----------|----------------------------------|------------------------------------------|
| `ANALYST_PROVIDER`   | no       | `nvidia`                         | Forces chosen provider to front of chain |
| `NVIDIA_API_KEY`     | primary  | *(empty)*                        | `nvapi-...` from build.nvidia.com        |
| `NVIDIA_MODEL`       | no       | `minimaxai/minimax-m2.7`         | Use an available completion/chat model   |
| `NVIDIA_TEMPERATURE` | no       | `0.6`                            | Default reasoning temperature            |
| `NVIDIA_MAX_TOKENS`  | no       | `4096`                           | Headroom for reasoning buffers           |

**Bot posture (paper-first):**

| Variable          | Safe default | Live-mode value                  |
|-------------------|--------------|----------------------------------|
| `LIVE_MODE`       | `false`      | `true` (requires API keys & USDC)|
| `BOT_LLM_ENABLED` | `true`       | `true`                           |

## License & Security

This project strictly disables mainnet trading unless `LIVE_MODE=true` is explicitly provided, verified, and proper exchange secrets/keys are injected. Use paper mode to validate Brier Score efficiency first.
