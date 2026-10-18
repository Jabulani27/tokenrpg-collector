"""--install-hook must never damage a player's Claude Code settings."""
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest

_spec = importlib.util.spec_from_file_location(
    "tally_tokens", os.path.join(os.path.dirname(__file__), "..", "collector", "tally_tokens.py"))
tt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tt)


class InstallHookTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "settings.json")
        os.environ["CLAUDE_CONFIG_DIR"] = self.dir

    def tearDown(self):
        os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def run_it(self):
        with contextlib.redirect_stdout(io.StringIO()):
            tt.install_hook([self.dir])

    def load(self):
        with open(self.path) as fh:
            return json.load(fh)

    def test_creates_file_when_missing(self):
        self.run_it()
        s = self.load()
        for ev in ("SessionStart", "SessionEnd"):
            self.assertEqual(s["hooks"][ev][0]["hooks"][0]["command"], tt.hook_command())

    def test_keeps_existing_settings_and_hooks(self):
        guard = {"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.py"}]}
        start = {"hooks": [{"type": "command", "command": "other-start"}]}
        with open(self.path, "w") as fh:
            json.dump({"theme": "dark", "permissions": {"allow": ["Read"]},
                       "hooks": {"PreToolUse": [guard], "SessionStart": [start]}}, fh)
        self.run_it()
        s = self.load()
        self.assertEqual(s["theme"], "dark")
        self.assertEqual(s["permissions"], {"allow": ["Read"]})
        self.assertEqual(s["hooks"]["PreToolUse"], [guard])
        self.assertEqual(s["hooks"]["SessionStart"][0], start)
        self.assertEqual(len(s["hooks"]["SessionStart"]), 2)
        self.assertTrue(any(f.startswith("settings.json.bak-tokenrpg") for f in os.listdir(self.dir)))

    def test_idempotent(self):
        self.run_it()
        self.run_it()
        s = self.load()
        self.assertEqual(len(s["hooks"]["SessionStart"]), 1)
        self.assertEqual(len(s["hooks"]["SessionEnd"]), 1)

    def test_invalid_json_left_untouched(self):
        with open(self.path, "w") as fh:
            fh.write('{"theme": "dark",,}')
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            tt.install_hook([self.dir])
        with open(self.path) as fh:
            self.assertEqual(fh.read(), '{"theme": "dark",,}')


if __name__ == "__main__":
    unittest.main()
