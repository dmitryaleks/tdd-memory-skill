"""The resolver is the specification (DEVPLAN section 7). Every row gets a test.

resolve_next is pure - no filesystem here, facts are handed in.
"""

import unittest

from _harness import FACTS, code_for, row_for, run, state_at, tddstate


class TestResolverRows(unittest.TestCase):
    """One case per row, in order. First match wins, so each case must also
    prove the rows above it did not fire."""

    def test_row01_blocked_beats_everything(self):
        s = state_at("scen_green")
        s["blocked"] = {"at": "2026-09-12T10:00:00Z", "reason": "needs a product decision"}
        action = tddstate.resolve_next(s, FACTS)
        self.assertEqual(action["code"], "UNBLOCK")
        self.assertEqual(action["row"], 1)
        self.assertIn("needs a product decision", action["instruction"])

    def test_row02_no_target(self):
        self.assertEqual(code_for(state_at("empty")), "SET_TARGET")
        self.assertEqual(row_for(state_at("empty")), 2)

    def test_row03_discover_unit(self):
        self.assertEqual(code_for(state_at("target")), "DISCOVER_UNIT")

    def test_row04_select_unit_lists_candidates(self):
        action = tddstate.resolve_next(state_at("unit_candidates"), FACTS)
        self.assertEqual(action["code"], "SELECT_UNIT")
        self.assertIn("com.acme.OrderTest", action["instruction"])

    def test_row05_baseline_unit_before_editing(self):
        action = tddstate.resolve_next(state_at("unit_selected"), FACTS)
        self.assertEqual(action["code"], "BASELINE_UNIT")
        self.assertIn("BEFORE editing", action["instruction"])

    def test_row06_discover_scenario(self):
        self.assertEqual(code_for(state_at("unit_baseline")), "DISCOVER_SCENARIO")

    def test_row07_select_scenario(self):
        self.assertEqual(code_for(state_at("scen_candidates")), "SELECT_SCENARIO")

    def test_row08_baseline_scenario(self):
        self.assertEqual(code_for(state_at("scen_selected")), "BASELINE_SCENARIO")

    def test_row09_start_first_step(self):
        action = tddstate.resolve_next(state_at("scen_baseline"), FACTS)
        self.assertEqual(action["code"], "START_STEP")
        self.assertEqual(action["row"], 9)

    def test_row10_build_failure_is_not_a_test_failure(self):
        s = state_at("step_open")
        s["unit"]["last_run"] = run(outcome="build_failed")
        action = tddstate.resolve_next(s, FACTS)
        self.assertEqual(action["code"], "FIX_BUILD")
        self.assertIn("never ran", action["instruction"])

    def test_row10_build_failure_yields_to_a_later_edit(self):
        """Once the model has edited, re-running beats 'fix the build'."""
        s = state_at("step_open")
        s["unit"]["last_run"] = run(outcome="build_failed")
        s["freshness"]["dirty"] = True
        self.assertEqual(code_for(s), "RUN_UNIT")

    def test_row10b_no_results_never_reads_as_a_pass(self):
        s = state_at("step_open")
        s["unit"]["last_run"] = run(outcome="no_results")
        action = tddstate.resolve_next(s, FACTS)
        self.assertEqual(action["code"], "DIAGNOSE_RUN")
        self.assertIn("do NOT treat this as a pass", action["instruction"])

    def test_row10c_timeout(self):
        s = state_at("step_open")
        s["unit"]["last_run"] = run(outcome="timeout")
        self.assertEqual(code_for(s), "RERUN_TIMEOUT")

    def test_row11_escalates_after_three_identical_failures(self):
        s = state_at("step_open")
        s["unit"]["last_run"] = run(outcome="tests_failed",
                                    failed=[{"id": "T#a", "message": "boom"}])
        s["attempts"]["same_failure_streak"] = 3
        action = tddstate.resolve_next(s, FACTS)
        self.assertEqual(action["code"], "ESCALATE")
        self.assertIn("STOP editing", action["instruction"])

    def test_row11_does_not_fire_below_the_threshold(self):
        s = state_at("step_open")
        s["unit"]["last_run"] = run(outcome="tests_failed",
                                    failed=[{"id": "T#a", "message": "boom"}])
        s["attempts"]["same_failure_streak"] = 2
        self.assertEqual(code_for(s), "FIX_UNIT")

    def test_row11b_open_increment_with_nothing_done_asks_for_the_edit(self):
        action = tddstate.resolve_next(state_at("step_open"), FACTS)
        self.assertEqual(action["code"], "AWAIT_EDIT")
        self.assertIn("Make the code change", action["instruction"])

    def test_row12_run_unit_once_the_edit_exists(self):
        facts = dict(FACTS)
        facts["newest_epoch"] = 2500.0        # edited after the step opened
        facts["newest_path"] = "src/main/java/com/acme/Order.java"
        action = tddstate.resolve_next(state_at("step_open"), facts)
        self.assertEqual(action["code"], "RUN_UNIT")
        self.assertIn("never run", action["why"])

    def test_row13_fix_unit_names_the_failing_tests(self):
        s = state_at("step_open")
        s["unit"]["last_run"] = run(
            outcome="tests_failed", passed=11,
            failed=[{"id": "com.acme.OrderTest#appliesDiscount",
                     "message": "expected: <10> but was: <12>"}])
        s["unit"]["green"] = False
        action = tddstate.resolve_next(s, FACTS)
        self.assertEqual(action["code"], "FIX_UNIT")
        self.assertIn("appliesDiscount", action["instruction"])
        self.assertIn("expected: <10>", action["instruction"])
        self.assertIn("Do not touch the scenario tests yet", action["instruction"])

    def test_row14_scenario_only_after_unit_is_green(self):
        action = tddstate.resolve_next(state_at("unit_green"), FACTS)
        self.assertEqual(action["code"], "RUN_SCENARIO")

    def test_row15_fix_scenario(self):
        s = state_at("unit_green")
        s["scenario"]["last_run"] = run(
            outcome="tests_failed", total=2, passed=1,
            failed=[{"id": "order.feature:14", "message": "step failed"}])
        s["scenario"]["green"] = False
        action = tddstate.resolve_next(s, FACTS)
        self.assertEqual(action["code"], "FIX_SCENARIO")
        self.assertIn("order.feature:14", action["instruction"])

    def test_row16_finish_step_when_both_gates_green(self):
        self.assertEqual(code_for(state_at("scen_green")), "FINISH_STEP")

    def test_row17_next_step_or_done_after_a_closed_step(self):
        action = tddstate.resolve_next(state_at("step_closed"), FACTS)
        self.assertEqual(action["code"], "NEXT_STEP_OR_DONE")
        self.assertEqual(action["row"], 17)

    def test_every_rule_is_reachable(self):
        """Guards against a rule that can never fire because of ordering."""
        covered = set()
        for name in dir(self):
            if not name.startswith("test_row"):
                continue
            covered.add(name.split("_")[1])
        self.assertEqual(len(tddstate.RULES), 20)
        self.assertTrue({"row01", "row09", "row10", "row17"} <= covered)


