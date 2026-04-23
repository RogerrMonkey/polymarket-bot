# Live Mode Transition Runbook

This is the one document to open the first time you flip
`BOT_LIVE_MODE=true`. It bakes in the hard-earned rule from paper
validation: **no real money moves until every gate is green and you
have verified it end-to-end in dry-run first.**

---

## Prerequisites (verify ALL before proceeding)

- [ ] `python -m prediction_bot prelive-checklist` shows **20+/22 PASS**
- [ ] Brier score **< 0.22** on **20+ resolved markets**
- [ ] Win rate **> 52%** on approved paper trades (**n ≥ 10**)
- [ ] USDC balance **≥ 20** in wallet (run `balance-usdc`)
- [ ] `python -m prediction_bot verify-auth` PASS
- [ ] `scheduler_success_rate ≥ 80%` over **14+ days**
- [ ] No unhandled exceptions in the last **7 scheduler runs** (`logs/*.log`)

`python -m prediction_bot live-readiness` summarises most of the above
in one command — use it as the final sanity check.

---

## Transition Steps

1. `python -m pytest tests/ -q` — must be **185+/185**.
2. `python -m prediction_bot prelive-checklist` — screenshot the output
   for the record.
3. Edit `.env`:
   ```
   DRY_RUN=false
   BOT_LIVE_MODE=true
   BOT_MAX_POSITION_USDC=2        # start small, not 10
   ```
4. `python -m prediction_bot verify-auth` — must PASS.
5. `python -m prediction_bot balance-usdc` — confirm balance.
6. `python -m prediction_bot paper-loop --cycles 1`
   - Verify the logs show `execution_mode=live` and no exceptions.
7. Check `data/trades.jsonl` — the most recent row should have
   `synthetic=false` and a real `order_id`.
8. Open the dashboard (`python -m prediction_bot serve-dashboard`) and
   monitor for **30 minutes**.
9. On any error, immediately set `BOT_LIVE_MODE=false` in `.env` and
   interrupt the process.

---

## Position Sizing Ramp

Small first, bigger only after proof:

- **Week 1–2:** `BOT_MAX_POSITION_USDC=2`
- **Week 3+:** increase to `5` only after **5 profitable live trades**
- **Month 2+:** increase to `10` only after **consistent profitability**
  (two consecutive weeks net positive, no single-day drawdown > 10%)
- **Never exceed 10 per position in the first month.**

---

## Emergency Stop

- **Fast (next cycle):** set `KILL_SWITCH=true` in `.env`. The next
  paper-loop cycle aborts before placing any orders.
- **Nuclear (immediate):** kill the Python process, then set
  `BOT_LIVE_MODE=false` in `.env` before the next 03:00 UTC scheduled
  run. Confirm on the dashboard status bar.

---

## When to Roll Back

Flip back to paper immediately on any of:
- Two consecutive losing days.
- Any unexpected exception in `logs/*.log` (silent failures are worse
  than loud ones).
- Scheduler success rate falls below 80% in the trailing 14 days.
- `prelive-checklist` drops below 20/22 PASS.

There is no shame in a rollback. There is shame in a rollback you
should have done three days ago.
