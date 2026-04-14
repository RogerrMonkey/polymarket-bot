---
name: research-scout
description: Find new external information that challenges or updates current project knowledge. Use when asked to scout web, Reddit, Hacker News, or Quora for strategy/tool/workflow updates, validate novelty against local docs, and stage validated findings in memory/long-term-memory.md under new_learnings.
metadata:
  version: 1.0.0
  tags: [research, novelty-detection, memory, scouting]
---

# Research Scout

## Purpose
Continuously discover external updates that are materially new or contradictory relative to current project documentation and memory.

## Sources
- Web search (DuckDuckGo HTML endpoint)
- Reddit
- Hacker News (Algolia API)
- Quora (via focused web search: site:quora.com)

## Required Behavior
1. Search recent content for strategies, tools, announcements, and workflow changes relevant to the project.
2. Cross-reference each candidate with local documentation and memory.
3. Discard redundant findings.
4. Store only validated findings under `## new_learnings` in `memory/long-term-memory.md`.

## Entry Format
- Timestamp
- Source URL
- One-line note describing what changed or what was added

## Run

python skills/research-scout/research_scout.py --workspace-root . --hours 24

## Weekly Promotion

python skills/research-scout/promote_new_learnings.py --workspace-root .

The weekly promotion script reviews staged findings, promotes confirmed patterns into core memory sections, and clears `new_learnings` staging.
