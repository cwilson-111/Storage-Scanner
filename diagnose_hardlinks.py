#!/usr/bin/env python3
"""One-off diagnostic for the compare_scan_engines.py C:\\Windows\\Fonts
hardlink_dup/size mismatches -- not part of the app, delete after use.

Scans the whole volume once (same cost as any other Turbo Scan) and, for
every parsed record whose $FILE_NAME matches one of the target names,
prints its FULL raw detail: how many names it has, each one's exact
parent_frn/namespace, and its $DATA sizing -- so we can see directly
whether these are genuinely multiple $FILE_NAME entries with DIFFERENT
parent_frn values (a real parsing bug) or something else.

Must be run from an elevated terminal, same as compare_scan_engines.py.

Usage: python diagnose_hardlinks.py C:\\ 8514fix.fon 8514fixe.fon
"""

import sys

from storage_scanner import mft_parser, mft_volume
from storage_scanner.drive_info import get_volume_root
from storage_scanner.platform_support import IS_ROOT


def main(drive, target_names):
    if not IS_ROOT:
        print("Must run elevated.", file=sys.stderr)
        return 1

    target_names_lower = {n.lower() for n in target_names}
    volume_root = get_volume_root(drive)
    print(f"Opening {volume_root!r} ...")
    source = mft_volume.open_record_source(volume_root)
    matches = []
    try:
        print(f"Scanning {source.record_count:,} records for {target_names!r} ...")
        for n in range(source.record_count):
            record = mft_parser.parse_base_record(n, source)
            if record is None:
                continue
            for name_attr in record.names:
                if name_attr.name.lower() in target_names_lower:
                    matches.append((n, record))
                    break
    finally:
        source.close()

    print(f"\nFound {len(matches)} record(s) with a matching name.\n")
    for record_number, record in matches:
        print(f"=== record #{record_number} (frn={record.frn}) ===")
        print(f"  is_directory: {record.is_directory}")
        print(f"  is_reparse_point: {record.is_reparse_point}")
        print(f"  logical_size: {record.logical_size:,}")
        print(f"  alloc_size: {record.alloc_size:,}")
        print(f"  names ({len(record.names)}):")
        for name_attr in record.names:
            print(
                f"    name={name_attr.name!r} parent_frn={name_attr.parent_frn} "
                f"namespace={name_attr.namespace}"
            )
        print()

    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <drive> <name1> [name2 ...]", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2:]))
