#!/usr/bin/env python3
"""TokenRPG collector: turns local Claude Code logs into daily token totals.

Reads ~/.claude/projects/**/*.jsonl, plus any other Claude account's folder the player chose
(one per account, e.g. ~/.claude-work used through CLAUDE_CONFIG_DIR). Each folder uploads as
its own machine row. Runs on Linux, macOS and WSL with any Python 3.9+. Only numbers, project folder names
and a machine label leave this script (the label is the computer's name, plus the account folder's
name, e.g. "claude-work", seen only by the player and the site admin): never prompts, code, file paths or
message content. Standard library only, so a friend can drop the single file anywhere.

  tally_tokens.py                 print the last 28 days as JSON (keyed by folder if there are several)
  tally_tokens.py --days 0        print everything still on disk
  tally_tokens.py --push          upload to the server in ~/.config/tokenrpg/config.json
  tally_tokens.py --no-projects   leave project names out
  tally_tokens.py --install-hook  make Claude Code upload automatically (edits each folder's settings.json)
  tally_tokens.py --add-dir DIR   also count a Claude Code folder kept somewhere unusual
                                  (one on a Windows drive, /mnt/..., is read but never given hooks)

Days are local time. Streaming writes a message more than once, so usage is
de-duplicated on message id. The default 28-day window stays inside Claude Code's
30-day log cleanup, so a partly deleted day is never sent over a complete one.
"""
import argparse
import glob
import hashlib
import json
import os
import platform
import shlex
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta

VERSION = 10  # bump on every change; the server tells older copies to update themselves
USER_AGENT = f"tokenrpg-collector/{VERSION} (+https://tokenrpg.tinfoilhatsociety.cc)"
HERE = os.path.abspath(__file__)
LOG = os.path.join(os.path.dirname(HERE), "runs.log")
CONFIG = os.environ.get("TOKENRPG_CONFIG") or os.path.expanduser("~/.config/tokenrpg/config.json")
# Folders added with --add-dir. Not in config.json, because step 1 rewrites that file.
DIRS_FILE = os.path.join(os.path.dirname(CONFIG), "claude-dirs.txt")
# When the last upload started; the after-each-reply hook skips if it is recent.
LAST_UPLOAD = os.path.join(os.path.dirname(CONFIG), "last-upload")
# The Claude Code events that upload, and how often at most (seconds). Stop fires after every
# reply, so it is throttled; a session's start and end always upload.
HOOK_EVENTS = {"SessionStart": None, "SessionEnd": None, "Stop": 600}
# Where WSL mounts the Windows drives (/mnt/c, /mnt/d...). A Claude folder in there belongs to
# Windows's Claude Code, which would try to run our Linux hook command, so it is read but never
# given hooks. Only on WSL: on plain Linux, /mnt/<anything> is an ordinary disk.
WINDOWS_DRIVES = "/mnt"
FIELDS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_write": "cache_creation_input_tokens",
    "cache_read": "cache_read_input_tokens",
}


GENERIC = {"app", "src", "web", "webapp", "site", "frontend", "backend", "server", "client",
           "api", "code", "repo", "project", "dashboard", "admin", "scripts", "tools"}


def project_name(cwd):
    """Last folder name; 'parent/app' when the last one says nothing on its own. Splits on both
    slashes: transcripts from Windows Claude Code (read through WSL) hold C:\\Users\\... paths."""
    parts = [p for p in (cwd or "").replace("\\", "/").split("/") if p]
    if not parts:
        return "~"
    if parts[-1].lower() in GENERIC and len(parts) > 1:
        return f"{parts[-2]}/{parts[-1]}"
    return parts[-1]


def new_day():
    return {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "messages": 0,
            "models": defaultdict(int), "hours": defaultdict(int), "projects": defaultdict(int)}


