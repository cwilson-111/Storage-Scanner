"""Review-first cleanup recommendations.

Every recommendation names *why* it was flagged, an estimated recoverable
size, a risk level, and a proposed action — never a silent or "trust me"
deletion. Two categories ship here:

- Protected: system/app-managed or cloud-placeholder paths, flagged so the
  UI can refuse to let you delete them, not so it can suggest removing them.
- Review candidate: large files that haven't been touched in a long time —
  a *lead worth checking yourself*, not a verified-safe deletion. There's no
  signal available here (or anywhere in a metadata-only scan) that proves a
  file is safe to remove — only size and mtime/atime — which is why this
  stays a "review" category rather than a "safe" one.

Duplicate-candidate recommendations are built separately, from whatever the
existing duplicate-hashing pipeline (DuplicatesMixin) already found — see
build_duplicate_recommendations() — since that's real content-hash evidence,
not a metadata heuristic, and re-deriving it here would mean hashing files
twice. That evidence is byte-for-byte for files up to three hash windows in
size, but only a sample (first, middle and last window) above that — see
is_sampled_duplicate() — and the recommendation text says which.

A "safe candidate: superseded installer" category from the product roadmap
is deliberately not implemented: reliably detecting that a newer version of
some installer is already installed isn't possible from file metadata
alone, and a wrong "safe" label is actively harmful, not just unhelpful.

A fourth category, Orphaned install (find_orphaned_install_folders), is
*not* the same false-positive trap as the rejected "superseded installer"
idea above, despite the surface similarity ("this app-related thing looks
unnecessary"): it matches a literal InstallLocation string this app itself
previously observed registered to a real installed app (via
storage_scanner.installed_apps + history.record_install_locations_snapshot),
never something inferred from file naming or metadata heuristics. The
tradeoff for that reliability is temporal, not heuristic: it can only ever
flag a location the app has watched disappear from the registry across two
or more of its own snapshots, so it finds nothing on the very first run.
"""

import os
import time
from collections import namedtuple

from storage_scanner.formatting import human_size
from storage_scanner.settings import DEFAULT_DUPLICATE_EXCLUDES, DUPLICATE_HASH_CHUNK_BYTES

DEFAULT_OLD_DAYS = 180
DEFAULT_LARGE_BYTES = 100 * 1024 * 1024  # 100 MB

Recommendation = namedtuple(
    "Recommendation",
    ["node", "category", "reason", "risk", "recoverable_bytes", "action"],
)

# Categories, in the order they should be reviewed: protected first (so it's
# clear what's off-limits), then the candidate types.
CATEGORY_PROTECTED = "Protected"
CATEGORY_REVIEW = "Review candidate"
CATEGORY_DUPLICATE = "Duplicate candidate"
CATEGORY_ORPHANED_INSTALL = "Orphaned install"


def is_protected_path(path):
    """True if `path` falls under an OS/app-managed location this app
    already refuses to touch during duplicate scans — same list, reused
    here so "Protected" means the same thing everywhere in the app."""
    normalized = os.path.normcase(os.path.normpath(path))
    return any(os.path.normcase(marker) in normalized for marker in DEFAULT_DUPLICATE_EXCLUDES)


def _last_touched(node):
    """The more recent of mtime/atime, as a weak "last used" signal.

    Access time alone is unreliable (many filesystems update it lazily, or
    not at all, for performance), so this only ever makes a file look more
    *recently* touched than mtime suggests — never less. That asymmetry is
    intentional: it biases the "old" category toward fewer false positives.
    """
    return max(node.mtime, node.atime)


