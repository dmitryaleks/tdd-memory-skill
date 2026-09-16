"""Increment lifecycle and the guards around it (DEVPLAN sections 3.3, 8)."""

import json
import unittest

from _harness import GradleRepoCase, tddstate

GREEN_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.acme.OrderTest" tests="2" skipped="0" failures="0" errors="0">
  <testcase name="totalsLines()" classname="com.acme.OrderTest" time="0.01"/>
  <testcase name="appliesDiscount()" classname="com.acme.OrderTest" time="0.01"/>
</testsuite>
"""

GLUE_SOURCE = "package com.acme.steps;\npublic class PricingSteps { int v; }\n"
CALC_SOURCE = "package com.acme;\nclass PriceCalculator {}\n"
UNIT_SOURCE = "package com.acme;\nclass OrderTest { int v; }\n"
FEATURE_SOURCE = "Feature: Orders\n  Scenario: Discount\n    Given a basket\n"

RED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.acme.OrderTest" tests="2" skipped="0" failures="1" errors="0">
  <testcase name="totalsLines()" classname="com.acme.OrderTest" time="0.01"/>
  <testcase name="appliesDiscount()" classname="com.acme.OrderTest" time="0.01">
    <failure message="expected: &lt;10&gt; but was: &lt;12&gt;">stack</failure>
  </testcase>
</testsuite>
"""


class StepCase(GradleRepoCase):
    """Target selected, unit baseline green, scenarios not applicable."""

    def baseline(self):
        self.prepare_unit()
        self.select(**{"scenario.not_applicable": True, "scenario.green": True})
        self.stub_gradle(xml=[("TEST-a.xml", GREEN_XML)])
        self.cli("baseline", "unit")

    def open_step(self, title="extract PriceCalculator", *extra):
        self.baseline()
        return self.cli("step", "start", title, *extra)

    def edit_target(self, body="package com.acme;\npublic class Order { int x; }\n"):
        (self.repo / self.target).write_text(body, encoding="utf-8")

    def variant(self, marker):
        """A distinct edit of the target, named for what it is meant to do."""
        return "package com.acme;\npublic class Order { int %s; }\n" % marker

    SCEN_GREEN = ('<?xml version="1.0"?><testsuite name="c" tests="1" failures="0">'
                  '<testcase name="Discount" classname="Orders"/></testsuite>')
    SCEN_RED = ('<?xml version="1.0"?><testsuite name="c" tests="1" failures="1">'
                '<testcase name="Discount" classname="Orders">'
                '<failure message="expected 135 but was 150">x</failure>'
                '</testcase></testsuite>')

    def open_step_with_scenarios(self):
        self.prepare_unit()
        self.select(**{"scenario.selected": [
            {"id": "app/src/test/resources/features/order.feature:14",
             "name": "Discount", "status": "unknown"}],
            "scenario.runner_class": "com.acme.RunCucumberTest",
            "scenario.glue": ["app/src/test/java/com/acme/steps/PricingSteps.java"]})
        self.stub_gradle(xml=[("TEST-a.xml", GREEN_XML)])
        self.cli("baseline", "unit")
        self.stub_gradle(xml=[("TEST-c.xml", self.SCEN_GREEN)])
        self.cli("baseline", "scenario")
        self.cli("step", "start", "extract PriceCalculator")

    def green_run(self):
        self.stub_gradle(xml=[("TEST-a.xml", GREEN_XML)])
        self.cli("run", "unit")

    def red_run(self):
        self.stub_gradle(xml=[("TEST-a.xml", RED_XML)], exit_code=1)
        self.cli("run", "unit")


