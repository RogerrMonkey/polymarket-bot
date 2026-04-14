# Ship Feature

Full pipeline to implement: $ARGUMENTS

Steps:
1. Read tasks/todo.md and tasks/lessons.md
2. Use research-scout subagent if external API/library knowledge needed
3. Use strategy-analyst subagent if this touches trading logic
4. Implement the feature with type hints, loguru logging, and config.py constants
5. Write corresponding test in tests/
6. Use code-reviewer subagent to audit the new code
7. Fix any CRITICAL issues from the review
8. Update tasks/todo.md — mark done and add any follow-up items
9. Append any new lessons learned to tasks/lessons.md
