from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class BrierMetrics:
    sample_count: int
    brier_score: float | None
    rmse: float | None


class PredictionStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scan_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    candidate_count INTEGER NOT NULL,
                    decision_count INTEGER NOT NULL,
                    ingestion_errors TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    question TEXT NOT NULL,
                    market_probability REAL NOT NULL,
                    raw_model_probability REAL NOT NULL,
                    calibrated_probability REAL NOT NULL,
                    edge REAL NOT NULL,
                    approved INTEGER NOT NULL,
                    reasons TEXT NOT NULL,
                    opportunity_score REAL NOT NULL,
                    research_sentiment REAL,
                    research_confidence REAL,
                    research_evidence_count INTEGER,
                    outcome REAL,
                    resolved_at TEXT
                )
                """
            )
            conn.commit()

    def record_scan_run(
        self,
        candidate_count: int,
        decision_count: int,
        ingestion_errors: list[str],
        created_at: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scan_runs (created_at, candidate_count, decision_count, ingestion_errors)
                VALUES (?, ?, ?, ?)
                """,
                (created_at or _utc_now_iso(), candidate_count, decision_count, json.dumps(ingestion_errors)),
            )
            conn.commit()

    def record_prediction(
        self,
        venue: str,
        market_id: str,
        question: str,
        market_probability: float,
        raw_model_probability: float,
        calibrated_probability: float,
        edge: float,
        approved: bool,
        reasons: list[str],
        opportunity_score: float,
        research_sentiment: float | None,
        research_confidence: float | None,
        research_evidence_count: int | None,
        created_at: str | None = None,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO predictions (
                    created_at, venue, market_id, question, market_probability,
                    raw_model_probability, calibrated_probability, edge, approved,
                    reasons, opportunity_score, research_sentiment, research_confidence,
                    research_evidence_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at or _utc_now_iso(),
                    venue,
                    market_id,
                    question,
                    market_probability,
                    raw_model_probability,
                    calibrated_probability,
                    edge,
                    1 if approved else 0,
                    json.dumps(reasons),
                    opportunity_score,
                    research_sentiment,
                    research_confidence,
                    research_evidence_count,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def set_outcome(self, prediction_id: int, outcome: float, resolved_at: str | None = None) -> bool:
        if outcome not in {0.0, 1.0}:
            raise ValueError("Outcome must be 0.0 or 1.0")

        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE predictions
                SET outcome = ?, resolved_at = ?
                WHERE id = ?
                """,
                (outcome, resolved_at or _utc_now_iso(), prediction_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def unresolved_predictions(self, limit: int = 200) -> list[sqlite3.Row]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, created_at, venue, market_id, question, calibrated_probability, market_probability
                FROM predictions
                WHERE outcome IS NULL
                ORDER BY id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return rows

    def brier_metrics(self) -> BrierMetrics:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT calibrated_probability, outcome
                FROM predictions
                WHERE outcome IS NOT NULL
                """
            ).fetchall()

        if not rows:
            return BrierMetrics(sample_count=0, brier_score=None, rmse=None)

        total = 0.0
        for row in rows:
            p = float(row["calibrated_probability"])
            y = float(row["outcome"])
            total += (p - y) ** 2

        brier = total / len(rows)
        rmse = brier ** 0.5
        return BrierMetrics(sample_count=len(rows), brier_score=round(brier, 6), rmse=round(rmse, 6))

    def recent_predictions(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, created_at, venue, market_id, calibrated_probability, edge, approved, outcome
                FROM predictions
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return rows
