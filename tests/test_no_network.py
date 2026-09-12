"""DEVPLAN section 2 promises 'no network'. Enforce it mechanically."""

import ast
import unittest

from _harness import SRC

ALLOWED = {
    "argparse", "datetime", "hashlib", "json", "os", "pathlib", "re",
    "shutil", "subprocess", "sys", "time", "xml.etree.ElementTree",
}

FORBIDDEN_NAMES = (
    "urllib", "http", "socket", "requests", "ftplib", "smtplib",
    "telnetlib", "asyncio", "ssl", "webbrowser", "xmlrpc",
)


class TestImportWhitelist(unittest.TestCase):

    def setUp(self):
        self.tree = ast.parse(SRC.read_text(encoding="utf-8"), filename=str(SRC))

    def imported_modules(self):
        found = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    found.add(node.module)
        return found

    def test_only_whitelisted_modules_are_imported(self):
        extra = {m for m in self.imported_modules()
                 if m.split(".")[0] not in {a.split(".")[0] for a in ALLOWED}}
        self.assertEqual(extra, set(),
                         "tddstate.py imports outside the DEVPLAN whitelist: %s" % extra)

    def test_no_network_module_is_imported(self):
        for module in self.imported_modules():
            self.assertNotIn(module.split(".")[0], FORBIDDEN_NAMES)

    def test_no_dynamic_import_escape_hatch(self):
        source = SRC.read_text(encoding="utf-8")
        for needle in ("__import__", "importlib", "eval(", "exec("):
            self.assertNotIn(needle, source,
                             "%s would let the whitelist be bypassed" % needle)


if __name__ == "__main__":
    unittest.main()
