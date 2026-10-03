"""Byte-level parsing of NTFS Master File Table (MFT) records.

Every function here, and in mft_records (the record's on-disk layout: its
header, fixups, attribute walk and data runs), is a pure transform of
`bytes` -> structured data. There is no filesystem or ctypes.windll access
anywhere in either module, which is deliberate: it means the entire
correctness-critical parsing logic (fixups, attribute walking, $FILE_NAME
selection, $ATTRIBUTE_LIST merging) can be exercised by
tests/test_mft_parser.py with hand-built byte fixtures, no admin rights or
real NTFS volume required. A future raw-volume reader will feed real record
bytes in from disk (planned as storage_scanner/mft_volume.py) and is
intended to stay too thin itself to need its own tests -- all real parsing
logic belongs here instead.

Callers pass a "record source": any object with a `record_at(record_number)
-> bytes` method returning exactly one fixed-size MFT record's raw bytes.
This lets tests substitute an in-memory dict-backed fake for the real
sequential/random-access volume reader.
"""

import stat as _stat
import struct
from dataclasses import dataclass, field

from storage_scanner.alloc_size import is_cloud_placeholder_attrs
from storage_scanner.mft_records import (
    _ATTR_ATTRIBUTE_LIST,
    _ATTR_DATA,
    _ATTR_FILE_NAME,
    _ATTR_REPARSE_POINT,
    _ATTR_STANDARD_INFORMATION,
    _RECORD_FLAG_IN_USE,
    _RECORD_FLAG_IS_DIRECTORY,
    _SECTOR_SIZE,
    _iter_attributes,
    _read_header_and_fixup,
    _read_nonresident_value,
)

_FILE_ATTRIBUTE_REPARSE_POINT = _stat.FILE_ATTRIBUTE_REPARSE_POINT

