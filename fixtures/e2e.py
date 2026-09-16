#!/usr/bin/env python3
"""End-to-end walk of the whole loop (DEVPLAN section 15.2-15.5).

Everything runs as real subprocesses against a scratch copy of
`fixtures/sample-gradle-project`, installed the way a user would install it.

Gradle itself is replaced by a scripted stand-in that writes genuine JUnit XML
and Cucumber messages. That keeps the walk offline - a real Gradle run would
download JUnit and Cucumber from the network, which this project forbids -
while still exercising wrapper resolution, the result-directory wipe, the
subprocess, report parsing and outcome classification for real.

    python fixtures/e2e.py [--keep]

Exits non-zero on the first failed expectation.
"""

import argparse
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "fixtures" / "sample-gradle-project"
TARGET = "app/src/main/java/com/acme/Order.java"
FEATURE = "app/src/test/resources/features/order.feature"

# The stand-in decides what to emit from the content of Order.java, so the walk
# drives it exactly as it would drive the real thing: by editing code.
FAKE_GRADLE = r'''
import os, pathlib, sys

root = pathlib.Path(__file__).resolve().parent
source = (root / "app/src/main/java/com/acme/Order.java").read_text(encoding="utf-8")
argv = " ".join(sys.argv[1:])
scenario = "cucumber" in argv or "RunCucumberTest" in argv
results = root / "app/build/test-results/test"

for i in range(600):
    print("gradle chatter line %d - the noise this tool keeps out of context" % i)

if "NO SUCH TEST" in argv:
    print("FAILURE: Build failed with an exception.")
    print("* What went wrong:")
    print("Execution failed for task ':app:test'.")
    print("> No tests found for given includes: [com.acme.NoSuchTest](--tests filter)")
    sys.exit(1)

if "SYNTAX ERROR" in source:
    print("> Task :app:compileJava FAILED")
    print("%s/app/src/main/java/com/acme/Order.java:12: error: cannot find symbol"
          % root)
    print("        return priceCalculator.total(lines);")
    print("  symbol:   variable priceCalculator")
    print("1 error")
    print("")
    print("FAILURE: Build failed with an exception.")
    print("* What went wrong:")
    print("Execution failed for task ':app:compileJava'.")
    print("> Compilation failed; see the compiler error output for details.")
    print("* Try:")
    print("> Run with --stacktrace option to get the stack trace.")
    sys.exit(1)

# Two markers: one breaks the unit tests, the other breaks only the
# scenarios, so the walk can drive a scenario-only regression.
scen_red = "SCEN BROKEN" in source
unit_red = ("BROKEN" in source) and not scen_red
red = scen_red if scenario else unit_red
results.mkdir(parents=True, exist_ok=True)

if scenario:
    # Cucumber reports features by the path it loaded them by, not the path
    # discovery found them at: @SelectClasspathResource gives a classpath uri.
    lines = []
    lines.append('{"gherkinDocument":{"uri":"classpath:features/order.feature",'
                 '"feature":{"name":"Orders","children":['
                 '{"scenario":{"id":"sc-1","location":{"line":8},"name":"No discount for small orders"}},'
                 '{"scenario":{"id":"sc-2","location":{"line":14},"name":"Discount applied to large orders"}}'
                 ']}}}')
    for n, (pid, sid) in enumerate((("pk-1", "sc-1"), ("pk-2", "sc-2")), start=1):
        lines.append('{"pickle":{"id":"%s","uri":"classpath:features/order.feature",'
                     '"name":"s%d","astNodeIds":["%s"]}}' % (pid, n, sid))
        lines.append('{"testCase":{"id":"tc-%d","pickleId":"%s"}}' % (n, pid))
        lines.append('{"testCaseStarted":{"id":"ts-%d","testCaseId":"tc-%d"}}' % (n, n))
        status = "FAILED" if (red and n == 2) else "PASSED"
        lines.append('{"testStepFinished":{"testCaseStartedId":"ts-%d",'
                     '"testStepResult":{"status":"%s"}}}' % (n, status))
        lines.append('{"testCaseFinished":{"testCaseStartedId":"ts-%d"}}' % n)
    ndjson = root / "app/build/tdd/cucumber.ndjson"
    ndjson.parent.mkdir(parents=True, exist_ok=True)
    ndjson.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cases = ('  <testcase name="Discount applied to large orders" classname="Orders">\n'
             '    <failure message="expected 135 but was 150">stack</failure>\n'
             '  </testcase>' if red else
             '  <testcase name="Discount applied to large orders" classname="Orders"/>')
    (results / "TEST-com.acme.RunCucumberTest.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<testsuite name="Cucumber" tests="2" skipped="0" failures="%d" errors="0">\n'
        '  <testcase name="No discount for small orders" classname="Orders"/>\n%s\n'
        '</testsuite>\n' % (1 if red else 0, cases), encoding="utf-8")
else:
    cases = []
    for name in ("totalsLines", "leavesSmallOrdersAlone"):
        cases.append('  <testcase name="%s()" classname="com.acme.OrderTest"/>' % name)
    if red:
        cases.append('  <testcase name="appliesDiscountToLargeOrders()" '
                     'classname="com.acme.OrderTest">\n'
                     '    <failure message="expected: &lt;135&gt; but was: &lt;150&gt;">'
                     'at com.acme.OrderTest.appliesDiscountToLargeOrders(OrderTest.java:21)'
                     '</failure>\n  </testcase>')
    else:
        cases.append('  <testcase name="appliesDiscountToLargeOrders()" '
                     'classname="com.acme.OrderTest"/>')
    (results / "TEST-com.acme.OrderTest.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<testsuite name="com.acme.OrderTest" tests="3" skipped="0" failures="%d" '
        'errors="0">\n%s\n</testsuite>\n' % (1 if red else 0, "\n".join(cases)),
        encoding="utf-8")

print("BUILD %s" % ("FAILED" if red else "SUCCESSFUL"))
sys.exit(1 if red else 0)
'''

