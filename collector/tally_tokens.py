#!/usr/bin/env python3
"""TokenRPG collector: turns local Claude Code logs into daily token totals.

Reads ~/.claude/projects/**/*.jsonl, plus any other Claude account's folder the player chose
(one per account, e.g. ~/.claude-work used through CLAUDE_CONFIG_DIR). Each folder uploads as
its own machine row. Runs on Linux, macOS and WSL with any Python 3.9+.

An upload (v13) holds, for each day, tokens by type, the number of replies, tokens by model and
tokens by hour; and once per folder: a machine id (a one-way hash), a label (the computer's name,
plus the account folder's name when it isn't ~/.claude, e.g. "claude-work"), the kind of computer
(mac, linux, wsl or windows), the plan fields in PLAN_FIELDS from .claude.json (when it has none,
subscriptionType and rateLimitTier from .credentials.json, as Linux keeps them, never the login
tokens beside them), which of our hooks the folder has (event names only) and this script's
version. The label and the kind are seen only by the player and the site's admins. Never prompts,
code, file contents, file paths, the names of the folders you work in (v12 stopped reading the
working folder) or message content. Standard library only, so a friend can drop the single file
anywhere.

  tally_tokens.py                 print the last 28 days as JSON (keyed by folder if there are several)
  tally_tokens.py --days 0        print everything still on disk
  tally_tokens.py --push          upload to the server in ~/.config/tokenrpg/config.json
  tally_tokens.py --install-hook  make Claude Code upload automatically (edits each folder's settings.json)
  tally_tokens.py --add-dir DIR   also count a Claude Code folder kept somewhere unusual
                                  (one on a Windows drive, /mnt/..., is read but never given hooks)
  tally_tokens.py --hook EVENT    what the hooks run: upload, at most as often as the server says

Days are local time. Streaming writes a message more than once, so usage is
de-duplicated on message id. The default 28-day window stays inside Claude Code's
30-day log cleanup, so a partly deleted day is never sent over a complete one. Files
unchanged since the last upload are not read again (log-cache.json beside the config,
written by uploads only; printing reads it but never writes it).

Step 1 on the site's Settings page downloads this file from the public copy on GitHub
(github.com/Jabulani27/tokenrpg-collector); the site fills in only the config line. When the site has
a newer version, an upload replaces this file with it, but only when the site's file is byte-identical
to that public copy (since v14). Otherwise this file stays as it is and tries again later.
"""
import os
import sys
import time

try:
    import fcntl
except ImportError:                  # not on native Windows, which isn't supported anyway
    fcntl = None

VERSION = 14  # bump on every change; the server tells older copies to update themselves
USER_AGENT = f"tokenrpg-collector/{VERSION} (+https://tokenrpg.com)"
# The public copy of this file, in the GitHub repository deploy.sh publishes it to. Step 1 on the Settings page
# downloads this file from it (the site reads this line for the URL and fills in only the config line), and a
# self-update installs a new version only when the server's file is byte-identical to it (v14). So both an install
# and an update run the GitHub copy: the server can't change what runs here unless the GitHub copy changes too.
# Written here on purpose: never taken from the config or from a server's reply.
PUBLIC_COPY = "https://raw.githubusercontent.com/Jabulani27/tokenrpg-collector/main/collector/tally_tokens.py"
# Configs written before 23 October 2026 name the site's old host. Every upload moves them to the new one (v14);
# a config naming any other host (a test server, a dev box) is left exactly as it is.
OLD_HOST, SITE = "tokenrpg.tinfoilhatsociety.cc", "https://tokenrpg.com"
HERE = os.path.abspath(__file__)
LOG = os.path.join(os.path.dirname(HERE), "runs.log")
CONFIG = os.environ.get("TOKENRPG_CONFIG") or os.path.expanduser("~/.config/tokenrpg/config.json")
# Folders added with --add-dir. Not in config.json, because step 1 rewrites that file.
DIRS_FILE = os.path.join(os.path.dirname(CONFIG), "claude-dirs.txt")
# When the last upload started; a throttled hook skips if it is recent. One per account folder
# named by CLAUDE_CONFIG_DIR (see stamp_file), so one account's uploads never starve another's.
LAST_UPLOAD = os.path.join(os.path.dirname(CONFIG), "last-upload")
# Held by an upload from reading the logs until its reply is in, so uploads land in the order
# they read: a slow one can never arrive after a later one and put older numbers back.
UPLOAD_LOCK = os.path.join(os.path.dirname(CONFIG), "upload.lock")
# How often throttled hooks upload at most, in seconds: the server sends it with every upload
# ("upload_every") and it is kept here, so the rate changes on the server's side only.
UPLOAD_EVERY = os.path.join(os.path.dirname(CONFIG), "upload-every")
DEFAULT_EVERY, MIN_EVERY, MAX_EVERY = 120, 60, 3600
# Each log file's usage records as last read (path, size, times, inode), so an upload re-reads
# only the files that changed.
CACHE = os.path.join(os.path.dirname(CONFIG), "log-cache.json")
# The last self-update that was skipped: version, reason, when. A hook retries that version at most every
# UPDATE_RETRY seconds and logs the same reason once (GitHub serves its files up to 5 minutes stale after a release).
UPDATE_SKIPPED = os.path.join(os.path.dirname(CONFIG), "update-skipped")
UPDATE_RETRY = 600
# The Claude Code events that upload, and whether each is throttled to the server's rate. Stop
# fires after every reply and PostToolUse after every tool call, so also during one long reply;
# a session's start and end always upload. The hook commands carry no number.
HOOK_EVENTS = {"SessionStart": False, "SessionEnd": False, "Stop": True, "PostToolUse": True}
MID_TURN = "PostToolUse"             # the one that fires during a long reply (collector v11)


