# Privacy

Storage Scanner runs entirely on your machine. It has no telemetry, no
crash reporting, no analytics, no ads, and no account or login of any
kind. The only network request it ever makes is an anonymous check for a
newer version, which you can turn off — see **Update check** below for
exactly what that does and doesn't do.

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
  (macOS), `%LOCALAPPDATA%\NeuralStorageMatrix\storage_history.db`
  (Windows), or `~/.local/share/NeuralStorageMatrix/storage_history.db`
  (Linux, following the XDG Base Directory spec). Saved scans older than
  30 days thin out automatically to one a day, week, month and then year
  (Tools ▸ Settings ▸ Keep Every Saved Scan For); nothing else in this
  database is ever pruned.
- **Diagnostic logs** (for troubleshooting crashes, since a windowed build
  has no console to print to): a rotating log file under `logs/` in that
  same app-data folder.
- **Deletion audit log**: every delete the app has attempted — when, from
  where, how big, and what actually happened (sent to the Recycle
  Bin/Trash, deleted permanently after you confirmed it, refused, or
  failed) — stored in that same SQLite database and viewable in the app's
  own Audit Log window.

All of it stays on your machine. Nothing here is ever uploaded. You can
delete the whole app-data folder at any time; nothing about the app's
behavior depends on it persisting.

## What it deletes

Only files and folders you explicitly select. Deletes go to the Recycle
Bin/Trash. On Windows, before anything is deleted the app checks whether the
Recycle Bin can hold it (it can't for files on subst, network or removable
drives, paths over 260 characters, a bin that's turned off, or items bigger
than the bin); if it can't, nothing happens unless you confirm a permanent
delete. Drive roots, the folder a scan started from, and system and profile
folders (Windows, Program Files, your user folder, Documents, Downloads…)
are never deleted. See the Audit Log window (or the ledger above) for a
record of what's been removed and where it went.

## Update check

On launch, a released build of Storage Scanner makes one anonymous `GET`
request to GitHub's public release API to see if a newer version exists —
at most once every 24 hours (tracked locally; not on every single launch).
That request:

- sends nothing about you, your files, your system, or how you use the
  app — it's an unauthenticated request to a public endpoint, identical to
  what a browser visiting the releases page would trigger;
- never downloads or runs anything — the only outcome is a small,
  dismissible on-screen notice with a link to the Releases page for you to
  open yourself;
- fails silently (no notice, no error) if you're offline or GitHub is
  unreachable.

To turn it off, untick Tools ▸ Settings ▸ **Check for Updates on Launch**,
or set the environment variable `STORAGE_SCANNER_NO_UPDATE_CHECK=1` (useful
for managed installs and scheduled scans). A run that isn't a release —
from source, or a build of the main branch — never makes the request at
all; see `storage_scanner/update_check.py` and `storage_scanner/version.py`
for exactly how.

## What it does not do

No telemetry, no crash reporting to any remote service, no analytics, no
ads, no accounts. See **Update check** above for the one exception to "no
network activity," and exactly what it is limited to.

## Verifying this yourself

Storage Scanner needs no third-party runtime dependency for anything above
(see `sbom.json` attached to each release) — the only exceptions are two
optional, independently-imported packages (`pyarrow`/`openpyxl`, for the
Data Tools CSV export menu), none of
which read, write, or transmit anything beyond the local file you pick in
their own file dialog. The app is fully open source — every claim above is
checkable by reading the code, or by running the app inside a
network-isolated sandbox/firewall and confirming the only outbound request
is the update check described above (and that even that fails silently).
