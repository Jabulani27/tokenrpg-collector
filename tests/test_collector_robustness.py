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

    def test_self_update_replaces_only_with_valid_python(self):
        with tempfile.TemporaryDirectory() as d:
            copy = os.path.join(d, "tally_tokens.py")
            shutil.copy(SRC, copy)
            tt = load(copy)
            tt.QUIET, tt.LOG = True, os.path.join(d, "runs.log")
            newer = re.sub(r"^VERSION = \d+", "VERSION = 99", open(SRC).read(), count=1, flags=re.M)
            with mock.patch.object(tt, "http", return_value=(200, b"def main(:\n  broken")):
                tt.self_update("http://x", 99)
            self.assertNotIn("VERSION = 99", open(copy).read())   # broken download refused
            with mock.patch.object(tt, "http", return_value=(200, newer.encode())):
                tt.self_update("http://x", 99)
            self.assertIn("VERSION = 99", open(copy).read())


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
