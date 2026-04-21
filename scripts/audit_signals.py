"""One-off signal audit for v0.8.4 paper analyses.

Reads data/analyses.jsonl and prints:
  - decision distribution (SKIP/BUY/SELL/YES/NO/...) with percentages
  - confidence distribution
  - top-10 normalized reasoning strings
  - average edge by confidence level
  - average probability by decision
  - markets that cleared risk gates (if any)
  - top 5 days by analysis count

Stdlib-only. No loguru (this is an operator report, not pipeline code).
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ANALYSES = ROOT / "data" / "analyses.jsonl"
RISK_LOG = ROOT / "data" / "risk_log.jsonl"
TRADES = ROOT / "data" / "trades.jsonl"


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _pct(n: int, total: int) -> str:
    return f"{(n / total * 100.0):5.1f}%" if total else "  n/a "


def _avg(values: list[float]) -> str:
    return f"{sum(values) / len(values):.4f}" if values else "n/a"


def main() -> None:
    rows = _load(ANALYSES)
    risk_rows = _load(RISK_LOG)
    total = len(rows)

    print("=" * 60)
    print(f"Signal Audit - {ANALYSES}")
    print(f"Total analyses: {total}")
    print("=" * 60)

    if total == 0:
        print("No analyses to audit.")
        return

    # Decision distribution
    decisions = Counter(r.get("decision") or "UNSET" for r in rows)
    print("\nDecision distribution:")
    for name, count in decisions.most_common():
        print(f"  {name:<10} {count:>4}  {_pct(count, total)}")

    # Confidence distribution
    confidences = Counter(r.get("confidence") or "UNSET" for r in rows)
    print("\nConfidence distribution:")
    for name, count in confidences.most_common():
        print(f"  {name:<10} {count:>4}  {_pct(count, total)}")

    # Top reasoning strings
    reasoning_counts = Counter(
        (r.get("reasoning") or "").strip().lower()
        for r in rows
        if r.get("reasoning")
    )
    print("\nTop 10 reasoning strings:")
    for text, count in reasoning_counts.most_common(10):
        preview = (text[:80] + "…") if len(text) > 80 else text
        print(f"  [{count:>3}x] {preview}")

    # Average edge by confidence
    edge_by_conf: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        conf = r.get("confidence") or "UNSET"
        edge = r.get("edge")
        if isinstance(edge, (int, float)):
            edge_by_conf[conf].append(float(edge))
    print("\nAverage edge by confidence:")
    for conf in sorted(edge_by_conf.keys()):
        print(f"  {conf:<10} avg_edge={_avg(edge_by_conf[conf])}  n={len(edge_by_conf[conf])}")

    # Average probability by decision
    prob_by_dec: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        dec = r.get("decision") or "UNSET"
        p = r.get("probability")
        if isinstance(p, (int, float)):
            prob_by_dec[dec].append(float(p))
    print("\nAverage probability by decision:")
    for dec in sorted(prob_by_dec.keys()):
        print(f"  {dec:<10} avg_prob={_avg(prob_by_dec[dec])}  n={len(prob_by_dec[dec])}")

    # Rejection reasons (from risk_log.jsonl)
    rej_counts = Counter(r.get("reason", "?") for r in risk_rows)
    print(f"\nRejection reasons (from risk_log.jsonl, total {len(risk_rows)}):")
    for reason, count in rej_counts.most_common():
        print(f"  [{count:>4}]  {reason}")

    # Trades that passed the risk gate (from trades.jsonl - authoritative)
    trade_rows = _load(TRADES)
    real = [r for r in trade_rows if not str(r.get("market_id", "")).startswith("syn-")]
    synthetic = len(trade_rows) - len(real)
    print(f"\nMarkets that passed risk gate (trades.jsonl): {len(trade_rows)}")
    print(f"  real:      {len(real)}")
    print(f"  synthetic: {synthetic}")
    for r in real[:10]:
        mkt = r.get("market_id") or "?"
        side = r.get("side") or "?"
        size = r.get("size_usdc") or 0.0
        ts = (r.get("timestamp") or "")[:19]
        print(f"  {ts}  {mkt}  {side}  ${size}")

    # Top 5 days by analysis count
    day_counts = Counter((r.get("timestamp") or "")[:10] for r in rows)
    print("\nTop 5 days by analysis count:")
    for day, count in day_counts.most_common(5):
        print(f"  {day or '(no-date)':<12} {count}")

    print("\nDistinct days:", len([d for d in day_counts if d]))


if __name__ == "__main__":
    main()