def find_protected_and_review_candidates(
    root_node,
    now=None,
    old_days=DEFAULT_OLD_DAYS,
    large_bytes=DEFAULT_LARGE_BYTES,
):
    """Walk the scanned tree and flag Protected and Review-candidate files.

    Directories are only used for traversal; recommendations are always
    against individual files, since that's what "recoverable bytes" and a
    delete action actually apply to.
    """
    if now is None:
        now = time.time()
    cutoff = now - old_days * 86400

    recommendations = []
    stack = [root_node]
    while stack:
        node = stack.pop()
        if node.is_dir:
            stack.extend(node.children)
            continue
        if node.hardlink_dup or node.size <= 0:
            continue  # nothing recoverable here; the "real" link is elsewhere

        protected = is_protected_path(node.path) or node.is_cloud_placeholder
        if protected:
            reason = (
                "Cloud placeholder file — not stored locally, nothing to reclaim."
                if node.is_cloud_placeholder
                else "Located in an OS or application-managed path."
            )
            recommendations.append(
                Recommendation(
                    node=node,
                    category=CATEGORY_PROTECTED,
                    reason=reason,
                    risk="N/A",
                    recoverable_bytes=0,
                    action="Leave alone",
                )
            )
            continue

        if node.size >= large_bytes and _last_touched(node) < cutoff:
            days_old = int((now - _last_touched(node)) / 86400)
            recommendations.append(
                Recommendation(
                    node=node,
                    category=CATEGORY_REVIEW,
                    reason=(
                        f"Large file ({node.size / (1024**2):,.0f} MB) not modified "
                        f"or accessed in ~{days_old:,} days — worth checking "
                        f"whether you still need it."
                    ),
                    risk="Medium — not verified safe, just a candidate to look at",
                    recoverable_bytes=node.size,
                    action="Review, then delete or archive if unneeded",
                )
            )

    recommendations.sort(key=lambda r: r.recoverable_bytes, reverse=True)
    return recommendations


# Explicit .lower() rather than os.path.normcase(): normcase only folds
# case on Windows, but "Downloads"/"Desktop" are capitalized on macOS/Linux
# too, so relying on it would miss every match there.
_TRANSIENT_MARKERS = ("downloads", "desktop", "tmp", "temp")


def _is_transient_path(node):
    normalized = node.path.lower()
    return any(marker in normalized for marker in _TRANSIENT_MARKERS)


def pick_keeper(nodes):
    """Choose which node in a duplicate-content group to keep.

    Preference order: a copy outside a Downloads/Desktop/Temp-style path
    over one inside it (those are the folders duplicates typically get left
    in), then the oldest by mtime (more likely the original), then the
    shortest path as a final, arbitrary tiebreak. This is a *default
    suggestion*, not an automatic action — the UI shows the reasoning and
    lets you override it.
    """
    return min(
        nodes,
        key=lambda n: (_is_transient_path(n), n.mtime, len(n.path)),
    )


def keeper_reason(keeper, nodes):
    """Human-readable reason pick_keeper() chose `keeper` among `nodes`.

    Walks the same criteria in the same order pick_keeper() uses, and
    reports the first one that actually distinguished the keeper from at
    least one other copy — so the explanation always matches the decision.
    """
    others = [n for n in nodes if n is not keeper]
    if not others:
        return "Only copy in this group."

    if not _is_transient_path(keeper) and any(_is_transient_path(n) for n in others):
        return (
            "Not in a Downloads/Desktop/Temp folder, unlike at least one "
            "other copy in this group."
        )
    if any(n.mtime > keeper.mtime for n in others):
        return "Oldest modified date among the copies in this group — likely the original."
    return "Tiebreak (shortest path) among otherwise-identical copies."


def find_orphaned_install_folders(root_node, orphaned_locations):
    """Flag directories that exactly match a location this app has
    previously seen registered as some app's InstallLocation, where that
    app is no longer installed (per history.get_orphaned_install_locations
    -- `orphaned_locations` here is that same result, reduced to just the
    already-normalized install_location strings).

    Unlike find_protected_and_review_candidates, this walk cares about
    *directories*, not files -- an orphaned install is a whole folder, not
    an individual file inside it. On a match, the folder itself is
    flagged and its children are never independently walked or flagged a
    second time -- deleting the folder already reclaims everything inside
    it, so a second row for e.g. one of its .dll files would double-count
    the same bytes and give the user two separate "delete this" actions
    for what's really one decision.

    Risk is deliberately "Medium," not a "safe/verified" label: the
    matched InstallLocation is solid *registry* evidence the owning app
    is gone, but says nothing about whether the folder still holds real
    user data (save files, exported settings) worth keeping regardless.
    """
    recommendations = []
    stack = [root_node]
    while stack:
        node = stack.pop()
        if not node.is_dir:
            continue

        normalized = os.path.normcase(os.path.normpath(node.path))
        if normalized in orphaned_locations:
            recommendations.append(
                Recommendation(
                    node=node,
                    category=CATEGORY_ORPHANED_INSTALL,
                    reason=(
                        "Matches a location this app was previously seen "
                        "installed to, but that app is no longer installed."
                    ),
                    risk=(
                        "Medium — folder may still contain user data even though "
                        "the app is uninstalled"
                    ),
                    recoverable_bytes=node.size,
                    action="Review, then delete if no longer needed",
                )
            )
            continue  # don't also flag anything nested inside it

        stack.extend(node.children)

    recommendations.sort(key=lambda r: r.recoverable_bytes, reverse=True)
    return recommendations


