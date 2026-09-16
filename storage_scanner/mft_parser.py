"""Byte-level parsing of NTFS Master File Table (MFT) records.

Every function here is a pure transform of `bytes` -> structured data. There
is no filesystem or ctypes.windll access anywhere in this module, which is
deliberate: it means the entire correctness-critical parsing logic (fixups,
attribute walking, $FILE_NAME selection, $ATTRIBUTE_LIST merging) can be
exercised by tests/test_mft_parser.py with hand-built byte fixtures, no
admin rights or real NTFS volume required. A future raw-volume reader will
feed real record bytes in from disk (planned as storage_scanner/mft_volume.py)
and is intended to stay too thin itself to need its own tests -- all real
parsing logic belongs here instead.

Callers pass a "record source": any object with a `record_at(record_number)
-> bytes` method returning exactly one fixed-size MFT record's raw bytes.
This lets tests substitute an in-memory dict-backed fake for the real
sequential/random-access volume reader.
"""

import struct
from dataclasses import dataclass, field

from storage_scanner.scanner import is_cloud_placeholder_attrs
import stat as _stat

_FILE_ATTRIBUTE_REPARSE_POINT = _stat.FILE_ATTRIBUTE_REPARSE_POINT

# -- MULTI_SECTOR_HEADER + FILE_RECORD_SEGMENT_HEADER (48 bytes) ----------- #
_SIGNATURE_FILE = b"FILE"
_SIGNATURE_BAAD = b"BAAD"
_RECORD_HEADER_FORMAT = "<4sHHQHHHHIIQHHI"
_RECORD_HEADER_SIZE = struct.calcsize(_RECORD_HEADER_FORMAT)  # 48

_RECORD_FLAG_IN_USE = 0x0001
_RECORD_FLAG_IS_DIRECTORY = 0x0002

_SECTOR_SIZE = 512

# -- Attribute header: 16-byte common prefix, then a resident (+8 byte) or -- #
# -- non-resident (+48 byte, or +56 when compressed) variant --------------- #
_ATTR_COMMON_FORMAT = "<IIBBHHH"
_ATTR_COMMON_SIZE = struct.calcsize(_ATTR_COMMON_FORMAT)  # 16
_ATTR_RESIDENT_FORMAT = "<IHBB"
_ATTR_RESIDENT_SIZE = struct.calcsize(_ATTR_RESIDENT_FORMAT)  # 8
_ATTR_NONRESIDENT_FORMAT = "<QQHHIQQQ"
_ATTR_NONRESIDENT_SIZE = struct.calcsize(_ATTR_NONRESIDENT_FORMAT)  # 48

_ATTR_STANDARD_INFORMATION = 0x10
_ATTR_ATTRIBUTE_LIST = 0x20
_ATTR_FILE_NAME = 0x30
_ATTR_DATA = 0x80
_ATTR_END_MARKER = 0xFFFFFFFF

# -- $STANDARD_INFORMATION (resident): only the fixed, always-present ------ #
# -- creation/modified/changed/accessed FILETIMEs + FileAttributes matter -- #
_STANDARD_INFO_FORMAT = "<QQQQI"
_STANDARD_INFO_MIN_SIZE = struct.calcsize(_STANDARD_INFO_FORMAT)  # 36

# -- $FILE_NAME (resident) -------------------------------------------------- #
_FILE_NAME_FIXED_FORMAT = "<QQQQQQQII"
_FILE_NAME_FIXED_SIZE = struct.calcsize(_FILE_NAME_FIXED_FORMAT)  # 64

_FILENAME_NAMESPACE_POSIX = 0
_FILENAME_NAMESPACE_WIN32 = 1
_FILENAME_NAMESPACE_DOS = 2
_FILENAME_NAMESPACE_WIN32_AND_DOS = 3

# -- $ATTRIBUTE_LIST entry -------------------------------------------------- #
_ATTR_LIST_ENTRY_HEAD_FORMAT = "<IHBB"
_ATTR_LIST_ENTRY_HEAD_SIZE = struct.calcsize(_ATTR_LIST_ENTRY_HEAD_FORMAT)  # 8
_ATTR_LIST_ENTRY_TAIL_FORMAT = "<QQH"
_ATTR_LIST_ENTRY_TAIL_SIZE = struct.calcsize(_ATTR_LIST_ENTRY_TAIL_FORMAT)  # 18
_ATTR_LIST_ENTRY_MIN_SIZE = _ATTR_LIST_ENTRY_HEAD_SIZE + _ATTR_LIST_ENTRY_TAIL_SIZE  # 26

