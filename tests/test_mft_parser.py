"""Tests for storage_scanner.mft_parser against hand-built MFT record bytes.

Every record here is assembled by hand (not via mft_parser's own private
structures) so these tests are a genuine black-box check of the on-disk
byte format the parser has to handle -- no admin rights or real NTFS
volume needed, per the Turbo Scan plan's testing strategy.
"""

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import mft_parser
from storage_scanner.mft_parser import parse_base_record, _pack_frn

RECORD_SIZE = 1024
SECTOR_SIZE = 512
USA_OFFSET = 48
USA_SIZE = RECORD_SIZE // SECTOR_SIZE + 1  # 3: one USN + one original per sector
FIRST_ATTR_OFFSET = ((USA_OFFSET + USA_SIZE * 2) + 7) // 8 * 8  # 8-byte aligned

_NAMESPACE_WIN32 = 1
_NAMESPACE_DOS = 2


class FakeRecordSource:
    """In-memory stand-in for the real volume reader: record number -> bytes."""

    def __init__(self, records_by_number):
        self._records = records_by_number

    def record_at(self, record_number):
        return self._records[record_number]


def _build_resident_attr(attr_type, value, attribute_id):
    header_len = 16 + 8  # common header + resident-variant header
    value_offset = header_len
    unpadded = value_offset + len(value)
    total_len = (unpadded + 7) // 8 * 8
    common = struct.pack("<IIBBHHH", attr_type, total_len, 0, 0, 0, 0, attribute_id)
    resident = struct.pack("<IHBB", len(value), value_offset, 0, 0)
    body = bytearray(common + resident + value)
    body.extend(b"\x00" * (total_len - len(body)))
    return bytes(body)


def _build_nonresident_attr(
    attr_type, *, allocated_size, real_size, initialized_size,
    attribute_id, compression_unit=0, data_runs=b"\x00", name_length=0,
):
    common_len = 16
    nrh_len = 48
    extra = 8 if compression_unit else 0
    data_runs_offset = common_len + nrh_len + extra
    unpadded = data_runs_offset + len(data_runs)
    total_len = (unpadded + 7) // 8 * 8
    common = struct.pack(
        "<IIBBHHH", attr_type, total_len, 1, name_length, 0, 0, attribute_id
    )
    nrh = struct.pack(
        "<QQHHIQQQ", 0, 0, data_runs_offset, compression_unit, 0,
        allocated_size, real_size, initialized_size,
    )
    body = bytearray(common + nrh)
    if compression_unit:
        body.extend(struct.pack("<Q", allocated_size))  # placeholder CompressedSize
    body.extend(data_runs)
    body.extend(b"\x00" * (total_len - len(body)))
    return bytes(body)


def _std_info_value(mtime_filetime=0, atime_filetime=0, file_attributes=0):
    return struct.pack("<QQQQI", 0, mtime_filetime, 0, atime_filetime, file_attributes)


def _file_name_value(parent_frn, name, namespace=_NAMESPACE_WIN32):
    fixed = struct.pack("<QQQQQQQII", parent_frn, 0, 0, 0, 0, 0, 0, 0, 0)
    name_bytes = name.encode("utf-16-le")
    return fixed + bytes([len(name), namespace]) + name_bytes


def _attribute_list_value(entries):
    """`entries` is a list of (attr_type, attribute_id, base_frn)."""
    out = bytearray()
    for attr_type, attribute_id, base_frn in entries:
        entry_len = 26  # no name; the fixed head(8) + tail(18) size
        head = struct.pack("<IHBB", attr_type, entry_len, 0, 0)
        tail = struct.pack("<QQH", 0, base_frn, attribute_id)
        out += head + tail
    return bytes(out)


def _stamp_fixups(record, usn=b"\x01\x00"):
    """Simulate real NTFS on-disk storage: relocate each sector-end's real
    bytes into the USA and overwrite them with a shared USN -- the exact
    inverse of what mft_parser._apply_fixups reverses."""
    record = bytearray(record)
    record[USA_OFFSET:USA_OFFSET + 2] = usn
    for i in range(1, USA_SIZE):
        sector_end = i * SECTOR_SIZE - 2
        original = bytes(record[sector_end:sector_end + 2])
        record[USA_OFFSET + 2 * i:USA_OFFSET + 2 * i + 2] = original
        record[sector_end:sector_end + 2] = usn
    return bytes(record)


