from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from prediction_bot.dashboard import (
    _build_prelive_summary,
    _build_replay_summary,
    _build_resolver_summary,
    _build_trade_lifecycle,
    _build_trend_chart_rows,
    _load_risk_config,
    _save_kill_switch,
    create_dashboard_app,
)


def test_kill_switch_toggle_roundtrip(tmp_path: Path) -> None:
    cfg_path = tmp_path / "risk_config.json"

    _save_kill_switch(cfg_path, enabled=True)
    cfg = _load_risk_config(cfg_path)
    assert cfg.kill_switch is True

    _save_kill_switch(cfg_path, enabled=False)
    cfg = _load_risk_config(cfg_path)
    assert cfg.kill_switch is False


def test_trade_lifecycle_groups_by_order_id() -> None:
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {"order_id": "o1", "market_id": "m1", "side": "YES", "status": "pending", "timestamp": now, "size_usdc": 10},
        {"order_id": "o1", "market_id": "m1", "side": "YES", "status": "filled", "timestamp": now, "size_usdc": 10},
        {"order_id": "o2", "market_id": "m2", "side": "NO", "status": "failed", "timestamp": now, "size_usdc": 5},
    ]
    out = _build_trade_lifecycle(rows)
    assert len(out) == 2
    assert any(r["order_id"] == "o1" and "pending -> filled" in r["status_path"] for r in out)


def test_build_trend_chart_rows() -> None:
    trends = {
        "7d": {
            "total_signals": 10,
            "signals_traded": 4,
            "resolved_accuracy": 0.5,
            "brier_score": 0.2,
        },
        "30d": {
            "total_signals": 40,
            "signals_traded": 12,
            "resolved_accuracy": 0.65,
            "brier_score": 0.3,
        },
    }

    rows = _build_trend_chart_rows(trends)
    assert len(rows["throughput"]) == 2
    assert rows["throughput"][0]["pct"] == 40.0
    assert rows["quality"][0]["accuracy_pct"] == 50.0
    assert rows["quality"][1]["brier_quality_pct"] == 70.0


def test_build_prelive_summary() -> None:
    missing = _build_prelive_summary(None)
    assert missing["status"] == "unknown"

    payload = {
        "all_passed": False,
        "timestamp": "2026-04-06T07:00:00+00:00",
        "passed_count": 5,
        "failed_count": 2,
    }
    out = _build_prelive_summary(payload)
    assert out["status"] == "fail"
    assert out["failed_count"] == 2


def test_build_replay_summary() -> None:
    missing = _build_replay_summary(None)
    assert missing["status"] == "none"
    assert missing["scenario"] == "n/a"

    payload = {
        "scenario": "chop",
        "predictions_written": 42,
        "loops_written": 9,
    }
    out = _build_replay_summary(payload)
    assert out["status"] == "ready"
    assert out["scenario"] == "chop"
    assert out["predictions_written"] == 42


def test_build_resolver_summary() -> None:
    missing = _build_resolver_summary(None)
    assert missing["status"] == "none"
    assert missing["checked"] == 0

    payload = {
        "checked": 15,
        "resolved": 4,
        "applied": 4,
    }
    out = _build_resolver_summary(payload)
    assert out["status"] == "ready"
    assert out["checked"] == 15
    assert out["resolved"] == 4


# ---------------------------------------------------------------------------
# Flask endpoint smoke tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def dashboard_client(tmp_path: Path):
    """Create a minimal workspace and return a Flask test client."""
    # Minimal risk_config.json so the app can load
    risk_cfg = {
        "daily_loss_cap_pct": 0.05,
        "max_drawdown_pct": 0.10,
        "max_position_pct": 0.10,
        "min_edge": 0.03,
        "min_confidence": 0.60,
        "kelly_fraction": 0.25,
        "min_liquidity_usdc": 100.0,
        "kill_switch": False,
    }
    (tmp_path / "risk_config.json").write_text(json.dumps(risk_cfg))
    (tmp_path / "data").mkdir()

    app = create_dashboard_app(workspace_root=tmp_path)
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_home_returns_200(dashboard_client) -> None:
    resp = dashboard_client.get("/")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "Control Room" in body


def test_api_status_returns_valid_json(dashboard_client) -> None:
    resp = dashboard_client.get("/api/status")
    assert resp.status_code == 200
    assert resp.content_type.startswith("application/json")
    data = json.loads(resp.data)
    assert "provider" in data
    assert "warp_status" in data
    assert "paper_days_done" in data
    assert "checklist_pass_count" in data
    assert isinstance(data["uptime_seconds"], (int, float))
