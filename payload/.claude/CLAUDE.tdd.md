<!-- BEGIN tdd-memory-skill -->
## TDD refactoring loop

This repo tracks Java refactoring progress in `.claude/tdd/`, so it survives a lost
session, a disconnect or a compaction.

**Before anything else in a session, run:**

```bash
python .claude/tdd/tddstate.py next
```

Then do exactly the one action it prints, and run it again. It tells you the target
file, which tests are selected, which gate is red, and the exact next command.

Rules it enforces, so do not work around them: unit tests must be green before
scenario tests are run; an increment cannot be closed on a test result older than
your last edit; never run Gradle directly — `run unit` / `run scenario` parse the
reports and keep the output out of your context.

Full protocol: `.claude/skills/tdd-loop/SKILL.md`. Human-readable status:
`.claude/tdd/STATUS.md`.
<!-- END tdd-memory-skill -->
