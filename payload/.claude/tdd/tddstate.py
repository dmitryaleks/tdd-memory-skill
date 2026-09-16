#!/usr/bin/env python3
"""tddstate.py - durable memory for an agentic Java TDD refactoring loop.

One Java file at a time: find the pertinent JUnit tests, find the pertinent
Cucumber scenarios, refactor, get unit green, then get scenarios green.  This
program owns the loop position so that a session which dies mid-refactor loses
nothing: `tdd next` always prints exactly one instruction and one command.

DEVPLAN.md is the specification.  This file currently implements step 2 (core
state machine); commands belonging to steps 3-5 are registered but stubbed, so
the CLI surface is stable from the start.

Stdlib only, no network.  See DEVPLAN.md section 2 for the import whitelist -
it is enforced by tests/test_no_network.py.
"""

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

SCHEMA = 1
CMD = "python .claude/tdd/tddstate.py"

JOURNAL_MAX_BYTES = 2 * 1024 * 1024
LOCK_STALE_SECONDS = 15 * 60
HISTORY_TAIL_MAX = 10
NOTES_MAX = 50
CANDIDATE_DISPLAY_MAX = 6

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2
EXIT_BLOCKED = 10

# Commands that land in later DEVPLAN steps.  Registered now so the CLI
# surface never changes shape under the model.
# Every command is implemented; the stub machinery stays for future additions.
STUB_STEPS = {}

PHASE_BY_CODE = {
    "REPAIR": "INIT",
    "UNBLOCK": "BLOCKED",
    "SET_TARGET": "INIT",
    "DISCOVER_UNIT": "DISCOVER_UNIT",
    "SELECT_UNIT": "CONFIRM_UNIT",
    "BASELINE_UNIT": "BASELINE_UNIT",
    "DISCOVER_SCENARIO": "DISCOVER_SCENARIO",
    "SELECT_SCENARIO": "CONFIRM_SCENARIO",
    "BASELINE_SCENARIO": "BASELINE_SCENARIO",
    "START_STEP": "READY",
    "AWAIT_EDIT": "STEP_OPEN",
    "FIX_BUILD": "VERIFY_UNIT",
    "DIAGNOSE_RUN": "VERIFY_UNIT",
    "RERUN_TIMEOUT": "VERIFY_UNIT",
    "ESCALATE": "FIX_UNIT",
    "RUN_UNIT": "VERIFY_UNIT",
    "FIX_UNIT": "FIX_UNIT",
    "RUN_SCENARIO": "VERIFY_SCENARIO",
    "FIX_SCENARIO": "FIX_SCENARIO",
    "FINISH_STEP": "STEP_GREEN",
    "NEXT_STEP_OR_DONE": "STEP_GREEN",
}


class TddError(Exception):
    def __init__(self, message, code=EXIT_ERROR):
        Exception.__init__(self, message)
        self.code = code


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def now_iso(epoch=None):
    """UTC, second resolution.  Lexicographic order == chronological order."""
    if epoch is None:
        epoch = time.time()
    dt = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp():
    """Both representations: iso for humans, epoch for exact comparisons."""
    epoch = time.time()
    return now_iso(epoch), epoch


def hhmmss(iso):
    return iso[11:19] if iso and len(iso) >= 19 else "--:--:--"


def clone(value):
    return json.loads(json.dumps(value))


def wrap(text, width=84):
    """Minimal word wrap - `textwrap` is outside the import whitelist."""
    out, cur = [], ""
    for word in str(text).split():
        if cur and len(cur) + 1 + len(word) > width:
            out.append(cur)
            cur = word
        else:
            cur = word if not cur else cur + " " + word
    if cur:
        out.append(cur)
    return out or [""]


def labelled(label, text, width=84):
    """'LABEL  first line' then continuation lines aligned under it."""
    pad = " " * len(label)
    lines = wrap(text, width)
    return [label + lines[0]] + [pad + ln for ln in lines[1:]]


