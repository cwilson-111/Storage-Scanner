"""Lightweight update notice: on launch, check GitHub's public release API
for a newer version than the one currently running.

This is the only network call anywhere in the app (see PRIVACY.md). It:
- sends nothing about the user, their files, or their system — just an
  anonymous GET request to a public API endpoint;
- checks at most once every 24 hours (tracked via history.py's
  app_metadata table), not on every single launch;
- never downloads or runs anything — the only outcome is a version-string
  comparison, and the only action available is a link to the Releases page
  for the user to open themselves.

Every failure mode (offline, GitHub unreachable, malformed response, a
corrupt cached timestamp) resolves to "no update notice shown" — this must
never be able to disrupt startup or the rest of the app.
"""

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

from history import get_app_metadata, set_app_metadata
from storage_scanner.logging_setup import logger
from storage_scanner.version import __version__ as CURRENT_VERSION

RELEASES_API_URL = "https://api.github.com/repos/cwilson-111/Storage-Scanner/releases/latest"
RELEASES_PAGE_URL = "https://github.com/cwilson-111/Storage-Scanner/releases/latest"

_LAST_CHECK_KEY = "last_update_check_at"
_MIN_INTERVAL_HOURS = 24
_REQUEST_TIMEOUT_SECONDS = 3


def parse_version(tag):
    """"v1.2.3" -> (1, 2, 3). Returns None for anything that isn't a plain
    semver-shaped tag — a dev build's "0.0.0-dev", a "main"-stamped build,
    or anything unexpected from the API is deliberately never treated as
    a comparable version, rather than risk a wrong comparison."""
    if not tag:
        return None
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def is_newer(candidate_tag, current_tag):
    candidate = parse_version(candidate_tag)
    current = parse_version(current_tag)
    if candidate is None or current is None:
        return False
    return candidate > current


def _fetch_latest_release_tag():
    try:
        request = urllib.request.Request(
            RELEASES_API_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "StorageScanner-UpdateCheck",
            },
        )
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            data = json.load(response)
        return data.get("tag_name")
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        logger.debug("Update check request failed", exc_info=True)
        return None


def _should_check_now():
    last_checked = get_app_metadata(_LAST_CHECK_KEY)
    if not last_checked:
        return True
    try:
        elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last_checked)
    except ValueError:
        return True
    return elapsed.total_seconds() >= _MIN_INTERVAL_HOURS * 3600


def check_for_update(current_version=None):
    """Returns the newer release tag string if one is available, else None.
    Meant to be called from a background thread — this blocks on a network
    request (bounded by _REQUEST_TIMEOUT_SECONDS) when a check is due.
    """
    current_version = current_version or CURRENT_VERSION
    try:
        if not _should_check_now():
            return None
        set_app_metadata(_LAST_CHECK_KEY, datetime.now(timezone.utc).isoformat())
        latest_tag = _fetch_latest_release_tag()
        if latest_tag and is_newer(latest_tag, current_version):
            return latest_tag
        return None
    except Exception:  # noqa: BLE001 - an update check must never break startup
        logger.exception("Update check failed unexpectedly")
        return None
