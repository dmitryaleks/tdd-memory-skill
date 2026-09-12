"""Gradle runner and report parsers (DEVPLAN section 9).

Gradle is never actually invoked: run_process is replaced with a stub that
writes the fixture reports, so the whole pipeline is exercised offline.
"""

import contextlib
import io
import json
import os
import pathlib
import shutil
import tempfile
import unittest

from _harness import ROOT, GradleRepoCase, tddstate

REPORTS = ROOT / "fixtures" / "reports"
ORDER_XML = (REPORTS / "TEST-com.acme.OrderTest.xml").read_text(encoding="utf-8")
SUITES_XML = (REPORTS / "TEST-suites-wrapper.xml").read_text(encoding="utf-8")
NDJSON = (REPORTS / "cucumber.ndjson").read_text(encoding="utf-8")
COMPILE_ERROR = (REPORTS / "gradle-compile-error.txt").read_text(encoding="utf-8")
NO_TESTS = (REPORTS / "gradle-no-tests-found.txt").read_text(encoding="utf-8")

GREEN_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.acme.OrderTest" tests="2" skipped="0" failures="0" errors="0">
  <testcase name="totalsLines()" classname="com.acme.OrderTest" time="0.01"/>
  <testcase name="appliesDiscount()" classname="com.acme.OrderTest" time="0.01"/>
