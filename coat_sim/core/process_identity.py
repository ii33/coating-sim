"""Read process creation identity; access failures remain unknown."""
import os
import sys
from pathlib import Path

def _process_identity(pid: int) -> tuple[str, str | None]:
    """Return alive/dead/unknown and an OS process creation identity.

    Windows os.kill(pid, 0) can terminate a process; never use it here.
    Access failures remain unknown instead of interrupting another owner.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return "unknown", None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ("dead", None) if ctypes.get_last_error() in {87, 1168} else ("unknown", None)
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return "unknown", None
            if exit_code.value != 259:  # STILL_ACTIVE
                return "dead", None
            creation, exit_time, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
            if not kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time),
                                             ctypes.byref(kernel_time), ctypes.byref(user_time)):
                return "unknown", None
            stamp = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return "alive", f"windows:{stamp}"
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead", None
    except (PermissionError, OSError):
        return "unknown", None
    if sys.platform.startswith("linux"):
        try:
            # starttime is field 22; split after the command's final ')' first.
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] in {"Z", "X"}:
                return "dead", None
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            return "alive", f"linux:{boot_id}:{fields[19]}"
        except (OSError, ValueError, IndexError):
            return "unknown", None
    return "unknown", None
