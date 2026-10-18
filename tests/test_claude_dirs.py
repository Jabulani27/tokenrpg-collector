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
import unittest
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
        self.patches = [mock.patch.object(tt, "CONFIG", os.path.join(cfg, "config.json")),
                        mock.patch.object(tt, "DIRS_FILE", os.path.join(cfg, "claude-dirs.txt")),
                        mock.patch.object(tt, "_machine_key", return_value="machine-k"),
                        mock.patch.object(tt, "http", side_effect=self.fake_http),
                        mock.patch.object(tt, "QUIET", False),
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
        return 200, json.dumps({"accepted": len(body["days"]), "handle": "h", "device": body["device"]["label"]}).encode()

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

    def test_windows_paths_give_only_the_folder_name(self):
        self.assertEqual(tt.project_name(r"C:\Users\Pat\Clients\Acme Ltd\app"), "Acme Ltd/app")
        self.assertEqual(tt.project_name(r"C:\Users\Pat\Clients\Acme"), "Acme")
        self.assertEqual(tt.project_name("/home/pat/code/garden/app"), "garden/app")


if __name__ == "__main__":
    unittest.main()
