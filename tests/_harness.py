"""Shared test helpers: load the shipped tracker and build states to order."""

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "payload" / ".claude" / "tdd" / "tddstate.py"

_spec = importlib.util.spec_from_file_location("tddstate", SRC)
tddstate = importlib.util.module_from_spec(_spec)
sys.modules["tddstate"] = tddstate
_spec.loader.exec_module(tddstate)

# Epoch ordering used throughout: target < step start < run.
T_TARGET = 1000.0
T_STEP = 2000.0
T_RUN = 3000.0

FACTS = {"now": "2026-09-12T10:00:00Z", "now_epoch": T_RUN + 100,
         "newest_epoch": None, "newest_path": None, "missing": []}


def run(outcome="passed", failed=None, epoch=T_RUN, total=12, passed=12):
    return {
        "ran_at": tddstate.now_iso(epoch), "ran_at_epoch": epoch,
        "cmd": "gradlew :app:test", "log": "logs/x.log", "outcome": outcome,
        "total": total, "pass": passed, "fail": len(failed or []), "skipped": 0,
        "failed": failed or [],
        "signature": tddstate.failure_signature(failed or []),
    }


def state_at(level):
    """A state just complete enough to reach a given resolver row.

    `level` names the last thing that was accomplished, so each test asks for
    the level *below* the row it is exercising.
    """
    s = tddstate.default_state()
    order = ["empty", "target", "unit_candidates", "unit_selected", "unit_baseline",
             "scen_candidates", "scen_selected", "scen_baseline", "step_open",
             "unit_green", "scen_green", "step_closed"]
    idx = order.index(level)

    if idx >= order.index("target"):
        s["target"] = {"path": "src/main/java/com/acme/Order.java",
                       "class_fqn": "com.acme.Order", "simple_name": "Order",
                       "gradle_project": ":app", "goal": "extract pricing strategy",
                       "started_at": tddstate.now_iso(T_TARGET),
                       "started_at_epoch": T_TARGET}
    if idx >= order.index("unit_candidates"):
        s["unit"]["candidates"] = [
            {"id": "com.acme.OrderTest", "path": "src/test/java/com/acme/OrderTest.java",
             "score": 3, "reason": "name match"}]
    if idx >= order.index("unit_selected"):
        s["unit"]["selected"] = [
            {"id": "com.acme.OrderTest", "path": "src/test/java/com/acme/OrderTest.java",
             "status": "unknown"}]
    if idx >= order.index("unit_baseline"):
        s["unit"]["baseline"] = {"ran_at": tddstate.now_iso(T_TARGET), "total": 12,
                                 "pass": 12, "fail": 0, "failed_ids": []}
    if idx >= order.index("scen_candidates"):
        s["scenario"]["candidates"] = [
            {"id": "src/test/resources/features/order.feature:14",
             "name": "Discount applied", "tags": ["@pricing"], "score": 3,
             "reason": "step-def hop"}]
    if idx >= order.index("scen_selected"):
        s["scenario"]["selected"] = [
            {"id": "src/test/resources/features/order.feature:14",
             "name": "Discount applied", "tags": ["@pricing"], "status": "unknown"}]
    if idx >= order.index("scen_baseline"):
        s["scenario"]["baseline"] = {"ran_at": tddstate.now_iso(T_TARGET), "total": 2,
                                     "pass": 2, "fail": 0, "failed_ids": []}
    if idx >= order.index("step_open"):
        s["step"] = {"id": "s03", "n": 3, "title": "extract PriceCalculator",
                     "started_at": tddstate.now_iso(T_STEP), "started_at_epoch": T_STEP,
                     "base_commit": "abc1234", "snapshot_dir": "snapshots/s03",
                     "files": ["src/main/java/com/acme/Order.java"]}
    if idx >= order.index("unit_green"):
        s["unit"]["last_run"] = run()
        s["unit"]["green"] = True
    if idx >= order.index("scen_green"):
        s["scenario"]["last_run"] = run(total=2, passed=2)
        s["scenario"]["green"] = True
    if idx >= order.index("step_closed"):
        s["step"] = None
        s["steps_done"] = [{"id": "s03", "title": "extract PriceCalculator",
                            "finished_at": tddstate.now_iso(T_RUN)}]
    return s


def code_for(state, facts=None):
    return tddstate.resolve_next(state, facts or FACTS)["code"]


def row_for(state, facts=None):
    return tddstate.resolve_next(state, facts or FACTS)["row"]


class GradleRepoCase(unittest.TestCase):
    """A repo with a stub `gradlew`; run_process is replaced per test."""

    def setUp(self):
        self.repo = pathlib.Path(tempfile.mkdtemp(prefix="tddgr-"))
        (self.repo / "app" / "src" / "main" / "java" / "com" / "acme").mkdir(parents=True)
        (self.repo / "app" / "build.gradle").write_text("plugins { id 'java' }\n",
                                                        encoding="utf-8")
        for name in ("gradlew", "gradlew.bat"):
            wrapper = self.repo / name
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            os.chmod(str(wrapper), 0o755)
        self.target = "app/src/main/java/com/acme/Order.java"
        (self.repo / self.target).write_text(
            "package com.acme;\npublic class Order {}\n", encoding="utf-8")
        os.environ["TDD_REPO_ROOT"] = str(self.repo)
        self.real_run_process = tddstate.run_process

    def tearDown(self):
        tddstate.run_process = self.real_run_process
        os.environ.pop("TDD_REPO_ROOT", None)
        shutil.rmtree(str(self.repo), ignore_errors=True)

    def cli(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            code = tddstate.main(list(argv))
        self.last_output = buf.getvalue()
        return code

    def ctx(self):
        c = tddstate.Ctx(self.repo)
        tddstate.load_state(c)
        return c

    def stub_gradle(self, xml=None, output="", exit_code=0, timed_out=False,
                    ndjson=None, task="test"):
        repo = self.repo

        def _run(argv, cwd, timeout):
            if xml:
                rdir = repo / "app" / "build" / "test-results" / task
                rdir.mkdir(parents=True, exist_ok=True)
                for name, content in xml:
                    (rdir / name).write_text(content, encoding="utf-8")
            if ndjson is not None:
                path = repo / "app" / "build" / "tdd" / "cucumber.ndjson"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(ndjson, encoding="utf-8")
            return exit_code, output, 1.5, timed_out

        tddstate.run_process = _run

    def next_action(self):
        """`next` never writes state, so ask it directly."""
        self.cli("next", "--json")
        return json.loads(self.last_output)["action"]["code"]

    def select(self, **patch):
        ctx = self.ctx()
        with tddstate.Lock(ctx, force=True):
            tddstate.mutate(ctx, "select", patch)

    def prepare_unit(self):
        self.cli("init", "--target", self.target, "--goal", "extract pricing")
        self.select(**{"unit.selected": [
            {"id": "com.acme.OrderTest",
             "path": "app/src/test/java/com/acme/OrderTest.java",
             "status": "unknown"}]})
