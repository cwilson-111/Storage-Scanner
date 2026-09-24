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
twice.

A "safe candidate: superseded installer" category from the product roadmap
is deliberately not implemented: reliably detecting that a newer version of
some installer is already installed isn't possible from file metadata
alone, and a wrong "safe" label is actively harmful, not just unhelpful.
"""

import os
import time
from collections import namedtuple

from storage_scanner.settings import DEFAULT_DUPLICATE_EXCLUDES

DEFAULT_OLD_DAYS = 180
DEFAULT_LARGE_BYTES = 100 * 1024 * 1024  # 100 MB

Recommendation = namedtuple(
    "Recommendation",
    ["node", "category", "reason", "risk", "recoverable_bytes", "action"],
)

# Categories, in the order they should be reviewed: protected first (so it's
# clear what's off-limits), then the two candidate types.
CATEGORY_PROTECTED = "Protected"
CATEGORY_REVIEW = "Review candidate"
CATEGORY_DUPLICATE = "Duplicate candidate"


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
        return "Oldest modified date among identical copies — likely the original."
    return "Tiebreak (shortest path) among otherwise-identical copies."


def build_duplicate_recommendations(duplicate_groups):
    """Turn `_find_duplicate_files()`'s output — [(size, digest, nodes), ...]
    — into Recommendations: keep one file per group, flag the rest.
    """
    recommendations = []
    for size, _digest, nodes in duplicate_groups:
        keeper = pick_keeper(nodes)
        for node in nodes:
            if node is keeper:
                continue
            recommendations.append(
                Recommendation(
                    node=node,
                    category=CATEGORY_DUPLICATE,
                    reason=f"Identical content to {keeper.path} — recommended keeper.",
                    risk="Low — exact content match, confirmed by full hash",
                    recoverable_bytes=size,
                    action="Delete this copy (to Recycle Bin/Trash)",
                )
            )
    recommendations.sort(key=lambda r: r.recoverable_bytes, reverse=True)
    return recommendations
