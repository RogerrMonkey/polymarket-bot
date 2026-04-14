from prediction_bot.config import RiskSettings
from prediction_bot.core.risk import RiskEngine, kelly_fraction_for_binary
from prediction_bot.models import MarketSnapshot, TradeSignal


def _snap() -> MarketSnapshot:
    return MarketSnapshot(
        venue="test",
        market_id="m1",
        question="Will X happen?",
        yes_price=0.52,
        no_price=0.48,
        spread=0.02,
        volume=1200,
        liquidity=1500,
        expires_at=None,
        raw={},
    )


def test_kelly_fraction_non_negative() -> None:
    assert kelly_fraction_for_binary(0.6, 0.5) > 0
    assert kelly_fraction_for_binary(0.4, 0.6) == 0


def test_risk_engine_blocks_low_edge() -> None:
    engine = RiskEngine(RiskSettings(edge_threshold=0.04))
    signal = TradeSignal(market=_snap(), model_probability=0.53, market_probability=0.52)
    decision = engine.evaluate(signal, 0, 0, 0, 0)
    assert not decision.approved
    assert any("edge_below_threshold" in r for r in decision.reasons)


def test_risk_engine_approves_valid_trade() -> None:
    engine = RiskEngine(RiskSettings(edge_threshold=0.04, kelly_fraction=0.25, max_position_fraction=0.05))
    signal = TradeSignal(market=_snap(), model_probability=0.62, market_probability=0.52)
    decision = engine.evaluate(signal, 0, 0, 0, 0)
    assert decision.approved
    assert decision.position_fraction > 0
