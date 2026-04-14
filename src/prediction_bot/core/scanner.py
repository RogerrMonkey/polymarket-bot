from __future__ import annotations

from math import exp

from prediction_bot.config import ScanSettings
from prediction_bot.models import MarketSnapshot, ScanCandidate


class MarketScanner:
    """Deterministic candidate scanner from liquidity, volume, spread, and expiry constraints."""

    def __init__(self, settings: ScanSettings) -> None:
        self.settings = settings

    def scan(self, snapshots: list[MarketSnapshot]) -> list[ScanCandidate]:
        candidates: list[ScanCandidate] = []

        for snap in snapshots:
            if not self._passes_gates(snap):
                continue
            score, flags = self._score(snap)
            candidates.append(ScanCandidate(snapshot=snap, opportunity_score=score, anomaly_flags=flags))

        candidates.sort(key=lambda c: c.opportunity_score, reverse=True)
        return candidates

    def _passes_gates(self, snap: MarketSnapshot) -> bool:
        if snap.volume is None or snap.volume < self.settings.min_volume:
            return False
        if snap.liquidity is None or snap.liquidity < self.settings.min_liquidity:
            return False
        if snap.time_to_expiry_days is not None and snap.time_to_expiry_days > self.settings.max_days_to_expiry:
            return False
        if snap.spread is not None and snap.spread > max(self.settings.max_spread * 2, 0.10):
            return False
        return True

    def _score(self, snap: MarketSnapshot) -> tuple[float, list[str]]:
        flags: list[str] = []

        spread = snap.spread or 0.0
        volume = snap.volume or 0.0
        liquidity = snap.liquidity or 0.0
        expiry_days = snap.time_to_expiry_days

        if spread > self.settings.max_spread:
            flags.append("wide_spread")
        if spread > 0.10:
            flags.append("very_wide_spread")

        if expiry_days is not None and expiry_days < 2.0:
            flags.append("near_expiry")

        # Smooth normalization to avoid hypersensitivity to outliers.
        volume_component = 1.0 - exp(-volume / 1200.0)
        liquidity_component = 1.0 - exp(-liquidity / 1200.0)
        spread_penalty = min(spread / max(self.settings.max_spread, 0.01), 3.0)

        urgency_bonus = 0.0
        if expiry_days is not None:
            urgency_bonus = max(0.0, min(1.0, (self.settings.max_days_to_expiry - expiry_days) / self.settings.max_days_to_expiry))

        score = (0.45 * volume_component) + (0.40 * liquidity_component) + (0.25 * urgency_bonus) - (0.30 * spread_penalty)
        return round(score, 4), flags
