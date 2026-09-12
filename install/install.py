#!/usr/bin/env python3
"""Install the TDD memory skill into a Java repository.

Copies the payload, merges the hook block into `.claude/settings.json` without
disturbing hooks that are already there, adds the gitignore lines and the
CLAUDE.md block, then runs `doctor`.

Re-running it is safe: every step is idempotent, and a file you have edited
yourself is left alone unless you pass --force.

Stdlib only, no network. Usage:

    python install/install.py <path-to-java-repo> [--force] [--dry-run]
"""

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
PAYLOAD = HERE.parent / "payload"

# Always replaced: it is the program, and the docs are tested against it.
ENGINE = ".claude/tdd/tddstate.py"

CLAUDE_BLOCK = PAYLOAD / ".claude" / "CLAUDE.tdd.md"
CLAUDE_BEGIN = "<!-- BEGIN tdd-memory-skill -->"
CLAUDE_END = "<!-- END tdd-memory-skill -->"

GITIGNORE_BEGIN = "# BEGIN tdd-memory-skill"
GITIGNORE_END = "# END tdd-memory-skill"
GITIGNORE_LINES = (".claude/tdd/state/", ".claude/tdd/logs/",
                   ".claude/tdd/snapshots/", ".claude/tdd/archive/",
                   ".claude/tdd/STATUS.md")

HOOK_MARKER = "tddstate.py"


class Report(object):
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.changed, self.skipped, self.problems = [], [], []

    def change(self, message):
        self.changed.append(message)
        print("%s %s" % ("would" if self.dry_run else "  +", message))

    def skip(self, message):
        self.skipped.append(message)
        print("  = %s" % message)

    def problem(self, message):
        self.problems.append(message)
        print("  ! %s" % message)


