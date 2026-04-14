from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from prediction_bot.storage.prediction_store import PredictionStore


@dataclass(frozen=True)
class SyntheticReplayReport:
    scenario: str
    days: int
    loops_written: int
    predictions_written: int
    approved_signals: int
    trades_written: int
    risk_events_written: int
    analyses_written: int
    outcomes_written: int
    unresolved_stub_entries: int


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def replay_report_path(workspace_root: Path) -> Path:
    return workspace_root / "data" / "synthetic_replay_last.json"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _clamp(value: float, lo: float = 0.01, hi: float = 0.99) -> float:
    return max(lo, min(hi, value))


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")


def _write_stub_map(path: Path, stub_map: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stub_map, indent=2, sort_keys=True), encoding="utf-8")


def _scenario_market_probability(
    scenario: str,
    day_idx: int,
    days: int,
    loop_idx: int,
    loops_per_day: int,
    rng: random.Random,
) -> float:
    if scenario == "bull_trend":
        progress = (day_idx / max(1, days - 1)) if days > 1 else 1.0
        base = 0.42 + (0.38 * progress)
        noise = rng.uniform(-0.08, 0.08)
        return _clamp(base + noise, lo=0.10, hi=0.95)

    if scenario == "chop":
        return _clamp(0.5 + rng.uniform(-0.06, 0.06), lo=0.20, hi=0.80)

    if scenario == "event_shock":
        base = 0.5 + rng.uniform(-0.08, 0.08)
        shock_slot = max(1, loops_per_day // 3)
        if loop_idx % shock_slot == 0:
            base += rng.uniform(-0.28, 0.28)
        return _clamp(base, lo=0.05, hi=0.95)

    return _clamp(rng.uniform(0.15, 0.85))


def _scenario_bias_range(scenario: str) -> tuple[float, float]:
    if scenario == "bull_trend":
        return (-0.08, 0.22)
    if scenario == "chop":
        return (-0.08, 0.08)
    if scenario == "event_shock":
        return (-0.24, 0.24)
    return (-0.16, 0.16)


def _scenario_rate_multipliers(scenario: str) -> tuple[float, float]:
    if scenario == "bull_trend":
        return (1.15, 1.05)
    if scenario == "chop":
        return (0.8, 0.95)
    if scenario == "event_shock":
        return (1.3, 0.7)
    return (1.0, 1.0)


def run_synthetic_replay(
    workspace_root: Path,
    db_path: str,
    days: int = 14,
    loops_per_day: int = 12,
    candidates_per_loop: int = 5,
    approve_rate: float = 0.45,
    resolved_rate: float = 0.55,
    scenario: str = "default",
    seed: int = 7,
    write_resolution_stub: bool = True,
) -> SyntheticReplayReport:
    rng = random.Random(seed)
    store = PredictionStore(db_path)

    data_dir = workspace_root / "data"
    loop_path = data_dir / "loop_log.jsonl"
    trade_path = data_dir / "trades.jsonl"
    risk_path = data_dir / "risk_log.jsonl"
    analyses_path = data_dir / "analyses.jsonl"
    stub_path = data_dir / "resolution_stub.json"

    loops_written = 0
    predictions_written = 0
    approved_signals = 0
    trades_written = 0
    risk_events_written = 0
    analyses_written = 0
    outcomes_written = 0

    unresolved_stub_map: dict[str, float] = {}

    approved_mult, resolved_mult = _scenario_rate_multipliers(scenario)
    effective_approve_rate = _clamp(approve_rate * approved_mult, lo=0.0, hi=1.0)
    effective_resolved_rate = _clamp(resolved_rate * resolved_mult, lo=0.0, hi=1.0)
    bias_lo, bias_hi = _scenario_bias_range(scenario)

    now = _utc_now().replace(hour=9, minute=0, second=0, microsecond=0)
    day_base = now - timedelta(days=max(0, days - 1))

    for day_idx in range(days):
        day_start = day_base + timedelta(days=day_idx)

        for loop_idx in range(loops_per_day):
            ts = day_start + timedelta(minutes=5 * loop_idx)
            scan_candidates = candidates_per_loop + rng.randint(0, 2)
            approved_count = 0
            executed_count = 0

            for c_idx in range(candidates_per_loop):
                market_id = f"syn-{day_idx:02d}-{loop_idx:02d}-{c_idx:02d}"
                market_prob = _scenario_market_probability(
                    scenario=scenario,
                    day_idx=day_idx,
                    days=days,
                    loop_idx=loop_idx,
                    loops_per_day=loops_per_day,
                    rng=rng,
                )
                model_bias = rng.uniform(bias_lo, bias_hi)
                raw_prob = _clamp(market_prob + model_bias)
                calibrated_prob = _clamp((0.8 * raw_prob) + (0.2 * market_prob))
                edge = round(calibrated_prob - market_prob, 6)

                approved = abs(edge) >= 0.05 and (rng.random() < effective_approve_rate)
                reasons = [] if approved else ["Confidence below threshold" if rng.random() < 0.5 else "Insufficient edge"]

                prediction_id = store.record_prediction(
                    venue="polymarket",
                    market_id=market_id,
                    question=f"Synthetic market {market_id}",
                    market_probability=market_prob,
                    raw_model_probability=raw_prob,
                    calibrated_probability=calibrated_prob,
                    edge=edge,
                    approved=approved,
                    reasons=reasons,
                    opportunity_score=round(abs(edge) * 20.0, 4),
                    research_sentiment=round(rng.uniform(-0.4, 0.4), 6),
                    research_confidence=round(rng.uniform(0.2, 0.95), 6),
                    research_evidence_count=rng.randint(1, 8),
                    created_at=_iso(ts),
                )
                predictions_written += 1

                _append_jsonl(
                    analyses_path,
                    {
                        "timestamp": _iso(ts),
                        "market_id": market_id,
                        "source": "synthetic_replay",
                        "decision": "YES" if calibrated_prob >= market_prob else "NO",
                        "cost_usd": round(rng.uniform(0.0, 0.03), 6),
                    },
                )
                analyses_written += 1

                if approved:
                    approved_signals += 1
                    approved_count += 1
                    executed_count += 1
                    side = "YES" if edge >= 0 else "NO"
                    _append_jsonl(
                        trade_path,
                        {
                            "timestamp": _iso(ts),
                            "market_id": market_id,
                            "side": side,
                            "size_usdc": round(rng.uniform(3.0, 20.0), 6),
                            "status": "filled",
                            "order_type": "GTC",
                            "order_id": f"syn-order-{prediction_id}",
                        },
                    )
                    trades_written += 1

                else:
                    _append_jsonl(
                        risk_path,
                        {
                            "timestamp": _iso(ts),
                            "market_id": market_id,
                            "reason": reasons[0],
                        },
                    )
                    risk_events_written += 1

                if rng.random() < effective_resolved_rate:
                    default_outcome = 1.0 if calibrated_prob >= 0.5 else 0.0
                    # Inject disagreement to keep calibration non-trivial.
                    if rng.random() < 0.35:
                        default_outcome = 1.0 - default_outcome
                    settled_at = ts + timedelta(hours=rng.randint(1, 12))
                    if store.set_outcome(prediction_id, default_outcome, resolved_at=_iso(settled_at)):
                        outcomes_written += 1
                else:
                    unresolved_stub_map[market_id] = 1.0 if calibrated_prob >= 0.5 else 0.0

            store.record_scan_run(
                candidate_count=scan_candidates,
                decision_count=candidates_per_loop,
                ingestion_errors=[],
                created_at=_iso(ts),
            )
            _append_jsonl(
                loop_path,
                {
                    "timestamp": _iso(ts),
                    "scan_candidates": scan_candidates,
                    "approved": approved_count,
                    "executed": executed_count,
                    "synthetic": True,
                },
            )
            loops_written += 1

    if write_resolution_stub:
        _write_stub_map(stub_path, unresolved_stub_map)

    return SyntheticReplayReport(
        scenario=scenario,
        days=days,
        loops_written=loops_written,
        predictions_written=predictions_written,
        approved_signals=approved_signals,
        trades_written=trades_written,
        risk_events_written=risk_events_written,
        analyses_written=analyses_written,
        outcomes_written=outcomes_written,
        unresolved_stub_entries=len(unresolved_stub_map),
    )


def write_synthetic_replay_report(workspace_root: Path, report: SyntheticReplayReport) -> Path:
    path = replay_report_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": _utc_now().isoformat(),
        "scenario": report.scenario,
        "days": report.days,
        "loops_written": report.loops_written,
        "predictions_written": report.predictions_written,
        "approved_signals": report.approved_signals,
        "trades_written": report.trades_written,
        "risk_events_written": report.risk_events_written,
        "analyses_written": report.analyses_written,
        "outcomes_written": report.outcomes_written,
        "unresolved_stub_entries": report.unresolved_stub_entries,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def read_synthetic_replay_report(workspace_root: Path) -> dict[str, Any] | None:
    path = replay_report_path(workspace_root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def print_synthetic_replay_report(report: SyntheticReplayReport) -> None:
    print(f"scenario={report.scenario}")
    print(f"days={report.days}")
    print(f"loops_written={report.loops_written}")
    print(f"predictions_written={report.predictions_written}")
    print(f"approved_signals={report.approved_signals}")
    print(f"trades_written={report.trades_written}")
    print(f"risk_events_written={report.risk_events_written}")
    print(f"analyses_written={report.analyses_written}")
    print(f"outcomes_written={report.outcomes_written}")
    print(f"unresolved_stub_entries={report.unresolved_stub_entries}")