# A File Reference Number packs a 16-bit sequence number (incremented every
# time the slot is reused, so a stale reference can be detected) over a
# 48-bit MFT record number.
_FRN_RECORD_NUMBER_MASK = 0x0000FFFFFFFFFFFF

# FILETIME ticks (100ns units) between 1601-01-01 and 1970-01-01.
_FILETIME_UNIX_EPOCH_DIFF = 116444736000000000


def _filetime_to_unix(filetime):
    if not filetime:
        return 0.0
    return (filetime - _FILETIME_UNIX_EPOCH_DIFF) / 10_000_000


def _pack_frn(sequence_number, record_number):
    return ((sequence_number & 0xFFFF) << 48) | (record_number & _FRN_RECORD_NUMBER_MASK)


@dataclass
class FileNameAttr:
    """One resolved $FILE_NAME: one real hard link (a Win32/DOS-8.3 alias
    pair sharing the same parent has already been collapsed to one entry
    by _dedup_file_names -- see parse_base_record)."""

    parent_frn: int
    name: str
    namespace: int


@dataclass
class ParsedRecord:
    """Everything scanner-facing code needs from one base MFT record.

    `logical_size`/`alloc_size` are this record's own file data only (0 for
    directories) -- mirroring storage_scanner.models.Node before rollup, not
    after. `names` always has at least one entry (a record with none isn't
    linked into any directory and is treated as unparseable -- see
    parse_base_record).
    """

    frn: int
    is_directory: bool
    file_attributes: int
    is_reparse_point: bool
    is_cloud_placeholder: bool
    mtime: float
    atime: float
    logical_size: int
    alloc_size: int
    names: list = field(default_factory=list)


@dataclass
class _RawAttribute:
    attr_type: int
    attribute_id: int
    non_resident: bool
    is_named: bool = False  # True for a named stream (e.g. an alternate
                             # data stream) rather than the primary attribute
    value: bytes = b""
    allocated_size: int = 0
    real_size: int = 0
    initialized_size: int = 0
    data_runs: bytes = b""  # non-resident only; still encoded, never
                             # decoded here -- see decode_data_runs


@dataclass
class _AttributeListEntry:
    attr_type: int
    attribute_id: int
    base_frn: int  # FRN of the record segment actually holding this instance


def _read_header(raw):
    """Unpack the 48-byte record header, or None if too short/wrong shape."""
    if len(raw) < _RECORD_HEADER_SIZE:
        return None
    fields = struct.unpack_from(_RECORD_HEADER_FORMAT, raw, 0)
    (
        signature, usa_offset, usa_size, _lsn, sequence_number,
        hard_link_count, first_attribute_offset, flags, bytes_in_use,
        bytes_allocated, base_file_record_segment, _next_attribute_id,
        _reserved, _mft_record_number,
    ) = fields
    if signature != _SIGNATURE_FILE:
        return None  # e.g. b"BAAD" (torn write) or an all-zero unused slot
    return {
        "usa_offset": usa_offset,
        "usa_size": usa_size,
        "sequence_number": sequence_number,
        "hard_link_count": hard_link_count,
        "first_attribute_offset": first_attribute_offset,
        "flags": flags,
        "bytes_in_use": bytes_in_use,
        "bytes_allocated": bytes_allocated,
        "base_file_record_segment": base_file_record_segment,
    }


def _apply_fixups(raw, header):
    """Reverse the Update Sequence Array substitution, or None if the
    per-sector check fails (a torn/corrupt write).

    NTFS stamps the last 2 bytes of every 512-byte sector in a multi-sector
    record with a shared "USN" value and relocates the real bytes that used
    to live there into the Update Sequence Array, so a partially-written
    record can be detected (the stamped USN would then be missing from one
    sector). Every other field in the record is untrustworthy until this
    runs -- get it wrong and two real bytes every 512 bytes are silently
    corrupted, which can land inside a size or name field on a large record.
    """
    usa_offset = header["usa_offset"]
    usa_count = header["usa_size"]
    if usa_count == 0:
        return None
    usn_end = usa_offset + 2
    if usn_end > len(raw):
        return None
    usn = bytes(raw[usa_offset:usn_end])
    fixed = bytearray(raw)
    for i in range(1, usa_count):
        check_off = i * _SECTOR_SIZE - 2
        if check_off + 2 > len(raw):
            break  # record shorter than this sector -- nothing more to fix
        if bytes(raw[check_off:check_off + 2]) != usn:
            return None
        original_off = usa_offset + 2 * i
        if original_off + 2 > len(raw):
            return None
        fixed[check_off:check_off + 2] = raw[original_off:original_off + 2]
    return bytes(fixed)


