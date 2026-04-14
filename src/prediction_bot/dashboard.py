from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, flash, get_flashed_messages, jsonify, redirect, render_template_string, request, url_for

from prediction_bot.alerting import dispatch_alerts, read_alert_state
from prediction_bot.checklist import read_pre_live_report, run_pre_live_checklist, write_pre_live_report
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


DASHBOARD_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Polymarket Bot Control Room</title>
  <style>
    :root {
      --bg: #f3efe6;
      --panel: #fffdf8;
      --ink: #1d2a35;
      --muted: #617282;
      --accent: #1f7a8c;
      --danger: #b03a2e;
      --good: #2e8b57;
      --line: #dccfb8;
    }
    body { margin: 0; background: radial-gradient(circle at top right, #efe2cc 0%, var(--bg) 60%); color: var(--ink); font-family: "Trebuchet MS", "Segoe UI", sans-serif; }
    .wrap { max-width: 1200px; margin: 0 auto; padding: 20px; }
    h1 { margin: 0 0 12px; letter-spacing: 0.5px; }
    .sub { color: var(--muted); margin-bottom: 16px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 12px; }
    .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 14px; box-shadow: 0 6px 18px rgba(0,0,0,0.05); }
    .k { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.7px; }
    .v { font-size: 24px; margin-top: 4px; }
    .row { display: flex; gap: 10px; flex-wrap: wrap; margin: 14px 0; }
    button { border: 0; border-radius: 999px; padding: 10px 14px; cursor: pointer; font-weight: 600; }
    .a { background: var(--accent); color: white; }
    .d { background: var(--danger); color: white; }
    .g { background: var(--good); color: white; }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align: left; padding: 8px; border-bottom: 1px solid var(--line); font-size: 13px; }
    .flash { background: #fff3d8; border: 1px solid #e6c985; border-radius: 10px; padding: 10px; margin-bottom: 10px; }
    code { background: #f4eee0; padding: 2px 6px; border-radius: 6px; }
    .chart-row { display: grid; grid-template-columns: 120px 1fr 56px; gap: 8px; align-items: center; margin-bottom: 8px; }
    .chart-label { color: var(--muted); font-size: 12px; }
    .chart-track { background: #efe4cf; border-radius: 999px; height: 12px; overflow: hidden; }
    .chart-fill { height: 12px; border-radius: 999px; }
    .chart-fill.throughput { background: linear-gradient(90deg, #2f9e44, #6cc24a); }
    .chart-fill.accuracy { background: linear-gradient(90deg, #1f7a8c, #4db8c8); }
    .chart-fill.brierq { background: linear-gradient(90deg, #b8860b, #f3c35a); }
    .chart-val { text-align: right; font-size: 12px; color: var(--ink); }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Polymarket Bot Control Room</h1>
    <div class="sub">Manage scans, risk switches, and paper-readiness checks from one place.</div>
    <div class="sub"><a href="{{ url_for('trades_view') }}">Trade Lifecycle Drilldown</a> | <a href="{{ url_for('api_state') }}">API State</a> | <a href="{{ url_for('api_trades_lifecycle') }}">API Trades Lifecycle</a> | <a href="{{ url_for('api_trends') }}">API Trends</a> | <a href="{{ url_for('api_prelive_report') }}">API Prelive Report</a> | <a href="{{ url_for('api_replay_report') }}">API Replay Report</a> | <a href="{{ url_for('api_resolver_report') }}">API Resolver Report</a></div>

    {% for msg in flashes %}
      <div class="flash">{{ msg }}</div>
    {% endfor %}

    <div class="grid">
      <div class="panel"><div class="k">Preflight</div><div class="v">{{ preflight.message }}</div><div>blocked={{ preflight.blocked }} country={{ preflight.country }}</div></div>
      <div class="panel"><div class="k">Signals Today</div><div class="v">{{ summary.total_signals }}</div><div>traded={{ summary.signals_traded }} rejected={{ summary.signals_rejected }}</div></div>
      <div class="panel"><div class="k">Brier Score</div><div class="v">{{ summary.brier_score }}</div><div>risk_cap_hits={{ summary.risk_cap_hits }}</div></div>
      <div class="panel"><div class="k">Paper Gates</div><div class="v">{{ ready_for_live }}</div><div>{{ gate_failures|join(', ') if gate_failures else 'all clear' }}</div></div>
      <div class="panel"><div class="k">Kill Switch</div><div class="v">{{ risk.kill_switch }}</div><div>min_edge={{ risk.min_edge }} max_position={{ risk.max_position_pct }}</div></div>
      <div class="panel"><div class="k">USDC Ops</div><div class="v">{{ usdc.ready }}</div><div>{{ usdc_failed|join(', ') if usdc_failed else 'all critical checks pass' }}</div></div>
      <div class="panel"><div class="k">Telemetry Alerts</div><div class="v">{{ alerts|length }}</div><div>{{ alerts|join(', ') if alerts else 'none' }}</div></div>
      <div class="panel"><div class="k">Last Alert Dispatch</div><div class="v">{{ alert_state.last_result if alert_state else 'none' }}</div><div>status={{ alert_state.last_status if alert_state else 'n/a' }}</div></div>
      <div class="panel"><div class="k">7d Trend</div><div class="v">{{ trends['7d'].signals_traded }}/{{ trends['7d'].total_signals }}</div><div>approval={{ trends['7d'].approval_rate }} brier={{ trends['7d'].brier_score }}</div></div>
      <div class="panel"><div class="k">30d Trend</div><div class="v">{{ trends['30d'].signals_traded }}/{{ trends['30d'].total_signals }}</div><div>approval={{ trends['30d'].approval_rate }} brier={{ trends['30d'].brier_score }}</div></div>
      <div class="panel"><div class="k">Prelive Checklist</div><div class="v">{{ prelive_summary.status }}</div><div>pass={{ prelive_summary.passed_count }} fail={{ prelive_summary.failed_count }}</div></div>
      <div class="panel"><div class="k">Synthetic Replay</div><div class="v">{{ replay_summary.status }}</div><div>scenario={{ replay_summary.scenario }} loops={{ replay_summary.loops_written }}</div></div>
      <div class="panel"><div class="k">Outcome Resolver</div><div class="v">{{ resolver_summary.status }}</div><div>checked={{ resolver_summary.checked }} resolved={{ resolver_summary.resolved }} applied={{ resolver_summary.applied }}</div></div>
    </div>

    <div class="grid">
      <div class="panel">
        <h3>Trend Chart: Throughput</h3>
        {% for row in trend_charts.throughput %}
          <div class="chart-row">
            <div class="chart-label">{{ row.window }} traded</div>
            <div class="chart-track"><div class="chart-fill throughput" style="width: {{ row.pct }}%"></div></div>
            <div class="chart-val">{{ row.traded }}/{{ row.total }}</div>
          </div>
        {% endfor %}
      </div>
      <div class="panel">
        <h3>Trend Chart: Quality</h3>
        {% for row in trend_charts.quality %}
          <div class="chart-row">
            <div class="chart-label">{{ row.window }} accuracy</div>
            <div class="chart-track"><div class="chart-fill accuracy" style="width: {{ row.accuracy_pct }}%"></div></div>
            <div class="chart-val">{{ row.accuracy_pct }}%</div>
          </div>
          <div class="chart-row">
            <div class="chart-label">{{ row.window }} brier quality</div>
            <div class="chart-track"><div class="chart-fill brierq" style="width: {{ row.brier_quality_pct }}%"></div></div>
            <div class="chart-val">{{ row.brier_quality_pct }}%</div>
          </div>
        {% endfor %}
      </div>
    </div>

    <div class="row">
      <form method="post" action="{{ url_for('action_scan') }}"><button class="a">Run Scan</button></form>
      <form method="post" action="{{ url_for('action_preflight') }}"><button class="a">Run Preflight</button></form>
      <form method="post" action="{{ url_for('action_scorecard') }}"><button class="a">Refresh Scorecard</button></form>
      <form method="post" action="{{ url_for('action_prelive_checklist') }}"><button class="a">Run Prelive Checklist</button></form>
      <form method="post" action="{{ url_for('action_replay_synthetic') }}">
        <input type="hidden" name="days" value="3" />
        <input type="hidden" name="loops_per_day" value="6" />
        <input type="hidden" name="candidates_per_loop" value="5" />
        <input type="hidden" name="approve_rate" value="0.45" />
        <input type="hidden" name="resolved_rate" value="0.55" />
        <select name="scenario">
          <option value="bull_trend">Bull Trend</option>
          <option value="chop">Chop</option>
          <option value="event_shock">Event Shock</option>
          <option value="default" selected>Default</option>
        </select>
        <button class="a">Run Synthetic Replay</button>
      </form>
      <form method="post" action="{{ url_for('action_resolve_outcomes') }}">
        <input type="hidden" name="limit" value="200" />
        <input type="hidden" name="dry_run" value="true" />
        <input type="hidden" name="stub_mode" value="true" />
        <button class="a">Run Resolver (Dry Stub)</button>
      </form>
      <form method="post" action="{{ url_for('action_usdc_check') }}"><button class="a">Run USDC Checks</button></form>
      <form method="post" action="{{ url_for('action_send_alerts') }}"><button class="a">Send Alerts</button></form>
      <form method="post" action="{{ url_for('action_kill_switch') }}"><input type="hidden" name="value" value="true" /><button class="d">Kill Switch ON</button></form>
      <form method="post" action="{{ url_for('action_kill_switch') }}"><input type="hidden" name="value" value="false" /><button class="g">Kill Switch OFF</button></form>
    </div>

    <div class="panel">
      <h3>Recent Predictions</h3>
      <table>
        <thead><tr><th>ID</th><th>Market</th><th>P</th><th>Edge</th><th>Approved</th><th>Outcome</th></tr></thead>
        <tbody>
          {% for p in predictions %}
            <tr><td>{{ p.id }}</td><td>{{ p.market_id }}</td><td>{{ p.calibrated_probability }}</td><td>{{ p.edge }}</td><td>{{ p.approved }}</td><td>{{ p.outcome }}</td></tr>
          {% endfor %}
        </tbody>
      </table>
    </div>

    <div class="grid">
      <div class="panel">
        <h3>Recent Trades</h3>
        <table><thead><tr><th>Market</th><th>Side</th><th>Size</th><th>Status</th><th>Type</th></tr></thead>
        <tbody>
          {% for t in trades %}
            <tr><td>{{ t.market_id }}</td><td>{{ t.side }}</td><td>{{ t.size_usdc }}</td><td>{{ t.status }}</td><td>{{ t.order_type }}</td></tr>
          {% endfor %}
        </tbody></table>
      </div>
      <div class="panel">
        <h3>Loop Events</h3>
        <table><thead><tr><th>Candidates</th><th>Approved</th><th>Executed</th></tr></thead>
        <tbody>
          {% for row in loop_logs %}
            <tr><td>{{ row.scan_candidates }}</td><td>{{ row.approved }}</td><td>{{ row.executed }}</td></tr>
          {% endfor %}
        </tbody></table>
      </div>
      <div class="panel">
        <h3>Telemetry Snapshot (24h)</h3>
        <table><thead><tr><th>Metric</th><th>Value</th></tr></thead>
        <tbody>
          <tr><td>loops_24h</td><td>{{ telemetry.loops_24h }}</td></tr>
          <tr><td>no_candidate_loops_24h</td><td>{{ telemetry.no_candidate_loops_24h }}</td></tr>
          <tr><td>trades_24h</td><td>{{ telemetry.trades_24h }}</td></tr>
          <tr><td>risk_rejections_24h</td><td>{{ telemetry.risk_rejections_24h }}</td></tr>
          <tr><td>trade_status_counts</td><td>{{ telemetry.trade_status_counts }}</td></tr>
          <tr><td>top_rejection_reasons</td><td>{{ telemetry.top_rejection_reasons }}</td></tr>
        </tbody></table>
      </div>
      <div class="panel">
        <h3>Long-Window Trends</h3>
        <table><thead><tr><th>Window</th><th>Resolved Accuracy</th><th>Avg Edge</th><th>PnL Proxy</th></tr></thead>
        <tbody>
          <tr><td>7d</td><td>{{ trends['7d'].resolved_accuracy }}</td><td>{{ trends['7d'].avg_edge }}</td><td>{{ trends['7d'].realized_pnl_proxy }}</td></tr>
          <tr><td>30d</td><td>{{ trends['30d'].resolved_accuracy }}</td><td>{{ trends['30d'].avg_edge }}</td><td>{{ trends['30d'].realized_pnl_proxy }}</td></tr>
        </tbody></table>
      </div>
    </div>
  </div>
</body>
</html>
"""


TRADE_LIFECYCLE_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Trade Lifecycle Drilldown</title>
  <style>
    body { margin: 0; background: #f5efe3; color: #1d2a35; font-family: "Trebuchet MS", "Segoe UI", sans-serif; }
    .wrap { max-width: 1100px; margin: 0 auto; padding: 20px; }
    .panel { background: #fffdf8; border: 1px solid #dccfb8; border-radius: 12px; padding: 14px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align: left; padding: 8px; border-bottom: 1px solid #dccfb8; font-size: 13px; }
    a { color: #1f7a8c; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Trade Lifecycle Drilldown</h1>
    <div><a href="{{ url_for('home') }}">Back to Dashboard</a></div>
    <div class="panel">
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

    def _state() -> dict[str, Any]:
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
        state = _state()
        return render_template_string(
            DASHBOARD_HTML,
            **state,
        flashes=get_flashed_messages(),
        )

    @app.get("/api/state")
    def api_state():
        state = _state()
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
      state = _state()
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
