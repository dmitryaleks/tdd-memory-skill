# DEVPLAN — TDD Memory Skill

A Claude Code skill + hooks + local state tracker that gives an agentic Java refactoring loop
durable memory, so that a slow/unstable LLM — or a brand-new Claude session taking over — can always
answer two questions instantly: **where are we in the loop, and which tests do I run next?**

- Status: **complete**. All eight steps delivered; 277 tests plus a 87-check end-to-end walk pass.
- Date: 2026-09-12.
- Scope of this document: the complete design plus a step-by-step implementation plan with
  acceptance criteria.

---

## 1. Context and problem

In the target setup, Claude Code is driven by a proprietary LLM behind a misconfigured router. In
practice this means:

- the context window is exhausted *before* compaction fires, so history is lost abruptly;
- the connection drops and the model has to reconnect into a fresh session;
- the model is slow and not very strong, so it must not be asked to do subtle reasoning.

The work is agentic refactoring of Java code with a fixed rhythm:

> focus on one Java file → identify the pertinent JUnit unit tests → identify the pertinent
> Gherkin/Cucumber scenario tests → refactor → **unit tests green first** → **then scenario tests
> green** → repeat.

Each interruption destroys the loop position. The model then:

1. re-discovers which tests are relevant — slow, expensive, and non-deterministic, so it may well
   pick a different set than it did an hour ago;
2. re-runs the whole suite instead of the pertinent subset;
3. pastes thousands of lines of Gradle output into a context window that is already the bottleneck;
4. occasionally declares a step finished based on a test run that predates its own last edit.

### Goal

Make loop position a **first-class artifact on disk**, owned by a deterministic program rather than
by the model's memory, and make the "what next?" question answerable by one command that prints one
instruction.

### Non-goals

- No replacement for the build system and no test framework of our own.
- No refactoring intelligence. The tracker never decides *how* to refactor — only *what to do next
  in the loop* and *whether the gates are green*.
- No multi-user or server component, no daemon, no database.

---

## 2. Constraints

| Constraint | Decision |
|---|---|
| Network | **Zero.** No HTTP, no sockets, no telemetry, no package installation. |
| Build tool | **Gradle only** (wrapper preferred). |
| Language | **Python 3 stdlib** (3.8+), single self-contained file. |
| State location | `.claude/tdd/` inside the Java repo; runtime state **gitignored**. |
| Test execution | The tracker **runs Gradle itself** and parses the reports. |
| Platform | Windows, Linux, macOS. No shell-isms, no symlinks, `pathlib` throughout. |

Allowed imports, exhaustively: `argparse, datetime, hashlib, json, os, pathlib, re, shutil,
subprocess, sys, time, xml.etree.ElementTree`. A check in the test suite greps for anything else, so
the no-network constraint is mechanically enforced rather than merely promised.

---

## 3. Solution overview

### 3.1 Three layers of memory

Redundant on purpose — the weak model only has to catch one of them.

| Layer | Mechanism | Survives |
|---|---|---|
| 1 — always in context | A short `CLAUDE.md` block: *"Before anything else, run `tdd next` and obey it."* | Everything; `CLAUDE.md` is re-read at every session start. |
| 2 — auto-injected | `SessionStart` and `PreCompact` hooks run `tddstate.py resume --brief`, injecting the live position into the new context. | Reconnects, compaction, context exhaustion. |
| 3 — on disk | `session.json` + append-only `journal.jsonl` + human-readable `STATUS.md` + capped run logs. | Process kill, reboot, days later, a different human. |

Layer 2 is the one that directly fixes the reported failure mode: a reconnecting model opens its
very first turn already knowing the target file, the selected tests, the gate colours and the next
command — without spending a single tool call.

### 3.2 One command, one instruction

The model is never asked to *infer* loop position. `tdd next` is a **pure function of state** that
prints exactly one action:

```
TDD  target=src/main/java/com/acme/Order.java  phase=VERIFY_UNIT  step=3 "extract PriceCalculator"
UNIT  3 selected · last run 11/12 · 1 failing · same-failure streak 2
SCEN  2 selected · STALE (source edited after last run)
NEXT  Fix com.acme.OrderTest#appliesDiscount, then re-run the unit tests.
      Do not touch the scenario tests yet.
CMD   python .claude/tdd/tddstate.py run unit
WHY   unit gate is red; the scenario gate stays locked until unit is green
```

The whole agent protocol collapses to: **run `next` → do the one thing it says → run `next` again.**

### 3.3 What keeps the model honest

Three guards are enforced by the script, not by the prompt — a weak model cannot talk its way past
them:

1. **Ordering gate** — `run scenario` is refused while the unit gate is red. (`--force` exists and
   is journaled as an override.)
2. **Freshness gate** — `step done` is refused while any file the gate watches differs from what it
   was when the verifying run started. This is what stops "declared green on stale results". The
   watch is scoped per gate: the target and any file declared with `step start --file` re-open both
   gates, a selected unit test re-opens only the unit gate, and a `.feature` file, a step definition
   or the Cucumber runner re-opens only the scenario gate. Production code affects both; beyond that
   the two sides cannot influence each other, and scoping keeps one edit from re-running the lot.
**Freshness is decided by comparing recorded mtimes, not by ordering timestamps.** Each run stores
the mtime of every watched file as it launches; a result is stale when any of those differs now.
Ordering a file's mtime against the run's clock looked equivalent and is not: the two events can be
microseconds apart and filesystems differ in mtime resolution, so "did the edit come before or after
the run?" has no dependable answer at that margin — while "is this the same file that was tested?"
always does. Both orderings of the comparison produced intermittently wrong verdicts in testing.

3. **Anti-thrash** — every run gets a failure *signature*. Three consecutive identical signatures
   mean no progress is being made, so `next` stops issuing fix instructions and escalates to
   `revert-step` or to handing back to the human.