class TestStepStart(StepCase):

    def test_opening_an_increment_numbers_and_snapshots_it(self):
        self.assertEqual(self.open_step(), 0)
        step = self.ctx().state["step"]
        self.assertEqual(step["id"], "s01")
        self.assertEqual(step["n"], 1)
        self.assertEqual(step["title"], "extract PriceCalculator")
        snapshot = self.repo / ".claude" / "tdd" / "snapshots" / "s01" / self.target
        self.assertTrue(snapshot.is_file())
        self.assertEqual(snapshot.read_text(encoding="utf-8"),
                         (self.repo / self.target).read_text(encoding="utf-8"))

    def test_extra_files_are_snapshotted_and_watched(self):
        extra = "app/src/main/java/com/acme/PriceCalculator.java"
        (self.repo / extra).write_text("package com.acme;\nclass PriceCalculator {}\n",
                                       encoding="utf-8")
        self.open_step("extract calc", "--file", extra)
        step = self.ctx().state["step"]
        self.assertIn(extra, step["files"])
        self.assertTrue((self.repo / ".claude/tdd/snapshots/s01" / extra).is_file())

    def test_a_second_increment_cannot_be_opened_over_an_open_one(self):
        self.open_step()
        with _capture_stderr() as err:
            code = self.cli("step", "start", "another")
        self.assertEqual(code, tddstate.EXIT_REFUSED)
        self.assertIn("still open", err.getvalue())

    def test_editing_before_a_baseline_is_refused(self):
        self.prepare_unit()
        self.select(**{"scenario.not_applicable": True})
        self.assertEqual(self.cli("step", "start", "too early"),
                         tddstate.EXIT_REFUSED)

    def test_a_title_is_required(self):
        self.baseline()
        self.assertEqual(self.cli("step", "start"), tddstate.EXIT_ERROR)

    def test_numbering_continues_across_closed_increments(self):
        self.open_step("first")
        self.edit_target()
        self.green_run()
        self.cli("step", "done")
        self.cli("step", "start", "second")
        self.assertEqual(self.ctx().state["step"]["id"], "s02")

    def test_a_missing_file_is_reported_rather_than_silently_unsnapshotted(self):
        self.baseline()
        self.cli("step", "start", "x", "--file", "app/src/main/java/com/acme/Gone.java")
        self.assertIn("not on disk", self.last_output)


class TestFreshnessGate(StepCase):
    """The guard that stops 'declared green on stale results'."""

    def test_closing_is_refused_while_the_target_is_newer_than_the_run(self):
        self.open_step()
        self.edit_target()
        self.green_run()
        self.edit_target("package com.acme;\npublic class Order { int y; }\n")
        code = self.cli("step", "done")
        self.assertEqual(code, tddstate.EXIT_REFUSED)

    def test_the_refusal_names_one_reason_and_what_to_do(self):
        self.open_step()
        self.edit_target()
        self.green_run()
        self.edit_target("package com.acme;\npublic class Order { int z; }\n")
        with _capture_stderr() as err:
            self.cli("step", "done")
        message = err.getvalue()
        self.assertIn("cannot close s01", message)
        self.assertIn("Re-run the tests first", message)
        self.assertEqual(len(message.strip().splitlines()), 1)

    def test_an_edit_in_the_same_instant_as_the_run_still_counts(self):
        """The margin here is microseconds. Ordering timestamps cannot decide
        it; comparing the recorded mtimes can."""
        self.open_step()
        self.edit_target()
        self.green_run()
        run = self.ctx().state["unit"]["last_run"]
        target = self.repo / self.target
        self.edit_target("package com.acme;\npublic class Order { int q; }\n")
        # Force the pathological case: the edit carries the run's own timestamp.
        import os as _os
        _os.utime(str(target), (run["ran_at_epoch"], run["ran_at_epoch"]))
        self.assertEqual(self.cli("step", "done"), tddstate.EXIT_REFUSED)

    def test_a_run_in_the_same_instant_as_an_edit_is_not_falsely_stale(self):
        """The opposite error: refusing to close work that really was verified."""
        self.open_step()
        self.edit_target()
        self.green_run()
        run = self.ctx().state["unit"]["last_run"]
        self.assertEqual(
            run["watched"].get(self.target),
            (self.repo / self.target).stat().st_mtime,
            "the run must record the mtime it actually tested")
        self.assertEqual(self.cli("step", "done"), 0)

    def test_deleting_a_watched_file_invalidates_the_result(self):
        self.open_step()
        self.edit_target()
        self.green_run()
        (self.repo / self.target).unlink()
        self.assertEqual(self.cli("step", "done"), tddstate.EXIT_REFUSED)

    def test_closing_is_refused_while_a_gate_is_red(self):
        self.open_step()
        self.edit_target()
        self.red_run()
        self.assertEqual(self.cli("step", "done"), tddstate.EXIT_REFUSED)

    def test_closing_is_refused_when_no_run_happened_inside_the_increment(self):
        self.open_step()
        self.edit_target()
        self.assertEqual(self.cli("step", "done"), tddstate.EXIT_REFUSED)

    def test_a_fresh_green_run_closes_the_increment(self):
        self.open_step()
        self.edit_target()
        self.green_run()
        self.assertEqual(self.cli("step", "done"), 0)
        state = self.ctx().state
        self.assertIsNone(state["step"])
        self.assertEqual(state["steps_done"][0]["id"], "s01")
        self.assertEqual(state["steps_done"][0]["unit_runs"], 1)

    def test_closing_resets_the_attempt_counters(self):
        self.open_step()
        self.edit_target()
        self.red_run()
        self.green_run()
        self.cli("step", "done")
        attempts = self.ctx().state["attempts"]
        self.assertEqual(attempts["same_failure_streak"], 0)
        self.assertEqual(attempts["verify_unit"], 0)

    def test_force_closes_over_a_red_gate_and_journals_the_override(self):
        self.open_step()
        self.edit_target()
        self.red_run()
        self.assertEqual(self.cli("step", "done", "--force"), 0)
        events = [e["event"] for e in tddstate.read_journal(self.ctx())]
        self.assertIn("override", events)
        self.assertIn("forced", self.ctx().state["steps_done"][0])