</testsuite>
"""


def write_xml(tmp, name, content):
    path = pathlib.Path(tmp) / name
    path.write_text(content, encoding="utf-8")
    return path


class TestJUnitXmlParsing(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tddxml-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_counts_failures_passes_and_skips(self):
        report = tddstate.parse_junit_xml([write_xml(self.tmp, "a.xml", ORDER_XML)])
        self.assertEqual((report["total"], report["pass"], report["fail"],
                          report["skipped"]), (3, 1, 1, 1))

    def test_failure_id_and_first_message_line_only(self):
        report = tddstate.parse_junit_xml([write_xml(self.tmp, "a.xml", ORDER_XML)])
        failure = report["failed"][0]
        self.assertEqual(failure["id"], "com.acme.OrderTest#appliesDiscount()")
        self.assertEqual(failure["message"], "expected: <10> but was: <12>")
        self.assertNotIn("at com.acme", failure["message"])

    def test_testsuites_wrapper_and_errors_count_as_failures(self):
        report = tddstate.parse_junit_xml([write_xml(self.tmp, "b.xml", SUITES_XML)])
        self.assertEqual((report["total"], report["pass"], report["fail"]), (3, 2, 1))
        self.assertEqual(report["failed"][0]["id"], "com.acme.PricingTest#explodes()")

    def test_malformed_xml_is_skipped_not_fatal(self):
        bad = write_xml(self.tmp, "bad.xml", "<testsuite><testcase")
        good = write_xml(self.tmp, "good.xml", GREEN_XML)
        report = tddstate.parse_junit_xml([bad, good])
        self.assertEqual(report["total"], 2)

    def test_no_files_yields_zeros(self):
        self.assertEqual(tddstate.parse_junit_xml([])["total"], 0)


class TestCucumberNdjson(unittest.TestCase):

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="tddnd-"))
        self.path = self.tmp / "cucumber.ndjson"
        self.path.write_text(NDJSON, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_scenarios_resolve_to_uri_and_line(self):
        results = tddstate.parse_cucumber_ndjson(self.path)
        ids = sorted(r["id"] for r in results)
        self.assertEqual(ids, [
            "src/test/resources/features/order.feature:14",
            "src/test/resources/features/order.feature:20",
            "src/test/resources/features/order.feature:33",
        ])

    def test_outline_uses_the_example_row_line_not_the_scenario_line(self):
        results = {r["id"]: r for r in tddstate.parse_cucumber_ndjson(self.path)}
        self.assertIn("src/test/resources/features/order.feature:33", results)
        self.assertNotIn("src/test/resources/features/order.feature:26", results)

    def test_worst_step_status_wins_for_the_scenario(self):
        results = {r["name"]: r["status"] for r in tddstate.parse_cucumber_ndjson(self.path)}
        self.assertEqual(results["No discount for small orders"], "FAILED")
        self.assertEqual(results["Discount applied to large orders"], "PASSED")

    def test_classpath_uris_are_mapped_onto_repo_relative_paths(self):
        """Cucumber reports the path it loaded the feature by, which is not
        the path discovery found it at."""
        known = {"app/src/test/resources/features/order.feature"}
        for raw in ("classpath:features/order.feature",
                    "file:///work/app/src/test/resources/features/order.feature",
                    "features/order.feature"):
            self.assertEqual(
                tddstate.normalise_feature_uri(raw, known),
                "app/src/test/resources/features/order.feature", raw)

    def test_an_unknown_uri_is_left_recognisable(self):
        self.assertEqual(
            tddstate.normalise_feature_uri("classpath:other/x.feature", set()),
            "other/x.feature")

    def test_missing_file_returns_none_so_callers_fall_back(self):
        self.assertIsNone(tddstate.parse_cucumber_ndjson(self.tmp / "nope.ndjson"))


class TestClassification(unittest.TestCase):
    """A build failure is not a test failure - DEVPLAN section 9.4."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tddcls-")
        self.xml = [write_xml(self.tmp, "a.xml", ORDER_XML)]
        self.report = tddstate.parse_junit_xml(self.xml)
        self.green = tddstate.parse_junit_xml(
            [write_xml(self.tmp, "g.xml", GREEN_XML)])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_timeout(self):
        self.assertEqual(
            tddstate.classify_outcome(True, -1, [], {"total": 0, "fail": 0}, ""),
            "timeout")

    def test_compile_error_is_build_failed(self):
        self.assertEqual(
            tddstate.classify_outcome(False, 1, [], {"total": 0, "fail": 0},
                                      COMPILE_ERROR),
            "build_failed")

    def test_no_tests_found_is_no_results_not_build_failed(self):
        """Gradle exits non-zero and says 'Build failed', but this is a filter
        problem - calling it a build failure sends the model to fix code that
        is not broken."""
        self.assertEqual(
            tddstate.classify_outcome(False, 1, [], {"total": 0, "fail": 0}, NO_TESTS),
            "no_results")

    def test_silent_success_with_no_reports_is_never_a_pass(self):
        self.assertEqual(
            tddstate.classify_outcome(False, 0, [], {"total": 0, "fail": 0}, ""),
            "no_results")

    def test_failures_and_passes(self):
        self.assertEqual(
            tddstate.classify_outcome(False, 1, self.xml, self.report, ""),
            "tests_failed")
        self.assertEqual(
            tddstate.classify_outcome(False, 0, self.xml, self.green, ""), "passed")


class TestBuildErrorExtraction(unittest.TestCase):

    def test_keeps_javac_errors_and_gradle_explanation(self):
        lines = tddstate.extract_build_error(COMPILE_ERROR)
        blob = "\n".join(lines)
        self.assertIn("cannot find symbol", blob)
        self.assertIn("incompatible types", blob)
        self.assertIn("Execution failed for task ':app:compileJava'.", blob)

    def test_drops_the_boilerplate_try_section(self):
        blob = "\n".join(tddstate.extract_build_error(COMPILE_ERROR))
        self.assertNotIn("--stacktrace", blob)
        self.assertNotIn("help.gradle.org", blob)

    def test_is_capped(self):
        noise = "\n".join("x.java:%d: error: broken" % i for i in range(500))
        self.assertLessEqual(len(tddstate.extract_build_error(noise)),
                             tddstate.MAX_BUILD_ERROR_LINES)