PASSED, FAILED = [], []


def check(label, condition, detail=""):
    (PASSED if condition else FAILED).append(label)
    print("   %s %s%s" % ("ok  " if condition else "FAIL", label,
                          "" if condition else "  <- " + str(detail)))
    return bool(condition)


def section(title):
    print("\n== %s %s" % (title, "=" * max(0, 68 - len(title))))


class Repo(object):
    def __init__(self, path):
        self.path = path

    def tdd(self, *args, **kw):
        expect = kw.pop("expect", None)
        proc = subprocess.run(
            [sys.executable, str(self.path / ".claude" / "tdd" / "tddstate.py")]
            + list(args), cwd=str(self.path), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, encoding="utf-8", errors="replace")
        if expect is not None:
            check("`tdd %s` exits %d" % (" ".join(args), expect),
                  proc.returncode == expect,
                  "got %d: %s" % (proc.returncode, proc.stdout.strip()[:200]))
        return proc

    def state(self):
        return json.loads((self.path / ".claude/tdd/state/session.json")
                          .read_text(encoding="utf-8"))

    def action(self):
        return json.loads(self.tdd("next", "--json").stdout)["action"]["code"]

    def write_target(self, marker=None):
        """Set (or clear) the marker the Gradle stand-in reacts to.

        Strips only the comment, never the line it sits on: removing the whole
        line would take the class declaration with it, and every later marker
        would then silently fail to apply.
        """
        path = self.path / TARGET
        body = re.sub(r"[ \t]*// e2e:[^\n]*", "",
                      path.read_text(encoding="utf-8"))
        if marker:
            body = body.replace("public class Order {",
                                "public class Order { // e2e: %s" % marker, 1)
        path.write_text(body, encoding="utf-8")