def _assemble_record(
    record_number, attrs, *,
    in_use=True, is_directory=False, base_frn=0, sequence_number=1,
    hard_link_count=1, signature=b"FILE", corrupt_usa=False,
    record_size=RECORD_SIZE,
):
    """Frame a complete on-disk-shaped record around pre-built, already-
    ordered attribute bytes (`attrs`, sans the 0xFFFFFFFF end marker)."""
    attrs = bytes(attrs) + struct.pack("<I", mft_parser._ATTR_END_MARKER)

    bytes_in_use = FIRST_ATTR_OFFSET + len(attrs)
    if bytes_in_use > record_size:
        raise ValueError("record_size too small for these attributes in this test")

    flags = 0
    if in_use:
        flags |= mft_parser._RECORD_FLAG_IN_USE
    if is_directory:
        flags |= mft_parser._RECORD_FLAG_IS_DIRECTORY

    header = struct.pack(
        "<4sHHQHHHHIIQHHI",
        signature, USA_OFFSET, USA_SIZE, 0, sequence_number,
        hard_link_count, FIRST_ATTR_OFFSET, flags,
        bytes_in_use, record_size, base_frn, 0, 0, record_number,
    )

    buf = bytearray(record_size)
    buf[0:len(header)] = header
    buf[FIRST_ATTR_OFFSET:FIRST_ATTR_OFFSET + len(attrs)] = attrs

    stamped = bytearray(_stamp_fixups(bytes(buf)))
    if corrupt_usa:
        broken_off = 1 * SECTOR_SIZE - 2
        stamped[broken_off:broken_off + 2] = b"\xEE\xEE"
    return bytes(stamped)


def build_record(
    record_number, *,
    std_info_value=b"", file_names=(), data_attr=None, attribute_list_value=None,
    **assemble_kwargs,
):
    """Build one on-disk-shaped MFT record in the common
    $ATTRIBUTE_LIST/$STANDARD_INFORMATION/$FILE_NAME.../$DATA order.

    `file_names` is a list of (value_bytes, attribute_id) pairs. `data_attr`
    and `attribute_list_value`'s raw $DATA/$ATTRIBUTE_LIST attribute bytes
    are built by the caller via the helpers above (so the attribute_id and
    resident/non-resident shape are explicit and unambiguous). For a test
    that needs a non-standard attribute order, call _assemble_record directly.
    """
    attrs = bytearray()
    if attribute_list_value is not None:
        attrs += _build_resident_attr(mft_parser._ATTR_ATTRIBUTE_LIST, attribute_list_value, 0)
    if std_info_value is not None:
        attrs += _build_resident_attr(mft_parser._ATTR_STANDARD_INFORMATION, std_info_value, 0)
    for value, attribute_id in file_names:
        attrs += _build_resident_attr(mft_parser._ATTR_FILE_NAME, value, attribute_id)
    if data_attr is not None:
        attrs += data_attr
    return _assemble_record(record_number, attrs, **assemble_kwargs)


def _single_record_source(record_number, record_bytes):
    return FakeRecordSource({record_number: record_bytes})


def test_minimal_resident_record_parses():
    record = build_record(
        5,
        std_info_value=_std_info_value(file_attributes=0x20),
        file_names=[(_file_name_value(100, "hello.txt"), 1)],
        data_attr=_build_resident_attr(mft_parser._ATTR_DATA, b"hi", 2),
    )
    parsed = parse_base_record(5, _single_record_source(5, record))

    assert parsed is not None
    assert not parsed.is_directory
    assert parsed.logical_size == 2
    assert parsed.alloc_size == 2
    assert len(parsed.names) == 1
    assert parsed.names[0].parent_frn == 100
    assert parsed.names[0].name == "hello.txt"
    assert parsed.frn == _pack_frn(1, 5)