class TestGreenAgainstBaseline(unittest.TestCase):

    def state_with_baseline(self, failed_ids):
        state = tddstate.default_state()
        state["unit"]["baseline"] = {"failed_ids": failed_ids}
        return state

    def test_pre_existing_failure_does_not_break_the_gate(self):
        state = self.state_with_baseline(["com.acme.OrderTest#alreadyRed"])
        report = {"failed": [{"id": "com.acme.OrderTest#alreadyRed"}]}
        green, regressions, pre = tddstate.compute_green(
            state, "unit", report, "tests_failed")
        self.assertTrue(green)
        self.assertEqual(regressions, [])
        self.assertEqual(pre, ["com.acme.OrderTest#alreadyRed"])

    def test_a_new_failure_breaks_the_gate(self):
        state = self.state_with_baseline(["com.acme.OrderTest#alreadyRed"])
        report = {"failed": [{"id": "com.acme.OrderTest#alreadyRed"},
                             {"id": "com.acme.OrderTest#justBroke"}]}
        green, regressions, _ = tddstate.compute_green(
            state, "unit", report, "tests_failed")
        self.assertFalse(green)
        self.assertEqual(regressions, ["com.acme.OrderTest#justBroke"])

    def test_baseline_run_adopts_whatever_is_red(self):
        state = tddstate.default_state()
        report = {"failed": [{"id": "com.acme.OrderTest#alreadyRed"}]}
        green, regressions, pre = tddstate.compute_green(
            state, "unit", report, "tests_failed", baseline=True)
        self.assertTrue(green)
        self.assertEqual(pre, ["com.acme.OrderTest#alreadyRed"])

    def test_a_build_failure_is_never_green(self):
        state = self.state_with_baseline([])
        green, _, _ = tddstate.compute_green(state, "unit", {"failed": []},
                                             "build_failed")
        self.assertFalse(green)


class TestSummaryBudget(unittest.TestCase):
    """Keeping build output out of the context window is the whole point."""

    def run_record(self, failures, **over):
        run = {"outcome": "tests_failed", "cmd": "/long/path/to/gradlew :app:test",
               "duration_s": 12.3, "total": 60, "pass": 60 - len(failures),
               "fail": len(failures), "skipped": 0, "green": False,
               "failed": failures, "regressions": [f["id"] for f in failures],
               "pre_existing": [], "log": "logs/x.log", "build_error": []}
        run.update(over)
        return run

    def test_forty_failures_fit_in_twenty_lines(self):
        failures = [{"id": "com.acme.T#m%d" % i, "message": "boom " * 40}
                    for i in range(40)]
        text = tddstate.render_run_summary("unit", self.run_record(failures), [])
        self.assertLessEqual(len(text.splitlines()), 20)
        self.assertIn("further failure(s) not shown", text)

    def test_budget_holds_even_with_warnings_and_pre_existing(self):
        failures = [{"id": "com.acme.T#m%d" % i, "message": "boom"} for i in range(40)]
        run = self.run_record(failures, pre_existing=["com.acme.T#old"])
        text = tddstate.render_run_summary(
            "scenario", run, ["a warning that is quite long " * 3])
        self.assertLessEqual(len(text.splitlines()), 20)

    def test_regressions_are_listed_before_pre_existing_failures(self):
        failures = [{"id": "old#a", "message": "x"}, {"id": "new#b", "message": "y"}]
        run = self.run_record(failures, regressions=["new#b"], pre_existing=["old#a"])
        lines = tddstate.render_run_summary("unit", run, []).splitlines()
        fails = [l for l in lines if l.startswith("FAIL")]
        self.assertIn("new#b", fails[0])

    def test_build_failure_shows_the_error_and_says_tests_never_ran(self):
        run = self.run_record([], outcome="build_failed",
                              build_error=tddstate.extract_build_error(COMPILE_ERROR))
        text = tddstate.render_run_summary("unit", run, [])
        self.assertIn("BUILD FAILED", text)
        self.assertIn("the tests never ran", text)
        self.assertIn("cannot find symbol", text)

    def test_no_results_is_spelled_out_as_not_a_pass(self):
        run = self.run_record([], outcome="no_results", fail=0, total=0)
        self.assertIn("NOT a pass", tddstate.render_run_summary("unit", run, []))

    def test_summary_is_ascii(self):
        failures = [{"id": "com.acme.T#m%d" % i, "message": "boom " * 40}
                    for i in range(40)]
        tddstate.render_run_summary("unit", self.run_record(failures),
                                    ["warn"]).encode("ascii")