# ---------- the throttle: runs before the imports below, so a skipped hook costs ~30 ms ----------

def upload_every():
    """The server's rate (seconds), clamped; DEFAULT_EVERY until the first upload brings one."""
    try:
        with open(UPLOAD_EVERY) as fh:
            return min(MAX_EVERY, max(MIN_EVERY, int(fh.read().strip())))
    except (OSError, ValueError):
        return DEFAULT_EVERY


def stamp_file():
    """LAST_UPLOAD, or for a session whose CLAUDE_CONFIG_DIR is another folder, a stamp of its
    own. Such a folder may be counted only through that variable (it is not in claude-dirs.txt),
    so only its own sessions upload it; with one shared stamp, ~/.claude's uploads kept it waiting."""
    env = os.environ.get("CLAUDE_CONFIG_DIR") or ""
    if not env:
        return LAST_UPLOAD
    real = os.path.realpath(os.path.expanduser(env))
    if real == os.path.realpath(os.path.expanduser("~/.claude")):
        return LAST_UPLOAD
    import zlib                      # a C module: cheap, unlike hashlib
    return f"{LAST_UPLOAD}-{zlib.crc32(real.encode('utf-8', 'surrogateescape')):08x}"


def uploaded_within(seconds):
    """True if an upload started less than `seconds` ago. A stamp up to a minute ahead is
    jitter and counts as recent; further ahead (the clock was moved back) counts as long ago,
    so a wrong clock never stops uploads."""
    try:
        with open(stamp_file()) as fh:
            age = time.time() - float(fh.read().strip())
    except (OSError, ValueError):
        return False
    return -60 <= age < seconds


def mark_upload():
    """Record that an upload is starting; True if the stamp was written. Every upload marks it,
    so the throttled hooks also skip right after a session's start or end upload."""
    path = stamp_file()
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w") as fh:
            fh.write(f"{time.time():.3f}\n")   # not rounded: a rounded-up stamp reads as the future
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def claim_upload(throttled):
    """Check the throttle and mark the upload as one step, under a lock: tool calls run in
    parallel, and two hooks firing together must not both upload. False = skip this one.
    A throttled call whose stamp can't be written (a read-only folder) is skipped too, or every
    tool call would upload; a session's start and end still upload."""
    lock = None
    try:
        if fcntl:
            os.makedirs(os.path.dirname(LAST_UPLOAD), exist_ok=True)
            lock = open(stamp_file() + ".lock", "a")
            fcntl.flock(lock, fcntl.LOCK_EX)
    except OSError:
        lock = None                  # no lock is better than no upload
    try:
        if throttled and uploaded_within(upload_every()):
            return False
        return mark_upload() or not throttled
    finally:
        if lock:
            lock.close()


def hook_call(argv):
    """(is this a hook run, is it throttled), read from the command line before argparse is
    loaded. v11 hooks run --hook EVENT. Older ones run --push --quiet, the after-each-reply one
    with --at-most-every N: that N is ignored and the server's rate applies (v11), which changes
    how often they upload, never what."""
    if "--install-hook" in argv or "--add-dir" in argv:
        return False, False
    for i, arg in enumerate(argv):
        if arg == "--hook" or arg.startswith("--hook="):
            event = arg[7:] if arg.startswith("--hook=") else (argv[i + 1] if i + 1 < len(argv) else "")
            return True, HOOK_EVENTS.get(event, True)      # an event we don't know yet: throttled
    if "--push" in argv and "--quiet" in argv:
        return True, any(a == "--at-most-every" or a.startswith("--at-most-every=") for a in argv)
    return False, False


CLAIMED = False                      # set when the fast path below has already claimed the upload


def hook_fast_path(argv):
    """A hook run, before anything heavy is imported: a throttled call that isn't due exits here.
    One that is due hands the upload to a detached child and returns, so Claude Code never
    waits for it (and a Claude Code that exits can't take the upload down with it)."""
    global CLAIMED
    hook, throttled = hook_call(argv)
    if not hook:
        return
    if not claim_upload(throttled):
        sys.exit(0)
    CLAIMED = True
    try:
        if os.fork():
            os._exit(0)              # the hook itself: done
        os.setsid()                  # the upload: its own session, nothing to wait on
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
    except (AttributeError, OSError):
        pass                         # no fork: upload in this process, as before v11


if __name__ == "__main__":
    hook_fast_path(sys.argv[1:])

# The rest, after the fast path. What only an upload or setup needs (urllib, ssl, subprocess,
# platform, shutil) is imported where it is used, so printing or a cached upload starts quicker.
import argparse  # noqa: E402
import glob  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import shlex  # noqa: E402
from collections import defaultdict  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

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


def new_day():
    return {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "messages": 0,
            "models": defaultdict(int), "hours": defaultdict(int)}


