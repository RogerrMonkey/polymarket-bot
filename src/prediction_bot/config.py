from __future__ import annotations

import os
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass(frozen=True)
class ScanSettings:
    min_volume: float = _env_float("BOT_MIN_VOLUME", 200.0)
    min_liquidity: float = _env_float("BOT_MIN_LIQUIDITY", 200.0)
    max_days_to_expiry: int = _env_int("BOT_MAX_DAYS_TO_EXPIRY", 30)
    max_spread: float = _env_float("BOT_MAX_SPREAD", 0.05)


@dataclass(frozen=True)
class RiskSettings:
    edge_threshold: float = _env_float("BOT_EDGE_THRESHOLD", 0.04)
    max_position_fraction: float = _env_float("BOT_MAX_POSITION_FRACTION", 0.05)
    max_concurrent_positions: int = _env_int("BOT_MAX_CONCURRENT_POSITIONS", 15)
    max_drawdown_fraction: float = _env_float("BOT_MAX_DRAWDOWN_FRACTION", 0.08)
    max_daily_loss_fraction: float = _env_float("BOT_MAX_DAILY_LOSS_FRACTION", 0.15)
    kelly_fraction: float = _env_float("BOT_KELLY_FRACTION", 0.25)


@dataclass(frozen=True)
class RuntimeSettings:
    request_timeout_seconds: float = _env_float("BOT_REQUEST_TIMEOUT", 20.0)
    user_agent: str = os.getenv("BOT_USER_AGENT", "prediction-bot/0.1")


@dataclass(frozen=True)
class ResearchSettings:
    enabled: bool = _env_bool("BOT_RESEARCH_ENABLED", True)
    max_evidence_items: int = _env_int("BOT_RESEARCH_MAX_ITEMS", 30)
    min_relevance: float = _env_float("BOT_RESEARCH_MIN_RELEVANCE", 0.05)
    cryptopanic_api_token: str = os.getenv("BOT_CRYPTOPANIC_API_TOKEN", "FREE")
    gdelt_query: str = os.getenv("BOT_GDELT_QUERY", "bitcoin")
    news_min_relevance: float = _env_float("BOT_NEWS_MIN_RELEVANCE", 0.4)
    news_max_age_minutes: int = _env_int("BOT_NEWS_MAX_AGE_MINUTES", 30)
    include_legacy_social_sources: bool = _env_bool("BOT_INCLUDE_LEGACY_SOCIAL", False)
    web_query: str = os.getenv("BOT_RESEARCH_WEB_QUERY", "polymarket prediction market api update")
    reddit_query: str = os.getenv("BOT_RESEARCH_REDDIT_QUERY", "polymarket prediction market")
    hn_query: str = os.getenv("BOT_RESEARCH_HN_QUERY", "prediction market api")


@dataclass(frozen=True)
class CalibrationSettings:
    enabled: bool = _env_bool("BOT_CALIBRATION_ENABLED", True)
    base_shrink: float = _env_float("BOT_CALIBRATION_BASE_SHRINK", 0.35)
    confidence_weight: float = _env_float("BOT_CALIBRATION_CONF_WEIGHT", 0.45)
    evidence_weight: float = _env_float("BOT_CALIBRATION_EVIDENCE_WEIGHT", 0.03)
    max_evidence_for_weight: int = _env_int("BOT_CALIBRATION_MAX_EVIDENCE", 12)
    spread_penalty_weight: float = _env_float("BOT_CALIBRATION_SPREAD_PENALTY", 0.45)


@dataclass(frozen=True)
class ClaudeSettings:
    enabled: bool = _env_bool("BOT_CLAUDE_ENABLED", False)
    model: str = os.getenv("BOT_CLAUDE_MODEL", "claude-sonnet-4-6")
    web_search_max: int = _env_int("BOT_CLAUDE_WEB_SEARCH_MAX", 2)
    daily_budget_usd: float = _env_float("BOT_CLAUDE_DAILY_BUDGET", 2.0)


@dataclass(frozen=True)
class StorageSettings:
    db_path: str = os.getenv("BOT_DB_PATH", "data/predictions.db")


@dataclass(frozen=True)
class VenueSettings:
    enable_polymarket: bool = _env_bool("BOT_ENABLE_POLYMARKET", True)


@dataclass(frozen=True)
class ComplianceSettings:
    enforce_geoblock_gate: bool = _env_bool("BOT_ENFORCE_GEOBLOCK_GATE", True)


@dataclass(frozen=True)
class AppConfig:
    scan: ScanSettings = ScanSettings()
    risk: RiskSettings = RiskSettings()
    runtime: RuntimeSettings = RuntimeSettings()
    research: ResearchSettings = ResearchSettings()
    calibration: CalibrationSettings = CalibrationSettings()
    claude: ClaudeSettings = ClaudeSettings()
    storage: StorageSettings = StorageSettings()
    venue: VenueSettings = VenueSettings()
    compliance: ComplianceSettings = ComplianceSettings()


def load_config() -> AppConfig:
    return AppConfig()