`revert-step` restores the increment's snapshot and then **re-opens the same increment** with its
clock reset and its verdicts cleared, so `next` returns to `AWAIT_EDIT` — "make the code change for
*title*". Reverting is a fresh attempt at the same increment, not an abandonment, and the escalation
in guard 3 therefore has somewhere to land. `step abandon` is the separate, explicit way to drop one,
and it says plainly that the code on disk was *not* reverted.

---

## 4. Runtime layout (inside the Java repo)

```
<java-repo>/
  CLAUDE.md                       # + appended TDD protocol block (memory layer 1)
  .gitignore                      # + runtime state ignores
  .claude/
    settings.json                 # + merged hook block
    commands/
      tdd-next.md  tdd-status.md  tdd-target.md
    skills/tdd-loop/
      SKILL.md
      references/{protocol,discovery,recovery}.md
    tdd/
      tddstate.py                 # the tracker — single file, stdlib only
      STATUS.md                   # human mirror, regenerated on every mutation
      state/
        session.json              # machine state; atomic write (tmp + os.replace)
        journal.jsonl             # append-only event log; session.json rebuildable from it
        lock                      # advisory: pid + ISO timestamp; stale after 15 min
      logs/
        2026-09-12T10-31-02Z-unit.log     # full Gradle output — never enters the context
      snapshots/
        s03/src/main/java/com/acme/Order.java    # copies taken at `step start`
      archive/
        2026-09-12T12-00-00Z-Order/              # completed targets
```

`.gitignore` additions written by the installer:

```
.claude/tdd/state/
.claude/tdd/logs/
.claude/tdd/snapshots/
.claude/tdd/archive/
.claude/tdd/STATUS.md
```

`tddstate.py`, the skill and the commands are deliberately **not** ignored, so the human may commit
them and give every clone of the repo the same loop.

---

## 5. State schema (`session.json`, schema v1)

```json
{
  "schema": 1,
  "updated_at": "2026-09-12T10:31:02Z",
  "repo_root": "C:/work/acme",
  "target": {
    "path": "src/main/java/com/acme/Order.java",
    "class_fqn": "com.acme.Order",
    "simple_name": "Order",
    "gradle_project": ":app",
    "goal": "extract pricing strategy",
    "started_at": "2026-09-12T09:02:11Z"
  },
  "phase": "VERIFY_UNIT",
  "step": {
    "id": "s03",
    "n": 3,
    "title": "extract PriceCalculator",
    "started_at": "2026-09-12T10:20:00Z",
    "base_commit": "abc1234",
    "snapshot_dir": "snapshots/s03",
    "files": ["src/main/java/com/acme/Order.java"]
  },
  "steps_done": [
    {"id": "s01", "title": "introduce seam", "finished_at": "2026-09-12T09:40:00Z"},
    {"id": "s02", "title": "inline duplicate", "finished_at": "2026-09-12T10:12:00Z"}
  ],
  "unit": {
    "candidates": [
      {"id": "com.acme.OrderTest", "path": "src/test/java/com/acme/OrderTest.java",
       "score": 3, "reason": "name match: Order -> OrderTest"}
    ],
    "selected": [
      {"id": "com.acme.OrderTest", "path": "src/test/java/com/acme/OrderTest.java",
       "status": "fail"}
    ],
    "baseline": {
      "ran_at": "2026-09-12T09:05:44Z",
      "total": 12, "pass": 12, "fail": 0, "failed_ids": []
    },
    "last_run": {
      "ran_at": "2026-09-12T10:29:41Z",
      "cmd": "gradlew.bat :app:test --tests com.acme.OrderTest --console=plain",
      "log": "logs/2026-09-12T10-29-41Z-unit.log",
      "outcome": "tests_failed",
      "duration_s": 34,
      "total": 12, "pass": 11, "fail": 1, "skipped": 0,
      "failed": [
        {"id": "com.acme.OrderTest#appliesDiscount",
         "message": "expected: <10> but was: <12>"}
      ],
      "signature": "sha1:5f2c9a…"
    },
    "green": false
  },
  "scenario": {
    "not_applicable": false,
    "task": "test",
    "runner_class": "com.acme.RunCucumberTest",
    "filter_mode": "features",
    "candidates": [
      {"id": "src/test/resources/features/order.feature:14",
       "name": "Discount applied to large orders", "tags": ["@pricing"], "score": 3,
       "reason": "step 'the order total is {int}' defined in PricingSteps.java, which references Order"}
    ],
    "selected": [
      {"id": "src/test/resources/features/order.feature:14",
       "name": "Discount applied to large orders", "tags": ["@pricing"], "status": "unknown"}
    ],
    "baseline": {"…same shape as unit.baseline…": null},
    "last_run": {"…same shape as unit.last_run…": null},
    "green": false
  },
  "freshness": {
    "last_verified_at": "2026-09-12T10:22:03Z",
    "dirty": true,
    "dirty_files": ["src/main/java/com/acme/Order.java"]
  },
  "attempts": {
    "verify_unit": 2, "verify_scenario": 0,
    "same_failure_streak": 2, "last_signature": "sha1:5f2c9a…"
  },
  "next_action": {
    "code": "FIX_UNIT",
    "instruction": "Fix com.acme.OrderTest#appliesDiscount, then re-run the unit tests.",
    "command": "python .claude/tdd/tddstate.py run unit",
    "why": "unit gate is red; the scenario gate stays locked until unit is green"
  },
  "history_tail": [
    "10:29:41 run unit -> tests_failed 11/12",
    "10:22:03 step start s03 'extract PriceCalculator'"
  ],
  "notes": [
    {"at": "2026-09-12T09:12:00Z", "text": "PricingSteps also covers Invoice — do not touch it"}
  ],
  "blocked": null
}
```

### 5.1 Field notes

- **`signature`** — `sha1` over the sorted list of `"<test id>|<first line of failure message>"`.
  Two runs with the same signature made no progress, which increments
  `attempts.same_failure_streak`. A different signature, or green, resets it to 0.
