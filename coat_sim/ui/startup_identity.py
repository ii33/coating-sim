"""Windows startup identity anchored to a retained Popen handle, never a PID alone."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from threading import RLock
from typing import Any, Iterator


@dataclass(frozen=True)
class _ProcessIdentity:
    pid: int
    created: int
    exited: int | None = None


class _WindowsProcesses:
    """Read-only Win32 handles, pipe peer identity and process-parent snapshots."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes: Any = ctypes
        self.types = wintypes
        self.kernel = self.ctypes.WinDLL("kernel32", use_last_error=True)

        class ProcessEntry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

        self.entry_type = ProcessEntry
        signatures = {
            "OpenProcess": ((wintypes.DWORD, wintypes.BOOL, wintypes.DWORD), wintypes.HANDLE),
            "CloseHandle": ((wintypes.HANDLE,), wintypes.BOOL),
            "GetProcessId": ((wintypes.HANDLE,), wintypes.DWORD),
            "GetProcessTimes": ((wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4,
                                wintypes.BOOL),
            "WaitForSingleObject": ((wintypes.HANDLE, wintypes.DWORD), wintypes.DWORD),
            "GetNamedPipeClientProcessId": ((wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)),
                                             wintypes.BOOL),
            "CreateToolhelp32Snapshot": ((wintypes.DWORD, wintypes.DWORD), wintypes.HANDLE),
            "Process32FirstW": ((wintypes.HANDLE, ctypes.POINTER(ProcessEntry)), wintypes.BOOL),
            "Process32NextW": ((wintypes.HANDLE, ctypes.POINTER(ProcessEntry)), wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result

    def open(self, pid: int) -> int:
        # Query creation identity and wait state only; no signalling rights.
        handle = self.kernel.OpenProcess(0x1000 | 0x100000, False, pid)
        if not handle:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        return handle

    def close(self, handle: int) -> None:
        self.kernel.CloseHandle(handle)

    def identity(self, handle: int) -> _ProcessIdentity:
        pid = self.kernel.GetProcessId(handle)
        creation, exit_time, kernel_time, user_time = (self.types.FILETIME() for _ in range(4))
        state = self.kernel.WaitForSingleObject(handle, 0)
        if (not pid or state not in (0, 258)
                or not self.kernel.GetProcessTimes(handle, self.ctypes.byref(creation),
                    self.ctypes.byref(exit_time), self.ctypes.byref(kernel_time),
                    self.ctypes.byref(user_time))):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        created = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        exited = ((exit_time.dwHighDateTime << 32) | exit_time.dwLowDateTime) if state == 0 else None
        if not created or exited is not None and exited < created:
            raise ValueError("startup process creation identity unavailable")
        return _ProcessIdentity(pid, created, exited)

    def peer_pid(self, connection: Any) -> int:
        pid = self.types.ULONG()
        if not self.kernel.GetNamedPipeClientProcessId(connection.fileno(), self.ctypes.byref(pid)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        if not pid.value:
            raise ValueError("startup pipe peer identity unavailable")
        return pid.value

    def parents(self) -> dict[int, int]:
        handle = self.kernel.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        if handle == self.ctypes.c_void_p(-1).value:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        try:
            entry = self.entry_type()
            entry.dwSize = self.ctypes.sizeof(entry)
            if not self.kernel.Process32FirstW(handle, self.ctypes.byref(entry)):
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            result = {entry.th32ProcessID: entry.th32ParentProcessID}
            while self.kernel.Process32NextW(handle, self.ctypes.byref(entry)):
                result[entry.th32ProcessID] = entry.th32ParentProcessID
            if self.ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            return result
        finally:
            self.close(handle)


class WindowsStartupIdentity:
    """Authorize authenticated pipe senders from the exact launched process tree.

    A Windows venv redirector starts a second Python process. The pipe sender,
    claimed endpoint PID, parent chain and ordered process creation identities
    must all agree. Retained handles fence PID reuse, including explicit desktop
    host handoffs. Failure to inspect any required identity fails closed.
    """

    def __init__(self, process: Any) -> None:
        self._api = _WindowsProcesses()
        self._anchor = int(process._handle)  # Borrow the actual Popen handle, not OpenProcess(pid).
        if self._api.identity(self._anchor).pid != process.pid:
            raise ValueError("startup process handle identity mismatch")
        self._owned_handles: list[int] = []
        self._lock = RLock()
        self._closed = False
        self._handoff_seen = False

    @contextmanager
    def peer(self, connection: Any) -> Iterator[int]:
        # Capture/pin the OS peer before reading its event, while the pipe is connected.
        handle = self._api.open(self._api.peer_pid(connection))
        try:
            yield handle
        finally:
            self._api.close(handle)

    def _descendant(self, child_handle: int, ancestor_handle: int) -> bool:
        child, ancestor = self._api.identity(child_handle), self._api.identity(ancestor_handle)
        if child.exited is not None:
            return False
        parents = None
        seen: set[int] = set()
        with ExitStack() as stack:
            for _ in range(8):
                if child.pid == ancestor.pid:
                    return child.created == ancestor.created
                if child.pid in seen:
                    return False
                seen.add(child.pid)
                if parents is None:
                    parents = self._api.parents()
                parent_pid = parents.get(child.pid)
                if not parent_pid or parent_pid in seen:
                    return False
                if parent_pid == ancestor.pid:
                    parent = ancestor
                else:
                    handle = self._api.open(parent_pid)
                    stack.callback(self._api.close, handle)
                    parent = self._api.identity(handle)
                # A reused parent PID can name a newer, unrelated process. It
                # cannot have created a child before birth or after its exit.
                if (child.created < parent.created
                        or parent.exited is not None and child.created > parent.exited):
                    return False
                child = parent
        return False

    def authorize(self, event: dict[str, Any], peer_handle: int) -> int:
        with self._lock:
            if self._closed:
                raise ValueError("startup identity verifier is closed")
            peer = self._api.identity(peer_handle)
            if not self._descendant(peer_handle, self._anchor):
                raise ValueError("startup process ancestry identity mismatch")
            status = event["status"]
            if status == "ready" and event["endpoint"]["pid"] != peer.pid:
                raise ValueError("startup pipe sender identity mismatch")
            if status == "handoff":
                if self._handoff_seen:
                    raise ValueError("duplicate host handoff")
                if event["process_pid"] != peer.pid or event["host_pid"] == peer.pid:
                    raise ValueError("bootstrap pipe sender identity mismatch")
                host = self._api.open(event["host_pid"])
                try:
                    if not self._descendant(host, peer_handle):
                        raise ValueError("desktop host ancestry identity mismatch")
                except BaseException:
                    self._api.close(host)
                    raise
                self._owned_handles.append(host)
                self._anchor = host
                self._handoff_seen = True
            return peer.pid

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for handle in self._owned_handles:
                self._api.close(handle)
            self._owned_handles.clear()