def _drop_nested_under(recommendations, container_paths):
    """Strip any recommendation whose node.path falls under one of
    `container_paths` (directory paths -- raw or already-normalized,
    normalized here either way -- each treated as a path *prefix*, not a
    substring: "C:\\Foo" excludes "C:\\Foo\\bar.txt" but not an unrelated
    "C:\\Foo2\\bar.txt").

    Used to keep an orphaned-install folder's own recommendation as the
    single actionable row for that space, once one is found -- without
    this, a large old file inside that same folder could *also* show up
    as its own separate Review candidate, double-counting the same bytes
    in the "potentially recoverable" total and giving the user two
    different rows for what's really one decision.
    """
    if not container_paths:
        return recommendations
    prefixes = []
    for path in container_paths:
        normalized = os.path.normcase(os.path.normpath(path))
        prefixes.append(normalized if normalized.endswith(os.sep) else normalized + os.sep)
    kept = []
    for rec in recommendations:
        normalized = os.path.normcase(os.path.normpath(rec.node.path))
        if any(normalized.startswith(prefix) for prefix in prefixes):
            continue
        kept.append(rec)
    return kept


def is_sampled_duplicate(size):
    """True when a duplicate match of files this size was only sampled.

    DuplicatesMixin hashes the first, middle and last
    DUPLICATE_HASH_CHUNK_BYTES of each candidate. Up to three windows'
    worth of bytes those windows cover the whole file, so a match is
    byte-exact; beyond that, the bytes between the windows were never
    compared.
    """
    return size > 3 * DUPLICATE_HASH_CHUNK_BYTES
def get_sampled_duplicates_from_groups(target_nodes, duplicate_groups):
    """Identify which nodes in target_nodes come from sampled duplicate groups.

    Returns a tuple: (sampled_count, sampled_nodes_set)
    where sampled_count is the number of target_nodes that are in groups
    larger than 3 * DUPLICATE_HASH_CHUNK_BYTES (meaning only first/middle/last
    1 MB was compared, not the full content).
    """
    sampled_set = set()
    target_set = set(target_nodes)

    for size, _digest, nodes in duplicate_groups:
        if is_sampled_duplicate(size):
            # This group was only sampled, so any target nodes in it are sampled
            for node in nodes:
                if node in target_set:
                    sampled_set.add(node)

    return len(sampled_set), sampled_set


def build_duplicate_recommendations(duplicate_groups):
    """Turn `_find_duplicate_files()`'s output — [(size, digest, nodes), ...]
    — into Recommendations: keep one file per group, flag the rest.
    """
    window = human_size(DUPLICATE_HASH_CHUNK_BYTES)
    recommendations = []
    for size, _digest, nodes in duplicate_groups:
        keeper = pick_keeper(nodes)
        if is_sampled_duplicate(size):
            reason = (
                f"Same size and same first, middle and last {window} as {keeper.path} "
                "— recommended keeper."
            )
            risk = "Medium — sampled match; bytes between the compared windows weren't checked"
        else:
            reason = f"Identical content to {keeper.path} — recommended keeper."
            risk = "Low — every byte compared by hash"
        for node in nodes:
            if node is keeper:
                continue
            recommendations.append(
                Recommendation(
                    node=node,
                    category=CATEGORY_DUPLICATE,
                    reason=reason,
                    risk=risk,
                    recoverable_bytes=size,
                    action="Delete this copy (to Recycle Bin/Trash)",
                )
            )
    recommendations.sort(key=lambda r: r.recoverable_bytes, reverse=True)
    return recommendations
