from datetime import datetime, timedelta, timezone

from prediction_bot.config import ScanSettings
from prediction_bot.core.scanner import MarketScanner
from prediction_bot.models import MarketSnapshot


def _market(
    market_id: str,
    volume: float,
    liquidity: float,
    spread: float,
    days_to_expiry: int,
) -> MarketSnapshot:
    return MarketSnapshot(
        venue="test",
        market_id=market_id,
        question="Q",
        yes_price=0.5,
        no_price=0.5,
        spread=spread,
        volume=volume,
        liquidity=liquidity,
        expires_at=datetime.now(timezone.utc) + timedelta(days=days_to_expiry),
        raw={},
    )


def test_scanner_filters_market_constraints() -> None:
    scanner = MarketScanner(ScanSettings(min_volume=200, min_liquidity=200, max_days_to_expiry=30, max_spread=0.05))

    snapshots = [
        _market("good", 1000, 1500, 0.03, 10),
        _market("low_volume", 100, 1500, 0.03, 10),
        _market("far_expiry", 1000, 1500, 0.03, 45),
        _market("too_wide", 1000, 1500, 0.2, 10),
    ]

    candidates = scanner.scan(snapshots)
    assert len(candidates) == 1
    assert candidates[0].snapshot.market_id == "good"


def test_scanner_sorts_high_score_first() -> None:
    scanner = MarketScanner(ScanSettings(min_volume=0, min_liquidity=0, max_days_to_expiry=30, max_spread=0.05))

    a = _market("a", 1000, 1000, 0.02, 15)
    b = _market("b", 5000, 6000, 0.01, 7)

    candidates = scanner.scan([a, b])
    assert candidates[0].snapshot.market_id == "b"
