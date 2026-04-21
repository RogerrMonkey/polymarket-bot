from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from prediction_bot.storage.prediction_store import PredictionStore


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _today_bounds() -> tuple[datetime, datetime]:
    now = _utc_now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start, end


def _window_bounds(days: int) -> tuple[datetime, datetime]:
    if days <= 0:
        raise ValueError("days must be positive")
    end = _utc_now()
    start = end - timedelta(days=days)
    return start, end


def _db_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _prediction_rows_between(db_path: str, start: datetime, end: datetime) -> list[sqlite3.Row]:
    with _db_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, created_at, approved, reasons, market_probability, calibrated_probability, outcome
            FROM predictions
            """
        ).fetchall()

    out: list[sqlite3.Row] = []
    for row in rows:
        created = _parse_iso(str(row["created_at"]))
        if created is None:
            continue
        if start <= created < end:
            out.append(row)
    return out


def _daily_prediction_rows(db_path: str) -> list[sqlite3.Row]:
    start, end = _today_bounds()
    return _prediction_rows_between(db_path, start, end)


def compute_brier_score(db_path: str) -> float | None:
    store = PredictionStore(db_path)
    metrics = store.brier_metrics()
    return metrics.brier_score


def daily_summary(workspace_root: Path, db_path: str) -> dict[str, Any]:
    rows = _daily_prediction_rows(db_path)
    total_signals = len(rows)

    traded = [r for r in rows if int(r["approved"]) == 1]
    rejected = [r for r in rows if int(r["approved"]) == 0]

    reasons = Counter()
    for row in rejected:
        raw = str(row["reasons"] or "[]")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = [raw]
        if isinstance(parsed, list):
            for reason in parsed:
                reasons[str(reason)] += 1

    actual_pnl = 0.0
    for row in traded:
        outcome = row["outcome"]
        if outcome is None:
            continue
        # Proxy paper PnL estimate for binary contract: realized minus entry price.
        actual_pnl += float(outcome) - float(row["market_probability"])

    analyses_rows = _read_jsonl(workspace_root / "data" / "analyses.jsonl")
    start, end = _today_bounds()
    api_cost_usd = 0.0
    for row in analyses_rows:
        ts = _parse_iso(str(row.get("timestamp") or ""))
        if ts is None or not (start <= ts < end):
            continue
        try:
            api_cost_usd += float(row.get("cost_usd", 0.0))
        except (TypeError, ValueError):
            continue

    brier = compute_brier_score(db_path)

    risk_log_rows = _read_jsonl(workspace_root / "data" / "risk_log.jsonl")
    risk_cap_hits = 0
    for row in risk_log_rows:
        ts = _parse_iso(str(row.get("timestamp") or ""))
        if ts is None or not (start <= ts < end):
            continue
        reason = str(row.get("reason") or "")
        if "cap hit" in reason.lower() or "drawdown" in reason.lower() or "kill switch" in reason.lower():
            risk_cap_hits += 1

    return {
        "total_signals": total_signals,
        "signals_traded": len(traded),
        "signals_rejected": len(rejected),
        "rejection_reasons": dict(reasons),
        "estimated_pnl": round(actual_pnl, 6),
        "actual_pnl": round(actual_pnl, 6),
        "api_cost_usd": round(api_cost_usd, 6),
        "brier_score": brier,
        "risk_cap_hits": risk_cap_hits,
    }


def window_summary(workspace_root: Path, db_path: str, days: int) -> dict[str, Any]:
    start, end = _window_bounds(days)
    rows = _prediction_rows_between(db_path, start, end)

    total_signals = len(rows)
    traded = [r for r in rows if int(r["approved"]) == 1]
    rejected = [r for r in rows if int(r["approved"]) == 0]

    resolved = [r for r in rows if r["outcome"] is not None]
    resolved_traded = [r for r in traded if r["outcome"] is not None]

    directional_hits = 0
    brier_total = 0.0
    for row in resolved:
        predicted_yes = 1.0 if float(row["calibrated_probability"]) >= 0.5 else 0.0
        outcome = float(row["outcome"])
        if predicted_yes == outcome:
            directional_hits += 1
        brier_total += (float(row["calibrated_probability"]) - outcome) ** 2

    approval_rate = (len(traded) / total_signals) if total_signals else None
    resolved_accuracy = (directional_hits / len(resolved)) if resolved else None
    brier_score = (brier_total / len(resolved)) if resolved else None
    avg_edge = (sum(float(r["calibrated_probability"]) - float(r["market_probability"]) for r in rows) / total_signals) if total_signals else None

    pnl_proxy = 0.0
    for row in resolved_traded:
        pnl_proxy += float(row["outcome"]) - float(row["market_probability"])

    return {
        "window_days": days,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "total_signals": total_signals,
        "signals_traded": len(traded),
        "signals_rejected": len(rejected),
        "approval_rate": round(approval_rate, 6) if approval_rate is not None else None,
        "resolved_signals": len(resolved),
        "resolved_accuracy": round(resolved_accuracy, 6) if resolved_accuracy is not None else None,
        "brier_score": round(brier_score, 6) if brier_score is not None else None,
        "avg_edge": round(avg_edge, 6) if avg_edge is not None else None,
        "realized_pnl_proxy": round(pnl_proxy, 6),
    }


def build_long_window_trends(workspace_root: Path, db_path: str, windows: tuple[int, ...] = (7, 30)) -> dict[str, dict[str, Any]]:
    trends: dict[str, dict[str, Any]] = {}
    for days in windows:
        trends[f"{days}d"] = window_summary(workspace_root=workspace_root, db_path=db_path, days=days)
    return trends


def print_scorecard(workspace_root: Path, db_path: str) -> dict[str, Any]:
    summary = daily_summary(workspace_root=workspace_root, db_path=db_path)
    for key in [
        "total_signals",
        "signals_traded",
        "signals_rejected",
        "estimated_pnl",
        "actual_pnl",
        "api_cost_usd",
        "risk_cap_hits",
    ]:
        print(f"{key}={summary[key]}")

    # Brier score is lifetime across ALL resolved predictions (not today-scoped).
    # Surface sample_count so the operator can tell synthetic replay from real resolutions.
    store = PredictionStore(db_path)
    bm = store.brier_metrics()
    if bm.brier_score is None or bm.sample_count == 0:
        print("brier_score=unavailable (no resolved markets yet)")
    else:
        print(f"brier_score={bm.brier_score} (sample_count={bm.sample_count}, rmse={bm.rmse})")

    print("rejection_reasons:")
    reasons = summary.get("rejection_reasons", {})
    if not reasons:
        print("  none")
    else:
        for reason, count in reasons.items():
            print(f"  {reason}: {count}")
    return summary


def check_paper_gates(workspace_root: Path, db_path: str) -> tuple[bool, list[str]]:
    summary = daily_summary(workspace_root=workspace_root, db_path=db_path)
    failures: list[str] = []

    brier = summary.get("brier_score")
    if brier is None or float(brier) >= 0.25:
        failures.append("brier_score_gate_failed")

    total = int(summary.get("total_signals", 0))
    rejected = int(summary.get("signals_rejected", 0))
    if total <= 0:
        failures.append("rejection_rate_gate_failed:no_signals")
    else:
        rejection_rate = rejected / total
        if rejection_rate < 0.30 or rejection_rate > 0.85:
            failures.append("rejection_rate_gate_failed")

    budget = float(os.getenv("BOT_CLAUDE_DAILY_BUDGET", "2"))
    api_cost = float(summary.get("api_cost_usd", 0.0))
    if api_cost >= budget:
        failures.append("api_cost_gate_failed")

    analyses_rows = _read_jsonl(workspace_root / "data" / "analyses.jsonl")
    analysis_days = {
        ts.date().isoformat()
        for row in analyses_rows
        if (ts := _parse_iso(str(row.get("timestamp") or ""))) is not None
    }
    if len(analysis_days) < 14:
        failures.append("minimum_paper_days_gate_failed")

    now = _utc_now()
    kill_since = now - timedelta(days=7)
    risk_log_rows = _read_jsonl(workspace_root / "data" / "risk_log.jsonl")
    kill_switch_hits = 0
    for row in risk_log_rows:
        ts = _parse_iso(str(row.get("timestamp") or ""))
        if ts is None or ts < kill_since:
            continue
        reason = str(row.get("reason") or "")
        if "kill switch" in reason.lower():
            kill_switch_hits += 1

    if kill_switch_hits > 0:
        failures.append("kill_switch_recent_trigger")

    return len(failures) == 0, failures
