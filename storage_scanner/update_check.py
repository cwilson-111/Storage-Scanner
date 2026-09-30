"""Lightweight update notice: on launch, check GitHub's public release API
for a newer version than the one currently running.

This is the only network call anywhere in the app (see PRIVACY.md). It:
- sends nothing about the user, their files, or their system — just an
  anonymous GET request to a public API endpoint;
- is made only by a released build: a run whose version isn't a release
  tag (from source, "0.0.0-dev"; a CI build of main, "main") never makes it;
- can be turned off: Settings ▸ "Check for updates on launch", or the
  STORAGE_SCANNER_NO_UPDATE_CHECK environment variable;
- checks at most once every 24 hours (tracked via history.py's
  app_metadata table), not on every single launch;
- never downloads or runs anything — the only outcome is a version-string
  comparison, and the only action available is a link to the Releases page
  for the user to open themselves.

A Data build (version "data-vX.Y.Z") compares against the newest data-v
pre-release, which /releases/latest never returns.

Every failure mode (offline, GitHub unreachable, malformed response, a
corrupt cached timestamp) resolves to "no update notice shown" — this must
never be able to disrupt startup or the rest of the app.
"""

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

from history import get_app_metadata, set_app_metadata
from storage_scanner.logging_setup import logger
from storage_scanner.version import __version__ as CURRENT_VERSION

_REPO = "https://api.github.com/repos/cwilson-111/Storage-Scanner"
RELEASES_API_URL = f"{_REPO}/releases/latest"
RELEASES_LIST_API_URL = f"{_REPO}/releases?per_page=30"

ENABLED_KEY = "update_check_enabled"
DISABLE_ENV_VAR = "STORAGE_SCANNER_NO_UPDATE_CHECK"
DATA_PREFIX = "data-"

_LAST_CHECK_KEY = "last_update_check_at"
_MIN_INTERVAL_HOURS = 24
_REQUEST_TIMEOUT_SECONDS = 3


def parse_version(tag):
    """ "v1.2.3" -> (1, 2, 3). Returns None for anything that isn't a plain
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


def release_page_url(tag):
    """The page the update notice links to for `tag`."""
    return f"https://github.com/cwilson-111/Storage-Scanner/releases/tag/{tag}"


def update_check_enabled():
    """False if the user turned the check off, in Settings or through
    DISABLE_ENV_VAR (any value but empty or "0")."""
    if os.environ.get(DISABLE_ENV_VAR, "").strip() not in ("", "0"):
        return False
    return get_app_metadata(ENABLED_KEY, "1") != "0"


def _get_json(url):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "StorageScanner-UpdateCheck",
        },
    )
    with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
        return json.load(response)


def _fetch_latest_release_tag():
    try:
        return _get_json(RELEASES_API_URL).get("tag_name")
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        logger.debug("Update check request failed", exc_info=True)
        return None


def _fetch_latest_data_release_tag():
    """The newest data-vX.Y.Z tag among recent releases (pre-releases
    included), or None."""
    try:
        releases = _get_json(RELEASES_LIST_API_URL)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        logger.debug("Update check request failed", exc_info=True)
        return None
    tags = [
        release.get("tag_name") or ""
        for release in releases
        if isinstance(release, dict) and not release.get("draft")
    ]
    data_tags = [
        tag
        for tag in tags
        if tag.startswith(DATA_PREFIX) and parse_version(tag[len(DATA_PREFIX) :])
    ]
    return max(data_tags, key=lambda tag: parse_version(tag[len(DATA_PREFIX) :]), default=None)


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
    data_build = current_version.startswith(DATA_PREFIX)
    current = current_version[len(DATA_PREFIX) :] if data_build else current_version
    try:
        # Not a release (a source run, a CI build of main): nothing to
        # compare with, so no request at all.
        if parse_version(current) is None or not update_check_enabled():
            return None
        if not _should_check_now():
            return None
        set_app_metadata(_LAST_CHECK_KEY, datetime.now(timezone.utc).isoformat())
        if data_build:
            latest_tag = _fetch_latest_data_release_tag()
            latest = latest_tag[len(DATA_PREFIX) :] if latest_tag else None
        else:
            latest_tag = latest = _fetch_latest_release_tag()
        if latest and is_newer(latest, current):
            return latest_tag
        return None
    except Exception:  # noqa: BLE001 - an update check must never break startup
        logger.exception("Update check failed unexpectedly")
        return None