def build_repo(workdir):
    repo = workdir / "repo"
    shutil.copytree(str(SAMPLE), str(repo))
    (repo / "fake_gradle.py").write_text(FAKE_GRADLE, encoding="utf-8")
    (repo / "gradlew.bat").write_text(
        '@echo off\r\npython "%~dp0fake_gradle.py" %*\r\n', encoding="utf-8")
    wrapper = repo / "gradlew"
    wrapper.write_text('#!/bin/sh\nexec python3 "$(dirname "$0")/fake_gradle.py" "$@"\n',
                       encoding="utf-8")
    try:
        wrapper.chmod(0o755)
    except OSError:
        pass
    return Repo(repo)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true",
                        help="leave the scratch repo on disk for inspection")
    args = parser.parse_args(argv)

    workdir = pathlib.Path(tempfile.mkdtemp(prefix="tdd-e2e-"))
    repo = build_repo(workdir)
    print("scratch repo: %s" % repo.path)

    section("install")
    install = subprocess.run(
        [sys.executable, str(ROOT / "install" / "install.py"), str(repo.path),
         "--no-doctor"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding="utf-8", errors="replace")
    check("installer succeeds", install.returncode == 0, install.stdout)
    check("tracker installed", (repo.path / ".claude/tdd/tddstate.py").is_file())
    check("skill installed", (repo.path / ".claude/skills/tdd-loop/SKILL.md").is_file())
    check("CLAUDE.md carries the protocol block",
          "tdd-memory-skill" in (repo.path / "CLAUDE.md").read_text(encoding="utf-8"))
    doctor = repo.tdd("doctor")
    check("doctor passes", doctor.returncode == 0, doctor.stdout)
    check("doctor sees the cucumber forwarding",
          "cucumber system properties forwarded" in doctor.stdout)

    section("select the work")
    repo.tdd("init", "--target", TARGET, "--goal", "extract PriceCalculator",
             expect=0)
    check("phase is DISCOVER_UNIT", repo.action() == "DISCOVER_UNIT")

    discover = repo.tdd("discover", "unit")
    first = [l for l in discover.stdout.splitlines() if l.strip().startswith("[")]
    check("OrderTest ranks first", first and "com.acme.OrderTest" in first[0],
          first[:1])
    repo.tdd("select", "unit", "com.acme.OrderTest", expect=0)

    discover = repo.tdd("discover", "scenario")
    check("the target scenario is found through the step-def hop",
          FEATURE + ":14" in discover.stdout)
    check("the unrelated feature is not offered",
          "shipping.feature" not in discover.stdout)
    check("the cucumber runner is detected",
          repo.state()["scenario"]["runner_class"] == "com.acme.RunCucumberTest")
    repo.tdd("select", "scenario", FEATURE + ":8", FEATURE + ":14", expect=0)

    section("baselines, before any edit")
    check("next asks for the unit baseline", repo.action() == "BASELINE_UNIT")
    repo.tdd("baseline", "unit", expect=0)
    repo.tdd("baseline", "scenario", expect=0)
    check("both baselines are green",
          repo.state()["unit"]["green"] and repo.state()["scenario"]["green"])
    check("editing is refused before an increment exists",
          repo.action() == "START_STEP")

    section("one increment, the hard way")
    repo.tdd("step", "start", "extract PriceCalculator", expect=0)
    check("next asks for the code change", repo.action() == "AWAIT_EDIT")

    repo.write_target("SYNTAX ERROR")
    run = repo.tdd("run", "unit")
    check("a compile error is classified as a build failure",
          repo.state()["unit"]["last_run"]["outcome"] == "build_failed")
    check("the summary says the tests never ran", "never ran" in run.stdout)
    check("the javac error is shown", "cannot find symbol" in run.stdout)
    check("next sends the model at the build, not the tests",
          repo.action() == "FIX_BUILD")

    repo.write_target("BROKEN")
    run = repo.tdd("run", "unit")
    check("a real failure is classified as a test failure",
          repo.state()["unit"]["last_run"]["outcome"] == "tests_failed")
    check("the failing test is named",
          "appliesDiscountToLargeOrders" in run.stdout)
    summary = run.stdout.split("\nTDD ")[0].strip().splitlines()
    check("the summary fits the 20-line budget", len(summary) <= 20, len(summary))
    log = repo.path / ".claude/tdd" / repo.state()["unit"]["last_run"]["log"]
    log_lines = len(log.read_text(encoding="utf-8").splitlines())
    check("the full build output is on disk instead", log_lines > 500, log_lines)
    check("context saved: %d lines on disk, %d printed" % (log_lines, len(summary)),
          True)

    # The scenario gate already holds a result: its baseline. The gate is
    # working if the refused run left that result untouched.
    before_scenario = repo.state()["scenario"]["last_run"]["ran_at_epoch"]
    repo.tdd("run", "scenario", expect=2)
    check("the ordering gate holds while unit is red",
          repo.state()["scenario"]["last_run"]["ran_at_epoch"] == before_scenario)

    section("green, and the freshness gate")
    repo.write_target()
    repo.tdd("run", "unit", expect=0)
    check("unit is green", repo.state()["unit"]["green"])
    check("next moves on to the scenarios", repo.action() == "RUN_SCENARIO")

    repo.tdd("run", "scenario", expect=0)
    check("scenario ids survive the classpath uri",
          [i["id"] for i in repo.state()["scenario"]["selected"]]
          == [FEATURE + ":8", FEATURE + ":14"])
    check("scenario is green", repo.state()["scenario"]["green"])
    check("next offers to close the increment", repo.action() == "FINISH_STEP")

    repo.write_target("touched after the run")
    repo.tdd("step", "done", expect=2)
    check("an edit after a green run reopens the gate",
          repo.action() in ("RUN_UNIT", "FIX_UNIT"))
    repo.write_target()
    repo.tdd("run", "unit", expect=0)
    repo.tdd("run", "scenario", expect=0)
    repo.tdd("step", "done", expect=0)
    check("the increment is closed", repo.state()["steps_done"][0]["id"] == "s01")

    section("a scenario-only regression")
    # The case that matters most here: the scenarios go red while the unit
    # tests are green. Repairing that is a behaviour change, so it re-opens
    # the unit gate rather than looping around the scenarios alone.
    repo.tdd("step", "start", "tighten the discount boundary", expect=0)
    repo.write_target("SCEN BROKEN")
    repo.tdd("run", "unit", expect=0)
    repo.tdd("run", "scenario")
    check("scenarios can fail while unit is green",
          repo.state()["scenario"]["last_run"]["outcome"] == "tests_failed"
          and repo.state()["unit"]["green"])
    check("next asks for a scenario fix", repo.action() == "FIX_SCENARIO")
    guidance = repo.tdd("next").stdout
    check("the fix instruction names both gates", "BOTH gates" in guidance)
    check("and points at the unit gate first",
          "run unit" in guidance.split("CMD")[1])

    repo.write_target()
    check("the scenario fix re-opens the unit gate", repo.action() == "RUN_UNIT")
    repo.tdd("run", "scenario", expect=2)
    check("scenarios cannot be re-run before unit is verified again",
          repo.action() == "RUN_UNIT")
    repo.tdd("run", "unit", expect=0)
    check("only then does it ask for the scenarios",
          repo.action() == "RUN_SCENARIO")
    repo.tdd("run", "scenario", expect=0)
    check("the increment closes once both are green again",
          repo.action() == "FINISH_STEP")
    repo.tdd("step", "done", expect=0)

    section("losing the session")
    repo.tdd("step", "start", "inline the duplicate branch", expect=0)
    repo.write_target("BROKEN")
    repo.tdd("run", "unit")
    before = repo.state()

    (repo.path / ".claude/tdd/state/session.json").unlink()
    repair = repo.tdd("repair")
    check("repair rebuilds the state from the journal", repair.returncode == 0)
    check("nothing was lost", repo.state()["step"]["title"]
          == before["step"]["title"])
    check("the red gate survived", repo.state()["unit"]["green"] is False)

    brief = repo.tdd("resume", "--brief")
    for label, needle in (("the target", "Order.java"),
                          ("the open increment", "inline the duplicate branch"),
                          ("the failing test", "appliesDiscountToLargeOrders"),
                          ("the next command", "run unit")):
        check("a cold session is told %s" % label, needle in brief.stdout)
    check("the brief fits 40 lines",
          len(brief.stdout.splitlines()) <= 40, len(brief.stdout.splitlines()))

    section("thrashing, and the way out")
    for _ in range(2):
        repo.tdd("run", "unit")
    check("three identical failures escalate", repo.action() == "ESCALATE")
    original = (repo.path / TARGET).read_bytes()
    repo.tdd("revert-step", expect=0)
    check("revert restores the increment's starting point",
          b"BROKEN" not in (repo.path / TARGET).read_bytes())
    check("revert reopens the increment for a different attempt",
          repo.action() == "AWAIT_EDIT")
    check("the thrash counter is cleared",
          repo.state()["attempts"]["same_failure_streak"] == 0)

    section("stale lock")
    lock = repo.path / ".claude/tdd/state/lock"
    lock.write_text(json.dumps({"pid": 999999, "epoch": 1}), encoding="utf-8")
    check("a stale lock does not wedge the loop",
          repo.tdd("note", "still working").returncode == 0)

    section("finishing")
    repo.tdd("step", "abandon", expect=0)
    # A revert leaves both gates unverified, so `done` refuses until they are
    # re-run. That refusal is the point, not an obstacle.
    repo.tdd("done", expect=2)
    repo.tdd("run", "unit", expect=0)
    repo.tdd("run", "scenario", expect=0)
    repo.tdd("done", expect=0)
    archives = list((repo.path / ".claude/tdd/archive").iterdir())
    if check("the target is archived", len(archives) == 1, archives):
        check("the archive explains itself",
              "extract PriceCalculator" in
              (archives[0] / "summary.md").read_text(encoding="utf-8"))
    check("the loop is ready for the next target", repo.action() == "SET_TARGET")

    section("a filter that matches nothing")
    repo.tdd("init", "--target", TARGET, "--goal", "check no_results", expect=0)
    repo.tdd("select", "unit", "com.acme.NoSuchTest NO SUCH TEST", expect=0)
    repo.tdd("select", "scenario", "--none", expect=0)
    repo.tdd("baseline", "unit")
    check("an empty filter is never read as a pass",
          repo.state()["unit"]["last_run"]["outcome"] == "no_results")
    check("and it is not mistaken for a build failure",
          repo.state()["unit"]["green"] is False)

    print("\n%s" % ("=" * 72))
    print("%d checks passed, %d failed" % (len(PASSED), len(FAILED)))
    for label in FAILED:
        print("  FAILED: %s" % label)
    if args.keep:
        print("scratch repo kept at %s" % repo.path)
    else:
        shutil.rmtree(str(workdir), ignore_errors=True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
