from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

from prediction_bot.clients.http import HttpClient

CRYPTOPANIC_URL = "https://cryptopanic.com/api/v1/posts/"
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

HIGH_RELEVANCE_KEYWORDS = ["btc", "bitcoin", "price", "etf", "fed", "rate", "sec"]

BULLISH_KEYWORDS = {
    "rally",
    "breakout",
    "surge",
    "gain",
    "approval",
    "inflow",
    "adoption",
    "bullish",
    "up",
}

BEARISH_KEYWORDS = {
    "selloff",
    "drop",
    "ban",
    "lawsuit",
    "hack",
    "outflow",
    "bearish",
    "down",
    "risk",
}


@dataclass(frozen=True)
class NewsItem:
    title: str
    source: str
    url: str
    published_at: datetime
    raw_text: str
    relevance_score: float
    sentiment: str
    market_tags: list[str] = field(default_factory=list)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _keyword_relevance(text: str) -> float:
    lowered = text.lower()
    seen: set[str] = set()
    score = 0.0
    for keyword in HIGH_RELEVANCE_KEYWORDS:
        if keyword in lowered and keyword not in seen:
            seen.add(keyword)
            score += 0.2
    return min(1.0, score)


def _classify_sentiment(text: str) -> str:
    lowered = text.lower()
    bullish = sum(1 for w in BULLISH_KEYWORDS if w in lowered)
    bearish = sum(1 for w in BEARISH_KEYWORDS if w in lowered)
    if bullish > bearish:
        return "bullish"
    if bearish > bullish:
        return "bearish"
    return "neutral"


def _market_tags(text: str) -> list[str]:
    lowered = text.lower()
    tags: list[str] = []
    if "btc" in lowered or "bitcoin" in lowered:
        tags.append("btc")
    if "eth" in lowered or "ethereum" in lowered:
        tags.append("eth")
    if "fed" in lowered or "rate" in lowered:
        tags.append("macro")
    return tags


def sanitize_for_prompt(text: str) -> str:
    patterns = [
        r"\bignore\b",
        r"\bdisregard\b",
        r"\bforget\b",
        r"\bnew\s+instructions\b",
        r"\bsystem\s*:",
    ]
    cut_index = len(text)

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match is not None:
            cut_index = min(cut_index, match.start())

    sanitized = text[:cut_index]
    sanitized = re.sub(r"\s+", " ", sanitized).strip()
    if len(sanitized) > 500:
        sanitized = sanitized[:500]

    # Escape XML/JSON-sensitive characters and keep external data fenced semantically.
    escaped = html.escape(sanitized, quote=True)
    escaped = escaped.replace("{", "\\{").replace("}", "\\}")
    return f"[EXTERNAL_DATA] {escaped}"


class CryptoPanicFetcher:
    def __init__(self, http: HttpClient, api_token: str) -> None:
        self.http = http
        self.api_token = api_token
        self._seen_hashes: set[str] = set()

    def fetch_once(self, limit: int = 20) -> list[NewsItem]:
        try:
            payload = self.http.get_json(
                CRYPTOPANIC_URL,
                params={
                    "auth_token": self.api_token,
                    "kind": "news",
                    "currencies": "BTC,ETH",
                    "public": "true",
                },
            )
        except Exception:  # noqa: BLE001
            return []

        items = payload.get("results", []) if isinstance(payload, dict) else []
        out: list[NewsItem] = []

        for item in items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            if not url or not title:
                continue

            url_hash = hashlib.sha256(url.encode("utf-8", errors="ignore")).hexdigest()
            if url_hash in self._seen_hashes:
                continue
            self._seen_hashes.add(url_hash)

            published_at = _parse_datetime(str(item.get("published_at") or "")) or _utc_now()
            source_obj = item.get("source") if isinstance(item.get("source"), dict) else {}
            source_name = str(source_obj.get("title") or "cryptopanic")
            body = str(item.get("slug") or "")
            raw_text = f"{title} {body}".strip()
            out.append(
                NewsItem(
                    title=title,
                    source=source_name,
                    url=url,
                    published_at=published_at,
                    raw_text=raw_text,
                    relevance_score=_keyword_relevance(raw_text),
                    sentiment=_classify_sentiment(raw_text),
                    market_tags=_market_tags(raw_text),
                )
            )
            if len(out) >= limit:
                break

        return out


class GDELTFetcher:
    def __init__(self, http: HttpClient, query: str = "bitcoin") -> None:
        self.http = http
        self.query = query
        self._seen_hashes: set[str] = set()

    def fetch_once(self, limit: int = 10) -> list[NewsItem]:
        try:
            payload = self.http.get_json(
                GDELT_URL,
                params={
                    "query": self.query,
                    "mode": "artlist",
                    "format": "json",
                    "maxrecords": str(limit),
                },
            )
        except Exception:  # noqa: BLE001
            return []

        items = payload.get("articles", []) if isinstance(payload, dict) else []
        out: list[NewsItem] = []

        for item in items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            if not url or not title:
                continue

            url_hash = hashlib.sha256(url.encode("utf-8", errors="ignore")).hexdigest()
            if url_hash in self._seen_hashes:
                continue
            self._seen_hashes.add(url_hash)

            published_at = _parse_datetime(str(item.get("seendate") or "")) or _utc_now()
            source_name = str(item.get("sourceCountry") or "gdelt")
            raw_text = f"{title} {item.get('domain', '')}".strip()
            out.append(
                NewsItem(
                    title=title,
                    source=source_name,
                    url=url,
                    published_at=published_at,
                    raw_text=raw_text,
                    relevance_score=_keyword_relevance(raw_text),
                    sentiment=_classify_sentiment(raw_text),
                    market_tags=_market_tags(raw_text),
                )
            )

        return out


def _dedupe_news(items: Iterable[NewsItem]) -> list[NewsItem]:
    seen = set()
    out: list[NewsItem] = []
    for item in items:
        key = (item.url.lower(), item.title.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def get_relevant_news(
    http: HttpClient,
    cryptopanic_api_token: str,
    gdelt_query: str = "bitcoin",
    min_relevance: float = 0.4,
    max_age_minutes: int = 30,
) -> list[NewsItem]:
    cp = CryptoPanicFetcher(http=http, api_token=cryptopanic_api_token)
    gdelt = GDELTFetcher(http=http, query=gdelt_query)

    now = _utc_now()
    min_time = now - timedelta(minutes=max_age_minutes)
    all_items = _dedupe_news([*cp.fetch_once(limit=30), *gdelt.fetch_once(limit=10)])

    filtered = [
        item
        for item in all_items
        if item.relevance_score >= min_relevance and item.published_at >= min_time
    ]

    filtered.sort(key=lambda x: (x.relevance_score, x.published_at), reverse=True)
    return filtered