def _read_header_and_fixup(raw):
    header = _read_header(raw)
    if header is None:
        return None, None
    fixed = _apply_fixups(raw, header)
    if fixed is None:
        return None, None
    return header, fixed


def _iter_attributes(fixed, header):
    """Walk one record's attribute area, yielding a _RawAttribute per
    attribute up to the 0xFFFFFFFF end marker or `bytes_in_use`.

    Each attribute's own `length` field (not a hardcoded header size) is
    the stride to the next one -- this is what keeps the walk correct
    regardless of a non-resident attribute's optional trailing
    CompressedSize field (present only when CompressionUnit != 0), since
    that field is never read as part of computing the next offset.
    """
    offset = header["first_attribute_offset"]
    end = min(header["bytes_in_use"], len(fixed))
    while offset + 4 <= end:
        attr_type = struct.unpack_from("<I", fixed, offset)[0]
        if attr_type == _ATTR_END_MARKER:
            break
        if offset + _ATTR_COMMON_SIZE > end:
            break
        (_type, length, non_resident, name_length, _name_offset,
         _flags, attribute_id) = struct.unpack_from(_ATTR_COMMON_FORMAT, fixed, offset)
        if length == 0 or offset + length > end:
            break  # corrupt -- stop walking defensively rather than loop/overrun
        is_named = name_length != 0

        if non_resident:
            nrh_off = offset + _ATTR_COMMON_SIZE
            if nrh_off + _ATTR_NONRESIDENT_SIZE <= end:
                (_starting_vcn, _last_vcn, data_runs_offset,
                 _compression_unit, _reserved, allocated_size, real_size,
                 initialized_size) = struct.unpack_from(
                    _ATTR_NONRESIDENT_FORMAT, fixed, nrh_off
                )
                # The run-list bytes are never decoded here (see module
                # docstring) -- only sliced out, using the attribute's own
                # documented data_runs_offset field (not assumed from the
                # non-resident header's fixed size) so an unusual/padded
                # layout can't silently mis-slice them.
                data_runs = b""
                if data_runs_offset:
                    runs_start = offset + data_runs_offset
                    runs_end = offset + length
                    if offset < runs_start <= runs_end <= end:
                        data_runs = bytes(fixed[runs_start:runs_end])
                yield _RawAttribute(
                    attr_type=attr_type, attribute_id=attribute_id,
                    non_resident=True, is_named=is_named,
                    allocated_size=allocated_size,
                    real_size=real_size, initialized_size=initialized_size,
                    data_runs=data_runs,
                )
        else:
            rh_off = offset + _ATTR_COMMON_SIZE
            if rh_off + _ATTR_RESIDENT_SIZE <= end:
                value_length, value_offset, _resident_flags, _reserved = (
                    struct.unpack_from(_ATTR_RESIDENT_FORMAT, fixed, rh_off)
                )
                value_start = offset + value_offset
                value = bytes(fixed[value_start:value_start + value_length])
                yield _RawAttribute(
                    attr_type=attr_type, attribute_id=attribute_id,
                    non_resident=False, is_named=is_named, value=value,
                )

        offset += length


def _parse_standard_information(value):
    if len(value) < _STANDARD_INFO_MIN_SIZE:
        return None
    _creation, modified, _changed, accessed, file_attributes = (
        struct.unpack_from(_STANDARD_INFO_FORMAT, value, 0)
    )
    return {
        "mtime": _filetime_to_unix(modified),
        "atime": _filetime_to_unix(accessed),
        "file_attributes": file_attributes,
    }


def _parse_file_name(value):
    if len(value) < _FILE_NAME_FIXED_SIZE + 2:
        return None
    (parent_frn, _ctime, _mtime, _chtime, _atime, _alloc, _real,
     _attrs, _ea) = struct.unpack_from(_FILE_NAME_FIXED_FORMAT, value, 0)
    name_length = value[_FILE_NAME_FIXED_SIZE]
    namespace = value[_FILE_NAME_FIXED_SIZE + 1]
    name_start = _FILE_NAME_FIXED_SIZE + 2
    name_end = name_start + name_length * 2
    if name_end > len(value):
        return None
    name = value[name_start:name_end].decode("utf-16-le", errors="replace")
    return FileNameAttr(parent_frn=parent_frn, name=name, namespace=namespace)


def _dedup_file_names(names):
    """Collapse each (parent, Win32-name/DOS-8.3-alias) pair down to one
    entry per real hard link -- keyed on parent_frn, preferring the Win32
    (or Win32+DOS) name over a redundant pure-DOS alias for that same
    parent. Different parent_frn values are always genuinely different
    hard links and are never collapsed together."""
    by_parent = {}
    for name in names:
        existing = by_parent.get(name.parent_frn)
        if existing is None or existing.namespace == _FILENAME_NAMESPACE_DOS:
            by_parent[name.parent_frn] = name
    return list(by_parent.values())