class TestArgvConstruction(GradleRepoCase):

    def test_unit_argv_lists_each_selected_test(self):
        self.prepare_unit()
        state = self.ctx().state
        argv = tddstate.build_argv(self.ctx(), state, "unit", "test", ":app",
                                   self.repo / "app")
        self.assertIn(":app:test", argv)
        self.assertIn("--tests", argv)
        self.assertIn("com.acme.OrderTest", argv)
        self.assertIn("--console=plain", argv)

    def test_method_ids_use_gradle_dot_syntax(self):
        self.assertEqual(tddstate.gradle_test_pattern("a.b.CTest#m"), "a.b.CTest.m")

    def test_scenario_argv_filters_by_feature_and_asks_for_messages(self):
        self.prepare_unit()
        self.select(**{
            "scenario.runner_class": "com.acme.RunCucumberTest",
            "scenario.selected": [
                {"id": "app/src/test/resources/features/order.feature:14",
                 "name": "Discount applied", "tags": ["@pricing"]}]})
        argv = tddstate.build_argv(self.ctx(), self.ctx().state, "scenario", "test",
                                   ":app", self.repo / "app")
        joined = " ".join(argv)
        self.assertIn("--tests com.acme.RunCucumberTest", joined)
        self.assertIn("-Dcucumber.features=app/src/test/resources/features/"
                      "order.feature:14", joined)
        self.assertIn("-Dcucumber.plugin=message:build/tdd/cucumber.ndjson", joined)

    def test_tag_filter_mode(self):
        self.prepare_unit()
        self.select(**{"scenario.filter_mode": "tags", "scenario.selected": [
            {"id": "f:1", "tags": ["@pricing"]}, {"id": "f:2", "tags": ["@vat"]}]})
        argv = tddstate.build_argv(self.ctx(), self.ctx().state, "scenario", "test",
                                   ":app", self.repo / "app")
        self.assertIn("-Dcucumber.filter.tags=@pricing or @vat", argv)

    def test_paths_for_root_and_nested_projects(self):
        ctx = self.ctx()
        self.assertEqual(tddstate.task_path(":app", "test"), ":app:test")
        self.assertEqual(tddstate.task_path("", "test"), "test")
        self.assertEqual(tddstate.module_dir(ctx, ":services:billing"),
                         self.repo / "services" / "billing")
        self.assertEqual(tddstate.results_dir(ctx, ":app", "test"),
                         self.repo / "app" / "build" / "test-results" / "test")


