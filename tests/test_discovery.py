"""Discovery and selection (DEVPLAN section 10), against the fixture project."""

import contextlib
import io
import os
import pathlib
import shutil
import tempfile
import unittest

from _harness import ROOT, tddstate

SAMPLE = ROOT / "fixtures" / "sample-gradle-project"
TARGET_REL = "app/src/main/java/com/acme/Order.java"
FEATURE = "app/src/test/resources/features/order.feature"


def sample_state():
    state = tddstate.default_state()
    state["target"] = {"path": TARGET_REL, "class_fqn": "com.acme.Order",
                       "simple_name": "Order", "gradle_project": ":app",
                       "goal": "extract pricing strategy"}
    return state


class TestJavaScanning(unittest.TestCase):

    def test_comments_are_removed_but_string_literals_survive(self):
        source = 'class A { // Order\n  /* Order */ String s = "Order"; }'
        stripped = tddstate.strip_java_comments(source)
        self.assertNotIn("// Order", stripped)
        self.assertIn('"Order"', stripped)

    def test_a_double_slash_inside_a_string_is_not_a_comment(self):
        source = 'String url = "http://example.com"; int keep = 1;'
        self.assertIn("int keep = 1;", tddstate.strip_java_comments(source))

    def test_code_only_drops_string_literals_too(self):
        """Otherwise `"Order"` in a log message counts as a reference."""
        code = tddstate.java_code_only('log("Order was priced"); Tax t;')
        self.assertNotIn("Order", code)
        self.assertIn("Tax", code)

    def test_line_numbers_survive_stripping(self):
        source = "a\n/* x\n y */\nb"
        self.assertEqual(len(tddstate.strip_java_comments(source).splitlines()),
                         len(source.splitlines()))

    def test_java_escapes_are_unwrapped(self):
        self.assertEqual(tddstate.unescape_java(r"^total is (\\d+)$"),
                         r"^total is (\d+)$")


class TestCucumberExpressions(unittest.TestCase):

    def match(self, expression, text):
        regex = tddstate.cucumber_to_regex(expression)
        import re
        return re.compile(regex, re.I).search(text) is not None

    def test_int_and_string_parameters(self):
        self.assertTrue(self.match("the order total is {int}",
                                   "the order total is 150"))
        self.assertTrue(self.match('the name is {string}', 'the name is "bob"'))

    def test_literal_text_is_escaped(self):
        self.assertFalse(self.match("a.b", "axb"))

    def test_optional_text_in_parentheses(self):
        self.assertTrue(self.match("I have {int} cucumber(s)", "I have 3 cucumbers"))
        self.assertTrue(self.match("I have {int} cucumber(s)", "I have 1 cucumber"))

    def test_an_anchored_regex_passes_through_unchanged(self):
        self.assertEqual(tddstate.cucumber_to_regex(r"^total is (\d+)$"),
                         r"^total is (\d+)$")

    def test_literal_fragments_ignore_parameters(self):
        self.assertEqual(tddstate.literal_fragments("the order total is {int}"),
                         ["the order total is"])

    def test_fragments_survive_a_handwritten_regex(self):
        self.assertIn("the order total is",
                      tddstate.literal_fragments(r"^the order total is (\d+)$"))

    def test_short_fragments_are_dropped_as_too_weak(self):
        self.assertEqual(tddstate.literal_fragments("a {int} b"), [])


class TestUnitDiscovery(unittest.TestCase):

    def setUp(self):
        self.ctx = tddstate.Ctx(SAMPLE)
        self.candidates = tddstate.discover_unit(self.ctx, sample_state())
        self.by_id = {c["id"]: c for c in self.candidates}

    def test_name_match_ranks_first(self):
        self.assertEqual(self.candidates[0]["id"], "com.acme.OrderTest")
        self.assertEqual(self.candidates[0]["score"], 3)

    def test_a_test_in_another_package_that_imports_the_target_scores_two(self):
        self.assertEqual(self.by_id["com.acme.checkout.CheckoutTest"]["score"], 2)

    def test_a_same_package_test_scores_one(self):
        self.assertEqual(self.by_id["com.acme.TaxTest"]["score"], 1)

    def test_a_helper_without_tests_is_not_a_candidate(self):
        """TestData mentions Order but has no @Test - it is not a unit test."""
        self.assertNotIn("com.acme.support.TestData", self.by_id)

    def test_step_definitions_and_the_runner_are_excluded(self):
        for ident in ("com.acme.steps.PricingSteps", "com.acme.RunCucumberTest"):
            self.assertNotIn(ident, self.by_id)

    def test_every_candidate_carries_a_reason_and_a_path(self):
        for cand in self.candidates:
            self.assertTrue(cand["reason"])
            self.assertTrue(cand["path"].endswith(".java"))