def read_log(path):
    """One log file's usage records, in file order: [message id, local day, local hour, the four
    FIELDS counts, model]. Streaming writes a message more than once; only its first complete
    line is kept, the one a single pass over every file would count. A timestamp that doesn't
    parse leaves day and hour None: its id still counts as seen. The working folder (cwd) is never
    used (v12: project names are no longer collected)."""
    out, mine = [], set()
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
            if not (isinstance(u, dict) and mid and ts) or mid in mine:
                continue
            mine.add(mid)
            try:
                local = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
                day, hour = local.date().isoformat(), local.hour
            except ValueError:
                day = hour = None
            out.append([mid, day, hour, [u.get(theirs) or 0 for theirs in FIELDS.values()], msg.get("model")])
    return out


def summarise(records):
    """One file's records added up per day, exactly as _collect adds them when none of them is
    skipped: {day: [input, output, cache_write, cache_read, messages, models, hours]},
    the last two as [key, tokens] pairs in first-seen order (the order the upload's keys come
    out in). None when a count isn't a plain int, since floats added in another order could
    round differently; such a file is always added record by record."""
    days = {}
    try:
        for mid, key, hour, counts, model in records:
            if key is None:
                continue
            if any(type(v) is not int for v in counts):
                return None
            d = days.get(key)
            if d is None:
                d = days[key] = [0, 0, 0, 0, 0, {}, {}]
            d[0] += counts[0]
            d[1] += counts[1]
            d[2] += counts[2]
            d[3] += counts[3]
            d[4] += 1
            total = 0 + counts[0] + counts[1] + counts[2] + counts[3]
            for slot, k in ((5, model or "unknown"), (6, str(hour))):
                d[slot][k] = d[slot].get(k, 0) + total
    except Exception:                    # an odd model: add this file record by record
        return None
    return {k: v[:5] + [list(v[5].items()), list(v[6].items())] for k, v in days.items()}


def collect(window_days, base=None, seen=None, cache=None):
    """Daily totals for one Claude Code folder. Pass one `seen` set to every folder in a run, in
    claude_dirs() order: a message found in two folders then stays with the earlier one. With a
    LogCache, files that haven't changed since the last run aren't read again, and a file none
    of whose messages were seen before is added from its per-day sums; the result is identical
    to reading every file (--no-cache)."""
    if cache is not None:
        before = set(seen) if seen is not None else None
        try:
            return _collect(window_days, base, seen, cache)
        except Exception as e:           # a cache we can't use is a full read, never a failed hook
            note = f"tokenrpg: log cache not used ({type(e).__name__}); reading every file"
            say(note) if QUIET else print(note, file=sys.stderr)      # stdout may be the JSON
            cache.reset()
            if seen is not None:
                seen.clear()
                seen.update(before)
    return _collect(window_days, base, seen, None)


def _collect(window_days, base, seen, cache):
    since = None
    if window_days:
        since = (datetime.now() - timedelta(days=window_days - 1)).date().isoformat()
        cutoff = time.time() - (window_days + 1) * 86400
    seen = set() if seen is None else seen
    days = defaultdict(new_day)
    base = base or default_dir()
    for path in glob.glob(os.path.join(glob.escape(base), "projects", "**", "*.jsonl"), recursive=True):
        st = os.stat(path)
        if since and st.st_mtime < cutoff:
            continue
        records = entry = None
        if cache is not None:
            entry = cache.get(path, st)
            if entry is None:
                records = read_log(path)
                entry = cache.put(path, st, records)
            if entry["days"] is not None and seen.isdisjoint(entry["mids"]):
                seen.update(entry["mids"])           # the fast way: every record counts
                for key, (i, o, cw, cr, n, models, hours) in entry["days"].items():
                    if since and key < since:
                        continue
                    d = days[key]
                    d["input"] += i
                    d["output"] += o
                    d["cache_write"] += cw
                    d["cache_read"] += cr
                    d["messages"] += n
                    for k, total in models:
                        d["models"][k] += total
                    for k, total in hours:
                        d["hours"][k] += total
                continue
            if records is None:
                records = json.loads(entry["records"])
        if records is None:
            records = read_log(path)
        for mid, key, hour, counts, model in records:            # the record-by-record way
            if mid in seen:
                continue
            seen.add(mid)
            if key is None or (since and key < since):
                continue
            d = days[key]
            total = 0
            for ours, v in zip(FIELDS, counts):
                d[ours] += v
                total += v
            d["messages"] += 1
            d["models"][model or "unknown"] += total
            d["hours"][str(hour)] += total
    for d in days.values():
        d["models"].pop("<synthetic>", None)
        for k in ("models", "hours"):
            d[k] = dict(d[k])
    return dict(sorted(days.items()))


_OWN_SHA = []


def own_sha256():
    """sha256 of this script's bytes as loaded, so any change to the code (not only a VERSION
    bump) starts the cache afresh. Read once: a self-update replaces the file under us."""
    if not _OWN_SHA:
        try:
            with open(HERE, "rb") as fh:
                _OWN_SHA.append(hashlib.sha256(fh.read()).hexdigest())
        except OSError:
            _OWN_SHA.append(None)
    return _OWN_SHA[0]