def collect(window_days, projects=True, base=None, seen=None):
    """Daily totals for one Claude Code folder. Pass one `seen` set to every folder in a run, in
    claude_dirs() order: a message found in two folders then stays with the earlier one."""
    since = None
    if window_days:
        since = (datetime.now() - timedelta(days=window_days - 1)).date().isoformat()
        cutoff = time.time() - (window_days + 1) * 86400
    seen = set() if seen is None else seen
    days = defaultdict(new_day)
    base = base or default_dir()
    for path in glob.glob(os.path.join(glob.escape(base), "projects", "**", "*.jsonl"), recursive=True):
        if since and os.path.getmtime(path) < cutoff:
            continue
        with open(path, errors="ignore") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                msg = rec.get("message") or {}
                u, mid, ts = msg.get("usage"), msg.get("id"), rec.get("timestamp")
                if not (isinstance(u, dict) and mid and ts) or mid in seen:
                    continue
                seen.add(mid)
                try:
                    local = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
                except ValueError:
                    continue
                key = local.date().isoformat()
                if since and key < since:
                    continue
                d = days[key]
                total = 0
                for ours, theirs in FIELDS.items():
                    v = u.get(theirs) or 0
                    d[ours] += v
                    total += v
                d["messages"] += 1
                d["models"][msg.get("model") or "unknown"] += total
                d["hours"][str(local.hour)] += total
                if projects:
                    d["projects"][project_name(rec.get("cwd"))] += total
    for d in days.values():
        d["models"].pop("<synthetic>", None)
        for k in ("models", "hours", "projects"):
            d[k] = dict(d[k])
    return dict(sorted(days.items()))


# ---------- which Claude Code folders? one per account ----------

# Uploaded: ~/.claude, the folders the player said yes to (claude-dirs.txt, in the order they
# were added) and $CLAUDE_CONFIG_DIR. Other account folders are only *offered*, and only when a
# person runs setup in a terminal, so a self-updated collector never starts uploading a folder
# (an employer's account, say) that nobody chose. The order only ever grows at the end, and a
# message in two folders stays with the earlier one, so adding a folder never moves usage the
# server already has onto a second machine.

def default_dir():
    return os.path.expanduser("~/.claude")


def _canon(path):
    return os.path.realpath(os.path.expanduser(path))


def _ident(path):
    """What makes two names one folder: its inode (catches symlinks, trailing slashes and
    letter case on macOS), or its real path when it doesn't exist yet."""
    try:
        st = os.stat(os.path.expanduser(path))
        return (st.st_dev, st.st_ino)
    except OSError:
        return _canon(path)


def is_default(base):
    return _ident(base) == _ident(default_dir())


def switching_by_symlink():
    """~/.claude repointed between accounts: its folder changes under it, so the same usage
    would land on two machines. Such a machine uploads ~/.claude alone, as v6 did."""
    return os.path.islink(default_dir())


