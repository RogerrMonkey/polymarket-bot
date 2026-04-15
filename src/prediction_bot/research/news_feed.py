from __future__ import annotations

import hashlib
import html
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from loguru import logger

from prediction_bot.clients.http import HttpClient

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

# Default free RSS feeds (business + crypto). Overridable via BOT_RSS_FEEDS
# as a comma-separated list of URLs.
DEFAULT_RSS_FEEDS: tuple[str, ...] = (
    "https://feeds.reuters.com/reuters/businessNews",
    "https://rss.cnn.com/rss/money_news_international.rss",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
)

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


def _resolve_rss_feeds(override: list[str] | None = None) -> list[str]:
    if override:
        return list(override)
    env_value = os.getenv("BOT_RSS_FEEDS", "").strip()
    if env_value:
        urls = [u.strip() for u in env_value.split(",") if u.strip()]
        if urls:
            return urls
    return list(DEFAULT_RSS_FEEDS)


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


def _parse_rss_entry_datetime(entry: Any) -> datetime | None:
    parsed = None
    try:
        parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    except (AttributeError, TypeError):
        parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if parsed is not None:
        try:
            return datetime(*parsed[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
    try:
        raw = entry.get("published") or entry.get("updated")
    except (AttributeError, TypeError):
        raw = getattr(entry, "published", None) or getattr(entry, "updated", None)
    return _parse_datetime(str(raw)) if raw else None


class RSSFetcher:
    """Parse free RSS feeds via the feedparser library.

    Default feeds cover business + crypto and require no API key. Override the
    list via the BOT_RSS_FEEDS env var (comma-separated URLs) or an explicit
    feed_urls argument.
    """

    def __init__(self, http: HttpClient, feed_urls: list[str] | None = None) -> None:
        self.http = http
        self.feed_urls = _resolve_rss_feeds(feed_urls)
        self._seen_hashes: set[str] = set()

    def fetch_once(self, limit: int = 30) -> list[NewsItem]:
        try:
            import feedparser
        except ImportError:
            logger.warning("feedparser_not_installed — RSS news ingestion disabled")
            return []

        out: list[NewsItem] = []
        for url in self.feed_urls:
            if len(out) >= limit:
                break
            try:
                parsed = feedparser.parse(url)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"rss_fetch_failed url={url} error={exc}")
                continue

            feed_obj = getattr(parsed, "feed", None)
            try:
                feed_title = feed_obj.get("title", "") if feed_obj is not None else ""
            except (AttributeError, TypeError):
                feed_title = getattr(feed_obj, "title", "") if feed_obj is not None else ""
            source_name = f"rss:{(feed_title or url)[:40]}"

            entries = getattr(parsed, "entries", None) or []
            for entry in entries:
                if len(out) >= limit:
                    break
                try:
                    title = str(entry.get("title", "") or "").strip()
                    link = str(entry.get("link", "") or "").strip()
                    summary = str(entry.get("summary", "") or "")
                except (AttributeError, TypeError):
                    title = str(getattr(entry, "title", "") or "").strip()
                    link = str(getattr(entry, "link", "") or "").strip()
                    summary = str(getattr(entry, "summary", "") or "")

                if not title or not link:
                    continue

                link_hash = hashlib.sha256(link.encode("utf-8", errors="ignore")).hexdigest()
                if link_hash in self._seen_hashes:
                    continue
                self._seen_hashes.add(link_hash)

                published_at = _parse_rss_entry_datetime(entry) or _utc_now()
                raw_text = f"{title} {summary}".strip()
                out.append(
                    NewsItem(
                        title=title,
                        source=source_name,
                        url=link,
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
    gdelt_query: str = "bitcoin",
    min_relevance: float = 0.4,
    max_age_minutes: int = 30,
    rss_feeds: list[str] | None = None,
) -> list[NewsItem]:
    gdelt_items = GDELTFetcher(http=http, query=gdelt_query).fetch_once(limit=10)
    rss_items = RSSFetcher(http=http, feed_urls=rss_feeds).fetch_once(limit=30)

    now = _utc_now()
    min_time = now - timedelta(minutes=max_age_minutes)
    all_items = _dedupe_news([*gdelt_items, *rss_items])

    filtered = [
        item
        for item in all_items
        if item.relevance_score >= min_relevance and item.published_at >= min_time
    ]

    filtered.sort(key=lambda x: (x.relevance_score, x.published_at), reverse=True)
    return filtered
