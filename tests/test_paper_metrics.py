from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from prediction_bot.paper_metrics import build_long_window_trends, check_paper_gates, daily_summary, window_summary
from prediction_bot.storage.prediction_store import PredictionStore


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def test_daily_summary_counts_and_reasons(tmp_path: Path) -> None:
    db_path = str(tmp_path / "predictions.db")
    store = PredictionStore(db_path)

    store.record_prediction(
        venue="polymarket",
        market_id="m1",
        question="q1",
        market_probability=0.5,
        raw_model_probability=0.6,
        calibrated_probability=0.6,
        edge=0.1,
        approved=False,
        reasons=["Confidence below threshold"],
        opportunity_score=1.0,
        research_sentiment=0.0,
        research_confidence=0.0,
        research_evidence_count=0,
    )

    summary = daily_summary(workspace_root=tmp_path, db_path=db_path)
    assert summary["total_signals"] >= 1
    assert summary["signals_rejected"] >= 1
    assert "Confidence below threshold" in summary["rejection_reasons"]


def test_check_paper_gates_fails_without_data(tmp_path: Path) -> None:
    db_path = str(tmp_path / "predictions.db")
    PredictionStore(db_path)

    ready, failures = check_paper_gates(workspace_root=tmp_path, db_path=db_path)
    assert ready is False
    assert "brier_score_gate_failed" in failures


def test_check_paper_gates_detects_recent_kill_switch(tmp_path: Path) -> None:
    db_path = str(tmp_path / "predictions.db")
    store = PredictionStore(db_path)

    pred_id = store.record_prediction(
        venue="polymarket",
        market_id="m2",
        question="q2",
        market_probability=0.5,
        raw_model_probability=0.7,
        calibrated_probability=0.7,
        edge=0.2,
        approved=False,
        reasons=["test"],
        opportunity_score=1.2,
        research_sentiment=0.2,
        research_confidence=0.8,
        research_evidence_count=3,
    )
    store.set_outcome(pred_id, 1.0)

    risk_log = tmp_path / "data" / "risk_log.jsonl"
    risk_log.parent.mkdir(parents=True, exist_ok=True)
    risk_log.write_text(
        json.dumps({"timestamp": _now_iso(), "reason": "Kill switch active"}) + "\n",
        encoding="utf-8",
    )

    ready, failures = check_paper_gates(workspace_root=tmp_path, db_path=db_path)
    assert ready is False
    assert "kill_switch_recent_trigger" in failures


def test_window_summary_and_trends(tmp_path: Path) -> None:
    db_path = str(tmp_path / "predictions.db")
    store = PredictionStore(db_path)

    pred_id = store.record_prediction(
        venue="polymarket",
        market_id="m3",
        question="q3",
        market_probability=0.45,
        raw_model_probability=0.7,
        calibrated_probability=0.65,
        edge=0.2,
        approved=True,
        reasons=[],
        opportunity_score=1.5,
        research_sentiment=0.3,
        research_confidence=0.8,
        research_evidence_count=2,
    )
    store.set_outcome(pred_id, 1.0)

    summary = window_summary(workspace_root=tmp_path, db_path=db_path, days=7)
    assert summary["window_days"] == 7
    assert summary["total_signals"] == 1
    assert summary["signals_traded"] == 1
    assert summary["resolved_signals"] == 1
    assert summary["brier_score"] is not None

    trends = build_long_window_trends(workspace_root=tmp_path, db_path=db_path)
    assert "7d" in trends
    assert "30d" in trends
