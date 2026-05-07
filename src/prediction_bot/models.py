from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class MarketSnapshot:
    venue: str
    market_id: str
    question: str
    yes_price: float | None
    no_price: float | None
    spread: float | None
    volume: float | None
    liquidity: float | None
    expires_at: datetime | None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def time_to_expiry_days(self) -> float | None:
        if self.expires_at is None:
            return None
        delta = self.expires_at - utc_now()
        return delta.total_seconds() / 86400.0


@dataclass
class ScanCandidate:
    snapshot: MarketSnapshot
    opportunity_score: float
    anomaly_flags: list[str] = field(default_factory=list)
    # Populated by the runner after market_filter when the smart-money
    # layer is active. Typed as Any to avoid an import cycle into
    # prediction_bot.research.smart_money.
    smart_money: Any = None


@dataclass
class ResearchEvidence:
    source: str
    title: str
    summary: str
    url: str
    published_at: str | None


@dataclass
class ResearchSignal:
    sentiment_score: float
    confidence: float
    evidence_count: int
    highlights: list[str] = field(default_factory=list)


@dataclass
class TradeSignal:
    market: MarketSnapshot
    model_probability: float
    market_probability: float
    research_signal: ResearchSignal | None = None

    @property
    def edge(self) -> float:
        return self.model_probability - self.market_probability


@dataclass
class RiskDecision:
    approved: bool
    reasons: list[str]
    position_fraction: float
    edge: float