def test_directory_flag_and_empty_data():
    record = build_record(
        7, is_directory=True,
        std_info_value=_std_info_value(),
        file_names=[(_file_name_value(1, "SubDir"), 1)],
    )
    parsed = parse_base_record(7, _single_record_source(7, record))

    assert parsed is not None
    assert parsed.is_directory
    assert parsed.logical_size == 0
    assert parsed.alloc_size == 0


def test_nonresident_data_uses_header_fields_not_content():
    data_attr = _build_nonresident_attr(
        mft_parser._ATTR_DATA, allocated_size=65536, real_size=50000,
        initialized_size=50000, attribute_id=2,
    )
    record = build_record(
        9,
        std_info_value=_std_info_value(),
        file_names=[(_file_name_value(1, "big.bin"), 1)],
        data_attr=data_attr,
    )
    parsed = parse_base_record(9, _single_record_source(9, record))

    assert parsed is not None
    assert parsed.logical_size == 50000
    assert parsed.alloc_size == 65536


def test_compressed_nonresident_attribute_does_not_misalign_the_next_one():
    # The compressed $DATA's extra CompressedSize field only exists between
    # this attribute's header and its data runs -- if the walker ever
    # hardcoded the non-resident header size instead of trusting each
    # attribute's own `length` field, whatever comes right after it would
    # be read from the wrong offset and either fail to parse or come back
    # with garbage. Deliberately non-standard order: the compressed
    # attribute comes FIRST, so std_info/$FILE_NAME parsing right after it
    # is the thing actually being verified here.
    compressed_data = _build_nonresident_attr(
        mft_parser._ATTR_DATA, allocated_size=4096, real_size=9000,
        initialized_size=9000, attribute_id=2, compression_unit=4,
    )
    attrs = (
        compressed_data
        + _build_resident_attr(mft_parser._ATTR_STANDARD_INFORMATION, _std_info_value(file_attributes=0x21), 0)
        + _build_resident_attr(mft_parser._ATTR_FILE_NAME, _file_name_value(1, "compressed.bin"), 1)
    )
    record = _assemble_record(11, attrs)
    parsed = parse_base_record(11, _single_record_source(11, record))

    assert parsed is not None
    assert parsed.names[0].name == "compressed.bin"
    assert parsed.file_attributes == 0x21
    assert parsed.logical_size == 9000
    assert parsed.alloc_size == 4096


def test_same_parent_win32_and_dos_names_collapse_to_one():
    record = build_record(
        13,
        std_info_value=_std_info_value(),
        file_names=[
            (_file_name_value(50, "LONGFILENAME.TXT", _NAMESPACE_WIN32), 1),
            (_file_name_value(50, "LONGFI~1.TXT", _NAMESPACE_DOS), 2),
        ],
    )
    parsed = parse_base_record(13, _single_record_source(13, record))

    assert parsed is not None
    assert len(parsed.names) == 1
    assert parsed.names[0].name == "LONGFILENAME.TXT"
    assert parsed.names[0].namespace == _NAMESPACE_WIN32


def test_different_parent_names_are_both_kept_as_hard_links():
    record = build_record(
        17,
        std_info_value=_std_info_value(),
        file_names=[
            (_file_name_value(10, "a_link.txt"), 1),
            (_file_name_value(20, "b_link.txt"), 2),
        ],
    )
    parsed = parse_base_record(17, _single_record_source(17, record))

    assert parsed is not None
    assert len(parsed.names) == 2
    parents = {n.parent_frn for n in parsed.names}
    assert parents == {10, 20}


def test_baad_signature_is_skipped():
    record = build_record(
        19, signature=b"BAAD",
        std_info_value=_std_info_value(),
        file_names=[(_file_name_value(1, "x"), 1)],
    )
    assert parse_base_record(19, _single_record_source(19, record)) is None


def test_unused_record_is_skipped():
    record = build_record(
        21, in_use=False,
        std_info_value=_std_info_value(),
        file_names=[(_file_name_value(1, "x"), 1)],
    )
    assert parse_base_record(21, _single_record_source(21, record)) is None


def test_extension_record_is_never_its_own_tree_node():
    record = build_record(
        23, base_frn=_pack_frn(1, 5),  # claims to belong to base record 5
        std_info_value=_std_info_value(),
        file_names=[(_file_name_value(1, "x"), 1)],
    )
    assert parse_base_record(23, _single_record_source(23, record)) is None


