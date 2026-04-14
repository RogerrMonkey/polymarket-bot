from pathlib import Path

from prediction_bot.storage.prediction_store import PredictionStore


def test_prediction_store_brier_metrics(tmp_path: Path) -> None:
    db_path = tmp_path / "predictions.db"
    store = PredictionStore(str(db_path))

    p1 = store.record_prediction(
        venue="polymarket",
        market_id="m1",
        question="Q1",
        market_probability=0.5,
        raw_model_probability=0.6,
        calibrated_probability=0.58,
        edge=0.08,
        approved=True,
        reasons=[],
        opportunity_score=0.8,
        research_sentiment=0.2,
        research_confidence=0.6,
        research_evidence_count=4,
    )
    p2 = store.record_prediction(
        venue="polymarket",
        market_id="m2",
        question="Q2",
        market_probability=0.4,
        raw_model_probability=0.45,
        calibrated_probability=0.43,
        edge=0.03,
        approved=False,
        reasons=["edge_below_threshold:0.03"],
        opportunity_score=0.4,
        research_sentiment=-0.1,
        research_confidence=0.3,
        research_evidence_count=2,
    )

    assert store.set_outcome(p1, 1.0)
    assert store.set_outcome(p2, 0.0)

    metrics = store.brier_metrics()
    assert metrics.sample_count == 2
    assert metrics.brier_score is not None
    assert metrics.rmse is not None


def test_unresolved_predictions_and_custom_timestamps(tmp_path: Path) -> None:
    db_path = tmp_path / "predictions.db"
    store = PredictionStore(str(db_path))

    p1 = store.record_prediction(
        venue="polymarket",
        market_id="m3",
        question="Q3",
        market_probability=0.5,
        raw_model_probability=0.51,
        calibrated_probability=0.52,
        edge=0.02,
        approved=False,
        reasons=["test"],
        opportunity_score=0.2,
        research_sentiment=0.0,
        research_confidence=0.1,
        research_evidence_count=1,
        created_at="2026-04-01T00:00:00+00:00",
    )
    p2 = store.record_prediction(
        venue="polymarket",
        market_id="m4",
        question="Q4",
        market_probability=0.5,
        raw_model_probability=0.6,
        calibrated_probability=0.62,
        edge=0.12,
        approved=True,
        reasons=[],
        opportunity_score=0.9,
        research_sentiment=0.3,
        research_confidence=0.7,
        research_evidence_count=5,
    )

    unresolved = store.unresolved_predictions(limit=10)
    ids = [int(r["id"]) for r in unresolved]
    assert p1 in ids and p2 in ids

    assert store.set_outcome(p1, 1.0, resolved_at="2026-04-02T00:00:00+00:00")
    unresolved_after = store.unresolved_predictions(limit=10)
    ids_after = [int(r["id"]) for r in unresolved_after]
    assert p1 not in ids_after
    assert p2 in ids_after
