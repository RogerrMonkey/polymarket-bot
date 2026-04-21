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


def test_resolver_skips_already_resolved_markets(tmp_path: Path) -> None:
    db_path = str(tmp_path / "predictions.db")
    store = PredictionStore(db_path)
    _seed_prediction(store, "m-dupe")

    # Pre-seed the resolved_markets.jsonl with this id so the resolver skips it.
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "resolved_markets.jsonl").write_text(
        json.dumps({"market_id": "m-dupe", "resolved_yes": True}) + "\n",
        encoding="utf-8",
    )

    stub_path = tmp_path / "data" / "resolution_stub.json"
    stub_path.write_text(json.dumps({"m-dupe": 1}), encoding="utf-8")

    resolver = OutcomeResolver(
        workspace_root=tmp_path,
        http=None,
        dry_run=False,
        stub_mode=True,
        stub_path=stub_path,
    )
    report = resolver.settle_unresolved_predictions(store=store, limit=20)
    # No new updates — the row was skipped on the skip-list.
    assert report.resolved == 0
    assert report.applied == 0


def test_resolver_writes_resolved_markets_jsonl_on_apply(tmp_path: Path) -> None:
    db_path = str(tmp_path / "predictions.db")
    store = PredictionStore(db_path)
    _seed_prediction(store, "m-writeout")

    # Seed a trade entry so entry_price + pnl_usdc flow into the row.
    trades_path = tmp_path / "data" / "trades.jsonl"
    trades_path.parent.mkdir(parents=True, exist_ok=True)
    trades_path.write_text(
        json.dumps({
            "market_id": "m-writeout", "side": "BUY",
            "fill_price": 0.4, "fill_size": 10.0,
            "order_id": "ord-1", "status": "filled", "timestamp": "2026-04-21T00:00:00Z",
        }) + "\n",
        encoding="utf-8",
    )

    stub_path = tmp_path / "data" / "resolution_stub.json"
    stub_path.write_text(json.dumps({"m-writeout": 1}), encoding="utf-8")

    resolver = OutcomeResolver(
        workspace_root=tmp_path,
        http=None,
        dry_run=False,
        stub_mode=True,
        stub_path=stub_path,
    )
    report = resolver.settle_unresolved_predictions(store=store, limit=20)
    assert report.applied == 1

    written = (tmp_path / "data" / "resolved_markets.jsonl").read_text(encoding="utf-8").strip()
    row = json.loads(written.splitlines()[-1])
    assert row["market_id"] == "m-writeout"
    assert row["resolved_yes"] is True
    assert row["entry_price"] == 0.4
    # BUY @ 0.4 * 10 resolved YES → +6.0
    assert abs(row["pnl_usdc"] - 6.0) < 1e-9
    assert "resolution_timestamp" in row


def test_resolver_gamma_failure_is_logged_and_continues(tmp_path: Path) -> None:
    db_path = str(tmp_path / "predictions.db")
    store = PredictionStore(db_path)
    _seed_prediction(store, "m-gamma-fail")

    # No stub entry, no http → http==None means _market_payload returns None
    # for every market.  Resolver must NOT crash; it must return a clean report.
    resolver = OutcomeResolver(
        workspace_root=tmp_path,
        http=None,
        dry_run=False,
        stub_mode=False,
    )
    report = resolver.settle_unresolved_predictions(store=store, limit=5)
    assert report.checked == 1
    assert report.resolved == 0
    assert report.errors == 0  # payload None is an unresolved case, not an error


def test_resolver_calls_pnl_tracker_on_resolution(tmp_path: Path) -> None:
    db_path = str(tmp_path / "predictions.db")
    store = PredictionStore(db_path)
    _seed_prediction(store, "m-pnl-hook")

    # Seed an entry in paper_pnl so the resolver's record_resolution finds it.
    from prediction_bot.paper_pnl import PaperPnLTracker

    tracker = PaperPnLTracker(ledger_path=tmp_path / "data" / "paper_pnl.jsonl")
    tracker.record_entry({
        "market_id": "m-pnl-hook", "side": "BUY",
        "price": 0.4, "size_usdc": 10.0, "order_id": "ord-x",
    })

    stub_path = tmp_path / "data" / "resolution_stub.json"
    stub_path.parent.mkdir(parents=True, exist_ok=True)
    stub_path.write_text(json.dumps({"m-pnl-hook": 1}), encoding="utf-8")

    resolver = OutcomeResolver(
        workspace_root=tmp_path,
        http=None,
        dry_run=False,
        stub_mode=True,
        stub_path=stub_path,
    )
    resolver.settle_unresolved_predictions(store=store, limit=5)

    # Expect a resolution row in paper_pnl.jsonl with the computed P&L.
    ledger = (tmp_path / "data" / "paper_pnl.jsonl").read_text(encoding="utf-8").splitlines()
    resolutions = [json.loads(line) for line in ledger if '"resolution"' in line]
    assert len(resolutions) == 1
    assert resolutions[0]["market_id"] == "m-pnl-hook"
    assert abs(resolutions[0]["pnl_usdc"] - 6.0) < 1e-9


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