def test_corrupt_usa_check_is_skipped():
    record = build_record(
        29, corrupt_usa=True,
        std_info_value=_std_info_value(),
        file_names=[(_file_name_value(1, "x"), 1)],
    )
    assert parse_base_record(29, _single_record_source(29, record)) is None


def test_fixups_use_the_record_sources_real_sector_size_not_a_hardcoded_512():
    """A native 4Kn volume (BytesPerSector=4096, not the near-universal 512)
    stamps its Update Sequence Array fixups at 4096-byte sector boundaries,
    not 512-byte ones. Applying fixups at the wrong offset either corrupts
    real record bytes or (as tested here, the more common outcome) makes
    the per-sector USN check fail and the whole record gets silently
    rejected as unparseable -- files vanishing from a 4Kn scan with no
    error surfaced anywhere.

    Built by hand here, independent of this file's SECTOR_SIZE=512-based
    build_record()/_assemble_record() helpers, since a 4096-byte sector on
    a 4096-byte record needs its own USA layout (2 entries: one USN plus
    one original-bytes slot, vs. those helpers' fixed 3-entry/512 shape).
    """
    record_size = 4096
    true_sector_size = 4096
    usa_offset = 48
    usa_size = record_size // true_sector_size + 1  # 2

    std_info = _std_info_value(file_attributes=0x20)
    file_name = _file_name_value(1, "hello.txt")
    attrs = bytearray()
    attrs += _build_resident_attr(mft_parser._ATTR_STANDARD_INFORMATION, std_info, 0)
    attrs += _build_resident_attr(mft_parser._ATTR_FILE_NAME, file_name, 1)
    attrs += struct.pack("<I", mft_parser._ATTR_END_MARKER)

    first_attr_offset = ((usa_offset + usa_size * 2) + 7) // 8 * 8
    bytes_in_use = first_attr_offset + len(attrs)
    header = struct.pack(
        "<4sHHQHHHHIIQHHI",
        b"FILE", usa_offset, usa_size, 0, 1, 1, first_attr_offset,
        mft_parser._RECORD_FLAG_IN_USE, bytes_in_use, record_size, 0, 0, 0, 29,
    )
    buf = bytearray(record_size)
    buf[0:len(header)] = header
    buf[first_attr_offset:first_attr_offset + len(attrs)] = attrs

    # Stamp fixups at the TRUE 4096-byte sector boundary, as real NTFS
    # would on a native 4Kn volume.
    usn = b"\x07\x00"
    record = bytearray(buf)
    record[usa_offset:usa_offset + 2] = usn
    for i in range(1, usa_size):
        sector_end = i * true_sector_size - 2
        original = bytes(record[sector_end:sector_end + 2])
        record[usa_offset + 2 * i:usa_offset + 2 * i + 2] = original
        record[sector_end:sector_end + 2] = usn
    record = bytes(record)

    class _PlainSource:
        """No bytes_per_sector attribute -- matches every fake elsewhere
        in this file, and the historical (bugged) hardcoded-512 behavior."""
        def __init__(self, rec):
            self._rec = rec

        def record_at(self, record_number):
            return self._rec

    class _FourKSource(_PlainSource):
        bytes_per_sector = 4096

    # Old behavior: sector_size silently defaults to 512, which is wrong
    # for this record -- the USN check at the (wrong) 510 offset doesn't
    # match, so the record is rejected outright.
    assert parse_base_record(29, _PlainSource(record)) is None

    # Fixed behavior: the record source exposes the volume's real sector
    # size, fixups are applied at the correct 4094 offset, and the record
    # parses normally.
    parsed = parse_base_record(29, _FourKSource(record))
    assert parsed is not None
    assert parsed.names[0].name == "hello.txt"


def test_missing_standard_information_is_skipped():
    record = build_record(
        31, std_info_value=None,
        file_names=[(_file_name_value(1, "x"), 1)],
    )
    assert parse_base_record(31, _single_record_source(31, record)) is None


def test_no_file_name_at_all_is_skipped():
    record = build_record(33, std_info_value=_std_info_value(), file_names=[])
    assert parse_base_record(33, _single_record_source(33, record)) is None


