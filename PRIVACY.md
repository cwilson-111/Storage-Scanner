# Privacy

Storage Scanner runs entirely on your machine. It has no telemetry, no
crash reporting, no analytics, no ads, and no account or login of any
kind. The only network request it ever makes is an optional, anonymous
check for a newer version — see **Update check** below for exactly what
that does and doesn't do.

## What it reads

Only the filesystem paths you choose to scan: file/folder names, sizes,
and timestamps. For duplicate detection specifically, it reads file
contents locally to compute hashes for comparison — those contents are
never written anywhere but a temporary in-memory hash, and never leave
your machine.

## What it stores, and where

- **Scan history** (for the Growth History and forecasting features): a
  local SQLite database at
  `~/Library/Application Support/NeuralStorageMatrix/storage_history.db`
  (macOS) or `%LOCALAPPDATA%\NeuralStorageMatrix\storage_history.db`
  (Windows).
- **Diagnostic logs** (for troubleshooting crashes, since a windowed build
  has no console to print to): a rotating log file under `logs/` in that
  same app-data folder.
- **Deletion audit log**: every file the app has sent to the Recycle
  Bin/Trash — when, from where, and how big — stored in that same SQLite
  database and viewable in the app's own Audit Log window.

All of it stays on your machine. Nothing here is ever uploaded. You can
delete the whole app-data folder at any time; nothing about the app's
behavior depends on it persisting.

## What it deletes

Only files and folders you explicitly select, and always by sending them
to the Recycle Bin/Trash — never a permanent delete. See the Audit Log
window (or the ledger above) for a full record of what's been removed.

## Update check

On launch, Storage Scanner makes one anonymous `GET` request to GitHub's
public release API to see if a newer version exists — at most once every
24 hours (tracked locally; not on every single launch). That request:

- sends nothing about you, your files, your system, or how you use the
  app — it's an unauthenticated request to a public endpoint, identical to
  what a browser visiting the releases page would trigger;
- never downloads or runs anything — the only outcome is a small,
  dismissible on-screen notice with a link to the Releases page for you to
  open yourself;
- fails silently (no notice, no error) if you're offline or GitHub is
  unreachable.

Running from source rather than a downloaded release never triggers this
at all — see `storage_scanner/update_check.py` and `storage_scanner/version.py`
for exactly how.

## What it does not do

No telemetry, no crash reporting to any remote service, no analytics, no
ads, no accounts. See **Update check** above for the one exception to "no
network activity," and exactly what it is limited to.

## Verifying this yourself

Storage Scanner has zero third-party runtime dependencies (see `sbom.json`
attached to each release) and is fully open source — every claim above is
checkable by reading the code, or by running the app inside a
network-isolated sandbox/firewall and confirming the only outbound request
is the update check described above (and that even that fails silently).
