# Choosing the right tests

Read this when `next` says `SELECT_UNIT` or `SELECT_SCENARIO` and you are unsure what to keep.

`...` stands for `python .claude/tdd/tddstate.py`.

---

## What the scores mean

Discovery **ranks**; it never decides. Every candidate carries a score and the reason it was found.

### Unit candidates

| Score | Reason | Usually |
|---|---|---|
| 3 | Name match (`Order` → `OrderTest`) | keep |
| 2 | Imports or references the target, and contains real tests | keep if it exercises the behaviour you are changing |
| 1 | Same package only | usually drop |

Files with no `@Test` are never offered — a fixture helper that names the target constantly is not a
test. Step-definition classes and the Cucumber runner are never offered either; they belong to the
scenario side.

### Scenario candidates

| Score | Reason | Usually |
|---|---|---|
| 3 | A step in the scenario is defined in a class that references the target | keep |
| 2 | A **Background** step matched, or the scenario shares a file with a scoring one | keep if the behaviour is related |
| 1 | Shares a tag with a scoring scenario | usually drop |

A Background step runs for every scenario in its file, so it implicates all of them — weaker evidence
than a scenario's own step, which is why it scores 2.

---

## How to prune

Keep a test if changing the target could plausibly change its result. Drop it otherwise.

- **Keep** the obvious name match, and anything asserting the behaviour you are about to move.
- **Keep** a test in another package that drives the target through its public API — that is often
  the one that catches a broken extraction.
- **Drop** score-1 same-package tests unless you have a reason; they slow every run for no signal.
- **Drop** scenarios that merely share a tag.

Every selected test is run on every verification, so a bloated selection costs you on every single
iteration of the loop. A missed test costs you once, when it goes red later. Prefer a tight
selection, and add to it if something surprises you.

## Selecting something discovery missed

You can name any id, not just a discovered one:

```bash
python .claude/tdd/tddstate.py select unit com.acme.OrderTest com.acme.SomethingElseTest
```

Unknown ids are kept and flagged. Discovery ranks, it never vetoes — if you know a test matters, say
so.

## Re-selecting clears the baseline

Changing a selection clears that gate's baseline and last result, and says so. Both described the
*previous* set of tests, and grading a new selection against the old reference would quietly produce
wrong verdicts. After re-selecting, `next` will send you back to `baseline`.

So: get the selection right before baselining. If you must change it mid-target, expect to re-run
the baseline — and do it before your next edit.

## When no scenarios apply

If no step-definition class references the target, discovery says so and offers nothing. Two honest
outcomes:

- the scenarios reach it indirectly — pick them by hand with `select scenario <ids…>`;
- there is genuinely no Cucumber coverage — record that:

```bash
python .claude/tdd/tddstate.py select scenario --none
```

`--none` skips the scenario gate for this target entirely. Use it deliberately: it is the difference
between "verified, nothing to run" and "forgot to check".

## Scenario ids

A scenario id is `path/to/file.feature:<line>`, where the line is the `Scenario:` line. `Scenario
Outline` example rows resolve to the row's own line when the run produces Cucumber message output,
so a single failing example is identified exactly rather than blamed on the whole outline.