- **`outcome`** ∈ `passed | tests_failed | build_failed | no_results | timeout | not_run`.
  `build_failed` and `tests_failed` are deliberately distinct — see §9.4.
- **`green`** is computed against the **baseline**, not against zero: tests that were already red
  before the refactor started do not block the gate. They are listed in the summary as
  `pre-existing` so nobody is surprised by them.
- **`filter_mode`** ∈ `features | tags | none` — how scenario runs get narrowed (§9.3).
- **`scenario.not_applicable`** is set by `select scenario --none` for a target with no Cucumber
  coverage; the scenario gate is then skipped entirely and `next` says so.
- **`history_tail`** is capped at 10 entries and `notes` at 50. The full record lives in
  `journal.jsonl`.

### 5.2 Journal (`journal.jsonl`)

One JSON object per line, appended and `flush()`ed before `session.json` is replaced, so a crash
between the two leaves the journal ahead of the state — never behind:

```json
{"ts":"2026-09-12T10:29:41Z","event":"run","phase":"VERIFY_UNIT","step":"s03","data":{"kind":"unit","outcome":"tests_failed","pass":11,"fail":1,"signature":"sha1:5f2c9a…"}}
```

Events: `init, discover, select, baseline, step_start, step_done, step_abandon, run, record, note,
block, unblock, revert, repair, done, override`. Rotated to `journal.1.jsonl` past 2 MB.

`repair` replays the journal to reconstruct `session.json`, which is what makes a corrupted or
truncated state file a non-event.

---

## 6. State machine

```
INIT → TARGET_SET → DISCOVER_UNIT → CONFIRM_UNIT → BASELINE_UNIT
     → DISCOVER_SCENARIO → CONFIRM_SCENARIO → BASELINE_SCENARIO → READY
     → STEP_OPEN → VERIFY_UNIT ⇄ FIX_UNIT
                 → VERIFY_SCENARIO ⇄ FIX_SCENARIO
                 → STEP_GREEN → (STEP_OPEN | INIT, target archived)
     ⊥ BLOCKED   (reachable from anywhere via `block`, left via `unblock`)

   inside an increment, an edit re-opens only the gates it can affect:
     FIX_SCENARIO → VERIFY_UNIT       fix touched production code (both re-open)
     FIX_SCENARIO → VERIFY_SCENARIO   fix touched only glue or feature files
     VERIFY_UNIT  → STEP_GREEN        unit green and the scenario result still stands
     STEP_GREEN   → VERIFY_UNIT       a later edit on the unit side
     STEP_GREEN   → VERIFY_SCENARIO   a later edit on the scenario side
```

Baselines are taken **before the first edit**, which is what allows pre-existing red tests to be
distinguished from regressions the refactor introduced.

`FIX_SCENARIO` does **not** always loop back to `VERIFY_SCENARIO`. Repairing a scenario usually means
changing behaviour, and that change can break a unit test within the same increment, so it
invalidates both results and the gates are re-verified unit-first. Only when the repair is confined
to the glue or a feature file does the unit result survive and the loop return to `VERIFY_SCENARIO`
directly (§3.3, guard 2). An increment closes only when every applicable gate is green on runs newer
than the last edit — never on "the scenarios finally passed".

`phase` is stored for human readability and for the journal, but it is *not* the source of truth for
what to do next — §7 is. Storing a derived value and recomputing it independently means a corrupted
or hand-edited `phase` cannot send the loop off the rails.

---

## 7. The `next` resolver

A pure function `resolve_next(state, fs_facts) -> NextAction`. Conditions are evaluated **in order**;
the first match wins. This table *is* the specification — it is implemented as a literal ordered list
of `(predicate, action)` pairs so it can be read side by side with this document and unit-tested
exhaustively.

| # | Condition | code | Instruction (abridged) | Command |
|---|---|---|---|---|
| 1 | `blocked` is set | `UNBLOCK` | Blocked: *reason*. Resolve with the human, then unblock. | `tdd unblock` |
| 2 | no `target` | `SET_TARGET` | Name the Java file to work on. | `tdd init --target <file>` |
| 3 | unit candidates and selection both empty | `DISCOVER_UNIT` | Find the pertinent JUnit tests. | `tdd discover unit` |
| 4 | unit selection empty | `SELECT_UNIT` | Confirm which candidates are pertinent. | `tdd select unit <ids…>` |
| 5 | no unit baseline | `BASELINE_UNIT` | Record the pre-refactor baseline before editing. | `tdd baseline unit` |
| 6 | scenario applicable, candidates and selection both empty | `DISCOVER_SCENARIO` | Find the pertinent Cucumber scenarios. | `tdd discover scenario` |
| 7 | scenario applicable, selection empty | `SELECT_SCENARIO` | Confirm scenarios, or declare none apply. | `tdd select scenario <ids…>` / `--none` |
| 8 | scenario applicable, no baseline | `BASELINE_SCENARIO` | Record the scenario baseline. | `tdd baseline scenario` |
| 9 | no open step **and no closed steps** | `START_STEP` | Open the first refactor increment; do not edit before this. | `tdd step start "<title>"` |
| 10 | last run `build_failed`, not stale | `FIX_BUILD` | Compile/build error — fix it. Tests did not run. | `tdd run <kind>` |
| 10b | last run `no_results`, not stale | `DIAGNOSE_RUN` | No fresh reports — do **not** read this as a pass. | `tdd doctor` |
| 10c | last run `timeout`, not stale | `RERUN_TIMEOUT` | The run timed out; re-run or narrow the selection. | `tdd run <kind> --timeout N` |
| 11 | `same_failure_streak >= 3` | `ESCALATE` | Three identical failures: no progress. Revert or hand back. | `tdd revert-step` / `tdd block "<why>"` |
| 11b | increment open, nothing edited or run inside it yet | `AWAIT_EDIT` | Make the code change for this increment, then run the unit tests. | `tdd run unit` |
| 12 | unit result missing or stale | `RUN_UNIT` | Run the selected unit tests. | `tdd run unit` |
| 13 | unit not green | `FIX_UNIT` | Fix *these* tests (listed), then re-run. Scenarios stay locked. | `tdd run unit` |
| 14 | scenario applicable, result missing or stale | `RUN_SCENARIO` | Unit is green — now run the selected scenarios. | `tdd run scenario` |
| 15 | scenario not green | `FIX_SCENARIO` | Fix *these* scenarios (listed), then re-verify **both** gates — the fix re-opens the unit gate. | `tdd run unit` |
| 16 | all gates green, not dirty | `FINISH_STEP` | Both gates green — close the step. | `tdd step done` |
| 17 | no open step, at least one closed | `NEXT_STEP_OR_DONE` | Start the next increment, or finish the target. | `tdd step start "…"` / `tdd done` |

