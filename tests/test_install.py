"""Installer and doctor (DEVPLAN section 12).

The installer edits files a user already owns - settings.json, .gitignore,
CLAUDE.md - so the tests are mostly about what it must NOT disturb.
"""

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

from _harness import ROOT, tddstate

_spec = importlib.util.spec_from_file_location(
    "tdd_install", ROOT / "install" / "install.py")
install = importlib.util.module_from_spec(_spec)
sys.modules["tdd_install"] = install
_spec.loader.exec_module(install)

SAMPLE = ROOT / "fixtures" / "sample-gradle-project"


class InstallCase(unittest.TestCase):

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="tddinst-"))
        self.repo = self.tmp / "repo"
        shutil.copytree(str(SAMPLE), str(self.repo))
        (self.repo / "gradlew.bat").write_text("@echo off\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def run_install(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            code = install.main([str(self.repo), "--no-doctor"] + list(argv))
        self.output = buf.getvalue()
        return code

    def settings(self):
        return json.loads((self.repo / ".claude" / "settings.json")
                          .read_text(encoding="utf-8"))

    def our_commands(self, event):
        out = []
        for group in (self.settings().get("hooks") or {}).get(event) or []:
            for hook in group.get("hooks") or []:
                if "tddstate.py" in hook.get("command", ""):
                    out.append(hook["command"])
        return out


class TestFreshInstall(InstallCase):

    def test_the_payload_lands_where_the_docs_say_it_does(self):
        self.assertEqual(self.run_install(), 0)
        for rel in (".claude/tdd/tddstate.py",
                    ".claude/skills/tdd-loop/SKILL.md",
                    ".claude/skills/tdd-loop/references/recovery.md",
                    ".claude/commands/tdd-next.md"):
            self.assertTrue((self.repo / rel).is_file(), rel)

    def test_the_internal_claude_md_source_is_not_copied_verbatim(self):
        self.run_install()
        self.assertFalse((self.repo / ".claude" / "CLAUDE.tdd.md").exists())
        self.assertIn("TDD refactoring loop",
                      (self.repo / "CLAUDE.md").read_text(encoding="utf-8"))

    def test_all_four_hook_events_are_installed(self):
        self.run_install()
        for event in tddstate.HOOK_EVENTS:
            self.assertTrue(self.our_commands(event), event)

    def test_the_hook_command_uses_an_absolute_interpreter(self):
        """`python` vs `python3` on PATH is exactly what breaks hooks."""
        self.run_install()
        command = self.our_commands("SessionStart")[0]
        interpreter = command.split('"')[1]
        self.assertTrue(pathlib.Path(interpreter).is_absolute(), command)
        self.assertIn("$CLAUDE_PROJECT_DIR", command)

    def test_the_post_tool_use_hook_matches_the_editing_tools(self):
        self.run_install()
        groups = self.settings()["hooks"]["PostToolUse"]
        ours = [g for g in groups
                if any("tddstate.py" in h["command"] for h in g["hooks"])]
        self.assertEqual(ours[0]["matcher"], "Edit|Write|MultiEdit")

    def test_the_installed_tracker_runs(self):
        self.run_install()
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            code = tddstate.main(["--repo-root", str(self.repo), "next"])
        self.assertEqual(code, 0)
        self.assertIn("no active target", buf.getvalue())


class TestIdempotence(InstallCase):

    def test_a_second_run_changes_nothing(self):
        self.run_install()
        before = {p: p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}
        self.run_install()
        self.assertIn("0 change(s)", self.output)
        after = {p: p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}
        self.assertEqual(set(before), set(after) - {self.repo / ".claude" /
                                                    "settings.json.bak"})
        for path, body in before.items():
            self.assertEqual(after[path], body, path.name)

    def test_hooks_are_replaced_not_duplicated(self):
        self.run_install()
        self.run_install("--python", "/usr/bin/python3")
        self.assertEqual(len(self.our_commands("SessionStart")), 1)
        self.assertIn("/usr/bin/python3", self.our_commands("SessionStart")[0])

    def test_blocks_are_replaced_not_appended_twice(self):
        self.run_install()
        self.run_install()
        for name, marker in ((".gitignore", "# BEGIN tdd-memory-skill"),
                             ("CLAUDE.md", "<!-- BEGIN tdd-memory-skill -->")):
            body = (self.repo / name).read_text(encoding="utf-8")
            self.assertEqual(body.count(marker), 1, name)


class TestExistingContentSurvives(InstallCase):

    def seed_settings(self):
        (self.repo / ".claude").mkdir(parents=True, exist_ok=True)
        (self.repo / ".claude" / "settings.json").write_text(json.dumps({
            "model": "opus",
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command",
                                             "command": "echo mine"}]}],
                "PreToolUse": [{"matcher": "Bash",
                                "hooks": [{"type": "command",
                                           "command": "guard.sh"}]}],
            },
        }, indent=2), encoding="utf-8")

    def test_foreign_hooks_on_the_same_event_are_kept(self):
        self.seed_settings()
        self.run_install()
        commands = [h["command"]
                    for g in self.settings()["hooks"]["SessionStart"]
                    for h in g["hooks"]]
        self.assertIn("echo mine", commands)
        self.assertEqual(len(commands), 2)

    def test_unrelated_events_and_settings_are_untouched(self):
        self.seed_settings()
        self.run_install()
        settings = self.settings()
        self.assertEqual(settings["model"], "opus")
        self.assertEqual(settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"],
                         "guard.sh")

    def test_the_previous_settings_file_is_backed_up(self):
        self.seed_settings()
        self.run_install()
        backup = json.loads((self.repo / ".claude" / "settings.json.bak")
                            .read_text(encoding="utf-8"))
        self.assertNotIn("PreCompact", backup.get("hooks", {}))

    def test_existing_gitignore_and_claude_md_content_is_preserved(self):
        (self.repo / ".gitignore").write_text("build/\n*.class\n", encoding="utf-8")
        (self.repo / "CLAUDE.md").write_text("# Acme\n\nHouse rules.\n",
                                             encoding="utf-8")
        self.run_install()
        self.assertIn("*.class", (self.repo / ".gitignore").read_text(encoding="utf-8"))
        self.assertIn("House rules.",
                      (self.repo / "CLAUDE.md").read_text(encoding="utf-8"))

    def test_a_malformed_settings_file_is_reported_not_overwritten(self):
        (self.repo / ".claude").mkdir(parents=True, exist_ok=True)
        broken = self.repo / ".claude" / "settings.json"
        broken.write_text("{ not json", encoding="utf-8")
        self.run_install()
        self.assertIn("not valid JSON", self.output)
        self.assertEqual(broken.read_text(encoding="utf-8"), "{ not json")