def test_attribute_list_merges_a_file_name_from_an_extension_record():
    base_record_number = 40
    ext_record_number = 41
    ext_sequence = 3
    ext_frn = _pack_frn(ext_sequence, ext_record_number)

    base_record = build_record(
        base_record_number,
        std_info_value=_std_info_value(),
        file_names=[(_file_name_value(100, "primary.txt"), 1)],
        data_attr=_build_resident_attr(mft_parser._ATTR_DATA, b"abc", 3),
        attribute_list_value=_attribute_list_value([
            (mft_parser._ATTR_FILE_NAME, 2, ext_frn),
        ]),
    )
    ext_record = build_record(
        ext_record_number,
        base_frn=_pack_frn(1, base_record_number),
        sequence_number=ext_sequence,
        std_info_value=None,
        file_names=[(_file_name_value(200, "extra_link.txt"), 2)],
    )

    source = FakeRecordSource({base_record_number: base_record, ext_record_number: ext_record})
    parsed = parse_base_record(base_record_number, source)

    assert parsed is not None
    names = {n.parent_frn: n.name for n in parsed.names}
    assert names == {100: "primary.txt", 200: "extra_link.txt"}
    assert parsed.logical_size == 3  # the base record's own $DATA, unaffected


def test_attribute_list_entry_for_a_missing_extension_record_is_ignored():
    base_record_number = 50
    missing_ext_frn = _pack_frn(1, 999)  # no record 999 exists in the source

    base_record = build_record(
        base_record_number,
        std_info_value=_std_info_value(),
        file_names=[(_file_name_value(100, "primary.txt"), 1)],
        attribute_list_value=_attribute_list_value([
            (mft_parser._ATTR_FILE_NAME, 2, missing_ext_frn),
        ]),
    )
    source = FakeRecordSource({base_record_number: base_record})
    parsed = parse_base_record(base_record_number, source)

    assert parsed is not None
    assert len(parsed.names) == 1
    assert parsed.names[0].name == "primary.txt"


def test_attribute_list_entry_with_stale_sequence_number_is_ignored():
    """The extension record's slot was freed and reused for a different
    file since the $ATTRIBUTE_LIST entry was written -- the entry's FRN
    still names the right record number, but its expected sequence number
    (3) no longer matches what's actually stored there now (4), so it must
    be treated like a missing extension record, not trusted and merged in.
    """
    base_record_number = 55
    ext_record_number = 56
    stale_ext_frn = _pack_frn(3, ext_record_number)  # entry expects sequence 3

    base_record = build_record(
        base_record_number,
        std_info_value=_std_info_value(),
        file_names=[(_file_name_value(100, "primary.txt"), 1)],
        attribute_list_value=_attribute_list_value([
            (mft_parser._ATTR_FILE_NAME, 2, stale_ext_frn),
        ]),
    )
    # The slot was reused: its ACTUAL on-disk sequence number is 4, not
    # the 3 the base record's $ATTRIBUTE_LIST entry still expects.
    reused_record = build_record(
        ext_record_number,
        base_frn=_pack_frn(1, base_record_number),
        sequence_number=4,
        std_info_value=None,
        file_names=[(_file_name_value(999, "different_file.txt"), 2)],
    )

    source = FakeRecordSource(
        {base_record_number: base_record, ext_record_number: reused_record}
    )
    parsed = parse_base_record(base_record_number, source)

    assert parsed is not None
    assert len(parsed.names) == 1
    assert parsed.names[0].name == "primary.txt"  # the reused slot's name must not be merged in


def test_attribute_list_entry_for_a_freed_not_reused_extension_record_is_ignored():
    """The extension record's slot was freed and never reused -- IN_USE is
    clear. Even with a sequence number that happens to still match, a
    not-in-use record must never be trusted."""
    base_record_number = 57
    ext_record_number = 58
    ext_sequence = 3
    ext_frn = _pack_frn(ext_sequence, ext_record_number)

    base_record = build_record(
        base_record_number,
        std_info_value=_std_info_value(),
        file_names=[(_file_name_value(100, "primary.txt"), 1)],
        attribute_list_value=_attribute_list_value([
            (mft_parser._ATTR_FILE_NAME, 2, ext_frn),
        ]),
    )
    freed_record = build_record(
        ext_record_number,
        in_use=False,
        base_frn=_pack_frn(1, base_record_number),
        sequence_number=ext_sequence,
        std_info_value=None,
        file_names=[(_file_name_value(999, "different_file.txt"), 2)],
    )

    source = FakeRecordSource(
        {base_record_number: base_record, ext_record_number: freed_record}
    )
    parsed = parse_base_record(base_record_number, source)

    assert parsed is not None
    assert len(parsed.names) == 1
    assert parsed.names[0].name == "primary.txt"


