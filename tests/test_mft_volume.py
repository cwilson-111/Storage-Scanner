"""Tests for storage_scanner.mft_volume.RecordSource against a faked
ctypes.windll.kernel32 -- the same fake-the-Win32-call technique already
used by tests/test_elevation.py and tests/test_drive_info.py.

RecordSource now bootstraps the $MFT's *real* (possibly multi-extent)
physical layout by reading record #0 ($MFT's own record) and decoding its
$DATA attribute's data runs, rather than assuming one contiguous span --
that assumption was a real, confirmed bug (see mft_volume.py's module
docstring and the Turbo Scan plan) caught by running the validation gate
against a real, fragmented $MFT. So every fixture here needs a genuinely
valid record #0, not just filler bytes, and the multi-extent tests are
the ones that actually prove the fix -- they place extents at physically
non-adjacent offsets with an unreadable gap in between, so a regression
back to the single-span assumption would read the wrong data entirely
rather than merely index slightly off.

The real CreateFileW/DeviceIoControl/ReadFile calls can only be verified
end-to-end on a real, elevated Windows session against a real NTFS
volume. What's tested here is RecordSource's own logic around them:
extent resolution, chunk caching within an extent, offset arithmetic
across extents, bounds checking, and that every failure path raises
MftVolumeError rather than some other exception or a silent wrong answer.
"""

import ctypes
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import mft_volume
from storage_scanner.mft_volume import MftVolumeError, RecordSource

_RECORD_SIZE = 1024
_SECTOR_SIZE = 512
_USA_OFFSET = 48
_USA_SIZE = _RECORD_SIZE // _SECTOR_SIZE + 1
_FIRST_ATTR_OFFSET = ((_USA_OFFSET + _USA_SIZE * 2) + 7) // 8 * 8
_ATTR_DATA = 0x80
_ATTR_END_MARKER = 0xFFFFFFFF

# 1:1 cluster-to-record ratio for most fixtures, purely to keep the
# arithmetic simple for tests that aren't specifically about the
# records-per-cluster conversion (that gets its own dedicated test with a
# realistic 4096-byte cluster below).
_BYTES_PER_CLUSTER = _RECORD_SIZE


# -- fixture-building: a genuinely valid $MFT record #0 --------------------- #


def _minimal_unsigned_bytes(value):
    n = 1
    while True:
        try:
            return value.to_bytes(n, "little", signed=False)
        except OverflowError:
            n += 1


def _minimal_signed_bytes(value):
    n = 1
    while True:
        try:
            return value.to_bytes(n, "little", signed=True)
        except OverflowError:
            n += 1


def _encode_runs(extents):
    """`extents` is a list of (length_clusters, absolute_lcn) tuples, in
    order -- the inverse of mft_parser.decode_data_runs (which is already
    independently verified against hand-built bytes in
    tests/test_mft_parser.py, so reusing that same logic here as a
    fixture-building convenience doesn't weaken what's under test there)."""
    out = bytearray()
    previous_lcn = 0
    for length, lcn in extents:
        delta = lcn - previous_lcn
        previous_lcn = lcn
        length_bytes = _minimal_unsigned_bytes(length)
        delta_bytes = _minimal_signed_bytes(delta)
        header = (len(delta_bytes) << 4) | len(length_bytes)
        out.append(header)
        out.extend(length_bytes)
        out.extend(delta_bytes)
    out.append(0)
    return bytes(out)


def _stamp_fixups(record):
    record = bytearray(record)
    usn = b"\x01\x00"
    record[_USA_OFFSET : _USA_OFFSET + 2] = usn
    for i in range(1, _USA_SIZE):
        sector_end = i * _SECTOR_SIZE - 2
        original = bytes(record[sector_end : sector_end + 2])
        record[_USA_OFFSET + 2 * i : _USA_OFFSET + 2 * i + 2] = original
        record[sector_end : sector_end + 2] = usn
    return bytes(record)