def sweep_tmp(folder, older_than=600):
    """Remove our own half-written files (<name>.<pid>.tmp) that a killed run left behind,
    once they are older than any run could still be writing. Nothing else is touched."""
    mine = tuple(os.path.basename(p) + "." for p in (CACHE, LAST_UPLOAD, UPLOAD_EVERY)) + (
        os.path.basename(LAST_UPLOAD) + "-",)                   # the per-account stamps
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if not (name.startswith(mine) and name.endswith(".tmp")):
            continue
        middle = name[:-4].rsplit(".", 1)[-1]
        path = os.path.join(folder, name)
        try:
            if middle.isdigit() and time.time() - os.lstat(path).st_mtime > older_than:
                os.unlink(path)
        except OSError:
            pass


class LogCache:
    """Each log file's read_log() records and their per-day sums, keyed by its path and checked against its size,
    modification and change times and inode: any difference and the file is read again. Kept
    beside the config (log-cache.json, mode 600). Anything unexpected about the file (missing,
    corrupt, cut short, written by another version, Python or time zone) means an empty cache,
    so a full read, and it is replaced whole (written aside, then renamed), never edited."""

    def __init__(self, path=None):
        self.path = path or CACHE
        self.old, self.new = self._load(), {}

    def stamp(self):
        """What the records depend on besides the file: this collector's own code (its bytes,
        not just VERSION, so a v11 cache, whose records held project names, is never read by v12),
        this Python's timestamp parsing, the text encoding and the local time zone."""
        try:
            real = os.path.realpath("/etc/localtime")
            st = os.stat(real)
            zone = [real, st.st_size, st.st_mtime_ns]
        except OSError:
            zone = None
        with open(os.devnull, errors="ignore") as fh:
            encoding = fh.encoding           # what read_log's open() decodes with
        return [VERSION, own_sha256(), list(sys.version_info[:2]), encoding,
                os.environ.get("TZ"), list(time.tzname), time.timezone, time.altzone, zone]

    def _load(self):
        try:
            with open(self.path, "rb") as fh:
                head, _, body = fh.read().partition(b"\n")
            head = json.loads(head)
            if head.get("stamp") != self.stamp() or head.get("sha256") != hashlib.sha256(body).hexdigest():
                return {}
            files = json.loads(body)
            return files if isinstance(files, dict) else {}
        except (OSError, ValueError, AttributeError):
            return {}

    @staticmethod
    def key(st):
        return [st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino, st.st_dev]

    def get(self, path, st):
        """The file's entry if it is unchanged: {"st", "mids", "days" (summarise()), "records"
        (read_log()'s, as JSON text, parsed only when a file has to be added record by record)}."""
        entry = self.old.get(path)
        if (isinstance(entry, dict) and entry.get("st") == self.key(st) and isinstance(entry.get("mids"), list)
                and isinstance(entry.get("days"), (dict, type(None))) and isinstance(entry.get("records"), str)):
            self.new[path] = entry
            return entry
        return None

    def put(self, path, st, records):
        entry = self.new[path] = {"st": self.key(st), "mids": [r[0] for r in records], "days": summarise(records),
                                  "records": json.dumps(records, separators=(",", ":"))}
        return entry

    def reset(self):
        self.old, self.new = {}, {}

    def save(self):
        """Write the cache if anything changed. Entries for files not looked at in this run (an
        older day's file, another folder) are kept while the file exists."""
        sweep_tmp(os.path.dirname(self.path))
        files = dict(self.new)
        for path, entry in self.old.items():
            if path not in files and os.path.exists(path):
                files[path] = entry
        if files == self.old:
            return
        body = json.dumps(files, separators=(",", ":")).encode()
        head = json.dumps({"stamp": self.stamp(), "sha256": hashlib.sha256(body).hexdigest()}).encode()
        tmp = f"{self.path}.{os.getpid()}.tmp"
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(head + b"\n" + body)
            os.replace(tmp, self.path)
            self.old = files
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass


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
                reply = input(f"Also count the Claude account in {short(path)}? Its tokens go to "
                              "your hero. [Y/n] ").strip().lower()
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
        import subprocess
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
    import platform
    host = (platform.node() or "this computer").split(".")[0]
    if folder == default_dir():
        label = host[:40]
    else:
        label = f"{host[:24]} · {os.path.basename(folder).lstrip('.')[:13]}"
    return {"id": dev_id, "label": label}


# ---------- which plan is this machine on? ----------

# The plan badge's inputs, nothing else. billingType was sent until v12 and never used (audit L6): v13 doesn't read it.
PLAN_FIELDS = ("organizationType", "organizationRateLimitTier", "subscriptionCreatedAt")


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
LOG_UPLOADS = True                   # False for a throttled hook: its successes aren't logged
LOG_LINES = 1000                     # runs.log keeps the newest this many lines


def say(msg):
    if QUIET:
        try:
            lines = []
            if os.path.exists(LOG):
                with open(LOG) as fh:
                    lines = fh.readlines()[-(LOG_LINES - 1):]
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
    import ssl
    reason = getattr(e, "reason", e)
    return isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(reason)


def http(url, data=None, headers=None):
    """Return (status, body_bytes). Python from python.org on macOS ships without a CA bundle
    until 'Install Certificates' is run; curl uses the system keychain, so fall back to it."""
    import shutil
    import subprocess
    import urllib.error
    import urllib.request
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

class UpdateSkipped(Exception):
    """A self-update that can't go ahead; the message says why. This file is left as it was."""


