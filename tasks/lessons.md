# Lessons

- Keep memory automation scripts independent from trading pipeline modules to avoid coupling failures.
- Validate scheduled task commands with a manual run and Last Result check before relying on automation.
- Filter external research aggressively before promotion to long-term memory to prevent noise accumulation.
- Before implementation, explicitly load GPT.md directives, memory files, and relevant skill definitions so execution aligns with project operating rules.
- After structural edits to configuration files, always run full tests plus one live CLI command to catch silent logic regressions (for example parser flow bugs).
- When the user supplies a replacement master roadmap file, immediately set it as canonical in docs/memory and align active implementation to that file.
- In src-layout projects, run `pip install -e .` early so `python -m prediction_bot ...` works without ad-hoc `PYTHONPATH` tweaks.
- Treat live-execution stubs as explicit operator-visible states: print warnings when BOT_LIVE_MODE is enabled but order placement is still stubbed.
- For settlement, never infer resolved outcome from closed status alone; require winner/resolution fields or resolved pricing signals, otherwise classify as closed_not_settled.
- Clamp and validate model probabilities before sizing; Kelly math must guard non-finite values and denominator edge cases (market p near 0 or 1).
- Prompt sanitization checks need case-insensitive coverage because injection phrases often appear in mixed or upper case.
- Graceful shutdown paths should handle both KeyboardInterrupt and SIGINT/SIGTERM with explicit cancel_all_open_orders to reduce stale exposure.
- LLM cache-control assumptions should be validated in payload-level tests, not just by constructing helper metadata.
- CryptoPanic free tier discontinued 2026-04-01 — use GDELT + RSS (feedparser) instead; no API key required. Polymarket + polygon.llamarpc.com are DNS-blocked from India and only reachable with Cloudflare WARP active locally.
