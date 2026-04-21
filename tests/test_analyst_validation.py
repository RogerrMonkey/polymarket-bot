from __future__ import annotations

from prediction_bot.claude_analyst import AnalysisResult, _validate_and_normalise
from prediction_bot.models import MarketSnapshot


def _market(yes_price: float = 0.5) -> MarketSnapshot:
    return MarketSnapshot(
        venue="polymarket",
        market_id="m-test",
        question="Will something happen in a test?",
        yes_price=yes_price,
        no_price=None if yes_price is None else 1.0 - yes_price,
        spread=0.02,
        volume=20000.0,
        liquidity=25000.0,
        expires_at=None,
        raw={},
    )


def _result(**overrides) -> AnalysisResult:
    base = dict(
        probability=0.5,
        decision="SKIP",
        confidence="Medium",
        reasoning="baseline",
        edge=0.0,
        cost_usd=0.0,
        data_sources_used=[],
        provider="test",
    )
    base.update(overrides)
    return AnalysisResult(**base)


# Rule 1 — directional + Low → SKIP


def test_buy_low_conf_overridden_to_skip() -> None:
    out = _validate_and_normalise(_result(decision="BUY", confidence="Low", probability=0.7), _market(0.4))
    assert out.decision == "SKIP"
    assert "low_conf_directional" in out.reasoning


def test_yes_low_conf_also_overridden() -> None:
    # YES is the polymarket-native directional verb and maps to BUY for this rule.
    out = _validate_and_normalise(_result(decision="YES", confidence="Low", probability=0.7), _market(0.4))
    assert out.decision == "SKIP"


def test_sell_low_conf_overridden_to_skip() -> None:
    out = _validate_and_normalise(_result(decision="SELL", confidence="Low", probability=0.2), _market(0.5))
    assert out.decision == "SKIP"


# Rule 2 — SKIP + High → Medium


def test_skip_high_demoted_to_medium() -> None:
    out = _validate_and_normalise(_result(decision="SKIP", confidence="High", probability=0.5), _market(0.5))
    assert out.decision == "SKIP"
    assert out.confidence == "Medium"


# Rule 3 — probability clamping


def test_probability_clamped_high() -> None:
    out = _validate_and_normalise(_result(decision="SKIP", probability=0.995), _market(0.5))
    assert out.probability == 0.97


def test_probability_clamped_low() -> None:
    out = _validate_and_normalise(_result(decision="SKIP", probability=0.005), _market(0.5))
    assert out.probability == 0.03


def test_probability_left_alone_when_in_range() -> None:
    out = _validate_and_normalise(_result(decision="SKIP", probability=0.42), _market(0.5))
    assert out.probability == 0.42


# Rule 4 — BUY but prob < market_price


def test_buy_below_market_overridden_to_skip() -> None:
    # Model says BUY (bullish on YES) but its own probability is lower than the market's YES price.
    out = _validate_and_normalise(_result(decision="BUY", confidence="Medium", probability=0.40), _market(0.55))
    assert out.decision == "SKIP"
    assert "buy_below_market" in out.reasoning


def test_buy_above_market_kept() -> None:
    out = _validate_and_normalise(_result(decision="BUY", confidence="Medium", probability=0.70), _market(0.55))
    assert out.decision == "BUY"


# Rule 5 — SELL but prob > market_price


def test_sell_above_market_overridden_to_skip() -> None:
    out = _validate_and_normalise(_result(decision="SELL", confidence="Medium", probability=0.70), _market(0.55))
    assert out.decision == "SKIP"
    assert "sell_above_market" in out.reasoning


def test_sell_below_market_kept() -> None:
    out = _validate_and_normalise(_result(decision="SELL", confidence="Medium", probability=0.30), _market(0.55))
    assert out.decision == "SELL"


# Edge recomputation after clamping


def test_edge_recomputed_after_probability_clamp() -> None:
    # raw p=0.99 clamped to 0.97 against market=0.50 → edge 0.47 not 0.49
    out = _validate_and_normalise(
        _result(decision="SKIP", probability=0.99, edge=0.49),
        _market(0.50),
    )
    assert out.probability == 0.97
    assert abs(out.edge - 0.47) < 1e-6


def test_passthrough_when_no_rule_fires() -> None:
    out = _validate_and_normalise(
        _result(decision="BUY", confidence="High", probability=0.70, reasoning="strong edge"),
        _market(0.50),
    )
    assert out.decision == "BUY"
    assert out.confidence == "High"
    assert out.probability == 0.70
    assert out.reasoning == "strong edge"