def _nonresident_data_attr(runs_bytes, attribute_id=2):
    common_len = 16
    nrh_len = 48
    data_runs_offset = common_len + nrh_len
    unpadded = data_runs_offset + len(runs_bytes)
    total_len = (unpadded + 7) // 8 * 8
    common = struct.pack("<IIBBHHH", _ATTR_DATA, total_len, 1, 0, 0, 0, attribute_id)
    nrh = struct.pack("<QQHHIQQQ", 0, 0, data_runs_offset, 0, 0, 0, 0, 0)
    body = bytearray(common + nrh)
    body.extend(runs_bytes)
    body.extend(b"\x00" * (total_len - len(body)))
    return bytes(body)


def _build_record0(extents, record_size=_RECORD_SIZE):
    """A minimal valid MFT record #0: header + fixups + one non-resident,
    unnamed $DATA attribute whose data runs describe `extents` (a list of
    (length_clusters, absolute_lcn) tuples). No $STANDARD_INFORMATION or
    $FILE_NAME needed -- resolving extents only ever calls
    mft_parser.get_nonresident_data_runs_bytes, never parse_base_record."""
    runs_bytes = _encode_runs(extents)
    data_attr = _nonresident_data_attr(runs_bytes)
    attrs = data_attr + struct.pack("<I", _ATTR_END_MARKER)

    bytes_in_use = _FIRST_ATTR_OFFSET + len(attrs)
    header = struct.pack(
        "<4sHHQHHHHIIQHHI",
        b"FILE",
        _USA_OFFSET,
        _USA_SIZE,
        0,
        1,
        1,
        _FIRST_ATTR_OFFSET,
        0x0001,
        bytes_in_use,
        record_size,
        0,
        0,
        0,
        0,
    )
    buf = bytearray(record_size)
    buf[0 : len(header)] = header
    buf[_FIRST_ATTR_OFFSET : _FIRST_ATTR_OFFSET + len(attrs)] = attrs
    return _stamp_fixups(bytes(buf))


def _make_single_extent_volume(record_count, lcn=8, bytes_per_cluster=_BYTES_PER_CLUSTER):
    """One extent covering records [0, record_count) at `lcn`. Record 0 is
    the real $MFT record built above; records 1..record_count-1 are filled
    with a byte equal to their own record number (mod 256), so content
    reads can be checked, not just lengths."""
    records_per_cluster = bytes_per_cluster // _RECORD_SIZE
    assert (
        record_count % records_per_cluster == 0
    ), "record_count must be a whole number of clusters"
    length_clusters = record_count // records_per_cluster
    record0 = _build_record0([(length_clusters, lcn)], record_size=_RECORD_SIZE)
    other_records = b"".join(bytes([i % 256]) * _RECORD_SIZE for i in range(1, record_count))
    mft_bytes = record0 + other_records

    start_offset = lcn * bytes_per_cluster
    volume = bytearray(start_offset + len(mft_bytes))
    volume[start_offset : start_offset + len(mft_bytes)] = mft_bytes
    return bytes(volume)


