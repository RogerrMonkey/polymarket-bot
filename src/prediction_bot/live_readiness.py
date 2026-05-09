"""Live-readiness gate evaluation.

Surfaced as `python -m prediction_bot live-readiness`. Aggregates the
handful of thresholds we require before flipping BOT_LIVE_MODE=true and
prints a single bulleted PASS/FAIL report.

Gates:
- Auth verified (env present; cheap offline checks)
- Paper days >= 30
- Brier score < 0.22 over >= 20 resolved markets
- Win rate > 52% over >= 10 resolved trades
- Kill switch OFF
- Live mode currently OFF
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from datetime import timedelta

from prediction_bot.health_check import _brier, _env_bool, _paper_days
from prediction_bot.outcome_resolver import _read_resolved_market_ids
from prediction_bot.paper_pnl import PaperPnLTracker


@dataclass(frozen=True)
class ReadinessGate:
    name: str
    passed: bool
    detail: str


def _utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _auth_gate() -> ReadinessGate:
    required = ("POLYMARKET_PRIVATE_KEY", "POLYMARKET_FUNDER_ADDRESS", "SIGNATURE_TYPE")
    missing = [k for k in required if not (os.getenv(k) or "").strip()]
    if missing:
        return ReadinessGate("Auth verified", False, f"missing={','.join(missing)}")
    return ReadinessGate("Auth verified", True, "env present")


def _paper_days_gate(workspace_root: Path) -> ReadinessGate:
    days = _paper_days(workspace_root)
    return ReadinessGate(
        "Paper days: >= 30",
        days >= 30,
        f"paper days: {days}/30",
    )


def _brier_gate(db_path: str) -> ReadinessGate:
    score, n = _brier(db_path)
    if n < 20:
        return ReadinessGate(
            "Brier score < 0.22 (n>=20)",
            False,
            f"insufficient data ({n}/20 markets)",
        )
    if score is None:
        return ReadinessGate("Brier score < 0.22 (n>=20)", False, "brier_unavailable")
    return ReadinessGate(
        "Brier score < 0.22 (n>=20)",
        score < 0.22,
        f"brier={score:.3f} (n={n})",
    )


def _win_rate_gate(workspace_root: Path) -> ReadinessGate:
    pnl = PaperPnLTracker(ledger_path=workspace_root / "data" / "paper_pnl.jsonl").summary()
    n = int(pnl.get("total_trades") or 0)
    if n < 10:
        return ReadinessGate(
            "Win rate > 52% (n>=10)",
            False,
            f"insufficient data ({n}/10 trades)",
        )
    wr = pnl.get("win_rate")
    if wr is None:
        return ReadinessGate("Win rate > 52% (n>=10)", False, "win_rate_unavailable")
    return ReadinessGate(
        "Win rate > 52% (n>=10)",
        wr > 0.52,
        f"win_rate={wr:.2%} over {n} trades",
    )


def _kill_switch_gate() -> ReadinessGate:
    active = _env_bool("KILL_SWITCH")
    return ReadinessGate("Kill switch: OFF", not active, "ON" if active else "OFF")


def _live_mode_off_gate() -> ReadinessGate:
    on = _env_bool("BOT_LIVE_MODE")
    return ReadinessGate(
        "Live mode: currently OFF",
        not on,
        "currently ON (already live!)" if on else "currently OFF",
    )


def _estimate_ready_date(paper_days: int) -> str:
    """Rough ETA: one paper day per calendar day, so 30-n days out."""
    remaining = max(0, 30 - paper_days)
    if remaining <= 0:
        return "today (paper-days threshold met; other gates may still be pending)"
    today = datetime.now(timezone.utc).date()

    eta = today + timedelta(days=remaining)
    return f"~{eta.isoformat()} (est. based on paper-day accumulation rate)"


def _days_since_first_analysis(workspace_root: Path) -> float:
    """Days elapsed between the earliest analyses.jsonl entry and now."""
    path = workspace_root / "data" / "analyses.jsonl"
    if not path.exists():
        return 0.0
    earliest: datetime | None = None
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        ts = str(row.get("timestamp") or "")
        if not ts:
            continue
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if earliest is None or dt < earliest:
            earliest = dt
    if earliest is None:
        return 0.0
    elapsed = (datetime.now(timezone.utc) - earliest).total_seconds() / 86400.0
    return max(0.0, elapsed)


def _eta_paper_days(workspace_root: Path) -> tuple[str, float | None]:
    """Estimate ETA for the 30-day paper gate based on observed accumulation rate."""
    days = _paper_days(workspace_root)
    if days >= 30:
        return ("met", 0.0)
    elapsed = _days_since_first_analysis(workspace_root)
    if elapsed <= 0 or days <= 0:
        # No history yet; default to one paper day per calendar day.
        rate = 1.0
    else:
        rate = days / elapsed
    rate = max(rate, 0.05)  # guard against pathologically low rates
    days_remaining = (30 - days) / rate
    eta_date = (datetime.now(timezone.utc) + timedelta(days=days_remaining)).date().isoformat()
    return (eta_date, rate)


def _eta_brier(workspace_root: Path, db_path: str) -> tuple[str, float | None, int]:
    """Estimate ETA for n>=20 resolved markets based on resolution rate."""
    _score, n = _brier(db_path)
    resolved_count = len(_read_resolved_market_ids(workspace_root))
    # Use the larger of the two — predictions store may lag the markets file.
    effective_n = max(n, resolved_count)
    if effective_n >= 20:
        return ("met", None, effective_n)
    elapsed = _days_since_first_analysis(workspace_root)
    if elapsed > 0 and effective_n > 0:
        rate_per_day = effective_n / elapsed
    else:
        # No resolutions yet — Polymarket markets typically settle ~30 days
        # out, so assume that as a coarse upper bound.
        rate_per_day = 1.0 / 30.0
    rate_per_day = max(rate_per_day, 0.01)  # avoid divide-by-near-zero
    days_remaining = (20 - effective_n) / rate_per_day
    eta_date = (datetime.now(timezone.utc) + timedelta(days=days_remaining)).date().isoformat()
    return (eta_date, rate_per_day, effective_n)


def collect_readiness(workspace_root: Path, db_path: str) -> list[ReadinessGate]:
    return [
        _auth_gate(),
        _paper_days_gate(workspace_root),
        _brier_gate(db_path),
        _win_rate_gate(workspace_root),
        _kill_switch_gate(),
        _live_mode_off_gate(),
    ]


def print_readiness(workspace_root: Path, gates: list[ReadinessGate], db_path: str) -> None:
    """Print PASS/FAIL gates with per-gate ETA hints under the failing ones.

    Bottleneck = max(paper_eta, brier_eta) + 3-day buffer.
    """
    paper_eta, paper_rate = _eta_paper_days(workspace_root)
    brier_eta, brier_rate, brier_n = _eta_brier(workspace_root, db_path)

    print(f"Live Readiness Report - {_utc_today()}")
    print("=" * 56)
    for g in gates:
        status = "PASS" if g.passed else "FAIL"
        print(f"[{status}] {g.name}: {g.detail}")
        if not g.passed:
            if g.name == "Paper days: >= 30":
                if paper_eta == "met":
                    print("       (threshold met)")
                else:
                    rate_str = f"{paper_rate:.2f}" if paper_rate is not None else "?"
                    print(f"       Rate: {rate_str} days/day -> ETA: ~{paper_eta}")
            elif g.name == "Brier score < 0.22 (n>=20)":
                if brier_eta == "met":
                    print("       (n>=20 reached; check current score)")
                else:
                    rate_str = f"~{brier_rate:.2f}/day" if brier_rate is not None else "?"
                    print(f"       Resolution rate: {rate_str} -> ETA: ~{brier_eta}")
            elif g.name == "Win rate > 52% (n>=10)":
                if brier_n < 5:
                    print("       Insufficient data - need 10 resolved approved trades")
                else:
                    print(f"       ETA: ~{brier_eta} (gated by market resolutions)")
    print("=" * 56)
    failing = [g for g in gates if not g.passed]
    if not failing:
        print("VERDICT: READY - all gates passing. Follow docs/LIVE_MODE_RUNBOOK.md.")
        return

    print(f"VERDICT: NOT READY - {len(failing)} gates failing")
    # Compute overall ETA = max(paper, brier) + 3-day buffer.
    candidate_dates: list[str] = [d for d in (paper_eta, brier_eta) if d not in ("met", None)]
    if candidate_dates:
        latest = max(candidate_dates)
        try:
            latest_dt = datetime.fromisoformat(latest).replace(tzinfo=timezone.utc)
            overall_eta = (latest_dt + timedelta(days=3)).date().isoformat()
        except ValueError:
            overall_eta = latest
        bottleneck = "market resolution rate" if brier_eta != "met" and brier_eta == latest else "paper-day accumulation"
        print(f"Estimated ready: ~{overall_eta}")
        print(f"(bottleneck: {bottleneck})")
    else:
        print(f"Earliest ready: {_estimate_ready_date(_paper_days(workspace_root))}")


def run_live_readiness_command(workspace_root: Path, db_path: str) -> int:
    gates = collect_readiness(workspace_root=workspace_root, db_path=db_path)
    print_readiness(workspace_root, gates, db_path=db_path)
    return 0 if all(g.passed for g in gates) else 1


def build_readiness_payload(workspace_root: Path, db_path: str) -> dict[str, Any]:
    """Machine-readable variant - handy for dashboards and tests."""
    gates = collect_readiness(workspace_root=workspace_root, db_path=db_path)
    return {
        "timestamp": _utc_today(),
        "ready": all(g.passed for g in gates),
        "gates": [
            {"name": g.name, "passed": g.passed, "detail": g.detail}
            for g in gates
        ],
    }
