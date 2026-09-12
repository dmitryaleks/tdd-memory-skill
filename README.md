# tdd-memory-skill

Durable memory for an agentic Java refactoring loop in Claude Code.

One Java file at a time: find the JUnit tests that cover it, find the Cucumber scenarios that
exercise it, then refactor in small increments — unit tests green first, scenario tests green second.

The loop position lives on disk, so a dropped connection, a restart or a context window that runs out
before compaction costs nothing. A new session opens already knowing the target, the selected tests,
which gate is red and the exact next command.

**Local only.** Python 3 standard library, no network, no installs, no telemetry. The tracker imports
twelve stdlib modules and a test enforces that list.

---

## Install

```bash
python install/install.py /path/to/your/java-repo
```

That copies the payload into the repo, merges the hooks into `.claude/settings.json` without
disturbing hooks you already have, adds the `.gitignore` entries and the `CLAUDE.md` block, and runs
`doctor`. Re-running it is safe; `--dry-run` shows what it would do.

Windows and POSIX launchers are provided (`install/install.ps1`, `install/install.sh`); both just
hand off to `install.py`.

Requirements: Python 3.8+, a Gradle project with a wrapper. Git is optional — without it an increment
records no base commit, and everything else works.

### Check the setup

```bash
python .claude/tdd/tddstate.py doctor
```

The check most worth reading is **cucumber system-property forwarding**. Unless your build forwards
`-Dcucumber.*` to the test JVM, every scenario filter is silently ignored and the whole suite runs.
`doctor` prints the one-line fix for your build:

```groovy
tasks.named('test') {
    systemProperties System.properties.findAll { it.key.toString().startsWith('cucumber.') }
}
```

Without it the tracker still reports correct verdicts — it filters the results afterwards and says
so — but every run is slower than it needs to be.

---

## Using it

The whole protocol is one command:

```bash
python .claude/tdd/tddstate.py next
```

It prints **one instruction and one command**. Do that one thing, run it again. Nothing has to be
remembered between sessions, and nothing has to be worked out.

```
TDD   target=app/src/main/java/com/acme/Order.java  phase=FIX_UNIT  step=1 "extract PriceCalculator"
UNIT  1 selected | last run 2/3, 1 failing | streak 1
SCEN  2 selected | last run 2/2 green
NEXT  Fix these failing unit tests, then re-run them. Do not touch the scenario tests yet.
      Failing: com.acme.OrderTest#appliesDiscountToLargeOrders. First failure: expected: <135>
      but was: <150>
CMD   python .claude/tdd/tddstate.py run unit
WHY   the unit gate is red; the scenario gate stays locked until it is green
```

A typical target, start to finish:

```bash
tdd init --target app/src/main/java/com/acme/Order.java --goal "extract PriceCalculator"
tdd discover unit        # ranked candidates, with the reason for each
tdd select unit com.acme.OrderTest
tdd discover scenario    # two-hop: target -> step definitions -> feature scenarios
tdd select scenario app/src/test/resources/features/order.feature:14
tdd baseline unit        # before any edit, so pre-existing failures stay pre-existing
tdd baseline scenario
tdd step start "extract PriceCalculator"
#   ... edit the code ...
tdd run unit             # runs gradle, parses the reports, prints ~20 lines
tdd run scenario         # only once unit is green
tdd step done
tdd done                 # archives the target
```

(`tdd` above stands for `python .claude/tdd/tddstate.py`.)

For the human: `/tdd-next`, `/tdd-status` and `/tdd-target <file>` are slash commands, and
`.claude/tdd/STATUS.md` is a readable mirror regenerated on every change.

---

## What it actually enforces

Four guards live in the program rather than in the prompt, so a model cannot talk its way past them.

**Unit before scenario.** `run scenario` is refused while the unit gate is red. A scenario failing
over a red unit gate tells you nothing — it would fail either way.

**Freshness before greenness.** `step done` is refused when a watched file has changed since the run
that verified it. Each run records the mtime of every watched file as it starts, and a result is
stale when any of those differs now. This is the single most common way an agentic loop convinces
itself it has finished.

**Green measured against the baseline.** Tests already failing before you started do not block the
gate; they are counted, not blamed on you. Regressions — tests that passed at baseline and fail now —
are listed first in every summary.

**Three identical failures stop the loop.** Each run gets a failure signature. Three in a row with
the same signature means the approach is wrong, not that it needs another try, so `next` switches to
`ESCALATE`: revert the increment, or hand back to the human.

### Build output stays out of the context window

The tracker runs Gradle itself. Full output goes to `.claude/tdd/logs/`; the model sees a capped
summary of about twenty lines. In the end-to-end walk a failing run costs **4 printed lines against
607 on disk**.

It also separates outcomes Gradle reports identically:

| Outcome | Gradle says | The tracker says |
|---|---|---|
| Compile error | `BUILD FAILED` | **BUILD FAILED — the tests never ran.** Fix the error; change nothing else. |
| Real test failure | `BUILD FAILED` | **tests_failed**, naming the tests |
| Filter matched nothing | `BUILD FAILED` | **NO RESULTS** — not a pass, and not a broken build either |
| Task skipped as up-to-date | `BUILD SUCCESSFUL` | **NO RESULTS** — stale reports are never read as green |

---

## When a session dies

Nothing to do. Run `next`.

Every change is written to an append-only journal *before* the state file, so the journal is never
behind. If the state file is lost or truncated:

```bash
python .claude/tdd/tddstate.py repair
```

It replays the journal and rebuilds the state exactly — including notes, the open increment and the
red gate. `references/recovery.md` covers stale locks, timeouts and the rest.

---