class FakeRecordSourceWithClusters(FakeRecordSource):
    """Extends FakeRecordSource with read_clusters, for the non-resident
    $ATTRIBUTE_LIST tests below -- a fixed-size in-memory 'volume' addressed
    by LCN, independent of the MFT-records-by-number dict above (the same
    split real RecordSource makes: records vs. raw volume clusters)."""

    def __init__(self, records_by_number, volume_bytes, bytes_per_cluster):
        super().__init__(records_by_number)
        self._volume_bytes = volume_bytes
        self._bytes_per_cluster = bytes_per_cluster

    def read_clusters(self, lcn, cluster_count):
        start = lcn * self._bytes_per_cluster
        end = start + cluster_count * self._bytes_per_cluster
        return self._volume_bytes[start:end]


def _single_run_bytes(length_clusters, lcn):
    """One data run: length field 1 byte, LCN-offset field 2 bytes (signed,
    relative to 0 since it's the first/only run) -- header nibble 0x21."""
    return b"\x21" + bytes([length_clusters]) + lcn.to_bytes(2, "little", signed=True) + b"\x00"


def test_nonresident_attribute_list_merges_a_file_name_from_an_extension_record():
    # The real-world bug this covers: a heavily hard-linked file (common in
    # WinSxS/GAC-style system areas) has enough $FILE_NAME entries that its
    # $ATTRIBUTE_LIST itself has to go non-resident -- mft_parser used to
    # give up on those entirely (_parse_attribute_list returned [] for any
    # non-resident list), silently losing every extension-record $FILE_NAME
    # (and, when $DATA itself was one of the spilled attributes, the file's
    # real size too).
    base_record_number = 80
    ext_record_number = 81
    ext_sequence = 4
    ext_frn = _pack_frn(ext_sequence, ext_record_number)
    bytes_per_cluster = 64
    lcn = 3

    attr_list_value = _attribute_list_value([
        (mft_parser._ATTR_FILE_NAME, 2, ext_frn),
    ])
    attribute_list_attr = _build_nonresident_attr(
        mft_parser._ATTR_ATTRIBUTE_LIST, allocated_size=bytes_per_cluster,
        real_size=len(attr_list_value), initialized_size=len(attr_list_value),
        attribute_id=0, data_runs=_single_run_bytes(1, lcn),
    )
    attrs = bytearray()
    attrs += attribute_list_attr
    attrs += _build_resident_attr(mft_parser._ATTR_STANDARD_INFORMATION, _std_info_value(), 0)
    attrs += _build_resident_attr(mft_parser._ATTR_FILE_NAME, _file_name_value(100, "primary.txt"), 1)
    attrs += _build_resident_attr(mft_parser._ATTR_DATA, b"abc", 3)
    base_record = _assemble_record(base_record_number, attrs)

    ext_record = build_record(
        ext_record_number,
        base_frn=_pack_frn(1, base_record_number),
        sequence_number=ext_sequence,
        std_info_value=None,
        file_names=[(_file_name_value(200, "extra_link.txt"), 2)],
    )

    volume = bytearray(lcn * bytes_per_cluster + bytes_per_cluster)
    start = lcn * bytes_per_cluster
    volume[start:start + len(attr_list_value)] = attr_list_value

    source = FakeRecordSourceWithClusters(
        {base_record_number: base_record, ext_record_number: ext_record},
        bytes(volume), bytes_per_cluster,
    )
    parsed = parse_base_record(base_record_number, source)

    assert parsed is not None
    names = {n.parent_frn: n.name for n in parsed.names}
    assert names == {100: "primary.txt", 200: "extra_link.txt"}
    assert parsed.logical_size == 3  # the base record's own $DATA, unaffected