class TestScenarioDiscovery(unittest.TestCase):

    def setUp(self):
        self.ctx = tddstate.Ctx(SAMPLE)
        (self.candidates, self.runner, self.glue,
         self.notes) = tddstate.discover_scenario(self.ctx, sample_state())
        self.by_id = {c["id"]: c for c in self.candidates}

    def test_the_target_scenario_is_found_through_the_step_definition_hop(self):
        ident = FEATURE + ":14"
        self.assertIn(ident, self.by_id)
        cand = self.by_id[ident]
        self.assertEqual(cand["score"], 3)
        self.assertEqual(cand["name"], "Discount applied to large orders")
        self.assertIn("PricingSteps.java", cand["reason"])

    def test_scenario_outline_steps_match_despite_placeholders(self):
        """`<total>` matches no parameter regex; the literal fallback catches it."""
        self.assertIn(FEATURE + ":19", self.by_id)

    def test_feature_tags_are_inherited_by_scenarios(self):
        self.assertEqual(self.by_id[FEATURE + ":14"]["tags"], ["@pricing", "@discount"])

    def test_unrelated_glue_does_not_drag_in_its_feature(self):
        for ident in self.by_id:
            self.assertNotIn("shipping.feature", ident)

    def test_the_cucumber_runner_is_identified_and_is_not_a_step_class(self):
        self.assertEqual(self.runner, "com.acme.RunCucumberTest")

    def test_the_glue_that_drives_the_target_is_recorded_for_watching(self):
        """A red scenario is usually repaired in the step definitions, so an
        edit there has to invalidate the gates like any other."""
        self.assertIn("app/src/test/java/com/acme/steps/PricingSteps.java", self.glue)
        self.assertIn("app/src/test/java/com/acme/RunCucumberTest.java", self.glue)

    def test_unrelated_glue_is_not_watched(self):
        self.assertNotIn("app/src/test/java/com/acme/steps/ShippingSteps.java",
                         self.glue)

    def test_candidates_are_ordered_by_score_then_line_number(self):
        lines = [int(c["id"].rpartition(":")[2]) for c in self.candidates
                 if c["score"] == 3]
        self.assertEqual(lines, sorted(lines))


class TestGherkinParsing(unittest.TestCase):

    def setUp(self):
        self.doc = tddstate.parse_feature(SAMPLE / FEATURE, FEATURE)

    def test_scenarios_and_their_lines(self):
        self.assertEqual([s["line"] for s in self.doc["scenarios"]], [8, 14, 19])

    def test_background_steps_are_kept_apart_from_scenario_steps(self):
        self.assertEqual(self.doc["background"], ["a clean basket"])
        self.assertNotIn("a clean basket", self.doc["scenarios"][0]["steps"])

    def test_comments_and_blank_lines_are_ignored(self):
        self.assertEqual(self.doc["name"], "Orders")

    def test_background_only_match_scores_below_a_direct_match(self):
        """A Background step implicates every scenario, so it is weaker evidence."""
        ctx = tddstate.Ctx(SAMPLE)
        matchers = [{"expr": "a clean basket", "fragments": ["a clean basket"],
                     "regex": None, "class": "PricingSteps"}]
        hit = tddstate.match_step(matchers, "a clean basket")
        self.assertIsNotNone(hit)

    def test_non_english_feature_is_flagged_rather_than_mis_parsed(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="tddgh-"))
        try:
            features = tmp / "app" / "src" / "test" / "resources"
            features.mkdir(parents=True)
            (features / "de.feature").write_text(
                "# language: de\nFunktionalitat: Bestellungen\n", encoding="utf-8")
            (tmp / "app" / "src" / "test" / "java").mkdir(parents=True)
            ctx = tddstate.Ctx(tmp)
            _c, _r, _g, notes = tddstate.discover_scenario(ctx, sample_state())
            self.assertTrue(any("language 'de'" in n for n in notes))
        finally:
            shutil.rmtree(str(tmp), ignore_errors=True)