Row 11b exists because the baseline is taken *before* the increment opens: without it the baseline
run merely looks "stale", `next` sends the model to `run unit`, that passes, scenarios pass, and it
closes an increment in which no code was ever written. It fires only while nothing at all has
happened inside the increment - once a run has occurred there is a real verdict and rows 13/15/16
own it, so the rule can never mask a red gate. Its command stays `run unit` so that a change made to
a file the tracker does not watch (a newly extracted class) cannot deadlock the loop.

Rows 10/10b/10c fire only while the result still stands: once the model has edited, the run is
stale and re-running (row 12) is the right move rather than re-reading an obsolete error. Rows 9 and
17 are split on `steps_done` so both stay reachable — row 9 is the first increment of a target, row
17 every increment after one has closed.

"Stale" in rows 12 and 14 means: no run since the step opened, **or** `freshness.dirty` is set, **or**
the run predates the newest mtime among the target and selected test files. Staleness is recomputed
from the filesystem on every `next` call rather than trusted from state, so an edit made outside
Claude Code (in the IDE, say) still invalidates the gate.

`fs_facts` — mtimes, git HEAD, the existence of report directories — is gathered once per invocation
and passed in, which keeps `resolve_next` pure and therefore testable with no filesystem at all.

---

## 8. CLI reference

All commands are `python .claude/tdd/tddstate.py <cmd>`; the docs abbreviate this to `tdd`. Every
mutating command appends a journal event and regenerates `STATUS.md`. Read commands take `--json`
for machine use; the default output is the capped text block.

### 8.1 Commands

| Command | Effect |
|---|---|
| `init --target <path> [--goal "…"]` | Start a target. Resolves the FQN from the package declaration and the Gradle subproject from the nearest ancestor `build.gradle[.kts]`. Refuses to clobber an in-flight target without `--force`. |
| `status [--json]` | Full picture: target, step, both gates, baselines, recent history. ≤ 40 lines. |
| `next [--json]` | The single next action (§7). The workhorse. |
| `resume [--brief] [--quiet-if-idle]` | Session-opening brief: position + gates + next action + last 3 journal lines. `--quiet-if-idle` prints nothing when no target is active, so the hook stays silent on unrelated sessions. |
| `discover unit` | Ranked JUnit candidates with a reason each (§10.1). Writes `unit.candidates`. |
| `discover scenario` | Ranked Cucumber candidates via the two-hop search (§10.2). |
| `select unit <ids…>` | Confirm the pertinent tests. `--all` takes every candidate scoring ≥ 2. |
| `select scenario <ids…> \| --none` | Same for scenarios; `--none` marks the target as having no scenario coverage. |
| `baseline unit\|scenario` | Run and record the pre-refactor baseline. |
| `step start "<title>"` | Open an increment: assign `sNN`, snapshot the target (+ any `--file` extras), record `git rev-parse HEAD` when available. Refused before the baselines exist, or while another increment is open. |
| `step done` | Close the increment. **Refused** unless both applicable gates are green and `freshness.dirty` is clear. |
| `step abandon` | Close without the gates, journaled as abandoned. |
| `revert-step` | Restore the snapshot taken at `step start`, reset attempt counters and verdicts, and restart the increment's clock so the loop asks for a different change. |
| `run unit [--timeout N]` | Execute Gradle for the selected unit tests, parse reports, update state, print the capped summary (§9). |
| `run scenario [--timeout N] [--force]` | Same for scenarios. Refused while the unit gate is red unless `--force`. |
| `record unit\|scenario --result pass\|fail [--note "…"]` | Manual fallback when the wrapper cannot drive the build. Journaled as a manual record so it is never mistaken for a parsed result. |
| `note "<text>"` | Free-form memory for the next session. |
| `block "<reason>"` / `unblock` | Escalate to the human / clear. |
| `checkpoint` | Regenerate `STATUS.md` from state. Idempotent; safe as a `Stop` hook. |
| `repair` | Rebuild `session.json` by replaying `journal.jsonl`. |
| `doctor` | Environment check (§12). |
| `done` | Archive the finished target to `archive/<ts>-<SimpleName>/` and clear the active session. |
| `touch --from-hook` | Internal: reads hook JSON on stdin, flips `freshness.dirty` when a watched file was edited. Always exits 0. |

### 8.2 Exit codes

| Code | Meaning |
|---|---|
| 0 | Command completed. **A red test result is still 0** — the verdict is data, not an error. |
| 1 | Tracker error: bad usage, unreadable state, missing Gradle wrapper. |
| 2 | Refused by a gate or guard (scenario-before-unit, `step done` while dirty). |
| 10 | State is `blocked`. |

Red tests exiting 0 is a deliberate choice: a weak model reading "command failed" tends to start
debugging the tracker rather than the test.

---

## 9. Gradle integration

### 9.1 Invocation

- **Wrapper resolution** — `gradlew.bat` on Windows, `./gradlew` elsewhere, plain `gradle` as a last
  resort. Searched upward from the target file to the repo root. Absence is a `doctor` failure with a
  clear message, not a stack trace.