def self_update(base_url, latest):
    """Replace this file with version `latest` (the server's announced one), only when the file the server hands out
    declares that version, is byte-identical (SHA-256) to PUBLIC_COPY and compiles. Anything else, GitHub unreachable
    included, keeps this file: fail closed. A hook tries a skipped version again at most every UPDATE_RETRY seconds and
    logs a reason once, so a release GitHub hasn't caught up with is simply picked up by a later run."""
    if QUIET and _skipped_lately(latest):
        return
    try:
        install(verified_download(base_url, latest))
    except UpdateSkipped as e:
        return _skipped(latest, str(e))
    try:
        os.unlink(UPDATE_SKIPPED)
    except OSError:
        pass
    say(f"tokenrpg: updated the collector from v{VERSION} to v{latest}")


def verified_download(base_url, latest):
    """The server's /collector.py, once it has passed every check. Raises UpdateSkipped."""
    def get(url, where):
        try:
            status, body = http(url)
        except Exception as e:  # noqa: BLE001 - unreachable, timed out, cut off: all mean "not now"
            raise UpdateSkipped(f"can't reach {where}: {getattr(e, 'reason', e)}")
        if status != 200:
            raise UpdateSkipped(f"{where} answered {status}")
        return body

    body = get(base_url + "/collector.py", "the server")
    m = re.search(rb"^VERSION = (\d+)", body, re.M)
    if not m or int(m.group(1)) != latest:
        raise UpdateSkipped(f"the server's file is not v{latest}")
    public = get(PUBLIC_COPY, "the public copy on GitHub")
    if hashlib.sha256(body).digest() != hashlib.sha256(public).digest():
        raise UpdateSkipped("the server's file doesn't match the public copy on GitHub")
    if b"def main" not in body:
        raise UpdateSkipped("the file has no main()")
    try:
        compile(body, "tally_tokens.py", "exec")  # refuse anything that isn't valid Python
    except (SyntaxError, ValueError) as e:
        raise UpdateSkipped(f"the file isn't valid Python ({e})")
    return body


STALE_TMP = 3600   # seconds: an install temp file older than this was left by a run that died mid-write


def sweep_install_temps():
    """Delete `<this file>.<pid>.tmp` files over an hour old, left by a run killed between write and replace.
    A younger one may be another run's install in progress, so it is left alone."""
    for path in glob.glob(f"{glob.escape(HERE)}.*.tmp"):
        if not path[len(HERE) + 1:-len(".tmp")].isdigit():
            continue
        try:
            if time.time() - os.path.getmtime(path) > STALE_TMP:
                os.unlink(path)
        except OSError:
            pass


def install(body):
    """Put `body` in place of this file: written beside it under a name of this process's own, flushed to disk, then
    swapped in whole, so two runs updating at once never write into, or install, each other's half-written file, and
    a power cut after the swap can't leave an empty or partial file in its place."""
    sweep_install_temps()
    tmp = f"{HERE}.{os.getpid()}.tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, HERE)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise UpdateSkipped(f"can't replace {HERE}: {e}")


def _last_skip():
    try:
        with open(UPDATE_SKIPPED) as fh:
            last = json.load(fh)
        return last if isinstance(last, dict) else {}
    except (OSError, ValueError, RecursionError):      # unreadable, not JSON, or nested past Python's limit
        return {}


def _skipped_lately(latest):
    """True only for a readable note of this version skipped under UPDATE_RETRY ago; anything odd in it is "no"."""
    last = _last_skip()
    try:
        return last.get("version") == latest and 0 <= time.time() - float(last.get("at")) < UPDATE_RETRY
    except (TypeError, ValueError, OverflowError):     # OverflowError: an integer too big for a float
        return False


def _skipped(latest, reason):
    """Say why (once per version and reason when run by a hook; always at the terminal) and note it."""
    last = _last_skip()
    if not QUIET or (last.get("version"), last.get("reason")) != (latest, reason):
        say(f"tokenrpg: update to v{latest} skipped ({reason}); v{VERSION} keeps running and tries again later")
    tmp = f"{UPDATE_SKIPPED}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump({"version": latest, "reason": reason, "at": time.time()}, fh)
        os.replace(tmp, UPDATE_SKIPPED)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ---------- Claude Code hook ----------

def _q(path):
    """Shell-quote a path, leaving a leading ~/ to expand (the Settings page shows ~ paths)."""
    return "~/" + shlex.quote(path[2:]) if path.startswith("~/") else shlex.quote(path)


def hook_command(event, py=None, script=None, no_projects=False):
    """Absolute interpreter + script paths: a hook started from the desktop app may not
    have the terminal's PATH. which('python3') is the stable symlink (it survives a
    Homebrew minor upgrade); sys.executable is the fallback. The command names its event and
    carries no number: how often it uploads is the server's to say (v11)."""
    import shutil
    py = py or shutil.which("python3") or sys.executable
    return _command(event, _q(py), _q(script or HERE), no_projects)


def _command(event, py, script, no_projects=False, prefix=""):
    flag = " --no-projects" if no_projects else ""
    return f"{prefix}{py} {script} --hook {event}{flag} >/dev/null 2>&1 || true"


def _entry(command):
    return {"hooks": [{"type": "command", "async": True, "timeout": 60, "command": command}]}


