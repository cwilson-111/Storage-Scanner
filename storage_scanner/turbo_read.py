"""Reads one folder of an NTFS volume for Turbo Scan, as a finalized Node.

scan_subtree_using_cache() is the shared entry point both
turbo_scan._run_turbo_in_process and mft_scan_cli.run_mft_scan (the headless
elevated-helper subprocess) call. When the volume's cache
(storage_scanner.turbo_cache) is valid, it's brought up to date from the USN
journal (storage_scanner.usn_journal) and only the requested folder's
records are loaded from it; otherwise every MFT record is read and parsed,
and the result cached for next time. Caching is a pure optimization: any
failure anywhere in that path (a locked/damaged cache DB, no USN journal
support, a wrapped/recreated journal) just falls back to the plain full read
Turbo Scan always did before caching existed, and never changes what a scan
returns. It does report which path it took (MftRead), for the scan-details
strip.
"""

import os
from dataclasses import dataclass
from typing import Optional

from storage_scanner import mft_parser, mft_scan, turbo_cache, usn_journal
from storage_scanner.logging_setup import logger

# Every NTFS volume's root directory is always MFT record #5 -- matches
# mft_scan._ROOT_RECORD_NUMBER, duplicated here rather than imported per
# this codebase's existing convention (mft_parser.py/mft_scan.py each keep
# their own copy of this same mask rather than cross-import a private
# constant for something this fundamental).
_ROOT_RECORD_NUMBER = 5
_FRN_RECORD_NUMBER_MASK = 0x0000FFFFFFFFFFFF


class _CacheOutdated(usn_journal.UsnJournalError):
    """The USN journal works but can't account for everything since the
    cache was built. Its message is short enough for the scan-details strip."""


@dataclass(frozen=True)
class MftRead:
    """How a Turbo Scan got the volume's records: the cache refreshed from
    the USN journal, or a full MFT read -- and, for a full read, why the
    cache couldn't be used (a short phrase; the log has the detail).
    Crosses the elevated helper's process boundary as a dict
    (to_dict/from_dict)."""

    incremental: bool
    full_read_reason: Optional[str] = None  # None when incremental

    def describe(self):
        if self.incremental:
            return "Incremental (USN journal)"
        return f"Full ({self.full_read_reason})" if self.full_read_reason else "Full"

    def to_dict(self):
        return {"incremental": self.incremental, "full_read_reason": self.full_read_reason}

    @classmethod
    def from_dict(cls, data):
        return cls(
            incremental=bool(data.get("incremental")),
            full_read_reason=data.get("full_read_reason"),
        )


def _relative_parts(root_path, target_path):
    """`target_path`'s components below `root_path`."""
    relative = os.path.relpath(os.path.abspath(target_path), root_path)
    return [part for part in relative.split(os.sep) if part and part != os.curdir]


def find_subtree_node(root_node, target_path):
    """Walk down from `root_node` (built for an entire volume) to the Node
    matching `target_path` exactly.

    Used after a full MFT read, whose records arrive in arbitrary order, so
    there's no way to know which ones live under a requested subfolder
    without building the whole volume's tree first. (A rescan from the
    cache doesn't need this: turbo_cache.find_record_by_path resolves the
    folder in the database and loads only what's under it.)

    Raises if any path component isn't found in the tree (a real path that
    isn't reachable in the scanned tree signals something is wrong, not a
    case to silently paper over) -- sending the caller to the fallback
    engine rather than trust a wrong result.
    """
    node = root_node
    for part in _relative_parts(root_node.path, target_path):
        match = next(
            (c for c in node.children if os.path.normcase(c.name) == os.path.normcase(part)),
            None,
        )
        if match is None:
            raise RuntimeError(
                f"Turbo Scan could not locate {os.path.abspath(target_path)!r} "
                "in the volume tree"
            )
        node = match
    return node


