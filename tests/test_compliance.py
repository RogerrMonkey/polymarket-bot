from prediction_bot.config import AppConfig, ComplianceSettings, VenueSettings
from prediction_bot.pipeline.compliance import run_preflight


class _FakePolymarketClient:
    def __init__(self, blocked: bool) -> None:
        self._blocked = blocked

    def geoblock_status(self) -> dict[str, object]:
        return {
            "blocked": self._blocked,
            "country": "IN",
            "region": "MH",
            "ip": "203.0.113.42",
        }


def _config(enforce: bool = True) -> AppConfig:
    return AppConfig(
        venue=VenueSettings(enable_polymarket=True),
        compliance=ComplianceSettings(enforce_geoblock_gate=enforce),
    )


def test_preflight_blocked_status() -> None:
    status = run_preflight(config=_config(), polymarket=_FakePolymarketClient(blocked=True))
    assert status.checked is True
    assert status.blocked is True
    assert status.country == "IN"


def test_preflight_pass_status() -> None:
    status = run_preflight(config=_config(), polymarket=_FakePolymarketClient(blocked=False))
    assert status.checked is True
    assert status.blocked is False