- **Subproject** — the nearest ancestor directory of the target containing `build.gradle[.kts]`,
  mapped to its Gradle path (`:app`, `:services:billing`). Single-project builds yield an empty
  prefix.
- **Unit run**
  `<gw> :app:test --tests "com.acme.OrderTest" --tests "com.acme.PricingTest" --console=plain`
- **Scenario run** — the configured `scenario.task` (default `test`, often `cucumberTest` or
  `integrationTest`), restricted to the runner class with `--tests`, and narrowed by filter mode
  (§9.3).
- Always `--console=plain`. The daemon is left enabled — `--no-daemon` would add 10–20 s to every
  iteration of a loop whose whole point is fast feedback.

### 9.2 The up-to-date trap

Gradle skips `test` when nothing it tracks has changed, leaving the **previous** XML in place. A
naive parser then happily reports the old verdict — precisely the "green on stale results" failure
this project exists to prevent.

Mitigation, applied on every run:

1. Delete `build/test-results/<task>/` before invoking Gradle.
2. Record the run start time.
3. After the run, assert that the XML files exist and their mtimes are newer than the start time.
4. If not, the outcome is **`no_results`**, never a pass. `next` then routes to a diagnostic
   instruction ("the test task did not execute — check the task name and the `--tests` filter")
   rather than to a verdict.

### 9.3 Scenario filtering, and the system-property trap

Cucumber on the JUnit Platform is filtered through system properties:

- `-Dcucumber.features=src/test/resources/features/order.feature:14` — exact scenarios
  (`filter_mode: features`);
- `-Dcucumber.filter.tags="@pricing"` — by tag (`filter_mode: tags`);
- `-Dcucumber.plugin="message:build/tdd/cucumber.ndjson"` — machine-readable results.

**The trap:** a `-D` on the Gradle command line reaches the *Gradle* JVM, not the *test* JVM. Unless
the build forwards it, every filter is silently ignored and the full suite runs — slowly, and with
results that do not match what was asked for.

`doctor` probes for forwarding and prints the exact one-liner to add:

```groovy
// build.gradle (Groovy)
tasks.named('test') {
    systemProperties System.properties.findAll { it.key.toString().startsWith('cucumber.') }
}
```

```kotlin
// build.gradle.kts (Kotlin)
tasks.named<Test>("test") {
    systemProperties(System.getProperties().filterKeys { it.toString().startsWith("cucumber.") })
}
```

Without forwarding the runner degrades gracefully to `filter_mode: none` — run the whole Cucumber
task, then filter the *results* down to the selected scenarios — and `next` states plainly that runs
are slower than they need to be until the snippet is added. Degrading loudly beats failing silently.

### 9.4 Report parsing and result classification

**JUnit XML** (`xml.etree.ElementTree`) from `**/build/test-results/<task>/TEST-*.xml`: per test case
`classname`, `name`, status, and the first line of any `<failure>`/`<error>` message. This format is
common to JUnit 4, JUnit 5 and the Platform Suite, so one parser covers every layout in play.

**Cucumber NDJSON**, when the `message` plugin is active: exact `uri:line` → scenario name → status,
which is what makes per-scenario reporting precise. Without it, scenario names are recovered from the
Platform Suite XML test-case names.

**Feature paths are normalised.** Cucumber reports a feature by the path it *loaded* it by —
`classpath:features/order.feature` under `@SelectClasspathResource`, or an absolute `file://` uri —
while discovery works in repo-relative paths. Left alone the two never match, so every scenario id
and the post-hoc filtering of §9.3 would be wrong. Reported uris are matched back onto the known
feature paths by suffix, in either direction: a classpath uri is a suffix of the repo-relative path,
an absolute file uri has it as a suffix.

**Classification** — the distinction that matters most for a weak model:

| Outcome | Detected by | What `next` says |
|---|---|---|
| `passed` | XML fresh, no failures beyond baseline | advance the loop |
| `tests_failed` | XML fresh, failures present | fix *these named tests* |
| `build_failed` | non-zero exit **and** no fresh XML; `error:`/`FAILURE:`/`Execution failed` in output | **fix the compile/build error — the tests never ran** |
| `no_results` | non-zero exit whose output contains `No tests found for given includes` | a `--tests` filter that matches nothing looks exactly like a build failure but is not — calling it one sends the model to fix code that is not broken |
| `no_results` | exit 0 but no fresh XML (§9.2) | diagnose the task/filter |
| `timeout` | subprocess timeout (default 900 s) | re-run or narrow the selection |

Conflating a compile error with a test failure is the single most common way an LLM wastes a cycle
staring at raw Gradle output. Here the classifier makes the call, and for `build_failed` the printed
excerpt is the extracted `error:` region (≤ 60 lines) rather than a test summary.

### 9.5 Context-window protection

This is the second reason the tracker runs the build itself.

- Full output always goes to `logs/<ts>-<kind>.log` — it is never printed.
- The printed summary is hard-capped: a counts line, then **≤ 15 failures**, one line each, message
  truncated to 160 characters.
- Pre-existing baseline failures are summarised as a count, not enumerated.
- Every summary ends with the log path, so the model can `grep` the log deliberately when it needs
  detail — rather than receiving 4 000 lines it did not ask for.

A typical failing run costs roughly 20 lines of context instead of several thousand. Over a long
refactor that difference is the whole budget.

---

## 10. Test discovery

Discovery is deterministic and runs once per target; the selection is then **sticky in state**. This
removes the most expensive part of every restart, and — just as importantly — guarantees that the
same tests are used before and after an interruption.

### 10.1 Unit tests

From `src/main/java/com/acme/Order.java` ⇒ FQN `com.acme.Order`, simple name `Order`:

