from __future__ import annotations

import html
import json
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Iterable

from prediction_bot.clients.http import HttpClient
from prediction_bot.models import ResearchEvidence


def _normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _ddg_decode_url(raw: str) -> str:
    if raw.startswith("//"):
        raw = "https:" + raw
    if "duckduckgo.com/l/?" in raw:
        parsed = urllib.parse.urlparse(raw)
        query = urllib.parse.parse_qs(parsed.query)
        if "uddg" in query and query["uddg"]:
            return urllib.parse.unquote(query["uddg"][0])
    return raw


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_text(value: str | None, limit: int = 240) -> str:
    if not value:
        return ""
    text = _normalize_space(value)
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


class ResearchSources:
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def from_memory_new_learnings(self, markdown_text: str) -> list[ResearchEvidence]:
        lines = markdown_text.splitlines()
        out: list[ResearchEvidence] = []
        in_section = False

        for line in lines:
            if line.startswith("## "):
                in_section = line.strip().lower() == "## new_learnings"
                continue
            if not in_section:
                continue
            if "source:" not in line:
                continue

            url_match = re.search(r"source:\s+(https?://[^\s\|]+)", line)
            note_match = re.search(r"note:\s+(.+)$", line)
            via_match = re.search(r"via:\s+([^\|]+)", line)
            if not url_match:
                continue

            url = url_match.group(1).strip()
            summary = note_match.group(1).strip() if note_match else ""
            source = (via_match.group(1).strip() if via_match else "memory").lower()

            out.append(
                ResearchEvidence(
                    source=f"memory:{source}",
                    title="memory_new_learning",
                    summary=_safe_text(summary, 300),
                    url=url,
                    published_at=None,
                )
            )

        return out

    def web_search(self, query: str, limit: int = 6) -> list[ResearchEvidence]:
        try:
            response = self.http.session.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query},
                timeout=self.http.timeout_seconds,
            )
            response.raise_for_status()
            text = response.text
        except Exception:  # noqa: BLE001
            return []

        pattern = re.compile(
            r'<a[^>]*class="result__a"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
            re.IGNORECASE,
        )

        out: list[ResearchEvidence] = []
        for match in pattern.finditer(text):
            href = _ddg_decode_url(html.unescape(match.group("href")))
            if not href.startswith("http"):
                continue
            title = _normalize_space(html.unescape(re.sub(r"<[^>]+>", "", match.group("title"))))
            out.append(
                ResearchEvidence(
                    source="web",
                    title=_safe_text(title),
                    summary="web search result",
                    url=href,
                    published_at=None,
                )
            )
            if len(out) >= limit:
                break

        return out

    def reddit_search(self, query: str, limit: int = 6) -> list[ResearchEvidence]:
        scoped = f"{query} (polymarket OR \"prediction market\")"
        try:
            payload = self.http.get_json(
                "https://www.reddit.com/search.json",
                params={"q": scoped, "sort": "new", "t": "day", "limit": str(limit)},
            )
        except Exception:  # noqa: BLE001
            return []

        items = payload.get("data", {}).get("children", []) if isinstance(payload, dict) else []

        out: list[ResearchEvidence] = []
        for item in items:
            data = item.get("data", {}) if isinstance(item, dict) else {}
            permalink = data.get("permalink")
            if not permalink:
                continue
            out.append(
                ResearchEvidence(
                    source="reddit",
                    title=_safe_text(str(data.get("title", ""))),
                    summary=_safe_text(str(data.get("selftext", "")), 280) or "reddit discussion",
                    url="https://www.reddit.com" + str(permalink),
                    published_at=_utc_now_iso(),
                )
            )

        return out

    def hacker_news_search(self, query: str, limit: int = 6) -> list[ResearchEvidence]:
        try:
            payload = self.http.get_json(
                "https://hn.algolia.com/api/v1/search_by_date",
                params={"query": query, "tags": "story", "hitsPerPage": str(limit)},
            )
        except Exception:  # noqa: BLE001
            return []

        hits = payload.get("hits", []) if isinstance(payload, dict) else []
        out: list[ResearchEvidence] = []

        for hit in hits:
            if not isinstance(hit, dict):
                continue
            url = str(hit.get("url") or "")
            title = str(hit.get("title") or "")
            if not url or not title:
                continue
            out.append(
                ResearchEvidence(
                    source="hacker_news",
                    title=_safe_text(title),
                    summary=_safe_text(str(hit.get("story_text") or "hacker news story"), 280),
                    url=url,
                    published_at=str(hit.get("created_at") or None),
                )
            )

        return out


def dedupe_evidence(items: Iterable[ResearchEvidence]) -> list[ResearchEvidence]:
    seen = set()
    out: list[ResearchEvidence] = []

    for item in items:
        key = (item.url.lower(), item.title.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(item)

    return out
