# Project Audit & State Assessment Prompt
# Paste this verbatim when you first open Claude Code in this project.

---

You are being onboarded to an autonomous prediction market trading bot project.
Before you do anything else, perform a full audit so you have an exact picture
of where the project stands and what must happen next.

## STEP 1 — Load your operating context

Read these files in order. Do not skip any:

1. `.claude/CLAUDE.md`                   — project constitution, absolute rules, code style
2. `tasks/lessons.md`                    — hard-learned mistakes, never repeat these
3. `tasks/todo.md`                       — full implementation history and pending items
4. `polymarket_bot_plan.md`              — canonical roadmap (master reference)
5. `risk_config.json`                    — live risk parameters

## STEP 2 — Audit the source tree

Run these commands and read the output:

```bash
# Package structure
find src/prediction_bot -name "*.py" | sort

# Test coverage mapping (which modules have tests)
ls tests/

# Installed dependencies
pip show anthropic py-clob-client flask aiohttp | grep -E "^(Name|Version):"

# Verify editable install is working
python -m prediction_bot --help 2>&1 | head -20
```

## STEP 3 — Assess runtime health

Run these and read every line of output:

```bash
# Pre-live gate status (the single most important health check)
python -m prediction_bot prelive-checklist

# Paper scorecard (how many real analysis days have been accumulated)
python -m prediction_bot scorecard

# Last loop log entries (are real API calls succeeding or failing?)
tail -30 data/loop_log.jsonl

# Risk rejection log (what is the risk engine blocking and why?)
tail -20 data/risk_log.jsonl

# Actual trade history (synthetic vs real)
tail -20 data/trades.jsonl

# USDC / wallet readiness
python -m prediction_bot usdc-check
```

## STEP 4 — Test suite health

```bash
python -m pytest tests/ -q --tb=short 2>&1 | tail -40
```

Note any failures — these represent regressions that must be fixed before
any new feature work.

## STEP 5 — Environment variable audit

Check which required env vars are present (without printing values):

```bash
python - <<'EOF'
import os
required = [
    "ANTHROPIC_API_KEY",
    "POLYMARKET_PRIVATE_KEY",
    "POLYMARKET_FUNDER_ADDRESS",
    "SIGNATURE_TYPE",
    "POLYGON_RPC_URL",
    "POLYGON_WALLET_ADDRESS",
    "DRY_RUN",
    "BOT_LIVE_MODE",
    "USDC_DAILY_TRANSFER_LIMIT",
    "USDC_MAX_SINGLE_TRANSFER",
    "USDC_MIN_BUFFER",
    "BOT_CRYPTOPANIC_API_TOKEN",
]
optional = [
    "BOT_MIN_VOLUME",
    "BOT_EDGE_THRESHOLD",
    "BOT_KELLY_FRACTION",
    "BOT_RESEARCH_ENABLED",
    "BOT_CALIBRATION_ENABLED",
    "LIVE_MODE",
]
print("=== REQUIRED ===")
for k in required:
    status = "SET" if os.getenv(k) else "MISSING"
    print(f"  {status:8s} {k}")
print("\n=== OPTIONAL ===")
for k in optional:
    status = "SET" if os.getenv(k) else "default"
    print(f"  {status:8s} {k}")
EOF
```

## STEP 6 — Synthesize your findings

After running all of the above, produce a structured report with these exact
sections. Be specific — reference actual file names, line counts, error
messages, and metric values you observed:

### 6A — What is fully working
List each component that is operational. For each, state the evidence
(e.g. "risk_engine.py — 5 pre-trade checks passing per risk_log.jsonl").

### 6B — What is broken or blocked
For each blocker, state:
- Component name and file
- Exact failure mode (copy the error if there is one)
- Root cause (missing env var / geo-restriction / code bug / missing data)
- Whether it blocks paper trading, live readiness, or both

### 6C — Current metrics snapshot
Pull exact numbers:
- Paper analysis days accumulated (need ≥ 14 to clear Phase 9 gate)
- Pre-live checklist: X passed / Y total checks
- Test suite: X passed / Y total / Z failed
- Real trades logged vs synthetic trades logged
- Risk rejections: top 2 rejection reasons from risk_log.jsonl

### 6D — The critical path to Phase 9 gate clearance
Phase 9 requires:
1. ≥ 14 distinct paper analysis days with stable daily signal flow
2. All 17 pre-live checklist checks passing (currently 4/17)
3. Zero test failures
4. ANTHROPIC_API_KEY set (enables real Claude analyst calls instead of synthetic)

State what must be done in what order. Do not suggest live trading — paper
validation must complete first per CLAUDE.md absolute rules.

### 6E — Recommended immediate next actions (top 3 only)
Rank by impact. For each:
- Action title
- Which file(s) to change or which command to run
- Expected outcome
- Estimated effort (minutes / hours)

Do not suggest more than 3 actions. Focus on unblocking the paper loop so
real analysis days start accumulating.

## CONSTRAINTS (re-read CLAUDE.md before acting on anything)

- `LIVE_MODE=true` must NEVER be set until Phase 9 gates are cleared
- `DRY_RUN` must remain true during all paper work
- Never hardcode credentials — all secrets via `.env` only
- Never delete `tasks/lessons.md`
- Every new module needs a paired `tests/test_<module>.py`
- Use `loguru` for all logging — no bare `print()` calls
- Run `paper_metrics` validation before changing any strategy parameter

---
# Known context for your audit (do not assume — verify these with the commands above)

Last known state (as of 2026-04-06, may be stale):
- Pre-live checklist: 4/17 checks passing
  - PASSING: kill_switch=False, daily_loss_cap=5%, max_position=10%, kelly=25%
  - FAILING: all API credentials, Polymarket endpoint connectivity, paper gate
- Loop log: all real API calls failing with DNS resolution errors
  (gamma-api.polymarket.com, clob.polymarket.com — likely India geo-block)
- Trades log: all entries marked synthetic=true, 0 real paper trades
- Paper days: 0 real analysis days accumulated (14 required for Phase 9)
- Risk config (risk_config.json): conservative settings in place, kill_switch off
- Test suite: comprehensive (25 test files), last run status unknown
- Synthetic replay: functional, used for pipeline smoke tests only
- Claude analyst: ANTHROPIC_API_KEY missing, falling back to deterministic stub
- USDC/wallet: all wallet env vars missing, Polygon RPC returning 401

Your job is to verify each of these and correct anything that has changed
since that snapshot.
