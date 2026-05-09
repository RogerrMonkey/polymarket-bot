"""System health snapshot - human-readable one-page summary.

Surfaced as `python -m prediction_bot health-check` and used both for
operator status checks and for the daily paper-accumulation period when
we want a single command that answers "is the bot alive?".
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from prediction_bot.checklist import (
    build_pre_live_report_payload,
    read_pre_live_report,
)
from prediction_bot.outcome_resolver import _read_resolved_market_ids
from prediction_bot.paper_pnl import PaperPnLTracker
from prediction_bot.scheduler_health import read_scheduler_health
from prediction_bot.utils.network import check_warp_active


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _analyst_label() -> str:
    """Return a short "provider (model)" string for the head of the chain."""
    try:
        from prediction_bot.claude_analyst import build_provider_chain

        chain = build_provider_chain()
        if not chain:
            return "unconfigured"
        head = chain[0]
        return f"{head.name} ({getattr(head, 'model', 'n/a')})"
    except Exception as exc:  # noqa: BLE001
        return f"error:{exc}"


def _last_run_summary(workspace_root: Path) -> str:
    rows = [r for r in read_scheduler_health(workspace_root) if r.get("type") != "heartbeat"]
    if not rows:
        return "never"
    last = rows[-1]
    return (
        f"{last.get('date')} "
        f"(status: {last.get('status')}, {last.get('analyses_today')} analyses)"
    )


def _today_warp_drops(workspace_root: Path) -> tuple[int, int]:
    """Return (warp_drops_today, total_cycles_today_proxy)."""
    today = datetime.now(timezone.utc).date().isoformat()
    rows = [
        r for r in read_scheduler_health(workspace_root)
        if r.get("type") != "heartbeat" and str(r.get("date") or "") == today
    ]
    if not rows:
        return 0, 0
    drops = sum(int(r.get("warp_drops") or 0) for r in rows)
    cycles = sum(int(r.get("analyses_today") or 0) for r in rows)
    return drops, max(cycles, len(rows))


def _paper_days(workspace_root: Path) -> int:
    """Distinct UTC dates present in data/analyses.jsonl."""
    path = workspace_root / "data" / "analyses.jsonl"
    if not path.exists():
        return 0
    days: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        ts = str(row.get("timestamp") or "")
        if len(ts) >= 10:
            days.add(ts[:10])
    return len(days)


def _prelive_counts(workspace_root: Path) -> tuple[int, int]:
    report = read_pre_live_report(workspace_root)
    if not report:
        return 0, 0
    passed = int(report.get("passed_count", 0) or 0)
    total = passed + int(report.get("failed_count", 0) or 0)
    return passed, total


def _brier(db_path: str) -> tuple[float | None, int]:
    try:
        from prediction_bot.storage.prediction_store import PredictionStore

        store = PredictionStore(db_path)
        metrics = store.brier_metrics()
        return metrics.brier_score, metrics.sample_count
    except Exception:  # noqa: BLE001
        return None, 0


def _today_analyses_breakdown(workspace_root: Path) -> dict[str, int]:
    """Return today's BUY/SELL/SKIP counts from analyses.jsonl."""
    out = {"BUY": 0, "SELL": 0, "SKIP": 0, "total": 0}
    path = workspace_root / "data" / "analyses.jsonl"
    if not path.exists():
        return out
    today = datetime.now(timezone.utc).date().isoformat()
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        ts = str(row.get("timestamp") or "")
        if ts[:10] != today:
            continue
        decision = str(row.get("decision") or "").upper()
        if decision == "YES":
            decision = "BUY"
        elif decision == "NO":
            decision = "SELL"
        if decision in {"BUY", "SELL", "SKIP"}:
            out[decision] += 1
            out["total"] += 1
    return out