def _read_nonresident_attribute_list(raw_attr, record_source):
    """Reassemble a non-resident $ATTRIBUTE_LIST's real bytes by reading its
    own data runs' clusters straight off the volume, or None if that isn't
    possible (a record_source that can't do raw cluster reads -- e.g. the
    lightweight record_at-only fakes some tests use -- an empty/undecodable
    run list, or a failed read), all treated as best-effort, never an error.

    Assumes no run is sparse (offset_size == 0), which decode_data_runs
    silently drops -- true of every $ATTRIBUTE_LIST seen in practice, since
    sparse is a $DATA-stream-only NTFS feature, but would misalign/truncate
    the reassembled bytes if it ever weren't.
    """
    read_clusters = getattr(record_source, "read_clusters", None)
    if read_clusters is None or not raw_attr.data_runs:
        return None
    try:
        chunks = [
            read_clusters(lcn, length_clusters)
            for length_clusters, lcn in decode_data_runs(raw_attr.data_runs)
        ]
    except (LookupError, OSError):
        return None
    if not chunks:
        return None
    return b"".join(chunks)[:raw_attr.real_size]


def _parse_attribute_list(raw_attr, record_source):
    """Resolve a $ATTRIBUTE_LIST's entries, resident or non-resident.

    A resident list is parsed directly out of the record. A non-resident
    one (needed once a heavily hard-linked file/directory -- common in
    WinSxS/GAC-style system areas -- has too many $FILE_NAME/other entries
    to fit inline) is reassembled via _read_nonresident_attribute_list;
    if that fails, this returns [] ("no extra attributes resolvable"),
    same as always, never an error.
    """
    if raw_attr.non_resident:
        value = _read_nonresident_attribute_list(raw_attr, record_source)
        if value is None:
            return []
    else:
        value = raw_attr.value
    entries = []
    offset = 0
    while offset + _ATTR_LIST_ENTRY_MIN_SIZE <= len(value):
        attr_type, entry_length, _name_length, _name_offset = (
            struct.unpack_from(_ATTR_LIST_ENTRY_HEAD_FORMAT, value, offset)
        )
        if entry_length == 0:
            break
        _starting_vcn, base_frn, attribute_id = struct.unpack_from(
            _ATTR_LIST_ENTRY_TAIL_FORMAT, value, offset + _ATTR_LIST_ENTRY_HEAD_SIZE
        )
        entries.append(_AttributeListEntry(
            attr_type=attr_type, attribute_id=attribute_id, base_frn=base_frn,
        ))
        offset += entry_length
    return entries


