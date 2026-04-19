from __future__ import annotations

import json
from pathlib import Path

from datetime import datetime, timedelta, timezone

from prediction_bot.models import MarketSnapshot
from prediction_bot.risk_engine import (
    AnalysisResult,
    PortfolioState,
    RiskConfig,
    compute_edge_breakdown,
    pre_trade_check,
)


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


# --- v0.8.3 edge breakdown tests ---


def test_compute_edge_breakdown_returns_all_keys() -> None:
    market = _market(price=0.5, volume=20000.0)
    breakdown = compute_edge_breakdown(raw_edge=0.10, decision="YES", market=market)
    for key in ("raw_edge", "kelly", "vol_weight", "time_decay", "final_edge"):
        assert key in breakdown
    assert breakdown["raw_edge"] == 0.10
    # denom = 1 - 0.5 = 0.5 -> kelly = 0.10 / 0.5 = 0.2
    assert breakdown["kelly"] == 0.2
    # volume 20k -> tier 1.0; no expires_at -> time_decay 1.0
    assert breakdown["vol_weight"] == 1.0
    assert breakdown["time_decay"] == 1.0
    assert breakdown["final_edge"] == 0.10


def test_volume_weight_tiers() -> None:
    low = compute_edge_breakdown(0.1, "YES", _market(price=0.5, volume=1000.0))
    mid = compute_edge_breakdown(0.1, "YES", _market(price=0.5, volume=20000.0))
    high = compute_edge_breakdown(0.1, "YES", _market(price=0.5, volume=100000.0))
    assert low["vol_weight"] == 0.5
    assert mid["vol_weight"] == 1.0
    assert high["vol_weight"] == 1.2
    # Final edge reflects weighting
    assert low["final_edge"] == 0.05
    assert high["final_edge"] == 0.12


def test_time_decay_less_than_three_days() -> None:
    near = _market(price=0.5, volume=20000.0)
    near.expires_at = datetime.now(timezone.utc) + timedelta(days=1)
    breakdown = compute_edge_breakdown(0.10, "YES", near)
    assert breakdown["time_decay"] == 0.3
    assert breakdown["final_edge"] == 0.03

    far = _market(price=0.5, volume=20000.0)
    far.expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    b2 = compute_edge_breakdown(0.10, "YES", far)
    assert b2["time_decay"] == 1.0


def test_pre_trade_check_rejects_kelly_below_min(tmp_path: Path) -> None:
    portfolio = PortfolioState.from_json_file(tmp_path / "state.json", default_starting_balance=1000.0)
    # min_kelly_fraction=0.5 so that our small kelly gets rejected
    config = RiskConfig(
        min_edge=0.01,
        min_confidence="Low",
        min_kelly_fraction=0.5,
        min_volume_24h=0.0,
    )
    # raw_edge=0.05, price=0.5 -> denom=0.5 -> kelly=0.1 < 0.5
    analysis = AnalysisResult(probability=0.55, decision="YES", confidence="High", edge=0.05)
    approved, reason, size = pre_trade_check(
        analysis=analysis,
        market=_market(price=0.5, volume=20000.0),
        portfolio=portfolio,
        config=config,
        log_path=tmp_path / "risk_log.jsonl",
    )
    assert approved is False
    assert reason == "Kelly fraction below minimum"
    assert size == 0.0


def test_pre_trade_check_rejects_low_volume(tmp_path: Path) -> None:
    portfolio = PortfolioState.from_json_file(tmp_path / "state.json", default_starting_balance=1000.0)
    config = RiskConfig(
        min_edge=0.01,
        min_confidence="Low",
        min_kelly_fraction=0.0,
        min_volume_24h=10000.0,
        min_liquidity_usdc=0.0,
    )
    analysis = AnalysisResult(probability=0.65, decision="YES", confidence="High", edge=0.15)
    approved, reason, _ = pre_trade_check(
        analysis=analysis,
        market=_market(price=0.5, volume=500.0),
        portfolio=portfolio,
        config=config,
        log_path=tmp_path / "risk_log.jsonl",
    )
    assert approved is False
    assert reason == "Volume below minimum"


def test_pre_trade_check_rejects_insufficient_final_edge(tmp_path: Path) -> None:
    portfolio = PortfolioState.from_json_file(tmp_path / "state.json", default_starting_balance=1000.0)
    # raw_edge=0.06, but thin market volume (1000) -> weight 0.5 -> final_edge=0.03 < 0.05
    config = RiskConfig(
        min_edge=0.05,
        min_confidence="Low",
        min_kelly_fraction=0.0,
        min_volume_24h=0.0,
        min_liquidity_usdc=0.0,
    )
    analysis = AnalysisResult(probability=0.56, decision="YES", confidence="High", edge=0.06)
    approved, reason, _ = pre_trade_check(
        analysis=analysis,
        market=_market(price=0.5, volume=1000.0),
        portfolio=portfolio,
        config=config,
        log_path=tmp_path / "risk_log.jsonl",
    )
    assert approved is False
    assert reason == "Insufficient edge"


def test_rejection_log_includes_breakdown(tmp_path: Path) -> None:
    portfolio = PortfolioState.from_json_file(tmp_path / "state.json", default_starting_balance=1000.0)
    config = RiskConfig(
        min_edge=0.99,
        min_confidence="Low",
        min_kelly_fraction=0.0,
        min_volume_24h=0.0,
        min_liquidity_usdc=0.0,
    )
    analysis = AnalysisResult(probability=0.6, decision="YES", confidence="High", edge=0.1)
    log_path = tmp_path / "risk_log.jsonl"
    approved, reason, _ = pre_trade_check(
        analysis=analysis,
        market=_market(price=0.5, volume=20000.0),
        portfolio=portfolio,
        config=config,
        log_path=log_path,
    )
    assert approved is False
    assert reason == "Insufficient edge"
    line = log_path.read_text(encoding="utf-8").strip().splitlines()[-1]
    entry = json.loads(line)
    assert "edge_breakdown" in entry
    assert set(entry["edge_breakdown"].keys()) == {
        "raw_edge",
        "kelly",
        "vol_weight",
        "time_decay",
        "final_edge",
    }
