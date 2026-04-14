---
name: consolidate-memory
description: Consolidate the last 24 hours of conversation logs into memory/recent-memory.md, memory/long-term-memory.md, and memory/project-memory.md. Use when asked to refresh memory, extract decisions, capture preferences, promote patterns, or run nightly memory maintenance.
metadata:
  version: 1.0.0
  tags: [memory, consolidation, automation]
---

# Consolidate Memory Skill

## Purpose
Maintain a persistent memory layer by extracting high-signal information from recent conversation logs and updating memory files.

## Inputs
- Workspace root
- Conversation logs from the past 24 hours

## Outputs
- memory/recent-memory.md (rolling 48-hour context)
- memory/long-term-memory.md (distilled facts, preferences, patterns)
- memory/project-memory.md (active project state)

## Execution
Run:

python skills/consolidate-memory/consolidate_memory.py --workspace-root . --hours 24

Optional:

python skills/consolidate-memory/consolidate_memory.py --workspace-root . --hours 24 --logs-path "<path-to-log-or-dir>"

## Promotion Rules
- Promote stable preferences and repeatable behavior from recent memory to long-term memory.
- Promote verified project facts and durable constraints to long-term memory.
- Keep volatile, session-specific details in recent memory only.

## Validation Checklist
- All three memory files exist and are updated with a new timestamp.
- Recent memory only contains entries within rolling 48 hours.
- Long-term memory receives only durable items (not transient chatter).
- Project memory reflects current objective, state, and next actions.
