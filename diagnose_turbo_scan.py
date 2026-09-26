#!/usr/bin/env python3
"""One-off diagnostic for the compare_scan_engines.py "could not locate ...
in the volume tree" failure -- not part of the app, delete after use.

Reproduces exactly what turbo_scan._run_turbo_in_process does internally
(read every MFT record, parse it, build the whole-volume tree) but prints
detail at each stage so we can see WHERE real records are going missing,
rather than just the final "not found" symptom.

Must be run from an elevated terminal, same as compare_scan_engines.py.

Usage: python diagnose_turbo_scan.py C:\\
"""

import sys

from storage_scanner import mft_parser, mft_scan, mft_volume
from storage_scanner.drive_info import get_volume_root
from storage_scanner.platform_support import IS_ROOT

_FRN_RECORD_NUMBER_MASK = 0x0000FFFFFFFFFFFF


def _classify_orphans(records, root_frn):
    """Reproduces mft_scan.build_tree's reachability walk, but instead of
    building real Node objects, records which (child_frn, parent_frn)
    occurrences got attached -- then explains every occurrence that
    didn't, bucketed by why, with a few example names per bucket."""
    frn_to_record = {r.frn: r for r in records}

    children_by_parent = {}
    for record in records:
        if record.frn == root_frn:
            continue
        for name_attr in record.names:
            children_by_parent.setdefault(name_attr.parent_frn, []).append((record, name_attr))

    attached = set()
    visited_dir_frns = {root_frn}
    stack = [root_frn]
    while stack:
        frn = stack.pop()
        for child_record, name_attr in children_by_parent.get(frn, []):
            attached.add((child_record.frn, name_attr.parent_frn))
            is_dir = child_record.is_directory and not child_record.is_reparse_point
            if is_dir and child_record.frn not in visited_dir_frns:
                visited_dir_frns.add(child_record.frn)
                stack.append(child_record.frn)

    buckets = {
        "missing_parent": [],
        "parent_is_reparse_point": [],
        "parent_not_a_directory": [],
        "cascading_unreachable_parent": [],
    }
    for record in records:
        if record.frn == root_frn:
            continue
        for name_attr in record.names:
            if (record.frn, name_attr.parent_frn) in attached:
                continue
            parent_record = frn_to_record.get(name_attr.parent_frn)
            if parent_record is None:
                bucket = "missing_parent"
            elif parent_record.is_reparse_point:
                bucket = "parent_is_reparse_point"
            elif not parent_record.is_directory:
                bucket = "parent_not_a_directory"
            else:
                bucket = "cascading_unreachable_parent"
            buckets[bucket].append(name_attr.name)

    print("\nOrphan breakdown (why each unattached occurrence wasn't reachable):")
    descriptions = {
        "missing_parent": "parent record missing entirely",
        "parent_is_reparse_point": (
            "parent is a reparse point (expected leaf -- matches the "
            "Compatible engine's own behavior, not a bug)"
        ),
        "parent_not_a_directory": "parent record exists but isn't a directory (corrupt link?)",
        "cascading_unreachable_parent": (
            "parent exists and is a real directory, but IT was never " "reached either (cascading)"
        ),
    }
    for key, names in buckets.items():
        print(f"  {descriptions[key]}: {len(names):,}")
        if names:
            print(f"    examples: {names[:8]}")


def main(drive):
    if not IS_ROOT:
        print("Must run elevated.", file=sys.stderr)
        return 1

    volume_root = get_volume_root(drive)
    print(f"Opening {volume_root!r} ...")
    source = mft_volume.open_record_source(volume_root)
    try:
        print(f"record_count (sum of decoded $MFT extents): {source.record_count:,}")
        print(f"record_size: {source._record_size}")
        print(f"extents (start_record, record_count, lcn): {len(source._extents)} extent(s)")
        for start, count, lcn in source._extents[:10]:
            print(f"  start_record={start:,} count={count:,} lcn={lcn:,}")
        if len(source._extents) > 10:
            print(f"  ... and {len(source._extents) - 10} more")

        parsed_count = 0
        failed_count = 0
        for n in range(source.record_count):
            try:
                r = mft_parser.parse_base_record(n, source)
            except Exception as exc:
                failed_count += 1
                if failed_count <= 5:
                    print(f"  record {n}: EXCEPTION during parse: {exc!r}")
                continue
            if r is not None:
                parsed_count += 1
        print(f"parsed OK (usable base records): {parsed_count:,}")
        print(f"raised an exception while parsing: {failed_count:,}")
        print("(the rest were None: unused slots, extension records, bad signature, etc.)")
    finally:
        source.close()

    # Rebuild for real (a second full read -- same as the app would do)
    print("\nRe-reading and building the whole-volume tree ...")
    source = mft_volume.open_record_source(volume_root)
    try:
        records = []
        for n in range(source.record_count):
            r = mft_parser.parse_base_record(n, source)
            if r is not None:
                records.append(r)
    finally:
        source.close()

    root_node, orphan_count, _row_frns = mft_scan.build_tree(records, root_path=volume_root)
    if root_node is None:
        print("build_tree returned None -- no record 5 (root) found or it's not a directory!")
        return 1

    print(f"orphan_count: {orphan_count:,}")
    print(f"root_node.file_count: {root_node.file_count:,}")
    print(f"root_node.size: {root_node.size:,}")

    child_names = sorted(c.name for c in root_node.children)
    print(f"\nroot has {len(child_names)} direct children:")
    print(f"  {child_names}")

    windows_node = next((c for c in root_node.children if c.name.lower() == "windows"), None)
    users_node = next((c for c in root_node.children if c.name.lower() == "users"), None)
    print(f"\n'Windows' found as root child: {windows_node is not None}")
    print(f"'Users' found as root child: {users_node is not None}")

    if windows_node is not None:
        print(f"  Windows.children count: {len(windows_node.children)}")
        fonts_node = next((c for c in windows_node.children if c.name.lower() == "fonts"), None)
        print(f"  'Fonts' found under Windows: {fonts_node is not None}")

    if users_node is not None:
        print(f"  Users.children count: {len(users_node.children)}")
        danet_node = next((c for c in users_node.children if c.name.lower() == "danet"), None)
        print(f"  'danet' found under Users: {danet_node is not None}")
        if danet_node is not None:
            print(f"    danet.children count: {len(danet_node.children)}")
            docs_node = next(
                (c for c in danet_node.children if c.name.lower() == "documents"), None
            )
            print(f"    'Documents' found under danet: {docs_node is not None}")

    root_record = next((r for r in records if (r.frn & _FRN_RECORD_NUMBER_MASK) == 5), None)
    if root_record is not None:
        _classify_orphans(records, root_record.frn)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "C:\\"))