class TestDoRun(GradleRepoCase):

    def test_green_run_updates_the_gate_and_clears_dirtiness(self):
        self.prepare_unit()
        self.stub_gradle(xml=[("TEST-a.xml", GREEN_XML)])
        self.assertEqual(self.cli("baseline", "unit"), 0)
        state = self.ctx().state
        self.assertEqual(state["unit"]["last_run"]["outcome"], "passed")
        self.assertTrue(state["unit"]["green"])
        self.assertFalse(state["freshness"]["dirty"])
        self.assertEqual(state["unit"]["baseline"]["total"], 2)
        self.assertEqual(state["unit"]["selected"][0]["status"], "pass")

    def test_full_output_goes_to_disk_not_to_stdout(self):
        self.prepare_unit()
        noise = "\n".join("gradle chatter line %d" % i for i in range(3000))
        self.stub_gradle(xml=[("TEST-a.xml", ORDER_XML)], output=noise, exit_code=1)
        self.cli("baseline", "unit")
        self.assertNotIn("gradle chatter line 2999", self.last_output)
        log = self.ctx().state["unit"]["last_run"]["log"]
        body = (self.repo / ".claude" / "tdd" / log).read_text(encoding="utf-8")
        self.assertIn("gradle chatter line 2999", body)
        self.assertIn("# cmd      :", body)

    def test_identical_failures_raise_the_streak_until_next_escalates(self):
        self.prepare_unit()
        self.stub_gradle(xml=[("TEST-a.xml", GREEN_XML)])
        self.cli("baseline", "unit")
        self.select(**{"scenario.not_applicable": True,
                       "step": {"id": "s01", "n": 1, "title": "extract calc",
                                "started_at": tddstate.now_iso(),
                                "started_at_epoch": tddstate.time.time()}})
        self.stub_gradle(xml=[("TEST-a.xml", ORDER_XML)], exit_code=1)
        for expected in (1, 2, 3):
            self.cli("run", "unit")
            self.assertEqual(
                self.ctx().state["attempts"]["same_failure_streak"], expected)
        self.assertEqual(self.next_action(), "ESCALATE")
        self.cli("next")
        self.assertIn("STOP editing", self.last_output)

    def test_a_different_failure_resets_the_streak(self):
        self.prepare_unit()
        self.stub_gradle(xml=[("TEST-a.xml", GREEN_XML)])
        self.cli("baseline", "unit")
        self.stub_gradle(xml=[("TEST-a.xml", ORDER_XML)], exit_code=1)
        self.cli("run", "unit")
        self.stub_gradle(xml=[("TEST-a.xml", SUITES_XML)], exit_code=1)
        self.cli("run", "unit")
        self.assertEqual(self.ctx().state["attempts"]["same_failure_streak"], 1)

    def test_build_failure_is_reported_as_such(self):
        self.prepare_unit()
        self.stub_gradle(output=COMPILE_ERROR, exit_code=1)
        self.cli("baseline", "unit")
        run = self.ctx().state["unit"]["last_run"]
        self.assertEqual(run["outcome"], "build_failed")
        self.assertFalse(run["green"])
        self.assertTrue(run["build_error"])
        self.assertIn("cannot find symbol", self.last_output)

    def test_stale_reports_from_an_up_to_date_task_are_not_a_pass(self):
        """Gradle skipping the task must never read as green - DEVPLAN 9.2."""
        self.prepare_unit()
        rdir = self.repo / "app" / "build" / "test-results" / "test"
        rdir.mkdir(parents=True)
        (rdir / "TEST-old.xml").write_text(GREEN_XML, encoding="utf-8")
        self.stub_gradle(output="> Task :app:test UP-TO-DATE\n", exit_code=0)
        self.cli("baseline", "unit")
        run = self.ctx().state["unit"]["last_run"]
        self.assertEqual(run["outcome"], "no_results")
        self.assertFalse(run["green"])

    def test_timeout_is_recorded_without_a_verdict(self):
        self.prepare_unit()
        self.stub_gradle(timed_out=True, exit_code=-1)
        self.cli("baseline", "unit")
        self.assertEqual(self.ctx().state["unit"]["last_run"]["outcome"], "timeout")
        self.assertFalse(self.ctx().state["unit"]["green"])

    def test_run_timestamp_is_the_start_so_edits_during_a_run_invalidate_it(self):
        self.prepare_unit()
        self.stub_gradle(xml=[("TEST-a.xml", GREEN_XML)])
        self.cli("baseline", "unit")
        run = self.ctx().state["unit"]["last_run"]
        self.assertLess(run["ran_at_epoch"], tddstate.time.time())
        self.assertAlmostEqual(run["ran_at_epoch"], tddstate.time.time(), delta=10)

    def test_logs_are_pruned(self):
        self.prepare_unit()
        logs = self.repo / ".claude" / "tdd" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        for i in range(60):
            (logs / ("old-%02d-unit.log" % i)).write_text("x", encoding="utf-8")
        self.stub_gradle(xml=[("TEST-a.xml", GREEN_XML)])
        self.cli("baseline", "unit")
        self.assertLessEqual(len(list(logs.glob("*.log"))), tddstate.LOGS_KEPT)


