"""Raw NTFS volume access for Turbo Scan: opens a volume, locates every
physical extent of its Master File Table, and streams its raw bytes in
large chunks, one MFT record at a time.

Deliberately thin on branching logic -- all real parsing/tree-building
logic lives in mft_parser.py/mft_scan.py instead, which take RecordSource
as a duck-typed dependency (`record_at(n) -> bytes`, `record_count`) and
carry the bulk of this feature's test coverage with hand-built fakes. The
actual CreateFileW/DeviceIoControl/ReadFile calls here can only be
exercised end-to-end on a real, elevated Windows session against a real
NTFS volume -- but the chunk-caching, extent-resolution, and offset
arithmetic around them (RecordSource's real logic) is unit-tested in
tests/test_mft_volume.py against a faked ctypes.windll.kernel32, the same
technique already used for storage_scanner.drive_info/file_ops.

An earlier version of this module treated the $MFT as one contiguous span
starting at MftStartLcn for MftValidDataLength bytes. That's wrong for any
$MFT with more than one extent -- MftValidDataLength is the *logical*
size of the whole $MFT stream, not a promise it's physically contiguous
-- and the Turbo Scan validation gate (compare_scan_engines.py) caught it
on the very first real, well-used volume it ran against: entire
foundational directories (C:\\Windows, C:\\Users) were silently missing
from the scanned tree, because most of a real, fragmented $MFT was never
being read at all. Record #0 ($MFT's own record) is always physically at
the start of the first extent, so reading *it* via the naive single-
extent assumption is always safe; decoding its own $DATA attribute's data
runs (mft_parser.decode_data_runs) then gives the $MFT's true, possibly
multi-extent layout, which is what RecordSource now actually uses.
"""

import ctypes
from ctypes import wintypes

from storage_scanner import mft_parser
from storage_scanner.logging_setup import logger

_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_FILE_BEGIN = 0

_FSCTL_GET_NTFS_VOLUME_DATA = 0x00090064

# A multiple of both the sector size and any real BytesPerFileRecordSegment
# (1024 on every NTFS volume in practice) -- large enough to make sequential
# scans through the whole MFT fast, without needing a syscall per record.
_READ_CHUNK_BYTES = 4 * 1024 * 1024

# The aux (out-of-order lookup) chunk is deliberately much smaller than the
# primary one above. Measured on a real, heavily hard-linked C:\Windows:
# out-of-order $ATTRIBUTE_LIST extension-record lookups reused an already-
# loaded aux chunk only ~5x on average before needing a different one, so a
# full 4MB window per lookup was reading ~4096x more data than needed just
# to capture that much weaker locality. Still a multiple of the sector/
# record size, and still big enough to capture same-record extension
# records that do cluster together (common -- see RecordSource docstring).
_AUX_READ_CHUNK_BYTES = 64 * 1024


class MftVolumeError(Exception):
    """Any failure opening a volume or reading its $MFT. Always caught by
    storage_scanner.turbo_scan's broad fallback-to-Compatible-engine
    handling -- never expected to reach a user directly."""


class _NTFS_VOLUME_DATA_BUFFER(ctypes.Structure):
    _fields_ = [
        ("VolumeSerialNumber", ctypes.c_int64),
        ("NumberSectors", ctypes.c_int64),
        ("TotalClusters", ctypes.c_int64),
        ("FreeClusters", ctypes.c_int64),
        ("TotalReserved", ctypes.c_int64),
        ("BytesPerSector", wintypes.DWORD),
        ("BytesPerCluster", wintypes.DWORD),
        ("BytesPerFileRecordSegment", wintypes.DWORD),
        ("ClustersPerFileRecordSegment", wintypes.DWORD),
        ("MftValidDataLength", ctypes.c_int64),
        ("MftStartLcn", ctypes.c_int64),
        ("Mft2StartLcn", ctypes.c_int64),
        ("MftZoneStart", ctypes.c_int64),
        ("MftZoneEnd", ctypes.c_int64),
    ]