def short(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def _dirs_file():
    """[(path, counted)]: folders the player added, and ones they declined ('-' in front)."""
    try:
        with open(DIRS_FILE) as fh:
            lines = [line.strip() for line in fh if line.strip()]
    except OSError:
        return []
    return [(line[1:].strip(), False) if line.startswith("-") else (line, True) for line in lines]


def _remember(path, counted):
    entries = _dirs_file()
    if any(_ident(p) == _ident(path) and c == counted for p, c in entries):
        return                       # never reorder: order decides which folder keeps a message
    entries = [(p, c) for p, c in entries if _ident(p) != _ident(path)] + [(path, counted)]
    os.makedirs(os.path.dirname(DIRS_FILE), exist_ok=True)
    tmp = DIRS_FILE + ".tmp"
    with open(tmp, "w") as fh:
        fh.writelines(("" if c else "-") + p + "\n" for p, c in entries)
    os.replace(tmp, DIRS_FILE)


def claude_dirs(for_hooks=False):
    """The folders to upload, ~/.claude first, as the paths that name them. Uploads need
    projects/; hooks go into any named folder, since a new account has no projects/ yet."""
    if switching_by_symlink():
        return [default_dir()] if for_hooks or os.path.isdir(os.path.join(default_dir(), "projects")) else []
    env = os.environ.get("CLAUDE_CONFIG_DIR") or ""
    named = [p for p, counted in _dirs_file() if counted] + [env]
    out, idents = [], set()
    for path in [default_dir()] + named:
        if not path:
            continue
        full = os.path.expanduser(path)
        if for_hooks:
            # ~/.claude only if it is in use or nothing else is named (v6 hooked $CLAUDE_CONFIG_DIR alone)
            if path == default_dir() and not os.path.isdir(full) and any(named):
                continue
        elif not os.path.isdir(os.path.join(full, "projects")):
            continue
        ident = _ident(path)
        if ident in idents:
            continue
        idents.add(ident)
        out.append(default_dir() if is_default(path) else _canon(path))
    return out


def _sessions(base):
    return {os.path.basename(p) for p in
            glob.glob(os.path.join(glob.escape(base), "projects", "**", "*.jsonl"), recursive=True)}


def copy_of(path, others=None):
    """The counted folder `path` shares session files with, if any. Another account never
    does, a copied folder does, and a copy's old days would be counted twice."""
    mine = _sessions(path)
    for other in claude_dirs() if others is None else others:
        if _ident(other) != _ident(path) and mine & _sessions(other):
            return other
    return None


def discover():
    """Account folders not counted or declined yet: ~/.claude* and ~/.config/claude* holding
    projects/ and .claude.json. Claude Code keeps .claude.json inside a CLAUDE_CONFIG_DIR
    folder; a backup of ~/.claude has none, and a copy of a counted folder is skipped."""
    if switching_by_symlink():
        return []
    known = {_ident(p) for p in claude_dirs(for_hooks=True)} | {_ident(p) for p, _ in _dirs_file()}
    home = glob.escape(os.path.expanduser("~"))
    found = []
    for pattern in (".claude*", os.path.join(".config", "claude*")):
        for path in sorted(glob.glob(os.path.join(home, pattern))):
            if (_ident(path) in known or not os.path.isdir(os.path.join(path, "projects"))
                    or not os.path.isfile(os.path.join(path, ".claude.json"))):
                continue
            known.add(_ident(path))
            twin = copy_of(path, claude_dirs() + found)
            if twin:
                say(f"  Skipped {short(path)}: it shares sessions with {short(twin)}, so it looks like a copy.")
            else:
                found.append(_canon(path))
    return found


def offer_new_dirs():
    """Run by hand only. Asks about each new account folder; without a terminal to ask in,
    says how to add it instead."""
    for path in discover():
        if not sys.stdin.isatty():
            say(f"Found another Claude account folder, {short(path)}; not counted. To count it:\n"
                f"  python3 {shlex.quote(HERE)} --add-dir {shlex.quote(path)}")
            continue
        answer = None
        for _ in range(3):
            try:
                reply = input(f"Also count the Claude account in {short(path)}? Its tokens and project "
                              "names go to your hero. [Y/n] ").strip().lower()
            except EOFError:
                break
            if reply in ("", "y", "yes", "n", "no"):
                answer = reply in ("", "y", "yes")
                break
        _remember(path, bool(answer))
        say(f"✓ Counting {short(path)}." if answer else
            f"  Left out {short(path)}. To count it later: --add-dir {shlex.quote(path)}")


# ---------- which machine is this? ----------

def _machine_key():
    """The OS's own stable machine id, so a reinstall of the collector is the same machine."""
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):       # Linux, WSL
        try:
            with open(path) as fh:
                key = fh.read().strip()
            if key:
                return key
        except OSError:
            pass
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                                 capture_output=True, text=True, timeout=10).stdout
            for line in out.splitlines():
                if "IOPlatformUUID" in line:
                    return line.split('"')[-2]
        except (OSError, subprocess.SubprocessError, IndexError):
            pass
    return None


def device(base=None):
    """{"id", "label"} for one Claude Code folder. The id is a one-way hash of the machine id
    plus this user's home and the folder: stable across reinstalls, distinct per OS user and per
    account, and it reveals nothing about the machine. ~/.claude hashes exactly as collector v6
    did, so existing machines keep their rows (a new id would count their history twice). Other
    folders hash their real path, so one folder has one id however it was found. With no machine
    id available, a random id is kept next to the config."""
    folder = default_dir() if is_default(base or default_dir()) else _canon(base)
    key = _machine_key()
    if key:
        dev_id = hashlib.sha256(f"tokenrpg:{key}:{os.path.expanduser('~')}:{folder}".encode()).hexdigest()[:32]
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
        if folder != default_dir():
            dev_id = hashlib.sha256(f"tokenrpg:{dev_id}:{folder}".encode()).hexdigest()[:32]
    host = (platform.node() or "this computer").split(".")[0]
    if folder == default_dir():
        label = host[:40]
    else:
        label = f"{host[:24]} · {os.path.basename(folder).lstrip('.')[:13]}"
    return {"id": dev_id, "label": label}


