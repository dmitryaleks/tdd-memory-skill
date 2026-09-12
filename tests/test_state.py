"""State I/O, journal, repair, locking and the rendered output budgets."""

import json
import os
import pathlib
import shutil
import tempfile
import unittest

from _harness import state_at, tddstate


class RepoCase(unittest.TestCase):
    """A throwaway Java repo with the tracker installed into it."""

    def setUp(self):
        self.repo = pathlib.Path(tempfile.mkdtemp(prefix="tddtest-"))
        (self.repo / "app" / "src" / "main" / "java" / "com" / "acme").mkdir(parents=True)
        (self.repo / "app" / "build.gradle").write_text("plugins { id 'java' }\n",
                                                        encoding="utf-8")
        self.target_rel = "app/src/main/java/com/acme/Order.java"
        (self.repo / self.target_rel).write_text(
            "package com.acme;\n\npublic class Order {}\n", encoding="utf-8")
        os.environ["TDD_REPO_ROOT"] = str(self.repo)

    def tearDown(self):
        os.environ.pop("TDD_REPO_ROOT", None)
        shutil.rmtree(str(self.repo), ignore_errors=True)

    def cli(self, *argv):
        """Run the CLI, swallowing its stdout so test output stays readable."""
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            return tddstate.main(list(argv))

    def ctx(self):
        c = tddstate.Ctx(self.repo)
        tddstate.load_state(c)
        return c


class TestPatching(unittest.TestCase):

    def test_dotted_paths_create_intermediate_dicts(self):
        state = {}
        tddstate.apply_patch(state, {"a.b.c": 1})
        self.assertEqual(state, {"a": {"b": {"c": 1}}})

    def test_patch_values_are_deep_copied(self):
        shared = {"x": [1, 2]}
        state = {}
        tddstate.apply_patch(state, {"k": shared})
        shared["x"].append(3)
        self.assertEqual(state["k"]["x"], [1, 2])

    def test_none_clears_a_field(self):
        state = {"step": {"id": "s01"}}
        tddstate.apply_patch(state, {"step": None})
        self.assertIsNone(state["step"])


class TestInit(RepoCase):

    def test_init_resolves_fqn_and_gradle_project(self):
        self.assertEqual(self.cli("init", "--target", self.target_rel,
                                  "--goal", "extract pricing"), 0)
        state = self.ctx().state
        self.assertEqual(state["target"]["class_fqn"], "com.acme.Order")
        self.assertEqual(state["target"]["simple_name"], "Order")
        self.assertEqual(state["target"]["gradle_project"], ":app")
        self.assertEqual(state["phase"], "DISCOVER_UNIT")

    def test_init_refuses_to_silently_replace_an_active_target(self):
        self.cli("init", "--target", self.target_rel)
        other = "app/src/main/java/com/acme/Invoice.java"
        (self.repo / other).write_text("package com.acme;\npublic class Invoice {}\n",
                                       encoding="utf-8")
        self.assertEqual(self.cli("init", "--target", other), tddstate.EXIT_REFUSED)
        self.assertEqual(self.cli("init", "--target", other, "--force"), 0)

    def test_init_rejects_a_non_java_target(self):
        (self.repo / "notes.txt").write_text("x", encoding="utf-8")
        self.assertEqual(self.cli("init", "--target", "notes.txt"), tddstate.EXIT_ERROR)

    def test_root_project_yields_empty_gradle_path(self):
        (self.repo / "build.gradle").write_text("plugins { id 'java' }\n", encoding="utf-8")
        (self.repo / "src" / "main" / "java" / "com" / "acme").mkdir(parents=True)
        rel = "src/main/java/com/acme/Root.java"
        (self.repo / rel).write_text("package com.acme;\npublic class Root {}\n",
                                     encoding="utf-8")
        self.cli("init", "--target", rel)
        self.assertEqual(self.ctx().state["target"]["gradle_project"], "")