## Layout

```
payload/.claude/           what gets copied into your repo
  tdd/tddstate.py            the tracker: state machine, gradle runner, discovery
  skills/tdd-loop/           SKILL.md + references loaded on demand
  commands/                  /tdd-next, /tdd-status, /tdd-target
  CLAUDE.tdd.md              the block merged into your CLAUDE.md
install/install.py         installer (.ps1 / .sh are launchers)
fixtures/                  sample Gradle project, recorded reports, e2e.py
tests/                     253 tests
DEVPLAN.md                 the design, and why each decision went the way it did
```

Inside your repo at runtime:

```
.claude/tdd/STATUS.md        human-readable mirror
.claude/tdd/state/           session.json + append-only journal.jsonl  (gitignored)
.claude/tdd/logs/            full build output                          (gitignored)
.claude/tdd/snapshots/       per-increment file copies for revert-step  (gitignored)
.claude/tdd/archive/         finished targets                           (gitignored)
```

---

## Tests

```bash
cd tests && python -m unittest discover -s . -p "test_*.py"
```

253 tests, offline, a few seconds. They cover the resolver row by row, the report parsers against
recorded fixtures, discovery against a real sample project, the guards, the installer, and the
documentation itself — every command, flag and action code the skill mentions is checked against the
CLI, because a weak model types what the docs tell it to type.

The end-to-end walk is also runnable on its own:

```bash
python fixtures/e2e.py --keep
```

73 checks against a scratch copy of the sample project, installed the way you would install it,
driving real subprocesses. Gradle is replaced by a stand-in that writes genuine JUnit XML and
Cucumber messages — a real Gradle run would download dependencies, and this project does not use the
network.

---

## Choosing the work: the file, and the tests

Nominating the file is a human decision. Picking the tests is a three-way split, and the split is
deliberate.

### Nominating the file

Either say it in plain language — "refactor `Order.java`, I want the pricing rule extracted", which
is what the skill's description triggers on — or use the slash command:

```
/tdd-target app/src/main/java/com/acme/Order.java extract PriceCalculator
```

Both end in the same call:

```bash
python .claude/tdd/tddstate.py init --target app/src/main/java/com/acme/Order.java \
    --goal "extract PriceCalculator"
```

`init` resolves the fully-qualified name from the file's `package` declaration, and the Gradle
subproject from the nearest ancestor `build.gradle` (`:app`, or `:services:billing`). It refuses to
replace a target that is still active unless given `--force`; finishing the current one with `done`
is almost always what was meant.

The `--goal` is free text and worth writing properly: it appears in every future session's resume
brief, so it is the one piece of intent that survives a reconnect.

### Who picks the tests

| | Role |
|---|---|
| **The tracker** | *Proposes.* Deterministic, ranked candidates, each with the reason it was found. Never decides. |
| **The model** | *Confirms and prunes.* Runs `select`, which is what makes the choice stick. |
| **You** | *Override at any point.* Name ids directly; discovery has no veto. |

The model runs `select`, but it chooses from a ranked list it did not invent. That is the point:
letting an unreliable model free-associate about which tests are relevant is what produced a
different answer every session.

### How the ranking works

**Unit tests**, from `Order.java` ⇒ `com.acme.Order`:

```
FOUND 3 unit candidate(s)
  [3] com.acme.OrderTest              name match: Order -> OrderTest
  [2] com.acme.checkout.CheckoutTest  imports com.acme.Order
  [1] com.acme.TaxTest                same package com.acme
```

Scores 2 and 1 additionally require a JUnit marker (`@Test` and friends). Without that check a
fixture helper such as `TestData.java` — which names the target constantly but contains no test —
outranks real tests. Step-definition classes and the Cucumber runner are excluded outright: they are
glue, and they belong to the scenario side.

**Scenario tests** need two hops, because feature files never name a Java class:

1. find the step-definition classes that import or reference `Order`;
2. extract their `@Given` / `@When` / `@Then` expressions and match those against the steps of every
   `.feature` file, then walk up to the enclosing scenario.

```
FOUND 3 scenario candidate(s)
  [3] app/src/test/resources/features/order.feature:14  Discount applied to large orders  @pricing @discount
      step 'the order total is {int}' is defined in PricingSteps.java, which references Order
NOTE  cucumber runner class detected: com.acme.RunCucumberTest
```

Matching is regex-first with a literal-fragment fallback. A `Scenario Outline` step reads `Given the
order total is <total>`, which no parameter regex matches, so strict matching would silently drop
every outline in the codebase.

The model then confirms:

```bash
tdd select unit com.acme.OrderTest com.acme.checkout.CheckoutTest
tdd select scenario app/src/test/resources/features/order.feature:14
```

### Four things worth knowing

**Selection is sticky.** It lives in the state file, so a restart never re-derives it. That is the
largest single saving on a reconnect, and — more importantly — it guarantees the *same* tests are
used before and after an interruption.

**Discovery ranks, it never vetoes.** An id it did not find is still accepted, and flagged. If you
know a test matters, say so.

**Re-selecting clears that gate's baseline and last result.** Both described the previous set of
tests, and grading a new selection against the old reference produces quietly wrong verdicts. Get
the selection right before baselining, or expect to re-baseline.

**`select scenario --none`** records that a target genuinely has no Cucumber coverage and skips that
gate entirely — the difference between "verified, nothing to run" and "forgot to check".

On pruning: every selected test runs on *every* verification, so a bloated selection taxes each
iteration of the loop, while a missed test costs you once, later. Prefer a tight selection and add to
it when something surprises you. `.claude/skills/tdd-loop/references/discovery.md` covers the
judgement calls.