class TestLocalEdits(InstallCase):

    def test_an_edited_doc_is_kept_unless_forced(self):
        self.run_install()
        skill = self.repo / ".claude" / "skills" / "tdd-loop" / "SKILL.md"
        skill.write_text("# my own version\n", encoding="utf-8")
        self.run_install()
        self.assertIn("kept yours", self.output)
        self.assertEqual(skill.read_text(encoding="utf-8"), "# my own version\n")
        self.run_install("--force")
        self.assertIn("tdd-loop", skill.read_text(encoding="utf-8"))

    def test_the_tracker_itself_is_always_replaced(self):
        """The docs are tested against the engine; they must not drift apart."""
        self.run_install()
        engine = self.repo / ".claude" / "tdd" / "tddstate.py"
        engine.write_text("# stale copy\n", encoding="utf-8")
        self.run_install()
        self.assertIn("def resolve_next", engine.read_text(encoding="utf-8"))


class TestDryRun(InstallCase):

    def test_dry_run_writes_nothing(self):
        before = sorted(p.relative_to(self.repo).as_posix()
                        for p in self.repo.rglob("*"))
        self.run_install("--dry-run")
        after = sorted(p.relative_to(self.repo).as_posix()
                       for p in self.repo.rglob("*"))
        self.assertEqual(before, after)
        self.assertIn("would", self.output)


