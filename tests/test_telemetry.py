from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from prediction_bot.storage.prediction_store import PredictionStore
from prediction_bot.telemetry import build_alerts, build_telemetry_snapshot, build_trend_snapshot


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def test_build_telemetry_snapshot_counts(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)

    (data / "loop_log.jsonl").write_text(
        json.dumps({"timestamp": _iso_now(), "scan_candidates": 0, "approved": 0, "executed": 0}) + "\n",
        encoding="utf-8",
    )
    (data / "trades.jsonl").write_text(
        json.dumps({"timestamp": _iso_now(), "status": "filled"}) + "\n",
        encoding="utf-8",
    )
    (data / "risk_log.jsonl").write_text(
        json.dumps({"timestamp": _iso_now(), "reason": "Confidence below threshold"}) + "\n",
        encoding="utf-8",
    )

    snapshot = build_telemetry_snapshot(tmp_path)
    assert snapshot["loops_24h"] == 1
    assert snapshot["trades_24h"] == 1
    assert snapshot["risk_rejections_24h"] == 1


def test_build_alerts_multiple_conditions() -> None:
    telemetry = {
        "loops_24h": 5,
        "no_candidate_loops_24h": 5,
        "trades_24h": 0,
        "risk_rejections_24h": 10,
        "trade_status_counts": {},
        "top_rejection_reasons": {},
    }

    alerts = build_alerts(
        telemetry=telemetry,
        preflight_blocked=True,
        usdc_ready=False,
        rejection_rate=0.95,
        api_cost_usd=10.0,
    )

    assert "critical:preflight_blocked" in alerts
    assert "warning:usdc_operational_checks_not_ready" in alerts
    assert "warning:no_candidates_in_recent_loops" in alerts
    assert "warning:high_rejection_rate" in alerts
    assert "warning:api_cost_budget_reached" in alerts


def test_build_trend_snapshot_returns_windows(tmp_path: Path) -> None:
    db_path = str(tmp_path / "predictions.db")
    store = PredictionStore(db_path)
    pred_id = store.record_prediction(
        venue="polymarket",
        market_id="m4",
        question="q4",
        market_probability=0.5,
        raw_model_probability=0.8,
        calibrated_probability=0.75,
        edge=0.25,
        approved=True,
        reasons=[],
        opportunity_score=2.0,
        research_sentiment=0.5,
        research_confidence=0.9,
        research_evidence_count=4,
    )
    store.set_outcome(pred_id, 1.0)

    trends = build_trend_snapshot(tmp_path, db_path=db_path)
    assert "7d" in trends
    assert "30d" in trends
    assert trends["7d"]["total_signals"] >= 1
