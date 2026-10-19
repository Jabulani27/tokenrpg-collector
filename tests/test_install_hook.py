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
        self._every = tt.UPLOAD_EVERY
        tt.UPLOAD_EVERY = os.path.join(self.dir, "upload-every")     # never the player's own

    def tearDown(self):
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        tt.UPLOAD_EVERY = self._every

    def run_it(self):
        with contextlib.redirect_stdout(io.StringIO()):
            tt.install_hook([self.dir])

    def load(self):
        with open(self.path) as fh:
            return json.load(fh)

    def test_creates_file_when_missing(self):
        self.run_it()
        s = self.load()
        self.assertEqual(list(s["hooks"]), ["SessionStart", "SessionEnd", "Stop", "PostToolUse"])
        for ev in s["hooks"]:
            self.assertEqual(s["hooks"][ev], [{"hooks": [{"type": "command", "async": True, "timeout": 60,
                                                          "command": tt.hook_command(ev)}]}])
            self.assertNotRegex(tt.hook_command(ev), r"\d{2,}")          # the hooks carry no number

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
        with open(self.path) as fh:
            first = fh.read()
        self.run_it()
        s = self.load()
        for ev in tt.HOOK_EVENTS:
            self.assertEqual(len(s["hooks"][ev]), 1)
        with open(self.path) as fh:
            self.assertEqual(fh.read(), first)
        self.assertEqual([f for f in os.listdir(self.dir) if ".bak-tokenrpg" in f], [])   # nothing to back up

    def ours(self, s):
        return {ev: [h["command"] for e in s["hooks"].get(ev, []) for h in e.get("hooks", [])
                     if "tally_tokens.py" in h.get("command", "")] for ev in tt.HOOK_EVENTS}

    def test_rerun_replaces_a_v9_setup_and_keeps_everyone_elses_hooks(self):
        # A v9 settings.json as step 3 wrote it on 2026-10-15, plus other people's hooks in every place
        # ours could sit: their own entry for the same event, and one of theirs merged into our entry.
        v9 = lambda extra="": {"hooks": [{"type": "command", "async": True, "timeout": 60, "command":  # noqa: E731
                                          "/usr/bin/python3 /home/x/.local/share/tokenrpg/tally_tokens.py --push --quiet"
                                          + extra + " >/dev/null 2>&1 || true"}]}
        guard = {"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.py"}]}
        their_stop = {"hooks": [{"type": "command", "command": "notify-send done"}]}
        merged = {"hooks": [{"type": "command", "command": "echo start"}] + v9()["hooks"]}
        their_post = {"matcher": "Edit", "hooks": [{"type": "command", "command": "fmt.sh"}]}
        before = {"theme": "dark", "hooks": {"PreToolUse": [guard], "SessionStart": [merged],
                                             "SessionEnd": [v9()], "Stop": [their_stop, v9(" --at-most-every 600")],
                                             "PostToolUse": [their_post]}}
        with open(self.path, "w") as fh:
            json.dump(before, fh)
        for _ in range(2):
            self.run_it()
            s = self.load()
            # the interpreter path the v9 hooks named is kept (a bare "python3" would not be)
            self.assertEqual(self.ours(s), {ev: [tt._command(ev, "/usr/bin/python3", tt._q(tt.HERE))]
                                            for ev in tt.HOOK_EVENTS})
            self.assertEqual(s["theme"], "dark")
            self.assertEqual(s["hooks"]["PreToolUse"], [guard])
            self.assertEqual(s["hooks"]["Stop"][0], their_stop)                   # in its place
            self.assertEqual(s["hooks"]["SessionStart"][0], {"hooks": [{"type": "command", "command": "echo start"}]})
            self.assertEqual(s["hooks"]["PostToolUse"][0], their_post)
            self.assertEqual(len(s["hooks"]["PostToolUse"]), 2)
        self.assertEqual(len([f for f in os.listdir(self.dir) if ".bak-tokenrpg" in f]), 1)   # second run: no change

    def test_rerun_keeps_no_projects_if_someone_added_it(self):
        with open(self.path, "w") as fh:
            json.dump({"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command":
                       "python3 ~/.local/share/tokenrpg/tally_tokens.py --push --quiet --no-projects"}]}]}}, fh)
        self.run_it()
        for ev, cmds in self.ours(self.load()).items():
            self.assertEqual(len(cmds), 1, ev)
            self.assertIn("--no-projects", cmds[0])

    def test_rerun_keeps_a_chosen_interpreter_and_settings_and_says_what_changed(self):
        # Review 2026-10-19: v10 left a hand-customised hook alone; v11's replace must keep the
        # VAR=value prefix and an interpreter given as a path, and print what it changed.
        custom = "TOKENRPG_CONFIG=/srv/x/cfg.json FOO='a b' /opt/py/bin/python3 ~/.local/share/tokenrpg/tally_tokens.py --push --quiet"
        bare = "python3 ~/.local/share/tokenrpg/tally_tokens.py --push --quiet >/dev/null 2>&1 || true"
        with open(self.path, "w") as fh:
            json.dump({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": custom}]}],
                                 "SessionEnd": [{"hooks": [{"type": "command", "command": bare}]}]}}, fh)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            tt.install_hook([self.dir])
        cmds = self.ours(self.load())
        here = tt._q(tt.HERE)
        self.assertEqual(cmds["Stop"], ["TOKENRPG_CONFIG=/srv/x/cfg.json FOO='a b' /opt/py/bin/python3 " + here
                                        + " --hook Stop >/dev/null 2>&1 || true"])
        self.assertEqual(cmds["SessionEnd"], [tt.hook_command("SessionEnd")])     # bare python3: the absolute one
        self.assertEqual(cmds["PostToolUse"], ["TOKENRPG_CONFIG=/srv/x/cfg.json FOO='a b' /opt/py/bin/python3 " + here
                                               + " --hook PostToolUse >/dev/null 2>&1 || true"])   # copied from ours
        text = out.getvalue()
        self.assertIn("Stop: " + custom, text)
        self.assertIn("SessionEnd: " + bare, text)
        before = open(self.path).read()
        with contextlib.redirect_stdout(io.StringIO()):
            tt.install_hook([self.dir])
        self.assertEqual(open(self.path).read(), before)                        # and stays put

    def test_hooks_not_in_claude_codes_shape_are_left_untouched(self):
        with open(self.path, "w") as fh:
            fh.write('{"hooks": {"Stop": {"oops": 1}}}')
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            tt.install_hook([self.dir])
        with open(self.path) as fh:
            self.assertEqual(fh.read(), '{"hooks": {"Stop": {"oops": 1}}}')

    def test_invalid_json_left_untouched(self):
        with open(self.path, "w") as fh:
            fh.write('{"theme": "dark",,}')
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            tt.install_hook([self.dir])
        with open(self.path) as fh:
            self.assertEqual(fh.read(), '{"theme": "dark",,}')


if __name__ == "__main__":
    unittest.main()