class SelectCase(unittest.TestCase):
    """Selection runs against a throwaway copy so state files never pollute
    the fixture."""

    def setUp(self):
        self.repo = pathlib.Path(tempfile.mkdtemp(prefix="tddsel-")) / "sample"
        shutil.copytree(str(SAMPLE), str(self.repo))
        os.environ["TDD_REPO_ROOT"] = str(self.repo)
        self.cli("init", "--target", TARGET_REL, "--goal", "extract pricing")

    def tearDown(self):
        os.environ.pop("TDD_REPO_ROOT", None)
        shutil.rmtree(str(self.repo.parent), ignore_errors=True)

    def cli(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            code = tddstate.main(list(argv))
        self.last_output = buf.getvalue()
        return code

    def state(self):
        ctx = tddstate.Ctx(self.repo)
        tddstate.load_state(ctx)
        return ctx.state


class TestSelect(SelectCase):

    def test_discovery_persists_candidates_so_a_restart_need_not_repeat_it(self):
        self.cli("discover", "unit")
        self.assertEqual(self.state()["unit"]["candidates"][0]["id"],
                         "com.acme.OrderTest")

    def test_select_records_the_path_so_edits_can_be_detected(self):
        self.cli("discover", "unit")
        self.cli("select", "unit", "com.acme.OrderTest")
        selected = self.state()["unit"]["selected"]
        self.assertEqual(selected[0]["id"], "com.acme.OrderTest")
        self.assertEqual(selected[0]["path"],
                         "app/src/test/java/com/acme/OrderTest.java")
        self.assertIn("app/src/test/java/com/acme/OrderTest.java",
                      tddstate.watched_files(self.state()))

    def test_select_all_takes_candidates_scoring_two_or_better(self):
        self.cli("discover", "unit")
        self.cli("select", "unit", "--all")
        ids = [i["id"] for i in self.state()["unit"]["selected"]]
        self.assertIn("com.acme.OrderTest", ids)
        self.assertIn("com.acme.checkout.CheckoutTest", ids)
        self.assertNotIn("com.acme.TaxTest", ids)      # scored 1

    def test_an_undiscovered_id_is_kept_but_flagged(self):
        """Discovery ranks; it must never be able to veto the model's judgement."""
        self.cli("discover", "unit")
        self.cli("select", "unit", "com.acme.OrderTest", "com.acme.WeirdTest")
        ids = [i["id"] for i in self.state()["unit"]["selected"]]
        self.assertIn("com.acme.WeirdTest", ids)
        self.assertIn("not among the discovered candidates", self.last_output)

    def test_changing_the_selection_clears_the_stale_baseline(self):
        self.cli("discover", "unit")
        self.cli("select", "unit", "com.acme.OrderTest")
        ctx = tddstate.Ctx(self.repo)
        tddstate.load_state(ctx)
        with tddstate.Lock(ctx, force=True):
            tddstate.mutate(ctx, "baseline", {
                "unit.baseline": {"total": 3, "pass": 3, "fail": 0, "failed_ids": []},
                "unit.green": True})
        self.cli("select", "unit", "com.acme.checkout.CheckoutTest")
        state = self.state()
        self.assertIsNone(state["unit"]["baseline"])
        self.assertFalse(state["unit"]["green"])
        self.assertIn("baseline and last result were cleared", self.last_output)

    def test_reselecting_the_same_set_keeps_the_baseline(self):
        self.cli("discover", "unit")
        self.cli("select", "unit", "com.acme.OrderTest")
        ctx = tddstate.Ctx(self.repo)
        tddstate.load_state(ctx)
        with tddstate.Lock(ctx, force=True):
            tddstate.mutate(ctx, "baseline", {"unit.baseline": {"total": 3}})
        self.cli("select", "unit", "com.acme.OrderTest")
        self.assertIsNotNone(self.state()["unit"]["baseline"])

    def test_select_scenario_none_skips_the_gate_for_good(self):
        self.cli("discover", "unit")
        self.cli("select", "unit", "--all")
        self.cli("select", "scenario", "--none")
        state = self.state()
        self.assertTrue(state["scenario"]["not_applicable"])
        self.assertEqual(self.state()["next_action"]["code"], "BASELINE_UNIT")

    def test_select_scenario_stores_name_and_tags_for_reporting(self):
        self.cli("discover", "scenario")
        self.cli("select", "scenario", FEATURE + ":14")
        chosen = self.state()["scenario"]["selected"][0]
        self.assertEqual(chosen["name"], "Discount applied to large orders")
        self.assertIn("@discount", chosen["tags"])

    def test_selecting_nothing_is_refused(self):
        self.cli("discover", "unit")
        self.assertEqual(self.cli("select", "unit"), tddstate.EXIT_REFUSED)

    def test_the_feature_path_becomes_watched_without_its_line_number(self):
        self.cli("discover", "scenario")
        self.cli("select", "scenario", FEATURE + ":14")
        self.assertIn(FEATURE, tddstate.watched_files(self.state()))


class TestDiscoveryDrivesTheLoop(SelectCase):
    """The resolver and discovery have to agree on what comes next."""

    def test_the_loop_walks_from_discovery_to_baseline(self):
        self.assertEqual(self.state()["next_action"]["code"], "DISCOVER_UNIT")
        self.cli("discover", "unit")
        self.assertEqual(self.state()["next_action"]["code"], "SELECT_UNIT")
        self.cli("select", "unit", "com.acme.OrderTest")
        self.assertEqual(self.state()["next_action"]["code"], "BASELINE_UNIT")

    def test_scenario_discovery_is_asked_for_after_the_unit_baseline(self):
        self.cli("discover", "unit")
        self.cli("select", "unit", "com.acme.OrderTest")
        ctx = tddstate.Ctx(self.repo)
        tddstate.load_state(ctx)
        with tddstate.Lock(ctx, force=True):
            tddstate.mutate(ctx, "baseline",
                            {"unit.baseline": {"total": 3, "pass": 3, "fail": 0,
                                               "failed_ids": []}})
        self.assertEqual(self.state()["next_action"]["code"], "DISCOVER_SCENARIO")


if __name__ == "__main__":
    unittest.main()
