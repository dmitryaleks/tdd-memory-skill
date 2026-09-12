---
description: Start the TDD refactoring loop on a Java file
argument-hint: <path/to/File.java> [goal]
allowed-tools: Bash(python .claude/tdd/tddstate.py:*), Read, Glob
---

Start work on the Java file given in `$ARGUMENTS`.

1. The first token is the file path. Anything after it is the goal. If no goal
   was given, ask the user for a one-line description of what is being changed —
   it goes into every future session's resume brief, so it is worth having.
2. If the path is ambiguous or does not exist, use Glob to locate it and confirm
   with the user before continuing.
3. Then run:

```bash
python .claude/tdd/tddstate.py init --target <path> --goal "<goal>"
```

4. Follow the `NEXT` line it prints, and keep following `next` from there.

If another target is already active, `init` refuses rather than discarding it.
Do not pass `--force` unless the user explicitly says to abandon the current
target — finishing it with `done` is almost always what they meant.