def _open_volume(volume_root):
    # CreateFileW wants the bare device path ("\\\\.\\C:", no trailing
    # backslash) for raw volume access -- volume_root (from
    # drive_info.get_volume_root) is "C:\\", so strip the separator.
    device_path = "\\\\.\\" + volume_root.rstrip("\\")
    kernel32 = ctypes.windll.kernel32
    # HANDLE is pointer-sized -- without this, ctypes' default 32-bit
    # signed-int return type would truncate/misinterpret it on 64-bit
    # Windows (the same class of bug file_ops.py's ShellExecuteW binding
    # already works around for its own pointer-sized HINSTANCE return).
    kernel32.CreateFileW.restype = ctypes.c_void_p
    handle = kernel32.CreateFileW(
        device_path, _GENERIC_READ, _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None, _OPEN_EXISTING, 0, None,
    )
    if handle is None or handle == _INVALID_HANDLE_VALUE:
        raise MftVolumeError(
            f"Could not open {device_path!r} for raw access (admin rights required)"
        )
    return handle


def _get_ntfs_volume_data(handle):
    buffer = _NTFS_VOLUME_DATA_BUFFER()
    bytes_returned = wintypes.DWORD(0)
    succeeded = ctypes.windll.kernel32.DeviceIoControl(
        handle, _FSCTL_GET_NTFS_VOLUME_DATA, None, 0,
        ctypes.byref(buffer), ctypes.sizeof(buffer),
        ctypes.byref(bytes_returned), None,
    )
    if not succeeded:
        raise MftVolumeError("FSCTL_GET_NTFS_VOLUME_DATA failed")
    return buffer


def _set_file_pointer(handle, offset):
    # SetFilePointerEx takes a signed 64-bit distance -- ctypes needs the
    # exact width declared or the high 32 bits silently get dropped.
    distance = ctypes.c_int64(offset)
    new_position = ctypes.c_int64(0)
    succeeded = ctypes.windll.kernel32.SetFilePointerEx(
        handle, distance, ctypes.byref(new_position), _FILE_BEGIN,
    )
    if not succeeded:
        raise MftVolumeError(f"SetFilePointerEx failed seeking to offset {offset}")


def _read_bytes(handle, offset, length):
    _set_file_pointer(handle, offset)
    buffer = ctypes.create_string_buffer(length)
    bytes_read = wintypes.DWORD(0)
    succeeded = ctypes.windll.kernel32.ReadFile(
        handle, buffer, length, ctypes.byref(bytes_read), None,
    )
    if not succeeded:
        raise MftVolumeError(f"ReadFile failed at offset {offset} (length {length})")
    if bytes_read.value != length:
        raise MftVolumeError(
            f"ReadFile returned {bytes_read.value} bytes, expected {length}, "
            f"at offset {offset}"
        )
    return buffer.raw[:bytes_read.value]


def _close_handle(handle):
    try:
        ctypes.windll.kernel32.CloseHandle(handle)
    except OSError:
        logger.debug("CloseHandle failed for an MFT volume handle", exc_info=True)


