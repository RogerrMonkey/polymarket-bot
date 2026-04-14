#!/usr/bin/env python3
"""Consolidate recent conversation logs into project memory files.

Updates:
- memory/recent-memory.md (rolling window)
- memory/long-term-memory.md (durable facts/preferences/patterns)
- memory/project-memory.md (active state)
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
from pathlib import Path
from typing import Dict, List, Tuple

SECTION_KEYS = {
    "decisions": "Key Decisions",
    "preferences": "Preferences and Workflow Signals",
    "facts": "Verified Facts",
    "open_threads": "Open Threads",
}

LONG_TERM_KEYS = {
    "facts": "Distilled Facts",
    "preferences": "User Preferences",
    "patterns": "Reliable Patterns",
    "guardrails": "Guardrails",
}

DECISION_WORDS = {
    "decide",
    "decision",
    "chose",
    "chosen",
    "will",
    "should",
    "phase",
    "scope",
    "approve",
    "approved",
    "plan",
    "gate",
}

PREFERENCE_WORDS = {
    "prefer",
    "always",
    "never",
    "must",
    "style",
    "workflow",
    "without",
    "before",
    "after",
    "detailed",
}

FACT_WORDS = {
    "workspace",
    "file",
    "path",
    "api",
    "endpoint",
    "rate",
    "limit",
    "risk",
    "threshold",
    "phase",
    "week",
    "task",
}


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_date(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%d")


def iso_stamp(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def normalize_text(line: str) -> str:
    line = re.sub(r"\s+", " ", line).strip()
    line = line.replace("\u2013", "-").replace("\u2014", "-")
    return line


def is_signal_line(line: str) -> bool:
    if len(line) < 25 or len(line) > 280:
        return False
    bad_prefixes = ("http://", "https://", "{" , "}", "[", "]")
    if line.startswith(bad_prefixes):
        return False
    return True


def contains_any(line: str, words: set[str]) -> bool:
    lower = line.lower()
    return any(word in lower for word in words)


def dedupe_keep_order(lines: List[str]) -> List[str]:
    seen = set()
    result = []
    for line in lines:
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(line)
    return result


def discover_log_files(logs_path: str | None, hours: int) -> List[Path]:
    cutoff = now_utc() - dt.timedelta(hours=hours)
    candidates: List[Path] = []

    def add_file_if_recent(path: Path) -> None:
        if not path.is_file():
            return
        try:
            modified = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
        except OSError:
            return
        if modified >= cutoff:
            candidates.append(path)

    if logs_path:
        p = Path(logs_path)
        if p.is_file():
            add_file_if_recent(p)
        elif p.is_dir():
            for child in p.rglob("*"):
                add_file_if_recent(child)

    env_log = os.getenv("VSCODE_TARGET_SESSION_LOG")
    if env_log:
        p = Path(env_log)
        if p.is_file():
            add_file_if_recent(p)

    appdata = os.getenv("APPDATA")
    if appdata:
        ws_root = Path(appdata) / "Code" / "User" / "workspaceStorage"
        if ws_root.is_dir():
            for workspace_dir in ws_root.iterdir():
                debug_dir = workspace_dir / "GitHub.copilot-chat" / "debug-logs"
                if not debug_dir.is_dir():
                    continue
                for child in debug_dir.rglob("*"):
                    add_file_if_recent(child)

    unique = {str(p.resolve()): p for p in candidates}
    return sorted(unique.values(), key=lambda p: p.stat().st_mtime)


def read_signal_lines(files: List[Path]) -> List[str]:
    lines: List[str] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for raw in text.splitlines():
            line = normalize_text(raw)
            if is_signal_line(line):
                lines.append(line)
    return dedupe_keep_order(lines)


def extract_categories(lines: List[str]) -> Dict[str, List[str]]:
    decisions, preferences, facts = [], [], []
    for line in lines:
        if contains_any(line, DECISION_WORDS):
            decisions.append(line)
        if contains_any(line, PREFERENCE_WORDS):
            preferences.append(line)
        if contains_any(line, FACT_WORDS):
            facts.append(line)

    decisions = dedupe_keep_order(decisions)[:20]
    preferences = dedupe_keep_order(preferences)[:20]
    facts = dedupe_keep_order(facts)[:20]

    open_threads = []
    for line in decisions:
        if "next" in line.lower() or "todo" in line.lower() or "pending" in line.lower():
            open_threads.append(line)
    open_threads = dedupe_keep_order(open_threads)[:12]

    return {
        "decisions": decisions,
        "preferences": preferences,
        "facts": facts,
        "open_threads": open_threads,
    }


def parse_recent_file(path: Path) -> Dict[str, List[Tuple[dt.datetime, str]]]:
    parsed: Dict[str, List[Tuple[dt.datetime, str]]] = {k: [] for k in SECTION_KEYS}
    if not path.exists():
        return parsed

    section = None
    pattern = re.compile(r"^- \[(?P<ts>[^\]]+)\] (?P<text>.+)$")

    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return parsed

    heading_to_key = {v: k for k, v in SECTION_KEYS.items()}

    for line in lines:
        if line.startswith("## "):
            header = line[3:].strip()
            section = heading_to_key.get(header)
            continue
        if not section:
            continue
        m = pattern.match(line)
        if not m:
            continue
        ts_str = m.group("ts")
        text = m.group("text").strip()
        try:
            ts = dt.datetime.strptime(ts_str, "%Y-%m-%d %H:%M UTC").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        parsed[section].append((ts, text))

    return parsed


def merge_recent(
    existing: Dict[str, List[Tuple[dt.datetime, str]]],
    new_data: Dict[str, List[str]],
    window_hours: int,
) -> Dict[str, List[Tuple[dt.datetime, str]]]:
    now = now_utc()
    cutoff = now - dt.timedelta(hours=window_hours)
    merged: Dict[str, List[Tuple[dt.datetime, str]]] = {k: [] for k in SECTION_KEYS}

    for key in SECTION_KEYS:
        bucket: Dict[str, dt.datetime] = {}
        for ts, text in existing.get(key, []):
            if ts < cutoff:
                continue
            bucket[text] = max(bucket.get(text, ts), ts)
        for text in new_data.get(key, []):
            bucket[text] = now
        merged[key] = sorted(((ts, text) for text, ts in bucket.items()), key=lambda x: x[0], reverse=True)

    return merged


def write_recent(path: Path, data: Dict[str, List[Tuple[dt.datetime, str]]], window_hours: int) -> None:
    now = now_utc()
    lines: List[str] = [
        "# Recent Memory (Rolling 48hr Context)",
        "",
        f"Last updated: {iso_date(now)}",
        f"Window policy: keep only the latest {window_hours} hours of high-signal context.",
        "",
    ]

    for key, title in SECTION_KEYS.items():
        lines.append(f"## {title}")
        entries = data.get(key, [])
        if not entries:
            lines.append("- [" + iso_stamp(now) + "] No new high-signal items captured.")
        else:
            for ts, text in entries[:30]:
                lines.append(f"- [{iso_stamp(ts)}] {text}")
        lines.append("")

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def parse_long_term(path: Path) -> Dict[str, List[str]]:
    parsed: Dict[str, List[str]] = {k: [] for k in LONG_TERM_KEYS}
    if not path.exists():
        return parsed

    heading_to_key = {v: k for k, v in LONG_TERM_KEYS.items()}
    current = None

    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return parsed

    for line in lines:
        if line.startswith("## "):
            current = heading_to_key.get(line[3:].strip())
            continue
        if current and line.startswith("- "):
            parsed[current].append(line[2:].strip())

    for key in parsed:
        parsed[key] = dedupe_keep_order(parsed[key])

    return parsed


def promote_items(recent: Dict[str, List[Tuple[dt.datetime, str]]]) -> Dict[str, List[str]]:
    facts = [text for _, text in recent.get("facts", [])]
    prefs = [text for _, text in recent.get("preferences", [])]
    decisions = [text for _, text in recent.get("decisions", [])]

    promoted_prefs = [
        line for line in prefs if contains_any(line, {"prefer", "always", "never", "must", "workflow", "style"})
    ]

    promoted_patterns = [
        line
        for line in (decisions + prefs)
        if contains_any(line, {"verify", "test", "gate", "phase", "before", "after", "risk", "check"})
    ]

    promoted_guardrails = [
        line
        for line in (decisions + prefs)
        if contains_any(line, {"never", "must", "do not", "stop", "limit", "block"})
    ]

    return {
        "facts": dedupe_keep_order(facts)[:30],
        "preferences": dedupe_keep_order(promoted_prefs)[:30],
        "patterns": dedupe_keep_order(promoted_patterns)[:30],
        "guardrails": dedupe_keep_order(promoted_guardrails)[:30],
    }


def write_long_term(path: Path, existing: Dict[str, List[str]], promoted: Dict[str, List[str]]) -> None:
    now = now_utc()
    merged: Dict[str, List[str]] = {}
    for key in LONG_TERM_KEYS:
        merged[key] = dedupe_keep_order(existing.get(key, []) + promoted.get(key, []))[:60]

    lines = [
        "# Long-Term Memory (Distilled Facts, Preferences, Patterns)",
        "",
        f"Last updated: {iso_date(now)}",
        "",
    ]

    for key, title in LONG_TERM_KEYS.items():
        lines.append(f"## {title}")
        values = merged.get(key, [])
        if values:
            lines.extend([f"- {item}" for item in values])
        else:
            lines.append("- No durable items captured yet.")
        lines.append("")

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_project_memory(path: Path, workspace_root: Path, recent: Dict[str, List[Tuple[dt.datetime, str]]], log_count: int) -> None:
    now = now_utc()
    plan_path = workspace_root / "plan.md"
    plan_signals: List[str] = []
    if plan_path.exists():
        try:
            for line in plan_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                clean = normalize_text(line)
                if clean.startswith("##") or clean.startswith("###") or "Phase" in clean:
                    plan_signals.append(clean)
                if len(plan_signals) >= 8:
                    break
        except OSError:
            pass

    top_decisions = [text for _, text in recent.get("decisions", [])][:6]

    lines = [
        "# Project Memory (Active Project State)",
        "",
        f"Last updated: {iso_date(now)}",
        "",
        "## Current Objective",
        "- Maintain a persistent memory system and keep project state synchronized before implementation.",
        "",
        "## Current State",
        f"- Consolidation run processed {log_count} recent log file(s).",
        "- Memory files are maintained under memory/ and refreshed by automation.",
        "",
        "## Active Decisions",
    ]

    if top_decisions:
        lines.extend([f"- {item}" for item in top_decisions])
    else:
        lines.append("- No new decisions detected in the latest consolidation window.")

    lines.extend([
        "",
        "## Plan Signals",
    ])

    if plan_signals:
        lines.extend([f"- {item}" for item in plan_signals])
    else:
        lines.append("- plan.md not found or did not include parseable phase headings.")

    lines.extend([
        "",
        "## Next Actions",
        "- Continue implementation using plan.md while keeping nightly memory consolidation enabled.",
        "",
        "## References",
        "- plan.md",
        "- memory/recent-memory.md",
        "- memory/long-term-memory.md",
        "- memory/project-memory.md",
        "",
    ])

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Consolidate recent conversation logs into memory files.")
    parser.add_argument("--workspace-root", default=".", help="Workspace root path")
    parser.add_argument("--logs-path", default=None, help="Optional log file or directory")
    parser.add_argument("--hours", type=int, default=24, help="How far back to read logs")
    parser.add_argument("--recent-window-hours", type=int, default=48, help="Rolling window for recent memory")
    args = parser.parse_args()

    workspace_root = Path(args.workspace_root).resolve()
    memory_dir = workspace_root / "memory"
    recent_path = memory_dir / "recent-memory.md"
    long_term_path = memory_dir / "long-term-memory.md"
    project_path = memory_dir / "project-memory.md"

    ensure_parent(recent_path)
    ensure_parent(long_term_path)
    ensure_parent(project_path)

    log_files = discover_log_files(args.logs_path, args.hours)
    lines = read_signal_lines(log_files)
    extracted = extract_categories(lines)

    existing_recent = parse_recent_file(recent_path)
    merged_recent = merge_recent(existing_recent, extracted, args.recent_window_hours)
    write_recent(recent_path, merged_recent, args.recent_window_hours)

    existing_long = parse_long_term(long_term_path)
    promoted = promote_items(merged_recent)
    write_long_term(long_term_path, existing_long, promoted)

    write_project_memory(project_path, workspace_root, merged_recent, len(log_files))

    print("Memory consolidation complete.")
    print(f"Workspace: {workspace_root}")
    print(f"Logs processed: {len(log_files)}")
    print(f"Recent entries: {sum(len(v) for v in merged_recent.values())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
