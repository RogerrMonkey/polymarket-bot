from __future__ import annotations

import re

POSITIVE_WORDS = {
    "approve",
    "approved",
    "bullish",
    "boost",
    "growth",
    "improve",
    "improved",
    "progress",
    "rebound",
    "record",
    "strong",
    "surge",
    "win",
    "upside",
}

NEGATIVE_WORDS = {
    "ban",
    "banned",
    "blocked",
    "breach",
    "crash",
    "decline",
    "deprecate",
    "deprecated",
    "downside",
    "drop",
    "lawsuit",
    "risk",
    "sanction",
    "shortfall",
    "weak",
}


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z][a-zA-Z\-']{2,}", text.lower())


def sentiment_score(text: str) -> float:
    words = _tokenize(text)
    if not words:
        return 0.0

    pos = sum(1 for w in words if w in POSITIVE_WORDS)
    neg = sum(1 for w in words if w in NEGATIVE_WORDS)

    if pos == 0 and neg == 0:
        return 0.0

    raw = (pos - neg) / max(pos + neg, 1)
    return max(-1.0, min(1.0, raw))