def payload_files():
    for path in sorted(PAYLOAD.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            yield path, path.relative_to(PAYLOAD).as_posix()


def copy_payload(repo, report, force):
    for source, rel in payload_files():
        if rel == ".claude/CLAUDE.tdd.md":
            continue  # merged into CLAUDE.md instead of copied
        destination = repo / rel
        body = source.read_bytes()
        if destination.is_file():
            if destination.read_bytes() == body:
                report.skip("%s unchanged" % rel)
                continue
            if rel != ENGINE and not force:
                report.problem("%s differs from the payload - kept yours "
                               "(pass --force to overwrite)" % rel)
                continue
        if not report.dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
        report.change("write %s" % rel)


def hook_block(python_exe):
    script = '"$CLAUDE_PROJECT_DIR/.claude/tdd/tddstate.py"'
    prefix = '"%s" %s' % (python_exe, script)

    def entry(rest, timeout, matcher=None):
        group = {"hooks": [{"type": "command",
                            "command": "%s %s" % (prefix, rest),
                            "timeout": timeout}]}
        if matcher:
            group["matcher"] = matcher
        return [group]

    return {
        # A fresh or reconnected session opens already knowing the position.
        "SessionStart": entry("resume --brief --quiet-if-idle", 15),
        # The pointer survives compaction.
        "PreCompact": entry("resume --brief", 15),
        # STATUS.md is current whenever the human looks.
        "Stop": entry("checkpoint --quiet", 15),
        # An edit to a watched file invalidates the gate.
        "PostToolUse": entry("touch --from-hook", 10, "Edit|Write|MultiEdit"),
    }


def merge_hooks(settings, ours):
    """Add our hooks, drop any previous copy of them, keep everyone else's."""
    hooks = settings.setdefault("hooks", {})
    for event, groups in ours.items():
        kept = []
        for group in hooks.get(event) or []:
            entries = [h for h in (group.get("hooks") or [])
                       if HOOK_MARKER not in (h.get("command") or "")]
            if entries:
                survivor = dict(group)
                survivor["hooks"] = entries
                kept.append(survivor)
        hooks[event] = kept + groups
    return settings


def install_hooks(repo, report, python_exe):
    path = repo / ".claude" / "settings.json"
    existing, raw = {}, None
    if path.is_file():
        raw = path.read_text(encoding="utf-8")
        try:
            existing = json.loads(raw)
        except ValueError:
            report.problem("settings.json is not valid JSON - hooks not installed")
            return
        if not isinstance(existing, dict):
            report.problem("settings.json is not an object - hooks not installed")
            return

    merged = merge_hooks(json.loads(json.dumps(existing)), hook_block(python_exe))
    body = json.dumps(merged, indent=2) + "\n"
    if raw is not None and json.loads(raw) == merged:
        report.skip(".claude/settings.json hooks already current")
        return
    if not report.dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            (path.parent / "settings.json.bak").write_text(raw, encoding="utf-8")
        path.write_text(body, encoding="utf-8")
    report.change("merge hooks into .claude/settings.json%s"
                  % (" (previous saved as settings.json.bak)" if raw else ""))


def upsert_block(path, begin, end, block, report, label):
    """Replace the marked block if present, append it otherwise."""
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    if begin in current and end in current:
        head, _, rest = current.partition(begin)
        _, _, tail = rest.partition(end)
        updated = head + block + tail
    else:
        separator = "" if not current or current.endswith("\n\n") else \
            ("\n" if current.endswith("\n") else "\n\n")
        updated = current + separator + block + "\n"
    if updated == current:
        report.skip("%s already current" % label)
        return
    if not report.dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(updated, encoding="utf-8")
    report.change("%s %s" % ("update" if begin in current else "add", label))


def install_gitignore(repo, report):
    block = "\n".join([GITIGNORE_BEGIN] + list(GITIGNORE_LINES) + [GITIGNORE_END])
    upsert_block(repo / ".gitignore", GITIGNORE_BEGIN, GITIGNORE_END, block,
                 report, ".gitignore entries")


def install_claude_md(repo, report):
    block = CLAUDE_BLOCK.read_text(encoding="utf-8").strip()
    upsert_block(repo / "CLAUDE.md", CLAUDE_BEGIN, CLAUDE_END, block,
                 report, "CLAUDE.md protocol block")


def check_prerequisites(repo, report):
    if sys.version_info < (3, 8):
        report.problem("python %d.%d is too old; 3.8+ is required"
                       % sys.version_info[:2])
    wrappers = [n for n in ("gradlew", "gradlew.bat") if (repo / n).is_file()]
    if not wrappers and not shutil.which("gradle"):
        report.problem("no Gradle wrapper in %s and no `gradle` on PATH - the "
                       "tracker will install, but `run` cannot work yet" % repo)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Install the TDD memory skill into a Java repository.")
    parser.add_argument("repo", help="path to the Java repository")
    parser.add_argument("--force", action="store_true",
                        help="overwrite payload files you have edited")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change and write nothing")
    parser.add_argument("--no-hooks", action="store_true",
                        help="skip .claude/settings.json")
    parser.add_argument("--no-doctor", action="store_true",
                        help="skip the environment check at the end")
    parser.add_argument("--python", default=None,
                        help="interpreter to bake into the hook commands "
                             "(default: the one running this script)")
    args = parser.parse_args(argv)

    repo = pathlib.Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        print("install: %s is not a directory" % repo, file=sys.stderr)
        return 1
    if not PAYLOAD.is_dir():
        print("install: payload missing at %s" % PAYLOAD, file=sys.stderr)
        return 1

    # Forward slashes: Windows accepts them and they need no JSON escaping.
    python_exe = pathlib.Path(args.python or sys.executable).as_posix()

    report = Report(args.dry_run)
    print("installing into %s%s" % (repo, "  (dry run)" if args.dry_run else ""))
    check_prerequisites(repo, report)
    copy_payload(repo, report, args.force)
    if not args.no_hooks:
        install_hooks(repo, report, python_exe)
    install_gitignore(repo, report)
    install_claude_md(repo, report)

    print("%d change(s), %d already current, %d problem(s)"
          % (len(report.changed), len(report.skipped), len(report.problems)))

    if args.no_doctor or args.dry_run:
        print("next: python .claude/tdd/tddstate.py doctor")
        return 0
    print()
    return subprocess.call([sys.executable, str(repo / ENGINE), "doctor"],
                           cwd=str(repo))


if __name__ == "__main__":
    sys.exit(main())