def test_nonresident_attribute_list_without_cluster_reader_is_ignored():
    # A record_source that can't do raw cluster reads (e.g. a lightweight
    # record_at-only fake, or -- in principle -- a future record source
    # without volume access) must degrade gracefully: no extra names
    # resolved, but the base record's own attributes still parse fine.
    base_record_number = 90
    attribute_list_attr = _build_nonresident_attr(
        mft_parser._ATTR_ATTRIBUTE_LIST, allocated_size=64,
        real_size=26, initialized_size=26, attribute_id=0,
        data_runs=_single_run_bytes(1, 3),
    )
    attrs = bytearray()
    attrs += attribute_list_attr
    attrs += _build_resident_attr(mft_parser._ATTR_STANDARD_INFORMATION, _std_info_value(), 0)
    attrs += _build_resident_attr(mft_parser._ATTR_FILE_NAME, _file_name_value(100, "primary.txt"), 1)
    base_record = _assemble_record(base_record_number, attrs)

    source = _single_record_source(base_record_number, base_record)
    parsed = parse_base_record(base_record_number, source)

    assert parsed is not None
    assert len(parsed.names) == 1
    assert parsed.names[0].name == "primary.txt"


def test_mtime_atime_and_reparse_and_cloud_placeholder_flags():
    filetime_2020_01_01 = 132223104000000000  # arbitrary real-looking FILETIME
    reparse_and_offline = 0x400 | 0x00001000  # FILE_ATTRIBUTE_REPARSE_POINT | OFFLINE
    record = build_record(
        60,
        std_info_value=_std_info_value(
            mtime_filetime=filetime_2020_01_01,
            atime_filetime=filetime_2020_01_01,
            file_attributes=reparse_and_offline,
        ),
        file_names=[(_file_name_value(1, "placeholder.bin"), 1)],
    )
    parsed = parse_base_record(60, _single_record_source(60, record))

    assert parsed is not None
    assert parsed.is_reparse_point
    assert parsed.is_cloud_placeholder
    assert parsed.mtime > 0
    assert parsed.atime > 0


def test_recall_on_open_without_reparse_point_is_not_a_cloud_placeholder():
    # The real bug this covers: CompactOS/WIMBoot-compressed system files
    # set FILE_ATTRIBUTE_RECALL_ON_OPEN on-disk ("decompress from the WIM
    # on open") without ever being a reparse point. Confirmed via a raw
    # MFT read against a real machine's C:\Windows\Boot: thousands of
    # ordinary boot DLLs parsed this way, and Compatible's live os.stat()
    # never reported RECALL_ON_OPEN for the same files at all -- they are
    # not cloud placeholders. A genuine OneDrive-style placeholder is
    # always also a reparse point (IO_REPARSE_TAG_CLOUD).
    recall_on_open_only = 0x20 | 0x00040000  # FILE_ATTRIBUTE_ARCHIVE | RECALL_ON_OPEN
    record = build_record(
        61,
        std_info_value=_std_info_value(file_attributes=recall_on_open_only),
        file_names=[(_file_name_value(1, "kd_02_10df.dll"), 1)],
    )
    parsed = parse_base_record(61, _single_record_source(61, record))

    assert parsed is not None
    assert not parsed.is_reparse_point
    assert not parsed.is_cloud_placeholder


# -- decode_data_runs -------------------------------------------------------- #
# This is the fix for a real bug the Turbo Scan validation gate caught on a
# real (fragmented) $MFT: mft_volume.py previously assumed the $MFT was one
# contiguous span, silently dropping most of a real volume's records. These
# runs are built by hand against the documented NTFS data-run byte format
# (header nibble = length-field size / LCN-offset-field size, then an
# unsigned length, then a signed little-endian LCN delta relative to the
# previous run), not derived from the decoder itself.

def test_decode_single_run():
    # header 0x21: length field 1 byte, LCN-offset field 2 bytes.
    runs = b"\x21" + bytes([10]) + (1000).to_bytes(2, "little", signed=True) + b"\x00"
    assert mft_parser.decode_data_runs(runs) == [(10, 1000)]


