from __future__ import annotations

from dataclasses import dataclass

from prediction_bot.clients.polymarket import PolymarketClient
from prediction_bot.config import AppConfig


@dataclass(frozen=True)
class PreflightStatus:
    checked: bool
    blocked: bool | None
    country: str | None
    region: str | None
    ip: str | None
    message: str


def run_preflight(config: AppConfig, polymarket: PolymarketClient | None) -> PreflightStatus:
    if not config.venue.enable_polymarket:
        return PreflightStatus(
            checked=False,
            blocked=None,
            country=None,
            region=None,
            ip=None,
            message="polymarket_disabled",
        )

    if polymarket is None:
        return PreflightStatus(
            checked=False,
            blocked=None,
            country=None,
            region=None,
            ip=None,
            message="polymarket_client_missing",
        )

    try:
        geo = polymarket.geoblock_status() or {}
        blocked = geo.get("blocked")
        country = str(geo.get("country")) if geo.get("country") is not None else None
        region = str(geo.get("region")) if geo.get("region") is not None else None
        ip = str(geo.get("ip")) if geo.get("ip") is not None else None

        if blocked is True:
            return PreflightStatus(
                checked=True,
                blocked=True,
                country=country,
                region=region,
                ip=ip,
                message="geoblock_blocked",
            )

        return PreflightStatus(
            checked=True,
            blocked=False,
            country=country,
            region=region,
            ip=ip,
            message="geoblock_pass",
        )
    except Exception as exc:  # noqa: BLE001
        return PreflightStatus(
            checked=False,
            blocked=None,
            country=None,
            region=None,
            ip=None,
            message=f"geoblock_check_failed:{exc}",
        )