def _resolve_mft_extents(handle, mft_start_lcn, bytes_per_cluster, record_size, bytes_per_sector):
    """Return every physical extent of the $MFT as a list of
    (start_record_number, record_count, lcn) tuples, in record-number
    order, decoded from record #0's ($MFT's own record) $DATA attribute.

    Record #0 is always physically at the very start of the $MFT's first
    extent -- reading it via the naive single-extent assumption is the one
    place that's always safe, and is how the $MFT's *real* layout is
    bootstrapped in the first place.

    Raises MftVolumeError if record #0's $DATA runs can't be found/decoded
    at all (should be exceedingly rare -- record #0 is as foundational as
    NTFS metadata gets) -- silently falling back to the old single-extent
    guess here would just reintroduce the exact bug this function exists
    to fix, so this surfaces as a normal Turbo Scan failure instead
    (turbo_scan.py's broad except falls back to the Compatible engine).
    """
    record0_offset = mft_start_lcn * bytes_per_cluster
    record0_bytes = _read_bytes(handle, record0_offset, record_size)

    runs_bytes = mft_parser.get_nonresident_data_runs_bytes(
        record0_bytes, sector_size=bytes_per_sector,
    )
    if not runs_bytes:
        raise MftVolumeError(
            "Could not find $MFT's own non-resident $DATA run list on record #0 "
            "-- can't determine the $MFT's real (possibly multi-extent) layout"
        )

    records_per_cluster = bytes_per_cluster // record_size
    if records_per_cluster <= 0:
        raise MftVolumeError(
            f"Implausible records-per-cluster ({records_per_cluster}) for "
            f"BytesPerCluster={bytes_per_cluster}, BytesPerFileRecordSegment={record_size}"
        )

    extents = []
    next_record = 0
    for length_clusters, lcn in mft_parser.decode_data_runs(runs_bytes):
        record_count = length_clusters * records_per_cluster
        if record_count > 0:
            extents.append((next_record, record_count, lcn))
        next_record += record_count

    if not extents:
        raise MftVolumeError("$MFT's own data runs decoded to zero usable extents")
    return extents


