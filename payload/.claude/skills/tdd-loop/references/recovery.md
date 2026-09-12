# When something looks wrong

`...` stands for `python .claude/tdd/tddstate.py`. Start with `... status`; it usually answers the
question on its own.

---

## "I have no idea where I was"

That is the normal case after a restart, and it costs nothing:

```bash
python .claude/tdd/tddstate.py next
```

Everything — the target, the selected tests, the gate colours, the open increment — is on disk. You
do not need to re-read the code, re-run discovery, or ask the human. `... resume` prints the same
position with a little recent history.

## "The state file is corrupt / missing"

```bash
python .claude/tdd/tddstate.py repair
```

Every change is written to an append-only journal *before* the state file, so the journal is never
behind. `repair` replays it and rebuilds the state exactly. A half-written final line from a hard
kill is skipped. Nothing is lost, including notes.

## "Another tdd command is running"

An advisory lock is held by a command that died. Locks expire after fifteen minutes on their own. If
you are certain nothing else is running:

```bash
python .claude/tdd/tddstate.py --force-unlock next
```

Read commands (`next`, `status`, `resume`) never take the lock, so they always work.

---

## Run outcomes that are not verdicts

| Summary says | Meaning | Do |
|---|---|---|
| `BUILD FAILED` | Compile or build error. **The tests never ran.** | Fix the error shown. Do not change the tests, and do not read this as a test failure. |
| `NO RESULTS` | The task produced no fresh reports. | **Not a pass.** Usually a wrong task name or a `--tests` filter that matches nothing. Check the filter; `... status` shows the exact command used. |
| `TIMED OUT` | The run exceeded its limit; nothing was verified. | Re-run, or narrow the selection: `... run unit --timeout 1800`. |

Gradle prints `BUILD FAILED` for all three of these. The tracker tells them apart for you — trust
its classification over the raw log.

## "The whole Cucumber suite ran when I selected two scenarios"

`-Dcucumber.*` never reached the test JVM, so the filter was ignored. The results were filtered
afterwards, so the verdict is right, but every run is slower than it needs to be. The build file
needs to forward the properties — `... doctor` prints the exact snippet. Tell the human; it is a
one-line change to `build.gradle`.

---

## "The same tests keep failing"

After three runs with an identical failure signature, `next` stops asking you to fix and says
`ESCALATE`. Take it literally — three identical failures mean the approach is wrong, not that it
needs one more attempt.

```bash
python .claude/tdd/tddstate.py revert-step
```

This restores the files to exactly their content when the increment opened, clears the counters and
re-opens the same increment, so you can try a *different* change. If you do not know what else to
try:

```bash
python .claude/tdd/tddstate.py block "tried X and Y; the discount rule seems to depend on Z"
```

`block` stops the loop until a human clears it. Say what you tried — that note is what they will
read.

## "`step done` refuses to close"

It names the reason in one line. There are only two:

- **a gate is red** — fix the named tests;
- **a result is stale** — you edited after the last run, so re-run it.

Both mean the same thing: the increment is not verified yet. `--force` exists and is journaled as an
override, but closing an unverified increment defeats the point of the loop.

## Overrides

`run scenario --force`, `step done --force` and `done --force` all work and are all recorded in the
journal as overrides. Use them when a human asks you to, not to get past a guard you find
inconvenient.

---

## Where things are

| Path | What |
|---|---|
| `.claude/tdd/STATUS.md` | Human-readable mirror; regenerated on every change. |
| `.claude/tdd/logs/*.log` | Full build output. `grep` here instead of re-running. |
| `.claude/tdd/state/session.json` | Machine state. Never edit it by hand. |
| `.claude/tdd/state/journal.jsonl` | Append-only history; `repair` reads this. |
| `.claude/tdd/snapshots/<id>/` | File copies taken at `step start`, used by `revert-step`. |
| `.claude/tdd/archive/` | Finished targets. |