def scan_subtree_using_cache(record_source, volume_root, target_path, progress_q, cancel_event):
    """(Node for `target_path`, finalized, MftRead). When this volume's
    cache is valid, applies the USN journal's changes to it and loads only
    the records under `target_path` -- a rescan of one folder costs that
    folder, not the volume. Otherwise reads and parses every MFT record,
    caches them for next time, and slices `target_path` out of the whole
    volume's tree. `progress_q` may be None (the headless mft_scan_cli.py
    elevated-helper path relays progress through a file instead).

    Never raises for a cache/journal-layer problem specifically: no cache
    yet, a locked or damaged cache DB, no USN journal on this volume, a
    journal ID mismatch or wrapped journal since the cache was built all
    fall straight through to a full read -- caching is a pure optimization
    layered on top of the exact full-read behavior Turbo Scan always had,
    not a prerequisite for a scan to succeed.

    Does raise if `cancel_event` is set mid-scan, or if `target_path`
    isn't in the volume -- both mean no trustworthy result, and propagate
    up to turbo_scan.scan_with_best_engine's broad except, which falls back to the
    Compatible engine.
    """
    volume_serial = record_source.volume_serial
    cached = None
    try:
        # init_cache_db() is a cheap, idempotent CREATE TABLE IF NOT EXISTS
        # -- called here rather than once at app startup because this same
        # function is also the entry point for mft_scan_cli.py's headless
        # elevated-helper subprocess, which never runs app.py's own
        # startup (see history.init_history_db()'s call site there) at all.
        turbo_cache.init_cache_db()
        cached = turbo_cache.get_cached_volume(volume_serial)
        reason = "first scan of this drive"
    except Exception:  # noqa: BLE001 - caching is a pure optimization, never fatal to the scan
        logger.warning(
            "Turbo Scan cache is unavailable for %r; scanning without it",
            volume_root,
            exc_info=True,
        )
        reason = "cache unavailable"

    if cached is not None and cached["record_size"] != record_source.record_size:
        reason = "drive layout changed"
    elif cached is not None:
        try:
            node = _try_incremental_scan(
                record_source, cached, volume_root, target_path, progress_q, cancel_event
            )
        except (usn_journal.UsnJournalError, turbo_cache.TurboCacheCorruptError) as exc:
            logger.info(
                "Turbo Scan cache for %r could not be refreshed incrementally, "
                "falling back to a full scan: %s",
                volume_root,
                exc,
            )
            if isinstance(exc, turbo_cache.TurboCacheCorruptError):
                reason = "cache was corrupt"
            elif isinstance(exc, _CacheOutdated):
                reason = str(exc)
            else:
                reason = "USN journal unreadable"
            try:
                turbo_cache.invalidate_volume(volume_serial)
            except Exception:  # noqa: BLE001 - best-effort cleanup only
                logger.warning(
                    "Could not invalidate Turbo Scan cache for %r",
                    volume_root,
                    exc_info=True,
                )
        else:
            if node is not None:
                return node, MftRead(incremental=True)
            reason = "no journal position cached"

    records = _full_scan_and_cache(
        record_source, volume_serial, volume_root, progress_q, cancel_event
    )
    node = _subtree_from_records(records, volume_root, target_path)
    return node, MftRead(incremental=False, full_read_reason=reason)


def _full_scan_and_cache(record_source, volume_serial, volume_root, progress_q, cancel_event):
    records = []
    for record_number in range(record_source.record_count):
        if cancel_event.is_set():
            # Raise rather than return the partial list built so far --
            # Turbo Scan's contract (see turbo_scan.scan_with_best_engine's
            # docstring) is that it never hands back a partial
            # tree; the caller's broad except already treats any Turbo
            # failure, cancellation included, as "fall back to Compatible."
            raise RuntimeError("Turbo Scan was cancelled")
        parsed = mft_parser.parse_base_record(record_number, record_source)
        if parsed is not None:
            records.append(parsed)
            if progress_q is not None and len(records) % 5000 == 0:
                progress_q.put(("progress", len(records)))

    root_frn = next(
        (r.frn for r in records if (r.frn & _FRN_RECORD_NUMBER_MASK) == _ROOT_RECORD_NUMBER),
        None,
    )
    if root_frn is not None:
        try:
            turbo_cache.save_full_scan(
                volume_serial,
                volume_root,
                root_frn,
                record_source.record_size,
                records,
            )
            # Captured only now, after the scan (and the cache write of its
            # results) has fully finished -- capturing it any earlier would
            # risk losing changes made while the scan itself was still
            # running.
            state = usn_journal.ensure_journal(record_source.raw_handle)
            turbo_cache.save_journal_cursor(volume_serial, state.journal_id, state.next_usn)
        except Exception:  # noqa: BLE001 - caching is a pure optimization, never fatal to the scan
            logger.warning(
                "Could not cache this Turbo Scan of %r; the next scan of this "
                "volume will do a full rebuild again",
                volume_root,
                exc_info=True,
            )
    return records


