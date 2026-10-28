"""Collector behaviours that exist because of real failures: Mac certificates, silent hooks,
stale copies. The curl-fallback test needs the local dev server on 127.0.0.1:5077."""
import importlib.util
import json
import os
import re
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.request
from unittest import mock

HERE = os.path.dirname(__file__)
SRC = os.path.join(HERE, "..", "collector", "tally_tokens.py")


def load(path=SRC):
    spec = importlib.util.spec_from_file_location("tally_tokens_t", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def dev_server_up():
    try:
        urllib.request.urlopen("http://127.0.0.1:5077/", timeout=2)
        return True
    except Exception:
        return False


class Robustness(unittest.TestCase):
    def test_hook_command_uses_absolute_paths(self):
        tt = load()
        py, script = shlex.split(tt.hook_command("Stop"))[:2]
        self.assertTrue(os.path.isabs(py), py)
        self.assertTrue(os.path.isabs(script) and script.endswith("tally_tokens.py"), script)

    def test_quiet_writes_log_not_terminal(self):
        tt = load()
        with tempfile.TemporaryDirectory() as d:
            tt.LOG, tt.QUIET = os.path.join(d, "runs.log"), True
            with mock.patch("builtins.print") as p:
                tt.say("hello from the hook")
            p.assert_not_called()
            self.assertIn("hello from the hook", open(tt.LOG).read())

    @unittest.skipUnless(dev_server_up() and shutil.which("curl"), "needs dev server + curl")
    def test_cert_failure_falls_back_to_curl(self):
        tt = load()
        err = urllib.error.URLError(ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED"))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            status, body = tt.http("http://127.0.0.1:5077/api/ingest", b'{"days":{}}',
                                   {"Content-Type": "application/json", "Authorization": "Bearer nope"})
        self.assertEqual(status, 401)                      # reached the app via curl
        self.assertEqual(json.loads(body)["error"], "bad upload token")

    def test_non_cert_errors_are_not_masked(self):
        tt = load()
        with mock.patch("urllib.request.urlopen",
                               side_effect=urllib.error.URLError("Name or service not known")):
            with self.assertRaises(urllib.error.URLError):
                tt.http("http://example.invalid/")



# The public copy every self-update is checked against (v14). Literal here too: changing the collector's is a decision.
PUBLIC = "https://raw.githubusercontent.com/Jabulani27/tokenrpg-collector/main/collector/tally_tokens.py"


def versioned(n, extra=""):
    with open(SRC, "rb") as fh:
        src = fh.read()
    return re.sub(rb"^VERSION = \d+", b"VERSION = %d" % n, src, count=1, flags=re.M) + extra.encode()


class WindowsCheckScripts(unittest.TestCase):
    """Lawfix review LOW (second pass): the Windows test script downloaded the collector from tokenrpg.com and ran it.
    Every script that fetches the collector must fetch PUBLIC_COPY, as Settings step 1 does."""

    def test_windows_scripts_download_the_collector_from_github(self):
        folder = os.path.join(HERE, "..", "windows-check")
        if not os.path.isdir(folder):
            self.skipTest("windows-check is not in the public copy")
        public = load().PUBLIC_COPY
        found = 0
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".ps1"):
                continue
            with open(os.path.join(folder, name), encoding="utf-8-sig") as fh:
                text = fh.read()
            self.assertNotRegex(text, r"tokenrpg\.com/collector\.py", name)
            for url in re.findall(r"\$CollectorUrl\s*=\s*'([^']*)'", text):
                self.assertEqual(url, public, name)
                found += 1
        self.assertGreaterEqual(found, 1)


