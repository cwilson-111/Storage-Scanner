"""The on-disk layout of one NTFS MFT record: its header, the Update
Sequence Array fixups, the walk over its attributes, and the data-run lists
that say where a non-resident attribute's clusters are.

Pure byte transforms like mft_parser, which turns these raw attributes into
a ParsedRecord; both are tested with hand-built records in
tests/test_mft_parser.py. A run list is only sliced out of its attribute
here; decode_data_runs decodes one only where real clusters must be read:
the $MFT's own extents (mft_volume) and a non-resident $ATTRIBUTE_LIST or
$REPARSE_POINT (_read_nonresident_value).
"""

import struct
from dataclasses import dataclass
from typing import Optional

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
_ATTR_REPARSE_POINT = 0xC0
_ATTR_END_MARKER = 0xFFFFFFFF

# -- Attribute flags (the common header's Flags field) ---------------------- #
# A compressed or sparse non-resident attribute carries one more 8-byte
# field right after the non-resident header: the clusters it really
# occupies on disk. AllocatedSize still counts every cluster of its range,
# including the ones compression saved and the holes of a sparse file.
_ATTR_FLAG_COMPRESSION_MASK = 0x00FF
_ATTR_FLAG_SPARSE = 0x8000
_COMPRESSED_SIZE_OFFSET = _ATTR_COMMON_SIZE + _ATTR_NONRESIDENT_SIZE  # 64


@dataclass
class _RawAttribute:
    attr_type: int
    attribute_id: int
    non_resident: bool
    is_named: bool = False  # True for a named stream (e.g. an alternate
    # data stream) rather than the primary attribute
    name: str = ""  # decoded for a named $DATA stream only
    value: bytes = b""
    allocated_size: int = 0
    real_size: int = 0
    initialized_size: int = 0
    compressed_size: Optional[int] = None  # compressed or sparse only
    data_runs: bytes = b""  # non-resident only; still encoded, never
    # decoded here -- see decode_data_runs

    def on_disk_size(self):
        """Bytes this attribute's value really occupies on disk."""
        if not self.non_resident:
            # Resident data lives inside the MFT record itself -- there's
            # no separate cluster allocation, so this is just its length
            # (matching how tiny/resident files show up through the
            # fallback engine's GetCompressedFileSizeW path).
            return len(self.value)
        if self.compressed_size is not None:
            return self.compressed_size
        return self.allocated_size


