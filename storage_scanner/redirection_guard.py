"""Keep an elevated Storage Scanner from following junctions a normal user made.

An elevated process (Turbo Scan's --mft-scan helper, or the app restarted
as administrator) writes its log, history and Turbo cache under the user's
%LOCALAPPDATA%. Anything running as that user, without admin rights, can
replace a folder there with a junction to, say, C:\\Windows\\System32, and
the elevated process would then create, append to, rename and delete files
with those fixed names in there.

Windows 11 22H2 and later can make a process refuse to follow junctions
created by non-administrators (RedirectionGuard).
guard_elevated_process turns that on as an elevated process starts, before
anything opens app data; Storage-Scanner.py calls it first thing, so this
module imports nothing from the app. Older Windows has no such switch.
Junctions Windows itself made (the profile's "My Documents" and the like)
are still followed; a path through a junction the user made fails to open
in an elevated scan instead of being followed.
"""

import ctypes
import sys

_PROCESS_REDIRECTION_TRUST_POLICY = 16  # PROCESS_MITIGATION_POLICY value
_ENFORCE_REDIRECTION_TRUST = 1  # PROCESS_MITIGATION_REDIRECTION_TRUST_POLICY bit 0

# Whether guard_elevated_process turned it on, for the log.
enforced = False


class _RedirectionTrustPolicy(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint32)]


def guard_elevated_process():
    """Turn RedirectionGuard on if this process runs elevated on Windows.
    Returns whether it's on."""
    global enforced
    if sys.platform != "win32":
        return False
    try:
        if not ctypes.windll.shell32.IsUserAnAdmin():  # type: ignore[attr-defined]
            return False  # it writes only where its own user could anyway
        policy = _RedirectionTrustPolicy(_ENFORCE_REDIRECTION_TRUST)
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        enforced = bool(
            kernel32.SetProcessMitigationPolicy(
                _PROCESS_REDIRECTION_TRUST_POLICY, ctypes.byref(policy), ctypes.sizeof(policy)
            )
        )
    except (AttributeError, OSError):
        enforced = False  # before Windows 8: no mitigation policies at all
    return enforced