class RecordSource:
    """Duck-typed record source for mft_parser.parse_base_record and
    mft_scan.build_tree: `record_at(n) -> bytes` plus a `record_count`
    attribute, backed by a raw NTFS volume handle.

    Sequential access (the common case -- walking record 0, 1, 2, ...) is
    served from a large in-memory chunk with no syscall per record, one
    chunk load per (extent, chunk-within-that-extent) pair -- a chunk
    never spans two extents, since they aren't guaranteed physically
    adjacent.

    Out-of-order access (mft_parser's $ATTRIBUTE_LIST extension-record
    lookups, interleaved with the main sequential walk) is served from a
    second, independent chunk slot rather than evicting the sequential
    one. That split used to not exist -- a single shared chunk meant every
    out-of-order lookup evicted the sequential walk's chunk, and the very
    next sequential record_at() call then had to reload it right back,
    turning one interruption into (at least) two full chunk reads. That
    was believed harmless ("correctness first, since that path is rare"),
    until fixing mft_parser's non-resident-$ATTRIBUTE_LIST handling made
    those lookups actually happen for the first time on a real, heavily
    hard-linked C:\\Windows: measured (via a temporary instrumented run,
    not a unit test -- this class's own tests use small synthetic volumes
    far too small to reproduce thrashing at any real scale) at 95,598
    chunk loads for what a clean sequential pass over the same ~1.15M
    records needs only ~282 of -- 93.8% of total scan time.

    record_at() tracks which record continues the sequential walk with an
    explicit `_sequential_position`, not by asking "is record_number
    already cached somewhere" -- a first attempt at the primary/aux split
    got this wrong (checked aux before deciding primary-vs-aux) and made
    no real improvement (88,481 aux loads, 94.3% of scan time, on the same
    real volume): whenever an out-of-order lookup happened to land in the
    region the walk was about to reach next, aux silently satisfied it,
    primary never advanced to reclaim that role, and the walk itself
    ended up permanently misclassified as "out of order" against a stale
    primary chunk that stopped updating after its very first load.
    """

    def __init__(self, volume_root):
        self._handle = _open_volume(volume_root)
        try:
            volume_data = _get_ntfs_volume_data(self._handle)
            self._record_size = volume_data.BytesPerFileRecordSegment
            if self._record_size <= 0:
                raise MftVolumeError(
                    f"Implausible BytesPerFileRecordSegment ({self._record_size}) "
                    f"reported for {volume_root!r}"
                )
            self._bytes_per_cluster = volume_data.BytesPerCluster
            # Public: mft_parser.parse_base_record reads this off the
            # record source to apply fixups at the volume's real sector
            # size instead of assuming 512 -- see mft_parser._apply_fixups.
            self.bytes_per_sector = volume_data.BytesPerSector
            self._extents = _resolve_mft_extents(
                self._handle, volume_data.MftStartLcn,
                self._bytes_per_cluster, self._record_size, self.bytes_per_sector,
            )
        except MftVolumeError:
            _close_handle(self._handle)
            raise

        # Public, for storage_scanner.turbo_cache (cache key) and
        # storage_scanner.usn_journal (shares this handle rather than
        # opening the volume a second time -- see raw_handle below).
        self.volume_serial = volume_data.VolumeSerialNumber
        self.record_size = self._record_size

        self.record_count = sum(count for _start, count, _lcn in self._extents)
        self._chunk_records = max(1, _READ_CHUNK_BYTES // self._record_size)
        self._chunk_start_record = None
        self._chunk_record_count = 0
        self._chunk_data = b""
        # Second, independent slot for out-of-order lookups -- deliberately
        # much smaller than the primary chunk above (see
        # _AUX_READ_CHUNK_BYTES) and see the class docstring for why this
        # can't just share the primary chunk at all.
        self._aux_chunk_records = max(1, _AUX_READ_CHUNK_BYTES // self._record_size)
        self._aux_chunk_start_record = None
        self._aux_chunk_record_count = 0
        self._aux_chunk_data = b""
        # The sequential walk's logical position, tracked explicitly and
        # updated *only* by a primary-chunk hit or load -- never by an aux
        # one. This is what actually decides "is this call a continuation
        # of the walk," not "does some cache happen to already have this
        # record" -- see record_at for why that distinction was the real
        # bug in the first version of the primary/aux split.
        self._sequential_position = None

    def record_at(self, record_number):
        if not 0 <= record_number < self.record_count:
            raise IndexError(record_number)

        # Fast path, checked unconditionally: any record still within the
        # already-loaded primary chunk is free, whether or not this call
        # is itself "the next" one in the walk -- an out-of-order lookup
        # that happens to land nearby (common: an extension record is
        # often physically close to its base record) gets served from the
        # sequential walk's own chunk instead of costing a fresh aux load.
        if self._chunk_start_record is not None and (
            self._chunk_start_record
            <= record_number
            < self._chunk_start_record + self._chunk_record_count
        ):
            self._sequential_position = record_number
            return self._slice(self._chunk_data, self._chunk_start_record, record_number)

        # A primary miss: this either continues the sequential walk (past
        # the end of the current chunk) or is a genuine out-of-order jump.
        # This MUST be decided from _sequential_position, not from
        # whether aux happens to already hold record_number -- checking
        # aux first here was the bug in the original primary/aux split:
        # whenever an out-of-order lookup happened to land in the region
        # the walk was about to reach next, aux would silently satisfy it,
        # primary would never advance to reclaim that role, and every
        # later genuinely-sequential call would then look non-sequential
        # against a permanently stale primary chunk -- so aux ended up
        # doing everything the single shared chunk used to (88,481 loads,
        # 94.3% of scan time, measured against a real C:\Windows -- no
        # better than before the split existed).
        is_sequential = (
            self._sequential_position is None
            or record_number == self._sequential_position + 1
        )
        if is_sequential:
            self._sequential_position = record_number
            self._load_chunk_containing(record_number)
            return self._slice(self._chunk_data, self._chunk_start_record, record_number)

        if self._aux_chunk_start_record is not None and (
            self._aux_chunk_start_record
            <= record_number
            < self._aux_chunk_start_record + self._aux_chunk_record_count
        ):
            return self._slice(self._aux_chunk_data, self._aux_chunk_start_record, record_number)
        self._load_aux_chunk_containing(record_number)
        return self._slice(self._aux_chunk_data, self._aux_chunk_start_record, record_number)

    def _slice(self, data, start_record, record_number):
        local_offset = (record_number - start_record) * self._record_size
        return data[local_offset:local_offset + self._record_size]

    def read_clusters(self, lcn, cluster_count):
        """Read `cluster_count` clusters starting at LCN `lcn` straight off
        the volume, uncached -- used only to reassemble a non-resident
        $ATTRIBUTE_LIST (see mft_parser._parse_attribute_list), which lives
        in its own data runs rather than the MFT record stream this class
        otherwise serves."""
        offset = lcn * self._bytes_per_cluster
        length = cluster_count * self._bytes_per_cluster
        return _read_bytes(self._handle, offset, length)

    def _find_extent(self, record_number):
        for extent_start, extent_count, lcn in self._extents:
            if extent_start <= record_number < extent_start + extent_count:
                return extent_start, extent_count, lcn
        raise MftVolumeError(
            f"record {record_number} is not covered by any known $MFT extent "
            f"(record_count={self.record_count})"
        )

    def _read_chunk_containing(self, record_number, chunk_records):
        """Read the whole `chunk_records`-sized, extent-clamped chunk that
        contains `record_number`, returning (data, chunk_start_record,
        chunk_record_count) -- shared by the primary and aux chunk loaders,
        which differ only in chunk size and which instance attributes they
        store the result into.
        """
        extent_start, extent_count, extent_lcn = self._find_extent(record_number)
        offset_in_extent = record_number - extent_start
        # Clamp the chunk to stay within this one extent -- a single
        # ReadFile call must be physically contiguous, and extents aren't
        # guaranteed adjacent on disk.
        chunk_start_in_extent = (offset_in_extent // chunk_records) * chunk_records
        records_to_read = min(chunk_records, extent_count - chunk_start_in_extent)

        byte_offset = (
            extent_lcn * self._bytes_per_cluster
            + chunk_start_in_extent * self._record_size
        )
        length = records_to_read * self._record_size
        data = _read_bytes(self._handle, byte_offset, length)
        return data, extent_start + chunk_start_in_extent, records_to_read

    def _load_chunk_containing(self, record_number):
        self._chunk_data, self._chunk_start_record, self._chunk_record_count = (
            self._read_chunk_containing(record_number, self._chunk_records)
        )

    def _load_aux_chunk_containing(self, record_number):
        self._aux_chunk_data, self._aux_chunk_start_record, self._aux_chunk_record_count = (
            self._read_chunk_containing(record_number, self._aux_chunk_records)
        )

    @property
    def raw_handle(self):
        """The open volume HANDLE, for storage_scanner.usn_journal's
        FSCTL_QUERY_USN_JOURNAL/FSCTL_READ_USN_JOURNAL calls -- both are
        read-only operations, so this read-only handle serves them fine,
        avoiding a second CreateFileW call/handle for the same device.

        Deliberately read-only, same as always: an earlier version of
        this class opened with GENERIC_READ | GENERIC_WRITE so this same
        handle could also serve FSCTL_CREATE_USN_JOURNAL (which does need
        write access) -- confirmed on a real machine to be a real mistake,
        not just an unnecessary permission ask: FortiClient (this user's
        AV/EDR) outright blocked opening the volume at all once write
        access was requested, breaking every Turbo Scan, not just journal
        creation. usn_journal.ensure_journal now queries for an already-
        existing journal first (the common case on any real, actively-
        used Windows system) and only ever *attempts* FSCTL_CREATE_USN_
        JOURNAL -- on this same read-only handle, which just fails
        cleanly with an OS-level access-denied rather than an AV block --
        as a last resort. A volume with no journal at all simply never
        gets incremental refresh; it still gets a full scan every time,
        exactly like before this feature existed."""
        return self._handle

    def close(self):
        _close_handle(self._handle)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


def open_record_source(volume_root):
    return RecordSource(volume_root)