class TestScenarioFixReopensTheUnitGate(StepCase):
    """A scenario-driven fix is still a change to the behaviour under test.

    Both gates have to go green again within the same increment, unit first:
    the edit that satisfies a Cucumber scenario can just as easily break a
    unit test.
    """

    def reach_a_red_scenario(self):
        self.open_step_with_scenarios()
        self.edit_target()
        self.stub_gradle(xml=[("TEST-a.xml", GREEN_XML)])
        self.cli("run", "unit")
        self.stub_gradle(xml=[("TEST-c.xml", self.SCEN_RED)], exit_code=1)
        self.cli("run", "scenario")
        self.assertEqual(self.next_action(), "FIX_SCENARIO")

    def test_the_fix_instruction_sends_the_model_at_the_unit_gate(self):
        """Its command must be the one that will be correct after the fix."""
        self.reach_a_red_scenario()
        self.cli("next")
        self.assertIn("BOTH gates", self.last_output)
        self.assertIn("run unit", self.last_output.split("CMD")[1])

    def test_a_scenario_driven_edit_re_opens_the_unit_gate(self):
        self.reach_a_red_scenario()
        self.edit_target(self.variant("fixed"))
        self.assertEqual(self.next_action(), "RUN_UNIT")

    def test_running_scenarios_straight_after_the_fix_is_refused(self):
        self.reach_a_red_scenario()
        self.edit_target(self.variant("fixed"))
        self.assertEqual(self.cli("run", "scenario"), tddstate.EXIT_REFUSED)

    def test_a_scenario_fix_that_breaks_a_unit_test_is_caught(self):
        """The regression this ordering exists to prevent."""
        self.reach_a_red_scenario()
        self.edit_target(self.variant("broke"))
        self.stub_gradle(xml=[("TEST-a.xml", RED_XML)], exit_code=1)
        self.cli("run", "unit")
        self.assertEqual(self.next_action(), "FIX_UNIT")
        self.assertEqual(self.cli("step", "done"), tddstate.EXIT_REFUSED)

    def test_the_increment_closes_only_once_both_gates_are_green_again(self):
        self.reach_a_red_scenario()
        self.edit_target(self.variant("fixed"))
        self.stub_gradle(xml=[("TEST-a.xml", GREEN_XML)])
        self.cli("run", "unit")
        self.assertEqual(self.next_action(), "RUN_SCENARIO")
        self.stub_gradle(xml=[("TEST-c.xml", self.SCEN_GREEN)])
        self.cli("run", "scenario")
        self.assertEqual(self.next_action(), "FINISH_STEP")
        self.assertEqual(self.cli("step", "done"), 0)

    def test_editing_the_step_definitions_re_opens_the_scenario_gate(self):
        """Cucumber failures are often repaired in the glue, not the source.

        Glue drives the scenarios only, so it re-opens that gate alone - but
        it must still block the increment from closing.
        """
        self.reach_a_red_scenario()
        self.edit_target(self.variant("fixed"))
        self.stub_gradle(xml=[("TEST-a.xml", GREEN_XML)])
        self.cli("run", "unit")
        self.stub_gradle(xml=[("TEST-c.xml", self.SCEN_GREEN)])
        self.cli("run", "scenario")
        self.assertEqual(self.next_action(), "FINISH_STEP")

        glue = self.repo / "app/src/test/java/com/acme/steps/PricingSteps.java"
        glue.parent.mkdir(parents=True, exist_ok=True)
        glue.write_text(GLUE_SOURCE, encoding="utf-8")
        self.assertEqual(self.next_action(), "RUN_SCENARIO")
        self.assertEqual(self.cli("step", "done"), tddstate.EXIT_REFUSED)


