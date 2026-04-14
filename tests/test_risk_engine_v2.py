from __future__ import annotations

import json
from pathlib import Path

from prediction_bot.models import MarketSnapshot
from prediction_bot.risk_engine import AnalysisResult, PortfolioState, RiskConfig, pre_trade_check


def _market(price: float = 0.52, volume: float = 20000.0) -> MarketSnapshot:
    return MarketSnapshot(
        venue="polymarket",
        market_id="mkt-1",
        question="Will BTC close up in 5 minutes?",
        yes_price=price,
        no_price=1.0 - price,
        spread=0.02,
        volume=volume,
        liquidity=25000.0,
        expires_at=None,
        raw={},
    )


def test_risk_config_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "risk_config.json"
    config = RiskConfig(min_edge=0.07)
    config.save_json_file(path)

    loaded = RiskConfig.from_json_file(path)
    assert loaded.min_edge == 0.07


def test_portfolio_state_atomic_persistence(tmp_path: Path) -> None:
    state_path = tmp_path / "portfolio_state.json"
    portfolio = PortfolioState.from_json_file(state_path, default_starting_balance=100.0)

    portfolio.record_pnl(-2.0)
    assert state_path.exists()

    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["daily_pnl"] == -2.0
    assert data["current_balance"] == 98.0


def test_pre_trade_check_rejects_skip(tmp_path: Path) -> None:
    portfolio = PortfolioState.from_json_file(tmp_path / "state.json", default_starting_balance=100.0)
    config = RiskConfig()
    analysis = AnalysisResult(probability=0.6, decision="SKIP", confidence="High", edge=0.1)

    approved, reason, size = pre_trade_check(
        analysis=analysis,
        market=_market(),
        portfolio=portfolio,
        config=config,
        log_path=tmp_path / "risk_log.jsonl",
    )

    assert approved is False
    assert reason == "Claude returned SKIP"
    assert size == 0.0


def test_pre_trade_check_rejects_low_confidence(tmp_path: Path) -> None:
    portfolio = PortfolioState.from_json_file(tmp_path / "state.json", default_starting_balance=100.0)
    config = RiskConfig(min_confidence="Medium")
    analysis = AnalysisResult(probability=0.7, decision="YES", confidence="Low", edge=0.2)

    approved, reason, _ = pre_trade_check(
        analysis=analysis,
        market=_market(),
        portfolio=portfolio,
        config=config,
        log_path=tmp_path / "risk_log.jsonl",
    )

    assert approved is False
    assert reason == "Confidence below threshold"


def test_pre_trade_check_approves_valid_trade(tmp_path: Path) -> None:
    portfolio = PortfolioState.from_json_file(tmp_path / "state.json", default_starting_balance=1000.0)
    config = RiskConfig(min_edge=0.05, min_confidence="Medium", max_position_pct=0.1, kelly_fraction=0.25)
    analysis = AnalysisResult(probability=0.65, decision="YES", confidence="High", edge=0.13)

    approved, reason, size = pre_trade_check(
        analysis=analysis,
        market=_market(price=0.52, volume=25000.0),
        portfolio=portfolio,
        config=config,
        log_path=tmp_path / "risk_log.jsonl",
    )

    assert approved is True
    assert reason == "APPROVED"
    assert size >= 1.0


def test_pre_trade_check_clamps_probability_and_handles_boundaries(tmp_path: Path) -> None:
    portfolio = PortfolioState.from_json_file(tmp_path / "state.json", default_starting_balance=1000.0)
    config = RiskConfig(min_edge=0.01, min_confidence="Low", max_position_pct=0.1, kelly_fraction=0.25)

    analysis = AnalysisResult(probability=2.5, decision="YES", confidence="High", edge=0.2)
    approved, reason, size = pre_trade_check(
        analysis=analysis,
        market=_market(price=0.55, volume=25000.0),
        portfolio=portfolio,
        config=config,
        log_path=tmp_path / "risk_log.jsonl",
    )
    assert approved is True
    assert reason == "APPROVED"
    assert size > 0

    boundary = _market(price=1.0, volume=25000.0)
    approved2, reason2, size2 = pre_trade_check(
        analysis=analysis,
        market=boundary,
        portfolio=portfolio,
        config=config,
        log_path=tmp_path / "risk_log.jsonl",
    )
    assert approved2 is False
    assert reason2 == "Invalid market price"
    assert size2 == 0.0
