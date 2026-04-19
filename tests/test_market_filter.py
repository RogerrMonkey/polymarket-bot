from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from loguru import logger

from prediction_bot.models import MarketSnapshot, ScanCandidate
from prediction_bot.research.market_filter import filter_markets
from prediction_bot.risk_engine import RiskConfig


def _candidate(
    *,
    question: str = "Will Bitcoin close above 100k by year end?",
    volume: float | None = 20000.0,
    yes_price: float | None = 0.50,
    days_until_expiry: float | None = 30.0,
) -> ScanCandidate:
    expires_at = None
    if days_until_expiry is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(days=days_until_expiry)
    snap = MarketSnapshot(
        venue="polymarket",
        market_id="m-1",
        question=question,
        yes_price=yes_price,
        no_price=None if yes_price is None else 1.0 - yes_price,
        spread=0.02,
        volume=volume,
        liquidity=25000.0,
        expires_at=expires_at,
        raw={},
    )
    return ScanCandidate(snapshot=snap, opportunity_score=0.5, anomaly_flags=[])


def _config(**overrides) -> RiskConfig:
    base = dict(
        min_volume_24h=5000.0,
        min_edge=0.05,
        min_kelly_fraction=0.05,
    )
    base.update(overrides)
    return RiskConfig(**base)


def test_filter_keeps_healthy_market() -> None:
    keep = _candidate()
    result = filter_markets([keep], _config())
    assert len(result) == 1
    assert result[0] is keep


def test_filter_drops_low_volume() -> None:
    drop = _candidate(volume=1000.0)
    result = filter_markets([drop], _config(min_volume_24h=5000.0))
    assert result == []


def test_filter_respects_configured_volume_threshold() -> None:
    # Same candidate, different threshold -> different outcome
    c = _candidate(volume=3000.0)
    assert filter_markets([c], _config(min_volume_24h=2000.0)) == [c]
    assert filter_markets([c], _config(min_volume_24h=5000.0)) == []


def test_filter_drops_market_with_none_volume() -> None:
    drop = _candidate(volume=None)
    assert filter_markets([drop], _config()) == []


def test_filter_drops_resolves_too_soon() -> None:
    drop = _candidate(days_until_expiry=1.0)
    assert filter_markets([drop], _config()) == []


def test_filter_drops_too_far_out() -> None:
    drop = _candidate(days_until_expiry=365.0)
    assert filter_markets([drop], _config()) == []


def test_filter_keeps_expiry_none_when_other_gates_pass() -> None:
    # No expiry info -> we can't time-filter, so we keep it
    keep = _candidate(days_until_expiry=None)
    assert filter_markets([keep], _config()) == [keep]


def test_filter_drops_near_certain_price_low() -> None:
    drop = _candidate(yes_price=0.01)
    assert filter_markets([drop], _config()) == []


def test_filter_drops_near_certain_price_high() -> None:
    drop = _candidate(yes_price=0.995)
    assert filter_markets([drop], _config()) == []


def test_filter_keeps_price_at_threshold() -> None:
    keep = _candidate(yes_price=0.02)  # boundary inclusive of keep
    assert filter_markets([keep], _config()) == [keep]


def test_filter_drops_malformed_question() -> None:
    drop = _candidate(question="Short?")
    assert filter_markets([drop], _config()) == []


def test_filter_mixed_candidate_list() -> None:
    good_a = _candidate(question="Will Ethereum close above 5k this quarter?")
    good_b = _candidate(question="Will the Fed cut rates before July meeting?")
    thin = _candidate(question="Something legit but thin volume market", volume=100.0)
    soon = _candidate(question="Will something resolve quickly today?", days_until_expiry=0.5)
    far = _candidate(question="Will something in 2028 happen someday?", days_until_expiry=400.0)
    certain = _candidate(question="Will the sun rise tomorrow again?", yes_price=0.99)
    malformed = _candidate(question="tiny")

    result = filter_markets(
        [good_a, thin, good_b, soon, far, certain, malformed], _config()
    )
    assert good_a in result
    assert good_b in result
    assert len(result) == 2


def test_filter_logs_summary(caplog) -> None:
    # Loguru -> caplog bridge: add a sink that routes to logging
    import logging as _logging

    class _PropagateHandler(_logging.Handler):
        def emit(self, record):
            pass

    handler_id = logger.add(
        _PropagateHandler(),
        level="INFO",
        format="{message}",
    )

    # Also capture via print-like sink so we can inspect the message text
    captured: list[str] = []
    sink_id = logger.add(lambda msg: captured.append(str(msg)), level="INFO")

    try:
        candidates = [
            _candidate(),
            _candidate(volume=100.0),
            _candidate(days_until_expiry=1.0),
            _candidate(question="x"),
        ]
        filter_markets(candidates, _config())
    finally:
        logger.remove(handler_id)
        logger.remove(sink_id)

    joined = "\n".join(captured)
    assert "market_filter" in joined
    assert "4 in" in joined
    assert "1 passed" in joined
    assert "3 filtered" in joined


def test_filter_empty_input() -> None:
    assert filter_markets([], _config()) == []