class TestPerGateWatching(StepCase):
    """Each gate watches only what can actually change its result.

    Production code affects both. Beyond that they are independent: a feature
    file cannot change what the unit tests do, and a unit test cannot change
    what the scenarios do.
    """

    def both_green(self):
        self.open_step_with_scenarios()
        self.edit_target()
        self.stub_gradle(xml=[("TEST-a.xml", GREEN_XML)])
        self.cli("run", "unit")
        self.stub_gradle(xml=[("TEST-c.xml", self.SCEN_GREEN)])
        self.cli("run", "scenario")
        self.assertEqual(self.next_action(), "FINISH_STEP")

    def touch(self, rel, body):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def test_each_gate_watches_its_own_side(self):
        self.open_step_with_scenarios()
        state = self.ctx().state
        unit = tddstate.watched_files(state, "unit")
        scen = tddstate.watched_files(state, "scenario")
        self.assertIn("app/src/test/java/com/acme/OrderTest.java", unit)
        self.assertNotIn("app/src/test/resources/features/order.feature", unit)
        self.assertIn("app/src/test/resources/features/order.feature", scen)
        self.assertIn("app/src/test/java/com/acme/steps/PricingSteps.java", scen)
        self.assertNotIn("app/src/test/java/com/acme/OrderTest.java", scen)

    def test_the_target_is_watched_by_both(self):
        self.open_step_with_scenarios()
        state = self.ctx().state
        for kind in ("unit", "scenario"):
            self.assertIn(self.target, tddstate.watched_files(state, kind), kind)

    def test_files_declared_on_the_increment_are_watched_by_both(self):
        extra = "app/src/main/java/com/acme/PriceCalculator.java"
        self.touch(extra, CALC_SOURCE)
        self.prepare_unit()
        self.select(**{"scenario.not_applicable": True, "scenario.green": True})
        self.stub_gradle(xml=[("TEST-a.xml", GREEN_XML)])
        self.cli("baseline", "unit")
        self.cli("step", "start", "extract calc", "--file", extra)
        for kind in ("unit", "scenario"):
            self.assertIn(extra, tddstate.watched_files(self.ctx().state, kind), kind)

    def test_editing_a_feature_file_leaves_the_unit_result_alone(self):
        self.both_green()
        self.touch("app/src/test/resources/features/order.feature", FEATURE_SOURCE)
        facts = tddstate.gather_facts(self.ctx())
        state = self.ctx().state
        self.assertIsNone(tddstate.staleness(state, "unit", facts))
        self.assertIsNotNone(tddstate.staleness(state, "scenario", facts))
        self.assertEqual(self.next_action(), "RUN_SCENARIO")

    def test_editing_a_unit_test_leaves_the_scenario_result_alone(self):
        self.both_green()
        self.touch("app/src/test/java/com/acme/OrderTest.java", UNIT_SOURCE)
        facts = tddstate.gather_facts(self.ctx())
        state = self.ctx().state
        self.assertIsNotNone(tddstate.staleness(state, "unit", facts))
        self.assertIsNone(tddstate.staleness(state, "scenario", facts))
        self.assertEqual(self.next_action(), "RUN_UNIT")

    def test_a_feature_edit_does_not_force_a_pointless_unit_re_run(self):
        """The whole point of scoping: one gate re-runs, not both."""
        self.both_green()
        self.touch("app/src/test/resources/features/order.feature", FEATURE_SOURCE)
        self.stub_gradle(xml=[("TEST-c.xml", self.SCEN_GREEN)])
        self.assertEqual(self.cli("run", "scenario"), 0)
        self.assertEqual(self.next_action(), "FINISH_STEP")

    def test_production_code_still_invalidates_both(self):
        self.both_green()
        self.edit_target(self.variant("changed"))
        facts = tddstate.gather_facts(self.ctx())
        state = self.ctx().state
        for kind in ("unit", "scenario"):
            self.assertIsNotNone(tddstate.staleness(state, kind, facts), kind)

    def test_a_scenario_run_does_not_clear_dirt_owned_by_the_unit_gate(self):
        self.both_green()
        self.touch("app/src/test/java/com/acme/OrderTest.java", UNIT_SOURCE)
        ctx = self.ctx()
        with tddstate.Lock(ctx, force=True):
            tddstate.mutate(ctx, "touch", {
                "freshness.dirty": True,
                "freshness.dirty_files": ["app/src/test/java/com/acme/OrderTest.java"]})
        self.stub_gradle(xml=[("TEST-c.xml", self.SCEN_GREEN)])
        self.cli("run", "scenario")
        state = self.ctx().state
        self.assertTrue(state["freshness"]["dirty"])
        self.assertIn("app/src/test/java/com/acme/OrderTest.java",
                      state["freshness"]["dirty_files"])
        self.assertEqual(self.next_action(), "RUN_UNIT")