class SelfUpdate(unittest.TestCase):
    """v14: an update is installed only when the server's file declares the announced version, is byte-identical to
    the public copy on GitHub and compiles. Every other case leaves the running file exactly as it was (fail closed),
    says why once, and tries again on a later run."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.copy = os.path.join(self.dir, "tally_tokens.py")
        shutil.copy(SRC, self.copy)
        with open(self.copy, "rb") as fh:
            self.before = fh.read()
        self.tt = load(self.copy)
        self.tt.QUIET, self.tt.LOG = True, os.path.join(self.dir, "runs.log")
        self.tt.UPDATE_SKIPPED = os.path.join(self.dir, "update-skipped")
        self.answers, self.asked = {}, []

    def http(self, url, data=None, headers=None):
        self.asked.append(url)
        answer = self.answers[url]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def update(self, server, github, latest=99):
        self.answers = {"https://server.example/collector.py": server, PUBLIC: github}
        with mock.patch.object(self.tt, "http", side_effect=self.http):
            self.tt.self_update("https://server.example", latest)

    def running(self):
        with open(self.copy, "rb") as fh:
            return fh.read()

    def log(self):
        try:
            with open(self.tt.LOG) as fh:
                return fh.read()
        except OSError:
            return ""

    def leftovers(self):
        return sorted(n for n in os.listdir(self.dir) if n.startswith("tally_tokens.py."))

    def assert_kept(self, why):
        self.assertEqual(self.running(), self.before)
        self.assertEqual(self.leftovers(), [])
        self.assertIn(f"update to v99 skipped ({why}", self.log())

    def test_install_writes_under_its_own_name_beside_the_file(self):
        """Lawfix review LOW: a fixed tally_tokens.py.new let two runs updating at once write into, or install, each
        other's half-written file, and a leftover .new that can't be opened blocked every update. Now the temp file is
        in the same folder (so the swap is one rename) and named after this process."""
        blocker = self.copy + ".new"
        os.mkdir(blocker)                     # what a fixed name can't get past; also any other run's .new
        replaced = []
        real_replace = os.replace

        def spy(src, dst):
            replaced.append((src, dst))
            return real_replace(src, dst)
        new = versioned(99)
        with mock.patch.object(self.tt.os, "replace", side_effect=spy):
            self.update((200, new), (200, new))
        self.assertEqual(self.running(), new)
        self.assertEqual(len(replaced), 1)
        tmp = replaced[0][0]
        self.assertEqual(replaced[0][1], self.copy)
        self.assertEqual(os.path.dirname(tmp), self.dir)
        self.assertIn(f".{os.getpid()}.", os.path.basename(tmp))
        self.assertEqual(self.leftovers(), ["tally_tokens.py.new"])          # the blocker, untouched

    def test_install_flushes_the_temp_file_to_disk_before_the_swap(self):
        """Lawfix review LOW (second pass): without fsync a power cut just after os.replace can leave an empty or
        partial tally_tokens.py, and every hook then fails to start."""
        order = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            order.append(("fsync", os.readlink(f"/proc/self/fd/{fd}") if os.path.exists("/proc/self/fd") else fd))
            return real_fsync(fd)

        def replace(src, dst):
            order.append(("replace", src))
            return real_replace(src, dst)
        new = versioned(99)
        with mock.patch.object(self.tt.os, "fsync", side_effect=fsync), \
                mock.patch.object(self.tt.os, "replace", side_effect=replace):
            self.update((200, new), (200, new))
        self.assertEqual(self.running(), new)
        self.assertEqual([k for k, _ in order], ["fsync", "replace"])
        if os.path.exists("/proc/self/fd"):
            self.assertEqual(order[0][1], order[1][1])                    # the temp file itself, not some other fd

    def test_install_sweeps_install_temps_left_over_an_hour_ago(self):
        """A run killed between write and replace leaves tally_tokens.py.<pid>.tmp behind; the next install deletes
        those over an hour old. A younger one may be another run's install in progress, so it stays."""
        def make(name, age):
            path = os.path.join(self.dir, name)
            with open(path, "wb") as fh:
                fh.write(b"half")
            os.utime(path, (time.time() - age, time.time() - age))
        make("tally_tokens.py.111.tmp", 7200)              # stale: swept
        make("tally_tokens.py.222.tmp", 60)                # young: another run's, kept
        make("tally_tokens.py.notes.tmp", 7200)            # not a pid temp: not ours to delete
        make("other.py.333.tmp", 7200)                     # another file's: kept
        new = versioned(99)
        self.update((200, new), (200, new))
        self.assertEqual(self.running(), new)
        self.assertEqual(self.leftovers(), ["tally_tokens.py.222.tmp", "tally_tokens.py.notes.tmp"])
        self.assertTrue(os.path.exists(os.path.join(self.dir, "other.py.333.tmp")))

    def test_an_odd_skip_note_reads_as_not_skipped_lately(self):
        """Lawfix review LOW: an 'at' too big for a float raised OverflowError out of the hook. Any odd note is "no"."""
        odd = ['{"version": 99, "at": 1' + "0" * 400 + '}', '{"version": 99, "at": "soon"}', '{"version": 99}',
               '{"version": 99, "at": [1]}', '[1, 2]', '"x"', 'not json', '\xff\xfe', "[" * 100000]
        for text in odd:
            with open(self.tt.UPDATE_SKIPPED, "w", encoding="latin-1") as fh:
                fh.write(text)
            self.assertFalse(self.tt._skipped_lately(99), text[:40])
        new = versioned(99)
        self.update((200, new), (200, new))                  # and the update still goes ahead
        self.assertEqual(self.running(), new)

    def test_the_public_copy_is_written_in_the_collector(self):
        self.assertEqual(self.tt.PUBLIC_COPY, PUBLIC)
        with open(SRC) as fh:
            src = fh.read()
        self.assertEqual(len(re.findall(r"PUBLIC_COPY\s*=(?!=)", src)), 1, "assigned once: nothing else may change it")
        self.assertNotIn("global PUBLIC_COPY", src)

    @unittest.skipUnless(os.path.exists(os.path.join(HERE, "..", "deploy.sh")), "deploy.sh is not in the public copy")
    def test_the_public_copy_is_where_deploy_puts_the_collector(self):
        with open(os.path.join(HERE, "..", "deploy.sh")) as fh:
            deploy = fh.read()
        repo = re.search(r"COLLECTOR_MIRROR_REMOTE:-git@github\.com:([\w.-]+/[\w.-]+?)\.git\}", deploy).group(1)
        dest = re.search(r"^\s*collector/tally_tokens\.py:(\S+)$", deploy, re.M).group(1)
        self.assertEqual(PUBLIC, f"https://raw.githubusercontent.com/{repo}/main/{dest}")

    def test_applied_when_the_server_and_github_agree(self):
        new = versioned(99)
        self.update((200, new), (200, new))
        self.assertEqual(self.running(), new)
        self.assertEqual(self.asked, ["https://server.example/collector.py", PUBLIC])
        self.assertIn("updated the collector from v", self.log())

    def test_skipped_when_github_has_other_bytes(self):
        self.update((200, versioned(99)), (200, versioned(99, "\n# one byte more\n")))
        self.assert_kept("the server's file doesn't match the public copy on GitHub")

    def test_skipped_when_github_is_unreachable(self):
        self.update((200, versioned(99)), urllib.error.URLError("Name or service not known"))
        self.assert_kept("can't reach the public copy on GitHub")

    def test_skipped_when_github_has_no_such_file(self):
        self.update((200, versioned(99)), (404, b"404: Not Found"))
        self.assert_kept("the public copy on GitHub answered 404")

    def test_skipped_when_the_file_is_not_the_announced_version(self):
        for server_version in (98, 100):
            new = versioned(server_version)
            self.update((200, new), (200, new), latest=99)
            self.assert_kept("the server's file is not v99")
            os.unlink(self.tt.UPDATE_SKIPPED)                # or the second case would wait UPDATE_RETRY
            os.unlink(self.tt.LOG)

    def test_skipped_when_the_server_fails(self):
        self.update(urllib.error.URLError("timed out"), (200, versioned(99)))
        self.assert_kept("can't reach the server")
        self.assertEqual(self.asked, ["https://server.example/collector.py"])

    def test_still_refuses_invalid_python_both_sides_agree_on(self):
        broken = b"VERSION = 99\ndef main(:\n  broken\n"
        self.update((200, broken), (200, broken))
        self.assert_kept("the file isn't valid Python")

    def test_a_skip_is_logged_once_and_retried_later_not_on_every_upload(self):
        """GitHub's raw files are cached for up to 5 minutes: a release can read as a mismatch for a while. A hook
        uploads every 2 minutes; it must not fetch twice a time or fill runs.log, and must pick the release up."""
        new = versioned(99)
        stale = (200, self.before)
        self.update((200, new), stale)
        self.update((200, new), stale)                       # within UPDATE_RETRY: nothing fetched
        self.assertEqual(len(self.asked), 2)
        self.assertEqual(self.log().count("skipped"), 1)
        with open(self.tt.UPDATE_SKIPPED) as fh:
            note = json.load(fh)
        note["at"] -= self.tt.UPDATE_RETRY + 1
        with open(self.tt.UPDATE_SKIPPED, "w") as fh:
            json.dump(note, fh)
        self.update((200, new), stale)                       # tried again, the same reason not logged again
        self.assertEqual(len(self.asked), 4)
        self.assertEqual(self.log().count("skipped"), 1)
        self.assertEqual(self.running(), self.before)
        note["at"] -= self.tt.UPDATE_RETRY + 1
        with open(self.tt.UPDATE_SKIPPED, "w") as fh:
            json.dump(note, fh)
        self.update((200, new), (200, new))                  # GitHub caught up
        self.assertEqual(self.running(), new)
        self.assertFalse(os.path.exists(self.tt.UPDATE_SKIPPED))

    def test_a_newer_version_or_a_hand_run_is_tried_at_once(self):
        stale = (200, self.before)
        self.update((200, versioned(99)), stale)
        self.update((200, versioned(100)), stale, latest=100)
        self.assertEqual(len(self.asked), 4)
        self.tt.QUIET = False
        with mock.patch("builtins.print") as shown:
            self.update((200, versioned(100)), stale, latest=100)
        self.assertEqual(len(self.asked), 6)
        self.assertIn("skipped", shown.call_args[0][0])


