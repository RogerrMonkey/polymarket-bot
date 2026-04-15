from __future__ import annotations

from pathlib import Path

from prediction_bot.clients.http import HttpClient
from prediction_bot.config import ResearchSettings
from prediction_bot.models import MarketSnapshot, ResearchEvidence, ResearchSignal
from prediction_bot.research.news_feed import get_relevant_news, sanitize_for_prompt
from prediction_bot.research.relevance import relevance_score
from prediction_bot.research.sentiment import sentiment_score
from prediction_bot.research.sources import ResearchSources, dedupe_evidence


class ResearchPipeline:
    def __init__(
        self,
        settings: ResearchSettings,
        http: HttpClient,
        workspace_root: Path,
    ) -> None:
        self.settings = settings
        self.workspace_root = workspace_root
        self.sources = ResearchSources(http)

    def gather_evidence(self) -> list[ResearchEvidence]:
        evidence: list[ResearchEvidence] = []

        long_term_path = self.workspace_root / "memory" / "long-term-memory.md"
        if long_term_path.exists():
            text = long_term_path.read_text(encoding="utf-8", errors="ignore")
            evidence.extend(self.sources.from_memory_new_learnings(text))

        news_items = get_relevant_news(
            http=self.sources.http,
            gdelt_query=self.settings.gdelt_query,
            min_relevance=self.settings.news_min_relevance,
            max_age_minutes=self.settings.news_max_age_minutes,
        )
        for item in news_items:
            evidence.append(
                ResearchEvidence(
                    source=f"news:{item.source}",
                    title=item.title,
                    summary=sanitize_for_prompt(item.raw_text),
                    url=item.url,
                    published_at=item.published_at.isoformat(),
                )
            )

        if self.settings.include_legacy_social_sources:
            evidence.extend(self.sources.web_search(self.settings.web_query, limit=10))
            evidence.extend(self.sources.reddit_search(self.settings.reddit_query, limit=10))
            evidence.extend(self.sources.hacker_news_search(self.settings.hn_query, limit=10))

        deduped = dedupe_evidence(evidence)
        return deduped[: self.settings.max_evidence_items]

    def analyze_markets(self, markets: list[MarketSnapshot]) -> dict[str, ResearchSignal]:
        evidence = self.gather_evidence()
        signals: dict[str, ResearchSignal] = {}

        for market in markets:
            weighted_sentiment = 0.0
            total_weight = 0.0
            highlights: list[str] = []
            used = 0

            for item in evidence:
                text = f"{item.title} {item.summary}"
                rel = relevance_score(market.question, text)
                if rel < self.settings.min_relevance:
                    continue

                sent = sentiment_score(text)
                weighted_sentiment += sent * rel
                total_weight += rel
                used += 1

                if len(highlights) < 3:
                    highlights.append(f"{item.source}:{item.title[:80]}")

            if used == 0:
                signals[market.market_id] = ResearchSignal(
                    sentiment_score=0.0,
                    confidence=0.0,
                    evidence_count=0,
                    highlights=[],
                )
                continue

            avg_sentiment = weighted_sentiment / max(total_weight, 1e-9)
            confidence = min(1.0, (used / 8.0) * 0.5 + min(total_weight, 1.0) * 0.5)

            signals[market.market_id] = ResearchSignal(
                sentiment_score=round(max(-1.0, min(1.0, avg_sentiment)), 4),
                confidence=round(confidence, 4),
                evidence_count=used,
                highlights=highlights,
            )

        return signals
