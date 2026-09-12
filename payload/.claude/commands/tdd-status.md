---
description: Show the full TDD loop status - target, selected tests, both gates, recent history
allowed-tools: Bash(python .claude/tdd/tddstate.py:*)
---

Run:

```bash
python .claude/tdd/tddstate.py status
```

Show the output as printed. Summarise in one or two sentences only if the user
asked a specific question; otherwise let the output speak for itself.

The same information, formatted for reading, is always on disk at
`.claude/tdd/STATUS.md`.