def _watchlist_status(workspace_root: Path) -> str:
    """Quick freshness signal: count of IDs and last-modified hours-ago."""
    path = workspace_root / "watchlist.json"
    if not path.exists():
        return "missing"
    try:
        ids = json.loads(path.read_text(encoding="utf-8"))
        n = len(ids) if isinstance(ids, list) else 0
    except Exception:  # noqa: BLE001
        return "unreadable"
    if n == 0:
        return "empty"
    age_hours = (datetime.now(timezone.utc).timestamp() - path.stat().st_mtime) / 3600.0
    age_str = f"{int(age_hours)}h" if age_hours < 96 else f"{int(age_hours/24)}d"
    label = "fresh" if age_hours < 168 else "stale"
    return f"{n} markets, {label} (updated {age_str} ago)"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def collect_health(workspace_root: Path, db_path: str) -> dict[str, Any]:
    """Gather everything needed for the printed one-pager (and JSON)."""
    warp = check_warp_active()
    paper_days = _paper_days(workspace_root)
    passed, total = _prelive_counts(workspace_root)
    resolved = len(_read_resolved_market_ids(workspace_root))
    brier, n = _brier(db_path)
    pnl = PaperPnLTracker(ledger_path=workspace_root / "data" / "paper_pnl.jsonl").summary()
    return {
        "timestamp": _utc_now_iso(),
        "analyst": _analyst_label(),
        "warp_active": warp,
        "last_run": _last_run_summary(workspace_root),
        "paper_days": paper_days,
        "prelive_passed": passed,
        "prelive_total": total,
        "resolved_markets": resolved,
        "brier_score": brier,
        "brier_samples": n,
        "starting_bankroll": pnl.get("starting_bankroll", 100.0),
        "current_bankroll": pnl.get("current_bankroll", 100.0),
        "total_trades": pnl.get("total_trades", 0),
        "kill_switch": _env_bool("KILL_SWITCH"),
        "live_mode": _env_bool("BOT_LIVE_MODE"),
        "today_analyses": _today_analyses_breakdown(workspace_root),
        "watchlist_status": _watchlist_status(workspace_root),
        "warp_drops_today": _today_warp_drops(workspace_root),
    }


def _status_line(data: dict[str, Any]) -> str:
    if data["live_mode"]:
        return "LIVE MODE ACTIVE - monitor closely"
    if data["kill_switch"]:
        return "KILL SWITCH ACTIVE - no orders will be placed"
    if data["paper_days"] < 14:
        remaining = 14 - data["paper_days"]
        return f"ACCUMULATING PAPER DAYS - check back in ~{remaining} days"
    if data["paper_days"] < 30:
        remaining = 30 - data["paper_days"]
        return f"PAPER MODE - {remaining} days to minimum live-readiness window"
    return "PAPER MODE - live-readiness window reached; run live-readiness for gate status"


def print_health(data: dict[str, Any]) -> None:
    warp = "YES" if data["warp_active"] else "NO"
    ks = "ON" if data["kill_switch"] else "OFF"
    lm = "ON" if data["live_mode"] else "OFF (paper only)"
    brier = (
        f"{data['brier_score']:.3f} (n={data['brier_samples']})"
        if data["brier_score"] is not None
        else "unavailable"
    )
    if data["brier_samples"] < 20:
        brier = f"{brier}, insufficient for validation"
    bankroll = f"${data['current_bankroll']:.2f}"
    if data["total_trades"] == 0:
        bankroll = f"${data['starting_bankroll']:.2f} (no resolved trades yet)"

    print(f"System Health - {data['timestamp']}")
    print("-----------------------------")
    print(f"Analyst provider : {data['analyst']}")
    print(f"WARP active      : {warp}")
    print(f"Last run         : {data['last_run']}")
    print(f"Paper days       : {data['paper_days']} / 14 (min gate) | {data['paper_days']} / 30 (live gate)")
    print(f"Prelive PASS     : {data['prelive_passed']} / {data['prelive_total']}")
    print(f"Resolved markets : {data['resolved_markets']}")
    print(f"Brier score      : {brier}")
    print(f"Bankroll (paper) : {bankroll}")
    today = data.get("today_analyses") or {"total": 0, "BUY": 0, "SELL": 0, "SKIP": 0}
    print(f"Today's analyses : {today.get('total', 0)} ({today.get('BUY', 0)} BUY, {today.get('SELL', 0)} SELL, {today.get('SKIP', 0)} SKIP)")
    drops, cycles = data.get("warp_drops_today") or (0, 0)
    if cycles:
        print(f"WARP drops today : {drops} / {cycles} cycles")
    print(f"Watchlist status : {data.get('watchlist_status', 'unknown')}")
    print(f"Kill switch      : {ks}")
    print(f"Live mode        : {lm}")
    print("-----------------------------")
    print(f"Status: {_status_line(data)}")


def run_health_check_command(workspace_root: Path, db_path: str) -> int:
    data = collect_health(workspace_root=workspace_root, db_path=db_path)
    print_health(data)
    return 0
