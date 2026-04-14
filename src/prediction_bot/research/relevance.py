from __future__ import annotations

import re

PROJECT_TERMS = {
    "polymarket",
    "prediction",
    "market",
    "trading",
    "bot",
    "api",
    "clob",
    "orderbook",
    "liquidity",
    "settlement",
}

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how", "if", "in", "is", "it", "of", "on",
    "or", "that", "the", "this", "to", "was", "were", "will", "with", "would", "before", "after",
}


def _tokenize(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9\-]{2,}", text.lower())
    return {w for w in words if w not in STOPWORDS}


def relevance_score(market_question: str, evidence_text: str) -> float:
    market_tokens = _tokenize(market_question)
    evidence_tokens = _tokenize(evidence_text)

    if not evidence_tokens:
        return 0.0

    question_overlap = len(market_tokens.intersection(evidence_tokens)) / max(len(market_tokens), 1)
    project_overlap = len(PROJECT_TERMS.intersection(evidence_tokens)) / max(len(PROJECT_TERMS), 1)

    score = (0.7 * question_overlap) + (0.3 * project_overlap)
    return max(0.0, min(1.0, score))