def truncate(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def join_ids(ids, limit=3):
    ids = list(ids)
    head = ", ".join(ids[:limit])
    return head if len(ids) <= limit else "%s (+%d more)" % (head, len(ids) - limit)


# --------------------------------------------------------------------------
# state skeleton
# --------------------------------------------------------------------------

def empty_gate():
    return {
        "candidates": [],
        "selected": [],
        "baseline": None,
        "last_run": None,
        "green": False,
    }


def default_state():
    scenario = empty_gate()
    scenario.update({
        "not_applicable": False,
        "task": "test",
        "runner_class": None,
        "filter_mode": "features",
    })
    return {
        "schema": SCHEMA,
        "updated_at": None,
        "repo_root": None,
        "target": None,
        "phase": "INIT",
        "step": None,
        "steps_done": [],
        "unit": empty_gate(),
        "scenario": scenario,
        "freshness": {"last_verified_at": None, "dirty": False, "dirty_files": []},
        "attempts": {
            "verify_unit": 0,
            "verify_scenario": 0,
            "same_failure_streak": 0,
            "last_signature": None,
        },
        "next_action": None,
        "history_tail": [],
        "notes": [],
        "blocked": None,
    }


def apply_patch(state, patch):
    """Apply {'dotted.path': value} onto state.  The only way state mutates.

    Keeping every mutation expressible as a patch is what makes `repair`
    exact: replaying the journal performs literally the same operations in
    the same order, so there is no reducer to drift out of sync.
    """
    for dotted, value in patch.items():
        parts = dotted.split(".")
        node = state
        for part in parts[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = clone(value)
    return state


# --------------------------------------------------------------------------
# context, paths, io
# --------------------------------------------------------------------------

def find_repo_root(explicit=None):
    if explicit:
        return pathlib.Path(explicit).expanduser().resolve()
    env = os.environ.get("TDD_REPO_ROOT")
    if env:
        return pathlib.Path(env).expanduser().resolve()
    # Installed layout: <repo>/.claude/tdd/tddstate.py
    here = pathlib.Path(__file__).resolve()
    if len(here.parents) >= 3:
        derived = here.parents[2]
        if (derived / ".claude" / "tdd").is_dir():
            return derived
    cur = pathlib.Path.cwd().resolve()
    for cand in [cur] + list(cur.parents):
        if (cand / ".claude" / "tdd").is_dir():
            return cand
        if (cand / ".git").exists() or list(cand.glob("settings.gradle*")):
            return cand
    return cur


class Ctx(object):
    def __init__(self, repo_root):
        self.repo = pathlib.Path(repo_root)
        self.tdd = self.repo / ".claude" / "tdd"
        self.state_dir = self.tdd / "state"
        self.session = self.state_dir / "session.json"
        self.journal = self.state_dir / "journal.jsonl"
        self.journal_prev = self.state_dir / "journal.1.jsonl"
        self.lock = self.state_dir / "lock"
        self.status_md = self.tdd / "STATUS.md"
        self.logs = self.tdd / "logs"
        self.snapshots = self.tdd / "snapshots"
        self.archive = self.tdd / "archive"
        self.state = default_state()
        self.corrupt = False
        self.existed = False

    def ensure_dirs(self):
        for d in (self.state_dir, self.logs, self.snapshots, self.archive):
            d.mkdir(parents=True, exist_ok=True)

    def rel(self, path):
        """Repo-relative POSIX path - state never stores absolute paths."""
        p = pathlib.Path(path)
        if not p.is_absolute():
            p = (self.repo / p)
        try:
            return p.resolve().relative_to(self.repo.resolve()).as_posix()
        except ValueError:
            raise TddError("%s is outside the repository %s" % (path, self.repo))


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(str(tmp), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(str(tmp), str(path))


def load_state(ctx):
    if not ctx.session.exists():
        return ctx
    ctx.existed = True
    try:
        raw = ctx.session.read_text(encoding="utf-8")
        state = json.loads(raw)
    except (ValueError, OSError):
        ctx.corrupt = True
        return ctx
    if not isinstance(state, dict):
        ctx.corrupt = True
        return ctx
    schema = state.get("schema")
    if schema is not None and schema > SCHEMA:
        raise TddError(
            "session.json uses schema %s but this tracker understands %s. "
            "Upgrade .claude/tdd/tddstate.py." % (schema, SCHEMA))
    merged = default_state()
    merged.update(state)
    ctx.state = merged
    return ctx


def write_state(ctx):
    atomic_write(ctx.session, json.dumps(ctx.state, indent=2, sort_keys=False) + "\n")


def append_journal(ctx, event, patch, data=None):
    """Append-only log.  Flushed before session.json is replaced, so the
    journal is never *behind* the state - only ever ahead of it."""
    ctx.state_dir.mkdir(parents=True, exist_ok=True)
    rotate_journal_if_needed(ctx)
    step = ctx.state.get("step") or {}
    record = {
        "ts": now_iso(),
        "event": event,
        "phase": ctx.state.get("phase"),
        "step": step.get("id"),
        "patch": patch,
    }
    if data:
        record["data"] = data
    with open(str(ctx.journal), "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, sort_keys=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def rotate_journal_if_needed(ctx):
    try:
        size = ctx.journal.stat().st_size
    except OSError:
        return
    if size <= JOURNAL_MAX_BYTES:
        return
    os.replace(str(ctx.journal), str(ctx.journal_prev))
    # A snapshot first, so `repair` never needs the rotated-away history.
    snapshot = {"ts": now_iso(), "event": "snapshot", "state": clone(ctx.state)}
    with open(str(ctx.journal), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(snapshot) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def read_journal(ctx):
    if not ctx.journal.exists():
        return []
    events = []
    with open(str(ctx.journal), "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue  # a torn final line is expected after a hard kill
    return events


# --------------------------------------------------------------------------
# advisory lock
# --------------------------------------------------------------------------

class Lock(object):
    """Advisory only.  Guards against two sessions mutating at once; a stale
    lock expires rather than wedging the loop forever."""

    def __init__(self, ctx, force=False):
        self.ctx = ctx
        self.force = force
        self.taken = False

    def __enter__(self):
        ctx = self.ctx
        ctx.state_dir.mkdir(parents=True, exist_ok=True)
        if ctx.lock.exists() and not self.force:
            try:
                info = json.loads(ctx.lock.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                info = {}
            age = time.time() - float(info.get("epoch") or 0)
            if age < LOCK_STALE_SECONDS and int(info.get("pid") or -1) != os.getpid():
                raise TddError(
                    "another tdd command is running (pid %s, held %ds). "
                    "If that is wrong, re-run with --force-unlock."
                    % (info.get("pid"), int(age)), EXIT_REFUSED)
        iso, epoch = stamp()
        ctx.lock.write_text(
            json.dumps({"pid": os.getpid(), "at": iso, "epoch": epoch}),
            encoding="utf-8")
        self.taken = True
        return self

    def __exit__(self, *exc):
        if self.taken:
            try:
                self.ctx.lock.unlink()
            except OSError:
                pass
        return False


# --------------------------------------------------------------------------
# filesystem facts (kept separate so the resolver stays pure)
# --------------------------------------------------------------------------

def watched_files(state):
    """Files whose modification invalidates a test result."""
    paths = []
    target = state.get("target") or {}
    if target.get("path"):
        paths.append(target["path"])
    for item in (state.get("unit") or {}).get("selected") or []:
        if item.get("path"):
            paths.append(item["path"])
    for item in (state.get("scenario") or {}).get("selected") or []:
        ident = item.get("path") or item.get("id") or ""
        paths.append(ident.split(":")[0] if ident else "")
    # The step definitions and the runner, recorded by `discover scenario`.
    # A red scenario is very often repaired in the glue rather than in the
    # production code, and such an edit must invalidate the gates too.
    paths.extend((state.get("scenario") or {}).get("glue") or [])
    return [p for p in dict.fromkeys(paths) if p]


def gather_facts(ctx, state=None):
    state = state if state is not None else ctx.state
    facts = {
        "now": now_iso(),
        "now_epoch": time.time(),
        "newest_epoch": None,
        "newest_path": None,
        "mtimes": {},
        "missing": [],
    }
    for rel in watched_files(state):
        path = ctx.repo / rel
        try:
            mtime = path.stat().st_mtime
        except OSError:
            facts["missing"].append(rel)
            continue
        facts["mtimes"][rel] = mtime
        if facts["newest_epoch"] is None or mtime > facts["newest_epoch"]:
            facts["newest_epoch"] = mtime
            facts["newest_path"] = rel
    return facts


# --------------------------------------------------------------------------
# gate inspection
# --------------------------------------------------------------------------

def gate(state, kind):
    return state.get(kind) or {}


def scenario_applicable(state):
    return not gate(state, "scenario").get("not_applicable", False)


def has_verdict(state, kind):
    run = gate(state, kind).get("last_run")
    return bool(run) and run.get("outcome") not in (None, "not_run")


def run_epoch(run):
    if not run:
        return None
    if run.get("ran_at_epoch") is not None:
        return float(run["ran_at_epoch"])
    iso = run.get("ran_at")
    if not iso:
        return None
    try:
        dt = datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
        return dt.replace(tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return None


def staleness(state, kind, facts):
    """Why a result cannot be trusted, or None if it can.

    Recomputed from the filesystem on every call rather than trusted from
    state, so an edit made outside Claude Code (in the IDE) still counts.
    """
    run = gate(state, kind).get("last_run")
    if not has_verdict(state, kind):
        return "never run"
    ran = run_epoch(run)
    step = state.get("step") or {}
    started = step.get("started_at_epoch")
    if ran is not None and started is not None and ran < float(started):
        return "predates the current step"
    if (state.get("freshness") or {}).get("dirty"):
        files = (state.get("freshness") or {}).get("dirty_files") or []
        return "edited since (%s)" % join_ids(files, 2) if files else "edited since"
    # Exact comparison against the mtimes recorded when the run started.
    # Ordering a file's mtime against the run's clock is unreliable: the two
    # can be microseconds apart, and filesystems differ in mtime resolution,
    # so "before or after?" has no dependable answer at that margin. "Is it
    # the same file as the one that was tested?" always does.
    recorded = run.get("watched")
    if isinstance(recorded, dict):
        current = facts.get("mtimes") or {}
        for rel, was in recorded.items():
            now = current.get(rel)
            if now is None:
                return "%s is gone since the run" % rel
            if abs(float(now) - float(was)) > 1e-6:
                return "%s changed after the run" % rel
        for rel in current:
            if rel not in recorded:
                return "%s appeared after the run" % rel
        return None

    # Manual `record` entries carry no snapshot; fall back to ordering, and
    # resolve a tie as stale rather than blessing an unverified change.
    newest = facts.get("newest_epoch")
    if ran is not None and newest is not None and newest >= ran:
        return "%s changed after the run" % (facts.get("newest_path") or "a watched file")
    return None


def gate_ok(state, kind, facts):
    if kind == "scenario" and not scenario_applicable(state):
        return True
    return bool(gate(state, kind).get("green")) and staleness(state, kind, facts) is None


def latest_run(state):
    """(kind, run) for the most recent run of either gate."""
    best_kind, best_run, best_epoch = None, None, None
    for kind in ("unit", "scenario"):
        run = gate(state, kind).get("last_run")
        if not run:
            continue
        epoch = run_epoch(run) or 0
        if best_epoch is None or epoch >= best_epoch:
            best_kind, best_run, best_epoch = kind, run, epoch
    return best_kind, best_run


def failure_signature(failures):
    """sha1 over sorted 'id|first line of message'.

    Order-independent, so a reordered but otherwise identical failure set
    hashes the same - that is what makes 'no progress' detectable.
    """
    parts = sorted(
        "%s|%s" % (f.get("id", ""), (f.get("message") or "").splitlines()[0].strip()
                   if f.get("message") else "")
        for f in failures or [])
    digest = hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()
    return "sha1:" + digest


# --------------------------------------------------------------------------
# the resolver - DEVPLAN section 7.  Pure: (state, facts) -> next action.
# --------------------------------------------------------------------------

def NA(code, instruction, command, why, row):
    return {"code": code, "instruction": instruction, "command": command,
            "why": why, "row": row}


def _target_name(state):
    return (state.get("target") or {}).get("simple_name") or "the target"


def _r01_blocked(s, f):
    blocked = s.get("blocked")
    if not blocked:
        return None
    return NA("UNBLOCK",
              "Work is BLOCKED: %s. Do not keep editing. Resolve this with the "
              "human, then clear the block." % blocked.get("reason", "no reason recorded"),
              CMD + " unblock",
              "a previous session escalated and stopped the loop", 1)


def _r02_no_target(s, f):
    if s.get("target"):
        return None
    return NA("SET_TARGET",
              "Name the single Java file to refactor, and say what you are changing.",
              CMD + ' init --target <path/to/File.java> --goal "<goal>"',
              "no target is active", 2)


def _r03_discover_unit(s, f):
    u = gate(s, "unit")
    if u.get("candidates") or u.get("selected"):
        return None
    return NA("DISCOVER_UNIT",
              "Find the JUnit tests that cover %s." % _target_name(s),
              CMD + " discover unit",
              "no unit-test candidates recorded yet", 3)


def _r04_select_unit(s, f):
    u = gate(s, "unit")
    if u.get("selected"):
        return None
    ids = [c.get("id", "?") for c in u.get("candidates") or []]
    return NA("SELECT_UNIT",
              "Confirm which of these actually exercise %s: %s"
              % (_target_name(s), join_ids(ids, CANDIDATE_DISPLAY_MAX)),
              CMD + " select unit <ids...>",
              "candidates are found, but nothing is selected yet", 4)


def _r05_baseline_unit(s, f):
    if gate(s, "unit").get("baseline"):
        return None
    return NA("BASELINE_UNIT",
              "Record the unit-test baseline BEFORE editing anything.",
              CMD + " baseline unit",
              "the baseline is what separates pre-existing failures from "
              "regressions you introduce", 5)


def _r06_discover_scenario(s, f):
    if not scenario_applicable(s):
        return None
    sc = gate(s, "scenario")
    if sc.get("candidates") or sc.get("selected"):
        return None
    return NA("DISCOVER_SCENARIO",
              "Find the Cucumber scenarios that exercise %s." % _target_name(s),
              CMD + " discover scenario",
              "no scenario candidates recorded yet", 6)


def _r07_select_scenario(s, f):
    if not scenario_applicable(s):
        return None
    sc = gate(s, "scenario")
    if sc.get("selected"):
        return None
    ids = [c.get("id", "?") for c in sc.get("candidates") or []]
    return NA("SELECT_SCENARIO",
              "Confirm the pertinent scenarios, or declare that none apply: %s"
              % join_ids(ids, CANDIDATE_DISPLAY_MAX),
              CMD + " select scenario <ids...>   (or: " + CMD + " select scenario --none)",
              "candidates are found, but nothing is selected yet", 7)


def _r08_baseline_scenario(s, f):
    if not scenario_applicable(s):
        return None
    if gate(s, "scenario").get("baseline"):
        return None
    return NA("BASELINE_SCENARIO",
              "Record the scenario baseline BEFORE editing anything.",
              CMD + " baseline scenario",
              "the baseline separates pre-existing scenario failures from "
              "regressions you introduce", 8)


def _r09_start_first_step(s, f):
    if s.get("step") or s.get("steps_done"):
        return None
    return NA("START_STEP",
              "Open the first refactor increment. Do not edit any code before this.",
              CMD + ' step start "<what this increment does>"',
              "baselines are recorded and no increment is open", 9)


def _r10_build_broken(s, f):
    kind, run = latest_run(s)
    if not run or run.get("outcome") != "build_failed":
        return None
    if staleness(s, kind, f):
        return None  # already edited since; re-running is the right move
    return NA("FIX_BUILD",
              "The build failed - the tests never ran. Fix the compile/build error "
              "shown in the last summary (full output: %s), then re-run."
              % (run.get("log") or "see .claude/tdd/logs/"),
              CMD + " run " + (kind or "unit"),
              "a build failure is not a test failure; nothing was verified", 10)


def _r10b_no_results(s, f):
    kind, run = latest_run(s)
    if not run or run.get("outcome") != "no_results":
        return None
    if staleness(s, kind, f):
        return None
    return NA("DIAGNOSE_RUN",
              "The test task produced no fresh results. Check the task name and the "
              "--tests filter before trusting anything; do NOT treat this as a pass.",
              CMD + " doctor",
              "no report files were written, so there is no verdict", 10)


def _r10c_timeout(s, f):
    kind, run = latest_run(s)
    if not run or run.get("outcome") != "timeout":
        return None
    if staleness(s, kind, f):
        return None
    return NA("RERUN_TIMEOUT",
              "The last run timed out. Re-run it, or narrow the selection if the "
              "suite is simply too slow.",
              CMD + " run " + (kind or "unit") + " --timeout 1800",
              "a timeout leaves the gate unverified", 10)


def _r11_escalate(s, f):
    streak = (s.get("attempts") or {}).get("same_failure_streak") or 0
    if streak < 3:
        return None
    return NA("ESCALATE",
              "STOP editing. The last %d runs failed identically, so the current "
              "approach is not working. Revert this increment and try a different "
              "one, or hand back to the human." % streak,
              CMD + " revert-step    (or: " + CMD + ' block "<why you are stuck>")',
              "three identical failure signatures means no progress is being made", 11)


def _r11b_await_edit(s, f):
    """An increment is open but nothing has changed yet.

    Without this the baseline run (taken before the step) reads as merely
    "stale", and `next` would send the model straight to `run unit` - which
    passes, then passes scenarios, then closes an increment in which no code
    was ever written.  The command stays `run unit` so that a change made to a
    file the tracker does not watch (a newly extracted class, say) cannot
    deadlock the loop: running simply moves it along.
    """
    step = s.get("step")
    if not step:
        return None
    started = step.get("started_at_epoch")
    if started is None:
        return None
    if (s.get("freshness") or {}).get("dirty"):
        return None
    newest = f.get("newest_epoch")
    if newest is not None and newest > float(started):
        return None
    # Fires only while *nothing at all* has happened in this increment. Once a
    # run has occurred inside it there is a real verdict to act on, and rows
    # 13/15/16 own that - otherwise this rule would mask a red gate.
    for kind in ("unit", "scenario"):
        ran = run_epoch(gate(s, kind).get("last_run"))
        if ran is not None and ran >= float(started):
            return None
    return NA("AWAIT_EDIT",
              "Make the code change for \"%s\". When it is written, run the unit "
              "tests." % step.get("title"),
              CMD + " run unit",
              "the increment is open but no watched file has changed yet", 11)


def _r12_run_unit(s, f):
    why = staleness(s, "unit", f)
    if not why:
        return None
    return NA("RUN_UNIT",
              "Run the selected unit tests: %s"
              % join_ids([i.get("id", "?") for i in gate(s, "unit").get("selected") or []]),
              CMD + " run unit",
              "unit result is %s" % why, 12)


def _r13_fix_unit(s, f):
    if gate(s, "unit").get("green"):
        return None
    run = gate(s, "unit").get("last_run") or {}
    failing = [x.get("id", "?") for x in run.get("failed") or []]
    first = (run.get("failed") or [{}])[0]
    detail = ""
    if first.get("message"):
        detail = " First failure: %s" % truncate(first["message"], 160)
    return NA("FIX_UNIT",
              "Fix these failing unit tests, then re-run them. Do not touch the "
              "scenario tests yet. Failing: %s.%s" % (join_ids(failing), detail),
              CMD + " run unit",
              "the unit gate is red; the scenario gate stays locked until it is green", 13)


def _r14_run_scenario(s, f):
    if not scenario_applicable(s):
        return None
    why = staleness(s, "scenario", f)
    if not why:
        return None
    return NA("RUN_SCENARIO",
              "Unit tests are green. Now run the selected scenarios: %s"
              % join_ids([i.get("id", "?") for i in gate(s, "scenario").get("selected") or []]),
              CMD + " run scenario",
              "scenario result is %s" % why, 14)


def _r15_fix_scenario(s, f):
    if not scenario_applicable(s):
        return None
    if gate(s, "scenario").get("green"):
        return None
    run = gate(s, "scenario").get("last_run") or {}
    failing = [x.get("id", "?") for x in run.get("failed") or []]
    return NA("FIX_SCENARIO",
              "Unit tests pass but these scenarios fail, so the behaviour changed. "
              "Fix them - then re-verify BOTH gates, unit first: the change you make "
              "here can break the unit tests, and it invalidates their last result. "
              "Failing: %s" % join_ids(failing),
              CMD + " run unit",
              "the scenario gate is red, and a fix for it re-opens the unit gate", 15)


def _r16_finish_step(s, f):
    if not s.get("step"):
        return None
    return NA("FINISH_STEP",
              "Both gates are green on fresh runs. Close this increment.",
              CMD + " step done",
              "unit and scenario gates verified after the last edit", 16)


def _r17_next_step_or_done(s, f):
    goal = (s.get("target") or {}).get("goal")
    done = len(s.get("steps_done") or [])
    return NA("NEXT_STEP_OR_DONE",
              "Increment %d is closed and both gates are green. Either open the next "
              "increment towards \"%s\", or finish this target."
              % (done, goal or "the goal"),
              CMD + ' step start "<next increment>"   (or: ' + CMD + " done)",
              "no increment is open and nothing is red", 17)


# Order is the specification.  First match wins.
RULES = [
    _r01_blocked,
    _r02_no_target,
    _r03_discover_unit,
    _r04_select_unit,
    _r05_baseline_unit,
    _r06_discover_scenario,
    _r07_select_scenario,
    _r08_baseline_scenario,
    _r09_start_first_step,
    _r10_build_broken,
    _r10b_no_results,
    _r10c_timeout,
    _r11_escalate,
    _r11b_await_edit,
    _r12_run_unit,
    _r13_fix_unit,
    _r14_run_scenario,
    _r15_fix_scenario,
    _r16_finish_step,
    _r17_next_step_or_done,
]


def resolve_next(state, facts):
    for rule in RULES:
        action = rule(state, facts)
        if action:
            return action
    # Unreachable: _r17 has no guard.  Fail loud rather than silently idle.
    return NA("UNKNOWN", "State is not recognised. Show the full picture and repair "
                         "if it looks wrong.", CMD + " status", "resolver fell through", 0)


def refresh_derived(ctx, facts=None):
    facts = facts or gather_facts(ctx)
    action = resolve_next(ctx.state, facts)
    ctx.state["next_action"] = {k: action[k] for k in ("code", "instruction", "command", "why")}
    ctx.state["phase"] = PHASE_BY_CODE.get(action["code"], ctx.state.get("phase") or "INIT")
    return action, facts


# --------------------------------------------------------------------------
# mutation
# --------------------------------------------------------------------------

def mutate(ctx, event, patch, summary=None, data=None):
    """Apply a patch, journal it, persist, refresh STATUS.md.

    `updated_at` and `history_tail` go into the patch itself so that a journal
    replay reproduces them exactly.
    """
    iso = now_iso()
    full = dict(patch)
    full["updated_at"] = iso
    if summary:
        tail = list(ctx.state.get("history_tail") or [])
        tail.insert(0, "%s %s" % (hhmmss(iso), summary))
        full["history_tail"] = tail[:HISTORY_TAIL_MAX]
    apply_patch(ctx.state, full)
    refresh_derived(ctx)
    append_journal(ctx, event, full, data)
    write_state(ctx)
    write_status_md(ctx)


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def header_line(state):
    target = state.get("target") or {}
    if not target:
        return "TDD   no active target"
    step = state.get("step") or {}
    bits = ["TDD   target=%s" % target.get("path")]
    bits.append("phase=%s" % state.get("phase"))
    if step:
        bits.append('step=%s "%s"' % (step.get("n"), step.get("title")))
    return "  ".join(bits)


def gate_line(state, kind, facts, label=None):
    label = label if label is not None else ("UNIT  " if kind == "unit" else "SCEN  ")
    if kind == "scenario" and not scenario_applicable(state):
        return label + "n/a - this target has no scenario coverage"
    g = gate(state, kind)
    selected = g.get("selected") or []
    bits = ["%d selected" % len(selected)]
    run = g.get("last_run")
    if not run or run.get("outcome") in (None, "not_run"):
        bits.append("never run")
    elif run.get("outcome") == "passed":
        bits.append("last run %d/%d green" % (run.get("pass", 0), run.get("total", 0)))
    elif run.get("outcome") == "tests_failed":
        bits.append("last run %d/%d, %d failing"
                    % (run.get("pass", 0), run.get("total", 0), run.get("fail", 0)))
    else:
        bits.append("last run %s" % run.get("outcome"))
    streak = (state.get("attempts") or {}).get("same_failure_streak") or 0
    if streak and kind == "unit":
        bits.append("streak %d" % streak)
    why = staleness(state, kind, facts)
    if why and run:
        bits.append("STALE: %s" % why)
    return label + " | ".join(bits)


def render_next(state, facts):
    action = state.get("next_action") or {}
    lines = [header_line(state)]
    if state.get("target"):
        lines.append(gate_line(state, "unit", facts))
        lines.append(gate_line(state, "scenario", facts))
    lines += labelled("NEXT  ", action.get("instruction", ""))
    if action.get("command"):
        lines.append("CMD   " + action["command"])
    if action.get("why"):
        lines += labelled("WHY   ", action["why"])
    return "\n".join(lines)


def render_status(state, facts):
    lines = [header_line(state)]
    target = state.get("target") or {}
    if target.get("goal"):
        lines += labelled("GOAL  ", target["goal"])
    if target.get("gradle_project") is not None:
        lines.append("BUILD gradle project '%s', scenario task '%s'"
                     % (target.get("gradle_project") or ":", gate(state, "scenario").get("task")))
    done = state.get("steps_done") or []
    if done:
        lines.append("STEPS %d closed: %s"
                     % (len(done), join_ids([d.get("title", "?") for d in done], 3)))
    for kind in ("unit", "scenario"):
        lines.append(gate_line(state, kind, facts))
        g = gate(state, kind)
        if kind == "scenario" and not scenario_applicable(state):
            continue
        for item in (g.get("selected") or [])[:6]:
            lines.append("        - %s" % item.get("id"))
        base = g.get("baseline")
        if base:
            lines.append("      baseline %d/%d at %s"
                         % (base.get("pass", 0), base.get("total", 0), hhmmss(base.get("ran_at"))))
        run = g.get("last_run") or {}
        for fail in (run.get("failed") or [])[:5]:
            lines.append("      FAIL %s  %s"
                         % (fail.get("id"), truncate(fail.get("message") or "", 90)))
        if run.get("log"):
            lines.append("      log  %s" % run["log"])
    notes = state.get("notes") or []
    for note in notes[:3]:
        lines += labelled("NOTE  ", note.get("text", ""))
    for entry in (state.get("history_tail") or [])[:5]:
        lines.append("      " + entry)
    lines.append("")
    lines.append(render_next(state, facts))
    return "\n".join(lines)


def render_resume(state, facts, journal_tail):
    action = state.get("next_action") or {}
    target = state.get("target") or {}
    step = state.get("step") or {}
    lines = ["=== TDD LOOP - RESUMING WHERE THE LAST SESSION STOPPED ==="]
    lines.append("target  %s" % target.get("path"))
    if target.get("goal"):
        lines += labelled("goal    ", target["goal"], 78)
    lines.append("phase   %s%s" % (state.get("phase"),
                                   ('   step %s "%s"' % (step.get("n"), step.get("title")))
                                   if step else ""))
    lines.append(gate_line(state, "unit", facts, "unit    "))
    lines.append(gate_line(state, "scenario", facts, "scen    "))
    lines += labelled("NEXT    ", action.get("instruction", ""), 78)
    if action.get("command"):
        lines.append("CMD     " + action["command"])
    for i, entry in enumerate(journal_tail[:3]):
        lines.append(("recent  " if i == 0 else "        ") + entry)
    for i, note in enumerate((state.get("notes") or [])[:2]):
        lines += labelled("note    " if i == 0 else "        ",
                          note.get("text", ""), 78)
    lines.append("rule    Run `%s next` before anything else, and again after "
                 "every action." % CMD)
    lines.append("=" * 58)
    return "\n".join(lines)


def write_status_md(ctx):
    state = ctx.state
    facts = gather_facts(ctx)
    target = state.get("target") or {}
    step = state.get("step") or {}
    action = state.get("next_action") or {}
    out = []
    out.append("# TDD status")
    out.append("")
    out.append("_Generated by `.claude/tdd/tddstate.py`. Do not edit by hand - "
               "it is overwritten on every state change._")
    out.append("")
    if not target:
        out.append("No active target. Start one with:")
        out.append("")
        out.append("```")
        out.append(CMD + " init --target <path/to/File.java> --goal \"<goal>\"")
        out.append("```")
        atomic_write(ctx.status_md, "\n".join(out) + "\n")
        return
    out.append("| | |")
    out.append("|---|---|")
    out.append("| Target | `%s` |" % target.get("path"))
    out.append("| Goal | %s |" % (target.get("goal") or "-"))
    out.append("| Phase | **%s** |" % state.get("phase"))
    out.append("| Increment | %s |"
               % (('%s - "%s"' % (step.get("id"), step.get("title"))) if step else "none open"))
    out.append("| Updated | %s |" % state.get("updated_at"))
    out.append("")
    out.append("## Next action")
    out.append("")
    out.append("> %s" % (action.get("instruction") or "-"))
    out.append("")
    out.append("```")
    out.append(action.get("command") or "")
    out.append("```")
    out.append("")
    for kind, title in (("unit", "Unit tests (JUnit)"), ("scenario", "Scenario tests (Cucumber)")):
        out.append("## %s" % title)
        out.append("")
        if kind == "scenario" and not scenario_applicable(state):
            out.append("_Not applicable: this target has no scenario coverage._")
            out.append("")
            continue
        g = gate(state, kind)
        why = staleness(state, kind, facts)
        out.append("- Gate: **%s**%s" % ("GREEN" if gate_ok(state, kind, facts) else "RED",
                                         (" (stale: %s)" % why) if why else ""))
        selected = g.get("selected") or []
        out.append("- Selected (%d):" % len(selected))
        for item in selected:
            out.append("  - `%s`%s" % (item.get("id"),
                                       (" - %s" % item["name"]) if item.get("name") else ""))
        run = g.get("last_run")
        if run:
            out.append("- Last run: `%s` at %s -> **%s** (%s/%s passed)"
                       % (run.get("cmd"), run.get("ran_at"), run.get("outcome"),
                          run.get("pass"), run.get("total")))
            for fail in (run.get("failed") or [])[:15]:
                out.append("  - FAIL `%s` - %s"
                           % (fail.get("id"), truncate(fail.get("message") or "", 160)))
            if run.get("log"):
                out.append("- Full output: `.claude/tdd/%s`" % run["log"])
        out.append("")
    if state.get("steps_done"):
        out.append("## Closed increments")
        out.append("")
        for entry in state["steps_done"]:
            out.append("- `%s` %s (%s)" % (entry.get("id"), entry.get("title"),
                                           entry.get("finished_at")))
        out.append("")
    if state.get("notes"):
        out.append("## Notes")
        out.append("")
        for note in state["notes"]:
            out.append("- %s - %s" % (hhmmss(note.get("at")), note.get("text")))
        out.append("")
    if state.get("history_tail"):
        out.append("## Recent activity")
        out.append("")
        for entry in state["history_tail"]:
            out.append("- %s" % entry)
        out.append("")
    atomic_write(ctx.status_md, "\n".join(out) + "\n")


def journal_tail_lines(ctx, limit=3):
    out = []
    for event in reversed(read_journal(ctx)):
        if event.get("event") == "snapshot":
            continue
        data = event.get("data") or {}
        summary = data.get("summary") or event.get("event")
        out.append("%s %s" % (hhmmss(event.get("ts")), summary))
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------
# discovery: which tests cover this file  (DEVPLAN section 10)
# --------------------------------------------------------------------------

MAX_CANDIDATES = 25
CANDIDATES_DISPLAYED = 12
MIN_LITERAL_FRAGMENT = 4

EXCLUDED_DIRS = {"build", "out", "target", "bin", ".git", ".gradle", ".idea",
                 ".claude", "node_modules", ".venv"}

UNIT_NAME_PATTERNS = ("%sTest", "%sTests", "Test%s", "%sIT", "%sITCase",
                      "%sSpec", "%sShould", "%sTestCase")

JUNIT_MARKERS = ("@Test", "@ParameterizedTest", "@RepeatedTest", "@TestFactory",
                 "@TestTemplate", "extends TestCase")

STEP_ANNOTATIONS = ("@Given", "@When", "@Then", "@And", "@But")

# Deliberately narrow: every step-definition class imports io.cucumber, so a
# generic marker would identify glue as the runner.
CUCUMBER_RUNNER_MARKERS = ("@CucumberOptions", "RunWith(Cucumber",
                           'IncludeEngines("cucumber")', "SelectClasspathResource")

# Java string/comment tokens.  Kept as one alternation so a `//` inside a
# string literal is not mistaken for a comment.
_JAVA_TOKEN_RE = re.compile(
    r'"""(?:.|\n)*?"""|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|//[^\n]*|/\*(?:.|\n)*?\*/',
    re.S)

_STEP_ANNOTATION_RE = re.compile(
    r'@(Given|When|Then|And|But)\s*\(\s*(?:value\s*=\s*)?"((?:\\.|[^"\\])*)"', re.S)

CUCUMBER_PARAMS = {
    "{int}": r"[-+]?\d+", "{byte}": r"[-+]?\d+", "{short}": r"[-+]?\d+",
    "{long}": r"[-+]?\d+", "{float}": r"[-+]?\d*\.?\d+",
    "{double}": r"[-+]?\d*\.?\d+", "{bigdecimal}": r"[-+]?\d*\.?\d+",
    "{biginteger}": r"[-+]?\d+", "{word}": r"\S+", "{string}": r'"[^"]*"',
    "{}": r".+",
}

GHERKIN_FEATURE_RE = re.compile(r"^\s*Feature\s*:\s*(.*)$")
GHERKIN_BACKGROUND_RE = re.compile(r"^\s*Background\s*:")
GHERKIN_SCENARIO_RE = re.compile(
    r"^\s*(Scenario Outline|Scenario Template|Scenario|Example)\s*:\s*(.*)$")
GHERKIN_STEP_RE = re.compile(r"^\s*(?:Given|When|Then|And|But|\*)\s+(\S.*)$")
GHERKIN_TAG_RE = re.compile(r"^\s*(@\S+(?:\s+@\S+)*)\s*$")
GHERKIN_LANGUAGE_RE = re.compile(r"^\s*#\s*language\s*:\s*(\S+)")


def _blank_out(text):
    """Replace a token with spaces, keeping newlines so line numbers hold."""
    return re.sub(r"[^\n]", " ", text)


def strip_java_comments(source):
    """Comments out, string literals kept - annotations live in strings."""
    return _JAVA_TOKEN_RE.sub(
        lambda m: _blank_out(m.group(0)) if m.group(0).startswith("/") else m.group(0),
        source)


def java_code_only(source):
    """Comments *and* string literals out, so `"Order"` in a message is not
    mistaken for a reference to the class."""
    return _JAVA_TOKEN_RE.sub(lambda m: _blank_out(m.group(0)), source)


def unescape_java(literal):
    """The Java source `"\\\\d+"` holds the regex `\\d+`."""
    return re.sub(r'\\(.)', lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)),
                  literal)


def iter_java_files(base):
    if not base.is_dir():
        return
    for path in sorted(base.rglob("*.java")):
        rel = path.relative_to(base)
        if any(part in EXCLUDED_DIRS for part in rel.parts[:-1]):
            continue
        yield path


def find_dirs(ctx, pattern):
    out = []
    for path in sorted(ctx.repo.rglob(pattern)):
        if not path.is_dir():
            continue
        rel = path.relative_to(ctx.repo)
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        out.append(path)
    return out


def test_source_roots(ctx):
    return find_dirs(ctx, "src/test/java")


def feature_files(ctx):
    out = []
    for path in sorted(ctx.repo.rglob("*.feature")):
        rel = path.relative_to(ctx.repo)
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        out.append(path)
    return out


def read_text(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def java_package(source):
    match = re.search(r"^\s*package\s+([A-Za-z_][\w.]*)\s*;", source, re.M)
    return match.group(1) if match else None


def load_test_sources(ctx):
    """Every Java file under a test root, read once and classified."""
    out = []
    for root in test_source_roots(ctx):
        for path in iter_java_files(root):
            source = read_text(path)
            no_comments = strip_java_comments(source)
            package = java_package(no_comments)
            simple = path.stem
            out.append({
                "path": ctx.rel(path), "file": path, "simple": simple,
                "fqn": "%s.%s" % (package, simple) if package else simple,
                "package": package, "source": no_comments,
                "code": java_code_only(source),
                "is_junit": any(m in no_comments for m in JUNIT_MARKERS),
                "is_stepdef": any(m in no_comments for m in STEP_ANNOTATIONS),
                "is_runner": any(m in no_comments for m in CUCUMBER_RUNNER_MARKERS),
            })
    return out


def references_target(entry, target):
    """Why this file looks related to the target, or None."""
    fqn, simple = target["class_fqn"], target["simple_name"]
    if re.search(r"^\s*import\s+%s\s*;" % re.escape(fqn), entry["source"], re.M):
        return "imports %s" % fqn
    if re.search(r"\b%s\b" % re.escape(simple), entry["code"]):
        return "references %s" % simple
    if target.get("package") and entry.get("package") == target["package"]:
        return "same package %s" % target["package"]
    return None


def discover_unit(ctx, state):
    target = dict(state.get("target") or {})
    target["package"] = (target.get("class_fqn") or "").rsplit(".", 1)[0] \
        if "." in (target.get("class_fqn") or "") else None
    simple = target.get("simple_name") or ""
    wanted = set(pattern % simple for pattern in UNIT_NAME_PATTERNS)

    candidates = []
    for entry in load_test_sources(ctx):
        # Glue and runners are not unit tests, however much they mention the
        # class - they belong to scenario discovery instead.
        if entry["is_stepdef"] or entry["is_runner"]:
            continue
        score, reason = 0, None
        if entry["simple"] in wanted:
            score, reason = 3, "name match: %s -> %s" % (simple, entry["simple"])
        elif entry["is_junit"]:
            why = references_target(entry, target)
            if why and why.startswith("same package"):
                score, reason = 1, why
            elif why:
                score, reason = 2, why
        if not score:
            continue
        if score == 3 and not entry["is_junit"]:
            reason += " (no @Test found - check it is really a test)"
        candidates.append({"id": entry["fqn"], "path": entry["path"],
                           "score": score, "reason": reason})
    candidates.sort(key=lambda c: (-c["score"], c["id"]))
    return candidates[:MAX_CANDIDATES]


def cucumber_to_regex(expression):
    """Cucumber expression -> regex. Already-anchored regexes pass through."""
    if expression.startswith("^") or expression.endswith("$"):
        return expression
    parts = re.split(r"(\{[a-zA-Z]*\})", expression)
    out = []
    for part in parts:
        if part.startswith("{") and part.endswith("}"):
            out.append(CUCUMBER_PARAMS.get(part.lower(), r".+"))
        else:
            out.append(_literal_to_regex(part))
    return "^" + "".join(out) + "$"


def _literal_to_regex(chunk):
    """`cucumber(s)` -> optional text, everything else escaped."""
    out = []
    for token in re.split(r"(\([^()]*\))", chunk):
        if token.startswith("(") and token.endswith(")") and len(token) > 2:
            out.append("(?:%s)?" % re.escape(token[1:-1]))
        else:
            out.append(re.escape(token))
    return "".join(out)


def literal_fragments(expression):
    """The fixed words of a step expression.

    Used as a recall fallback: Scenario Outline steps carry `<placeholders>`
    that no parameter regex matches, and hand-written regexes vary wildly.
    Discovery only ranks - the model confirms with `select` - so recall beats
    precision here.
    """
    body = re.sub(r"\{[a-zA-Z]*\}", "\x00", expression)
    body = re.sub(r"\([^()]*\)", "\x00", body)
    body = re.sub(r"[\[\]\\^$*+?|]", "\x00", body.strip("^$"))
    return [frag.strip() for frag in body.split("\x00")
            if len(frag.strip()) >= MIN_LITERAL_FRAGMENT]


def step_matchers(entry):
    """Every step expression defined in one step-definition class."""
    out = []
    for _keyword, literal in _STEP_ANNOTATION_RE.findall(entry["source"]):
        expression = unescape_java(literal)
        try:
            regex = re.compile(cucumber_to_regex(expression), re.I)
        except re.error:
            regex = None
        out.append({"expr": expression, "regex": regex,
                    "fragments": literal_fragments(expression),
                    "class": entry["simple"]})
    return out


def match_step(matchers, text):
    for matcher in matchers:
        if matcher["regex"] is not None and matcher["regex"].search(text):
            return matcher
    lowered = text.lower()
    for matcher in matchers:
        for fragment in matcher["fragments"]:
            if fragment.lower() in lowered:
                return matcher
    return None


def parse_feature(path, rel):
    """Line-oriented Gherkin: enough for tags, scenario boundaries and steps."""
    doc = {"path": rel, "name": None, "tags": [], "language": "en",
           "background": [], "scenarios": []}
    pending_tags, current, in_background = [], None, False
    for number, raw in enumerate(read_text(path).splitlines(), start=1):
        language = GHERKIN_LANGUAGE_RE.match(raw)
        if language:
            doc["language"] = language.group(1)
            continue
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        tags = GHERKIN_TAG_RE.match(raw)
        if tags:
            pending_tags.extend(tags.group(1).split())
            continue
        feature = GHERKIN_FEATURE_RE.match(raw)
        if feature:
            doc["name"] = feature.group(1).strip()
            doc["tags"] = pending_tags
            pending_tags, current, in_background = [], None, False
            continue
        if GHERKIN_BACKGROUND_RE.match(raw):
            current, in_background, pending_tags = None, True, []
            continue
        scenario = GHERKIN_SCENARIO_RE.match(raw)
        if scenario:
            current = {"line": number, "name": scenario.group(2).strip(),
                       "tags": doc["tags"] + pending_tags, "steps": []}
            doc["scenarios"].append(current)
            pending_tags, in_background = [], False
            continue
        step = GHERKIN_STEP_RE.match(raw)
        if step:
            text = step.group(1).strip()
            if in_background:
                doc["background"].append(text)
            elif current is not None:
                current["steps"].append(text)
    return doc


def discover_scenario(ctx, state):
    """Two hops: target -> step-definition classes -> feature scenarios."""
    target = dict(state.get("target") or {})
    target["package"] = (target.get("class_fqn") or "").rsplit(".", 1)[0] \
        if "." in (target.get("class_fqn") or "") else None

    sources = load_test_sources(ctx)
    runner_entry = next((e for e in sources
                         if e["is_runner"] and not e["is_stepdef"]), None)
    runner = runner_entry["fqn"] if runner_entry else None

    matchers, glue, glue_paths = [], [], []
    for entry in sources:
        if not entry["is_stepdef"]:
            continue
        why = references_target(entry, target)
        if not why:
            continue
        glue.append("%s (%s)" % (entry["simple"], why))
        glue_paths.append(entry["path"])
        matchers.extend(step_matchers(entry))
    if runner_entry:
        glue_paths.append(runner_entry["path"])

    notes = []
    if not glue:
        notes.append("no step-definition class references %s - either the scenarios "
                     "reach it indirectly (select them by hand) or there is no "
                     "scenario coverage (`%s select scenario --none`)"
                     % (target.get("simple_name"), CMD))

    scored, by_file, tagged = {}, {}, {}
    for path in feature_files(ctx):
        rel = ctx.rel(path)
        doc = parse_feature(path, rel)
        if doc["language"] != "en":
            notes.append("%s declares language '%s'; only English keywords are "
                         "parsed, so its scenarios may be missed"
                         % (rel, doc["language"]))
        background_hit = match_step(matchers, " ".join(doc["background"])) \
            if doc["background"] and matchers else None
        for scenario in doc["scenarios"]:
            ident = "%s:%d" % (rel, scenario["line"])
            by_file.setdefault(rel, []).append((ident, scenario))
            hit, score, reason = None, 0, None
            for text in scenario["steps"]:
                hit = match_step(matchers, text)
                if hit:
                    score = 3
                    reason = "step '%s' is defined in %s.java, which references %s" % (
                        truncate(hit["expr"], 50), hit["class"],
                        target.get("simple_name"))
                    break
            if not hit and background_hit:
                score = 2
                reason = ("Background step '%s' is defined in %s.java and runs for "
                          "every scenario in this feature"
                          % (truncate(background_hit["expr"], 50),
                             background_hit["class"]))
            if score:
                scored[ident] = {"id": ident, "name": scenario["name"],
                                 "tags": scenario["tags"], "score": score,
                                 "reason": reason}
                if score == 3:
                    for tag in scenario["tags"]:
                        tagged.setdefault(tag, ident)

    # Score 2: other scenarios in a file that already scored.
    for rel, entries in by_file.items():
        if not any(scored.get(ident, {}).get("score") == 3 for ident, _ in entries):
            continue
        for ident, scenario in entries:
            if ident not in scored:
                scored[ident] = {"id": ident, "name": scenario["name"],
                                 "tags": scenario["tags"], "score": 2,
                                 "reason": "same feature file as a matching scenario"}
    # Score 1: tag overlap with a scoring scenario, elsewhere.
    if tagged:
        for rel, entries in by_file.items():
            for ident, scenario in entries:
                if ident in scored:
                    continue
                shared = [t for t in scenario["tags"] if t in tagged]
                if shared:
                    scored[ident] = {"id": ident, "name": scenario["name"],
                                     "tags": scenario["tags"], "score": 1,
                                     "reason": "shares %s with a matching scenario"
                                               % shared[0]}

    candidates = sorted(scored.values(), key=scenario_sort_key)[:MAX_CANDIDATES]
    return candidates, runner, glue_paths, notes


def scenario_sort_key(cand):
    """Rank by score, then file, then *numeric* line - ':8' precedes ':14'."""
    path, _, line = cand["id"].rpartition(":")
    try:
        return (-cand["score"], path, int(line))
    except ValueError:
        return (-cand["score"], cand["id"], 0)


def find_test_path(ctx, fqn):
    for entry in load_test_sources(ctx):
        if entry["fqn"] == fqn or entry["simple"] == fqn:
            return entry["path"], entry["fqn"]
    return None, fqn


def render_candidates(kind, candidates, notes):
    if not candidates:
        lines = ["FOUND 0 %s candidates" % kind]
    else:
        lines = ["FOUND %d %s candidate(s)%s" % (
            len(candidates), kind,
            " (showing %d)" % CANDIDATES_DISPLAYED
            if len(candidates) > CANDIDATES_DISPLAYED else "")]
    for cand in candidates[:CANDIDATES_DISPLAYED]:
        head = cand["id"]
        if cand.get("name"):
            head += "  %s" % truncate(cand["name"], 48)
        if cand.get("tags"):
            head += "  %s" % " ".join(cand["tags"][:3])
        lines.append("  [%d] %s" % (cand["score"], truncate(head, 104)))
        lines.append("      %s" % truncate(cand["reason"], 100))
    for note in notes or []:
        lines += labelled("NOTE  ", note)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# gradle: invocation, report parsing, classification  (DEVPLAN section 9)
# --------------------------------------------------------------------------

DEFAULT_TIMEOUT = 900
SUMMARY_LINE_BUDGET = 20
MAX_FAILURES_SHOWN = 15
MAX_FAILURES_STORED = 25
MAX_MESSAGE_CHARS = 160
MAX_BUILD_ERROR_LINES = 60
LOGS_KEPT = 50

BUILD_ERROR_MARKERS = (
    "* What went wrong:",
    "FAILURE: Build failed",
    "Execution failed for task",
    "Compilation failed",
    "Could not resolve",
    "Could not find method",
    "Could not determine",
)

# Gradle says this when a --tests filter matches nothing.  It exits non-zero
# and writes no reports, which looks exactly like a build failure but is not:
# it is a filter problem, and must read as `no_results`.
NO_TESTS_MARKER = "No tests found for given includes"

CUCUMBER_STATUS_ORDER = ["PASSED", "SKIPPED", "PENDING", "UNDEFINED",
                         "AMBIGUOUS", "FAILED"]


def gradle_wrapper(ctx, module):
    """Wrapper first, PATH gradle as a last resort."""
    name = "gradlew.bat" if os.name == "nt" else "gradlew"
    seen = []
    for base in [ctx.repo] + [module] + list(module.parents):
        if base in seen or not str(base).startswith(str(ctx.repo)):
            continue
        seen.append(base)
        cand = base / name
        if cand.is_file():
            if os.name != "nt" and not os.access(str(cand), os.X_OK):
                # A wrapper checked out without the exec bit still works via sh.
                return ["sh", str(cand)]
            return [str(cand)]
    found = shutil.which("gradle")
    if found:
        return [found]
    raise TddError(
        "no Gradle wrapper (%s) under %s and no `gradle` on PATH" % (name, ctx.repo))


def module_dir(ctx, gradle_project):
    if not gradle_project:
        return ctx.repo
    return ctx.repo.joinpath(*[p for p in gradle_project.split(":") if p])


def task_path(gradle_project, task):
    return "%s:%s" % (gradle_project, task) if gradle_project else task


def results_dir(ctx, gradle_project, task):
    return module_dir(ctx, gradle_project) / "build" / "test-results" / task


def wipe_results(path):
    """Deleting the task's declared output both clears stale XML and makes
    Gradle consider the task out of date, so it actually re-runs."""
    shutil.rmtree(str(path), ignore_errors=True)


def gradle_test_pattern(ident):
    """'a.b.CTest#m' -> 'a.b.CTest.m'; Gradle's --tests uses dots."""
    return (ident or "").replace("#", ".")


def tag_expression(selected):
    tags = []
    for item in selected:
        for tag in item.get("tags") or []:
            if tag not in tags:
                tags.append(tag)
    return " or ".join(tags)


def build_argv(ctx, state, kind, task, gradle_project, module):
    argv = list(gradle_wrapper(ctx, module))
    argv.append(task_path(gradle_project, task))
    if kind == "unit":
        for item in gate(state, "unit").get("selected") or []:
            if item.get("id"):
                argv += ["--tests", gradle_test_pattern(item["id"])]
    else:
        sc = gate(state, "scenario")
        if sc.get("runner_class"):
            argv += ["--tests", sc["runner_class"]]
        mode = sc.get("filter_mode") or "features"
        selected = sc.get("selected") or []
        if mode == "features":
            feats = ",".join(i["id"] for i in selected if i.get("id"))
            if feats:
                argv.append("-Dcucumber.features=" + feats)
        elif mode == "tags":
            tags = tag_expression(selected)
            if tags:
                argv.append("-Dcucumber.filter.tags=" + tags)
        # Relative to the test JVM's working directory, which Gradle sets to
        # the project directory.
        argv.append("-Dcucumber.plugin=message:" + CUCUMBER_NDJSON_REL)
    argv.append("--console=plain")
    return argv


CUCUMBER_NDJSON_REL = "build/tdd/cucumber.ndjson"


def run_process(argv, cwd, timeout):
    start = time.time()
    try:
        proc = subprocess.run(
            argv, cwd=str(cwd), timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace")
        return proc.returncode, proc.stdout or "", time.time() - start, False
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return -1, out, time.time() - start, True
    except OSError as exc:
        raise TddError("could not start gradle (%s): %s" % (argv[0], exc))


def collect_fresh_xml(ctx, rdir, task, since):
    """Only reports written by *this* run count.  DEVPLAN section 9.2."""
    files = [p for p in sorted(rdir.glob("TEST-*.xml"))] if rdir.is_dir() else []
    fresh = [p for p in files if _mtime(p) >= since - 1]
    if fresh:
        return fresh
    # Multi-module builds may put results somewhere other than the module we
    # guessed; look wider before giving up.
    wider = []
    for path in ctx.repo.glob("**/build/test-results/%s/TEST-*.xml" % task):
        if _mtime(path) >= since - 1:
            wider.append(path)
    return sorted(wider)


def _mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def first_line(text):
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def parse_junit_xml(files):
    """JUnit 4, JUnit 5 and Platform Suite all emit this shape."""
    report = {"total": 0, "pass": 0, "fail": 0, "skipped": 0, "failed": [], "cases": []}
    for path in files:
        try:
            root = ET.parse(str(path)).getroot()
        except Exception:
            continue  # a half-written report is not worth crashing over
        suites = [root] if root.tag == "testsuite" else root.findall(".//testsuite")
        for suite in suites:
            for case in suite.findall("testcase"):
                cls = case.get("classname") or suite.get("name") or ""
                name = case.get("name") or "?"
                ident = "%s#%s" % (cls, name) if cls else name
                report["total"] += 1
                bad = case.find("failure")
                if bad is None:
                    bad = case.find("error")
                if bad is not None:
                    report["fail"] += 1
                    message = bad.get("message") or bad.text or ""
                    report["failed"].append(
                        {"id": ident, "name": name, "message": first_line(message)})
                    report["cases"].append({"id": ident, "name": name, "status": "fail"})
                elif case.find("skipped") is not None:
                    report["skipped"] += 1
                    report["cases"].append({"id": ident, "name": name, "status": "skipped"})
                else:
                    report["pass"] += 1
                    report["cases"].append({"id": ident, "name": name, "status": "pass"})
    return report


def _gherkin_lines(doc, out):
    """astNodeId -> source line, including Scenario Outline example rows."""
    def walk(children):
        for child in children or []:
            scenario = child.get("scenario")
            if scenario:
                out[scenario.get("id")] = (scenario.get("location") or {}).get("line")
                for example in scenario.get("examples") or []:
                    for row in example.get("tableBody") or []:
                        out[row.get("id")] = (row.get("location") or {}).get("line")
            rule = child.get("rule")
            if rule:
                walk(rule.get("children"))
    walk((doc.get("feature") or {}).get("children"))


def parse_cucumber_ndjson(path):
    """Cucumber Messages -> [{id: 'uri:line', name, status}] or None."""
    if not path or not path.is_file():
        return None
    pickles, lines, cases, started, status = {}, {}, {}, {}, {}
    try:
        with open(str(path), "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if "gherkinDocument" in msg:
                    _gherkin_lines(msg["gherkinDocument"], lines)
                elif "pickle" in msg:
                    pick = msg["pickle"]
                    pickles[pick.get("id")] = {
                        "uri": pick.get("uri"), "name": pick.get("name"),
                        "ast": pick.get("astNodeIds") or []}
                elif "testCase" in msg:
                    cases[msg["testCase"].get("id")] = msg["testCase"].get("pickleId")
                elif "testCaseStarted" in msg:
                    started[msg["testCaseStarted"].get("id")] = \
                        msg["testCaseStarted"].get("testCaseId")
                elif "testStepFinished" in msg:
                    step = msg["testStepFinished"]
                    sid = step.get("testCaseStartedId")
                    state = ((step.get("testStepResult") or {}).get("status") or "").upper()
                    if sid and state in CUCUMBER_STATUS_ORDER:
                        cur = status.get(sid)
                        if cur is None or (CUCUMBER_STATUS_ORDER.index(state)
                                           > CUCUMBER_STATUS_ORDER.index(cur)):
                            status[sid] = state
    except OSError:
        return None

    results = []
    for sid, case_id in started.items():
        pick = pickles.get(cases.get(case_id))
        if not pick:
            continue
        line = None
        for ast in reversed(pick.get("ast") or []):   # outline row beats scenario
            if lines.get(ast):
                line = lines[ast]
                break
        ident = "%s:%s" % (pick.get("uri"), line) if line else pick.get("uri")
        results.append({"id": ident, "uri": pick.get("uri"), "line": line,
                        "name": pick.get("name"),
                        "status": status.get(sid, "UNKNOWN")})
    return results or None


def normalise_feature_uri(uri, known):
    """Cucumber reports a feature by the path it was *loaded* by.

    With `@SelectClasspathResource("features")` that is
    `classpath:features/order.feature`, while discovery works in repo-relative
    paths like `app/src/test/resources/features/order.feature`. Left alone the
    two never match, and every scenario id would be wrong.
    """
    if not uri:
        return uri
    raw = uri.replace("\\", "/")
    for prefix in ("classpath:", "file://", "file:"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
    raw = raw.lstrip("/")
    if raw in known:
        return raw
    # The overlap can run either way: a classpath uri is a suffix of the
    # repo-relative path, while an absolute file uri has it as a suffix.
    matches = [path for path in known
               if raw.endswith("/" + path) or path.endswith("/" + raw)]
    if matches:
        return max(matches, key=len)
    return raw


def merge_scenario_results(state, report, ndjson_path):
    """Prefer Cucumber's own messages; fall back to XML test-case names."""
    warnings = []
    sc = gate(state, "scenario")
    selected = sc.get("selected") or []
    sel_ids = set(i.get("id") for i in selected if i.get("id"))
    by_name = dict((i.get("name"), i.get("id")) for i in selected if i.get("name"))

    known = set()
    for item in list(selected) + list(sc.get("candidates") or []):
        ident = item.get("path") or item.get("id") or ""
        if ident:
            known.add(ident.rpartition(":")[0] if ":" in ident else ident)

    nd = parse_cucumber_ndjson(ndjson_path)
    if nd:
        for entry in nd:
            path = normalise_feature_uri(entry.get("uri"), known)
            entry["id"] = ("%s:%s" % (path, entry["line"])) if entry.get("line")                 else path
    if not nd:
        warnings.append(
            "no cucumber message output - scenario ids came from JUnit XML names; "
            "run `%s doctor` to check cucumber system-property forwarding" % CMD)
        out = dict(report)
        out["failed"] = [{"id": by_name.get(f.get("name")) or f.get("id"),
                          "name": f.get("name"), "message": f.get("message")}
                         for f in report.get("failed") or []]
        return out, warnings

    ran = len(nd)
    if sel_ids:
        subset = [r for r in nd if r.get("id") in sel_ids]
        if subset and len(subset) < ran:
            warnings.append(
                "the cucumber task ran %d scenarios but %d are selected - the "
                "-Dcucumber filter is not reaching the test JVM; results were "
                "filtered afterwards (see `%s doctor`)" % (ran, len(subset), CMD))
            nd = subset
    failed = [{"id": r["id"], "name": r.get("name"),
               "message": "%s: %s" % (r["status"].lower(), r.get("name") or "")}
              for r in nd if r["status"] not in ("PASSED", "SKIPPED")]
    return {"total": len(nd),
            "pass": sum(1 for r in nd if r["status"] == "PASSED"),
            "fail": len(failed),
            "skipped": sum(1 for r in nd if r["status"] == "SKIPPED"),
            "failed": failed, "cases": []}, warnings


def looks_like_build_failure(output):
    if NO_TESTS_MARKER in output:
        return False
    if re.search(r"(?m)^.*\berror:", output):
        return True
    return any(marker in output for marker in BUILD_ERROR_MARKERS)


def classify_outcome(timed_out, exit_code, xml_files, report, output):
    """The distinction that matters most: a build failure is not a test failure."""
    if timed_out:
        return "timeout"
    if not xml_files or report["total"] == 0:
        if exit_code != 0 and looks_like_build_failure(output):
            return "build_failed"
        return "no_results"
    return "tests_failed" if report["fail"] else "passed"


def extract_build_error(output, max_lines=MAX_BUILD_ERROR_LINES):
    lines = output.splitlines()
    picked = []
    for line in lines:
        if re.search(r"\berror:", line) or line.strip().startswith("e: "):
            picked.append(line.rstrip())
    for i, line in enumerate(lines):
        if line.startswith("* What went wrong:"):
            for follow in lines[i:i + 25]:
                if follow.startswith("* Try:"):
                    break
                picked.append(follow.rstrip())
            break
    if not picked:
        picked = [line.rstrip() for line in lines[-max_lines:]]
    seen, out = set(), []
    for line in picked:
        if line and line not in seen:
            seen.add(line)
            out.append(line)
    return out[:max_lines]


def compute_green(state, kind, report, outcome, baseline=False):
    """Green is measured against the baseline, not against zero: tests that
    were already red before the refactor started are not this step's fault."""
    if outcome not in ("passed", "tests_failed"):
        return False, [], []
    current = [f["id"] for f in report.get("failed") or []]
    if baseline:
        return True, [], current        # whatever is red now defines the baseline
    known = set((gate(state, kind).get("baseline") or {}).get("failed_ids") or [])
    regressions = [i for i in current if i not in known]
    pre_existing = [i for i in current if i in known]
    return (not regressions), regressions, pre_existing


def run_signature(outcome, report, build_error):
    if outcome == "tests_failed":
        return failure_signature(report.get("failed") or [])
    if outcome == "build_failed":
        blob = "\n".join(build_error[:3])
        return "sha1:" + hashlib.sha1(blob.encode("utf-8")).hexdigest()
    return None


def update_selected_status(state, kind, report):
    """A selected unit test is a class; its cases are class#method."""
    failed_ids = set(f["id"] for f in report.get("failed") or [])
    out = []
    for item in gate(state, kind).get("selected") or []:
        ident = item.get("id") or ""
        entry = dict(item)
        if ident in failed_ids or any(f.startswith(ident + "#") for f in failed_ids):
            entry["status"] = "fail"
        elif report.get("total"):
            entry["status"] = "pass"
        else:
            entry["status"] = "unknown"
        out.append(entry)
    return out


def write_run_log(ctx, kind, argv, exit_code, duration, output):
    ctx.logs.mkdir(parents=True, exist_ok=True)
    name = "%s-%s.log" % (now_iso().replace(":", "-"), kind)
    path = ctx.logs / name
    header = [
        "# tdd run %s" % kind,
        "# cmd      : %s" % " ".join(argv),
        "# cwd      : %s" % ctx.repo,
        "# exit     : %s" % exit_code,
        "# duration : %.1fs" % duration,
        "# " + "-" * 70,
        "",
    ]
    atomic_write(path, "\n".join(header) + output)
    prune_logs(ctx)
    return "logs/" + name


def prune_logs(ctx, keep=LOGS_KEPT):
    try:
        logs = sorted(ctx.logs.glob("*.log"), key=_mtime, reverse=True)
    except OSError:
        return
    for stale in logs[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


def short_cmd(cmd):
    """Keep the informative tail of a long gradle line, drop the wrapper path."""
    parts = (cmd or "").split()
    if parts:
        parts[0] = pathlib.PurePath(parts[0]).name
    line = " ".join(parts)
    return truncate(line, 100)


def render_run_summary(kind, run, warnings):
    """Hard line budget - this is the whole point of running the build here.
    Full output is on disk; the model gets counts and named failures."""
    if run["outcome"] == "build_failed":
        lines = ["RUN   %s | %s | %.1fs" % (kind, short_cmd(run["cmd"]),
                                            run.get("duration_s") or 0),
                 "RES   BUILD FAILED - the tests never ran. Fix this first."]
        lines += ["      " + l for l in (run.get("build_error") or [])[:MAX_BUILD_ERROR_LINES]]
        lines.append("LOG   .claude/tdd/%s" % run.get("log"))
        return "\n".join(lines)

    head = ["RUN   %s | %s | %.1fs" % (kind, short_cmd(run["cmd"]),
                                       run.get("duration_s") or 0)]
    if run["outcome"] == "no_results":
        head.append("RES   NO RESULTS - the task wrote no fresh reports. "
                    "This is NOT a pass.")
    elif run["outcome"] == "timeout":
        head.append("RES   TIMED OUT after %.0fs - nothing verified."
                    % (run.get("duration_s") or 0))
    else:
        head.append("RES   %s | %d/%d passed | %d failing | %d skipped | gate %s"
                    % (run["outcome"], run.get("pass", 0), run.get("total", 0),
                       run.get("fail", 0), run.get("skipped", 0),
                       "GREEN" if run.get("green") else "RED"))

    tail = []
    pre = run.get("pre_existing") or []
    if pre:
        tail.append("NOTE  %d pre-existing baseline failure(s) ignored: %s"
                    % (len(pre), join_ids(pre, 2)))
    for warning in warnings or []:
        tail += labelled("WARN  ", warning)
    tail.append("LOG   .claude/tdd/%s" % run.get("log"))

    regressions = set(run.get("regressions") or [])
    failures = run.get("failed") or []
    # Regressions first: those are the ones this increment caused.
    failures = ([f for f in failures if f.get("id") in regressions]
                + [f for f in failures if f.get("id") not in regressions])

    room = max(1, SUMMARY_LINE_BUDGET - len(head) - len(tail) - 1)
    shown = min(len(failures), MAX_FAILURES_SHOWN, room)
    body = ["FAIL  %s | %s" % (f.get("id"), truncate(f.get("message") or "",
                                                     MAX_MESSAGE_CHARS))
            for f in failures[:shown]]
    if len(failures) > shown:
        body.append("MORE  %d further failure(s) not shown - grep the log"
                    % (len(failures) - shown))
    return "\n".join(head + body + tail)


def do_run(ctx, kind, timeout, baseline=False):
    state = ctx.state
    target = state.get("target") or {}
    gradle_project = target.get("gradle_project") or ""
    task = "test" if kind == "unit" else (gate(state, "scenario").get("task") or "test")
    module = module_dir(ctx, gradle_project)
    rdir = results_dir(ctx, gradle_project, task)
    ndjson = module / CUCUMBER_NDJSON_REL

    wipe_results(rdir)
    if kind == "scenario":
        ndjson.parent.mkdir(parents=True, exist_ok=True)
        try:
            ndjson.unlink()
        except OSError:
            pass

    argv = build_argv(ctx, state, kind, task, gradle_project, module)
    # Taken as close to the launch as possible: anything edited after this
    # point is, correctly, not covered by the result.
    watched_at_start = gather_facts(ctx).get("mtimes") or {}
    start_iso, start_epoch = stamp()
    exit_code, output, duration, timed_out = run_process(argv, ctx.repo, timeout)

    xml = collect_fresh_xml(ctx, rdir, task, start_epoch)
    report = parse_junit_xml(xml)
    warnings = []
    if kind == "scenario" and (xml or ndjson.is_file()):
        report, warnings = merge_scenario_results(state, report, ndjson)

    outcome = classify_outcome(timed_out, exit_code, xml, report, output)
    green, regressions, pre_existing = compute_green(state, kind, report, outcome, baseline)
    build_error = extract_build_error(output) if outcome == "build_failed" else []
    log_rel = write_run_log(ctx, kind, argv, exit_code, duration, output)
    signature = run_signature(outcome, report, build_error)

    last_run = {
        # The run's START, not its finish: an edit made while gradle was
        # working must read as newer than the result it invalidates.
        "ran_at": start_iso, "ran_at_epoch": start_epoch,
        "watched": watched_at_start,
        "cmd": " ".join(argv), "log": log_rel, "outcome": outcome,
        "duration_s": round(duration, 1), "exit_code": exit_code,
        "total": report["total"], "pass": report["pass"],
        "fail": report["fail"], "skipped": report["skipped"],
        "failed": report["failed"][:MAX_FAILURES_STORED],
        "signature": signature, "green": green,
        "regressions": regressions[:MAX_FAILURES_STORED],
        "pre_existing": pre_existing[:MAX_FAILURES_STORED],
        "build_error": build_error, "warnings": warnings,
    }

    attempts = dict(state.get("attempts") or {})
    key = "verify_unit" if kind == "unit" else "verify_scenario"
    if not baseline:
        attempts[key] = (attempts.get(key) or 0) + 1
    if green:
        attempts["same_failure_streak"] = 0
        attempts["last_signature"] = None
    elif signature:
        attempts["same_failure_streak"] = (
            (attempts.get("same_failure_streak") or 0) + 1
            if signature == attempts.get("last_signature") else 1)
        attempts["last_signature"] = signature

    patch = {
        "%s.last_run" % kind: last_run,
        "%s.green" % kind: green,
        "%s.selected" % kind: update_selected_status(state, kind, report),
        "attempts": attempts,
        "freshness.dirty": False,
        "freshness.dirty_files": [],
        "freshness.last_verified_at": start_iso,
    }
    if baseline:
        patch["%s.baseline" % kind] = {
            "ran_at": start_iso, "total": report["total"], "pass": report["pass"],
            "fail": report["fail"],
            "failed_ids": [f["id"] for f in report["failed"]][:MAX_FAILURES_STORED],
        }

    summary_line = "%s%s %s -> %s %d/%d" % (
        "baseline " if baseline else "run ", kind, "", outcome,
        report["pass"], report["total"])
    mutate(ctx, "baseline" if baseline else "run", patch,
           summary=" ".join(summary_line.split()),
           data={"summary": " ".join(summary_line.split()), "kind": kind,
                 "outcome": outcome, "signature": signature})
    return last_run, warnings


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def open_ctx(args):
    ctx = Ctx(find_repo_root(getattr(args, "repo_root", None)))
    load_state(ctx)
    return ctx


def corrupt_action():
    return NA("REPAIR",
              "session.json is unreadable. Rebuild it from the append-only journal "
              "- no progress is lost.", CMD + " repair", "state file is corrupt", 0)


def cmd_init(args):
    ctx = open_ctx(args)
    ctx.ensure_dirs()
    rel = ctx.rel(args.target)
    path = ctx.repo / rel
    if not path.is_file():
        raise TddError("no such file: %s" % rel)
    if path.suffix != ".java":
        raise TddError("target must be a .java file, got %s" % rel)

    current = ctx.state.get("target") or {}
    if current and current.get("path") != rel and not args.force:
        raise TddError(
            "target %s is still active (phase %s). Finish it with `%s done`, or "
            "pass --force to switch." % (current.get("path"), ctx.state.get("phase"), CMD),
            EXIT_REFUSED)

    source = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^\s*package\s+([A-Za-z_][\w.]*)\s*;", source, re.M)
    package = match.group(1) if match else None
    simple = path.stem
    fqn = "%s.%s" % (package, simple) if package else simple

    with Lock(ctx, force=args.force_unlock):
        fresh = default_state()
        fresh["repo_root"] = ctx.repo.as_posix()
        fresh["notes"] = ctx.state.get("notes") or []
        ctx.state = fresh
        iso, epoch = stamp()
        patch = {
            "repo_root": ctx.repo.as_posix(),
            "target": {
                "path": rel,
                "class_fqn": fqn,
                "simple_name": simple,
                "gradle_project": gradle_project_for(ctx, path),
                "goal": args.goal,
                "started_at": iso,
                "started_at_epoch": epoch,
            },
        }
        mutate(ctx, "init", patch, summary="init target %s" % simple,
               data={"summary": "init target %s" % simple})
    print(render_next(ctx.state, gather_facts(ctx)))
    return EXIT_OK


def gradle_project_for(ctx, target_path):
    """Nearest ancestor build script -> Gradle project path (':app')."""
    repo = ctx.repo.resolve()
    cur = target_path.resolve().parent
    while True:
        if (cur / "build.gradle").exists() or (cur / "build.gradle.kts").exists():
            if cur == repo:
                return ""
            return ":" + cur.relative_to(repo).as_posix().replace("/", ":")
        if cur == repo or cur.parent == cur:
            return ""
        cur = cur.parent


def cmd_next(args):
    ctx = open_ctx(args)
    if ctx.corrupt:
        action = corrupt_action()
        if args.json:
            print(json.dumps(action, indent=2))
        else:
            print("TDD   state file is corrupt")
            print("\n".join(labelled("NEXT  ", action["instruction"])))
            print("CMD   " + action["command"])
        return EXIT_OK
    facts = gather_facts(ctx)
    action = resolve_next(ctx.state, facts)
    ctx.state["next_action"] = {k: action[k] for k in ("code", "instruction", "command", "why")}
    ctx.state["phase"] = PHASE_BY_CODE.get(action["code"], ctx.state.get("phase"))
    if args.json:
        print(json.dumps({"action": action, "phase": ctx.state["phase"],
                          "target": ctx.state.get("target"),
                          "unit_green": gate_ok(ctx.state, "unit", facts),
                          "scenario_green": gate_ok(ctx.state, "scenario", facts)}, indent=2))
    else:
        print(render_next(ctx.state, facts))
    return EXIT_BLOCKED if action["code"] == "UNBLOCK" else EXIT_OK


def cmd_status(args):
    ctx = open_ctx(args)
    if ctx.corrupt:
        print("TDD   state file is corrupt - run `%s repair`" % CMD)
        return EXIT_OK
    facts = gather_facts(ctx)
    refresh_derived(ctx, facts)
    if args.json:
        print(json.dumps(ctx.state, indent=2))
    else:
        print(render_status(ctx.state, facts))
    return EXIT_OK


def cmd_resume(args):
    ctx = open_ctx(args)
    if ctx.corrupt:
        print("=== TDD LOOP ===")
        print("state file is corrupt - run `%s repair` to rebuild it from the journal" % CMD)
        return EXIT_OK
    if not ctx.state.get("target"):
        if args.quiet_if_idle:
            return EXIT_OK
        print("=== TDD LOOP ===")
        print("No active target. Start one with: %s init --target <File.java>" % CMD)
        return EXIT_OK
    facts = gather_facts(ctx)
    refresh_derived(ctx, facts)
    print(render_resume(ctx.state, facts, journal_tail_lines(ctx)))
    return EXIT_OK


def cmd_note(args):
    ctx = open_ctx(args)
    text = " ".join(args.text).strip()
    if not text:
        raise TddError("note text is empty")
    with Lock(ctx, force=args.force_unlock):
        notes = list(ctx.state.get("notes") or [])
        notes.insert(0, {"at": now_iso(), "text": text})
        mutate(ctx, "note", {"notes": notes[:NOTES_MAX]},
               summary="note: " + truncate(text, 60),
               data={"summary": "note: " + truncate(text, 60)})
    print("noted.")
    return EXIT_OK


def cmd_block(args):
    ctx = open_ctx(args)
    reason = " ".join(args.reason).strip()
    if not reason:
        raise TddError("give a reason: %s block \"<why you are stuck>\"" % CMD)
    with Lock(ctx, force=args.force_unlock):
        mutate(ctx, "block", {"blocked": {"at": now_iso(), "reason": reason}},
               summary="BLOCKED: " + truncate(reason, 60),
               data={"summary": "BLOCKED: " + truncate(reason, 60)})
    print("blocked. The loop will refuse to advance until `%s unblock`." % CMD)
    return EXIT_BLOCKED


def cmd_unblock(args):
    ctx = open_ctx(args)
    if not ctx.state.get("blocked"):
        print("not blocked.")
        return EXIT_OK
    with Lock(ctx, force=args.force_unlock):
        mutate(ctx, "unblock", {"blocked": None}, summary="unblocked",
               data={"summary": "unblocked"})
    print(render_next(ctx.state, gather_facts(ctx)))
    return EXIT_OK


def cmd_checkpoint(args):
    """Idempotent, journal-free: safe to wire to the Stop hook."""
    ctx = open_ctx(args)
    if ctx.corrupt:
        return EXIT_OK
    refresh_derived(ctx)
    write_status_md(ctx)
    if ctx.existed:
        write_state(ctx)
    if not args.quiet:
        print("STATUS.md refreshed.")
    return EXIT_OK


def cmd_repair(args):
    """Replay the journal.  Because every mutation is a patch, replaying is
    literally the same sequence of operations - there is no reducer to drift."""
    ctx = open_ctx(args)
    events = read_journal(ctx)
    if not events:
        raise TddError("journal is empty or missing - nothing to repair from")
    state = default_state()
    applied = 0
    for event in events:
        if event.get("event") == "snapshot" and isinstance(event.get("state"), dict):
            state = clone(event["state"])
            applied = 0
            continue
        patch = event.get("patch")
        if isinstance(patch, dict):
            apply_patch(state, patch)
            applied += 1
    ctx.state = state
    ctx.ensure_dirs()
    refresh_derived(ctx)
    write_state(ctx)
    write_status_md(ctx)
    print("repaired: replayed %d events from %s" % (applied, ctx.journal.name))
    print()
    print(render_next(ctx.state, gather_facts(ctx)))
    return EXIT_OK


# --------------------------------------------------------------------------
# doctor: environment checks  (DEVPLAN section 12)
# --------------------------------------------------------------------------

HOOK_EVENTS = ("SessionStart", "PreCompact", "Stop", "PostToolUse")

GITIGNORE_LINES = (".claude/tdd/state/", ".claude/tdd/logs/",
                   ".claude/tdd/snapshots/", ".claude/tdd/archive/",
                   ".claude/tdd/STATUS.md")

FORWARDING_GROOVY = ("tasks.named('test') { systemProperties "
                     "System.properties.findAll { it.key.toString()"
                     ".startsWith('cucumber.') } }")
FORWARDING_KOTLIN = ('tasks.named<Test>("test") { systemProperties('
                     'System.getProperties().filterKeys { '
                     'it.toString().startsWith("cucumber.") }) }')

TASK_NAME_RES = (
    re.compile(r"tasks\.register\(\s*['\"](\w+)['\"]"),
    re.compile(r"tasks\.named<?\w*>?\(\s*['\"](\w+)['\"]"),
    re.compile(r"(?m)^\s*task\s+(\w+)\s*\(\s*type\s*:\s*Test"),
    re.compile(r"(?m)^\s*(\w*[Tt]est)\s*\{"),
)


def build_scripts(ctx, module=None):
    out = []
    for base in dict.fromkeys([module, ctx.repo]):
        if base is None:
            continue
        for name in ("build.gradle", "build.gradle.kts"):
            path = base / name
            if path.is_file():
                out.append(path)
    if out:
        return out
    # Multi-module repo with no target set yet: the root often holds only
    # settings.gradle, so look for the subproject scripts before giving up.
    for path in sorted(ctx.repo.rglob("build.gradle*")):
        rel = path.relative_to(ctx.repo)
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        out.append(path)
        if len(out) >= 5:
            break
    return out


def test_task_names(scripts):
    names = []
    for path in scripts:
        body = read_text(path)
        for pattern in TASK_NAME_RES:
            for name in pattern.findall(body):
                if "test" in name.lower() and name not in names:
                    names.append(name)
    return names


def cucumber_forwarding_present(scripts):
    """Static check: does any build script hand system properties to the tests?

    A real probe would have to run the suite, which is far too slow for a
    diagnostic - so this reads the build files and says what it did.
    """
    for path in scripts:
        for line in read_text(path).splitlines():
            if "systemPropert" not in line:
                continue
            if ("cucumber" in line or "System.properties" in line
                    or "System.getProperties" in line):
                return True, path
    return False, None


def load_settings(ctx):
    path = ctx.repo / ".claude" / "settings.json"
    if not path.is_file():
        return None, path
    try:
        return json.loads(path.read_text(encoding="utf-8")), path
    except (ValueError, OSError):
        return {}, path


def installed_hook_events(settings):
    found = set()
    for event, groups in ((settings or {}).get("hooks") or {}).items():
        for group in groups or []:
            for hook in group.get("hooks") or []:
                if "tddstate.py" in (hook.get("command") or ""):
                    found.add(event)
    return found


def cmd_doctor(args):
    ctx = open_ctx(args)
    state = ctx.state
    target = state.get("target") or {}
    module = module_dir(ctx, target.get("gradle_project") or "")
    scripts = build_scripts(ctx, module)
    checks = []

    def add(level, label, detail=None):
        checks.append((level, label, detail))

    if sys.version_info >= (3, 8):
        add("OK", "python %d.%d.%d" % sys.version_info[:3])
    else:
        add("FAIL", "python %d.%d is too old" % sys.version_info[:2],
            "install Python 3.8 or newer")

    try:
        wrapper = gradle_wrapper(ctx, module)
        add("OK", "gradle: %s" % " ".join(pathlib.PurePath(p).name for p in wrapper))
    except TddError as exc:
        add("FAIL", "gradle wrapper missing", str(exc))

    if scripts:
        add("OK", "build script: %s" % ", ".join(ctx.rel(p) for p in scripts))
        tasks = test_task_names(scripts)
        if tasks:
            configured = gate(state, "scenario").get("task") or "test"
            extra = [t for t in tasks if t != "test"]
            if extra and configured == "test":
                add("WARN", "test tasks found: %s" % ", ".join(tasks),
                    "scenarios run under '%s'; if they live in another task set it "
                    "with `%s select scenario --task <name> <ids...>`"
                    % (configured, CMD))
            else:
                add("OK", "test tasks: %s (scenarios use '%s')"
                    % (", ".join(tasks), configured))
    else:
        add("WARN", "no build.gradle found near the target",
            "run doctor from the repository root, or set a target first")

    roots = test_source_roots(ctx)
    if roots:
        add("OK", "test sources: %s" % join_ids([ctx.rel(r) for r in roots], 3))
    else:
        add("WARN", "no src/test/java directory found",
            "unit discovery has nothing to search")

    features = feature_files(ctx)
    if features:
        add("OK", "feature files: %d" % len(features))
        forwarded, where = cucumber_forwarding_present(scripts)
        if forwarded:
            add("OK", "cucumber system properties forwarded in %s" % ctx.rel(where))
        else:
            add("WARN", "cucumber system properties are NOT forwarded to the test JVM",
                "-Dcucumber.* will be ignored and the whole suite will run. Add to "
                "your build script:  %s"
                % (FORWARDING_KOTLIN if any(str(p).endswith(".kts") for p in scripts)
                   else FORWARDING_GROOVY))
        observed = (gate(state, "scenario").get("last_run") or {}).get("warnings") or []
        if any("not reaching the test JVM" in w for w in observed):
            add("WARN", "a previous run confirmed the filter was ignored",
                "this is observed evidence, not a guess - add the snippet above")
    else:
        add("OK", "no .feature files; the scenario gate will not be used")

    try:
        ctx.ensure_dirs()
        probe = ctx.state_dir / ".doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add("OK", "state directory writable: .claude/tdd/state")
    except OSError as exc:
        add("FAIL", "cannot write to .claude/tdd/state", str(exc))

    settings, settings_path = load_settings(ctx)
    if settings is None:
        add("WARN", "no .claude/settings.json - hooks are not installed",
            "run install/install.py to add them; the loop still works without "
            "hooks, but a new session will not be told where it left off")
    else:
        installed = installed_hook_events(settings)
        missing = [e for e in HOOK_EVENTS if e not in installed]
        if not missing:
            add("OK", "hooks installed: %s" % ", ".join(sorted(installed)))
        else:
            add("WARN", "hooks missing: %s" % ", ".join(missing),
                "re-run install/install.py; check event names with /hooks if your "
                "Claude Code version differs")

    gitignore = ctx.repo / ".gitignore"
    if gitignore.is_file():
        body = read_text(gitignore)
        absent = [line for line in GITIGNORE_LINES if line not in body]
        if absent:
            add("WARN", "runtime state is not gitignored",
                "add: %s" % " ".join(absent))
        else:
            add("OK", "runtime state is gitignored")
    else:
        add("WARN", "no .gitignore", "add: %s" % " ".join(GITIGNORE_LINES))

    if shutil.which("git"):
        head = git_head(ctx)
        add("OK", "git available%s" % (" (HEAD %s)" % head if head else
                                       " but this is not a repository"))
    else:
        add("WARN", "git not on PATH",
            "increments will record no base commit; revert-step still works from "
            "the snapshots")

    print("DOCTOR  %s" % ctx.repo)
    for level, label, detail in checks:
        print("%-5s %s" % (level, label))
        if detail:
            for line in labelled("      ", detail, 92):
                print(line)
    fails = sum(1 for level, _l, _d in checks if level == "FAIL")
    warns = sum(1 for level, _l, _d in checks if level == "WARN")
    print("%d ok, %d warning(s), %d failure(s)"
          % (len(checks) - fails - warns, warns, fails))
    return EXIT_ERROR if fails else EXIT_OK


# --------------------------------------------------------------------------
# increments: snapshots, git, archive  (DEVPLAN sections 8 and 13)
# --------------------------------------------------------------------------

SNAPSHOTS_KEPT = 10


def git_head(ctx):
    """Short HEAD, or None. Git is optional - only `base_commit` depends on it."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(ctx.repo), timeout=10,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding="utf-8", errors="replace")
        if proc.returncode == 0:
            return (proc.stdout or "").strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def take_snapshot(ctx, step_id, rel_paths):
    """Copy the files an increment may touch, so `revert-step` can undo it."""
    base = ctx.snapshots / step_id
    shutil.rmtree(str(base), ignore_errors=True)
    saved = []
    for rel in rel_paths:
        source = ctx.repo / rel
        if not source.is_file():
            continue
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(source), str(target))
        saved.append(rel)
    prune_snapshots(ctx)
    return saved


def restore_snapshot(ctx, step_id, rel_paths):
    base = ctx.snapshots / step_id
    restored, missing = [], []
    for rel in rel_paths:
        source = base / rel
        if not source.is_file():
            missing.append(rel)
            continue
        target = ctx.repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        # copyfile, not copy2: the file really did just change on disk, so it
        # should carry a current mtime rather than the snapshot's.
        shutil.copyfile(str(source), str(target))
        restored.append(rel)
    return restored, missing


def prune_snapshots(ctx, keep=SNAPSHOTS_KEPT):
    try:
        dirs = sorted((d for d in ctx.snapshots.iterdir() if d.is_dir()),
                      key=_mtime, reverse=True)
    except OSError:
        return
    for stale in dirs[keep:]:
        shutil.rmtree(str(stale), ignore_errors=True)


def gate_blockers(state, facts):
    """One short reason per gate that is not green-and-fresh."""
    blockers = []
    for kind in ("unit", "scenario"):
        if kind == "scenario" and not scenario_applicable(state):
            continue
        why = staleness(state, kind, facts)
        if why:
            blockers.append("%s result is %s" % (kind, why))
        elif not gate(state, kind).get("green"):
            run = gate(state, kind).get("last_run") or {}
            failing = [f.get("id") for f in run.get("failed") or []]
            blockers.append("%s gate is red (%s)" % (kind, join_ids(failing, 2)))
    return blockers


def require_target(ctx):
    if ctx.corrupt:
        raise TddError("state is unreadable - run `%s repair` first" % CMD)
    if not ctx.state.get("target"):
        raise TddError("no active target - run `%s init --target <File.java>`" % CMD,
                       EXIT_REFUSED)


def cmd_discover(args):
    ctx = open_ctx(args)
    require_target(ctx)
    kind = args.kind
    if kind == "scenario" and not scenario_applicable(ctx.state):
        raise TddError("this target is marked as having no scenario coverage",
                       EXIT_REFUSED)

    if kind == "unit":
        candidates, notes, patch = discover_unit(ctx, ctx.state), [], {}
        patch["unit.candidates"] = candidates
    else:
        candidates, runner, glue_paths, notes = discover_scenario(ctx, ctx.state)
        patch = {"scenario.candidates": candidates, "scenario.glue": glue_paths}
        if runner and not gate(ctx.state, "scenario").get("runner_class"):
            patch["scenario.runner_class"] = runner
            notes.append("cucumber runner class detected: %s" % runner)
        if glue_paths:
            notes.append("watching %d glue file(s) for edits: %s"
                         % (len(glue_paths), join_ids(glue_paths, 3)))

    with Lock(ctx, force=args.force_unlock):
        mutate(ctx, "discover", patch,
               summary="discover %s -> %d candidate(s)" % (kind, len(candidates)),
               data={"summary": "discover %s -> %d candidate(s)"
                                % (kind, len(candidates))})
    print(render_candidates(kind, candidates, notes))
    print()
    print(render_next(ctx.state, gather_facts(ctx)))
    return EXIT_OK


def cmd_select(args):
    ctx = open_ctx(args)
    require_target(ctx)
    kind = args.kind
    candidates = gate(ctx.state, kind).get("candidates") or []
    by_id = dict((c["id"], c) for c in candidates)

    task_patch = {}
    if kind == "scenario" and getattr(args, "task", None):
        task_patch["scenario.task"] = args.task

    if kind == "scenario" and args.none:
        with Lock(ctx, force=args.force_unlock):
            mutate(ctx, "select",
                   {"scenario.not_applicable": True, "scenario.selected": [],
                    "scenario.baseline": None, "scenario.last_run": None,
                    "scenario.green": True},
                   summary="select scenario -> none apply",
                   data={"summary": "select scenario -> none apply"})
        print("recorded: this target has no scenario coverage; the scenario gate "
              "is skipped from now on.")
        print()
        print(render_next(ctx.state, gather_facts(ctx)))
        return EXIT_OK

    if args.all:
        chosen = [c["id"] for c in candidates if c["score"] >= 2]
        if not chosen:
            raise TddError("no candidate scored 2 or better - name the ids you want",
                           EXIT_REFUSED)
    else:
        chosen = list(args.ids or [])
    if not chosen:
        raise TddError("name at least one id, or pass --all%s"
                       % (" / --none" if kind == "scenario" else ""), EXIT_REFUSED)

    selected, unknown = [], []
    for ident in chosen:
        cand = by_id.get(ident)
        if kind == "unit":
            path = cand.get("path") if cand else None
            if not path:
                path, ident = find_test_path(ctx, ident)
            entry = {"id": ident, "path": path, "status": "unknown"}
        else:
            entry = {"id": ident, "name": (cand or {}).get("name"),
                     "tags": (cand or {}).get("tags") or [], "status": "unknown"}
        if not cand:
            unknown.append(ident)
        selected.append(entry)

    previous = [i.get("id") for i in gate(ctx.state, kind).get("selected") or []]
    changed = previous != [i["id"] for i in selected]
    patch = {"%s.selected" % kind: selected}
    if changed and previous:
        # A baseline and a verdict describe the old selection; keeping them
        # would silently grade the new set against the wrong reference.
        patch["%s.baseline" % kind] = None
        patch["%s.last_run" % kind] = None
        patch["%s.green" % kind] = False
    if kind == "scenario":
        patch["scenario.not_applicable"] = False
        patch.update(task_patch)

    with Lock(ctx, force=args.force_unlock):
        mutate(ctx, "select", patch,
               summary="select %s -> %d" % (kind, len(selected)),
               data={"summary": "select %s -> %d" % (kind, len(selected)),
                     "unknown": unknown})

    print("selected %d %s test(s): %s"
          % (len(selected), kind, join_ids([i["id"] for i in selected], 6)))
    if unknown:
        print("NOTE  not among the discovered candidates (kept anyway): %s"
              % join_ids(unknown, 4))
    if changed and previous:
        print("NOTE  the selection changed, so the %s baseline and last result were "
              "cleared - they described a different set of tests." % kind)
    missing = [i["id"] for i in selected if kind == "unit" and not i.get("path")]
    if missing:
        print("WARN  no source file found for: %s - edits to them cannot be "
              "detected" % join_ids(missing, 4))
    print()
    print(render_next(ctx.state, gather_facts(ctx)))
    return EXIT_OK


def cmd_run(args):
    ctx = open_ctx(args)
    require_target(ctx)
    kind = args.kind
    state = ctx.state

    if kind == "scenario":
        if not scenario_applicable(state):
            raise TddError("this target is marked as having no scenario coverage",
                           EXIT_REFUSED)
        facts = gather_facts(ctx)
        if not gate_ok(state, "unit", facts) and not args.force:
            why = staleness(state, "unit", facts) or "red"
            raise TddError(
                "ORDERING GATE: unit tests are not green (%s). Get them green first, "
                "or pass --force. Running scenarios over a red unit gate wastes a "
                "cycle and hides which change broke what." % why, EXIT_REFUSED)
    if not (gate(state, kind).get("selected") or []):
        raise TddError("nothing selected for %s - run `%s discover %s` first"
                       % (kind, CMD, kind), EXIT_REFUSED)

    with Lock(ctx, force=args.force_unlock):
        if args.force and kind == "scenario":
            append_journal(ctx, "override", {},
                           {"summary": "forced scenario run over a red unit gate"})
        last_run, warnings = do_run(ctx, kind, args.timeout, baseline=False)
    print(render_run_summary(kind, last_run, warnings))
    print()
    print(render_next(ctx.state, gather_facts(ctx)))
    return EXIT_OK


def cmd_baseline(args):
    ctx = open_ctx(args)
    require_target(ctx)
    kind = args.kind
    if ctx.state.get("step"):
        raise TddError(
            "increment %s is open - a baseline must be taken before any editing, "
            "otherwise your own changes get recorded as 'pre-existing'."
            % (ctx.state["step"].get("id")), EXIT_REFUSED)
    if kind == "scenario" and not scenario_applicable(ctx.state):
        raise TddError("this target is marked as having no scenario coverage",
                       EXIT_REFUSED)
    if not (gate(ctx.state, kind).get("selected") or []):
        raise TddError("nothing selected for %s - run `%s discover %s` first"
                       % (kind, CMD, kind), EXIT_REFUSED)

    with Lock(ctx, force=args.force_unlock):
        last_run, warnings = do_run(ctx, kind, args.timeout, baseline=True)
    print(render_run_summary(kind, last_run, warnings))
    if last_run["fail"]:
        print("NOTE  %d test(s) were already failing. They are now the baseline and "
              "will not be counted against your refactor." % last_run["fail"])
    print()
    print(render_next(ctx.state, gather_facts(ctx)))
    return EXIT_OK


def cmd_record(args):
    """Manual fallback for when the wrapper cannot drive the build.

    Journaled as manual so a hand-entered verdict is never mistaken for a
    parsed one.
    """
    ctx = open_ctx(args)
    require_target(ctx)
    kind, passed = args.kind, args.result == "pass"
    iso, epoch = stamp()
    note = " ".join(args.note or []) or "recorded by hand"
    last_run = {
        "ran_at": iso, "ran_at_epoch": epoch, "cmd": "(manual)", "log": None,
        "outcome": "passed" if passed else "tests_failed", "manual": True,
        "duration_s": 0, "exit_code": None, "total": 0, "pass": 0,
        "fail": 0 if passed else 1, "skipped": 0,
        "failed": [] if passed else [{"id": "(manual)", "message": note}],
        "signature": None, "green": passed, "regressions": [], "pre_existing": [],
        "build_error": [], "warnings": ["result entered by hand, not parsed"],
    }
    with Lock(ctx, force=args.force_unlock):
        mutate(ctx, "record",
               {"%s.last_run" % kind: last_run, "%s.green" % kind: passed,
                "freshness.dirty": False, "freshness.dirty_files": [],
                "freshness.last_verified_at": iso},
               summary="record %s -> %s (manual)" % (kind, args.result),
               data={"summary": "record %s -> %s (manual)" % (kind, args.result),
                     "manual": True, "note": note})
    print("recorded %s = %s (manual). Prefer `%s run %s` when the build can be driven."
          % (kind, args.result, CMD, kind))
    print()
    print(render_next(ctx.state, gather_facts(ctx)))
    return EXIT_OK


def cmd_step(args):
    return {"start": _step_start, "done": _step_done,
            "abandon": _step_abandon}[args.action](args)


def _step_start(args):
    ctx = open_ctx(args)
    require_target(ctx)
    state = ctx.state
    title = " ".join(args.title or []).strip()
    if not title:
        raise TddError('give the increment a title: %s step start "<what it does>"'
                       % CMD)
    if state.get("step"):
        open_step = state["step"]
        raise TddError(
            "increment %s (\"%s\") is still open - close it with `%s step done` or "
            "drop it with `%s step abandon`"
            % (open_step.get("id"), open_step.get("title"), CMD, CMD), EXIT_REFUSED)
    if not gate(state, "unit").get("baseline"):
        raise TddError("no unit baseline yet - run `%s baseline unit` before editing"
                       % CMD, EXIT_REFUSED)
    if scenario_applicable(state) and not gate(state, "scenario").get("baseline"):
        raise TddError("no scenario baseline yet - run `%s baseline scenario` before "
                       "editing" % CMD, EXIT_REFUSED)

    number = len(state.get("steps_done") or []) + 1
    step_id = "s%02d" % number
    files = [(state.get("target") or {}).get("path")]
    for extra in args.file or []:
        files.append(ctx.rel(extra))
    files = [f for f in dict.fromkeys(files) if f]
    saved = take_snapshot(ctx, step_id, files)

    iso, epoch = stamp()
    step = {"id": step_id, "n": number, "title": title,
            "started_at": iso, "started_at_epoch": epoch,
            "base_commit": git_head(ctx), "snapshot_dir": "snapshots/%s" % step_id,
            "files": files, "snapshotted": saved, "reverts": 0}
    with Lock(ctx, force=args.force_unlock):
        mutate(ctx, "step_start", {"step": step},
               summary='step start %s "%s"' % (step_id, truncate(title, 40)),
               data={"summary": 'step start %s "%s"' % (step_id, truncate(title, 40))})

    print("increment %s opened: %s" % (step_id, title))
    print("      snapshot: %d file(s) under .claude/tdd/snapshots/%s%s"
          % (len(saved), step_id,
             ", base commit %s" % step["base_commit"] if step["base_commit"] else ""))
    if len(saved) < len(files):
        print("WARN  not on disk, so not snapshotted: %s"
              % join_ids([f for f in files if f not in saved], 3))
    print()
    print(render_next(ctx.state, gather_facts(ctx)))
    return EXIT_OK


def _step_done(args):
    ctx = open_ctx(args)
    require_target(ctx)
    state = ctx.state
    step = state.get("step")
    if not step:
        raise TddError("no increment is open - `%s step start \"<title>\"` first" % CMD,
                       EXIT_REFUSED)

    facts = gather_facts(ctx)
    blockers = gate_blockers(state, facts)
    if blockers and not args.force:
        # The freshness gate: this is what stops "declared green on stale results".
        raise TddError("cannot close %s: %s. Re-run the tests first."
                       % (step.get("id"), "; ".join(blockers)), EXIT_REFUSED)

    iso, _epoch = stamp()
    attempts = state.get("attempts") or {}
    record = {"id": step.get("id"), "n": step.get("n"), "title": step.get("title"),
              "started_at": step.get("started_at"), "finished_at": iso,
              "base_commit": step.get("base_commit"),
              "unit_runs": attempts.get("verify_unit") or 0,
              "scenario_runs": attempts.get("verify_scenario") or 0,
              "reverts": step.get("reverts") or 0}
    if args.force and blockers:
        record["forced"] = blockers

    with Lock(ctx, force=args.force_unlock):
        if args.force and blockers:
            append_journal(ctx, "override", {},
                           {"summary": "forced step done over: %s" % "; ".join(blockers)})
        mutate(ctx, "step_done",
               {"step": None,
                "steps_done": list(state.get("steps_done") or []) + [record],
                "attempts": {"verify_unit": 0, "verify_scenario": 0,
                             "same_failure_streak": 0, "last_signature": None},
                "freshness.dirty": False, "freshness.dirty_files": []},
               summary='step done %s "%s"' % (record["id"], truncate(record["title"], 40)),
               data={"summary": 'step done %s "%s"'
                                % (record["id"], truncate(record["title"], 40))})

    print("increment %s closed: %s (%d unit run(s), %d scenario run(s))"
          % (record["id"], record["title"], record["unit_runs"],
             record["scenario_runs"]))
    print()
    print(render_next(ctx.state, gather_facts(ctx)))
    return EXIT_OK


def _step_abandon(args):
    ctx = open_ctx(args)
    require_target(ctx)
    step = ctx.state.get("step")
    if not step:
        raise TddError("no increment is open", EXIT_REFUSED)
    iso, _epoch = stamp()
    record = {"id": step.get("id"), "n": step.get("n"), "title": step.get("title"),
              "started_at": step.get("started_at"), "finished_at": iso,
              "abandoned": True}
    with Lock(ctx, force=args.force_unlock):
        mutate(ctx, "step_abandon",
               {"step": None,
                "steps_done": list(ctx.state.get("steps_done") or []) + [record],
                "attempts": {"verify_unit": 0, "verify_scenario": 0,
                             "same_failure_streak": 0, "last_signature": None}},
               summary="step abandon %s" % record["id"],
               data={"summary": "step abandon %s" % record["id"]})
    print("increment %s abandoned. The code on disk was NOT changed - use "
          "`%s revert-step` before abandoning if you wanted it undone."
          % (record["id"], CMD))
    print()
    print(render_next(ctx.state, gather_facts(ctx)))
    return EXIT_OK


def cmd_revert_step(args):
    ctx = open_ctx(args)
    require_target(ctx)
    step = ctx.state.get("step")
    if not step:
        raise TddError("no increment is open - nothing to revert", EXIT_REFUSED)
    files = step.get("snapshotted") or step.get("files") or []
    if not files:
        raise TddError("increment %s has no snapshot to restore" % step.get("id"),
                       EXIT_REFUSED)

    restored, missing = restore_snapshot(ctx, step.get("id"), files)
    if not restored:
        raise TddError("snapshot for %s is gone from .claude/tdd/snapshots - "
                       "restore the files yourself (git checkout works if the "
                       "increment recorded a base commit: %s)"
                       % (step.get("id"), step.get("base_commit") or "none"),
                       EXIT_REFUSED)

    # The increment starts over: same title, clean slate, gates unverified.
    iso, epoch = stamp()
    fresh = dict(step)
    fresh.update({"started_at": iso, "started_at_epoch": epoch,
                  "reverts": (step.get("reverts") or 0) + 1})
    with Lock(ctx, force=args.force_unlock):
        mutate(ctx, "revert",
               {"step": fresh,
                "unit.last_run": None, "unit.green": False,
                "scenario.last_run": None,
                "scenario.green": bool(not scenario_applicable(ctx.state)),
                "attempts": {"verify_unit": 0, "verify_scenario": 0,
                             "same_failure_streak": 0, "last_signature": None},
                "freshness.dirty": False, "freshness.dirty_files": []},
               summary="revert %s (attempt %d)" % (step.get("id"),
                                                   fresh["reverts"] + 1),
               data={"summary": "revert %s (attempt %d)"
                                % (step.get("id"), fresh["reverts"] + 1),
                     "restored": restored})

    print("restored %d file(s) to the state they had when %s opened: %s"
          % (len(restored), step.get("id"), join_ids(restored, 4)))
    if missing:
        print("WARN  no snapshot for: %s" % join_ids(missing, 4))
    print("      the increment is still open - try a different approach.")
    print()
    print(render_next(ctx.state, gather_facts(ctx)))
    return EXIT_OK


def cmd_done(args):
    ctx = open_ctx(args)
    require_target(ctx)
    state = ctx.state
    if state.get("step"):
        raise TddError("increment %s is still open - close or abandon it first"
                       % state["step"].get("id"), EXIT_REFUSED)
    facts = gather_facts(ctx)
    blockers = gate_blockers(state, facts)
    if blockers and not args.force:
        raise TddError("cannot finish %s: %s. Pass --force to archive it anyway."
                       % ((state.get("target") or {}).get("simple_name"),
                          "; ".join(blockers)), EXIT_REFUSED)

    target = state.get("target") or {}
    folder = ctx.archive / ("%s-%s" % (now_iso().replace(":", "-"),
                                       target.get("simple_name") or "target"))
    folder.mkdir(parents=True, exist_ok=True)
    atomic_write(folder / "session.json", json.dumps(state, indent=2) + "\n")
    if ctx.status_md.is_file():
        shutil.copyfile(str(ctx.status_md), str(folder / "STATUS.md"))
    atomic_write(folder / "summary.md", render_archive_summary(state, blockers))

    # Captured before the reset: `mutate` clears steps_done in place.
    closed = len(state.get("steps_done") or [])
    archived_rel = ctx.rel(folder)
    target_path = target.get("path")

    reset = default_state()
    patch = dict((key, reset[key]) for key in reset if key != "schema")
    with Lock(ctx, force=args.force_unlock):
        if args.force and blockers:
            append_journal(ctx, "override", {},
                           {"summary": "forced done over: %s" % "; ".join(blockers)})
        mutate(ctx, "done", patch,
               summary="done %s (%d increment(s))"
                       % (target.get("simple_name"), closed),
               data={"summary": "done %s" % target.get("simple_name"),
                     "archive": archived_rel})

    print("finished %s - %d increment(s) archived to %s"
          % (target_path, closed, archived_rel))
    print()
    print(render_next(ctx.state, gather_facts(ctx)))
    return EXIT_OK


def render_archive_summary(state, blockers):
    target = state.get("target") or {}
    out = ["# %s" % target.get("path"), ""]
    out.append("- Goal: %s" % (target.get("goal") or "-"))
    out.append("- Started: %s" % target.get("started_at"))
    out.append("- Finished: %s" % now_iso())
    out.append("- Unit tests: %s"
               % join_ids([i.get("id") for i in gate(state, "unit").get("selected") or []], 8))
    if scenario_applicable(state):
        out.append("- Scenarios: %s"
                   % join_ids([i.get("id") for i in
                               gate(state, "scenario").get("selected") or []], 8))
    else:
        out.append("- Scenarios: none applicable")
    if blockers:
        out.append("- **Archived with gates not green:** %s" % "; ".join(blockers))
    out += ["", "## Increments", ""]
    for record in state.get("steps_done") or []:
        flag = " (abandoned)" if record.get("abandoned") else ""
        out.append("- `%s` %s%s - %s runs, %s revert(s)"
                   % (record.get("id"), record.get("title"), flag,
                      (record.get("unit_runs") or 0) + (record.get("scenario_runs") or 0),
                      record.get("reverts") or 0))
    if state.get("notes"):
        out += ["", "## Notes", ""]
        for note in state["notes"]:
            out.append("- %s" % note.get("text"))
    return "\n".join(out) + "\n"


def cmd_touch(args):
    """PostToolUse hook target.  Must never fail a tool call: always exit 0.

    Reads stdin only under --from-hook.  Claude Code writes the event JSON and
    closes the pipe; anywhere else stdin may be an open pipe that never closes,
    and a blocking read here would hang the tool call it is attached to.
    """
    try:
        if not getattr(args, "from_hook", False):
            return EXIT_OK
        payload = sys.stdin.read()
        data = json.loads(payload) if payload.strip() else {}
        edited = ((data.get("tool_input") or {}).get("file_path")
                  or (data.get("tool_input") or {}).get("notebook_path"))
        if not edited:
            return EXIT_OK
        ctx = open_ctx(args)
        if ctx.corrupt or not ctx.state.get("target"):
            return EXIT_OK
        rel = ctx.rel(edited)
        if rel not in watched_files(ctx.state):
            return EXIT_OK
        freshness = dict(ctx.state.get("freshness") or {})
        dirty = list(freshness.get("dirty_files") or [])
        if rel not in dirty:
            dirty.append(rel)
        with Lock(ctx, force=True):
            mutate(ctx, "touch",
                   {"freshness.dirty": True, "freshness.dirty_files": dirty},
                   data={"summary": "edited " + rel})
    except Exception:
        pass
    return EXIT_OK


def cmd_stub(args):
    step = STUB_STEPS.get(args.command, "?")
    print("tdd: `%s` is not implemented yet - it lands in DEVPLAN step %s."
          % (args.command, step), file=sys.stderr)
    return EXIT_ERROR


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="tddstate.py",
        description="Durable memory for a Java TDD refactoring loop. "
                    "Start every session with `next`.")
    parser.add_argument("--repo-root", default=None,
                        help="repository root (default: inferred from this script's location)")
    parser.add_argument("--force-unlock", action="store_true",
                        help="ignore a lock held by another process")
    subs = parser.add_subparsers(dest="command")
    subs.required = True

    p = subs.add_parser("init", help="start work on a Java file")
    p.add_argument("--target", required=True)
    p.add_argument("--goal", default=None)
    p.add_argument("--force", action="store_true", help="replace the active target")
    p.set_defaults(func=cmd_init)

    p = subs.add_parser("next", help="the single next action (start here)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_next)

    p = subs.add_parser("status", help="full picture")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = subs.add_parser("resume", help="session-opening brief")
    p.add_argument("--brief", action="store_true", help="accepted for symmetry; output is already brief")
    p.add_argument("--quiet-if-idle", action="store_true",
                   help="print nothing when no target is active (for hooks)")
    p.set_defaults(func=cmd_resume)

    p = subs.add_parser("note", help="leave a note for the next session")
    p.add_argument("text", nargs="+")
    p.set_defaults(func=cmd_note)

    p = subs.add_parser("block", help="stop the loop and escalate to the human")
    p.add_argument("reason", nargs="+")
    p.set_defaults(func=cmd_block)

    p = subs.add_parser("unblock", help="clear a block")
    p.set_defaults(func=cmd_unblock)

    p = subs.add_parser("checkpoint", help="regenerate STATUS.md")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_checkpoint)

    p = subs.add_parser("repair", help="rebuild session.json from the journal")
    p.set_defaults(func=cmd_repair)

    p = subs.add_parser("touch", help="internal: PostToolUse hook target")
    p.add_argument("--from-hook", action="store_true")
    p.set_defaults(func=cmd_touch)

    p = subs.add_parser("step", help="open, close or drop one refactor increment")
    p.add_argument("action", choices=["start", "done", "abandon"])
    p.add_argument("title", nargs="*")
    p.add_argument("--file", action="append", default=[],
                   help="extra file this increment may touch (snapshotted, watched)")
    p.add_argument("--force", action="store_true",
                   help="close even though a gate is not green (journaled)")
    p.set_defaults(func=cmd_step)

    p = subs.add_parser("revert-step",
                        help="restore the files to their state when the increment opened")
    p.set_defaults(func=cmd_revert_step)

    p = subs.add_parser("done", help="finish the target and archive it")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_done)

    p = subs.add_parser("discover", help="find the tests that cover the target")
    p.add_argument("kind", choices=["unit", "scenario"])
    p.set_defaults(func=cmd_discover)

    p = subs.add_parser("select", help="confirm which candidates are pertinent")
    p.add_argument("kind", choices=["unit", "scenario"])
    p.add_argument("ids", nargs="*")
    p.add_argument("--all", action="store_true",
                   help="take every candidate scoring 2 or better")
    p.add_argument("--none", action="store_true",
                   help="scenario only: this target has no scenario coverage")
    p.add_argument("--task", default=None,
                   help="scenario only: the gradle task the scenarios run under "
                        "(default 'test'; often 'cucumberTest' or 'integrationTest')")
    p.set_defaults(func=cmd_select)

    p = subs.add_parser("doctor", help="check the environment and the build setup")
    p.set_defaults(func=cmd_doctor)

    p = subs.add_parser("run", help="run the selected tests and record the verdict")
    p.add_argument("kind", choices=["unit", "scenario"])
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p.add_argument("--force", action="store_true",
                   help="run scenarios even though the unit gate is not green")
    p.set_defaults(func=cmd_run)

    p = subs.add_parser("baseline", help="record the pre-refactor baseline")
    p.add_argument("kind", choices=["unit", "scenario"])
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p.set_defaults(func=cmd_baseline)

    p = subs.add_parser("record", help="enter a verdict by hand (fallback)")
    p.add_argument("kind", choices=["unit", "scenario"])
    p.add_argument("--result", required=True, choices=["pass", "fail"])
    p.add_argument("--note", nargs="*", default=[])
    p.set_defaults(func=cmd_record)

    for name in sorted(STUB_STEPS):
        p = subs.add_parser(name, help="(DEVPLAN step %d - not implemented yet)"
                                       % STUB_STEPS[name])
        p.add_argument("rest", nargs="*")
        p.set_defaults(func=cmd_stub)

    return parser


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except TddError as exc:
        print("tdd: %s" % exc, file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
