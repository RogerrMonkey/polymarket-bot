from __future__ import annotations

from prediction_bot.config import RiskSettings
from prediction_bot.models import RiskDecision, TradeSignal


def kelly_fraction_for_binary(probability: float, price: float) -> float:
    """Full Kelly fraction for a binary contract priced in [0, 1].

    Contract pays 1 on success and 0 on failure; cost is price.
    Net odds b is (1-price)/price.
    """
    if price <= 0.0 or price >= 1.0:
        return 0.0
    if probability <= 0.0 or probability >= 1.0:
        return 0.0

    b = (1.0 - price) / price
    q = 1.0 - probability
    raw = ((probability * b) - q) / b
    return max(0.0, raw)


class RiskEngine:
    def __init__(self, settings: RiskSettings) -> None:
        self.settings = settings

    def evaluate(
        self,
        signal: TradeSignal,
        current_exposure_fraction: float,
        daily_loss_fraction: float,
        max_drawdown_fraction: float,
        open_positions: int,
    ) -> RiskDecision:
        reasons: list[str] = []
        edge = signal.edge

        if edge <= self.settings.edge_threshold:
            reasons.append(f"edge_below_threshold:{edge:.4f}")

        if open_positions >= self.settings.max_concurrent_positions:
            reasons.append("too_many_open_positions")

        if daily_loss_fraction >= self.settings.max_daily_loss_fraction:
            reasons.append("daily_loss_limit_reached")

        if max_drawdown_fraction >= self.settings.max_drawdown_fraction:
            reasons.append("max_drawdown_limit_reached")

        full_kelly = kelly_fraction_for_binary(signal.model_probability, signal.market_probability)
        position_fraction = min(
            self.settings.max_position_fraction,
            full_kelly * self.settings.kelly_fraction,
        )

        if (current_exposure_fraction + position_fraction) > 1.0:
            reasons.append("portfolio_exposure_overflow")

        if position_fraction <= 0.0:
            reasons.append("non_positive_position_size")

        approved = len(reasons) == 0
        return RiskDecision(
            approved=approved,
            reasons=reasons,
            position_fraction=round(position_fraction, 6),
            edge=round(edge, 6),
        )