# -- Reparse tags (the first 4 bytes of $REPARSE_POINT) --------------------- #
# The Cloud Files API's tags (OneDrive Files On-Demand and other sync
# engines): IO_REPARSE_TAG_CLOUD and its variants _CLOUD_1.._CLOUD_F,
# which differ only in bits 12-15, plus the older IO_REPARSE_TAG_ONEDRIVE.
_REPARSE_TAG_CLOUD = 0x9000001A
_REPARSE_TAG_CLOUD_MASK = 0xFFFF0FFF
_REPARSE_TAG_ONEDRIVE = 0x80000021
# Windows Overlay Filter compression (Compact OS, `compact /exe`): the
# unnamed $DATA is sparse and empty, and the compressed bytes live in a
# named $DATA stream instead.
_REPARSE_TAG_WOF = 0x80000017
_WOF_STREAM_NAME = "WofCompressedData"

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
    after. `alloc_size` is what the data really occupies on disk, as
    GetCompressedFileSizeW reports it for the Compatible engine: less than
    the logical size for a compressed, sparse or Compact OS-compressed file.
    `names` always has at least one entry (a record with none isn't
    linked into any directory and is treated as unparseable -- see
    parse_base_record).

    `is_link` marks a reparse point that stands for another path -- a
    junction, symbolic link or mount point -- which a scan never follows
    below its root. A cloud-sync reparse point (OneDrive Files On-Demand)
    isn't one: that folder holds its own local files, and the cloud filter
    hides its reparse bit from ordinary callers, so the Compatible engine
    walks it like any folder.
    """

    frn: int
    is_directory: bool
    file_attributes: int
    is_link: bool
    is_cloud_placeholder: bool
    mtime: float
    atime: float
    logical_size: int
    alloc_size: int
    names: list = field(default_factory=list)


@dataclass
class _AttributeListEntry:
    attr_type: int
    attribute_id: int
    base_frn: int  # FRN of the record segment actually holding this instance


def _parse_standard_information(value):
    if len(value) < _STANDARD_INFO_MIN_SIZE:
        return None
    _creation, modified, _changed, accessed, file_attributes = struct.unpack_from(
        _STANDARD_INFO_FORMAT, value, 0
    )
    return {
        "mtime": _filetime_to_unix(modified),
        "atime": _filetime_to_unix(accessed),
        "file_attributes": file_attributes,
    }


def _parse_file_name(value):
    if len(value) < _FILE_NAME_FIXED_SIZE + 2:
        return None
    parent_frn, _ctime, _mtime, _chtime, _atime, _alloc, _real, _attrs, _ea = struct.unpack_from(
        _FILE_NAME_FIXED_FORMAT, value, 0
    )
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


def _parse_attribute_list(raw_attr, record_source):
    """Resolve a $ATTRIBUTE_LIST's entries, resident or non-resident.

    A resident list is parsed directly out of the record. A non-resident
    one (needed once a heavily hard-linked file/directory -- common in
    WinSxS/GAC-style system areas -- has too many $FILE_NAME/other entries
    to fit inline) is reassembled via _read_nonresident_value; if that
    fails, this returns [] ("no extra attributes resolvable"), same as
    always, never an error.
    """
    if raw_attr.non_resident:
        value = _read_nonresident_value(raw_attr, record_source)
        if value is None:
            return []
    else:
        value = raw_attr.value
    entries = []
    offset = 0
    while offset + _ATTR_LIST_ENTRY_MIN_SIZE <= len(value):
        attr_type, entry_length, _name_length, _name_offset = struct.unpack_from(
            _ATTR_LIST_ENTRY_HEAD_FORMAT, value, offset
        )
        if entry_length == 0:
            break
        _starting_vcn, base_frn, attribute_id = struct.unpack_from(
            _ATTR_LIST_ENTRY_TAIL_FORMAT, value, offset + _ATTR_LIST_ENTRY_HEAD_SIZE
        )
        entries.append(
            _AttributeListEntry(
                attr_type=attr_type,
                attribute_id=attribute_id,
                base_frn=base_frn,
            )
        )
        offset += entry_length
    return entries


def _reparse_tag(raw_attr, record_source):
    """The reparse tag at the start of a $REPARSE_POINT's value, or 0 when
    it can't be read (the caller then treats the record as a link, the
    safe default -- see ParsedRecord)."""
    if raw_attr is None:
        return 0
    value = raw_attr.value
    if raw_attr.non_resident:
        value = _read_nonresident_value(raw_attr, record_source) or b""
    return struct.unpack_from("<I", value)[0] if len(value) >= 4 else 0


def _is_cloud_tag(tag):
    return (tag & _REPARSE_TAG_CLOUD_MASK) == _REPARSE_TAG_CLOUD or tag == _REPARSE_TAG_ONEDRIVE


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
    # Real record sources (mft_volume.RecordSource) carry the volume's
    # actual BytesPerSector; fakes/tests without one default to 512, the
    # near-universal case, matching this module's previous hardcoded
    # behavior.
    sector_size = getattr(record_source, "bytes_per_sector", _SECTOR_SIZE)

    raw = record_source.record_at(record_number)
    header, fixed = _read_header_and_fixup(raw, sector_size)
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
            ext_header, ext_fixed = _read_header_and_fixup(ext_raw, sector_size)
            if ext_header is None:
                continue
            if not (ext_header["flags"] & _RECORD_FLAG_IN_USE):
                continue  # slot was freed and not yet reused -- stale reference
            expected_sequence = (entry.base_frn >> 48) & 0xFFFF
            if ext_header["sequence_number"] != expected_sequence:
                # NTFS reused this record slot for a different file since the
                # $ATTRIBUTE_LIST entry was written -- trusting it here would
                # merge that other file's attributes into this one.
                continue
            for ext_attr in _iter_attributes(ext_fixed, ext_header):
                if (
                    ext_attr.attr_type == entry.attr_type
                    and ext_attr.attribute_id == entry.attribute_id
                ):
                    attrs.append(ext_attr)

    std_info = None
    names = []
    data_attr = None
    wof_attr = None
    reparse_attr = None
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
            # ADS support is explicitly out of scope for now. The one
            # exception is Compact OS's stream, below.
            data_attr = attr
        elif attr.attr_type == _ATTR_DATA and attr.name == _WOF_STREAM_NAME:
            wof_attr = attr
        elif attr.attr_type == _ATTR_REPARSE_POINT and reparse_attr is None:
            reparse_attr = attr

    if std_info is None:
        return None

    names = _dedup_file_names(names)
    if not names:
        return None

    file_attributes = std_info["file_attributes"]
    is_reparse_point = bool(file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT)
    tag = _reparse_tag(reparse_attr, record_source) if is_reparse_point else 0

    if data_attr is None:
        logical_size = 0
        alloc_size = 0
    else:
        logical_size = data_attr.real_size if data_attr.non_resident else len(data_attr.value)
        alloc_size = data_attr.on_disk_size()
    if tag == _REPARSE_TAG_WOF and wof_attr is not None:
        # The unnamed stream is sparse and holds nothing; the compressed
        # stream is what the file occupies on disk.
        alloc_size += wof_attr.on_disk_size()

    return ParsedRecord(
        frn=_pack_frn(header["sequence_number"], record_number),
        is_directory=bool(header["flags"] & _RECORD_FLAG_IS_DIRECTORY),
        file_attributes=file_attributes,
        is_link=is_reparse_point and not _is_cloud_tag(tag),
        is_cloud_placeholder=is_cloud_placeholder_attrs(file_attributes),
        mtime=std_info["mtime"],
        atime=std_info["atime"],
        logical_size=logical_size,
        alloc_size=alloc_size,
        names=names,
    )
