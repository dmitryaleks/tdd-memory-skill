---
name: tdd-loop
description: Refactor one Java file at a time under JUnit and Cucumber, with the loop position tracked on disk so progress survives a lost session. Use when refactoring Java, resuming after a restart or disconnect, or when asked where the refactor got to and which tests to run next. Triggers: refactor, TDD loop, resume, where was I, which tests next, unit tests green, scenario tests, Cucumber, Gherkin, JUnit, tdd next.
user-invocable: true
allowed-tools:
  - Bash(python .claude/tdd/tddstate.py:*)
  - Read
  - Edit
  - Grep
  - Glob
---

# TDD refactoring loop

One Java file at a time: find the JUnit tests that cover it, find the Cucumber scenarios that
exercise it, then refactor in small increments — **unit tests green first, scenario tests green
second**.

A tracker at `.claude/tdd/tddstate.py` owns the loop position on disk, so a dropped connection, a
restart or a lost context costs nothing. The human-readable mirror is `.claude/tdd/STATUS.md`.

## Rule 1 — `next` decides, you execute

```bash
python .claude/tdd/tddstate.py next
```

It prints **one instruction and one command**. Do that one thing. Run `next` again. That is the
entire protocol — you never have to work out where you are, and you never have to remember anything
between sessions.

Run it first thing in every session, and again after every action.

## The commands

Below, `...` stands for `python .claude/tdd/tddstate.py`. Type them exactly; `next` always prints
the full command for you.

| Command | When |
|---|---|
| `... next` | always: first, and between every action |
| `... init --target <File.java> --goal "<what you are changing>"` | starting on a new file |
| `... discover unit` · `... discover scenario` | `next` says DISCOVER_… |
| `... select unit <ids…>` · `... select scenario <ids…>` | `next` says SELECT_… |
| `... select scenario --none` | this target genuinely has no Cucumber coverage |
| `... baseline unit` · `... baseline scenario` | `next` says BASELINE_… — before any edit |
| `... step start "<title>"` | before you touch any code |
| `... run unit` · `... run scenario` | to verify — never run Gradle yourself |
| `... step done` | both gates green |
| `... revert-step` | you are stuck and `next` says ESCALATE |
| `... done` | the target is finished |
| `... note "<text>"` | something the next session must know |
| `... block "<why>"` | you need a human decision |
| `... status` | the full picture when you are confused |

## Hard rules

1. **Run `next` first and after every action.** Never guess the loop position, and never re-derive
   which tests are relevant — the selection is already recorded and sticky.
2. **Never edit code before `step start`.** Increments are snapshotted so `revert-step` can undo
   them; an edit made outside an increment cannot be undone.
3. **Never run Gradle yourself.** `run unit` / `run scenario` execute it, parse the reports and
   summarise them in about twenty lines. Running Gradle directly floods your context with thousands
   of lines and records nothing.
4. **Never run scenario tests while the unit gate is red.** The tracker refuses it. Fix the unit
   tests first — a red unit gate hides which change broke what.
5. **Never trust a test result older than your last edit.** `next` marks it STALE and `step done`
   refuses it. Re-run instead.
6. **Never paste raw build output into your reply.** The summary is already the useful part; the
   full log path is printed with it, so `grep` that file if you need detail.
7. **A build failure is not a test failure.** If the summary says BUILD FAILED, the tests never ran:
   fix the compile error and change nothing else.
8. **If `next` says ESCALATE, stop editing.** Three identical failures mean the approach is wrong,
   not that it needs one more try. `revert-step` and try differently, or `block` and ask.
9. **Never edit anything under `.claude/tdd/state/`.** Use the commands; they keep the journal
   consistent.

## What `next` can ask for

| Code | Do this |
|---|---|
| `SET_TARGET` | `init --target …` with a one-line goal |
| `DISCOVER_UNIT` / `DISCOVER_SCENARIO` | run the discover command |
| `SELECT_UNIT` / `SELECT_SCENARIO` | read the candidates, keep the ones that really cover the target |
| `BASELINE_UNIT` / `BASELINE_SCENARIO` | record the pre-refactor baseline — do not edit first |
| `START_STEP` | open one small increment with a clear title |
| `AWAIT_EDIT` | write the code change for the increment that is open |
| `RUN_UNIT` / `RUN_SCENARIO` | run those tests |
| `FIX_UNIT` / `FIX_SCENARIO` | fix exactly the named tests, then re-run |
| `FIX_BUILD` | a compile or build error — the tests never ran |
| `DIAGNOSE_RUN` | no reports were produced; this is **not** a pass |
| `RERUN_TIMEOUT` | the run timed out; nothing was verified |
| `ESCALATE` | stop editing; revert or ask the human |
| `FINISH_STEP` | `step done` |
| `NEXT_STEP_OR_DONE` | open the next increment, or `done` |
| `UNBLOCK` | a human decision is outstanding |
| `REPAIR` | `repair` rebuilds the state from the journal — nothing is lost |

## Keep increments small

One increment is one named change: "extract PriceCalculator", "inline the duplicate branch". If you
cannot put it in a short title, it is too big. Small increments make `revert-step` cheap and keep
the failure signature meaningful.

## When something looks wrong

- `... status` — the full picture, including both baselines and the last run.
- `references/recovery.md` — lost session, corrupt state, stale lock, no results, timeouts,
  overrides.
- `references/protocol.md` — what each phase means and why the order is fixed.
- `references/discovery.md` — how to prune candidate tests well.