# ---------- which plan is this machine on? ----------

PLAN_FIELDS = ("organizationType", "organizationRateLimitTier", "subscriptionCreatedAt", "billingType")


def plan(base=None):
    """A few named plan fields for one folder's account, nothing else. A CLAUDE_CONFIG_DIR folder
    keeps its .claude.json inside; ~/.claude's is ~/.claude.json, or inside it if someone set
    CLAUDE_CONFIG_DIR=~/.claude, so the one Claude Code wrote last wins. Claude Code keeps the
    login tokens in a neighbouring file (or the macOS Keychain); they are never read into what we send."""
    base = base or default_dir()
    cfg_file = os.path.join(base, ".claude.json")
    if is_default(base):
        files = [f for f in (os.path.expanduser("~/.claude.json"), cfg_file) if os.path.isfile(f)]
        cfg_file = max(files, key=os.path.getmtime) if files else cfg_file
    out = {}
    try:
        with open(cfg_file) as fh:
            acct = json.load(fh).get("oauthAccount") or {}
        out = {k: str(acct[k])[:60] for k in PLAN_FIELDS if acct.get(k) is not None}
    except (OSError, ValueError, AttributeError):
        pass
    if "organizationType" not in out:        # Linux keeps a copy beside the login tokens
        try:
            with open(os.path.join(base, ".credentials.json")) as fh:
                oauth = json.load(fh).get("claudeAiOauth") or {}
            for ours, theirs in (("subscriptionType", "subscriptionType"), ("rateLimitTier", "rateLimitTier")):
                if oauth.get(theirs):
                    out[ours] = str(oauth[theirs])[:60]
        except (OSError, ValueError, AttributeError):
            pass
    return out


# ---------- output: the terminal when run by hand, runs.log when run by the hook ----------

QUIET = False