def _try_incremental_scan(
    record_source, cached, volume_root, target_path, progress_q, cancel_event
):
    """Brings the cache up to date from the USN journal, then returns the
    finalized Node for `target_path` built from just its cached records --
    or None if there's no usable cursor to refresh from (the caller then
    does a full scan). Raises usn_journal.UsnJournalError for every other
    reason a refresh can't proceed, and turbo_cache.TurboCacheCorruptError
    for a damaged cache -- the caller invalidates the cache and falls back
    to a full scan in both cases, just with a logged reason.

    Posts progress the same way _full_scan_and_cache does -- previously
    this function posted nothing at all, regardless of engine path
    (in-process or elevated-helper), which is a real, separate gap from
    the elevated-helper-specific process-boundary fix: even an
    already-elevated in-process Turbo Scan going through a cached
    incremental refresh showed zero progress of any kind, found via a
    real user report. A `("status", text)` message (main_window._poll_
    progress just sets the status bar text verbatim) marks the start of
    each phase -- reparsing the dirty records, then loading the requested
    folder's records; `("progress", n)` during the reparse loop matches
    _full_scan_and_cache's own existing convention, at a finer interval
    since a dirty set is typically far smaller than a full volume.
    """
    if cached["next_usn"] is None:
        return None  # cached records exist, but no journal cursor was ever established

    handle = record_source.raw_handle
    state = usn_journal.query_journal(handle)  # raises UsnJournalError if no journal exists
    if state.journal_id != cached["usn_journal_id"]:
        raise _CacheOutdated("USN journal was recreated")
    if cached["next_usn"] < state.lowest_valid_usn:
        raise _CacheOutdated("USN journal wrapped since last scan")

    dirty, new_next_usn = usn_journal.read_journal_changes(
        handle,
        state.journal_id,
        cached["next_usn"],
        lowest_valid_usn=state.lowest_valid_usn,
    )

    if progress_q is not None and dirty:
        progress_q.put(("status", f"Turbo Scan: applying {len(dirty):,} change(s)…"))

    upserts, deletes = [], []
    for i, dirty_record in enumerate(dirty, start=1):
        if cancel_event.is_set():
            # As in _full_scan_and_cache: raise rather than return None,
            # which this function's own contract reserves for "no usable
            # cursor" -- conflating that with "cancelled" would send a
            # cancelled scan straight into a full, wasted volume rescan
            # instead of aborting the whole Turbo attempt immediately.
            raise RuntimeError("Turbo Scan was cancelled")
        parsed = mft_parser.parse_base_record(dirty_record.record_number, record_source)
        if parsed is None:
            deletes.append(dirty_record.record_number)
        else:
            upserts.append(parsed)
        if progress_q is not None and i % 200 == 0:
            progress_q.put(("progress", i))

    turbo_cache.apply_incremental_changes(cached["volume_serial"], upserts, deletes, new_next_usn)

    if progress_q is not None:
        progress_q.put(("status", "Turbo Scan: loading cached records…"))
    return _subtree_from_cache(cached, volume_root, target_path)


def _subtree_from_cache(cached, volume_root, target_path):
    """The finalized Node for `target_path`, built from only the cached
    records under it."""
    volume_root = os.path.abspath(volume_root)
    found = turbo_cache.find_record_by_path(
        cached["volume_serial"], cached["root_frn"], _relative_parts(volume_root, target_path)
    )
    if found is None:
        raise RuntimeError(
            f"Turbo Scan could not locate {os.path.abspath(target_path)!r} in the volume tree"
        )
    target_record, actual_parts = found
    # On-disk spelling, as a full read's tree would have it.
    path = os.path.join(volume_root, *actual_parts)

    if not target_record.is_directory:
        node = mft_scan.file_node(target_record, actual_parts[-1], path)
        return mft_scan.finalize_subtree(node, {id(node): target_record.frn})

    records = turbo_cache.load_subtree_records(cached["volume_serial"], target_record)
    # The requested folder is the root here, so a reparse point is followed
    # (mft_scan._make_node's root rule) without reroot_if_reparse_point.
    root_node, orphan_count, frn_by_node_id = mft_scan.build_tree(
        records,
        root_path=path,
        root_record_number=target_record.frn & _FRN_RECORD_NUMBER_MASK,
    )
    if orphan_count:
        logger.warning("Turbo Scan of %r had %d unreachable record(s)", path, orphan_count)
    logger.debug("Turbo Scan of %r: %d cached records loaded", path, len(records))
    return mft_scan.finalize_subtree(root_node, frn_by_node_id)


def _subtree_from_records(records, volume_root, target_path):
    """The finalized Node for `target_path`, sliced out of a whole-volume
    tree built from a full read's `records`."""
    root_node, orphan_count, frn_by_node_id = mft_scan.build_tree(records, root_path=volume_root)
    if root_node is None:
        raise RuntimeError("Turbo Scan could not locate a root directory record")
    if orphan_count:
        logger.warning("Turbo Scan of %r had %d unreachable record(s)", volume_root, orphan_count)
    subtree_node = find_subtree_node(root_node, target_path)
    # A requested folder that's itself a reparse point (junction/symlink)
    # must still be followed, matching scanner.scan()'s own root handling
    # -- see mft_scan.reroot_if_reparse_point's docstring for why this
    # can't just be decided up front, during build_tree().
    subtree_node = mft_scan.reroot_if_reparse_point(
        subtree_node, target_path, records, frn_by_node_id
    )
    # Hard-link dedup is deliberately scoped to just this subtree, not the
    # whole volume -- see mft_scan.finalize_subtree's docstring for why.
    return mft_scan.finalize_subtree(subtree_node, frn_by_node_id)