def hook_settings(py=None, script=None, no_projects=False):
    """The "hooks" block --install-hook merges in. The server shows the same block on the
    Settings page for people who edit settings.json by hand, so the two cannot disagree."""
    return {event: [_entry(hook_command(event, py, script, no_projects))] for event in HOOK_EVENTS}


_WORD = r"""(?:'[^']*'|"[^"]*"|[^\s'"])+"""
_OUR_COMMAND = re.compile(r"\s*((?:[A-Za-z_][A-Za-z0-9_]*=(?:" + _WORD + r")?\s+)*)(" + _WORD + r")\s+"
                          r"""(?:'[^']*tally_tokens\.py'|"[^"]*tally_tokens\.py"|[^\s'"]*tally_tokens\.py)(?=\s|$)""")


def kept_from(command):
    """(prefix, interpreter) a player chose in one of our old hook commands, kept by a rerun of
    step 3: leading VAR=value settings (e.g. TOKENRPG_CONFIG=...) and an interpreter given as a
    path. A bare "python3" is not kept: hooks get an absolute one, as the desktop app may not
    have the terminal's PATH. ("", None) when there is nothing to keep."""
    m = _OUR_COMMAND.match(command or "")
    if not m:
        return "", None
    py = m.group(2)
    return m.group(1), (py if py.strip("'\"").startswith(("/", "~/")) else None)


def _ours(hook):
    """One of our hook commands, in any form a collector has written (matching "tokenrpg"
    failed when the path was capitalised)."""
    return isinstance(hook, dict) and "tally_tokens.py" in str(hook.get("command", ""))


def installed_hooks(base):
    """Which of our events this folder's settings.json has a hook for. Only these event
    names are sent, so the site can say whether a machine also updates during long replies;
    nothing else in settings.json (which can hold env vars and keys) is read into the upload."""
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


def install_hook(dirs=None, no_projects=False):
    """Add the HOOK_EVENTS upload hooks to the settings.json of every counted Claude Code
    folder, so a session in any account uploads them all. Run by hand with no folders given,
    it first offers any account folder not counted yet.

    Keeps every existing setting and every hook that isn't ours, replaces our older hooks
    rather than adding a second set, backs each file up first, leaves a folder that is already
    set up alone, and refuses to touch a file that is not valid JSON."""
    if dirs is None:
        offer_new_dirs()
        dirs = claude_dirs(for_hooks=True) or [default_dir()]
    added, problems = False, []
    for base in dirs:
        result = _install_hook_in(base, no_projects)
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


def machine_os(version_file="/proc/version"):
    """The kind of computer, coarsely: "mac", "wsl", "windows" or "linux" (any other Unix too). Only
    this word is sent, so Settings can name each machine for its owner; nothing else about the
    system is. WSL is told apart the way is_wsl() does."""
    if sys.platform == "darwin":
        return "mac"
    if sys.platform.startswith(("win", "cygwin", "msys")):
        return "windows"
    return "wsl" if is_wsl(version_file) else "linux"


def on_windows_drive(path):
    """True for a folder on a Windows drive seen from WSL: /mnt/<drive letter>/..., after
    resolving symlinks, so a link from the Linux home to a Windows folder counts too. A Linux
    disk such as /mnt/data never does, and nothing does outside WSL."""
    real = _canon(path)
    if not real.startswith(WINDOWS_DRIVES + "/"):
        return False
    drive = real[len(WINDOWS_DRIVES) + 1:].split("/", 1)[0]
    return len(drive) == 1 and "a" <= drive.lower() <= "z" and is_wsl()


