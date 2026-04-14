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
- [x] Add Phase 3 deterministic news_feed.py (CryptoPanic + GDELT) with prompt-injection-safe sanitization and pipeline integration.
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
- Pending: Accumulate >=14 distinct paper-analysis days and stable daily signal flow to clear Phase 9 gates.