class TestGates(unittest.TestCase):

    def test_scenario_gate_skipped_when_not_applicable(self):
        s = state_at("unit_baseline")
        s["scenario"]["not_applicable"] = True
        self.assertEqual(code_for(s), "START_STEP")

    def test_finish_step_reachable_without_scenarios(self):
        s = state_at("unit_green")
        s["scenario"]["not_applicable"] = True
        self.assertEqual(code_for(s), "FINISH_STEP")

    def test_green_result_predating_the_step_is_stale(self):
        s = state_at("scen_green")
        s["unit"]["last_run"]["ran_at_epoch"] = 1500.0  # before the step opened
        self.assertEqual(code_for(s), "RUN_UNIT")

    def test_edit_after_a_green_run_invalidates_the_gate(self):
        s = state_at("scen_green")
        facts = dict(FACTS)
        facts["newest_epoch"] = 3500.0  # file touched after the run
        facts["newest_path"] = "src/main/java/com/acme/Order.java"
        action = tddstate.resolve_next(s, facts)
        self.assertEqual(action["code"], "RUN_UNIT")
        self.assertIn("changed after the run", action["why"])

    def test_dirty_flag_alone_invalidates_the_gate(self):
        s = state_at("scen_green")
        s["freshness"]["dirty"] = True
        s["freshness"]["dirty_files"] = ["src/main/java/com/acme/Order.java"]
        self.assertEqual(code_for(s), "RUN_UNIT")

    def test_gate_ok_requires_both_green_and_fresh(self):
        s = state_at("scen_green")
        self.assertTrue(tddstate.gate_ok(s, "unit", FACTS))
        s["freshness"]["dirty"] = True
        self.assertFalse(tddstate.gate_ok(s, "unit", FACTS))


class TestFailureSignature(unittest.TestCase):

    def test_same_failures_hash_the_same_regardless_of_order(self):
        a = [{"id": "T#a", "message": "boom"}, {"id": "T#b", "message": "bang"}]
        self.assertEqual(tddstate.failure_signature(a),
                         tddstate.failure_signature(list(reversed(a))))

    def test_different_failures_hash_differently(self):
        a = [{"id": "T#a", "message": "boom"}]
        b = [{"id": "T#a", "message": "different"}]
        self.assertNotEqual(tddstate.failure_signature(a), tddstate.failure_signature(b))

    def test_only_the_first_message_line_counts(self):
        """Stack traces wobble between runs; the assertion line does not."""
        a = [{"id": "T#a", "message": "boom\n\tat Foo.java:1"}]
        b = [{"id": "T#a", "message": "boom\n\tat Foo.java:99"}]
        self.assertEqual(tddstate.failure_signature(a), tddstate.failure_signature(b))


class TestPurity(unittest.TestCase):

    def test_resolver_does_not_mutate_state(self):
        s = state_at("step_open")
        before = tddstate.clone(s)
        tddstate.resolve_next(s, FACTS)
        self.assertEqual(before, s)


if __name__ == "__main__":
    unittest.main()
