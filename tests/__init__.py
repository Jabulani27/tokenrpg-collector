"""The whole suite runs with MAIL_ALLOWLIST set (22oct2026.md 2d, Diamante's guard idea): whatever a test hands the real
mailer, it can only ever address these made-up ones (and the SMTP connection is faked besides)."""
import os

os.environ["MAIL_ALLOWLIST"] = ("friend@example.com,player@example.com,admin@example.com,rob@example.com,"
                                "someone@example.com,zzboss@x.com")
# Production sets SITE_URL, and account emails refuse to go without it outside debug and tests (accounts._base,
# review L10): the suite runs like production, at the test client's own address.
os.environ.setdefault("SITE_URL", "http://localhost")
# The app's default data folder is the checkout's data/ (app/db.py reads TOKENRPG_DATA at import). A module that imports
# the app before pointing it elsewhere, or a request made while the default path is back in place, wrote
# data/tokenrpg.sqlite there (second review L-8: nine modules did). The whole suite's default is a scratch folder
# instead, removed when the run ends; tests that set their own folder, or start a process with its own, are unchanged.
if "TOKENRPG_DATA" not in os.environ:
    import atexit
    import shutil
    import tempfile
    os.environ["TOKENRPG_DATA"] = tempfile.mkdtemp(prefix="tokenrpg-suite-")
    atexit.register(shutil.rmtree, os.environ["TOKENRPG_DATA"], True)
