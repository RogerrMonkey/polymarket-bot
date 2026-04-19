"""Pre-analyst market filter.

Removes low-signal markets before they reach the LLM analyst, saving
Groq API calls on markets where no meaningful edge is possible.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Iterable

from loguru import logger

from prediction_bot.models import ScanCandidate
from prediction_bot.risk_engine import RiskConfig


FILTER_REASONS = (
    "low_volume",
    "resolves_too_soon",
    "too_far_out",
    "near_certain_price",
    "malformed_question",
)


def _days_remaining(expires_at) -> float | None:
    if expires_at is None:
        return None
    if not isinstance(expires_at, datetime):
        return None
    delta = expires_at - datetime.now(timezone.utc)
    return delta.total_seconds() / 86400.0


def _evaluate(candidate: ScanCandidate, config: RiskConfig) -> str | None:
    """Return the first filter reason the candidate fails, or None to keep."""
    snap = candidate.snapshot

    volume = snap.volume
    if volume is None or volume < config.min_volume_24h:
        return "low_volume"

    days = _days_remaining(snap.expires_at)
    if days is not None:
        if days < 2.0:
            return "resolves_too_soon"
        if days > 180.0:
            return "too_far_out"

    yes_price = snap.yes_price
    if yes_price is not None:
        if yes_price < 0.02 or yes_price > 0.98:
            return "near_certain_price"

    question = (snap.question or "").strip()
    if len(question) < 20:
        return "malformed_question"

    return None


def filter_markets(
    candidates: Iterable[ScanCandidate],
    config: RiskConfig,
) -> list[ScanCandidate]:
    """Return the candidates that survive all quality filters.

    Emits a single INFO log line summarizing the pass/filter breakdown
    so the operator can tune thresholds without digging through history.
    """
    passed: list[ScanCandidate] = []
    reasons: Counter[str] = Counter()
    total = 0

    for candidate in candidates:
        total += 1
        reason = _evaluate(candidate, config)
        if reason is None:
            passed.append(candidate)
        else:
            reasons[reason] += 1

    filtered = total - len(passed)
    reason_str = ",".join(f"{k}={v}" for k, v in reasons.most_common()) or "none"
    logger.info(
        "market_filter: {total} in \u2192 {passed} passed ({filtered} filtered: {reasons})",
        total=total,
        passed=len(passed),
        filtered=filtered,
        reasons=reason_str,
    )
    return passed
