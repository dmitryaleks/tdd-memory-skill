"""The skill, its references and the slash commands.

These tests exist to stop documentation drift. A weak model types what the docs
tell it to type, so a command or flag that no longer exists is not a cosmetic
problem - it is a dead end in the loop.
"""

import argparse
import re
import unittest

from _harness import ROOT, tddstate

PAYLOAD = ROOT / "payload" / ".claude"
SKILL = PAYLOAD / "skills" / "tdd-loop" / "SKILL.md"
REFERENCES = PAYLOAD / "skills" / "tdd-loop" / "references"
COMMANDS = PAYLOAD / "commands"
CLAUDE_BLOCK = PAYLOAD / "CLAUDE.tdd.md"

DOCS = [SKILL, CLAUDE_BLOCK] + sorted(REFERENCES.glob("*.md")) + \
    sorted(COMMANDS.glob("*.md"))

SKILL_LINE_BUDGET = 120


def text(path):
    return path.read_text(encoding="utf-8")


def frontmatter(path):
    body = text(path)
    if not body.startswith("---"):
        return {}
    _, raw, _rest = body.split("---", 2)
    out, key = {}, None
    for line in raw.splitlines():
        if not line.strip():
            continue
        if line.startswith("  - ") and key:
            out.setdefault(key + "[]", []).append(line[4:].strip())
        elif ":" in line and not line.startswith(" "):
            key, _, value = line.partition(":")
            key = key.strip()
            out[key] = value.strip()
    return out


def cli_commands():
    parser = tddstate.build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return parser, dict(action.choices)
    raise AssertionError("no subparsers found")


class TestSkillShape(unittest.TestCase):

    def test_skill_fits_its_line_budget(self):
        """It is loaded into a scarce context window every time it triggers."""
        self.assertLessEqual(len(text(SKILL).splitlines()), SKILL_LINE_BUDGET)

    def test_frontmatter_declares_what_the_harness_needs(self):
        meta = frontmatter(SKILL)
        self.assertEqual(meta.get("name"), "tdd-loop")
        self.assertEqual(meta.get("user-invocable"), "true")
        self.assertTrue(meta.get("description"))
        self.assertIn("allowed-tools[]", meta)

    def test_the_description_carries_the_words_that_should_trigger_it(self):
        description = frontmatter(SKILL).get("description", "").lower()
        for trigger in ("refactor", "resume", "junit", "cucumber", "java"):
            self.assertIn(trigger, description, trigger)

    def test_allowed_tools_permit_the_tracker_and_nothing_dangerous(self):
        tools = frontmatter(SKILL)["allowed-tools[]"]
        self.assertTrue(any("tddstate.py" in t for t in tools))
        for tool in tools:
            if tool.startswith("Bash("):
                self.assertIn("tddstate.py", tool,
                              "unrestricted Bash would let the model run gradle "
                              "directly, which rule 3 forbids")

    def test_every_reference_exists_and_is_pointed_at(self):
        body = text(SKILL)
        for name in ("protocol.md", "discovery.md", "recovery.md"):
            self.assertTrue((REFERENCES / name).is_file(), name)
            self.assertIn("references/%s" % name, body)

    def test_references_are_not_inlined_into_the_skill(self):
        """They must load on demand, not ride along in every trigger."""
        for reference in REFERENCES.glob("*.md"):
            self.assertNotIn(text(reference)[:200], text(SKILL))


class TestCommandsAreReal(unittest.TestCase):
    """Every command the docs tell the model to type must actually exist."""

    def setUp(self):
        self.parser, self.subcommands = cli_commands()

    # Two spellings: the full command, and the `... <cmd>` shorthand the skill
    # uses in its tables. Checking only the first leaves the table - the most
    # likely home for an invented command - unguarded. Leading global flags are
    # skipped so `tddstate.py --force-unlock next` resolves to `next`.
    INVOCATION_RES = (
        re.compile(r"tddstate\.py\s+(?:--[\w-]+\s+)*([a-z][\w-]*)"),
        re.compile(r"\A\.\.\.\s+(?:--[\w-]+\s+)*([a-z][\w-]*)"),
    )

    @staticmethod
    def code_spans(line):
        """Backticked spans, or the whole line inside a fenced block.

        Flags are only meaningful inside the span that holds the command:
        prose elsewhere on the line may mention Gradle's own flags.
        """
        spans = re.findall(r"`([^`]+)`", line)
        return spans if spans else [line]

    def documented_invocations(self):
        for doc in DOCS:
            for line in text(doc).splitlines():
                for span in self.code_spans(line):
                    for pattern in self.INVOCATION_RES:
                        match = pattern.search(span)
                        if match:
                            yield doc.name, span, match.group(1)
                            break

    def test_no_documented_subcommand_is_invented(self):
        seen = set()
        for name, _span, command in self.documented_invocations():
            self.assertIn(command, self.subcommands,
                          "%s documents `%s`, which the CLI does not have"
                          % (name, command))
            seen.add(command)
        self.assertIn("next", seen)
        self.assertGreaterEqual(len(seen), 10)

    def test_documented_flags_exist_on_the_command_they_follow(self):
        global_flags = set()
        for action in self.parser._actions:
            global_flags.update(action.option_strings)
        checked = 0
        for name, span, command in self.documented_invocations():
            valid = set(global_flags)
            for action in self.subcommands[command]._actions:
                valid.update(action.option_strings)
            for flag in re.findall(r"(?<![\w-])(--[a-z][\w-]*)", span):
                checked += 1
                self.assertIn(flag, valid,
                              "%s shows `%s` with %s, which it does not accept"
                              % (name, command, flag))
        self.assertGreater(checked, 5, "flag checking matched nothing")

    def test_the_skill_covers_every_command_a_session_needs(self):
        body = text(SKILL)
        for command in ("next", "init", "discover", "select", "baseline", "step",
                        "run", "revert-step", "done", "note", "block", "status"):
            self.assertIn("... %s" % command if command != "next" else "next", body,
                          command)

    def test_still_stubbed_commands_are_not_advertised_as_usable(self):
        """`doctor` is referenced, but only as a diagnostic to run when asked."""
        for stub in tddstate.STUB_STEPS:
            self.assertNotIn("... %s\n" % stub, text(SKILL))


