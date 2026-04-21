# Signal Audit — v0.8.4 baseline

Snapshot of `data/analyses.jsonl` taken 2026-04-21.
Produced by `scripts/audit_signals.py`.

## Summary
- 121 analyses across 5 distinct paper days (2026-04-05, -06, -15, -19, -20)
- Analyst: groq / llama-3.3-70b-versatile
- **Zero markets have cleared the risk gate to date.**

## Decision distribution
| Decision | Count | %     |
|----------|-------|-------|
| SKIP     | 102   | 84.3% |
| NO       | 10    |  8.3% |
| YES      |  9    |  7.4% |

## Confidence distribution
| Conf   | Count | %     |
|--------|-------|-------|
| Low    | 48    | 39.7% |
| Medium | 37    | 30.6% |
| High   | 22    | 18.2% |
| UNSET  | 14    | 11.6% |

## Top 10 reasoning strings (normalized)
1. [17×] lack of relevant news and high market volume
2. [ 4×] no relevant news, high volume market implies efficient pricing
3. [ 4×] no relevant news, high volume market, no edge
4. [ 3×] no relevant news, high volume market
5. [ 2×] market price reflects base rate of regime stability
6. [ 2×] lack of relevant news, high volume market
7. [ 2×] lack of relevant news and high volume market
8. [ 2×] liquidity is high, no relevant news
9. [ 1×] market price reflects consensus, no clear signal to contradict
10. [ 1×] high volume market with no high relevance recent news

## Average edge by confidence
| Conf   | avg_edge | n  |
|--------|----------|----|
| High   | 0.0900   | 22 |
| Low    | 0.0461   | 48 |
| Medium | 0.0154   | 37 |

## Average probability by decision
| Decision | avg_prob | n   |
|----------|----------|-----|
| NO       | 0.1220   |   5 |
| SKIP     | 0.1356   | 102 |

## Top 5 days by analysis count
| Day         | n  |
|-------------|----|
| 2026-04-19  | 57 |
| 2026-04-20  | 40 |
| 2026-04-06  | 10 |
| 2026-04-15  | 10 |
| 2026-04-05  |  4 |

## Tuning decision for v0.8.4
Per session rule: `BOT_MIN_KELLY_FRACTION` is lowered to 0.03 only if
SKIP > 95%, and confidence prompt is strengthened only if Low > 90%.
Neither threshold is met (SKIP=84.3%, Low=39.7%) so **no parameter
tune is applied in this version**. The real blocker to surface is that
zero markets have cleared the risk gate — worth investigating
separately: are directional calls failing min_kelly_fraction? min_edge?
(See also `data/risk_log.jsonl`.)

## Reproducing
```bash
python scripts/audit_signals.py
```
