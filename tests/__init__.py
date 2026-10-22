"""The whole suite runs with MAIL_ALLOWLIST set (22oct2026.md 2d, Diamante's guard idea): whatever a test hands the real
mailer, it can only ever address these made-up ones (and the SMTP connection is faked besides)."""
import os

os.environ["MAIL_ALLOWLIST"] = ("friend@example.com,player@example.com,admin@example.com,rob@example.com,"
                                "someone@example.com,zzboss@x.com")
# Production sets SITE_URL, and account emails refuse to go without it outside debug and tests (accounts._base,
# review L10): the suite runs like production, at the test client's own address.
os.environ.setdefault("SITE_URL", "http://localhost")