class TestDoctor(InstallCase):

    def doctor(self):
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            code = tddstate.main(["--repo-root", str(self.repo), "doctor"])
        return code, buf.getvalue()

    def test_doctor_passes_on_a_freshly_installed_fixture(self):
        self.run_install()
        code, report = self.doctor()
        self.assertEqual(code, 0, report)
        self.assertIn("0 failure(s)", report)

    def test_doctor_sees_the_installed_hooks(self):
        self.run_install()
        _code, report = self.doctor()
        self.assertIn("hooks installed", report)

    def test_doctor_reports_missing_hooks_before_install(self):
        _code, report = self.doctor()
        self.assertIn("hooks are not installed", report)

    def test_doctor_finds_the_cucumber_forwarding_snippet(self):
        self.run_install()
        _code, report = self.doctor()
        self.assertIn("cucumber system properties forwarded", report)

    def test_doctor_warns_when_forwarding_is_missing(self):
        build = self.repo / "app" / "build.gradle"
        body = build.read_text(encoding="utf-8")
        build.write_text(body.replace(
            "systemProperties System.properties.findAll "
            "{ it.key.toString().startsWith('cucumber.') }", ""), encoding="utf-8")
        self.run_install()
        _code, report = self.doctor()
        self.assertIn("NOT forwarded", report)
        self.assertIn("tasks.named('test')", report)

    def test_doctor_fails_without_a_gradle_wrapper(self):
        (self.repo / "gradlew.bat").unlink()
        old_which = tddstate.shutil.which
        tddstate.shutil.which = lambda name: None if name == "gradle" else old_which(name)
        try:
            code, report = self.doctor()
        finally:
            tddstate.shutil.which = old_which
        self.assertEqual(code, tddstate.EXIT_ERROR)
        self.assertIn("gradle wrapper missing", report)

    def test_doctor_reports_the_runtime_state_gitignore(self):
        self.run_install()
        _code, report = self.doctor()
        self.assertIn("runtime state is gitignored", report)

    def test_doctor_surfaces_observed_filter_evidence(self):
        """A recorded warning from a real run beats the static check."""
        self.run_install()
        ctx = tddstate.Ctx(self.repo)
        tddstate.load_state(ctx)
        with tddstate.Lock(ctx, force=True):
            tddstate.mutate(ctx, "run", {"scenario.last_run": {
                "warnings": ["the -Dcucumber filter is not reaching the test JVM"]}})
        _code, report = self.doctor()
        self.assertIn("previous run confirmed", report)

    def test_doctor_suggests_setting_a_non_default_scenario_task(self):
        build = self.repo / "app" / "build.gradle"
        build.write_text(build.read_text(encoding="utf-8")
                         + "\ntasks.register('cucumberTest', Test) { }\n",
                         encoding="utf-8")
        self.run_install()
        _code, report = self.doctor()
        self.assertIn("cucumberTest", report)
        self.assertIn("select scenario --task", report)


class TestScenarioTaskFlag(InstallCase):

    def test_the_task_can_be_set_when_selecting_scenarios(self):
        self.run_install()
        env = os.environ.get("TDD_REPO_ROOT")
        os.environ["TDD_REPO_ROOT"] = str(self.repo)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                tddstate.main(["init", "--target",
                               "app/src/main/java/com/acme/Order.java"])
                tddstate.main(["select", "scenario", "--task", "cucumberTest",
                               "app/src/test/resources/features/order.feature:14"])
            ctx = tddstate.Ctx(self.repo)
            tddstate.load_state(ctx)
            self.assertEqual(ctx.state["scenario"]["task"], "cucumberTest")
        finally:
            if env is None:
                os.environ.pop("TDD_REPO_ROOT", None)
            else:
                os.environ["TDD_REPO_ROOT"] = env


if __name__ == "__main__":
    unittest.main()