class TestScenarioResults(GradleRepoCase):

    def prepare_scenarios(self):
        self.prepare_unit()
        self.select(**{
            "unit.baseline": {"ran_at": tddstate.now_iso(), "total": 2, "pass": 2,
                              "fail": 0, "failed_ids": []},
            "unit.green": True,
            "unit.last_run": {"ran_at": tddstate.now_iso(),
                              "ran_at_epoch": tddstate.time.time(),
                              "outcome": "passed", "total": 2, "pass": 2, "fail": 0,
                              "skipped": 0, "failed": [], "green": True},
            "scenario.runner_class": "com.acme.RunCucumberTest",
            "scenario.selected": [
                {"id": "src/test/resources/features/order.feature:14",
                 "name": "Discount applied to large orders", "status": "unknown"},
                {"id": "src/test/resources/features/order.feature:20",
                 "name": "No discount for small orders", "status": "unknown"}]})

    def test_classpath_ndjson_still_matches_the_selection(self):
        """The realistic case: @SelectClasspathResource feature uris."""
        self.prepare_scenarios()
        classpath_ndjson = NDJSON.replace(
            '"src/test/resources/features/order.feature"',
            '"classpath:features/order.feature"')
        self.stub_gradle(xml=[("TEST-c.xml", GREEN_XML)],
                         ndjson=classpath_ndjson, exit_code=1)
        self.cli("run", "scenario")
        run = self.ctx().state["scenario"]["last_run"]
        self.assertEqual([f["id"] for f in run["failed"]],
                         ["src/test/resources/features/order.feature:20"])

    def test_ndjson_gives_precise_scenario_ids(self):
        self.prepare_scenarios()
        self.stub_gradle(xml=[("TEST-c.xml", GREEN_XML)], ndjson=NDJSON, exit_code=1)
        self.cli("run", "scenario")
        run = self.ctx().state["scenario"]["last_run"]
        self.assertEqual(run["outcome"], "tests_failed")
        self.assertEqual([f["id"] for f in run["failed"]],
                         ["src/test/resources/features/order.feature:20"])

    def test_unfiltered_run_is_filtered_afterwards_with_a_warning(self):
        """When -Dcucumber.* does not reach the test JVM the whole task runs."""
        self.prepare_scenarios()
        self.stub_gradle(xml=[("TEST-c.xml", GREEN_XML)], ndjson=NDJSON, exit_code=1)
        self.cli("run", "scenario")
        run = self.ctx().state["scenario"]["last_run"]
        self.assertEqual(run["total"], 2)          # 3 ran, 2 selected
        self.assertTrue(any("not reaching the test JVM" in w
                            for w in run["warnings"]))

    def test_without_ndjson_ids_fall_back_to_names_with_a_warning(self):
        self.prepare_scenarios()
        cucumber_xml = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="Cucumber" tests="2" failures="1" errors="0" skipped="0">
  <testcase name="Discount applied to large orders" classname="Orders"/>
  <testcase name="No discount for small orders" classname="Orders">
    <failure message="expected 10 but was 12"/>
  </testcase>
