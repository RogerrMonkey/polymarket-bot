from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


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
        return False, reason, 0.0

    if _confidence_rank(analysis.confidence) < _confidence_rank(config.min_confidence):
        reason = "Confidence below threshold"
        _log_rejection(reason, analysis, market, log_path)
        return False, reason, 0.0

    if analysis.edge < config.min_edge:
        reason = "Insufficient edge"
        _log_rejection(reason, analysis, market, log_path)
        return False, reason, 0.0

    if analysis.decision.upper() == "SKIP":
        reason = "Claude returned SKIP"
        _log_rejection(reason, analysis, market, log_path)
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

    approved_size = min(
        kelly * config.kelly_fraction * portfolio.current_balance,
        config.max_position_pct * portfolio.current_balance,
    )

    if approved_size < 1.0:
        reason = "Bet size below minimum"
        _log_rejection(reason, analysis, market, log_path)
        return False, reason, 0.0

    return True, "APPROVED", round(approved_size, 6)
