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

from prediction_bot.health_check import _brier, _env_bool, _paper_days
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
    from datetime import timedelta

    eta = today + timedelta(days=remaining)
    return f"~{eta.isoformat()} (est. based on paper-day accumulation rate)"


def collect_readiness(workspace_root: Path, db_path: str) -> list[ReadinessGate]:
    return [
        _auth_gate(),
        _paper_days_gate(workspace_root),
        _brier_gate(db_path),
        _win_rate_gate(workspace_root),
        _kill_switch_gate(),
        _live_mode_off_gate(),
    ]


def print_readiness(workspace_root: Path, gates: list[ReadinessGate]) -> None:
    print(f"Live Readiness Report - {_utc_today()}")
    print("====================================")
    for g in gates:
        status = "PASS" if g.passed else "FAIL"
        print(f"[{status}] {g.name}: {g.detail}")
    print("====================================")
    failing = [g for g in gates if not g.passed]
    if not failing:
        print("VERDICT: READY - all gates passing. Follow docs/LIVE_MODE_RUNBOOK.md.")
    else:
        print(f"VERDICT: NOT READY - {len(failing)} gates failing")
        print(f"Earliest ready: {_estimate_ready_date(_paper_days(workspace_root))}")


def run_live_readiness_command(workspace_root: Path, db_path: str) -> int:
    gates = collect_readiness(workspace_root=workspace_root, db_path=db_path)
    print_readiness(workspace_root, gates)
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
