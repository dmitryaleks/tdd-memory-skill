---
description: Show the one thing to do next in the TDD refactoring loop
allowed-tools: Bash(python .claude/tdd/tddstate.py:*)
---

Run:

```bash
python .claude/tdd/tddstate.py next
```

Show the output to the user exactly as printed, then do the single action it
names — nothing more. Run `next` again afterwards.

If it reports that the state is corrupt, run
`python .claude/tdd/tddstate.py repair` first: the state rebuilds from the
append-only journal and no progress is lost.
