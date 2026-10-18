"""Collector behaviours that exist because of real failures: Mac certificates, silent hooks,
stale copies. The curl-fallback test needs the local dev server on 127.0.0.1:5077."""
import importlib.util
import json
import os
import re
import shlex
import shutil
import ssl
import tempfile
import unittest
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
        py, script = shlex.split(tt.hook_command())[:2]
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
        with mock.patch.object(tt.urllib.request, "urlopen", side_effect=err):
            status, body = tt.http("http://127.0.0.1:5077/api/ingest", b'{"days":{}}',
                                   {"Content-Type": "application/json", "Authorization": "Bearer nope"})
        self.assertEqual(status, 401)                      # reached the app via curl
        self.assertEqual(json.loads(body)["error"], "bad upload token")

    def test_non_cert_errors_are_not_masked(self):
        tt = load()
        with mock.patch.object(tt.urllib.request, "urlopen",
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


if __name__ == "__main__":
    unittest.main()