</testsuite>
"""
        self.stub_gradle(xml=[("TEST-c.xml", cucumber_xml)], exit_code=1)
        self.cli("run", "scenario")
        run = self.ctx().state["scenario"]["last_run"]
        self.assertEqual(run["failed"][0]["id"],
                         "src/test/resources/features/order.feature:20")
        self.assertTrue(any("no cucumber message output" in w for w in run["warnings"]))


class TestGates(GradleRepoCase):

    def test_scenario_run_is_refused_while_unit_is_red(self):
        self.prepare_unit()
        self.stub_gradle(xml=[("TEST-a.xml", ORDER_XML)], exit_code=1)
        self.cli("baseline", "unit")
        self.select(**{"scenario.selected": [{"id": "f.feature:1", "name": "x"}],
                       "unit.green": False})
        code = self.cli("run", "scenario")
        self.assertEqual(code, tddstate.EXIT_REFUSED)

    def test_force_overrides_the_gate_and_is_journaled(self):
        self.prepare_unit()
        self.stub_gradle(xml=[("TEST-a.xml", ORDER_XML)], exit_code=1)
        self.cli("baseline", "unit")
        self.select(**{"scenario.selected": [{"id": "f.feature:1", "name": "x"}],
                       "unit.green": False})
        self.stub_gradle(xml=[("TEST-c.xml", GREEN_XML)])
        self.assertEqual(self.cli("run", "scenario", "--force"), 0)
        events = [e["event"] for e in tddstate.read_journal(self.ctx())]
        self.assertIn("override", events)

    def test_baseline_is_refused_once_an_increment_is_open(self):
        self.prepare_unit()
        self.select(**{"step": {"id": "s01", "n": 1, "title": "x",
                                "started_at": tddstate.now_iso(),
                                "started_at_epoch": tddstate.time.time()}})
        self.assertEqual(self.cli("baseline", "unit"), tddstate.EXIT_REFUSED)

    def test_running_with_nothing_selected_is_refused(self):
        self.cli("init", "--target", self.target)
        self.assertEqual(self.cli("run", "unit"), tddstate.EXIT_REFUSED)

    def test_scenario_run_refused_when_marked_not_applicable(self):
        self.prepare_unit()
        self.select(**{"scenario.not_applicable": True})
        self.assertEqual(self.cli("run", "scenario"), tddstate.EXIT_REFUSED)


class TestAwaitEdit(GradleRepoCase):
    """Row 11b: do not let the model close an increment it never edited."""

    def open_step(self):
        self.prepare_unit()
        self.stub_gradle(xml=[("TEST-a.xml", GREEN_XML)])
        self.cli("baseline", "unit")
        self.select(**{"scenario.not_applicable": True,
                       "step": {"id": "s01", "n": 1, "title": "extract calc",
                                "started_at": tddstate.now_iso(),
                                "started_at_epoch": tddstate.time.time()}})

    def test_freshly_opened_increment_asks_for_the_edit(self):
        self.open_step()
        self.assertEqual(self.next_action(), "AWAIT_EDIT")
        self.cli("next")
        self.assertIn("Make the code change", self.last_output)

    def test_it_does_not_mask_a_red_gate(self):
        self.open_step()
        self.stub_gradle(xml=[("TEST-a.xml", ORDER_XML)], exit_code=1)
        self.cli("run", "unit")
        self.assertEqual(self.next_action(), "FIX_UNIT")

    def test_it_stops_once_the_target_is_edited(self):
        self.open_step()
        path = self.repo / self.target
        os.utime(str(path), (tddstate.time.time() + 60, tddstate.time.time() + 60))
        self.assertEqual(self.next_action(), "RUN_UNIT")


class TestManualRecord(GradleRepoCase):

    def test_manual_verdicts_are_marked_as_such(self):
        self.prepare_unit()
        self.assertEqual(self.cli("record", "unit", "--result", "pass"), 0)
        run = self.ctx().state["unit"]["last_run"]
        self.assertTrue(run["manual"])
        self.assertTrue(self.ctx().state["unit"]["green"])
        events = [e for e in tddstate.read_journal(self.ctx())
                  if e["event"] == "record"]
        self.assertTrue(events[0]["data"]["manual"])


if __name__ == "__main__":
    unittest.main()
