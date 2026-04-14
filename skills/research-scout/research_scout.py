#!/usr/bin/env python3
"""Research scout: discover novelty and stage validated findings in long-term memory."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

import requests

STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "your", "about", "have", "has", "been", "will",
    "are", "was", "were", "would", "could", "should", "their", "there", "where", "when", "what", "which", "using",
    "update", "new", "more", "than", "over", "under", "into", "after", "before", "between", "through", "across",
}

CONTEXT_TERMS = {
    "polymarket", "kalshi", "prediction", "trading", "bot", "agent", "workflow", "automation",
    "clob", "orderbook", "api", "llm", "risk", "kelly", "drawdown", "backtest",
}

BLOCKED_DOMAINS = {
    "www-.quora.com",
    "adultszone.quora.com",
}

CONTRADICTION_TERMS = {
    "deprecated", "deprecating", "sunset", "breaking", "no longer", "restriction", "blocked", "ban", "compliance",
    "policy change", "terms update", "rate limit", "migration required", "mandatory", "enforcement",
}

DEFAULT_QUERIES = [
    "Polymarket API update",
    "Kalshi API update",
    "prediction market bot strategy",
    "AI trading workflow automation",
    "LLM agent engineering workflow",
]

USER_AGENT = "research-scout/1.0 (+memory-maintenance)"


@dataclass
class Finding:
    source: str
    title: str
    url: str
    snippet: str
    published: str


@dataclass
class ValidatedFinding:
    timestamp: str
    url: str
    note: str
    source: str


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def ts_string(ts: dt.datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M UTC")


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def domain_from_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
        return (parsed.netloc or "").lower()
    except ValueError:
        return ""


def url_allowed(url: str) -> bool:
    domain = domain_from_url(url)
    if not domain:
        return False
    if domain in BLOCKED_DOMAINS:
        return False
    if domain.endswith("duckduckgo.com"):
        return False
    return True


def tokenize(text: str) -> Set[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_\-]{2,}", text.lower())
    return {w for w in words if w not in STOPWORDS}


def contradiction_signal(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in CONTRADICTION_TERMS)


def relevance_score(text: str) -> int:
    tokens = tokenize(text)
    return len(tokens.intersection(CONTEXT_TERMS))


def read_text_if_exists(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def collect_reference_corpus(workspace_root: Path) -> Tuple[str, Set[str]]:
    docs = []
    for rel in [
        "plan.md",
        "GPT.md",
        "memory/recent-memory.md",
        "memory/long-term-memory.md",
        "memory/project-memory.md",
    ]:
        docs.append(read_text_if_exists(workspace_root / rel))

    corpus = "\n".join(docs)
    urls = set(re.findall(r"https?://[^\s\)\]\|]+", corpus))
    return corpus, urls


def ddg_decode_url(raw: str) -> str:
    if raw.startswith("//"):
        raw = "https:" + raw
    if "duckduckgo.com/l/?" in raw:
        parsed = urllib.parse.urlparse(raw)
        query = urllib.parse.parse_qs(parsed.query)
        if "uddg" in query and query["uddg"]:
            return urllib.parse.unquote(query["uddg"][0])
    return raw


def fetch_ddg(query: str, limit: int = 8) -> List[Finding]:
    url = "https://html.duckduckgo.com/html/"
    try:
        resp = requests.post(url, data={"q": query}, headers={"User-Agent": USER_AGENT}, timeout=20)
        resp.raise_for_status()
    except requests.RequestException:
        return []

    text = resp.text
    pattern = re.compile(
        r'<a[^>]*class="result__a"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
        re.IGNORECASE,
    )

    findings: List[Finding] = []
    for match in pattern.finditer(text):
        href = ddg_decode_url(html.unescape(match.group("href")))
        title = normalize_space(html.unescape(re.sub(r"<[^>]+>", "", match.group("title"))))
        if not href.startswith("http"):
            continue
        findings.append(Finding("web", title[:220], href, "", ""))
        if len(findings) >= limit:
            break
    return findings


def fetch_reddit(query: str, limit: int = 8) -> List[Finding]:
    endpoint = "https://www.reddit.com/search.json"
    scoped = f"{query} (polymarket OR kalshi OR \"prediction market\" OR trading bot OR llm agent)"
    params = {"q": scoped, "sort": "new", "t": "day", "limit": str(limit)}
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(endpoint, params=params, headers=headers, timeout=20)
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, json.JSONDecodeError):
        return []

    items = payload.get("data", {}).get("children", [])
    out: List[Finding] = []
    for item in items:
        data = item.get("data", {})
        permalink = data.get("permalink", "")
        if not permalink:
            continue
        url = "https://www.reddit.com" + permalink
        title = normalize_space(data.get("title", ""))
        snippet = normalize_space(data.get("selftext", ""))[:300] or f"Reddit post in r/{data.get('subreddit', 'unknown')}"
        created = data.get("created_utc")
        published = ""
        if isinstance(created, (int, float)):
            published = ts_string(dt.datetime.fromtimestamp(created, tz=dt.timezone.utc))
        out.append(Finding("reddit", title[:220], url, snippet, published))
    return out


def fetch_hn(query: str, hours: int, limit: int = 8) -> List[Finding]:
    endpoint = "https://hn.algolia.com/api/v1/search_by_date"
    cutoff = int((now_utc() - dt.timedelta(hours=hours)).timestamp())
    params = {
        "query": query,
        "tags": "story",
        "numericFilters": f"created_at_i>{cutoff}",
        "hitsPerPage": str(limit),
    }
    try:
        resp = requests.get(endpoint, params=params, headers={"User-Agent": USER_AGENT}, timeout=20)
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, json.JSONDecodeError):
        return []

    out: List[Finding] = []
    for hit in payload.get("hits", []):
        url = hit.get("url") or ""
        title = normalize_space(hit.get("title") or "")
        if not url or not title:
            continue
        snippet = normalize_space(hit.get("story_text") or "")[:300] or "Hacker News story"
        published = hit.get("created_at") or ""
        out.append(Finding("hacker_news", title[:220], url, snippet, published))
    return out


def fetch_quora(query: str, limit: int = 6) -> List[Finding]:
    out: List[Finding] = []
    for f in fetch_ddg(f"site:quora.com {query}", limit=limit):
        domain = domain_from_url(f.url)
        if not domain.endswith("quora.com"):
            continue
        text = f"{f.title} {f.snippet}"
        if relevance_score(text) < 2:
            continue
        out.append(Finding("quora", f.title, f.url, f.snippet, f.published))
    return out


def dedupe_findings(findings: Iterable[Finding]) -> List[Finding]:
    seen = set()
    out = []
    for f in findings:
        if not url_allowed(f.url):
            continue
        key = (f.url.lower(), f.title.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def build_note(f: Finding) -> str:
    source_text = f"{f.title} {f.snippet}"
    snippet = normalize_space(source_text)
    if len(snippet) > 140:
        snippet = snippet[:137] + "..."
    if contradiction_signal(source_text):
        return f"Potential contradiction/update for project assumptions: {snippet}"

    area = "workflow"
    lowered = source_text.lower()
    if "api" in lowered or "endpoint" in lowered or "rate limit" in lowered:
        area = "api"
    elif "strategy" in lowered or "edge" in lowered or "backtest" in lowered:
        area = "strategy"
    elif "tool" in lowered or "sdk" in lowered or "release" in lowered:
        area = "tooling"

    return f"Adds {area} context relevant to the project: {snippet}"


def novelty_check(f: Finding, corpus: str, existing_urls: Set[str], staged_urls: Set[str]) -> bool:
    if f.url in existing_urls or f.url in staged_urls:
        return False

    text = f"{f.title} {f.snippet}"
    if relevance_score(text) < 2 and not contradiction_signal(text):
        return False

    if "reddit post in r/" in f.snippet.lower() and relevance_score(f.title) < 2:
        return False

    tokens = tokenize(text)
    if not tokens:
        return False

    corpus_tokens = tokenize(corpus)
    overlap = len(tokens.intersection(corpus_tokens)) / max(len(tokens), 1)

    if overlap >= 0.78 and not contradiction_signal(text):
        return False

    return True


def parse_sections(markdown: str) -> Dict[str, List[str]]:
    sections: Dict[str, List[str]] = {}
    current = "__preamble__"
    sections[current] = []
    for line in markdown.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections.setdefault(current, [])
        else:
            sections.setdefault(current, []).append(line)
    return sections


def rebuild_markdown(sections: Dict[str, List[str]]) -> str:
    lines: List[str] = []
    pre = sections.get("__preamble__", [])
    lines.extend(pre)
    if pre and pre[-1].strip() != "":
        lines.append("")

    ordered = [k for k in sections.keys() if k != "__preamble__"]
    for idx, key in enumerate(ordered):
        lines.append(f"## {key}")
        body = sections.get(key, [])
        lines.extend(body)
        if idx < len(ordered) - 1 and (not body or body[-1].strip() != ""):
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def load_existing_new_learnings(sections: Dict[str, List[str]]) -> Tuple[List[str], Set[str]]:
    body = sections.get("new_learnings", [])
    urls = set()
    for line in body:
        for m in re.findall(r"https?://[^\s\|\]]+", line):
            urls.add(m)
    return body, urls


def stage_findings(long_term_path: Path, validated: List[ValidatedFinding]) -> int:
    text = read_text_if_exists(long_term_path)
    if not text:
        text = "# Long-Term Memory (Distilled Facts, Preferences, Patterns)\n\nLast updated: " + now_utc().strftime("%Y-%m-%d") + "\n\n"

    sections = parse_sections(text)
    if "new_learnings" not in sections:
        sections["new_learnings"] = ["- No staged learnings yet."]

    existing_body, existing_urls = load_existing_new_learnings(sections)
    lines = [line for line in existing_body if "No staged learnings yet." not in line]

    added = 0
    for item in validated:
        if item.url in existing_urls:
            continue
        line = f"- [{item.timestamp}] source: {item.url} | via: {item.source} | note: {item.note}"
        lines.append(line)
        existing_urls.add(item.url)
        added += 1

    if not lines:
        lines = ["- No staged learnings yet."]

    sections["new_learnings"] = lines

    preamble = sections.get("__preamble__", [])
    updated = []
    date_line_set = False
    for ln in preamble:
        if ln.startswith("Last updated:"):
            updated.append(f"Last updated: {now_utc().strftime('%Y-%m-%d')}")
            date_line_set = True
        else:
            updated.append(ln)
    if not date_line_set:
        if updated and updated[-1].strip() != "":
            updated.append("")
        updated.append(f"Last updated: {now_utc().strftime('%Y-%m-%d')}")
    sections["__preamble__"] = updated

    long_term_path.write_text(rebuild_markdown(sections), encoding="utf-8")
    return added


def gather_findings(hours: int, queries: List[str]) -> List[Finding]:
    all_findings: List[Finding] = []
    for q in queries:
        all_findings.extend(fetch_ddg(q))
        all_findings.extend(fetch_reddit(q))
        all_findings.extend(fetch_hn(q, hours=hours))
        all_findings.extend(fetch_quora(q))
    return dedupe_findings(all_findings)


def main() -> int:
    parser = argparse.ArgumentParser(description="Scout web/reddit/hn/quora for novel project-relevant updates.")
    parser.add_argument("--workspace-root", default=".", help="Workspace root path")
    parser.add_argument("--hours", type=int, default=24, help="Time window for freshness-sensitive sources")
    parser.add_argument("--max-findings", type=int, default=15, help="Maximum validated findings to stage per run")
    parser.add_argument("--query", action="append", default=[], help="Optional additional query")
    args = parser.parse_args()

    workspace_root = Path(args.workspace_root).resolve()
    long_term_path = workspace_root / "memory" / "long-term-memory.md"

    queries = list(dict.fromkeys(DEFAULT_QUERIES + args.query))
    corpus, existing_urls = collect_reference_corpus(workspace_root)

    existing_text = read_text_if_exists(long_term_path)
    sections = parse_sections(existing_text) if existing_text else {"__preamble__": []}
    _, staged_urls = load_existing_new_learnings(sections)

    findings = gather_findings(args.hours, queries)

    validated: List[ValidatedFinding] = []
    for f in findings:
        if len(validated) >= args.max_findings:
            break
        if not novelty_check(f, corpus, existing_urls, staged_urls):
            continue
        note = build_note(f)
        validated.append(
            ValidatedFinding(
                timestamp=ts_string(now_utc()),
                url=f.url,
                note=note,
                source=f.source,
            )
        )

    added = stage_findings(long_term_path, validated)
    print(f"Research scout complete. candidates={len(findings)} validated={len(validated)} staged_added={added}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
