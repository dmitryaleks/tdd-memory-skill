# The loop, phase by phase

Read this when you want to know *why* the order is fixed, or what a phase name means. You never need
it to work — `next` tells you what to do without it.

`...` stands for `python .claude/tdd/tddstate.py`.

---

## One target, start to finish

```
init → discover unit → select unit → baseline unit
     → discover scenario → select scenario → baseline scenario
     → step start → edit → run unit → (fix → run unit)*
                         → run scenario → (fix → re-verify what it touched)*
     → step done → (next increment …) → done
```

Inside an increment, an edit re-opens only the gates it can affect, and `next` routes accordingly:

| Edited | Then |
|---|---|
| production code, or a `step start --file` file | run unit, then scenarios: both gates re-opened |
| a selected unit test | run unit; the scenario result still stands, so the increment can close |
| a feature file, a step definition, the runner | run scenarios only; the unit result still stands |

This is why a scenario fix usually sends you back to the unit tests: repairing a scenario normally
means changing behaviour, and that can break a unit test in the same increment. When the repair is
confined to the glue or a feature file, it does not, and the loop stays on the scenario side.

| Phase | What it means |
|---|---|
| `INIT` | No target. Nothing is being worked on. |
| `DISCOVER_UNIT` / `CONFIRM_UNIT` | Finding, then confirming, the JUnit tests that cover the target. |
| `BASELINE_UNIT` | Recording how those tests stood *before* any edit. |
| `DISCOVER_SCENARIO` / `CONFIRM_SCENARIO` / `BASELINE_SCENARIO` | The same for Cucumber scenarios. |
| `READY` | Baselines recorded, no increment open. |
| `STEP_OPEN` | An increment is open and waiting for the code change. |
| `VERIFY_UNIT` / `FIX_UNIT` | Running, or fixing, the unit gate. |
| `VERIFY_SCENARIO` / `FIX_SCENARIO` | The same for the scenario gate — only reachable once unit is green. |
| `STEP_GREEN` | Every applicable gate verified after the last edit; the increment can close. A further edit re-opens whichever gates it affects, so this is not a resting place. |
| `BLOCKED` | Someone escalated. Only `unblock` leaves this state. |

---

## Why the order is what it is

**Baselines come before the first edit.** A repository usually has some tests already failing. If you
record the baseline first, those stay "pre-existing" and do not block your increment. If you edit
first, you can no longer tell your regressions apart from what was already broken, and you will spend
a cycle fixing something you did not break.

**Unit before scenario.** Unit tests are faster and far more specific. A failing scenario over a red
unit gate tells you almost nothing — the scenario would fail for either reason. The tracker refuses
the scenario run until unit is green, so the signal stays clean. `--force` exists, is journaled as an
override, and is almost always the wrong call.

**One increment at a time.** `step start` snapshots the files an increment may touch, which is what
makes `revert-step` possible. An edit made before `step start` has no snapshot and cannot be undone
by the tracker.

**A scenario fix re-opens the unit gate.** Repairing a failing scenario means changing behaviour,
and that change can break a unit test in the same increment. So the edit invalidates both results and
you re-verify unit first, then scenarios. `run scenario` straight after such a fix is refused by the
ordering gate. The increment is finished when both gates are green on runs newer than your last
edit — not when the scenarios finally pass.

**Freshness outranks greenness.** A green result recorded before your last edit proves nothing.
`next` marks it STALE; `step done` refuses to close on it. Each gate watches only what can change
its own result: the target (and anything you passed to `step start --file`) re-opens both, a unit
test re-opens the unit gate alone, and a feature file, step definition or the runner re-opens the
scenario gate alone. This is the single most common way an
agentic loop convinces itself it is finished when it is not.

---

## What "green" actually means

Green is measured **against the baseline**, not against zero. A test that was already failing before
you started does not block the gate; it is reported as a pre-existing failure and counted, not
listed. A test that was passing at baseline and fails now is a **regression**, and regressions are
listed first in every run summary because they are the ones your increment caused.

---

## Increments

Good increment titles name one change:

- `extract PriceCalculator`
- `inline the duplicate discount branch`
- `replace the int amount with Money`

If the title needs an "and", split it. Small increments mean `revert-step` throws away little work,
and they keep the failure signature meaningful: three identical failures across a small increment is
strong evidence the approach is wrong, whereas across a huge one it means almost nothing.

Closing an increment (`step done`) requires both applicable gates green **on runs newer than your
last edit**. It resets the attempt counters, so each increment gets a fresh anti-thrash budget.

`step abandon` closes an increment without the gates. It does **not** revert your code — if you want
the code back as well, run `revert-step` first, then abandon.

---

## Finishing a target

`done` refuses while an increment is open or a gate is red. It archives the session, the status
mirror and a summary into `.claude/tdd/archive/<timestamp>-<Class>/`, then clears the active session
so the next target starts clean. Past targets stay readable in that archive.

---

## Leaving notes

`... note "<text>"` records something the next session cannot work out from the code — a constraint
the human gave you, a dead end you already tried, a file that looks related but is not. Notes appear
in the session-opening brief. Use them for judgement, not for facts the tracker already holds: it
already knows the target, the selected tests, the gate colours and the recent history.
