# Debug

Investigate and fix: $ARGUMENTS

Steps:
1. Use debug-investigator subagent to find root cause
2. Show the exact failing code path before touching anything
3. Apply minimal fix only — no refactoring scope creep
4. Run existing tests to verify fix doesn't break anything
5. Add a regression test for this specific bug
6. Append lesson to tasks/lessons.md if this was a non-obvious issue
