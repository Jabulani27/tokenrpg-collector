# TokenRPG collector

[TokenRPG](https://tokenrpg.com) is an idle RPG for friends who use Claude Code: the
tokens you spend move a hero up the Stack. This is the small script that runs on your computer. It
reads the usage numbers Claude Code already writes to its own logs and uploads daily totals.

It is one file, `collector/tally_tokens.py`, using only Python's standard library (Python 3.9 or
newer). It runs on Linux, macOS and WSL. This repository is a copy of it, published so you can read
exactly what runs on your machine.

## What is sent

Numbers only, for each day:

- tokens by type (input, output, cache writes, cache reads) and the number of messages;
- tokens by model and by hour of the day.

No project or folder names: until version 12 it also sent tokens by project folder name (the last folder of
the working directory); version 12 ignores the working directory. `--no-projects` is still accepted, so older
hook commands keep working, but changes nothing.

With each upload it also sends:

- the machine's id: a one-way SHA-256 hash of the OS machine id, your home folder and the Claude
  folder, so it says nothing about the machine;
- a label: the computer's name, plus the account folder's name for any folder other than `~/.claude`;
- the kind of computer, one word: `mac`, `linux`, `wsl` or `windows` (any other Unix is sent as `linux`);
- your plan tier, from these fields only: `organizationType`, `organizationRateLimitTier` and
  `subscriptionCreatedAt` in `~/.claude.json`, or, on Linux, `subscriptionType` and `rateLimitTier` in
  `~/.claude/.credentials.json` (until version 13 it also sent `billingType`, which the site never used or kept);
- the collector's version, and which of its own hooks (event names such as `Stop`) are installed.

**Never sent:** your prompts, Claude's replies, code, file contents, file paths, the names of the
folders you work in, or your Claude credentials or login tokens. Of the file that holds the login
tokens, the script uses only the two plan fields above. It authenticates with its own TokenRPG
upload token, never your Claude login.

## Install

Sign up at [tokenrpg.com](https://tokenrpg.com) and follow
**Settings → Connect Claude Code**. Step 1 downloads this file from this repository (raw.githubusercontent.com)
to `~/.local/share/tokenrpg/tally_tokens.py` and writes your config (the site's address and your upload token;
that line is the only part the site fills in), step 2 uploads your history, and step 3 adds hooks so
Claude Code uploads when a session starts or ends, and every few minutes while you work (after replies
and tool calls, at most as often as the site's reply to each upload says, between 1 and 60 minutes).

To start quickly, each upload keeps `log-cache.json` next to its config: the usage numbers it already read
from each log file, so the next one reads only the files that changed (printing reads it, never writes it). It stays on your computer; delete it at any
time, or run with `--no-cache`, and the next run reads every file again.

Run it without `--push` to see exactly what it would send, as JSON, without sending anything:

    python3 ~/.local/share/tokenrpg/tally_tokens.py

## Check the file you were given

The site serves the same file as `collector/tally_tokens.py` here, byte for byte. Compare the two:

    curl -fsSL https://tokenrpg.com/collector.py | sha256sum
    sha256sum collector/tally_tokens.py

On macOS, use `shasum -a 256` in place of `sha256sum`. The two lines should match.

## Updates

Your installed copy updates itself when the site has a newer version, and only to a file that is byte-identical
to `collector/tally_tokens.py` in this repository, the same file step 1 installs. After an upload it downloads the site's file and this
repository's copy (from raw.githubusercontent.com), and installs the new version only when the two match
and the file is the version the site announced. Otherwise, or when GitHub can't be reached, it keeps the
copy you have and tries again later. This is so since version 14; older versions updated from the site
alone, so their update to version 14 is the last one made that way. After an update, compare
`~/.local/share/tokenrpg/tally_tokens.py` too.

Both the install and the update check download from GitHub, so GitHub sees your computer's IP address then.

Version 14 also moves a config set up on the site's old address to https://tokenrpg.com, keeping
everything else in it.

## Run the tests

From a clone of this repository, with any Python 3.9 or newer and nothing to install:

    python3 -m unittest discover -s tests -t .

The tests use throwaway folders and never touch your own `~/.claude`.

## Licence

MIT; see [LICENSE](LICENSE).
