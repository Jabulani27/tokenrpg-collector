# TokenRPG collector

[TokenRPG](https://tokenrpg.tinfoilhatsociety.cc) is an idle RPG for friends who use Claude Code: the
tokens you spend move a hero up the Stack. This is the small script that runs on your computer. It
reads the usage numbers Claude Code already writes to its own logs and uploads daily totals.

It is one file, `collector/tally_tokens.py`, using only Python's standard library (Python 3.9 or
newer). It runs on Linux, macOS and WSL. This repository is a copy of it, published so you can read
exactly what runs on your machine.

## What is sent

Numbers only, for each day:

- tokens by type (input, output, cache writes, cache reads) and the number of messages;
- tokens by model, by hour of the day, and by project folder name (the last folder of the working
  directory, such as `my-app`, or `my-app/src` for a generic name). `--no-projects` leaves folder
  names out.

With each upload it also sends:

- the machine's id: a one-way SHA-256 hash of the OS machine id, your home folder and the Claude
  folder, so it says nothing about the machine;
- a label: the computer's name, plus the account folder's name for any folder other than `~/.claude`;
- your plan tier, from these fields only: `organizationType`, `organizationRateLimitTier`,
  `subscriptionCreatedAt` and `billingType` in `~/.claude.json`, or, on Linux, `subscriptionType` and
  `rateLimitTier` in `~/.claude/.credentials.json`;
- the collector's version, and which of its own hooks (event names such as `Stop`) are installed.

**Never sent:** your prompts, Claude's replies, code, file contents, file paths, or your Claude
credentials or login tokens. Of the file that holds the login tokens, the script uses only the two
plan fields above. It authenticates with its own TokenRPG upload token, never your Claude
login.

## Install

Sign up at [tokenrpg.tinfoilhatsociety.cc](https://tokenrpg.tinfoilhatsociety.cc) and follow
**Settings → Connect Claude Code**. Step 1 downloads this file to
`~/.local/share/tokenrpg/tally_tokens.py`, step 2 uploads your history, and step 3 adds hooks so
Claude Code uploads when a session starts or ends, and every few minutes while you work (after replies
and tool calls, at most as often as the site's reply to each upload says, between 1 and 60 minutes).

To start quickly, each upload keeps `log-cache.json` next to its config: the usage numbers it already read
from each log file, so the next one reads only the files that changed (printing reads it, never writes it). It stays on your computer; delete it at any
time, or run with `--no-cache`, and the next run reads every file again.

Run it without `--push` to see exactly what it would send, as JSON, without sending anything:

    python3 ~/.local/share/tokenrpg/tally_tokens.py

## Check the file you were given

The site serves the same file as `collector/tally_tokens.py` here, byte for byte. Compare the two:

    curl -fsSL https://tokenrpg.tinfoilhatsociety.cc/collector.py | sha256sum
    sha256sum collector/tally_tokens.py

On macOS, use `shasum -a 256` in place of `sha256sum`. The two lines should match. Your installed
copy updates itself when a newer version comes out, so after an update compare
`~/.local/share/tokenrpg/tally_tokens.py` too.

## Run the tests

From a clone of this repository, with any Python 3.9 or newer and nothing to install:

    python3 -m unittest discover -s tests -t .

The tests use throwaway folders and never touch your own `~/.claude`.

## Licence

MIT; see [LICENSE](LICENSE).