class TestDurability(RepoCase):

    def test_journal_is_written_before_state(self):
        self.cli("init", "--target", self.target_rel)
        ctx = self.ctx()
        self.assertTrue(ctx.journal.exists())
        events = tddstate.read_journal(ctx)
        self.assertEqual(events[0]["event"], "init")
        self.assertIn("target", events[0]["patch"])

    def test_state_write_is_atomic_leaving_no_tmp_file(self):
        self.cli("init", "--target", self.target_rel)
        leftovers = list((self.repo / ".claude" / "tdd" / "state").glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_repair_reproduces_state_from_the_journal(self):
        self.cli("init", "--target", self.target_rel, "--goal", "extract pricing")
        self.cli("note", "PricingSteps", "also", "covers", "Invoice")
        self.cli("block", "waiting", "on", "a", "decision")
        self.cli("unblock")
        before = json.loads((self.repo / ".claude/tdd/state/session.json")
                            .read_text(encoding="utf-8"))

        (self.repo / ".claude/tdd/state/session.json").unlink()
        self.assertEqual(self.cli("repair"), 0)

        after = json.loads((self.repo / ".claude/tdd/state/session.json")
                           .read_text(encoding="utf-8"))
        self.assertEqual(before, after)

    def test_repair_survives_a_torn_final_journal_line(self):
        self.cli("init", "--target", self.target_rel)
        self.cli("note", "something", "useful")
        journal = self.repo / ".claude/tdd/state/journal.jsonl"
        with open(str(journal), "a", encoding="utf-8") as fh:
            fh.write('{"ts": "2026-09-12T10:00:00Z", "event": "note", "pat')
        (self.repo / ".claude/tdd/state/session.json").unlink()
        self.assertEqual(self.cli("repair"), 0)
        self.assertEqual(len(self.ctx().state["notes"]), 1)

    def test_corrupt_state_routes_to_repair_instead_of_crashing(self):
        self.cli("init", "--target", self.target_rel)
        (self.repo / ".claude/tdd/state/session.json").write_text("{ not json",
                                                                  encoding="utf-8")
        ctx = self.ctx()
        self.assertTrue(ctx.corrupt)
        self.assertEqual(self.cli("next"), 0)

    def test_journal_rotation_snapshots_state_first(self):
        self.cli("init", "--target", self.target_rel)
        ctx = self.ctx()
        with open(str(ctx.journal), "a", encoding="utf-8") as fh:
            fh.write("x" * (tddstate.JOURNAL_MAX_BYTES + 10) + "\n")
        self.cli("note", "after", "rotation")
        ctx = self.ctx()
        self.assertTrue(ctx.journal_prev.exists())
        events = tddstate.read_journal(ctx)
        self.assertEqual(events[0]["event"], "snapshot")
        (self.repo / ".claude/tdd/state/session.json").unlink()
        self.assertEqual(self.cli("repair"), 0)
        self.assertEqual(self.ctx().state["target"]["simple_name"], "Order")


class TestLock(RepoCase):

    def test_a_fresh_lock_from_another_pid_blocks_mutation(self):
        self.cli("init", "--target", self.target_rel)
        lock = self.repo / ".claude/tdd/state/lock"
        lock.write_text(json.dumps({"pid": os.getpid() + 1, "epoch": tddstate.time.time()}),
                        encoding="utf-8")
        self.assertEqual(self.cli("note", "blocked"), tddstate.EXIT_REFUSED)
        self.assertEqual(self.cli("--force-unlock", "note", "forced"), 0)

    def test_a_stale_lock_is_ignored(self):
        self.cli("init", "--target", self.target_rel)
        lock = self.repo / ".claude/tdd/state/lock"
        lock.write_text(json.dumps({"pid": os.getpid() + 1,
                                    "epoch": tddstate.time.time() - 3600}),
                        encoding="utf-8")
        self.assertEqual(self.cli("note", "fine"), 0)

    def test_read_commands_never_take_the_lock(self):
        self.cli("init", "--target", self.target_rel)
        (self.repo / ".claude/tdd/state/lock").write_text(
            json.dumps({"pid": os.getpid() + 1, "epoch": tddstate.time.time()}),
            encoding="utf-8")
        self.assertEqual(self.cli("next"), 0)
        self.assertEqual(self.cli("status"), 0)


class TestFreshnessFromDisk(RepoCase):

    def test_touching_the_target_makes_a_green_run_stale(self):
        self.cli("init", "--target", self.target_rel)
        ctx = self.ctx()
        ctx.state = state_at("scen_green")
        ctx.state["target"]["path"] = self.target_rel
        facts = tddstate.gather_facts(ctx)
        self.assertIsNotNone(facts["newest_epoch"])
        self.assertIn("changed after the run",
                      tddstate.staleness(ctx.state, "unit", facts) or "")

    def test_watched_files_span_target_unit_and_feature_paths(self):
        s = state_at("scen_green")
        watched = tddstate.watched_files(s)
        self.assertIn("src/main/java/com/acme/Order.java", watched)
        self.assertIn("src/test/java/com/acme/OrderTest.java", watched)
        self.assertIn("src/test/resources/features/order.feature", watched)
        self.assertNotIn("src/test/resources/features/order.feature:14", watched)


class TestTouchHook(RepoCase):
    """PostToolUse hook: flips the freshness flag, and never disturbs the edit."""

    def hook(self, payload):
        import contextlib
        import io
        import sys
        stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(payload))
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                return tddstate.main(["touch", "--from-hook"])
        finally:
            sys.stdin = stdin

    def _select_target_as_watched(self):
        self.cli("init", "--target", self.target_rel)
        ctx = self.ctx()
        with tddstate.Lock(ctx, force=True):
            tddstate.mutate(ctx, "select", {"unit.selected": [
                {"id": "com.acme.OrderTest", "path": self.target_rel, "status": "unknown"}]})

    def test_editing_a_watched_file_marks_the_gate_dirty(self):
        self._select_target_as_watched()
        rc = self.hook({"tool_name": "Edit",
                        "tool_input": {"file_path": str(self.repo / self.target_rel)}})
        self.assertEqual(rc, 0)
        state = self.ctx().state
        self.assertTrue(state["freshness"]["dirty"])
        self.assertIn(self.target_rel, state["freshness"]["dirty_files"])

    def test_editing_an_unrelated_file_changes_nothing(self):
        self._select_target_as_watched()
        other = self.repo / "app" / "src" / "main" / "java" / "com" / "acme" / "Other.java"
        other.write_text("package com.acme;\npublic class Other {}\n", encoding="utf-8")
        self.assertEqual(self.hook({"tool_input": {"file_path": str(other)}}), 0)
        self.assertFalse(self.ctx().state["freshness"]["dirty"])

    def test_malformed_hook_payloads_still_exit_zero(self):
        """A hook that fails would break the Edit it is attached to."""
        self._select_target_as_watched()
        for payload in ({}, {"tool_input": {}}, {"tool_input": {"file_path": "/nope"}}):
            self.assertEqual(self.hook(payload), 0)

    def test_touch_without_the_flag_never_reads_stdin(self):
        """Guards against blocking forever on an inherited open pipe."""
        self.cli("init", "--target", self.target_rel)
        self.assertEqual(self.cli("touch"), 0)


