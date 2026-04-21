# Implementation Todo

## Plan
- [x] Scaffold core package and configuration.
- [x] Implement deterministic scanner and risk engine.
- [x] Implement Polymarket public ingestion client (Kalshi deferred in v1).
- [x] Build single-run pipeline and CLI entrypoint.
- [x] Add Gate Zero geoblock/compliance preflight in CLI and runner.
- [x] Add Phase 0 verify_access.py deployment access checks (CLOB/Gamma/WS with timings).
- [x] Add Phase 1 auth module with py-clob-client credential derivation and verify-auth CLI command.
- [x] Add Phase 2 data_feed.py with websocket market subscription, reconnect loop, REST reconciliation, and Chainlink BTC price reader cache.
- [x] Add Phase 3 deterministic news_feed.py (GDELT + RSS via feedparser) with prompt-injection-safe sanitization and pipeline integration.
- [x] Add Phase 6 deterministic risk_engine.py with JSON config, portfolio_state persistence, ordered pre-trade checks, and rejection logging.
- [x] Add Phase 4 watchlist manager with top-volume refresh and BTC 5-minute priority; apply watchlist filtering in scan runner.
- [x] Add Phase 5 Claude analyst module with tool-schema parsing, prompt caching, cost tracking, and optional runner integration.
- [x] Add Phase 8 paper scorecard module and CLI gates (`scorecard --check-gates`) including minimum 14-day paper-history check.
- [x] Add initial unit tests for scanner and risk logic.
- [x] Add research ingestion adapters and sentiment module.
- [x] Add calibrated probability model stack and Brier tracking.
- [x] Add USDC on-ramp/off-ramp operational runbook and checks.
- [x] Add persistent trade logging, metrics, and dashboard endpoints.

