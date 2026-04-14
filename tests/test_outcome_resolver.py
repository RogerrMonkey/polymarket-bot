from __future__ import annotations

import json
from pathlib import Path

from prediction_bot.outcome_resolver import OutcomeResolver
from prediction_bot.storage.prediction_store import PredictionStore


def _seed_prediction(store: PredictionStore, market_id: str) -> int:
    return store.record_prediction(
        venue="polymarket",
        market_id=market_id,
        question=f"Q {market_id}",
        market_probability=0.5,
        raw_model_probability=0.55,
        calibrated_probability=0.56,
        edge=0.06,
        approved=True,
        reasons=[],
        opportunity_score=1.0,
        research_sentiment=0.1,
        research_confidence=0.8,
        research_evidence_count=3,
    )


def test_outcome_resolver_stub_dry_run(tmp_path: Path) -> None:
    db_path = str(tmp_path / "predictions.db")
    store = PredictionStore(db_path)
    pred_id = _seed_prediction(store, "m-stub")

    stub_path = tmp_path / "data" / "resolution_stub.json"
    stub_path.parent.mkdir(parents=True, exist_ok=True)
    stub_path.write_text(json.dumps({"m-stub": 1}), encoding="utf-8")

    resolver = OutcomeResolver(
        workspace_root=tmp_path,
        http=None,
        dry_run=True,
        stub_mode=True,
        stub_path=stub_path,
    )
    report = resolver.settle_unresolved_predictions(store=store, limit=20)

    assert report.checked == 1
    assert report.resolved == 1
    assert report.applied == 0
    unresolved_ids = [int(r["id"]) for r in store.unresolved_predictions(limit=10)]
    assert pred_id in unresolved_ids


def test_outcome_resolver_stub_apply(tmp_path: Path) -> None:
    db_path = str(tmp_path / "predictions.db")
    store = PredictionStore(db_path)
    pred_id = _seed_prediction(store, "m-apply")

    stub_path = tmp_path / "data" / "resolution_stub.json"
    stub_path.parent.mkdir(parents=True, exist_ok=True)
    stub_path.write_text(json.dumps({"m-apply": 0}), encoding="utf-8")

    resolver = OutcomeResolver(
        workspace_root=tmp_path,
        http=None,
        dry_run=False,
        stub_mode=True,
        stub_path=stub_path,
    )
    report = resolver.settle_unresolved_predictions(store=store, limit=20)

    assert report.checked == 1
    assert report.applied == 1
    unresolved_ids = [int(r["id"]) for r in store.unresolved_predictions(limit=10)]
    assert pred_id not in unresolved_ids


def test_outcome_resolver_closed_not_settled_is_unresolved(tmp_path: Path) -> None:
    resolver = OutcomeResolver(
        workspace_root=tmp_path,
        http=None,
        dry_run=True,
        stub_mode=False,
    )

    resolver._market_payload = lambda market_id: {  # type: ignore[attr-defined]
        "id": market_id,
        "closed": True,
        "resolved": False,
        "outcomes": ["Yes", "No"],
        "outcomePrices": [0.51, 0.49],
    }

    decision = resolver.resolve_market_outcome("m-closed")
    assert decision.resolved is False
    assert decision.outcome is None
    assert decision.detail == "closed_not_settled"
