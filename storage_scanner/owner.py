"""Who owns a file or folder: the main tree's Owner column.

An owner is a security-descriptor read per item -- 85 to 190 µs each on
Windows here (a OneDrive folder, then System32), so a 1M-file scan would
spend minutes on it. The scan never reads it: the main tree asks only for
the rows on screen (ui/owner_column.py), on OwnerLookup's worker thread,
and each account is named once (per SID or uid), not once per item.

- Windows: GetNamedSecurityInfoW(OWNER_SECURITY_INFORMATION), named by
  LookupAccountSidW as "DOMAIN\\user" ("NT SERVICE\\TrustedInstaller",
  "BUILTIN\\Administrators"). A junction or symlink gives its own owner,
  not its target's (measured), matching the scan, which never follows one.
- macOS/Linux: lstat()'s st_uid, named by pwd.getpwuid.

Whatever can't be read -- access denied, a file in use (pagefile.sys), gone
since the scan, an account Windows can't name (a deleted user's SID), a uid
with no passwd entry -- is "", a blank cell.
"""

import os
import queue
import sys
import threading

from storage_scanner.logging_setup import logger

if sys.platform == "win32":
    import ctypes
    import functools
    from ctypes import wintypes

    from storage_scanner.recycle_windows import extended_path

    _SE_FILE_OBJECT = 1
    _OWNER_SECURITY_INFORMATION = 1
    _ERROR_INSUFFICIENT_BUFFER = 122
    _NAME_CHARS = 256  # an account or domain name is far shorter

    _accounts: "dict[bytes, str]" = {}  # a SID's bytes -> its name, "" if unnamed

    @functools.lru_cache(maxsize=1)
    def _win32():
        """advapi32 and kernel32 with the prototypes used here: instances
        of their own, so they don't change the shared ctypes.windll ones
        other modules call."""
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        out_pointer = ctypes.POINTER(ctypes.c_void_p)
        advapi32.GetNamedSecurityInfoW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.c_int,  # SE_OBJECT_TYPE
            wintypes.DWORD,  # SECURITY_INFORMATION
            out_pointer,  # owner SID
            out_pointer,  # group SID
            out_pointer,  # DACL
            out_pointer,  # SACL
            out_pointer,  # the security descriptor, freed with LocalFree
        ]
        advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
        dword_pointer = ctypes.POINTER(wintypes.DWORD)
        advapi32.LookupAccountSidW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            wintypes.LPWSTR,
            dword_pointer,
            wintypes.LPWSTR,
            dword_pointer,
            ctypes.POINTER(ctypes.c_int),  # SID_NAME_USE
        ]
        advapi32.LookupAccountSidW.restype = wintypes.BOOL
        advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
        advapi32.GetLengthSid.restype = wintypes.DWORD
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        return advapi32, kernel32

    def _account_name(advapi32, sid):
        """The SID's "DOMAIN\\name" (just the name for one with no domain,
        like "Everyone"), "" when Windows can't name it."""
        name_chars = domain_chars = _NAME_CHARS
        for _attempt in range(2):  # the second with the sizes the first asked for
            name = ctypes.create_unicode_buffer(name_chars)
            domain = ctypes.create_unicode_buffer(domain_chars)
            name_size = wintypes.DWORD(name_chars)
            domain_size = wintypes.DWORD(domain_chars)
            use = ctypes.c_int()
            if advapi32.LookupAccountSidW(
                None,
                sid,
                name,
                ctypes.byref(name_size),
                domain,
                ctypes.byref(domain_size),
                ctypes.byref(use),
            ):
                return f"{domain.value}\\{name.value}" if domain.value else name.value
            if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER:
                break
            name_chars, domain_chars = name_size.value, domain_size.value
        return ""

    def owner_of(path):
        """The account that owns `path`, "" when it can't be read."""
        advapi32, kernel32 = _win32()
        sid = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        status = advapi32.GetNamedSecurityInfoW(
            extended_path(path),  # past MAX_PATH, and names ending in "." or " "
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION,
            ctypes.byref(sid),
            None,
            None,
            None,
            ctypes.byref(descriptor),
        )
        if status:
            return ""
        try:
            if not sid.value:  # a descriptor with no owner at all
                return ""
            key = ctypes.string_at(sid.value, advapi32.GetLengthSid(sid))
            name = _accounts.get(key)
            if name is None:
                name = _accounts[key] = _account_name(advapi32, sid)
            return name
        finally:
            kernel32.LocalFree(descriptor)

else:
    import pwd

    _users: "dict[int, str]" = {}  # uid -> user name, "" if it has none

    def owner_of(path):
        """The user that owns `path`, "" when it can't be read."""
        try:
            uid = os.lstat(path).st_uid
        except (OSError, ValueError):
            return ""
        name = _users.get(uid)
        if name is None:
            try:
                name = pwd.getpwuid(uid).pw_name
            except KeyError:
                name = ""
            _users[uid] = name
        return name


class OwnerLookup:
    """Looks owners up on a worker thread, a request (a list of paths) at a
    time, in the order they were asked for. The Tk thread hands paths to
    request() and collects (path, owner) pairs with results(); the worker
    never touches Tk. It exits once it runs out of requests, and the next
    request starts another, so an idle app holds no thread."""

    def __init__(self, lookup=owner_of):
        self._lookup = lookup
        self._requests = queue.SimpleQueue()  # (generation, [path, ...])
        self._results = queue.SimpleQueue()  # (generation, path, owner)
        self._lock = threading.Lock()
        self._working = False
        # Bumped by cancel(): requests and results from before it are dropped.
        self._generation = 0

    def request(self, paths):
        self._requests.put((self._generation, paths))
        with self._lock:
            if self._working:
                return
            self._working = True
        threading.Thread(target=self._work, name="owner-lookup", daemon=True).start()

    def results(self, limit):
        """Up to `limit` (path, owner) pairs looked up since the last call,
        leaving out any asked for before the last cancel()."""
        found = []
        while len(found) < limit:
            try:
                generation, path, owner = self._results.get_nowait()
            except queue.Empty:
                break
            if generation == self._generation:
                found.append((path, owner))
        return found

    def cancel(self):
        """Forget every request not looked up yet (a new scan replaces the
        tree that asked)."""
        self._generation += 1

    def _work(self):
        while True:
            try:
                generation, paths = self._requests.get_nowait()
            except queue.Empty:
                # Checked again under the lock: request() puts before it
                # looks at _working, so a request either is seen here or
                # starts a worker of its own.
                with self._lock:
                    if self._requests.empty():
                        self._working = False
                        return
                continue
            for path in paths:
                if generation != self._generation:
                    break
                try:
                    owner = self._lookup(path)
                except Exception:  # noqa: BLE001 - one unreadable row must not stop the rest
                    logger.debug("Owner lookup failed for %s", path, exc_info=True)
                    owner = ""
                self._results.put((generation, path, owner))