def say(msg):
    if QUIET:
        try:
            lines = []
            if os.path.exists(LOG):
                with open(LOG) as fh:
                    lines = fh.readlines()[-199:]
            lines.append(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
            with open(LOG, "w") as fh:
                fh.writelines(lines)
        except OSError:
            pass
    else:
        print(msg)


def fail(msg):
    say(msg)
    sys.exit(1)


# ---------- HTTP: urllib, falling back to curl when Python can't verify certificates ----------

def _is_cert_error(e):
    reason = getattr(e, "reason", e)
    return isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(reason)


def http(url, data=None, headers=None):
    """Return (status, body_bytes). Python from python.org on macOS ships without a CA bundle
    until 'Install Certificates' is run; curl uses the system keychain, so fall back to it."""
    headers = dict(headers or {}, **{"User-Agent": USER_AGENT})
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        if not (_is_cert_error(e) and shutil.which("curl")):
            raise
    cmd = ["curl", "-sS", "--max-time", "30", "-w", "\n%{http_code}"]
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    if data is not None:
        cmd += ["-X", "POST", "--data-binary", "@-"]
    # Drop Python-style CA overrides: if they are what broke urllib, they would break curl too.
    env = {k: v for k, v in os.environ.items() if k not in ("SSL_CERT_FILE", "SSL_CERT_DIR")}
    out = subprocess.run(cmd + [url], input=data or b"", capture_output=True, timeout=60, env=env)
    if out.returncode != 0:
        raise urllib.error.URLError(out.stderr.decode(errors="ignore").strip() or "curl failed")
    body, _, code = out.stdout.rpartition(b"\n")
    return int(code), body


# ---------- self-update ----------

def self_update(base_url, latest):
    try:
        status, body = http(base_url + "/collector.py")
        if status != 200 or b"def main" not in body:
            return say(f"tokenrpg: update to v{latest} skipped (download returned {status})")
        compile(body, "tally_tokens.py", "exec")  # refuse anything that isn't valid Python
        tmp = HERE + ".new"
        with open(tmp, "wb") as fh:
            fh.write(body)
        os.replace(tmp, HERE)
        say(f"tokenrpg: updated the collector from v{VERSION} to v{latest}")
    except (OSError, SyntaxError, urllib.error.URLError) as e:
        say(f"tokenrpg: update to v{latest} skipped ({e})")


# ---------- Claude Code hook ----------

def _q(path):
    """Shell-quote a path, leaving a leading ~/ to expand (the Settings page shows ~ paths)."""
    return "~/" + shlex.quote(path[2:]) if path.startswith("~/") else shlex.quote(path)


def hook_command(every=None, py=None, script=None):
    """Absolute interpreter + script paths: a hook started from the desktop app may not
    have the terminal's PATH. which('python3') is the stable symlink (it survives a
    Homebrew minor upgrade); sys.executable is the fallback."""
    py = py or shutil.which("python3") or sys.executable
    flag = f" --at-most-every {every}" if every else ""
    return f"{_q(py)} {_q(script or HERE)} --push --quiet{flag} >/dev/null 2>&1 || true"


def hook_settings(py=None, script=None):
    """The "hooks" block --install-hook merges in. The server shows the same block on the
    Settings page for people who edit settings.json by hand, so the two cannot disagree."""
    return {event: [{"hooks": [{"type": "command", "async": True, "timeout": 60,
                                "command": hook_command(every, py, script)}]}]
            for event, every in HOOK_EVENTS.items()}


def installed_hooks(base):
    """Which of our events this folder's settings.json has a hook for. Only these event
    names are sent, so the site can say whether a machine updates every 10 minutes; nothing
    else in settings.json (which can hold env vars and keys) is read into the upload."""
    try:
        with open(os.path.join(base, "settings.json")) as fh:
            hooks = json.load(fh).get("hooks") or {}
        return sorted(e for e in HOOK_EVENTS if "tally_tokens.py" in json.dumps(hooks.get(e) or []))
    except (OSError, ValueError, AttributeError):
        return []


def hooks_that_upload(base):
    """installed_hooks() for the folder whose hooks upload `base`. A folder on a Windows drive
    has none of its own: the Linux side's hooks ($CLAUDE_CONFIG_DIR, else ~/.claude) upload it,
    so those are the ones reported."""
    if not on_windows_drive(base):
        return installed_hooks(base)
    primary = os.environ.get("CLAUDE_CONFIG_DIR") or default_dir()
    if on_windows_drive(primary):
        primary = default_dir()
    return [] if on_windows_drive(primary) else installed_hooks(primary)


def install_hook(dirs=None):
    """Add SessionStart + SessionEnd upload hooks to the settings.json of every counted Claude
    Code folder, so a session in any account uploads them all. Run by hand with no folders
    given, it first offers any account folder not counted yet.

    Keeps every existing setting and hook, backs each file up first, skips a folder whose
    hook is already there, and refuses to touch a file that is not valid JSON."""
    if dirs is None:
        offer_new_dirs()
        dirs = claude_dirs(for_hooks=True) or [default_dir()]
    added, problems = False, []
    for base in dirs:
        result = _install_hook_in(base)
        if result is None:
            added = True
        elif result:
            problems.append(result)
    if added:
        say("  Takes effect from your next Claude Code session.")
    if problems:
        fail("\n".join(problems))


def is_wsl(version_file="/proc/version"):
    """True inside WSL, whose kernel names itself "microsoft" (WSL 1: "Microsoft")."""
    try:
        with open(version_file) as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False


def on_windows_drive(path):
    """True for a folder on a Windows drive seen from WSL: /mnt/<drive letter>/..., after
    resolving symlinks, so a link from the Linux home to a Windows folder counts too. A Linux
    disk such as /mnt/data never does, and nothing does outside WSL."""
    real = _canon(path)
    if not real.startswith(WINDOWS_DRIVES + "/"):
        return False
    drive = real[len(WINDOWS_DRIVES) + 1:].split("/", 1)[0]
    return len(drive) == 1 and "a" <= drive.lower() <= "z" and is_wsl()


def _install_hook_in(base):
    """None when hooks were added, "" when already there or skipped, else what went wrong."""
    where = short(base)
    # The folder, or a settings.json symlinked from it, on a Windows drive.
    if on_windows_drive(base) or on_windows_drive(os.path.join(base, "settings.json")):
        say(f"  {where} is on a Windows drive, so no hooks were added to it: they are Linux commands that "
            "Windows's Claude Code would try to run. Its history still uploads whenever Claude Code runs on the Linux side.")
        return ""
    # A symlinked settings.json (dotfiles, or shared by two accounts) stays a symlink.
    path = os.path.realpath(os.path.join(base, "settings.json"))
    settings = {}
    if os.path.exists(path):
        try:
            with open(path) as fh:
                settings = json.load(fh)
        except ValueError as e:
            return (f"tokenrpg: {path} is not valid JSON ({e}); left it untouched. "
                    "Fix it, then run --install-hook again.")
        if not isinstance(settings, dict):
            return f"tokenrpg: {path} is not a JSON object; left it untouched."
    hooks = settings.setdefault("hooks", {})
    added = []
    for event, entries in hook_settings().items():
        existing = hooks.setdefault(event, [])
        if "tally_tokens.py" in json.dumps(existing):  # present in every form we have written
            continue
        existing.extend(entries)
        added.append(event)
    if not added:
        say(f"✓ {where}: already set up. Claude Code uploads when a session starts or ends, and every ~10 minutes in between.")
        return ""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        backup = f"{path}.bak-tokenrpg-{time.strftime('%Y%m%d-%H%M%S')}"
        with open(path) as src, open(backup, "w") as dst:
            dst.write(src.read())
        say(f"  Backed up your settings to {backup}")
    tmp = path + ".tmp-tokenrpg"
    with open(tmp, "w") as fh:
        json.dump(settings, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    say(f"✓ {where}: Claude Code will now upload when a session starts or ends, and every ~10 minutes "
        f"while you work (added: {', '.join(added)}).")
    return None


def add_dir(path, projects=True):
    """Count a Claude account folder: remember it, hook it up (unless it is on a Windows drive)
    and upload its history (only its own, so no other folder's older days are re-sent)."""
    path = _canon(path)
    if switching_by_symlink():
        fail(f"tokenrpg: {short(default_dir())} is a symlink, so it is the only folder counted "
             "(repointing it would move usage between machines). Nothing changed.")
    if not os.path.isdir(os.path.join(path, "projects")):
        fail(f"tokenrpg: {short(path)} has no projects/ folder, so it isn't a Claude Code folder "
             "or it hasn't been used yet. Nothing changed.")
    twin = copy_of(path)
    if twin:
        fail(f"tokenrpg: {short(path)} shares sessions with {short(twin)}, so it looks like a copy; "
             "counting it would count those days twice. Nothing changed.")
    _remember(path, True)
    say(f"✓ Counting {short(path)}.")
    install_hook([path])
    if os.path.exists(CONFIG):
        seen, days = set(), {}
        for d in claude_dirs():          # earlier folders first, so they keep their messages
            got = collect(0, projects=projects, base=d, seen=seen)
            if _ident(d) == _ident(path):
                days = got
        if days:
            push([(path, days)])


# ---------- upload ----------

def uploaded_within(seconds):
    """True if an upload started less than `seconds` ago. A stamp up to a minute ahead is
    jitter and counts as recent; further ahead (the clock was moved back) counts as long ago,
    so a wrong clock never stops uploads."""
    try:
        with open(LAST_UPLOAD) as fh:
            age = time.time() - float(fh.read().strip())
    except (OSError, ValueError):
        return False
    return -60 <= age < seconds


def mark_upload():
    """Record that an upload is starting. Every upload marks it, so the throttled hook also
    skips right after a session's start or end upload."""
    try:
        os.makedirs(os.path.dirname(LAST_UPLOAD), exist_ok=True)
        tmp = LAST_UPLOAD + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(f"{time.time():.3f}\n")   # not rounded: a rounded-up stamp reads as the future
        os.replace(tmp, LAST_UPLOAD)
    except OSError:
        pass


def push(uploads):
    """uploads: [(folder, days)]. One request per folder: each is its own machine row."""
    try:
        with open(CONFIG) as fh:
            cfg = json.load(fh)
        base, token = cfg["url"].rstrip("/"), cfg["token"]
    except (OSError, ValueError, KeyError) as e:
        fail(f"tokenrpg: can't read {CONFIG}: {e}. Re-run step 1 from your Settings page.")
    handle, latest = None, 0
    for folder, days in uploads:
        body = json.dumps({"days": days, "device": device(folder), "plan": plan(folder),
                           "hooks": hooks_that_upload(folder), "client": f"tally_tokens/{VERSION}"}).encode()
        try:
            status, raw = http(base + "/api/ingest", body,
                               {"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        except urllib.error.URLError as e:
            fail(f"tokenrpg: can't reach {base}: {getattr(e, 'reason', e)}")
        text = raw.decode(errors="ignore")
        if status == 401:
            fail("tokenrpg: the server didn't recognise your upload token. Copy step 1 from your "
                 "Settings page again (the token may have been reissued).")
        if status != 200:
            hint = " (Cloudflare blocked the request)" if "error code: 10" in text else ""
            fail(f"tokenrpg: upload refused ({status}){hint}: {text[:200]}")
        try:
            result = json.loads(text)
        except ValueError:
            fail(f"tokenrpg: unexpected reply from the server: {text[:200]}")
        skipped = result.get("skipped") or {}
        if skipped:
            say(f"tokenrpg: {len(skipped)} day(s) refused by the server: {skipped}")
        handle, n = result.get("handle"), result.get("accepted", 0)
        span = f" ({min(days)} to {max(days)})" if days else ""
        where = f" from {result['device']}" if result.get("device") else ""
        badge = f", {result['plan']}" if result.get("plan") else ""
        say(f"✓ Uploaded {n} day{'s' if n != 1 else ''} of Claude Code usage{span}{where}{badge}.")
        latest = max(latest, result.get("collector_version") or 0)
    if not QUIET:
        say(f"  Your hero: {base}/u/{handle}")
    if latest > VERSION:
        self_update(base, latest)


def main():
    global QUIET
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=28, help="window in days, 0 = everything (default 28)")
    ap.add_argument("--push", action="store_true", help="upload instead of printing")
    ap.add_argument("--no-projects", action="store_true", help="don't send project folder names")
    ap.add_argument("--quiet", action="store_true", help=f"write to {LOG} instead of the terminal")
    ap.add_argument("--install-hook", action="store_true",
                    help="add upload hooks to each Claude Code folder's settings.json (safe to re-run)")
    ap.add_argument("--add-dir", metavar="DIR",
                    help="also count another Claude account's folder and upload its history (safe to re-run)")
    ap.add_argument("--at-most-every", type=int, metavar="SECONDS",
                    help="skip this upload if one started in the last SECONDS (the after-each-reply hook uses 600)")
    ap.add_argument("--version", action="version", version=f"tokenrpg collector v{VERSION}")
    args = ap.parse_args()
    QUIET = args.quiet
    if args.add_dir:
        add_dir(args.add_dir, projects=not args.no_projects)
        return
    if args.install_hook:
        install_hook()
        return
    if args.push:
        if args.at_most_every and uploaded_within(args.at_most_every):
            return                       # quietly: this runs after every reply
        mark_upload()
    if args.push and not QUIET:
        offer_new_dirs()                 # a person is at the terminal; the hook never asks
    dirs, seen = claude_dirs(), set()
    per = [(d, collect(args.days, projects=not args.no_projects, base=d, seen=seen)) for d in dirs]
    if not args.push:
        out = per[0][1] if len(per) == 1 else {short(d): days for d, days in per}
        json.dump(out, sys.stdout, indent=1)
        print()
        return
    per = [(d, days) for d, days in per if days]
    if not per:
        say("tokenrpg: no Claude Code usage found in ~/.claude/projects yet. "
            "Use Claude Code for a bit, then run this again.")
        return
    if len(dirs) > 1 and not QUIET:
        say(f"Counting {len(dirs)} Claude Code folders: {', '.join(short(d) for d in dirs)}")
    push(per)


if __name__ == "__main__":
    main()
