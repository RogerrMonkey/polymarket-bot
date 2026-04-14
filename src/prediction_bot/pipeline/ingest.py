from __future__ import annotations

from dataclasses import dataclass, field

from prediction_bot.clients.polymarket import PolymarketClient
from prediction_bot.models import MarketSnapshot


@dataclass
class IngestionResult:
    snapshots: list[MarketSnapshot] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class UnifiedIngestor:
    def __init__(self, polymarket: PolymarketClient | None) -> None:
        self.polymarket = polymarket

    def run(self, limit_per_venue: int = 200) -> IngestionResult:
        result = IngestionResult()

        if self.polymarket is not None:
            try:
                result.snapshots.extend(self.polymarket.fetch_markets(limit=limit_per_venue))
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"polymarket_error:{exc}")

        return result