class TestRenderingBudgets(RepoCase):

    def _rich_state(self):
        s = state_at("step_open")
        s["unit"]["last_run"] = tddstate.clone(state_at("unit_green")["unit"]["last_run"])
        s["unit"]["last_run"]["outcome"] = "tests_failed"
        s["unit"]["last_run"]["failed"] = [
            {"id": "com.acme.OrderTest#t%d" % i,
             "message": "expected something quite long " * 6} for i in range(40)]
        s["notes"] = [{"at": "2026-09-12T09:00:00Z", "text": "a note " * 20}
                      for _ in range(10)]
        s["history_tail"] = ["10:0%d did a thing" % i for i in range(10)]
        return s

    def test_resume_brief_fits_in_forty_lines(self):
        ctx = self.ctx()
        ctx.state = self._rich_state()
        facts = tddstate.gather_facts(ctx)
        tddstate.refresh_derived(ctx, facts)
        text = tddstate.render_resume(ctx.state, facts, ["a", "b", "c", "d", "e"])
        self.assertLessEqual(len(text.splitlines()), 40)

    def test_next_stays_compact_even_with_forty_failures(self):
        ctx = self.ctx()
        ctx.state = self._rich_state()
        facts = tddstate.gather_facts(ctx)
        tddstate.refresh_derived(ctx, facts)
        text = tddstate.render_next(ctx.state, facts)
        self.assertLessEqual(len(text.splitlines()), 12)

    def test_status_stays_within_forty_lines(self):
        ctx = self.ctx()
        ctx.state = self._rich_state()
        facts = tddstate.gather_facts(ctx)
        tddstate.refresh_derived(ctx, facts)
        self.assertLessEqual(
            len(tddstate.render_status(ctx.state, facts).splitlines()), 40)

    def test_output_is_ascii_so_windows_consoles_cannot_mangle_it(self):
        ctx = self.ctx()
        ctx.state = self._rich_state()
        facts = tddstate.gather_facts(ctx)
        tddstate.refresh_derived(ctx, facts)
        for text in (tddstate.render_next(ctx.state, facts),
                     tddstate.render_resume(ctx.state, facts, ["x"])):
            text.encode("ascii")  # raises if a stray unicode separator crept in


