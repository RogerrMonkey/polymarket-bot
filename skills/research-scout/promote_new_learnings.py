#!/usr/bin/env python3
"""Weekly promotion for staged learnings in long-term memory."""

from __future__ import annotations

import argparse
import datetime as dt
import re
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple


def now_utc_str() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def today_str() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def read_text(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def parse_sections(markdown: str) -> Dict[str, List[str]]:
    sections: Dict[str, List[str]] = {"__preamble__": []}
    current = "__preamble__"
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

    keys = [k for k in sections if k != "__preamble__"]
    for i, key in enumerate(keys):
        lines.append(f"## {key}")
        body = sections.get(key, [])
        lines.extend(body)
        if i < len(keys) - 1 and (not body or body[-1].strip() != ""):
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def extract_new_learnings(section_lines: List[str]) -> List[Tuple[str, str, str]]:
    # returns (timestamp, url, note)
    out: List[Tuple[str, str, str]] = []
    pattern = re.compile(
        r"^- \[(?P<ts>[^\]]+)\]\s+source:\s+(?P<url>https?://[^\s\|]+)\s+\|\s+via:\s+[^\|]+\|\s+note:\s+(?P<note>.+)$"
    )
    for line in section_lines:
        m = pattern.match(line.strip())
        if not m:
            continue
        out.append((m.group("ts"), m.group("url"), m.group("note").strip()))
    return out


def topic_key(note: str) -> str:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9\-]{3,}", note.lower())
    stop = {
        "adds", "new", "context", "potential", "contradiction", "update", "what", "changes", "from", "with", "this", "that",
        "about", "into", "across", "note", "could", "would", "should", "might", "project", "search", "result", "reddit", "post",
    }
    filtered = [w for w in words if w not in stop]
    if not filtered:
        return "misc"
    topic = " ".join(filtered[:2])
    if topic in {"web search", "hacker news", "reddit post", "adds context"}:
        return "misc"
    return topic


def merge_unique(existing: List[str], incoming: List[str], max_items: int = 80) -> List[str]:
    seen = set()
    merged: List[str] = []
    for item in existing + incoming:
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged[:max_items]


def update_recent_memory(path: Path, summary: str) -> None:
    text = read_text(path)
    if not text:
        return
    sections = parse_sections(text)
    key = "Key Decisions"
    section = sections.get(key, [])
    entry = f"- [{now_utc_str()}] {summary}"
    if not section:
        sections[key] = [entry]
    else:
        if section and section[0].startswith("- ["):
            section.insert(0, entry)
        else:
            section.append(entry)
        sections[key] = section
    path.write_text(rebuild_markdown(sections), encoding="utf-8")


def update_project_memory(path: Path, summary: str) -> None:
    text = read_text(path)
    if not text:
        return
    sections = parse_sections(text)
    key = "Current State"
    section = [line for line in sections.get(key, []) if line.strip() != ""]
    section.append(f"- {summary}")
    sections[key] = section
    path.write_text(rebuild_markdown(sections), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Promote staged new_learnings into durable memory sections.")
    parser.add_argument("--workspace-root", default=".", help="Workspace root path")
    args = parser.parse_args()

    workspace_root = Path(args.workspace_root).resolve()
    long_term_path = workspace_root / "memory" / "long-term-memory.md"
    recent_path = workspace_root / "memory" / "recent-memory.md"
    project_path = workspace_root / "memory" / "project-memory.md"

    long_text = read_text(long_term_path)
    if not long_text:
        print("No long-term memory file found.")
        return 0

    sections = parse_sections(long_text)
    staged = extract_new_learnings(sections.get("new_learnings", []))

    if not staged:
        sections["new_learnings"] = ["- No staged learnings yet."]
        long_term_path.write_text(rebuild_markdown(sections), encoding="utf-8")
        print("No staged learnings to promote.")
        return 0

    topic_counts = Counter(topic_key(note) for _, _, note in staged)
    confirmed_topics = {topic for topic, count in topic_counts.items() if count >= 2 and topic != "misc"}

    fact_lines: List[str] = []
    pattern_lines: List[str] = []

    for ts, url, note in staged:
        if "contradiction" in note.lower() or "policy" in note.lower() or "restriction" in note.lower():
            fact_lines.append(f"- [{today_str()}] Watchpoint from staged learning: {note} ({url})")

    for topic in sorted(confirmed_topics):
        pattern_lines.append(
            f"- [{today_str()}] Confirmed pattern from staged learnings: '{topic}' appeared in {topic_counts[topic]} independent findings."
        )

    sections.setdefault("Distilled Facts", [])
    sections.setdefault("Reliable Patterns", [])

    sections["Distilled Facts"] = merge_unique(sections.get("Distilled Facts", []), fact_lines)
    sections["Reliable Patterns"] = merge_unique(sections.get("Reliable Patterns", []), pattern_lines)
    sections["new_learnings"] = ["- No staged learnings yet."]

    # Update preamble date if present
    pre = sections.get("__preamble__", [])
    for i, line in enumerate(pre):
        if line.startswith("Last updated:"):
            pre[i] = f"Last updated: {today_str()}"
    sections["__preamble__"] = pre

    long_term_path.write_text(rebuild_markdown(sections), encoding="utf-8")

    summary = (
        f"Weekly promotion processed {len(staged)} staged learning(s), promoted {len(fact_lines)} fact summary item(s) "
        f"and {len(pattern_lines)} confirmed pattern item(s), then cleared new_learnings staging."
    )
    update_recent_memory(recent_path, summary)
    update_project_memory(project_path, summary)

    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
