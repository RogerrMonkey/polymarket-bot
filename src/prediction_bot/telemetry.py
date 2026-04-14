from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from prediction_bot.paper_metrics import build_long_window_trends


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


def build_telemetry_snapshot(workspace_root: Path) -> dict[str, Any]:
    now = _utc_now()
    since_24h = now - timedelta(hours=24)

    loop_rows = _read_jsonl(workspace_root / "data" / "loop_log.jsonl")
    trade_rows = _read_jsonl(workspace_root / "data" / "trades.jsonl")
    risk_rows = _read_jsonl(workspace_root / "data" / "risk_log.jsonl")

    recent_loops = []
    for row in loop_rows:
        ts = _parse_iso(str(row.get("timestamp") or ""))
        if ts is None:
            # legacy rows without timestamp are considered current-session rows
            recent_loops.append(row)
            continue
        if ts >= since_24h:
            recent_loops.append(row)

    recent_trades = []
    for row in trade_rows:
        ts = _parse_iso(str(row.get("timestamp") or ""))
        if ts is None:
            continue
        if ts >= since_24h:
            recent_trades.append(row)

    recent_risk = []
    for row in risk_rows:
        ts = _parse_iso(str(row.get("timestamp") or ""))
        if ts is None:
            continue
        if ts >= since_24h:
            recent_risk.append(row)

    trade_status_counts = Counter(str(r.get("status") or "unknown") for r in recent_trades)
    rejection_counts = Counter(str(r.get("reason") or "unknown") for r in recent_risk)

    no_candidate_loops = sum(1 for r in recent_loops if int(r.get("scan_candidates") or 0) == 0)

    return {
        "loops_24h": len(recent_loops),
        "no_candidate_loops_24h": no_candidate_loops,
        "trades_24h": len(recent_trades),
        "trade_status_counts": dict(trade_status_counts),
        "risk_rejections_24h": len(recent_risk),
        "top_rejection_reasons": dict(rejection_counts.most_common(5)),
    }


def build_trend_snapshot(workspace_root: Path, db_path: str) -> dict[str, dict[str, Any]]:
    return build_long_window_trends(workspace_root=workspace_root, db_path=db_path, windows=(7, 30))


def build_alerts(
    telemetry: dict[str, Any],
    preflight_blocked: bool,
    usdc_ready: bool,
    rejection_rate: float | None,
    api_cost_usd: float,
) -> list[str]:
    alerts: list[str] = []

    if preflight_blocked:
        alerts.append("critical:preflight_blocked")

    if not usdc_ready:
        alerts.append("warning:usdc_operational_checks_not_ready")

    loops_24h = int(telemetry.get("loops_24h", 0))
    no_candidate_loops = int(telemetry.get("no_candidate_loops_24h", 0))
    if loops_24h >= 5 and no_candidate_loops == loops_24h:
        alerts.append("warning:no_candidates_in_recent_loops")

    if rejection_rate is not None and rejection_rate > 0.9:
        alerts.append("warning:high_rejection_rate")

    max_api_cost = float(os.getenv("BOT_CLAUDE_DAILY_BUDGET", "2"))
    if api_cost_usd >= max_api_cost:
        alerts.append("warning:api_cost_budget_reached")

    return alerts


def print_telemetry(telemetry: dict[str, Any], alerts: list[str], trends: dict[str, dict[str, Any]] | None = None) -> None:
    print(f"loops_24h={telemetry.get('loops_24h', 0)}")
    print(f"no_candidate_loops_24h={telemetry.get('no_candidate_loops_24h', 0)}")
    print(f"trades_24h={telemetry.get('trades_24h', 0)}")
    print(f"risk_rejections_24h={telemetry.get('risk_rejections_24h', 0)}")
    print(f"trade_status_counts={json.dumps(telemetry.get('trade_status_counts', {}), sort_keys=True)}")
    print(f"top_rejection_reasons={json.dumps(telemetry.get('top_rejection_reasons', {}), sort_keys=True)}")
    if trends is not None:
        print(f"trends={json.dumps(trends, sort_keys=True)}")
    print(f"alerts={json.dumps(alerts)}")