class HookAsClaudeRunsIt(unittest.TestCase):
    """The real script, started the way Claude Code starts a hook, against a local server that
    takes 2 s to answer: the hook returns at once and the upload lands afterwards (v11)."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        log = os.path.join(self.tmp, ".claude", "projects", "p")
        os.makedirs(log)
        with open(os.path.join(log, "s.jsonl"), "w") as fh:
            fh.write(json.dumps({"timestamp": "2026-10-01T12:00:00Z", "cwd": "/x/p", "message": {
                "id": "m1", "model": "m", "usage": {"cache_read_input_tokens": 5}}}) + "\n")
        self.cfg = os.path.join(self.tmp, ".config", "tokenrpg")
        os.makedirs(self.cfg)
        self.script = os.path.join(self.tmp, "share", "tally_tokens.py")       # runs.log goes beside it
        os.makedirs(os.path.dirname(self.script))
        shutil.copy(SRC, self.script)
        self.posts, self.arrived, self.delays = [], [], []
        posts, arrived, delays = self.posts, self.arrived, self.delays

        class Slow(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                posts.append(time.time())
                time.sleep(delays.pop(0) if delays else 2)
                arrived.append(body["days"])            # in the order the server stores them
                body = json.dumps({"accepted": 1, "handle": "h", "upload_every": 90, "collector_version": 0}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        with open(os.path.join(self.cfg, "config.json"), "w") as fh:
            json.dump({"url": f"http://127.0.0.1:{self.httpd.server_port}", "token": "t"}, fh)
        self.env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CONFIG_DIR", "TOKENRPG_CONFIG")}
        self.env.update(HOME=self.tmp, TOKENRPG_CONFIG=os.path.join(self.cfg, "config.json"))

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def hook(self, event):
        cmd = f"{shlex.quote(sys.executable)} {shlex.quote(self.script)} --hook {event} >/dev/null 2>&1 || true"
        start = time.time()
        subprocess.run(["sh", "-c", cmd], input=b'{"hook_event_name": "%s"}' % event.encode(), env=self.env,
                       timeout=30, check=True)
        return time.time() - start

    def wait_for(self, check, seconds=15):
        end = time.time() + seconds
        while time.time() < end and not check():
            time.sleep(0.05)
        return check()

    def test_the_hook_returns_before_the_upload_and_the_next_one_is_skipped(self):
        self.delays.append(4)
        took = self.hook("PostToolUse")
        # Returned while the upload was still in flight: the server had not answered yet. (A wall-clock
        # bound alone failed once in a loaded full-suite run: 2.1 s against 1.5.)
        self.assertEqual(self.arrived, [])
        self.assertLess(took, 4)
        every = os.path.join(self.cfg, "upload-every")
        self.assertTrue(self.wait_for(lambda: os.path.exists(every)), "the upload never finished")
        with open(every) as fh:
            self.assertEqual(fh.read().strip(), "90")               # the reply's rate, kept
        self.assertEqual(len(self.posts), 1)
        self.assertLess(self.hook("PostToolUse"), 1.5)              # seconds later: skipped
        self.assertLess(self.hook("Stop"), 1.5)
        time.sleep(0.5)
        self.assertEqual(len(self.posts), 1)
        self.hook("SessionEnd")                                     # never throttled
        self.assertTrue(self.wait_for(lambda: len(self.posts) == 2))
        self.assertTrue(self.wait_for(lambda: self.uploaded() == 1), "the SessionEnd upload never finished")
        time.sleep(0.3)
        self.assertEqual(self.uploaded(), 1)       # the throttled one landed (posts == 2) but isn't logged
        self.assertTrue(os.path.exists(os.path.join(self.cfg, "log-cache.json")))

    def test_a_later_upload_never_lands_before_an_earlier_slow_one(self):
        # Review 2026-10-19: a slow mid-reply upload arrived after SessionEnd and put the older
        # numbers back (INSERT OR REPLACE keeps the last). Uploads now hold upload.lock.
        self.delays.extend([2, 0])
        self.hook("PostToolUse")                                    # due: posts, and the server takes 2 s
        self.assertTrue(self.wait_for(lambda: len(self.posts) == 1))
        with open(os.path.join(self.tmp, ".claude", "projects", "p", "s.jsonl"), "a") as fh:
            fh.write(json.dumps({"timestamp": "2026-10-01T13:00:00Z", "cwd": "/x/p", "message": {
                "id": "m2", "model": "m", "usage": {"cache_read_input_tokens": 1000}}}) + "\n")
        self.hook("SessionEnd")                                     # the session's last word
        self.assertTrue(self.wait_for(lambda: len(self.arrived) == 2), "an upload never landed")
        self.assertEqual([d["2026-10-01"]["cache_read"] for d in self.arrived], [5, 1005])
        self.assertTrue(self.wait_for(lambda: self.uploaded() == 1))

    def uploaded(self):
        try:
            with open(os.path.join(os.path.dirname(self.script), "runs.log")) as fh:
                return fh.read().count("Uploaded 1 day")
        except OSError:
            return 0


if __name__ == "__main__":
    unittest.main()