class TestStatusMd(RepoCase):

    def test_status_md_is_written_and_names_the_next_command(self):
        self.cli("init", "--target", self.target_rel, "--goal", "extract pricing")
        md = (self.repo / ".claude/tdd/STATUS.md").read_text(encoding="utf-8")
        self.assertIn("extract pricing", md)
        self.assertIn("discover unit", md)
        self.assertIn(self.target_rel, md)

    def test_checkpoint_is_idempotent(self):
        self.cli("init", "--target", self.target_rel)
        self.cli("checkpoint", "--quiet")
        first = (self.repo / ".claude/tdd/STATUS.md").read_text(encoding="utf-8")
        self.cli("checkpoint", "--quiet")
        second = (self.repo / ".claude/tdd/STATUS.md").read_text(encoding="utf-8")
        self.assertEqual(first, second)

    def test_checkpoint_adds_no_journal_noise(self):
        """It runs on every Stop hook; it must not grow the journal."""
        self.cli("init", "--target", self.target_rel)
        before = len(tddstate.read_journal(self.ctx()))
        self.cli("checkpoint", "--quiet")
        self.cli("checkpoint", "--quiet")
        self.assertEqual(len(tddstate.read_journal(self.ctx())), before)


class TestExitCodes(RepoCase):

    def test_blocked_state_reports_its_own_exit_code(self):
        self.cli("init", "--target", self.target_rel)
        self.assertEqual(self.cli("block", "stuck", "on", "a", "decision"),
                         tddstate.EXIT_BLOCKED)
        self.assertEqual(self.cli("next"), tddstate.EXIT_BLOCKED)
        self.assertEqual(self.cli("unblock"), 0)
        self.assertEqual(self.cli("next"), 0)

    def test_resume_is_silent_when_idle_so_hooks_stay_quiet(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self.cli("resume", "--brief", "--quiet-if-idle")
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue(), "")

    def test_no_advertised_command_is_left_stubbed(self):
        """Every subcommand the parser offers must actually do something."""
        import argparse
        parser = tddstate.build_parser()
        subs = [a for a in parser._actions
                if isinstance(a, argparse._SubParsersAction)][0]
        for name, sub in subs.choices.items():
            self.assertIsNot(sub.get_default("func"), tddstate.cmd_stub, name)
        self.assertEqual(tddstate.STUB_STEPS, {})


if __name__ == "__main__":
    unittest.main()
