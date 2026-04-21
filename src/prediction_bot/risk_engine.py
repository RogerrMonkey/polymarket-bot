from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from prediction_bot.models import MarketSnapshot


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _confidence_rank(label: str) -> int:
    normalized = label.strip().lower()
    ranks = {"low": 0, "medium": 1, "high": 2}
    return ranks.get(normalized, -1)


def _clamp_probability(value: float) -> float:
    return max(0.0, min(1.0, value))


@dataclass(frozen=True)
class RiskConfig:
    daily_loss_cap_pct: float = 0.05
    max_drawdown_pct: float = 0.20
    max_position_pct: float = 0.10
    min_edge: float = 0.05
    min_confidence: str = "Medium"
    kelly_fraction: float = 0.25
    min_liquidity_usdc: float = 10000.0
    kill_switch: bool = False
    # v0.8.3 edge-quality gates
    min_kelly_fraction: float = 0.05
    min_volume_24h: float = 5000.0

    @classmethod
    def from_json_file(cls, path: str | Path) -> "RiskConfig":
        p = Path(path)
        if not p.exists():
            return cls()
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return cls()
        allowed = {k: data[k] for k in cls.__dataclass_fields__.keys() if k in data}
        return cls(**allowed)

    def save_json_file(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


@dataclass
class PortfolioState:
    starting_balance: float
    current_balance: float
    peak_balance: float
    daily_pnl: float
    open_positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    state_path: Path = field(default=Path("data/portfolio_state.json"))

    @classmethod
    def from_json_file(
        cls,
        path: str | Path,
        default_starting_balance: float = 0.0,
    ) -> "PortfolioState":
        p = Path(path)
        if not p.exists():
            return cls(
                starting_balance=default_starting_balance,
                current_balance=default_starting_balance,
                peak_balance=default_starting_balance,
                daily_pnl=0.0,
                open_positions={},
                state_path=p,
            )

        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("Invalid portfolio state format")

        return cls(
            starting_balance=float(data.get("starting_balance", default_starting_balance)),
            current_balance=float(data.get("current_balance", default_starting_balance)),
            peak_balance=float(data.get("peak_balance", default_starting_balance)),
            daily_pnl=float(data.get("daily_pnl", 0.0)),
            open_positions=dict(data.get("open_positions", {})),
            state_path=p,
        )

    def _atomic_save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        payload = {
            "starting_balance": self.starting_balance,
            "current_balance": self.current_balance,
            "peak_balance": self.peak_balance,
            "daily_pnl": self.daily_pnl,
            "open_positions": self.open_positions,
            "updated_at": _utc_now_iso(),
        }
        temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp_path.replace(self.state_path)

    def record_fill(self, trade: dict[str, Any]) -> None:
        trade_id = str(trade.get("order_id") or trade.get("market_id") or _utc_now_iso())
        self.open_positions[trade_id] = trade
        self._atomic_save()

    def record_pnl(self, amount: float) -> None:
        self.daily_pnl += amount
        self.current_balance += amount
        self.peak_balance = max(self.peak_balance, self.current_balance)
        self._atomic_save()


def _volume_weight(volume: float | None) -> float:
    """Edge multiplier based on 24h volume.

    Thin markets have wide spreads that eat edge; very liquid markets reward it.
    """
    v = float(volume or 0.0)
    if v < 5_000:
        return 0.5
    if v < 50_000:
        return 1.0
    return 1.2


def _time_decay_weight(market: MarketSnapshot) -> float:
    """Edge multiplier based on days to resolution.

    <3 days: price largely locked, discount edge heavily.
    """
    days = market.time_to_expiry_days
    if days is None:
        return 1.0
    if days < 3.0:
        return 0.3
    return 1.0


def compute_edge_breakdown(
    raw_edge: float,
    decision: str,
    market: MarketSnapshot,
) -> dict[str, float]:
    """Return the full edge calculation breakdown.

    - raw_edge:   |model_probability - market_price| as passed in
    - kelly:      edge-to-payoff ratio (fraction of bankroll to bet, unscaled)
    - vol_weight: volume-tier multiplier
    - time_decay: days-to-resolution multiplier
    - final_edge: raw_edge * vol_weight * time_decay
    """
    market_price = market.yes_price if market.yes_price is not None else 0.5

    decision_u = (decision or "").upper()
    if decision_u == "YES" or decision_u == "BUY":
        denom = max(1e-9, 1.0 - market_price)
    else:
        denom = max(1e-9, market_price)
    kelly = raw_edge / denom

    vol_weight = _volume_weight(market.volume)
    time_decay = _time_decay_weight(market)
    final_edge = raw_edge * vol_weight * time_decay

    breakdown = {
        "raw_edge": round(float(raw_edge), 6),
        "kelly": round(float(kelly), 6),
        "vol_weight": round(float(vol_weight), 4),
        "time_decay": round(float(time_decay), 4),
        "final_edge": round(float(final_edge), 6),
    }
    logger.debug(
        "edge_calc market={} raw_edge={} kelly={} vol_weight={} time_decay={} final_edge={}",
        market.market_id,
        breakdown["raw_edge"],
        breakdown["kelly"],
        breakdown["vol_weight"],
        breakdown["time_decay"],
        breakdown["final_edge"],
    )
    return breakdown


@dataclass(frozen=True)
class AnalysisResult:
    probability: float
    decision: str
    confidence: str
    edge: float
    reasoning: str = ""


def _log_rejection(
    reason: str,
    analysis: AnalysisResult,
    market: MarketSnapshot,
    log_path: str | Path,
    breakdown: dict[str, float] | None = None,
) -> None:
    entry = {
        "timestamp": _utc_now_iso(),
        "reason": reason,
        "market_id": market.market_id,
        "analysis": {
            "decision": analysis.decision,
            "confidence": analysis.confidence,
            "probability": analysis.probability,
            "edge": analysis.edge,
        },
    }
    if breakdown is not None:
        entry["edge_breakdown"] = breakdown

    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _emit_risk_gate_debug(
    analysis: AnalysisResult,
    market: MarketSnapshot,
    breakdown: dict[str, float] | None,
    config: RiskConfig,
    verdict: str,
    reason: str,
) -> None:
    """Single-line INFO log for every non-SKIP decision that enters the gate.

    Deliberately INFO (not DEBUG) so it survives default loguru level. The
    dashboard and operators can grep `risk_gate_debug` to see exactly why
    directional calls are blocked without waiting for a full audit pass.
    """
    decision_u = (analysis.decision or "").upper()
    if decision_u == "SKIP":
        return
    kelly = breakdown.get("kelly") if breakdown else None
    vol_w = breakdown.get("vol_weight") if breakdown else None
    td = breakdown.get("time_decay") if breakdown else None
    final_edge = breakdown.get("final_edge") if breakdown else None
    logger.info(
        "risk_gate_debug market={} decision={} confidence={} probability={} "
        "market_price={} raw_edge={} kelly={} vol_weight={} time_decay={} "
        "final_edge={} min_kelly={} verdict={} reason={}",
        market.market_id,
        analysis.decision,
        analysis.confidence,
        analysis.probability,
        market.yes_price,
        analysis.edge,
        kelly,
        vol_w,
        td,
        final_edge,
        config.min_kelly_fraction,
        verdict,
        reason,
    )


def pre_trade_check(
    analysis: AnalysisResult,
    market: MarketSnapshot,
    portfolio: PortfolioState,
    config: RiskConfig,
    log_path: str | Path = "data/risk_log.jsonl",
) -> tuple[bool, str, float]:
    try:
        normalized_probability = _clamp_probability(float(analysis.probability))
    except (TypeError, ValueError):
        reason = "Invalid model probability"
        _log_rejection(reason, analysis, market, log_path)
        _emit_risk_gate_debug(analysis, market, None, config, "REJECT", reason)
        return False, reason, 0.0

    analysis = AnalysisResult(
        probability=normalized_probability,
        decision=analysis.decision,
        confidence=analysis.confidence,
        edge=analysis.edge,
        reasoning=analysis.reasoning,
    )

    if config.kill_switch:
        reason = "Kill switch active"
        _log_rejection(reason, analysis, market, log_path)
        _emit_risk_gate_debug(analysis, market, None, config, "REJECT", reason)
        return False, reason, 0.0

    if _confidence_rank(analysis.confidence) < _confidence_rank(config.min_confidence):
        reason = "Confidence below threshold"
        _log_rejection(reason, analysis, market, log_path)
        _emit_risk_gate_debug(analysis, market, None, config, "REJECT", reason)
        return False, reason, 0.0

    if analysis.decision.upper() == "SKIP":
        reason = "Claude returned SKIP"
        _log_rejection(reason, analysis, market, log_path)
        # SKIP path intentionally not debug-logged (handled inside helper)
        return False, reason, 0.0

    breakdown = compute_edge_breakdown(analysis.edge, analysis.decision, market)

    if breakdown["final_edge"] < config.min_edge:
        reason = "Insufficient edge"
        _log_rejection(reason, analysis, market, log_path, breakdown=breakdown)
        _emit_risk_gate_debug(analysis, market, breakdown, config, "REJECT", reason)
        return False, reason, 0.0

    if breakdown["kelly"] < config.min_kelly_fraction:
        reason = "Kelly fraction below minimum"
        _log_rejection(reason, analysis, market, log_path, breakdown=breakdown)
        _emit_risk_gate_debug(analysis, market, breakdown, config, "REJECT", reason)
        return False, reason, 0.0

    if (market.volume or 0.0) < config.min_volume_24h:
        reason = "Volume below minimum"
        _log_rejection(reason, analysis, market, log_path, breakdown=breakdown)
        _emit_risk_gate_debug(analysis, market, breakdown, config, "REJECT", reason)
        return False, reason, 0.0

    if portfolio.daily_pnl < -(config.daily_loss_cap_pct * portfolio.starting_balance):
        reason = "Daily loss cap hit"
        _log_rejection(reason, analysis, market, log_path)
        return False, reason, 0.0

    if portfolio.current_balance < portfolio.peak_balance * (1 - config.max_drawdown_pct):
        reason = "Max drawdown hit"
        _log_rejection(reason, analysis, market, log_path)
        return False, reason, 0.0

    if (market.volume or 0.0) < config.min_liquidity_usdc:
        reason = "Insufficient liquidity"
        _log_rejection(reason, analysis, market, log_path)
        return False, reason, 0.0

    market_price = market.yes_price
    if market_price is None or market_price <= 0.0 or market_price >= 1.0:
        reason = "Invalid market price"
        _log_rejection(reason, analysis, market, log_path)
        return False, reason, 0.0

    try:
        odds = 1.0 / market_price
        denominator = odds - 1.0
        if denominator <= 0.0:
            reason = "Invalid market odds"
            _log_rejection(reason, analysis, market, log_path)
            return False, reason, 0.0
        kelly = (analysis.probability * odds - 1.0) / denominator
    except ZeroDivisionError:
        reason = "Kelly sizing division by zero"
        _log_rejection(reason, analysis, market, log_path)
        return False, reason, 0.0

    if not math.isfinite(kelly):
        reason = "Invalid Kelly size"
        _log_rejection(reason, analysis, market, log_path)
        return False, reason, 0.0

    # Paper-mode sizing: honour BOT_PAPER_BANKROLL + BOT_MAX_POSITION_USDC
    # hard caps so simulated position sizing mirrors what live mode will do.
    paper_bankroll = float(os.getenv("BOT_PAPER_BANKROLL", "100") or "100")
    max_position_usdc = float(os.getenv("BOT_MAX_POSITION_USDC", "10") or "10")
    bankroll = paper_bankroll if portfolio.current_balance <= 0 else portfolio.current_balance

    approved_size = min(
        kelly * config.kelly_fraction * bankroll,
        config.max_position_pct * bankroll,
        max_position_usdc,  # absolute dollar cap
    )

    if approved_size < 1.0:
        reason = "Bet size below minimum"
        _log_rejection(reason, analysis, market, log_path)
        _emit_risk_gate_debug(analysis, market, breakdown, config, "REJECT", reason)
        return False, reason, 0.0

    logger.info(
        "position_sized market={} kelly={:.3f} bankroll={} position_usdc={:.2f}",
        getattr(market, "market_id", "?"),
        float(kelly),
        round(bankroll, 2),
        round(float(approved_size), 2),
    )
    _emit_risk_gate_debug(analysis, market, breakdown, config, "APPROVE", "APPROVED")
    return True, "APPROVED", round(approved_size, 6)