def _read_header(raw):
    """Unpack the 48-byte record header, or None if too short/wrong shape."""
    if len(raw) < _RECORD_HEADER_SIZE:
        return None
    fields = struct.unpack_from(_RECORD_HEADER_FORMAT, raw, 0)
    (
        signature,
        usa_offset,
        usa_size,
        _lsn,
        sequence_number,
        hard_link_count,
        first_attribute_offset,
        flags,
        bytes_in_use,
        bytes_allocated,
        base_file_record_segment,
        _next_attribute_id,
        _reserved,
        _mft_record_number,
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


def _apply_fixups(raw, header, sector_size=_SECTOR_SIZE):
    """Reverse the Update Sequence Array substitution, or None if the
    per-sector check fails (a torn/corrupt write).

    NTFS stamps the last 2 bytes of every sector (the volume's real
    BytesPerSector -- 512 on the overwhelming majority of drives, but 4096
    on a native 4Kn volume) in a multi-sector record with a shared "USN"
    value and relocates the real bytes that used to live there into the
    Update Sequence Array, so a partially-written record can be detected
    (the stamped USN would then be missing from one sector). Every other
    field in the record is untrustworthy until this runs -- get the sector
    size wrong and two real bytes every sector are silently corrupted,
    which can land inside a size or name field on a large record.
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
        check_off = i * sector_size - 2
        if check_off + 2 > len(raw):
            break  # record shorter than this sector -- nothing more to fix
        if bytes(raw[check_off : check_off + 2]) != usn:
            return None
        original_off = usa_offset + 2 * i
        if original_off + 2 > len(raw):
            return None
        fixed[check_off : check_off + 2] = raw[original_off : original_off + 2]
    return bytes(fixed)


def _read_header_and_fixup(raw, sector_size=_SECTOR_SIZE):
    header = _read_header(raw)
    if header is None:
        return None, None
    fixed = _apply_fixups(raw, header, sector_size)
    if fixed is None:
        return None, None
    return header, fixed


def _iter_attributes(fixed, header):
    """Walk one record's attribute area, yielding a _RawAttribute per
    attribute up to the 0xFFFFFFFF end marker or `bytes_in_use`.

    Each attribute's own `length` field (not a hardcoded header size) is
    the stride to the next one -- this is what keeps the walk correct
    regardless of a non-resident attribute's optional trailing
    CompressedSize field (present only when it's compressed or sparse),
    since that field is never read as part of computing the next offset.
    """
    offset = header["first_attribute_offset"]
    end = min(header["bytes_in_use"], len(fixed))
    while offset + 4 <= end:
        attr_type = struct.unpack_from("<I", fixed, offset)[0]
        if attr_type == _ATTR_END_MARKER:
            break
        if offset + _ATTR_COMMON_SIZE > end:
            break
        _type, length, non_resident, name_length, name_offset, flags, attribute_id = (
            struct.unpack_from(_ATTR_COMMON_FORMAT, fixed, offset)
        )
        if length == 0 or offset + length > end:
            break  # corrupt -- stop walking defensively rather than loop/overrun
        is_named = name_length != 0
        name = ""
        if is_named and attr_type == _ATTR_DATA:
            name_start = offset + name_offset
            name = fixed[name_start : name_start + name_length * 2].decode(
                "utf-16-le", errors="replace"
            )

        if non_resident:
            nrh_off = offset + _ATTR_COMMON_SIZE
            if nrh_off + _ATTR_NONRESIDENT_SIZE <= end:
                (
                    _starting_vcn,
                    _last_vcn,
                    data_runs_offset,
                    _compression_unit,
                    _reserved,
                    allocated_size,
                    real_size,
                    initialized_size,
                ) = struct.unpack_from(_ATTR_NONRESIDENT_FORMAT, fixed, nrh_off)
                compressed_size = None
                size_end = _COMPRESSED_SIZE_OFFSET + 8
                if (
                    flags & (_ATTR_FLAG_COMPRESSION_MASK | _ATTR_FLAG_SPARSE)
                    and size_end <= length
                    and (not data_runs_offset or data_runs_offset >= size_end)
                ):
                    compressed_size = struct.unpack_from(
                        "<Q", fixed, offset + _COMPRESSED_SIZE_OFFSET
                    )[0]
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
                    attr_type=attr_type,
                    attribute_id=attribute_id,
                    non_resident=True,
                    is_named=is_named,
                    name=name,
                    allocated_size=allocated_size,
                    real_size=real_size,
                    initialized_size=initialized_size,
                    compressed_size=compressed_size,
                    data_runs=data_runs,
                )
        else:
            rh_off = offset + _ATTR_COMMON_SIZE
            if rh_off + _ATTR_RESIDENT_SIZE <= end:
                value_length, value_offset, _resident_flags, _reserved = struct.unpack_from(
                    _ATTR_RESIDENT_FORMAT, fixed, rh_off
                )
                value_start = offset + value_offset
                value = bytes(fixed[value_start : value_start + value_length])
                yield _RawAttribute(
                    attr_type=attr_type,
                    attribute_id=attribute_id,
                    non_resident=False,
                    is_named=is_named,
                    name=name,
                    value=value,
                )

        offset += length


def _read_nonresident_value(raw_attr, record_source):
    """Reassemble a non-resident attribute's real bytes (an $ATTRIBUTE_LIST
    or a $REPARSE_POINT that didn't fit in its record) by reading its own
    data runs' clusters straight off the volume, or None if that isn't
    possible (a record_source that can't do raw cluster reads -- e.g. the
    lightweight record_at-only fakes some tests use -- an empty/undecodable
    run list, or a failed read), all treated as best-effort, never an error.

    Assumes no run is sparse (offset_size == 0), which decode_data_runs
    silently drops -- true of both attributes in practice, since sparse is
    a $DATA-stream-only NTFS feature, but would misalign/truncate the
    reassembled bytes if it ever weren't.
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
    return b"".join(chunks)[: raw_attr.real_size]


def get_nonresident_data_runs_bytes(record_bytes, attr_type=_ATTR_DATA, sector_size=_SECTOR_SIZE):
    """Locate one raw MFT record's first non-resident, unnamed attribute
    of `attr_type` and return its still-encoded data-run bytes, or None if
    there's no such attribute (missing, resident, or named).

    This is the one place run-list bytes are exposed at all -- everything
    else, here and in mft_parser (parse_base_record included), sizes a non-resident
    attribute from its header fields (AllocatedSize/RealSize/
    CompressedSize), never its runs, and decodes runs only to read a
    spilled-out $ATTRIBUTE_LIST or $REPARSE_POINT. The sole
    consumer of this function is storage_scanner.mft_volume, which needs
    the $MFT's own record #0 $DATA runs to find every physical extent of a
    (possibly fragmented) $MFT -- decoding those bytes into (length, LCN)
    pairs happens there via decode_data_runs, not here.

    `sector_size` defaults to 512 (the near-universal case); mft_volume
    passes the volume's real BytesPerSector, since this is called to
    bootstrap the $MFT's own layout before a RecordSource object (which
    would otherwise carry that value) exists.
    """
    header, fixed = _read_header_and_fixup(record_bytes, sector_size)
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

        length = int.from_bytes(runs_bytes[offset : offset + length_size], "little", signed=False)
        offset += length_size

        if offset_size == 0:
            continue  # sparse run -- no physical allocation, LCN unchanged

        lcn_delta = int.from_bytes(runs_bytes[offset : offset + offset_size], "little", signed=True)
        offset += offset_size
        current_lcn += lcn_delta
        runs.append((length, current_lcn))

    return runs