def test_decode_two_runs_with_relative_lcn_deltas():
    # Each run's LCN is relative to the previous one, not absolute.
    runs = (
        b"\x11" + bytes([5]) + (100).to_bytes(1, "little", signed=True)
        + b"\x11" + bytes([8]) + (50).to_bytes(1, "little", signed=True)
        + b"\x00"
    )
    assert mft_parser.decode_data_runs(runs) == [(5, 100), (8, 150)]


def test_decode_negative_lcn_delta_moves_backward():
    runs = (
        b"\x21" + bytes([5]) + (1000).to_bytes(2, "little", signed=True)
        + b"\x21" + bytes([3]) + (-200).to_bytes(2, "little", signed=True)
        + b"\x00"
    )
    assert mft_parser.decode_data_runs(runs) == [(5, 1000), (3, 800)]


def test_decode_sparse_run_is_omitted_but_does_not_shift_later_lcns():
    # header 0x01: length field 1 byte, LCN-offset field 0 bytes (sparse --
    # no physical allocation, no LCN delta present at all).
    runs = (
        b"\x01" + bytes([20])
        + b"\x21" + bytes([5]) + (300).to_bytes(2, "little", signed=True)
        + b"\x00"
    )
    assert mft_parser.decode_data_runs(runs) == [(5, 300)]


def test_decode_empty_or_immediately_terminated_runs():
    assert mft_parser.decode_data_runs(b"") == []
    assert mft_parser.decode_data_runs(b"\x00") == []


def test_decode_multi_byte_length_field():
    # header 0x13: length field 3 bytes (a run large enough to need it),
    # LCN-offset field 1 byte.
    runs = b"\x13" + (100000).to_bytes(3, "little", signed=False) + bytes([5]) + b"\x00"
    assert mft_parser.decode_data_runs(runs) == [(100000, 5)]


# -- get_nonresident_data_runs_bytes ----------------------------------------- #

def _record_with_data_attr(record_number, data_attr):
    return build_record(
        record_number,
        std_info_value=_std_info_value(),
        file_names=[(_file_name_value(1, "x"), 1)],
        data_attr=data_attr,
    )


def test_get_data_runs_round_trips_through_decode():
    expected_runs = [(10, 1000), (5, 1500)]
    runs_bytes = (
        b"\x21" + bytes([10]) + (1000).to_bytes(2, "little", signed=True)
        + b"\x21" + bytes([5]) + (500).to_bytes(2, "little", signed=True)
        + b"\x00"
    )
    data_attr = _build_nonresident_attr(
        mft_parser._ATTR_DATA, allocated_size=0, real_size=0, initialized_size=0,
        attribute_id=2, data_runs=runs_bytes,
    )
    record = _record_with_data_attr(70, data_attr)

    found = mft_parser.get_nonresident_data_runs_bytes(record)

    assert found is not None
    assert mft_parser.decode_data_runs(found) == expected_runs


def test_get_data_runs_returns_none_for_resident_data():
    data_attr = _build_resident_attr(mft_parser._ATTR_DATA, b"tiny", 2)
    record = _record_with_data_attr(71, data_attr)
    assert mft_parser.get_nonresident_data_runs_bytes(record) is None


def test_get_data_runs_returns_none_for_a_named_stream():
    data_attr = _build_nonresident_attr(
        mft_parser._ATTR_DATA, allocated_size=0, real_size=0, initialized_size=0,
        attribute_id=2, name_length=1,  # a named alternate data stream
    )
    record = _record_with_data_attr(72, data_attr)
    assert mft_parser.get_nonresident_data_runs_bytes(record) is None


def test_get_data_runs_returns_none_when_no_data_attribute_exists():
    record = build_record(
        73, std_info_value=_std_info_value(),
        file_names=[(_file_name_value(1, "x"), 1)],
    )
    assert mft_parser.get_nonresident_data_runs_bytes(record) is None


def test_get_data_runs_returns_none_for_a_corrupt_record():
    record = build_record(
        74, signature=b"BAAD",
        std_info_value=_std_info_value(),
        file_names=[(_file_name_value(1, "x"), 1)],
    )
    assert mft_parser.get_nonresident_data_runs_bytes(record) is None
