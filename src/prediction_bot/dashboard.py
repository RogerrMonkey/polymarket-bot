from __future__ import annotations

import json
import os
import socket
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import Flask, flash, get_flashed_messages, jsonify, redirect, render_template_string, request, url_for

from prediction_bot.alerting import dispatch_alerts, read_alert_state
from prediction_bot.checklist import (
    collect_pre_live_checks,
    read_pre_live_report,
    run_pre_live_checklist,
    write_pre_live_report,
)
from prediction_bot.clients.http import HttpClient
from prediction_bot.clients.polymarket import PolymarketClient
from prediction_bot.config import load_config
from prediction_bot.outcome_resolver import OutcomeResolver, read_resolution_report, write_resolution_report
from prediction_bot.paper_metrics import check_paper_gates, daily_summary
from prediction_bot.pipeline.compliance import run_preflight
from prediction_bot.pipeline.runner import execute_scan_run
from prediction_bot.risk_engine import RiskConfig
from prediction_bot.storage.prediction_store import PredictionStore
from prediction_bot.synthetic_replay import read_synthetic_replay_report, run_synthetic_replay, write_synthetic_replay_report
from prediction_bot.telemetry import build_alerts, build_telemetry_snapshot, build_trend_snapshot
from prediction_bot.usdc_ops import USDCCheckReport, run_usdc_operational_checks


PAPER_DAYS_TARGET = 14
_PROCESS_START_TS = time.time()


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