def _install_hook_in(base, no_projects=False):
    """None when hooks were added or replaced, "" when already there or skipped, else what
    went wrong. Our old entries (any earlier collector's) are replaced, never duplicated, and
    every hook that isn't ours stays exactly as it was."""
    where = short(base)
    # The folder, or a settings.json symlinked from it, on a Windows drive.
    if on_windows_drive(base) or on_windows_drive(os.path.join(base, "settings.json")):
        say(f"  {where} is on a Windows drive, so no hooks were added to it: they are Linux commands that "
            "Windows's Claude Code would try to run. Its history still uploads whenever Claude Code runs on the Linux side.")
        return ""
    # A symlinked settings.json (dotfiles, or shared by two accounts) stays a symlink.
    path = os.path.realpath(os.path.join(base, "settings.json"))
    settings, original = {}, {}
    if os.path.exists(path):
        try:
            with open(path) as fh:
                text = fh.read()
            settings, original = json.loads(text), json.loads(text)
        except ValueError as e:
            return (f"tokenrpg: {path} is not valid JSON ({e}); left it untouched. "
                    "Fix it, then run --install-hook again.")
        if not isinstance(settings, dict):
            return f"tokenrpg: {path} is not a JSON object; left it untouched."
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict) or any(not isinstance(hooks.get(e, []), list) for e in HOOK_EVENTS):
        return f"tokenrpg: the \"hooks\" in {path} are not in the shape Claude Code uses; left it untouched."
    # Someone who added --no-projects to our hooks by hand keeps it. Since v12 it changes nothing (no
    # project names are read), but a rerun still never drops what a player chose.
    no_projects = no_projects or any(_ours(h) and "--no-projects" in str(h.get("command"))
                                     for e in HOOK_EVENTS for entry in hooks.get(e, [])
                                     if isinstance(entry, dict) and isinstance(entry.get("hooks"), list)
                                     for h in entry["hooks"])
    olds = {e: [str(h.get("command")) for entry in hooks.get(e, []) if isinstance(entry, dict)
                and isinstance(entry.get("hooks"), list) for h in entry["hooks"] if _ours(h)] for e in HOOK_EVENTS}
    every = [c for e in HOOK_EVENTS for c in olds[e]]       # a new event copies a customised one, if any
    anyold = next((c for c in every if kept_from(c) != ("", None)), every[0] if every else "")
    added, replaced, changed = [], [], []
    for event in HOOK_EVENTS:
        old = (olds[event] or [anyold])[0]
        prefix, py = kept_from(old)
        command = (_command(event, py, _q(HERE), no_projects, prefix) if py
                   else prefix + hook_command(event, no_projects=no_projects))
        ours = _entry(command)
        if olds[event] and olds[event] != [command]:
            changed.append((event, olds[event], command))
        entries, placed, had = [], False, False
        for entry in hooks.get(event, []):
            inner = entry.get("hooks") if isinstance(entry, dict) else None
            if not isinstance(inner, list) or not any(_ours(h) for h in inner):
                entries.append(entry)            # not ours: untouched, in its place
                continue
            had = True
            theirs = [h for h in inner if not _ours(h)]
            if theirs:                           # ours was merged into someone's entry by hand
                entries.append(dict(entry, hooks=theirs))
            if not placed:                       # ours goes where the old one was
                entries.append(ours)
                placed = True
        if not placed:
            entries.append(ours)
        if entries != hooks.get(event):
            (replaced if had else added).append(event)
        hooks[event] = entries
    if settings == original:
        say(f"✓ {where}: already set up. Claude Code uploads when a session starts or ends, and every "
            f"~{upload_every() // 60} minutes while you work, also during long replies.")
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
    for event, old, new in changed:
        say(f"  {event}: " + " and ".join(old) + f"\n    is now: {new}")
    what = "; ".join(f"{k}: {', '.join(v)}" for k, v in (("added", added), ("updated", replaced)) if v)
    say(f"✓ {where}: Claude Code will now upload when a session starts or ends, and every ~{upload_every() // 60} "
        f"minutes while you work, also during long replies ({what}).")
    return None


def add_dir(path, no_projects=False):
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
    install_hook([path], no_projects=no_projects)
    lock = upload_lock() if os.path.exists(CONFIG) else None
    try:
        _add_dir_upload(path)
    finally:
        if lock:
            lock.close()


def _add_dir_upload(path):
    if os.path.exists(CONFIG):
        seen, days, cache = set(), {}, LogCache()
        for d in claude_dirs():          # earlier folders first, so they keep their messages
            got = collect(0, base=d, seen=seen, cache=cache)
            if _ident(d) == _ident(path):
                days = got
        cache.save()
        if days:
            push([(path, days)])


# ---------- upload ----------

def keep_rate(every):
    """Store the server's upload rate for the throttled hooks (clamped; ignored unless a whole
    number of seconds). Written only when it changes."""
    if not isinstance(every, int) or isinstance(every, bool):
        return
    every = min(MAX_EVERY, max(MIN_EVERY, every))
    if every == upload_every() and os.path.exists(UPLOAD_EVERY):
        return
    try:
        tmp = f"{UPLOAD_EVERY}.{os.getpid()}.tmp"
        with open(tmp, "w") as fh:
            fh.write(f"{every}\n")
        os.replace(tmp, UPLOAD_EVERY)
    except OSError:
        pass


def move_host(cfg):
    """The config's server URL, moved to SITE when it names exactly OLD_HOST: for this run, and in the file (written
    whole, every other key kept, readable by you only). Any other host is returned exactly as written."""
    import urllib.parse
    url = cfg["url"]
    try:
        host = urllib.parse.urlsplit(url).hostname
    except ValueError:
        return url
    if host != OLD_HOST:
        return url
    tmp = f"{CONFIG}.{os.getpid()}.tmp"
    try:
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
            json.dump(dict(cfg, url=SITE), fh)
            fh.write("\n")
        os.replace(tmp, CONFIG)
        say(f"tokenrpg: {short(CONFIG)} now points at {SITE} (the site's new address)")
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return SITE


# What the player is told when an upload is refused, by HTTP status: our words, never the server's (lawfix review).
STEP_1_AGAIN = ("copy step 1 from your Settings page again. Its download line must fetch "
                f"{PUBLIC_COPY}, not a file from the site.")
LATER = "a later upload sends these days again."
REFUSED = {
    400: "the server couldn't read this upload. Nothing was saved.",
    403: "uploads are off for this account, or the request was blocked on its way. Nothing was saved.",
    422: "the server refused these numbers (too many days, or more tokens in a day than Claude Code can use). "
         "Nothing was saved.",
    426: "this collector is out of date: " + STEP_1_AGAIN,
    429: "too many uploads in the last hour. Nothing is lost: " + LATER,
}
REFUSED_OTHER = "Nothing was saved; " + LATER