| Score | Rule |
|---|---|
| 3 | Name match in `src/test/java`: `OrderTest`, `OrderTests`, `TestOrder`, `OrderIT`, `OrderSpec`, `OrderShould` |
| 2 | Test file importing `com.acme.Order`, or referencing `\bOrder\b` outside comments and string literals |
| 1 | Test file in the same package |

Scores 2 and 1 additionally require a JUnit marker (`@Test`, `@ParameterizedTest`, … or
`extends TestCase`). Without that check a fixture helper such as `TestData.java` — which names the
target constantly but contains no test — outranks real tests. Step-definition classes and the
Cucumber runner are excluded outright: they are glue, and they belong to §10.2.

Output is ranked, with the reason shown for each hit, and capped at 25 candidates.

### 10.2 Scenario tests — the two-hop search

Feature files almost never name a Java class, so a direct grep finds nothing. The search therefore
goes through the step definitions:

1. **Hop 1 — step-def classes.** Find files under `src/test/java` containing
   `@Given|@When|@Then|@And|@But` that import or reference the target class or its package.
2. **Extract step expressions** from those annotations. Normalise Cucumber expressions to regexes:
   `{int}` → `\d+`, `{string}` → `"[^"]*"`, `{word}` → `\S+`, `{float}` → `[\d.]+`, `{}` → `.+`.
   Annotations already holding a regex (`^…$`) are used as-is.
3. **Hop 2 — feature files.** Scan `src/test/resources/**/*.feature` for steps matching those
   expressions. For each match, walk upward to the enclosing `Scenario` / `Scenario Outline` /
   `Example` and emit `path:line`, the scenario name, and the tags it inherits from the scenario and
   its feature.