class TestAbandon(StepCase):

    def test_abandon_closes_without_the_gates(self):
        self.open_step()
        self.edit_target()
        self.red_run()
        self.assertEqual(self.cli("step", "abandon"), 0)
        state = self.ctx().state
        self.assertIsNone(state["step"])
        self.assertTrue(state["steps_done"][0]["abandoned"])

    def test_abandon_says_plainly_that_the_code_was_not_reverted(self):
        self.open_step()
        self.cli("step", "abandon")
        self.assertIn("NOT changed", self.last_output)

    def test_abandoned_increments_still_consume_a_number(self):
        self.open_step("first")
        self.cli("step", "abandon")
        self.cli("step", "start", "second")
        self.assertEqual(self.ctx().state["step"]["id"], "s02")


class TestRevert(StepCase):

    def test_revert_restores_the_file_byte_for_byte(self):
        self.open_step()
        original = (self.repo / self.target).read_bytes()
        self.edit_target("package com.acme;\npublic class Order { /* broken */ }\n")
        self.assertNotEqual((self.repo / self.target).read_bytes(), original)
        self.assertEqual(self.cli("revert-step"), 0)
        self.assertEqual((self.repo / self.target).read_bytes(), original)

    def test_revert_keeps_the_increment_open_for_another_attempt(self):
        self.open_step()
        self.edit_target()
        self.red_run()
        self.cli("revert-step")
        step = self.ctx().state["step"]
        self.assertIsNotNone(step)
        self.assertEqual(step["id"], "s01")
        self.assertEqual(step["reverts"], 1)

    def test_revert_clears_the_thrash_counter_and_the_stale_verdict(self):
        self.open_step()
        self.edit_target()
        self.red_run()
        self.red_run()
        self.assertEqual(self.ctx().state["attempts"]["same_failure_streak"], 2)
        self.cli("revert-step")
        state = self.ctx().state
        self.assertEqual(state["attempts"]["same_failure_streak"], 0)
        self.assertIsNone(state["unit"]["last_run"])

    def test_after_reverting_the_model_is_told_to_try_a_different_change(self):
        self.open_step()
        self.edit_target()
        self.red_run()
        self.cli("revert-step")
        self.assertEqual(self.next_action(), "AWAIT_EDIT")

    def test_escalation_recommends_revert_and_revert_resolves_it(self):
        self.open_step()
        self.edit_target()
        for _ in range(3):
            self.red_run()
        self.assertEqual(self.next_action(), "ESCALATE")
        self.cli("revert-step")
        self.assertEqual(self.next_action(), "AWAIT_EDIT")

    def test_revert_without_an_open_increment_is_refused(self):
        self.baseline()
        self.assertEqual(self.cli("revert-step"), tddstate.EXIT_REFUSED)

    def test_a_deleted_snapshot_is_reported_with_the_git_fallback(self):
        self.open_step()
        import shutil as _shutil
        _shutil.rmtree(str(self.repo / ".claude" / "tdd" / "snapshots" / "s01"))
        with _capture_stderr() as err:
            code = self.cli("revert-step")
        self.assertEqual(code, tddstate.EXIT_REFUSED)
        self.assertIn("git checkout", err.getvalue())