def parse_base_record(record_number, record_source):
    """Parse the base MFT record `record_number` into a ParsedRecord.

    Merges in any $FILE_NAME/$DATA/etc. attributes that spilled into
    separate extension records via $ATTRIBUTE_LIST (see _parse_attribute_list),
    so the caller never needs to know a record was split.

    Returns None for anything that isn't a usable base record: a bad/torn
    signature, a free (not-in-use) slot, an extension record (identified by
    a non-zero base_file_record_segment -- it's only ever consumed via
    another record's $ATTRIBUTE_LIST, never a tree node on its own), a
    record failing its fixup check, or a record missing $STANDARD_INFORMATION
    or every $FILE_NAME (both of which every real, linked-in file/directory
    has) -- all of these are "unreadable", never a raised exception, so a
    caller walking the whole MFT can just skip whatever comes back as None.
    """
    raw = record_source.record_at(record_number)
    header, fixed = _read_header_and_fixup(raw)
    if header is None:
        return None
    if not (header["flags"] & _RECORD_FLAG_IN_USE):
        return None
    if header["base_file_record_segment"] != 0:
        return None  # extension record; not a tree node on its own

    attrs = list(_iter_attributes(fixed, header))

    for attr in list(attrs):
        if attr.attr_type != _ATTR_ATTRIBUTE_LIST:
            continue
        for entry in _parse_attribute_list(attr, record_source):
            target_record_number = entry.base_frn & _FRN_RECORD_NUMBER_MASK
            if target_record_number == record_number:
                continue  # already covered by this record's own attributes
            try:
                ext_raw = record_source.record_at(target_record_number)
            except (LookupError, OSError):
                continue  # missing extension record -- best-effort, skip it
            ext_header, ext_fixed = _read_header_and_fixup(ext_raw)
            if ext_header is None:
                continue
            for ext_attr in _iter_attributes(ext_fixed, ext_header):
                if (ext_attr.attr_type == entry.attr_type
                        and ext_attr.attribute_id == entry.attribute_id):
                    attrs.append(ext_attr)

    std_info = None
    names = []
    data_attr = None
    for attr in attrs:
        if attr.attr_type == _ATTR_STANDARD_INFORMATION and std_info is None:
            std_info = _parse_standard_information(attr.value)
        elif attr.attr_type == _ATTR_FILE_NAME:
            name = _parse_file_name(attr.value)
            if name is not None:
                names.append(name)
        elif attr.attr_type == _ATTR_DATA and not attr.is_named and data_attr is None:
            # A file can have named $DATA attributes too (alternate data
            # streams), but v1 only tracks the primary unnamed stream --
            # ADS support is explicitly out of scope for now.
            data_attr = attr

    if std_info is None:
        return None

    names = _dedup_file_names(names)
    if not names:
        return None

    if data_attr is None:
        logical_size = 0
        alloc_size = 0
    elif data_attr.non_resident:
        logical_size = data_attr.real_size
        alloc_size = data_attr.allocated_size
    else:
        # Resident data lives inside the MFT record itself -- there's no
        # separate on-disk cluster allocation to report, so allocated size
        # is just the logical size (matching how tiny/resident files show
        # up through the fallback engine's GetCompressedFileSizeW path).
        logical_size = len(data_attr.value)
        alloc_size = logical_size

    file_attributes = std_info["file_attributes"]
    return ParsedRecord(
        frn=_pack_frn(header["sequence_number"], record_number),
        is_directory=bool(header["flags"] & _RECORD_FLAG_IS_DIRECTORY),
        file_attributes=file_attributes,
        is_reparse_point=bool(file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT),
        is_cloud_placeholder=is_cloud_placeholder_attrs(file_attributes),
        mtime=std_info["mtime"],
        atime=std_info["atime"],
        logical_size=logical_size,
        alloc_size=alloc_size,
        names=names,
    )


def get_nonresident_data_runs_bytes(record_bytes, attr_type=_ATTR_DATA):
    """Locate one raw MFT record's first non-resident, unnamed attribute
    of `attr_type` and return its still-encoded data-run bytes, or None if
    there's no such attribute (missing, resident, or named).

    This is the one place run-list bytes are exposed at all -- everything
    else in this module (parse_base_record included) only ever reads a
    non-resident attribute's AllocatedSize/RealSize/InitializedSize header
    fields, never its runs, because sizing never needs them. The sole
    consumer of this function is storage_scanner.mft_volume, which needs
    the $MFT's own record #0 $DATA runs to find every physical extent of a
    (possibly fragmented) $MFT -- decoding those bytes into (length, LCN)
    pairs happens there via decode_data_runs, not here.
    """
    header, fixed = _read_header_and_fixup(record_bytes)
    if header is None:
        return None
    for attr in _iter_attributes(fixed, header):
        if attr.attr_type == attr_type and not attr.is_named and attr.non_resident:
            return attr.data_runs or None
    return None


def decode_data_runs(runs_bytes):
    """Decode an NTFS non-resident attribute's data-run list into a list
    of (length_in_clusters, lcn) tuples describing each physical extent,
    in order. A sparse run (no physical allocation) is omitted rather than
    yielded with a placeholder LCN -- callers that need to preserve gaps
    for VCN accounting would have to special-case this, but the one
    current caller (locating $MFT extents) only cares about real,
    allocated extents.

    Each run is: a header byte (low nibble = length-field byte count, high
    nibble = LCN-offset-field byte count), then the length (unsigned,
    little-endian), then -- unless the LCN-offset size is 0, meaning a
    sparse run -- a *signed* little-endian LCN delta relative to the
    previous run's LCN (the first run's delta is relative to 0). The list
    ends at a single 0x00 header byte.
    """
    runs = []
    offset = 0
    current_lcn = 0
    while offset < len(runs_bytes):
        header = runs_bytes[offset]
        if header == 0:
            break
        length_size = header & 0x0F
        offset_size = (header >> 4) & 0x0F
        offset += 1

        length = int.from_bytes(runs_bytes[offset:offset + length_size], "little", signed=False)
        offset += length_size

        if offset_size == 0:
            continue  # sparse run -- no physical allocation, LCN unchanged

        lcn_delta = int.from_bytes(
            runs_bytes[offset:offset + offset_size], "little", signed=True
        )
        offset += offset_size
        current_lcn += lcn_delta
        runs.append((length, current_lcn))

    return runs
