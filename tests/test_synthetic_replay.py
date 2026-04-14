from __future__ import annotations

import json
from pathlib import Path

from prediction_bot.synthetic_replay import run_synthetic_replay
from prediction_bot.storage.prediction_store import PredictionStore


def test_synthetic_replay_generates_history(tmp_path: Path) -> None:
    db_path = str(tmp_path / "data" / "predictions.db")
    report = run_synthetic_replay(
        workspace_root=tmp_path,
        db_path=db_path,
        days=3,
        loops_per_day=2,
        candidates_per_loop=2,
        seed=42,
    )

    assert report.days == 3
    assert report.predictions_written == 12
    assert report.loops_written == 6

    analyses_path = tmp_path / "data" / "analyses.jsonl"
    assert analyses_path.exists()

    seen_days: set[str] = set()
    for line in analyses_path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        seen_days.add(str(row["timestamp"])[:10])
    assert len(seen_days) == 3

    store = PredictionStore(db_path)
    assert len(store.recent_predictions(limit=50)) >= 12
    assert (tmp_path / "data" / "resolution_stub.json").exists()