def _build_trade_lifecycle(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in trades:
        order_id = str(row.get("order_id") or "unknown-order")
        buckets.setdefault(order_id, []).append(row)

    summaries: list[dict[str, Any]] = []
    for order_id, rows in buckets.items():
        rows.sort(key=lambda r: str(r.get("timestamp") or ""))
        first = rows[0]
        last = rows[-1]
        status_path = " -> ".join(str(r.get("status") or "unknown") for r in rows)

        first_ts = _parse_iso(str(first.get("timestamp") or ""))
        last_ts = _parse_iso(str(last.get("timestamp") or ""))
        duration_seconds = None
        if first_ts is not None and last_ts is not None:
            duration_seconds = max(0.0, (last_ts - first_ts).total_seconds())

        summaries.append(
            {
                "order_id": order_id,
                "market_id": str(last.get("market_id") or ""),
                "side": str(last.get("side") or ""),
                "size_usdc": last.get("size_usdc"),
                "latest_status": str(last.get("status") or ""),
                "status_path": status_path,
                "events": len(rows),
                "duration_seconds": duration_seconds,
                "last_timestamp": str(last.get("timestamp") or ""),
            }
        )

    summaries.sort(key=lambda s: s.get("last_timestamp", ""), reverse=True)
    return summaries


def _read_jsonl(path: Path, limit: int = 20) -> list[dict[str, Any]]:
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
    return rows[-limit:][::-1]


def _read_jsonl_all(path: Path) -> list[dict[str, Any]]:
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


def _load_risk_config(path: Path) -> RiskConfig:
    return RiskConfig.from_json_file(path)


def _save_kill_switch(path: Path, enabled: bool) -> None:
    cfg = RiskConfig.from_json_file(path)
    updated = RiskConfig(
        daily_loss_cap_pct=cfg.daily_loss_cap_pct,
        max_drawdown_pct=cfg.max_drawdown_pct,
        max_position_pct=cfg.max_position_pct,
        min_edge=cfg.min_edge,
        min_confidence=cfg.min_confidence,
        kelly_fraction=cfg.kelly_fraction,
        min_liquidity_usdc=cfg.min_liquidity_usdc,
        kill_switch=enabled,
    )
    updated.save_json_file(path)


def _ratio_pct(numerator: int | float | None, denominator: int | float | None) -> float:
    try:
        n = float(numerator or 0.0)
        d = float(denominator or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if d <= 0:
        return 0.0
    pct = (n / d) * 100.0
    return max(0.0, min(100.0, round(pct, 2)))


def _clamp_pct(value: float | None) -> float:
    if value is None:
        return 0.0
    return max(0.0, min(100.0, round(float(value), 2)))


def _build_trend_chart_rows(trends: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    rows_throughput: list[dict[str, Any]] = []
    rows_quality: list[dict[str, Any]] = []

    for label in ("7d", "30d"):
        trend = trends.get(label, {})
        total = int(trend.get("total_signals") or 0)
        traded = int(trend.get("signals_traded") or 0)

        resolved_accuracy = trend.get("resolved_accuracy")
        brier = trend.get("brier_score")

        accuracy_pct = _clamp_pct((float(resolved_accuracy) * 100.0) if resolved_accuracy is not None else None)
        brier_quality_pct = _clamp_pct((1.0 - float(brier)) * 100.0) if brier is not None else 0.0

        rows_throughput.append(
            {
                "window": label,
                "traded": traded,
                "total": total,
                "pct": _ratio_pct(traded, total),
            }
        )
        rows_quality.append(
            {
                "window": label,
                "accuracy_pct": accuracy_pct,
                "brier_quality_pct": brier_quality_pct,
                "resolved_accuracy": resolved_accuracy,
                "brier_score": brier,
            }
        )

    return {
        "throughput": rows_throughput,
        "quality": rows_quality,
    }


def _build_prelive_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    if report is None:
        return {
            "status": "unknown",
            "timestamp": None,
            "passed_count": 0,
            "failed_count": 0,
        }

    all_passed = bool(report.get("all_passed"))
    return {
        "status": "pass" if all_passed else "fail",
        "timestamp": report.get("timestamp"),
        "passed_count": int(report.get("passed_count") or 0),
        "failed_count": int(report.get("failed_count") or 0),
    }


def _build_replay_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    if report is None:
        return {
            "status": "none",
            "scenario": "n/a",
            "predictions_written": 0,
            "loops_written": 0,
        }
    return {
        "status": "ready",
        "scenario": str(report.get("scenario") or "default"),
        "predictions_written": int(report.get("predictions_written") or 0),
        "loops_written": int(report.get("loops_written") or 0),
    }


def _build_resolver_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    if report is None:
        return {
            "status": "none",
            "checked": 0,
            "resolved": 0,
            "applied": 0,
        }
    resolved = int(report.get("resolved") or 0)
    checked = int(report.get("checked") or 0)
    return {
        "status": "ready",
        "checked": checked,
        "resolved": resolved,
        "applied": int(report.get("applied") or 0),
    }


# --------------------------------------------------------------------------- #
# v0.8.2 dashboard builders
# --------------------------------------------------------------------------- #


def _warp_status(host: str = "gamma-api.polymarket.com") -> dict[str, Any]:
    """Quick DNS probe to the Polymarket Gamma host; WARP routes it when DNS is blocked."""
    try:
        socket.gethostbyname(host)
        return {"ok": True, "label": "WARP ON", "host": host}
    except OSError as exc:
        return {"ok": False, "label": "WARP OFF", "host": host, "error": str(exc)}


def _resolve_provider_and_model() -> dict[str, str]:
    provider = (os.getenv("ANALYST_PROVIDER", "") or "").strip().lower()
    if provider == "groq" or (not provider and (os.getenv("GROQ_API_KEY", "") or "").strip()):
        return {"provider": "groq", "model": os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")}
    if provider == "anthropic" or (os.getenv("ANTHROPIC_API_KEY", "") or "").strip():
        return {"provider": "anthropic", "model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")}
    if provider == "ollama" or (os.getenv("OLLAMA_BASE_URL", "") or "").strip():
        return {"provider": "ollama", "model": os.getenv("OLLAMA_MODEL", "qwen2.5:3b")}
    return {"provider": "stub", "model": "deterministic"}


def _format_uptime(seconds: float) -> str:
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {mins}m"
    if hours:
        return f"{hours}h {mins}m"
    if mins:
        return f"{mins}m {secs}s"
    return f"{secs}s"


def _paper_days_progress(analyses: list[dict[str, Any]]) -> dict[str, Any]:
    """Distinct UTC dates from analyses.jsonl → progress toward PAPER_DAYS_TARGET."""
    distinct: set[str] = set()
    for row in analyses:
        ts = _parse_iso(str(row.get("timestamp") or ""))
        if ts is None:
            continue
        distinct.add(ts.date().isoformat())

    days_done = len(distinct)
    pct = _clamp_pct((days_done / PAPER_DAYS_TARGET) * 100.0 if PAPER_DAYS_TARGET else 0.0)
    days_remaining = max(0, PAPER_DAYS_TARGET - days_done)
    est_completion = (datetime.now(timezone.utc).date() + timedelta(days=days_remaining)).isoformat()
    return {
        "days_done": days_done,
        "target": PAPER_DAYS_TARGET,
        "pct": pct,
        "days_remaining": days_remaining,
        "estimated_completion": est_completion,
        "distinct_dates": sorted(distinct),
    }


def _polymarket_url(market_id: str) -> str:
    mid = (market_id or "").strip()
    if not mid:
        return ""
    return f"https://polymarket.com/event/{mid}"


def _todays_analyses(analyses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    today = datetime.now(timezone.utc).date()
    out: list[dict[str, Any]] = []
    for row in analyses:
        ts = _parse_iso(str(row.get("timestamp") or ""))
        if ts is None or ts.date() != today:
            continue
        decision = str(row.get("decision") or "").upper()
        decision_class = "grey"
        if decision in {"YES", "BUY"}:
            decision_class = "green"
        elif decision in {"NO", "SELL"}:
            decision_class = "red"
        market_id = str(row.get("market_id") or "")
        reasoning = str(row.get("reasoning") or "").strip()
        if len(reasoning) > 200:
            reasoning = reasoning[:200]
        out.append(
            {
                "timestamp": row.get("timestamp"),
                "market_id": market_id,
                "polymarket_url": _polymarket_url(market_id),
                "decision": decision or "SKIP",
                "decision_class": decision_class,
                "confidence": str(row.get("confidence") or ""),
                "probability": row.get("probability"),
                "edge": row.get("edge"),
                "provider": str(row.get("provider") or ""),
                "reasoning": reasoning,
            }
        )
    out.sort(key=lambda r: str(r.get("timestamp") or ""), reverse=True)
    return out


def _seven_day_trends(analyses: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a 7-day rolling window of analysis counts + avg confidence."""
    today = datetime.now(timezone.utc).date()
    buckets: dict[str, dict[str, Any]] = {}
    for i in range(6, -1, -1):
        day = (today - timedelta(days=i)).isoformat()
        buckets[day] = {"date": day, "count": 0, "buy": 0, "sell": 0, "skip": 0, "conf_sum": 0.0, "conf_n": 0}

    conf_score = {"high": 1.0, "medium": 0.5, "low": 0.0}
    for row in analyses:
        ts = _parse_iso(str(row.get("timestamp") or ""))
        if ts is None:
            continue
        key = ts.date().isoformat()
        if key not in buckets:
            continue
        b = buckets[key]
        b["count"] += 1
        d = str(row.get("decision") or "").upper()
        if d in {"BUY", "YES"}:
            b["buy"] += 1
        elif d in {"SELL", "NO"}:
            b["sell"] += 1
        else:
            b["skip"] += 1
        c = str(row.get("confidence") or "").strip().lower()
        if c in conf_score:
            b["conf_sum"] += conf_score[c]
            b["conf_n"] += 1

    rows: list[dict[str, Any]] = []
    max_count = max((b["count"] for b in buckets.values()), default=0)
    for day in sorted(buckets.keys()):
        b = buckets[day]
        avg_conf = (b["conf_sum"] / b["conf_n"]) if b["conf_n"] else None
        if avg_conf is None:
            avg_label = "—"
        elif avg_conf >= 0.67:
            avg_label = "High"
        elif avg_conf >= 0.34:
            avg_label = "Medium"
        else:
            avg_label = "Low"
        pct = int(round((b["count"] / max_count) * 100.0)) if max_count else 0
        rows.append(
            {
                "date": day[5:],  # MM-DD
                "count": b["count"],
                "buy": b["buy"],
                "sell": b["sell"],
                "skip": b["skip"],
                "avg_confidence": avg_label,
                "bar_pct": pct,
            }
        )

    total_7d = sum(r["count"] for r in rows)
    return {"rows": rows, "total_7d": total_7d, "max_count": max_count}


def _classify_rejection_reason(reason: str) -> str:
    lower = (reason or "").lower()
    if "kill" in lower:
        return "danger"
    if "confidence" in lower:
        return "warning"
    if "edge" in lower or "insufficient" in lower:
        return "info"
    return "neutral"


def _build_rejection_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        reason = str(row.get("reason") or "")
        market_id = str(row.get("market_id") or "")
        breakdown_raw = row.get("edge_breakdown") or {}
        breakdown: dict[str, Any] = {}
        if isinstance(breakdown_raw, dict):
            for key in ("kelly", "vol_weight", "time_decay", "final_edge", "raw_edge"):
                if key in breakdown_raw:
                    breakdown[key] = breakdown_raw[key]
        out.append(
            {
                "timestamp": str(row.get("timestamp") or ""),
                "market_id": market_id,
                "polymarket_url": _polymarket_url(market_id),
                "reason": reason,
                "class": _classify_rejection_reason(reason),
                "breakdown": breakdown,
            }
        )
    return out


def _top_rejection_reasons(risk_rows_all: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for row in risk_rows_all:
        reason = str(row.get("reason") or "").strip()
        if reason:
            counter[reason] += 1
    return [{"reason": r, "count": c} for r, c in counter.most_common(limit)]


def _decision_breakdown(analyses: list[dict[str, Any]]) -> dict[str, Any]:
    counter: Counter[str] = Counter()
    for row in analyses:
        decision = str(row.get("decision") or "").upper() or "SKIP"
        if decision in {"BUY", "YES"}:
            counter["BUY"] += 1
        elif decision in {"SELL", "NO"}:
            counter["SELL"] += 1
        else:
            counter["SKIP"] += 1
    total = sum(counter.values())
    rows = []
    for label in ("SKIP", "BUY", "SELL"):
        count = counter.get(label, 0)
        rows.append(
            {
                "label": label,
                "count": count,
                "pct": _clamp_pct((count / total * 100.0) if total else 0.0),
            }
        )
    return {"total": total, "rows": rows}


_CHECK_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Network",
        (
            "env_polygon_rpc_responsive",
            "access_clob_markets",
            "access_gamma_markets",
            "access_websocket",
        ),
    ),
    (
        "Analyst",
        (
            "analyst_provider_resolved",
            "news_feed_has_sources",
        ),
    ),
    (
        "Paper Progress",
        (
            "paper_gates_passed",
            "paper_minimum_14_days",
            "paper_loop_has_run_today",
        ),
    ),
    (
        "Wallet / USDC",
        (
            "env_polymarket_private_key_present",
            "env_polymarket_funder_address_present",
            "env_signature_type_present",
            "balance_usdc_gt_20",
            "open_orders_empty",
        ),
    ),
    (
        "Safety",
        (
            "env_dry_run_false",
            "risk_kill_switch_false",
            "risk_daily_loss_cap_range",
            "risk_max_position_pct",
            "risk_kelly_fraction",
        ),
    ),
)


def _group_checks_into_sections(checks: list[Any]) -> list[dict[str, Any]]:
    by_name: dict[str, Any] = {getattr(c, "name", ""): c for c in checks}
    claimed: set[str] = set()
    grouped: list[dict[str, Any]] = []

    for section_name, names in _CHECK_SECTIONS:
        checks_list: list[dict[str, Any]] = []
        for n in names:
            c = by_name.get(n)
            if c is None:
                continue
            claimed.add(n)
            checks_list.append({"name": c.name, "passed": bool(c.passed), "detail": c.detail})
        if checks_list:
            grouped.append({"section": section_name, "checks": checks_list})

    leftover = [
        {"name": getattr(c, "name", ""), "passed": bool(getattr(c, "passed", False)), "detail": getattr(c, "detail", "")}
        for c in checks
        if getattr(c, "name", "") and getattr(c, "name", "") not in claimed
    ]
    if leftover:
        grouped.append({"section": "Other", "checks": leftover})
    return grouped


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #


DASHBOARD_HTML = """
<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Polymarket Bot — Control Room</title>
  <style>
    :root {
      --bg: #0f1117;
      --panel: #1a1d27;
      --panel-2: #222634;
      --ink: #e5e7eb;
      --muted: #9ca3af;
      --accent: #6366f1;
      --accent-dim: #4338ca;
      --good: #22c55e;
      --warn: #f59e0b;
      --bad: #ef4444;
      --info: #38bdf8;
      --line: #2a2f3d;
      --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: system-ui, -apple-system, \"Segoe UI\", Roboto, sans-serif;
      font-size: 14px;
      line-height: 1.45;
    }
    .wrap { max-width: 1280px; margin: 0 auto; padding: 16px; }
    h1 { font-size: 20px; margin: 0 0 4px; letter-spacing: 0.2px; }
    h2 { font-size: 14px; margin: 0 0 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.8px; font-weight: 600; }
    h3 { font-size: 13px; margin: 0 0 8px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.6px; font-weight: 600; }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    .mono, code { font-family: var(--mono); font-size: 13px; }
    .status-bar {
      display: flex; gap: 10px; flex-wrap: wrap; align-items: center;
      background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
      padding: 10px 14px; margin-bottom: 16px;
    }
    .pill {
      display: inline-flex; align-items: center; gap: 6px;
      padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 600;
      background: var(--panel-2); color: var(--ink); border: 1px solid var(--line);
    }
    .pill.good { background: rgba(34, 197, 94, 0.12); color: var(--good); border-color: rgba(34, 197, 94, 0.35); }
    .pill.warn { background: rgba(245, 158, 11, 0.12); color: var(--warn); border-color: rgba(245, 158, 11, 0.35); }
    .pill.bad  { background: rgba(239, 68, 68, 0.12); color: var(--bad);  border-color: rgba(239, 68, 68, 0.35); }
    .pill.info { background: rgba(99, 102, 241, 0.14); color: #a5b4fc; border-color: rgba(99, 102, 241, 0.35); }
    .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; background: currentColor; }
    .spacer { flex: 1; }
    .grid { display: grid; gap: 14px; }
    .grid.cols-2 { grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); }
    .grid.cols-3 { grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }
    .panel {
      background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
      padding: 14px; min-width: 0;
    }
    .panel .head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 10px; }
    .panel .head h2 { margin: 0; }
    .panel .head .sub { color: var(--muted); font-size: 12px; }
    .bar-track { background: var(--panel-2); border-radius: 999px; height: 10px; overflow: hidden; margin: 8px 0; }
    .bar-fill { height: 10px; background: linear-gradient(90deg, var(--accent-dim), var(--accent)); border-radius: 999px; }
    .kv { display: flex; justify-content: space-between; gap: 10px; font-size: 13px; padding: 2px 0; }
    .kv .k { color: var(--muted); }
    .kv .v { font-family: var(--mono); }
    .cards { display: flex; flex-direction: column; gap: 8px; max-height: 360px; overflow: auto; }
    .card-row {
      background: var(--panel-2); border: 1px solid var(--line); border-radius: 8px;
      padding: 8px 10px; display: grid; grid-template-columns: 1fr auto; gap: 4px; align-items: center;
    }
    .card-row .left .market { font-family: var(--mono); font-size: 13px; color: var(--ink); }
    .card-row .left .market a { color: #a5b4fc; }
    .card-row .left .market a:hover { color: var(--accent); }
    .card-row .left .meta { color: var(--muted); font-size: 12px; margin-top: 2px; }
    .card-row .left .reasoning { color: #cbd5e1; font-size: 12px; margin-top: 4px; font-style: italic; line-height: 1.35; }
    .risk-row .mkt a { color: #c7d2fe; }
    .risk-row .mkt a:hover { color: var(--accent); }
    .risk-row .breakdown { color: var(--muted); font-family: var(--mono); font-size: 11px; margin-top: 2px; }
    .trend-row { display: grid; grid-template-columns: 60px 1fr 80px 60px; gap: 8px; align-items: center; padding: 4px 0; font-size: 12.5px; border-bottom: 1px solid var(--line); }
    .trend-row:last-child { border-bottom: 0; }
    .trend-row .date { font-family: var(--mono); color: var(--muted); }
    .trend-row .bar-cell { background: var(--panel-2); height: 14px; border-radius: 4px; overflow: hidden; }
    .trend-row .bar-cell .fill { background: linear-gradient(90deg, var(--accent-dim), var(--accent)); height: 14px; }
    .trend-row .conf { font-family: var(--mono); font-size: 11px; color: var(--info); }
    .trend-row .count { text-align: right; font-family: var(--mono); color: var(--ink); }
    .badge {
      display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 700;
      font-family: var(--mono); letter-spacing: 0.4px;
    }
    .badge.grey  { background: #2d3443; color: #cbd5e1; }
    .badge.green { background: rgba(34, 197, 94, 0.18); color: var(--good); }
    .badge.red   { background: rgba(239, 68, 68, 0.18); color: var(--bad); }
    .check-item {
      display: grid; grid-template-columns: 16px 1fr auto; gap: 8px; align-items: start;
      padding: 4px 0; font-size: 13px; border-bottom: 1px solid transparent;
    }
    .check-item .sym { font-weight: 700; }
    .check-item.pass .sym { color: var(--good); }
    .check-item.fail .sym { color: var(--bad); }
    .check-item .detail { color: var(--muted); font-family: var(--mono); font-size: 11.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .section-title {
      font-size: 11px; text-transform: uppercase; letter-spacing: 0.8px; color: var(--muted);
      margin: 10px 0 4px; padding-top: 6px; border-top: 1px solid var(--line);
    }
    .section-title:first-of-type { border-top: 0; padding-top: 0; margin-top: 0; }
    .risk-row {
      display: grid; grid-template-columns: auto 1fr auto; gap: 8px; align-items: center;
      padding: 6px 0; border-bottom: 1px solid var(--line); font-size: 12.5px;
    }
    .risk-row:last-child { border-bottom: 0; }
    .risk-row .ts { color: var(--muted); font-family: var(--mono); font-size: 11px; }
    .risk-row .mkt { font-family: var(--mono); font-size: 12px; color: #c7d2fe; }
    .metric-bar { display: grid; grid-template-columns: 60px 1fr 60px; gap: 8px; align-items: center; margin: 6px 0; font-size: 12px; }
    .metric-bar .label { color: var(--muted); font-family: var(--mono); }
    .metric-bar .track { background: var(--panel-2); height: 14px; border-radius: 4px; overflow: hidden; }
    .metric-bar .fill.skip  { background: #475569; height: 14px; }
    .metric-bar .fill.buy   { background: var(--good); height: 14px; }
    .metric-bar .fill.sell  { background: var(--bad);  height: 14px; }
    .metric-bar .val { text-align: right; font-family: var(--mono); color: var(--ink); }
    .rej-table { width: 100%; font-size: 12.5px; }
    .rej-table td { padding: 4px 6px; border-bottom: 1px solid var(--line); }
    .rej-table td.count { text-align: right; font-family: var(--mono); color: var(--warn); }
    .rej-table tr:last-child td { border-bottom: 0; }
    .flash { background: rgba(99, 102, 241, 0.12); border: 1px solid var(--accent); color: var(--ink); border-radius: 8px; padding: 8px 10px; margin-bottom: 10px; font-size: 13px; }
    .toolbar { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
    .toolbar form { margin: 0; }
    .btn {
      border: 1px solid var(--line); background: var(--panel-2); color: var(--ink);
      padding: 6px 12px; border-radius: 8px; font-size: 12px; cursor: pointer; font-weight: 600;
    }
    .btn:hover { background: #2d3243; border-color: var(--accent); }
    .btn.danger { background: rgba(239, 68, 68, 0.18); color: var(--bad); border-color: rgba(239, 68, 68, 0.35); }
    .btn.good   { background: rgba(34, 197, 94, 0.14); color: var(--good); border-color: rgba(34, 197, 94, 0.35); }
    .empty { color: var(--muted); font-size: 12.5px; font-style: italic; padding: 8px 0; }
    .footer-links { margin-top: 16px; font-size: 12px; color: var(--muted); }
    .footer-links a { margin-right: 12px; }
  </style>
</head>
<body>
  <div class=\"wrap\">
    <!-- Panel 1: Status Bar -->
    <div class=\"status-bar\">
      <span class=\"pill info\"><span class=\"dot\"></span>{{ status.provider }} · {{ status.model }}</span>
      {% if status.warp.ok %}
        <span class=\"pill good\"><span class=\"dot\"></span>{{ status.warp.label }}</span>
      {% else %}
        <span class=\"pill bad\"><span class=\"dot\"></span>{{ status.warp.label }}</span>
      {% endif %}
      {% if status.paper_mode %}
        <span class=\"pill warn\"><span class=\"dot\"></span>PAPER MODE</span>
      {% else %}
        <span class=\"pill bad\"><span class=\"dot\"></span>LIVE MODE</span>
      {% endif %}
      <span class=\"pill\">uptime {{ status.uptime }}</span>
      <span class=\"spacer\"></span>
      <span class=\"pill\">checklist {{ status.checklist_pass_count }}/{{ status.checklist_total }}</span>
    </div>

    <h1>Polymarket Bot — Control Room</h1>
    <div class=\"mono\" style=\"color: var(--muted); margin-bottom: 14px;\">{{ status.now_utc }}</div>

    {% for msg in flashes %}
      <div class=\"flash\">{{ msg }}</div>
    {% endfor %}

    <div class=\"grid cols-2\">
      <!-- Panel 2: Paper Progress -->
      <div class=\"panel\">
        <div class=\"head\"><h2>Paper Progress</h2><span class=\"sub\">phase gate tracker</span></div>
        <div class=\"kv\"><span class=\"k\">Distinct paper days</span><span class=\"v\">{{ paper_days.days_done }} / {{ paper_days.target }}</span></div>
        <div class=\"bar-track\"><div class=\"bar-fill\" style=\"width: {{ paper_days.pct }}%\"></div></div>
        <div class=\"kv\"><span class=\"k\">Days remaining</span><span class=\"v\">{{ paper_days.days_remaining }}</span></div>
        <div class=\"kv\"><span class=\"k\">Estimated completion</span><span class=\"v\">{{ paper_days.estimated_completion }}</span></div>
      </div>

      <!-- Panel 6: Performance Metrics -->
      <div class=\"panel\">
        <div class=\"head\"><h2>Performance</h2><span class=\"sub\">analysis + risk stats</span></div>
        <div class=\"kv\"><span class=\"k\">Brier score</span><span class=\"v\">{{ perf.brier_score if perf.brier_score is not none else '—' }}</span></div>
        <div class=\"kv\"><span class=\"k\">Total analyses</span><span class=\"v\">{{ perf.decision_breakdown.total }}</span></div>
        {% for row in perf.decision_breakdown.rows %}
          <div class=\"metric-bar\">
            <span class=\"label\">{{ row.label }}</span>
            <span class=\"track\"><span class=\"fill {{ row.label|lower }}\" style=\"width: {{ row.pct }}%\"></span></span>
            <span class=\"val\">{{ row.count }}</span>
          </div>
        {% endfor %}
        <h3 style=\"margin-top: 14px;\">Top rejection reasons</h3>
        {% if perf.top_rejections %}
          <table class=\"rej-table\">
            {% for r in perf.top_rejections %}
              <tr><td>{{ r.reason }}</td><td class=\"count\">{{ r.count }}</td></tr>
            {% endfor %}
          </table>
        {% else %}
          <div class=\"empty\">no rejections yet</div>
        {% endif %}
      </div>
    </div>

    <div class=\"grid cols-2\" style=\"margin-top: 14px;\">
      <!-- Panel 3: Today's Analysis Feed -->
      <div class=\"panel\">
        <meta http-equiv=\"refresh\" content=\"30\" />
        <div class=\"head\"><h2>Today's Analysis Feed</h2><span class=\"sub\">auto-refresh 30s</span></div>
        <div class=\"cards\">
          {% if todays_analyses %}
            {% for a in todays_analyses %}
              <div class=\"card-row\">
                <div class=\"left\">
                  <div class=\"market\">
                    {% if a.polymarket_url %}
                      <a href=\"{{ a.polymarket_url }}\" target=\"_blank\" rel=\"noopener noreferrer\">{{ a.market_id }}</a>
                    {% else %}
                      {{ a.market_id }}
                    {% endif %}
                  </div>
                  <div class=\"meta\">
                    conf={{ a.confidence }} · p={{ a.probability }} · edge={{ a.edge }} · provider={{ a.provider }}
                  </div>
                  {% if a.reasoning %}
                    <div class=\"reasoning\">“{{ a.reasoning }}”</div>
                  {% endif %}
                </div>
                <span class=\"badge {{ a.decision_class }}\">{{ a.decision }}</span>
              </div>
            {% endfor %}
          {% else %}
            <div class=\"empty\">No analyses yet today — scheduler runs at 08:00 UTC</div>
          {% endif %}
        </div>
      </div>

      <!-- Panel 4: Risk Log -->
      <div class=\"panel\">
        <div class=\"head\"><h2>Risk Log</h2><span class=\"sub\">last 20 rejections</span></div>
        <div class=\"cards\">
          {% if risk_rejections %}
            {% for r in risk_rejections %}
              <div class=\"risk-row\">
                <span class=\"ts\">{{ r.timestamp[:19] }}</span>
                <span class=\"mkt\">
                  {% if r.polymarket_url %}
                    <a href=\"{{ r.polymarket_url }}\" target=\"_blank\" rel=\"noopener noreferrer\">{{ r.market_id }}</a>
                  {% else %}
                    {{ r.market_id }}
                  {% endif %}
                  {% if r.breakdown %}
                    <div class=\"breakdown\">
                      {% if r.breakdown.kelly is defined %}kelly={{ r.breakdown.kelly }}{% endif %}
                      {% if r.breakdown.vol_weight is defined %} · vol×{{ r.breakdown.vol_weight }}{% endif %}
                      {% if r.breakdown.time_decay is defined %} · time×{{ r.breakdown.time_decay }}{% endif %}
                      {% if r.breakdown.final_edge is defined %} · final={{ r.breakdown.final_edge }}{% endif %}
                    </div>
                  {% endif %}
                </span>
                <span class=\"badge {% if r.class == 'danger' %}red{% elif r.class == 'warning' %}grey{% elif r.class == 'info' %}grey{% else %}grey{% endif %}\"
                      style=\"background: {% if r.class == 'danger' %}rgba(239,68,68,0.18); color: var(--bad);{% elif r.class == 'warning' %}rgba(245,158,11,0.18); color: var(--warn);{% elif r.class == 'info' %}rgba(56,189,248,0.18); color: var(--info);{% else %}#2d3443; color: #cbd5e1;{% endif %}\">{{ r.reason }}</span>
              </div>
            {% endfor %}
          {% else %}
            <div class=\"empty\">no recent rejections</div>
          {% endif %}
        </div>
      </div>
    </div>

    <!-- Panel 7: 7-day Trends -->
    <div class=\"panel\" style=\"margin-top: 14px;\">
      <div class=\"head\"><h2>7-day Trends</h2><span class=\"sub\">total analyses: {{ trends_7d.total_7d }}</span></div>
      {% if trends_7d.max_count %}
        {% for t in trends_7d.rows %}
          <div class=\"trend-row\">
            <span class=\"date\">{{ t.date }}</span>
            <span class=\"bar-cell\"><span class=\"fill\" style=\"width: {{ t.bar_pct }}%\"></span></span>
            <span class=\"conf\">conf {{ t.avg_confidence }}</span>
            <span class=\"count\">{{ t.count }}</span>
          </div>
        {% endfor %}
      {% else %}
        <div class=\"empty\">no analyses in last 7 days</div>
      {% endif %}
    </div>

    <!-- Panel 5: Prelive Checklist -->
    <div class=\"panel\" style=\"margin-top: 14px;\">
      <div class=\"head\"><h2>Pre-live Checklist</h2><span class=\"sub\">live gate status ({{ status.checklist_pass_count }}/{{ status.checklist_total }} PASS)</span></div>
      {% for section in checklist_grouped %}
        <div class=\"section-title\">{{ section.section }}</div>
        {% for item in section.checks %}
          <div class=\"check-item {% if item.passed %}pass{% else %}fail{% endif %}\">
            <span class=\"sym\">{% if item.passed %}✓{% else %}✗{% endif %}</span>
            <span>{{ item.name }}</span>
            <span class=\"detail\" title=\"{{ item.detail }}\">{{ item.detail }}</span>
          </div>
        {% endfor %}
      {% endfor %}
    </div>

    <div class=\"toolbar\">
      <form method=\"post\" action=\"{{ url_for('action_scan') }}\"><button class=\"btn\">Run Scan</button></form>
      <form method=\"post\" action=\"{{ url_for('action_preflight') }}\"><button class=\"btn\">Preflight</button></form>
      <form method=\"post\" action=\"{{ url_for('action_prelive_checklist') }}\"><button class=\"btn\">Re-run Checklist</button></form>
      <form method=\"post\" action=\"{{ url_for('action_usdc_check') }}\"><button class=\"btn\">USDC Check</button></form>
      <form method=\"post\" action=\"{{ url_for('action_send_alerts') }}\"><button class=\"btn\">Send Alerts</button></form>
      <form method=\"post\" action=\"{{ url_for('action_kill_switch') }}\"><input type=\"hidden\" name=\"value\" value=\"true\" /><button class=\"btn danger\">Kill Switch ON</button></form>
      <form method=\"post\" action=\"{{ url_for('action_kill_switch') }}\"><input type=\"hidden\" name=\"value\" value=\"false\" /><button class=\"btn good\">Kill Switch OFF</button></form>
    </div>

    <div class=\"footer-links\">
      <a href=\"{{ url_for('trades_view') }}\">Trade Lifecycle</a>
      <a href=\"{{ url_for('api_status') }}\">/api/status</a>
      <a href=\"{{ url_for('api_state') }}\">/api/state</a>
      <a href=\"{{ url_for('api_prelive_report') }}\">/api/prelive-report</a>
      <a href=\"{{ url_for('api_trends') }}\">/api/trends</a>
    </div>
  </div>
</body>
</html>
"""


TRADE_LIFECYCLE_HTML = """
<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Trade Lifecycle</title>
  <style>
    body { margin: 0; background: #0f1117; color: #e5e7eb; font-family: system-ui, -apple-system, \"Segoe UI\", Roboto, sans-serif; font-size: 14px; }
    .wrap { max-width: 1100px; margin: 0 auto; padding: 16px; }
    .panel { background: #1a1d27; border: 1px solid #2a2f3d; border-radius: 12px; padding: 14px; }
    table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
    th, td { text-align: left; padding: 8px; border-bottom: 1px solid #2a2f3d; }
    th { color: #9ca3af; text-transform: uppercase; font-size: 11px; letter-spacing: 0.6px; }
    a { color: #6366f1; text-decoration: none; }
  </style>
</head>
<body>
  <div class=\"wrap\">
    <h1 style=\"margin:0 0 8px;font-size:20px;\">Trade Lifecycle</h1>
    <div style=\"margin-bottom:14px;\"><a href=\"{{ url_for('home') }}\">← Back to Dashboard</a></div>
    <div class=\"panel\">
      <table>
        <thead>
          <tr>
            <th>Order ID</th>
            <th>Market</th>
            <th>Side</th>
            <th>Size (USDC)</th>
            <th>Latest Status</th>
            <th>Status Path</th>
            <th>Events</th>
            <th>Duration (s)</th>
            <th>Last Timestamp</th>
          </tr>
        </thead>
        <tbody>
          {% for row in lifecycle %}
          <tr>
            <td>{{ row.order_id }}</td>
            <td>{{ row.market_id }}</td>
            <td>{{ row.side }}</td>
            <td>{{ row.size_usdc }}</td>
            <td>{{ row.latest_status }}</td>
            <td>{{ row.status_path }}</td>
            <td>{{ row.events }}</td>
            <td>{{ row.duration_seconds }}</td>
            <td>{{ row.last_timestamp }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
</body>
</html>
"""


def create_dashboard_app(workspace_root: Path) -> Flask:
    app = Flask(__name__)
    app.secret_key = "polymarket-dashboard-local"

    def _build_status(checks: list[Any]) -> dict[str, Any]:
        provider_model = _resolve_provider_and_model()
        warp = _warp_status()
        live_mode = (os.getenv("BOT_LIVE_MODE", "false") or "false").strip().lower() in {"1", "true", "yes", "on"}
        pass_count = sum(1 for c in checks if getattr(c, "passed", False))
        return {
            "provider": provider_model["provider"],
            "model": provider_model["model"],
            "warp": warp,
            "paper_mode": not live_mode,
            "uptime": _format_uptime(time.time() - _PROCESS_START_TS),
            "now_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "checklist_pass_count": pass_count,
            "checklist_total": len(checks),
        }

    def _home_state() -> dict[str, Any]:
        config = load_config()

        analyses_all = _read_jsonl_all(workspace_root / "data" / "analyses.jsonl")
        risk_all = _read_jsonl_all(workspace_root / "data" / "risk_log.jsonl")
        recent_risk = _read_jsonl(workspace_root / "data" / "risk_log.jsonl", limit=20)

        checks = collect_pre_live_checks(workspace_root=workspace_root, db_path=config.storage.db_path)
        status = _build_status(checks)
        checklist_grouped = _group_checks_into_sections(checks)

        brier_score = None
        try:
            store = PredictionStore(config.storage.db_path)
            metrics = store.brier_metrics()
            brier_score = metrics.brier_score
        except Exception:  # noqa: BLE001
            brier_score = None

        perf = {
            "brier_score": brier_score,
            "decision_breakdown": _decision_breakdown(analyses_all),
            "top_rejections": _top_rejection_reasons(risk_all, limit=3),
        }

        return {
            "status": status,
            "paper_days": _paper_days_progress(analyses_all),
            "todays_analyses": _todays_analyses(analyses_all),
            "risk_rejections": _build_rejection_rows(recent_risk),
            "checklist_grouped": checklist_grouped,
            "perf": perf,
            "trends_7d": _seven_day_trends(analyses_all),
        }

    def _legacy_state() -> dict[str, Any]:
        """Retained for /api/state back-compat — preserves prior shape."""
        config = load_config()
        http = HttpClient(
            timeout_seconds=config.runtime.request_timeout_seconds,
            user_agent=config.runtime.user_agent,
        )
        polymarket = PolymarketClient(http) if config.venue.enable_polymarket else None
        preflight = run_preflight(config=config, polymarket=polymarket)

        summary = daily_summary(workspace_root=workspace_root, db_path=config.storage.db_path)
        ready, failures = check_paper_gates(workspace_root=workspace_root, db_path=config.storage.db_path)
        usdc_report: USDCCheckReport = run_usdc_operational_checks(workspace_root)
        usdc_failed = [c.name for c in usdc_report.checks if c.severity == "critical" and not c.passed]
        telemetry = build_telemetry_snapshot(workspace_root)
        trends = build_trend_snapshot(workspace_root=workspace_root, db_path=config.storage.db_path)
        trend_charts = _build_trend_chart_rows(trends)

        total_signals = int(summary.get("total_signals", 0))
        rejected = int(summary.get("signals_rejected", 0))
        rejection_rate = (rejected / total_signals) if total_signals > 0 else None
        alerts = build_alerts(
            telemetry=telemetry,
            preflight_blocked=bool(preflight.blocked),
            usdc_ready=usdc_report.ready,
            rejection_rate=rejection_rate,
            api_cost_usd=float(summary.get("api_cost_usd", 0.0)),
        )

        store = PredictionStore(config.storage.db_path)
        predictions = [dict(row) for row in store.recent_predictions(limit=20)]

        trades = _read_jsonl(workspace_root / "data" / "trades.jsonl", limit=20)
        loop_logs = _read_jsonl(workspace_root / "data" / "loop_log.jsonl", limit=20)
        risk = asdict(_load_risk_config(workspace_root / "risk_config.json"))
        alert_state = read_alert_state(workspace_root)
        prelive_report = read_pre_live_report(workspace_root)
        prelive_summary = _build_prelive_summary(prelive_report)
        replay_report = read_synthetic_replay_report(workspace_root)
        replay_summary = _build_replay_summary(replay_report)
        resolver_report = read_resolution_report(workspace_root)
        resolver_summary = _build_resolver_summary(resolver_report)
        lifecycle = _build_trade_lifecycle(_read_jsonl(workspace_root / "data" / "trades.jsonl", limit=500))

        return {
            "preflight": preflight,
            "summary": summary,
            "ready_for_live": ready,
            "gate_failures": failures,
            "predictions": predictions,
            "trades": trades,
            "loop_logs": loop_logs,
            "risk": risk,
            "usdc": asdict(usdc_report),
            "usdc_failed": usdc_failed,
            "telemetry": telemetry,
            "alerts": alerts,
            "alert_state": alert_state,
            "prelive_report": prelive_report,
            "prelive_summary": prelive_summary,
            "replay_report": replay_report,
            "replay_summary": replay_summary,
            "resolver_report": resolver_report,
            "resolver_summary": resolver_summary,
            "lifecycle": lifecycle,
            "trends": trends,
            "trend_charts": trend_charts,
        }

    @app.get("/")
    def home() -> str:
        state = _home_state()
        return render_template_string(
            DASHBOARD_HTML,
            **state,
            flashes=get_flashed_messages(),
        )

    @app.get("/api/status")
    def api_status():
        config = load_config()
        analyses_all = _read_jsonl_all(workspace_root / "data" / "analyses.jsonl")
        checks = collect_pre_live_checks(workspace_root=workspace_root, db_path=config.storage.db_path)
        provider_model = _resolve_provider_and_model()
        warp = _warp_status()
        paper_days = _paper_days_progress(analyses_all)
        pass_count = sum(1 for c in checks if getattr(c, "passed", False))
        live_mode = (os.getenv("BOT_LIVE_MODE", "false") or "false").strip().lower() in {"1", "true", "yes", "on"}
        return jsonify(
            {
                "provider": provider_model["provider"],
                "model": provider_model["model"],
                "warp_status": "on" if warp["ok"] else "off",
                "warp_host": warp["host"],
                "paper_mode": not live_mode,
                "paper_days_done": paper_days["days_done"],
                "paper_days_target": paper_days["target"],
                "paper_days_pct": paper_days["pct"],
                "checklist_pass_count": pass_count,
                "checklist_total": len(checks),
                "uptime_seconds": round(time.time() - _PROCESS_START_TS, 2),
                "uptime": _format_uptime(time.time() - _PROCESS_START_TS),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

    @app.get("/api/state")
    def api_state():
        state = _legacy_state()
        state["preflight"] = asdict(state["preflight"])
        return jsonify(state)

    @app.get("/api/trades-lifecycle")
    def api_trades_lifecycle():
        lifecycle = _build_trade_lifecycle(_read_jsonl(workspace_root / "data" / "trades.jsonl", limit=2000))
        return jsonify({"count": len(lifecycle), "rows": lifecycle})

    @app.get("/api/trends")
    def api_trends():
        config = load_config()
        trends = build_trend_snapshot(workspace_root=workspace_root, db_path=config.storage.db_path)
        return jsonify(trends)

    @app.get("/api/prelive-report")
    def api_prelive_report():
        report = read_pre_live_report(workspace_root)
        return jsonify(report or {"status": "missing"})

    @app.get("/api/replay-report")
    def api_replay_report():
        report = read_synthetic_replay_report(workspace_root)
        return jsonify(report or {"status": "missing"})

    @app.get("/api/resolver-report")
    def api_resolver_report():
        report = read_resolution_report(workspace_root)
        return jsonify(report or {"status": "missing"})

    @app.get("/trades")
    def trades_view() -> str:
        lifecycle = _build_trade_lifecycle(_read_jsonl(workspace_root / "data" / "trades.jsonl", limit=2000))
        return render_template_string(TRADE_LIFECYCLE_HTML, lifecycle=lifecycle)

    @app.post("/action/scan")
    def action_scan():
        config = load_config()
        result = execute_scan_run(
            config=config,
            limit_per_venue=20,
            top_n_for_risk=5,
            workspace_root=workspace_root,
        )
        flash(f"scan complete candidates={len(result.candidates)} decisions={len(result.risk_decisions)}")
        return redirect(url_for("home"))

    @app.post("/action/preflight")
    def action_preflight():
        config = load_config()
        http = HttpClient(
            timeout_seconds=config.runtime.request_timeout_seconds,
            user_agent=config.runtime.user_agent,
        )
        polymarket = PolymarketClient(http) if config.venue.enable_polymarket else None
        status = run_preflight(config=config, polymarket=polymarket)
        flash(f"preflight {status.message} blocked={status.blocked}")
        return redirect(url_for("home"))

    @app.post("/action/scorecard")
    def action_scorecard():
        ready, failures = check_paper_gates(workspace_root=workspace_root, db_path=load_config().storage.db_path)
        flash(f"scorecard ready_for_live={ready} failures={','.join(failures) if failures else 'none'}")
        return redirect(url_for("home"))

    @app.post("/action/prelive-checklist")
    def action_prelive_checklist():
        config = load_config()
        ready, checks = run_pre_live_checklist(workspace_root=workspace_root, db_path=config.storage.db_path)
        write_pre_live_report(workspace_root=workspace_root, checks=checks, all_passed=ready)
        failed = [c.name for c in checks if not c.passed]
        flash(f"prelive ready={ready} failed_checks={','.join(failed) if failed else 'none'}")
        return redirect(url_for("home"))

    @app.post("/action/replay-synthetic")
    def action_replay_synthetic():
        config = load_config()
        scenario = (request.form.get("scenario") or "default").strip()
        if scenario not in {"default", "bull_trend", "chop", "event_shock"}:
            scenario = "default"

        def _int(name: str, default: int) -> int:
            raw = request.form.get(name)
            try:
                return max(1, int(str(raw)))
            except (TypeError, ValueError):
                return default

        def _float(name: str, default: float) -> float:
            raw = request.form.get(name)
            try:
                return float(str(raw))
            except (TypeError, ValueError):
                return default

        report = run_synthetic_replay(
            workspace_root=workspace_root,
            db_path=config.storage.db_path,
            days=_int("days", 3),
            loops_per_day=_int("loops_per_day", 6),
            candidates_per_loop=_int("candidates_per_loop", 5),
            approve_rate=_float("approve_rate", 0.45),
            resolved_rate=_float("resolved_rate", 0.55),
            scenario=scenario,
            seed=7,
            write_resolution_stub=True,
        )
        write_synthetic_replay_report(workspace_root, report)
        flash(
            f"replay scenario={report.scenario} predictions={report.predictions_written} "
            f"loops={report.loops_written} outcomes={report.outcomes_written}"
        )
        return redirect(url_for("home"))

    @app.post("/action/resolve-outcomes")
    def action_resolve_outcomes():
        config = load_config()
        http = HttpClient(
            timeout_seconds=config.runtime.request_timeout_seconds,
            user_agent=config.runtime.user_agent,
        )
        store = PredictionStore(config.storage.db_path)
        dry_run = (request.form.get("dry_run") or "true").strip().lower() in {"1", "true", "yes", "on"}
        stub_mode = (request.form.get("stub_mode") or "true").strip().lower() in {"1", "true", "yes", "on"}
        try:
            limit = max(1, int(str(request.form.get("limit") or "200")))
        except ValueError:
            limit = 200

        resolver = OutcomeResolver(
            workspace_root=workspace_root,
            http=http,
            dry_run=dry_run,
            stub_mode=stub_mode,
        )
        report = resolver.settle_unresolved_predictions(store=store, limit=limit)
        write_resolution_report(workspace_root=workspace_root, report=report)
        flash(
            f"resolver checked={report.checked} resolved={report.resolved} "
            f"applied={report.applied} dry_run={dry_run} stub_mode={stub_mode}"
        )
        return redirect(url_for("home"))

    @app.post("/action/kill-switch")
    def action_kill_switch():
        value = (request.form.get("value") or "false").strip().lower() in {"1", "true", "yes", "on"}
        _save_kill_switch(workspace_root / "risk_config.json", enabled=value)
        flash(f"kill_switch set to {value}")
        return redirect(url_for("home"))

    @app.post("/action/usdc-check")
    def action_usdc_check():
        report = run_usdc_operational_checks(workspace_root)
        failed = [c.name for c in report.checks if c.severity == "critical" and not c.passed]
        flash(f"usdc ready={report.ready} critical_failures={','.join(failed) if failed else 'none'}")
        return redirect(url_for("home"))

    @app.post("/action/send-alerts")
    def action_send_alerts():
        state = _legacy_state()
        result = dispatch_alerts(
            workspace_root=workspace_root,
            alerts=list(state.get("alerts", [])),
            source="dashboard_action",
            force=False,
        )
        flash(
            f"alerts sent={result.sent} skipped={result.skipped} reason={result.reason} "
            f"count={result.alerts_count}"
        )
        return redirect(url_for("home"))

    return app


def run_dashboard(host: str, port: int, workspace_root: Path) -> int:
    app = create_dashboard_app(workspace_root=workspace_root)
    try:
        app.run(host=host, port=port, debug=False)
    except OSError as exc:
        print(f"dashboard_start_failed={exc}")
        print(f"hint=port_{port}_may_be_in_use")
        return 1
    return 0
