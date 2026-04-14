from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from prediction_bot.clients.http import HttpClient
from prediction_bot.models import MarketSnapshot


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _parse_outcome_prices(raw: Any) -> list[float]:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
    elif isinstance(raw, list):
        parsed = raw
    else:
        return []

    prices: list[float] = []
    for item in parsed:
        try:
            prices.append(float(item))
        except (TypeError, ValueError):
            continue
    return prices


def _parse_outcomes(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
    elif isinstance(raw, list):
        parsed = raw
    else:
        return []
    return [str(x) for x in parsed]


class PolymarketClient:
    BASE_URL = "https://gamma-api.polymarket.com"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def geoblock_status(self) -> dict[str, Any] | None:
        payload = self.http.get_json("https://polymarket.com/api/geoblock")
        if isinstance(payload, dict):
            return payload
        return None

    def fetch_markets(self, limit: int = 200) -> list[MarketSnapshot]:
        payload = self.http.get_json(
            f"{self.BASE_URL}/markets",
            params={
                "limit": limit,
                "active": "true",
                "closed": "false",
                "order": "volume24hr",
                "ascending": "false",
            },
        )
        if not isinstance(payload, list):
            return []

        markets: list[MarketSnapshot] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            markets.append(self._normalize(item))
        return markets

    def _normalize(self, raw: dict[str, Any]) -> MarketSnapshot:
        outcomes = _parse_outcomes(raw.get("outcomes"))
        prices = _parse_outcome_prices(raw.get("outcomePrices"))

        yes_price = None
        no_price = None
        if len(outcomes) >= 2 and len(prices) >= 2:
            lowered = [o.lower() for o in outcomes]
            if "yes" in lowered and "no" in lowered:
                yes_idx = lowered.index("yes")
                no_idx = lowered.index("no")
                yes_price = prices[yes_idx]
                no_price = prices[no_idx]
            else:
                yes_price = prices[0]
                no_price = prices[1]

        spread = None
        best_bid = raw.get("bestBid")
        best_ask = raw.get("bestAsk")
        try:
            if best_bid is not None and best_ask is not None:
                spread = float(best_ask) - float(best_bid)
        except (TypeError, ValueError):
            spread = None

        volume = None
        for key in ("volumeNum", "volume", "volume24hr"):
            value = raw.get(key)
            if value is None:
                continue
            try:
                volume = float(value)
                break
            except (TypeError, ValueError):
                continue

        liquidity = None
        for key in ("liquidityNum", "liquidity"):
            value = raw.get(key)
            if value is None:
                continue
            try:
                liquidity = float(value)
                break
            except (TypeError, ValueError):
                continue

        return MarketSnapshot(
            venue="polymarket",
            market_id=str(raw.get("id", raw.get("market", "unknown"))),
            question=str(raw.get("question", raw.get("title", ""))),
            yes_price=yes_price,
            no_price=no_price,
            spread=spread,
            volume=volume,
            liquidity=liquidity,
            expires_at=_parse_datetime(raw.get("endDate") or raw.get("closedTime") or raw.get("end_date_iso")),
            raw=raw,
        )