class TestActionCodes(unittest.TestCase):
    """The skill's action table has to match what the resolver can emit."""

    def setUp(self):
        source = tddstate.SRC if hasattr(tddstate, "SRC") else None
        body = (ROOT / "payload" / ".claude" / "tdd" / "tddstate.py").read_text(
            encoding="utf-8")
        self.emitted = set(re.findall(r'NA\("([A-Z_]+)"', body)) - {"UNKNOWN"}
        table = text(SKILL).split("## What `next` can ask for", 1)[1]
        self.documented = set(re.findall(r"`([A-Z][A-Z_]+)`", table))

    def test_every_code_the_resolver_emits_is_documented(self):
        self.assertEqual(self.emitted - self.documented, set())

    def test_no_documented_code_is_invented(self):
        self.assertEqual(self.documented - self.emitted - {"REPAIR"}, set())

    def test_the_resolver_really_can_produce_them(self):
        """Guards against a code that exists only in a string literal."""
        self.assertIn("ESCALATE", self.emitted)
        self.assertIn("AWAIT_EDIT", self.emitted)
        self.assertGreaterEqual(len(self.emitted), 15)


class TestPathsAreReal(unittest.TestCase):

    def setUp(self):
        self.ctx = tddstate.Ctx(ROOT / "nowhere")

    def test_documented_paths_match_the_tracker(self):
        wanted = {
            ".claude/tdd/state/session.json": self.ctx.session,
            ".claude/tdd/state/journal.jsonl": self.ctx.journal,
            ".claude/tdd/STATUS.md": self.ctx.status_md,
        }
        blob = "\n".join(text(doc) for doc in DOCS)
        for shown, actual in wanted.items():
            self.assertIn(shown, blob, shown)
            self.assertEqual(actual.relative_to(self.ctx.repo).as_posix(), shown)

    def test_the_claude_block_is_marker_delimited_for_idempotent_install(self):
        body = text(CLAUDE_BLOCK)
        self.assertTrue(body.startswith("<!-- BEGIN tdd-memory-skill -->"))
        self.assertTrue(body.rstrip().endswith("<!-- END tdd-memory-skill -->"))

    def test_the_claude_block_names_the_one_command_that_matters(self):
        self.assertIn("python .claude/tdd/tddstate.py next", text(CLAUDE_BLOCK))


class TestSlashCommands(unittest.TestCase):

    def test_each_command_declares_a_description(self):
        for path in sorted(COMMANDS.glob("*.md")):
            self.assertTrue(frontmatter(path).get("description"), path.name)

    def test_each_command_restricts_bash_to_the_tracker(self):
        for path in sorted(COMMANDS.glob("*.md")):
            tools = frontmatter(path).get("allowed-tools", "")
            if "Bash(" in tools:
                self.assertIn("tddstate.py", tools, path.name)

    def test_the_target_command_takes_an_argument(self):
        meta = frontmatter(COMMANDS / "tdd-target.md")
        self.assertIn("argument-hint", meta)
        self.assertIn("$ARGUMENTS", text(COMMANDS / "tdd-target.md"))

    def test_the_target_command_does_not_casually_discard_a_live_target(self):
        body = text(COMMANDS / "tdd-target.md")
        self.assertIn("--force", body)
        self.assertIn("unless the user explicitly says", body)


class TestRulesArePresent(unittest.TestCase):
    """The prohibitions are the load-bearing part of the skill."""

    def test_the_skill_states_each_guard(self):
        body = text(SKILL).lower()
        for phrase in ("never edit code before `step start`",
                       "never run gradle yourself",
                       "never run scenario tests while the unit gate is red",
                       "never trust a test result older than your last edit",
                       "a build failure is not a test failure"):
            self.assertIn(phrase, body, phrase)

    def test_recovery_covers_the_outcomes_that_are_not_verdicts(self):
        body = text(REFERENCES / "recovery.md")
        for phrase in ("BUILD FAILED", "NO RESULTS", "TIMED OUT", "repair",
                       "--force-unlock"):
            self.assertIn(phrase, body, phrase)


if __name__ == "__main__":
    unittest.main()