4. **Score:** 3 = the scenario contains a step defined in a class that directly references the
   target; 2 = a **Background** step matched (it runs for every scenario in the file, so it
   implicates all of them and is weaker evidence than a scenario's own step), or the scenario merely
   shares a file with a scoring one; 1 = tag overlap with a scoring scenario.

**Matching is regex-first with a literal-fragment fallback.** A `Scenario Outline` step reads
`Given the order total is <total>`, and no parameter regex matches `<total>`; hand-written step
regexes vary just as widely. So each expression also yields its fixed words (`the order total is`),
and containment of a fragment of four characters or more counts as a match. Discovery only *ranks* —
the model confirms with `select` — so recall is worth more here than precision.

The Cucumber runner class is detected during scenario discovery and stored as
`scenario.runner_class`, which is what later restricts `--tests` on a scenario run. Detection is
deliberately narrow (`@CucumberOptions`, `@Suite`+`IncludeEngines("cucumber")`, `RunWith(Cucumber`):
a marker as loose as `io.cucumber` matches every step-definition class.

**Changing the selection clears that gate's baseline and last result.** Both describe the previous
set of tests, and silently grading a new selection against the old reference is exactly the kind of
quiet wrongness this tool exists to prevent. An id the model names that discovery did not find is
still accepted — discovery ranks, it never vetoes — but is flagged in the output.

Gherkin parsing is line-oriented and deliberately minimal — keywords, tags, and the
`Scenario`/`Outline` boundary. Localised Gherkin keywords are out of scope for v1; `doctor` warns if
a `# language:` header other than `en` is found rather than silently mis-parsing it.

---

## 11. Skill, commands and the CLAUDE.md block

### 11.1 `.claude/skills/tdd-loop/SKILL.md`

Frontmatter: `name: tdd-loop`, a trigger-rich `description`, `user-invocable: true`, and
`allowed-tools` limited to `Bash(python *tddstate.py*)`, `Read`, `Edit`, `Grep`, `Glob`.

The body stays under ~120 lines and is written imperatively for a weak model — numbered rules and
explicit prohibitions, no prose:

```
1. Run `tdd next` FIRST, every time. Do exactly what it says. Then run it again.
2. Never edit code before `tdd step start "<title>"`.
3. Never run scenario tests while the unit gate is red. The tracker will refuse anyway.
4. Never trust a test result older than your last edit. `tdd next` tells you when it is stale.
5. Never paste raw Gradle output. The tracker already summarised it; the log path is there for grep.
6. If `tdd next` says ESCALATE, stop editing. Revert the step or block and ask the human.
7. If anything is confusing, run `tdd status`. If state looks wrong, run `tdd repair`.
```

Depth lives in `references/protocol.md` (phase walkthrough), `references/discovery.md` (how to prune
candidates, including which are safe to drop), and `references/recovery.md` (lost session, corrupted
state, stale lock, `no_results`) — loaded only when needed, so the skill itself stays cheap.

### 11.2 Slash commands

Thin wrappers for the human: `/tdd-next`, `/tdd-status`, `/tdd-target <file>`. Each restricts
`Bash` to the tracker; `/tdd-target` is told not to pass `--force` unless the user explicitly says to
abandon the live target.

**The docs are tested against the CLI.** `tests/test_skill.py` extracts every command, flag, action
code and state path that the skill, its references, the slash commands and the `CLAUDE.md` block
mention, and asserts each one really exists — including the `... <cmd>` shorthand the skill's tables
use, which is where an invented command is most likely to hide. Drift here is not cosmetic: a weak
model types what the docs tell it to type, so a command that no longer exists is a dead end in the
loop. Flag checks are scoped to the backticked span holding the command, since the prose
legitimately mentions Gradle's own flags such as `--tests`.

### 11.3 `CLAUDE.md` block (memory layer 1)

Roughly ten lines appended by the installer between marker comments so re-installation replaces
rather than duplicates:

```markdown
<!-- BEGIN tdd-memory-skill -->
## TDD refactoring loop
This repo tracks refactoring progress in `.claude/tdd/`. Progress survives session loss.
**Before anything else in a session, run:** `python .claude/tdd/tddstate.py next` — and do what it says.
Unit tests must be green before scenario tests are run. Never declare a step done without a fresh run.
Full protocol: `.claude/skills/tdd-loop/SKILL.md`. Human-readable status: `.claude/tdd/STATUS.md`.
<!-- END tdd-memory-skill -->
```

---

## 12. Hooks and installer

### 12.1 Hook block merged into `.claude/settings.json`

| Event | Command | Effect |
|---|---|---|
| `SessionStart` | `<py> …/tddstate.py resume --brief --quiet-if-idle` | A fresh or reconnected session opens already knowing the loop position. |
| `PreCompact` | `<py> …/tddstate.py resume --brief` | The pointer survives compaction. |
| `Stop` | `<py> …/tddstate.py checkpoint` | `STATUS.md` is current whenever the human looks. |
| `PostToolUse` matching `Edit\|Write\|MultiEdit` | `<py> …/tddstate.py touch --from-hook` | Marks `freshness.dirty` when the target or a selected test is edited. |

Every hook is time-boxed (10–15 s) and **exits 0 unconditionally**, so no tracker bug can wedge a
session. `<py>` is the absolute interpreter path resolved at install time from `sys.executable`,
which sidesteps `python` vs `python3` PATH differences on Windows.

Hook event names vary across Claude Code versions; the installer verifies them against the running
version and tells the human to check `/hooks` if a name is unknown, rather than writing config that
silently never fires.

### 12.2 Installer

The installer is **one Python program**, `install/install.py`; `install/install.ps1` and
`install/install.sh` are three-line launchers that locate an interpreter and hand off to it.
Duplicating a JSON merge across PowerShell and POSIX sh would be two implementations to keep in step
and only one of them testable — and Python is already a hard requirement.

It takes the target repo path and:

1. verify Python ≥ 3.8 and a Gradle wrapper;
2. copy `payload/.claude/…` into the repo (never overwriting an existing `SKILL.md` without `-Force`);
3. **merge** the hook block into `.claude/settings.json` — parse, merge, write; never clobber
   existing hooks — writing a `.bak` first;
4. append the `.gitignore` lines and the `CLAUDE.md` block, both marker-delimited and idempotent;
5. run `doctor`.

Hook merging identifies our own entries by the `tddstate.py` marker in the command, removes those,
and appends the current ones. That makes re-installation idempotent *and* lets the baked-in
interpreter path be corrected later, while hooks belonging to anyone else — on the same event or any
other — are carried through untouched. `tddstate.py` is always replaced, since the docs are tested
against it and the two must not drift; any other payload file you have edited yourself is kept, and
reported, unless you pass `--force`.

`--dry-run` reports every change and writes nothing.

`doctor` checks: Python version; the import whitelist; Gradle wrapper present and executable; the
test task exists; `src/test/java` and `**/*.feature` present; **cucumber system-property forwarding**
(§9.3); state directory writable; hook block present in settings; runtime state gitignored; git
availability. Each check prints `OK` / `WARN` / `FAIL` with a one-line fix, and a `FAIL` exits
non-zero.

git is reported but never warned about. It supplies `base_commit`, which is recorded for information
only — `revert-step` restores from the tracker's own snapshots — so on a machine without git a
warning every run would be noise about something that needs no fixing.

The forwarding check is **static** — it reads the build scripts rather than running the suite, which
would be far too slow for a diagnostic — and says so. When a previous scenario run actually observed
the filter being ignored, that is reported as well, as evidence rather than inference. Where a
non-default test task is found (`cucumberTest`, `integrationTest`), `doctor` names it and points at
`select scenario --task <name>`, which is how it gets configured.

---

## 13. Resilience

| Failure | Handling |
|---|---|
| Killed mid-write | `session.json` written to `.tmp` + `os.replace` (atomic on both POSIX and Windows). The journal is flushed first, so it is never behind the state. |
| Corrupted / truncated state | `repair` replays `journal.jsonl`. `next` detects an unparseable state file and routes straight to `repair`. |
| Two sessions at once | Advisory `state/lock` with pid + timestamp; stale after 15 min; `--force-unlock` documented in `references/recovery.md`. |
| Killed mid-run | The lock goes stale and clears; the partial log is kept; the outcome stays `not_run`, so the gate is not falsely green. |
| Unbounded growth | Journal rotates at 2 MB; logs pruned to the newest 50; snapshots to the newest 10; `history_tail` 10; `notes` 50. |
| Schema drift | `schema: 1` is checked on load; a newer schema refuses to run rather than misinterpreting fields. |
| Model ignores the protocol | Three independent backstops: the `CLAUDE.md` block, the `SessionStart` hook, and the gates that refuse out-of-order operations regardless of what the model believes. |

---

## 14. Implementation plan

Steps marked &#10003; are done. Each step is independently verifiable. Steps 2–5 all land in `tddstate.py`; keeping it a single
stdlib file is deliberate — installation is one copy, and there is nothing to break on the unstable
machine.

| # | Deliverable | Acceptance criteria |
|---|---|---|
| 1 &#10003; | `DEVPLAN.md` (this file), `README.md` | Design recorded; README covers install + daily use. |
| 2 &#10003; | **`tddstate.py` core** — atomic state I/O, journal, lock, phase table, `init`, `status`, `next`, `resume`, `note`, `block`/`unblock`, `checkpoint`, `repair`, `touch` | `resolve_next` is pure and covers every row of §7; `repair` reproduces state from the journal; `resume --brief` ≤ 40 lines. |
| 3 &#10003; | **Gradle runner + parsers** — wrapper/subproject resolution, result-dir wipe + freshness assert, JUnit XML + Cucumber NDJSON parsers, outcome classification, capped summary, `run`, `record` | All five outcomes produced on demand from fixtures; printed summary ≤ 20 lines for a 40-failure run; full log on disk. |
| 4 &#10003; | **Discovery** — `discover unit`, `discover scenario` (two-hop), `select` | On the fixture project `OrderTest` ranks first and `order.feature:14` is found through the step-def hop. |
| 5 &#10003; | **Steps and guards** — `step start/done`, snapshots, `revert-step`, ordering + freshness gates, signature/streak escalation, `done`/archive | Each gate refusal exits 2 with a one-line reason; streak hits `ESCALATE` on the third identical signature; `revert-step` restores the snapshot byte-for-byte. |
| 6 &#10003; | **Skill + references + slash commands** | `SKILL.md` ≤ 120 lines; references load only on demand; every documented command, flag and action code verified against the CLI. |
| 7 &#10003; | **Hooks + installer + `doctor`** | Install into a scratch copy of the fixture; re-running the installer is idempotent; existing hooks in `settings.json` survive. |
| 8 &#10003; | **Fixtures + tests** | §15 passes end to end: 277 unit tests plus a 87-check end-to-end walk. |

---

## 15. Verification

### 15.1 Offline unit tests — `python -m unittest discover tests`

No Gradle, no network, runs in seconds:

- `resolve_next` across every phase × gate × staleness combination (table-driven, mirroring §7);
- JUnit XML parsing: JUnit 4, JUnit 5 and Platform Suite samples, including `<error>` vs `<failure>`,
  skipped tests, and an empty suite;
- Cucumber NDJSON parsing → `uri:line` → status;
- outcome classification, especially `build_failed` vs `tests_failed` vs `no_results`;
- failure-signature stability (same failures → same hash, reordered → same hash) and escalation on
  the third repeat;
- discovery scoring and the Cucumber-expression → regex normalisation;
- `repair` reproduces `session.json` from `journal.jsonl`;
- the import whitelist (§2) — mechanically enforcing "no network".

### 15.2 End-to-end against `fixtures/sample-gradle-project` — `fixtures/e2e.py`

A minimal Java 17 + JUnit 5 + `cucumber-java`/`cucumber-junit-platform-engine` project with
`Order.java`, `OrderTest.java`, `PricingSteps.java` and `order.feature`, containing one deliberately
breakable behaviour:

```
init → discover unit (OrderTest first) → select → baseline unit (green)
     → discover scenario (order.feature:14 found via the step-def hop) → select → baseline scenario
     → step start → break Order.java
     → run unit            ⇒ tests_failed, ≤ 20 lines printed, full log on disk
     → run scenario        ⇒ REFUSED, exit 2 (ordering gate)
     → fix → run unit      ⇒ passed
     → run scenario        ⇒ passed
     → step done           ⇒ phase STEP_GREEN, STATUS.md updated
```

Gradle is replaced by a scripted stand-in that writes genuine JUnit XML and Cucumber messages: a
real Gradle run would download JUnit and Cucumber over the network, which §2 forbids. Everything else
is real — the installer, wrapper resolution, the result-directory wipe, the subprocess, the parsing
and the classification. `tests/test_e2e.py` runs the walk as part of the suite; running it directly
prints a transcript, and `--keep` leaves the scratch repo for inspection.

Plus the negative cases that matter most:

- a syntax error in `Order.java` ⇒ `build_failed` with javac lines, **not** a bogus test verdict;
- touching the target after a green run ⇒ `step done` refused, `next` says `RUN_UNIT`;
- a wrong `--tests` filter ⇒ `no_results`, never `passed`;
- removing the cucumber forwarding snippet ⇒ `filter_mode: none` with a visible warning, and the
  results still filtered correctly after the fact.

### 15.3 Resilience drills

- `rm state/session.json` → `repair` restores the position;
- kill Gradle mid-run → stale lock clears, state uncorrupted, outcome not falsely green;
- `resume --brief` from a cold shell prints the full position in ≤ 40 lines.

### 15.4 In-harness

Install into the fixture project, restart Claude Code, confirm the `SessionStart` hook injects the
brief unprompted; edit the target through the Edit tool and confirm `PostToolUse` flips
`freshness.dirty`.

### 15.5 The acceptance test that matters

Mid-refactor, with one gate red, kill the session. Start a brand-new one. **Without being told
anything**, the model must state the target, the selected tests, which gate is red, and the exact
next command — in its first turn, with zero exploratory tool calls.

---

## 16. Risks

| Risk | Mitigation |
|---|---|
| Cucumber system properties not forwarded | `doctor` probe + exact snippet; graceful degrade to post-hoc filtering with a visible warning (§9.3). |
| Gradle `test` UP-TO-DATE ⇒ stale XML | Wipe the result dir, assert mtimes, report `no_results` (§9.2). |
| Non-standard task names (`cucumberTest`, `integrationTest`) | `scenario.task` is configurable and detected at `init`; `doctor` lists the available test tasks. |
| Multi-module layouts | Subproject resolved from the target path; report globbing spans `**/build/test-results/`. |
| Python missing on the target machine | `doctor` fails fast with a clear message. Documented fallback: the model maintains `STATUS.md` by hand under the same template — weaker, but the disk memory survives. |
| Hook event names differ across versions | Installer verifies and points at `/hooks`. |
| Weak model ignores the protocol anyway | The gates refuse out-of-order operations regardless of what the model believes it is doing. |
| Discovery misses an indirectly relevant test | Candidates are ranked but never auto-pruned; the model may `select` anything, including tests discovery scored 0. |
| Localised Gherkin keywords | Out of scope for v1; `doctor` warns on a non-`en` `# language:` header. |

---

## 17. Repo layout (this project)

```
tdd-memory-skill/
  DEVPLAN.md                       # this document
  README.md                        # install + daily use
  payload/                         # copied verbatim into the Java repo
    .claude/skills/tdd-loop/SKILL.md + references/
    .claude/commands/tdd-{next,status,target}.md
    .claude/tdd/tddstate.py
    CLAUDE.tdd.md                  # the block appended to the project's CLAUDE.md
  install/
    install.ps1
    install.sh
  fixtures/
    sample-gradle-project/         # Java 17 + JUnit 5 + Cucumber
    reports/                       # recorded XML/NDJSON for offline parser tests
  tests/                           # unittest suite
```