def _make_two_extent_volume(count1, lcn1, count2, lcn2, bytes_per_cluster=_BYTES_PER_CLUSTER):
    """Extent 1: records [0, count1) at lcn1 (record 0 -- the real $MFT
    record -- lives here). Extent 2: records [count1, count1+count2) at
    lcn2, physically NOT adjacent to extent 1 -- there's an unreadable gap
    of zero bytes in between, so a regression back to the old
    single-contiguous-span assumption would read wrong/garbage data for
    every record in extent 2, not just land slightly off."""
    records_per_cluster = bytes_per_cluster // _RECORD_SIZE
    assert (
        count1 % records_per_cluster == 0 and count2 % records_per_cluster == 0
    ), "count1/count2 must each be a whole number of clusters"
    record0 = _build_record0(
        [(count1 // records_per_cluster, lcn1), (count2 // records_per_cluster, lcn2)],
        record_size=_RECORD_SIZE,
    )

    offset1 = lcn1 * bytes_per_cluster
    offset2 = lcn2 * bytes_per_cluster
    end1 = offset1 + count1 * _RECORD_SIZE
    end2 = offset2 + count2 * _RECORD_SIZE
    volume = bytearray(max(end1, end2))

    volume[offset1 : offset1 + _RECORD_SIZE] = record0
    for i in range(1, count1):
        off = offset1 + i * _RECORD_SIZE
        volume[off : off + _RECORD_SIZE] = bytes([i % 256]) * _RECORD_SIZE

    for i in range(count2):
        record_number = count1 + i
        off = offset2 + i * _RECORD_SIZE
        volume[off : off + _RECORD_SIZE] = bytes([record_number % 256]) * _RECORD_SIZE

    return bytes(volume)


# -- faked ctypes.windll.kernel32 -------------------------------------------- #


class _FakeCreateFileW:
    def __init__(self, fails=False):
        self.fails = fails
        self.restype = None
        self.last_access = None

    def __call__(self, path, access, share, security, disposition, flags, template):
        self.last_access = access
        if self.fails:
            return mft_volume._INVALID_HANDLE_VALUE
        return 424242


class _FakeDeviceIoControl:
    def __init__(self, mft_start_lcn, bytes_per_cluster, record_size, fails=False, volume_serial=0):
        self.mft_start_lcn = mft_start_lcn
        self.bytes_per_cluster = bytes_per_cluster
        self.record_size = record_size
        self.fails = fails
        self.volume_serial = volume_serial
        self.calls = 0

    def __call__(self, handle, code, in_buf, in_size, out_ref, out_size, bytes_ret_ref, overlapped):
        self.calls += 1
        if self.fails:
            return 0
        info = ctypes.cast(out_ref, ctypes.POINTER(mft_volume._NTFS_VOLUME_DATA_BUFFER)).contents
        info.VolumeSerialNumber = self.volume_serial
        info.BytesPerSector = 512
        info.BytesPerCluster = self.bytes_per_cluster
        info.BytesPerFileRecordSegment = self.record_size
        info.MftValidDataLength = 0  # unused by the new extent-based logic
        info.MftStartLcn = self.mft_start_lcn
        return 1


class _FakeSetFilePointerEx:
    def __init__(self):
        self.last_offset = None
        self.calls = 0

    def __call__(self, handle, distance, new_position_ref, method):
        self.calls += 1
        self.last_offset = distance.value
        return 1


class _FakeReadFile:
    def __init__(self, volume_bytes, pointer, fails=False, short_read=False):
        self.volume_bytes = volume_bytes
        self.pointer = pointer
        self.fails = fails
        self.short_read = short_read
        self.calls = []  # (offset, length)

    def __call__(self, handle, buffer, length, bytes_read_ref, overlapped):
        if self.fails:
            return 0
        offset = self.pointer.last_offset
        self.calls.append((offset, length))
        available = self.volume_bytes[offset : offset + length]
        actual_length = length - 1 if self.short_read else length
        data = available[:actual_length].ljust(length, b"\x00")
        buffer.raw = data
        ctypes.cast(bytes_read_ref, ctypes.POINTER(ctypes.c_uint32)).contents.value = actual_length
        return 1


class _FakeCloseHandle:
    def __init__(self):
        self.calls = 0

    def __call__(self, handle):
        self.calls += 1
        return 1


class _FakeKernel32:
    def __init__(
        self,
        volume_bytes,
        *,
        mft_start_lcn,
        bytes_per_cluster=_BYTES_PER_CLUSTER,
        record_size=_RECORD_SIZE,
        create_file_fails=False,
        device_io_control_fails=False,
        read_file_fails=False,
        short_read=False,
        volume_serial=0,
    ):
        self.CreateFileW = _FakeCreateFileW(fails=create_file_fails)
        self.DeviceIoControl = _FakeDeviceIoControl(
            mft_start_lcn,
            bytes_per_cluster,
            record_size,
            fails=device_io_control_fails,
            volume_serial=volume_serial,
        )
        self.SetFilePointerEx = _FakeSetFilePointerEx()
        self.ReadFile = _FakeReadFile(
            volume_bytes,
            self.SetFilePointerEx,
            fails=read_file_fails,
            short_read=short_read,
        )
        self.CloseHandle = _FakeCloseHandle()


class _FakeWinDLL:
    def __init__(self, kernel32):
        self.kernel32 = kernel32


def _patch(monkeypatch, kernel32):
    monkeypatch.setattr(ctypes, "windll", _FakeWinDLL(kernel32), raising=False)


# -- tests -------------------------------------------------------------------- #


def test_opens_volume_and_reports_record_count_from_decoded_extents(monkeypatch):
    volume_bytes = _make_single_extent_volume(record_count=10, lcn=8)
    kernel32 = _FakeKernel32(volume_bytes, mft_start_lcn=8)
    _patch(monkeypatch, kernel32)

    source = RecordSource("C:\\")

    assert source.record_count == 10


def test_opens_read_only(monkeypatch):
    # Deliberately read-only -- confirmed on a real machine that adding
    # GENERIC_WRITE (for storage_scanner.usn_journal's FSCTL_CREATE_USN_
    # JOURNAL, which shares this handle via raw_handle) gets the whole
    # volume-open blocked by FortiClient (this user's AV/EDR), breaking
    # every Turbo Scan, not just journal creation. usn_journal.py's own
    # create_journal()/ensure_journal() are designed to work against a
    # read-only handle instead -- see their docstrings.
    volume_bytes = _make_single_extent_volume(record_count=10, lcn=8)
    kernel32 = _FakeKernel32(volume_bytes, mft_start_lcn=8)
    _patch(monkeypatch, kernel32)

    RecordSource("C:\\")

    assert kernel32.CreateFileW.last_access == mft_volume._GENERIC_READ


def test_exposes_volume_serial_record_size_and_raw_handle(monkeypatch):
    volume_bytes = _make_single_extent_volume(record_count=10, lcn=8)
    kernel32 = _FakeKernel32(volume_bytes, mft_start_lcn=8, volume_serial=0xABCDEF)
    _patch(monkeypatch, kernel32)

    source = RecordSource("C:\\")

    assert source.volume_serial == 0xABCDEF
    assert source.record_size == _RECORD_SIZE
    assert source.raw_handle == 424242  # the fake handle _FakeCreateFileW returns


def test_sequential_reads_within_one_chunk_issue_a_single_syscall(monkeypatch):
    volume_bytes = _make_single_extent_volume(record_count=10, lcn=8)
    kernel32 = _FakeKernel32(volume_bytes, mft_start_lcn=8)
    _patch(monkeypatch, kernel32)

    source = RecordSource("C:\\")
    source.record_at(1)
    source.record_at(2)
    source.record_at(9)

    # +1 for the bootstrap read of record #0 during __init__.
    assert len(kernel32.ReadFile.calls) == 2


def test_record_at_returns_the_right_record_content(monkeypatch):
    volume_bytes = _make_single_extent_volume(record_count=5, lcn=8)
    kernel32 = _FakeKernel32(volume_bytes, mft_start_lcn=8)
    _patch(monkeypatch, kernel32)

    source = RecordSource("C:\\")
    record = source.record_at(3)

    assert len(record) == _RECORD_SIZE
    assert record == bytes([3]) * _RECORD_SIZE


def test_reading_a_record_in_a_different_chunk_issues_a_new_read(monkeypatch):
    monkeypatch.setattr(mft_volume, "_READ_CHUNK_BYTES", 2 * _RECORD_SIZE)
    volume_bytes = _make_single_extent_volume(record_count=6, lcn=8)
    kernel32 = _FakeKernel32(volume_bytes, mft_start_lcn=8)
    _patch(monkeypatch, kernel32)

    source = RecordSource("C:\\")
    reads_after_init = len(kernel32.ReadFile.calls)

    source.record_at(1)  # loads a 2-record chunk
    source.record_at(2)  # loads the next chunk (crosses the boundary)
    assert len(kernel32.ReadFile.calls) == reads_after_init + 2

    record = source.record_at(5)  # a later chunk still
    assert len(kernel32.ReadFile.calls) == reads_after_init + 3
    assert record == bytes([5]) * _RECORD_SIZE


def test_out_of_order_lookup_does_not_evict_the_sequential_walk_chunk(monkeypatch):
    # The real bug this covers: mft_parser's out-of-order $ATTRIBUTE_LIST
    # extension-record lookups, interleaved with the main sequential walk,
    # used to evict record_at's one shared chunk -- so resuming the walk
    # right afterward always needed a fresh reload too, turning one
    # out-of-order lookup into (at least) two full chunk reads. Confirmed
    # on a real C:\Windows: 95,598 chunk loads for what a clean sequential
    # pass over ~1.15M records needs only ~282 of (93.8% of total scan
    # time). The fix gives out-of-order lookups their own aux chunk slot.
    monkeypatch.setattr(mft_volume, "_READ_CHUNK_BYTES", 2 * _RECORD_SIZE)
    volume_bytes = _make_single_extent_volume(record_count=6, lcn=8)
    kernel32 = _FakeKernel32(volume_bytes, mft_start_lcn=8)
    _patch(monkeypatch, kernel32)

    source = RecordSource("C:\\")
    reads_after_init = len(kernel32.ReadFile.calls)

    source.record_at(0)  # loads the primary chunk covering [0, 2)
    source.record_at(1)  # still within it -- no new read
    assert len(kernel32.ReadFile.calls) == reads_after_init + 1

    source.record_at(5)  # out-of-order -- must not evict the primary chunk
    assert len(kernel32.ReadFile.calls) == reads_after_init + 2

    record = source.record_at(1)  # resuming the walk: still a hit on primary
    assert len(kernel32.ReadFile.calls) == reads_after_init + 2
    assert record == bytes([1]) * _RECORD_SIZE


def test_walk_advancing_past_an_out_of_order_detour_does_not_reload_primary(monkeypatch):
    # The real interleaving pattern mft_parser produces: record_at(n), then
    # some number of out-of-order $ATTRIBUTE_LIST lookups for record n, then
    # record_at(n + 1) to continue the walk -- never a revisit of n itself
    # (unlike the test above, which only proves aux can't evict primary).
    # A first attempt at this fix decided "is this call sequential" by
    # checking whether aux already held the record, which happened to make
    # every test above pass while still causing catastrophic thrashing on
    # a real volume (88,481 aux loads, 94.3% of scan time, no better than
    # no fix at all) -- because an out-of-order detour landing in the
    # region the walk was about to reach next let aux quietly satisfy the
    # walk's next step, so the walk's own primary chunk stopped advancing
    # for good. This is the case that regression actually needs: the walk
    # continuing to a genuinely new record right after a detour.
    monkeypatch.setattr(mft_volume, "_READ_CHUNK_BYTES", 2 * _RECORD_SIZE)
    volume_bytes = _make_single_extent_volume(record_count=6, lcn=8)
    kernel32 = _FakeKernel32(volume_bytes, mft_start_lcn=8)
    _patch(monkeypatch, kernel32)

    source = RecordSource("C:\\")
    reads_after_init = len(kernel32.ReadFile.calls)

    source.record_at(0)  # walk: loads primary [0, 2)
    assert len(kernel32.ReadFile.calls) == reads_after_init + 1

    source.record_at(5)  # detour: loads aux, must not touch primary
    assert len(kernel32.ReadFile.calls) == reads_after_init + 2

    record = source.record_at(1)  # walk continues to record 1 -- still in primary
    assert len(kernel32.ReadFile.calls) == reads_after_init + 2
    assert record == bytes([1]) * _RECORD_SIZE


def test_aux_chunk_uses_its_own_smaller_size_independent_of_the_primary_chunk(monkeypatch):
    # Measured on a real, heavily hard-linked C:\Windows: out-of-order
    # $ATTRIBUTE_LIST extension-record lookups reused an already-loaded aux
    # chunk only ~5x on average before needing a different one -- so giving
    # aux the same 4MB primary chunk size (the first version of this fix)
    # read ~4096x more data than needed per lookup (161.3s, 89.4% of a real
    # scan) for locality that thin. This proves the two sizes are actually
    # independent, not just incidentally matching in the other tests here
    # (which never separately monkeypatch _AUX_READ_CHUNK_BYTES).
    monkeypatch.setattr(mft_volume, "_READ_CHUNK_BYTES", 4 * _RECORD_SIZE)
    monkeypatch.setattr(mft_volume, "_AUX_READ_CHUNK_BYTES", 2 * _RECORD_SIZE)
    volume_bytes = _make_single_extent_volume(record_count=10, lcn=8)
    kernel32 = _FakeKernel32(volume_bytes, mft_start_lcn=8)
    _patch(monkeypatch, kernel32)

    source = RecordSource("C:\\")
    assert source._chunk_records == 4
    assert source._aux_chunk_records == 2

    reads_after_init = len(kernel32.ReadFile.calls)
    source.record_at(0)  # primary load covers [0, 4)
    assert len(kernel32.ReadFile.calls) == reads_after_init + 1

    record = source.record_at(7)  # out-of-order -- aux's own small chunk [6, 8)
    assert len(kernel32.ReadFile.calls) == reads_after_init + 2
    assert record == bytes([7]) * _RECORD_SIZE

    record = source.record_at(9)  # outside that aux window -- a new, separate aux load
    assert len(kernel32.ReadFile.calls) == reads_after_init + 3
    assert record == bytes([9]) * _RECORD_SIZE


def test_record_at_out_of_range_raises_index_error(monkeypatch):
    volume_bytes = _make_single_extent_volume(record_count=3, lcn=8)
    kernel32 = _FakeKernel32(volume_bytes, mft_start_lcn=8)
    _patch(monkeypatch, kernel32)

    source = RecordSource("C:\\")
    with pytest.raises(IndexError):
        source.record_at(3)
    with pytest.raises(IndexError):
        source.record_at(-1)


def test_create_file_failure_raises_mft_volume_error(monkeypatch):
    volume_bytes = _make_single_extent_volume(record_count=3, lcn=8)
    kernel32 = _FakeKernel32(volume_bytes, mft_start_lcn=8, create_file_fails=True)
    _patch(monkeypatch, kernel32)

    with pytest.raises(MftVolumeError):
        RecordSource("C:\\")


def test_device_io_control_failure_raises_and_closes_the_handle(monkeypatch):
    volume_bytes = _make_single_extent_volume(record_count=3, lcn=8)
    kernel32 = _FakeKernel32(volume_bytes, mft_start_lcn=8, device_io_control_fails=True)
    _patch(monkeypatch, kernel32)

    with pytest.raises(MftVolumeError):
        RecordSource("C:\\")

    assert kernel32.CloseHandle.calls == 1


def test_unreadable_record0_raises_and_closes_the_handle(monkeypatch):
    # record #0 can be "read" fine (ReadFile succeeds) but doesn't decode
    # to a usable $DATA run list -- e.g. corrupt/torn on a damaged volume.
    volume_bytes = bytearray(_make_single_extent_volume(record_count=3, lcn=8))
    volume_bytes[8 * _BYTES_PER_CLUSTER : 8 * _BYTES_PER_CLUSTER + 4] = b"BAAD"
    kernel32 = _FakeKernel32(bytes(volume_bytes), mft_start_lcn=8)
    _patch(monkeypatch, kernel32)

    with pytest.raises(MftVolumeError):
        RecordSource("C:\\")

    assert kernel32.CloseHandle.calls == 1


def test_read_file_failure_raises_mft_volume_error(monkeypatch):
    volume_bytes = _make_single_extent_volume(record_count=3, lcn=8)
    kernel32 = _FakeKernel32(volume_bytes, mft_start_lcn=8, read_file_fails=True)
    _patch(monkeypatch, kernel32)

    with pytest.raises(MftVolumeError):
        RecordSource("C:\\")


def test_short_read_raises_mft_volume_error_rather_than_silently_truncating(monkeypatch):
    volume_bytes = _make_single_extent_volume(record_count=3, lcn=8)
    kernel32 = _FakeKernel32(volume_bytes, mft_start_lcn=8, short_read=True)
    _patch(monkeypatch, kernel32)

    with pytest.raises(MftVolumeError):
        RecordSource("C:\\")


def test_close_calls_close_handle(monkeypatch):
    volume_bytes = _make_single_extent_volume(record_count=3, lcn=8)
    kernel32 = _FakeKernel32(volume_bytes, mft_start_lcn=8)
    _patch(monkeypatch, kernel32)

    source = RecordSource("C:\\")
    source.close()

    assert kernel32.CloseHandle.calls == 1


def test_context_manager_closes_on_exit(monkeypatch):
    volume_bytes = _make_single_extent_volume(record_count=3, lcn=8)
    kernel32 = _FakeKernel32(volume_bytes, mft_start_lcn=8)
    _patch(monkeypatch, kernel32)

    with RecordSource("C:\\") as source:
        source.record_at(1)

    assert kernel32.CloseHandle.calls == 1


# -- multi-extent: the actual regression test for the real bug -------------- #


def test_fragmented_mft_with_two_non_adjacent_extents_reads_correctly(monkeypatch):
    # Extent 1: records [0, 5) at lcn=8. Extent 2: records [5, 10) at
    # lcn=1000 -- far away, with a large gap of zero bytes in between that
    # a single-contiguous-span assumption would have read as extent 1's
    # "continuation" instead, producing wrong content or an IndexError.
    volume_bytes = _make_two_extent_volume(count1=5, lcn1=8, count2=5, lcn2=1000)
    kernel32 = _FakeKernel32(volume_bytes, mft_start_lcn=8)
    _patch(monkeypatch, kernel32)

    source = RecordSource("C:\\")

    assert source.record_count == 10
    assert source.record_at(3) == bytes([3]) * _RECORD_SIZE
    assert source.record_at(5) == bytes([5]) * _RECORD_SIZE  # first record of extent 2
    assert source.record_at(9) == bytes([9]) * _RECORD_SIZE  # last record of extent 2


def test_chunk_reads_never_cross_an_extent_boundary(monkeypatch):
    # A large chunk size that would easily span both extents if the code
    # didn't clamp to one extent per read -- if it ever did span them, the
    # single ReadFile call's offset/length would straddle the gap and pull
    # in garbage for the second extent's records instead of a fresh,
    # correctly-addressed read.
    monkeypatch.setattr(mft_volume, "_READ_CHUNK_BYTES", 100 * _RECORD_SIZE)
    volume_bytes = _make_two_extent_volume(count1=5, lcn1=8, count2=5, lcn2=1000)
    kernel32 = _FakeKernel32(volume_bytes, mft_start_lcn=8)
    _patch(monkeypatch, kernel32)

    source = RecordSource("C:\\")
    reads_after_init = len(kernel32.ReadFile.calls)

    assert source.record_at(1) == bytes([1]) * _RECORD_SIZE
    assert source.record_at(6) == bytes([6]) * _RECORD_SIZE
    # One read for each extent's chunk -- proves it didn't try (and fail)
    # to satisfy both records from a single oversized read.
    assert len(kernel32.ReadFile.calls) == reads_after_init + 2


# -- read_clusters (for non-resident $ATTRIBUTE_LIST reassembly) ------------ #


def test_read_clusters_reads_the_right_offset_and_length(monkeypatch):
    volume_bytes = _make_single_extent_volume(record_count=5, lcn=8)
    kernel32 = _FakeKernel32(volume_bytes, mft_start_lcn=8)
    _patch(monkeypatch, kernel32)

    source = RecordSource("C:\\")
    reads_after_init = len(kernel32.ReadFile.calls)

    data = source.read_clusters(lcn=8, cluster_count=2)

    assert len(kernel32.ReadFile.calls) == reads_after_init + 1
    offset, length = kernel32.ReadFile.calls[-1]
    assert offset == 8 * _BYTES_PER_CLUSTER
    assert length == 2 * _BYTES_PER_CLUSTER
    assert (
        data
        == volume_bytes[8 * _BYTES_PER_CLUSTER : 8 * _BYTES_PER_CLUSTER + 2 * _BYTES_PER_CLUSTER]
    )


def test_records_per_cluster_greater_than_one_is_handled(monkeypatch):
    # A realistic 4096-byte cluster with 1024-byte records: 4 records per
    # cluster, so a run of "3 clusters" must resolve to 12 records, not 3.
    bytes_per_cluster = 4096
    records_per_cluster = bytes_per_cluster // _RECORD_SIZE
    record_count = 3 * records_per_cluster
    lcn = 2
    volume_bytes = _make_single_extent_volume(
        record_count=record_count,
        lcn=lcn,
        bytes_per_cluster=bytes_per_cluster,
    )
    kernel32 = _FakeKernel32(
        volume_bytes,
        mft_start_lcn=lcn,
        bytes_per_cluster=bytes_per_cluster,
    )
    _patch(monkeypatch, kernel32)

    source = RecordSource("C:\\")

    assert source.record_count == record_count
    assert source.record_at(record_count - 1) == bytes([(record_count - 1) % 256]) * _RECORD_SIZE
