"""Scheduler-health bookkeeping.

Writes one row per completed scheduled paper-loop run to
`data/scheduler_health.jsonl`:

    {"date": "2026-04-21", "status": "ok"|"missed", "reason": "...",
     "warp_active": bool, "analyses_today": int, "timestamp": "..."}

Dashboard / prelive-checklist read this to gate live-mode readiness on
demonstrated daily reliability.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from prediction_bot.utils.network import check_warp_active


def scheduler_health_path(workspace_root: Path) -> Path:
    return workspace_root / "data" / "scheduler_health.jsonl"


def _utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _count_analyses_today(workspace_root: Path) -> int:
    path = workspace_root / "data" / "analyses.jsonl"
    if not path.exists():
        return 0
    today = _utc_today()
    n = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = str(row.get("timestamp") or "")
        if ts[:10] == today:
            n += 1
    return n


def record_cycle_health(
    workspace_root: Path,
    *,
    warp_active: bool | None = None,
    warp_auto_connect_attempted: bool = False,
    status_override: str | None = None,
    reason_override: str | None = None,
) -> dict[str, Any]:
    """Write a single scheduler-health row for today's run and return it.

    Called at the end of a paper-loop run. Status is "ok" if at least one
    analysis was written today, "missed" otherwise. Callers may pass
    status_override="crashed" when recovering from a stale run.lock.
    """
    analyses_today = _count_analyses_today(workspace_root)
    if warp_active is None:
        warp_active = check_warp_active()
    if status_override is not None:
        status = status_override
        reason = reason_override or "override"
    elif analyses_today > 0:
        status = "ok"
        reason = "analyses_written"
    else:
        status = "missed"
        reason = "no_analyses" if warp_active else "no_analyses_warp_inactive"

    payload: dict[str, Any] = {
        "date": _utc_today(),
        "status": status,
        "reason": reason,
        "warp_active": bool(warp_active),
        "warp_auto_connect_attempted": bool(warp_auto_connect_attempted),
        "analyses_today": analyses_today,
        "timestamp": _utc_now_iso(),
    }

    path = scheduler_health_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")
    return payload


def read_scheduler_health(workspace_root: Path, limit: int | None = None) -> list[dict[str, Any]]:
    path = scheduler_health_path(workspace_root)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if limit is not None and limit > 0:
        return out[-limit:]
    return out


def success_rate(workspace_root: Path, window: int = 14) -> tuple[float | None, int, int]:
    """Return (success_rate, ok_count, total) over last `window` entries.

    Returns (None, 0, 0) if there are no entries at all.
    """
    rows = read_scheduler_health(workspace_root, limit=window)
    total = len(rows)
    if total == 0:
        return None, 0, 0
    ok = sum(1 for r in rows if str(r.get("status") or "") == "ok")
    return round(ok / total, 4), ok, total