def push(uploads):
    """uploads: [(folder, days)]. One request per folder: each is its own machine row."""
    import urllib.error
    try:
        with open(CONFIG) as fh:
            cfg = json.load(fh)
        base, token = move_host(cfg).rstrip("/"), cfg["token"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        fail(f"tokenrpg: can't read {CONFIG}: {e}. To fix it, " + STEP_1_AGAIN)
    handle, latest = None, 0
    for folder, days in uploads:
        body = json.dumps({"days": days, "device": device(folder), "plan": plan(folder),
                           "hooks": hooks_that_upload(folder), "client": f"tally_tokens/{VERSION}",
                           "os": machine_os()}).encode()
        try:
            status, raw = http(base + "/api/ingest", body,
                               {"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        except urllib.error.URLError as e:
            fail(f"tokenrpg: can't reach {base}: {getattr(e, 'reason', e)}")
        text = raw.decode(errors="ignore")
        if status == 401:
            fail("tokenrpg: the server didn't recognise your upload token (it may have been reissued): "
                 + STEP_1_AGAIN)
        # The server's own text is never printed (lawfix review): only the status and a fixed message of ours.
        if status != 200:
            hint = " (Cloudflare blocked the request)" if "error code: 10" in text else ""
            fail(f"tokenrpg: upload refused ({status}){hint}: {REFUSED.get(status, REFUSED_OTHER)}")
        try:
            result = json.loads(text)
        except ValueError:
            fail(f"tokenrpg: unexpected reply from the server (not JSON). {REFUSED_OTHER}")
        skipped = result.get("skipped") or {}
        if skipped:
            say(f"tokenrpg: {len(skipped)} day(s) refused by the server: {skipped}")
        keep_rate(result.get("upload_every"))
        handle, n = result.get("handle"), result.get("accepted", 0)
        span = f" ({min(days)} to {max(days)})" if days else ""
        where = f" from {result['device']}" if result.get("device") else ""
        badge = f", {result['plan']}" if result.get("plan") else ""
        if LOG_UPLOADS:                  # not every 2 minutes' success: runs.log must cover days
            say(f"✓ Uploaded {n} day{'s' if n != 1 else ''} of Claude Code usage{span}{where}{badge}.")
        announced = result.get("collector_version")
        if isinstance(announced, int) and not isinstance(announced, bool):
            latest = max(latest, announced)
    if not QUIET:
        say(f"  Your hero: {base}/u/{handle}")
    if latest > VERSION:
        self_update(base, latest)


def main():
    global QUIET, LOG_UPLOADS
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=28, help="window in days, 0 = everything (default 28)")
    ap.add_argument("--push", action="store_true", help="upload instead of printing")
    ap.add_argument("--no-projects", action="store_true",
                    help="does nothing since v12, which never reads project names; still accepted, and kept "
                         "by --install-hook in hook commands that have it, so older commands keep working")
    ap.add_argument("--quiet", action="store_true", help=f"write to {LOG} instead of the terminal")
    ap.add_argument("--install-hook", action="store_true",
                    help="add upload hooks to each Claude Code folder's settings.json (safe to re-run)")
    ap.add_argument("--add-dir", metavar="DIR",
                    help="also count another Claude account's folder and upload its history (safe to re-run)")
    ap.add_argument("--hook", metavar="EVENT",
                    help="how Claude Code runs it (--push --quiet, throttled to the server's rate for "
                         "the events that fire often); --install-hook writes these")
    ap.add_argument("--at-most-every", type=int, metavar="SECONDS",
                    help="from older hooks: now means throttled to the server's rate, whatever SECONDS says")
    ap.add_argument("--no-cache", action="store_true",
                    help=f"read every log file again, without {CACHE}")
    ap.add_argument("--version", action="version", version=f"tokenrpg collector v{VERSION}")
    args = ap.parse_args()
    if args.hook:
        args.push = args.quiet = True
    QUIET = args.quiet
    if args.add_dir:
        add_dir(args.add_dir, no_projects=args.no_projects)
        return
    if args.install_hook:
        install_hook(no_projects=args.no_projects)
        return
    throttled = hook_call(sys.argv[1:])[1]                        # the same rule as the fast path
    if args.push and not CLAIMED:        # run as a script, the fast path at the top has claimed it
        if not claim_upload(throttled):
            return                       # quietly: this runs after every reply and tool call
    if args.push and not QUIET:
        offer_new_dirs()                 # a person is at the terminal; the hook never asks
    LOG_UPLOADS = not (QUIET and throttled)   # runs.log keeps start/end uploads and every problem
    lock = upload_lock() if args.push else None
    try:
        collect_and_send(args)
    finally:
        if lock:
            lock.close()


def upload_lock():
    """UPLOAD_LOCK, held from reading the logs until the server has replied, so an upload that
    started later (SessionEnd, after a slow mid-reply one) reads the logs only after the earlier
    one has landed, and the server's last copy is always the newest. None if it can't be had."""
    if not fcntl:
        return None
    try:
        os.makedirs(os.path.dirname(UPLOAD_LOCK), exist_ok=True)
        lock = open(UPLOAD_LOCK, "a")
        fcntl.flock(lock, fcntl.LOCK_EX)
        return lock
    except OSError:
        return None


def collect_and_send(args):
    dirs, seen = claude_dirs(), set()
    cache = None if args.no_cache else LogCache()
    per = [(d, collect(args.days, base=d, seen=seen, cache=cache)) for d in dirs]
    if cache is not None and args.push:
        cache.save()                     # an upload keeps it; printing only reads it (no state from a look)
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