## Review
- Completed: Phase 1-2 foundation with runnable scan and deterministic risk checks (Polymarket-first).
- Completed: Phase 3 baseline research ingestion with relevance and sentiment scoring integrated into runtime scan.
- Completed: Phase 5 baseline probability calibration with persistence and Brier metrics commands.
- Completed: Runner is now wired to deterministic `risk_engine.py` checks using `risk_config.json` and `data/risk_log.jsonl` rejection logging.
- Completed: Canonical roadmap switched to polymarket_bot_plan.md and plan.md set as alias.
- Completed: Optional Claude analysis path integrated (disabled by default), with deterministic fallback when missing API key/budget.
- Completed: Paper scorecard and gate checks now enforce minimum history duration before live readiness.
- Completed: Paper execution module (`executor.py`), loop runner (`paper-loop`), and operations dashboard (`serve-dashboard`) are implemented.
- Completed: USDC operational runbook (`docs/usdc_onramp_runbook.md`) and automated readiness checks (`usdc-check`) integrated into dashboard.
- Completed: Added telemetry snapshot and alerting hooks in CLI (`telemetry`) and dashboard panels.
- Completed: Trade lifecycle drilldown page/API added in dashboard (`/trades`, `/api/trades-lifecycle`).
- Completed: External alert delivery hook added via webhook dispatch (`notify-alerts`, dashboard send-alerts action).
- Completed: Compact pre-live checklist report pipeline (`prelive-checklist`) now persists `data/prelive_checklist.json` for operator review.
- Completed: Dashboard now supports pre-live checklist run action and pre-live report API (`/api/prelive-report`).
- Completed: Non-dry-run loop safety gate added; `paper-loop --no-dry-run` blocks unless checklist passes (override flag available).
- Completed: Synthetic data replay tooling added (`replay-synthetic`) with multi-day backfill and resolver stub-map generation.
- Completed: Outcome resolver job scaffolding added (`resolve-outcomes`) with dry-run/stub-first defaults and scheduler scripts.
- Completed: Executor live order-placement scaffold added with poll/cancel lifecycle and safe stub mode toggles.
- Completed: Data feed now emits Chainlink-enriched market state when oracle data is available.
- Completed: Replaced CryptoPanic (free tier discontinued 2026-04-01) with GDELT + RSS (feedparser); no API key required.
- Completed: First real paper-loop run against live Polymarket markets via Cloudflare WARP — analyses.jsonl now has entries with `provider=groq` and real market IDs.
- Completed: Checklist hardened with `paper_loop_has_run_today`, `news_feed_has_sources`, and WARP-aware network-error hints.
- Completed: Dashboard overhauled — dark theme (#0f1117), 6-panel layout (status bar, paper progress, analysis feed, risk log, prelive checklist, performance), `/api/status` JSON endpoint, Flask endpoint tests added. 96/96 tests passing.
- Completed: POLYGON_RPC_URL switched to 1rpc.io/matic (free, keyless, no WARP needed); USDC onramp/offramp set to transak; env dotenv comment bug fixed. prelive-checklist: 12/19 PASS.
- Completed: `scripts/setup_scheduler_windows.ps1` — registers PolymarketPaperLoop daily task at 03:00 UTC (08:30 IST) via Task Scheduler COM object with WARP reminder block.
- Completed: Paper day 4 banked — 30 analyses via provider=groq; paper_loop_has_run_today PASS. Tagged v0.8.2.
- Completed: v0.8.3 — analyst prompt v2 (SYSTEM_PROMPT rewrite, `detect_category`, volume tiers, 200-char reasoning), Kelly-adjusted edge (`compute_edge_breakdown` with vol_weight + time_decay), `BOT_MIN_KELLY_FRACTION=0.05`/`BOT_MIN_VOLUME_24H=5000` gates in risk_engine, `reasoning` persisted into analyses.jsonl, dashboard v2 (clickable polymarket.com market links, reasoning shown under analysis cards, edge breakdown shown under risk-log entries, new 7-day Trends panel). 111/111 tests passing (added 15 new).
- Completed: v0.8.4 — auth gate hardening (`_normalize_private_key` strips `0x`, `_normalize_funder_address` EIP-55 checksums via web3), analyst news-context upgrade (`select_relevant_news` keyword-overlap filter + `_extract_description` 300-char Gamma description block in prompt), new `research/market_filter.py` dropping low_volume / resolves_too_soon / too_far_out / near_certain_price / malformed_question candidates with loguru summary, Windows Task Scheduler registered (PolymarketPaperLoop daily 03:00 UTC) + `scheduled_job_registered` checklist check (graceful non-Windows skip). 140/140 tests passing (added 29 new). prelive-checklist: 12/19 PASS (remaining 7 blocked by empty wallet keys + paper day count).
- Completed: v0.8.5 — wallet unblock confirmed (verify-auth PASS: balance=0, 0 open orders), `ANTHROPIC_API_KEY` demoted from required to optional in `load_auth_settings` (analyst provider is groq, anthropic key is only for optional Claude backend), version-tolerant `_call_balance` / `_call_open_orders` helpers map to `get_balance_allowance(BalanceAllowanceParams(asset_type=COLLATERAL))` on py-clob-client 0.34.6, new `wallet_address_valid` prelive check (offline EIP-55), new `all_safety_checks_pass` meta-check (env KILL_SWITCH + risk_config.json kill_switch + BOT_LIVE_MODE + DRY_RUN), env-level `KILL_SWITCH=true` now aborts `paper-loop` before the first cycle, scorecard now prints `brier_score=<value> (sample_count=N, rmse=X)` or `brier_score=unavailable` when no resolutions, new `scripts/audit_signals.py` + baseline `docs/signal_audit_v0.8.4.md`. 147/147 tests passing (added 7 new). **prelive-checklist: 17/21 PASS.**
- Note: Polymarket + polygon.llamarpc.com are DNS-blocked from India — Cloudflare WARP must be active for CLOB/Gamma/WS checks. polygon.llamarpc.com replaced with 1rpc.io/matic for RPC (no WARP needed).
- Signal audit (v0.8.4 baseline, 121 analyses / 5 days): SKIP=84.3%, Low-conf=39.7%, zero markets cleared the risk gate. Neither user-configured tuning threshold tripped (SKIP<95%, Low<90%) so `BOT_MIN_KELLY_FRACTION` and the analyst prompt remain unchanged in v0.8.5. Full table: `docs/signal_audit_v0.8.4.md`.
- Lesson: env `KILL_SWITCH` was advisory-only — the authoritative flag lives in `risk_config.json` and is enforced per-order in `risk_engine.py:254`. v0.8.5 adds a loop-start env check so an operator-set env var actually kills the loop (expected user-mental-model behavior).
- Live-trading blockers (in dependency order):
  1. Fund wallet with >=20 USDC on Polygon (CoinDCX MATIC → withdraw to POLYGON_WALLET_ADDRESS → Uniswap v3 swap to USDC) — unlocks `balance_usdc_gt_20`.
  2. Reach 14 distinct paper-analysis days (currently 6/14) — scheduler runs 03:00 UTC daily when WARP is active.
  3. Resolve enough markets to hit Brier < 0.25 (currently 0.256999 over 8 samples, all synthetic) — needs real resolutions, which require (1) and (2).
  4. Flip DRY_RUN=false + BOT_LIVE_MODE=true last — only after 1–3 and a clean prelive.