class TestDone(StepCase):

    def finish_one_increment(self):
        self.open_step()
        self.edit_target()
        self.green_run()
        self.cli("step", "done")

    def test_done_archives_the_target_and_clears_the_session(self):
        self.finish_one_increment()
        self.assertEqual(self.cli("done"), 0)
        state = self.ctx().state
        self.assertIsNone(state["target"])
        self.assertEqual(state["steps_done"], [])
        archives = list((self.repo / ".claude" / "tdd" / "archive").iterdir())
        self.assertEqual(len(archives), 1)
        for name in ("session.json", "summary.md"):
            self.assertTrue((archives[0] / name).is_file(), name)

    def test_the_archived_summary_records_the_increments(self):
        self.finish_one_increment()
        self.cli("done")
        archive = next((self.repo / ".claude/tdd/archive").iterdir())
        summary = (archive / "summary.md").read_text(encoding="utf-8")
        self.assertIn("extract PriceCalculator", summary)
        self.assertIn("extract pricing", summary)

    def test_the_archived_session_keeps_the_finished_state(self):
        self.finish_one_increment()
        self.cli("done")
        archive = next((self.repo / ".claude/tdd/archive").iterdir())
        saved = json.loads((archive / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["target"]["simple_name"], "Order")

    def test_the_confirmation_reports_the_real_count_and_path(self):
        """The count must be read before the reset, and the path not doubled."""
        self.finish_one_increment()
        self.cli("done")
        self.assertIn("1 increment(s) archived to .claude/tdd/archive/",
                      self.last_output)
        self.assertNotIn(".claude/tdd/.claude/tdd", self.last_output)

    def test_done_is_refused_while_an_increment_is_open(self):
        self.open_step()
        self.assertEqual(self.cli("done"), tddstate.EXIT_REFUSED)

    def test_done_is_refused_while_a_gate_is_red(self):
        self.open_step()
        self.edit_target()
        self.red_run()
        self.cli("step", "done", "--force")
        self.assertEqual(self.cli("done"), tddstate.EXIT_REFUSED)

    def test_force_archives_anyway_and_says_so_in_the_summary(self):
        self.open_step()
        self.edit_target()
        self.red_run()
        self.cli("step", "done", "--force")
        self.assertEqual(self.cli("done", "--force"), 0)
        archive = next((self.repo / ".claude/tdd/archive").iterdir())
        self.assertIn("gates not green",
                      (archive / "summary.md").read_text(encoding="utf-8"))

    def test_after_done_the_loop_asks_for_the_next_target(self):
        self.finish_one_increment()
        self.cli("done")
        self.assertEqual(self.next_action(), "SET_TARGET")

    def test_repair_reproduces_the_cleared_session(self):
        """`done` is a patch like any other, so the journal replays it."""
        self.finish_one_increment()
        self.cli("done")
        before = (self.repo / ".claude/tdd/state/session.json").read_text(
            encoding="utf-8")
        (self.repo / ".claude/tdd/state/session.json").unlink()
        self.cli("repair")
        after = (self.repo / ".claude/tdd/state/session.json").read_text(
            encoding="utf-8")
        self.assertEqual(json.loads(before), json.loads(after))


class TestSnapshotHousekeeping(StepCase):

    def test_old_snapshots_are_pruned(self):
        self.baseline()
        snapshots = self.repo / ".claude" / "tdd" / "snapshots"
        snapshots.mkdir(parents=True, exist_ok=True)
        for i in range(20):
            (snapshots / ("old%02d" % i)).mkdir()
        self.cli("step", "start", "prune me")
        kept = [d for d in snapshots.iterdir() if d.is_dir()]
        self.assertLessEqual(len(kept), tddstate.SNAPSHOTS_KEPT)
        self.assertTrue((snapshots / "s01").is_dir())


class TestFullIncrementWalk(StepCase):
    """One increment, end to end, driven only by what `next` says."""

    def test_the_loop_reaches_a_closed_increment(self):
        self.baseline()
        self.assertEqual(self.next_action(), "START_STEP")
        self.cli("step", "start", "extract PriceCalculator")
        self.assertEqual(self.next_action(), "AWAIT_EDIT")
        self.edit_target()
        self.assertEqual(self.next_action(), "RUN_UNIT")
        self.red_run()
        self.assertEqual(self.next_action(), "FIX_UNIT")
        self.edit_target("package com.acme;\npublic class Order { int fixed; }\n")
        self.green_run()
        self.assertEqual(self.next_action(), "FINISH_STEP")
        self.assertEqual(self.cli("step", "done"), 0)
        self.assertEqual(self.next_action(), "NEXT_STEP_OR_DONE")


def _capture_stderr():
    import contextlib
    import io
    buf = io.StringIO()

    class _Ctx:
        def __enter__(self):
            self._redirect = contextlib.redirect_stderr(buf)
            self._redirect.__enter__()
            return buf

        def __exit__(self, *exc):
            return self._redirect.__exit__(*exc)

    return _Ctx()


if __name__ == "__main__":
    unittest.main()
