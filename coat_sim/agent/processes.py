"""Own worker process trees, including MCP children, on Windows and POSIX."""
from __future__ import annotations
import os
import signal
import subprocess
import sys

class ProcessTree:
    def __init__(self, process):
        self.process = process
        self._job = None
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes
                class Basic(ctypes.Structure):
                    _fields_ = [("PerProcessUserTimeLimit",ctypes.c_longlong),("PerJobUserTimeLimit",ctypes.c_longlong),("LimitFlags",wintypes.DWORD),("MinimumWorkingSetSize",ctypes.c_size_t),("MaximumWorkingSetSize",ctypes.c_size_t),("ActiveProcessLimit",wintypes.DWORD),("Affinity",ctypes.c_size_t),("PriorityClass",wintypes.DWORD),("SchedulingClass",wintypes.DWORD)]
                class IO(ctypes.Structure):
                    _fields_ = [(name,ctypes.c_ulonglong) for name in ("ReadOperationCount","WriteOperationCount","OtherOperationCount","ReadTransferCount","WriteTransferCount","OtherTransferCount")]
                class Extended(ctypes.Structure):
                    _fields_ = [("BasicLimitInformation",Basic),("IoInfo",IO),("ProcessMemoryLimit",ctypes.c_size_t),("JobMemoryLimit",ctypes.c_size_t),("PeakProcessMemoryUsed",ctypes.c_size_t),("PeakJobMemoryUsed",ctypes.c_size_t)]
                kernel = ctypes.windll.kernel32
                kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p,wintypes.LPCWSTR]
                kernel.CreateJobObjectW.restype = wintypes.HANDLE
                kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
                kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE,wintypes.HANDLE]
                kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                job = kernel.CreateJobObjectW(None,None)
                if job:
                    info = Extended()
                    info.BasicLimitInformation.LimitFlags = 0x2000
                    if kernel.SetInformationJobObject(job,9,ctypes.byref(info),ctypes.sizeof(info)) and kernel.AssignProcessToJobObject(job,wintypes.HANDLE(int(process._handle))):
                        self._job = job
                    else:
                        kernel.CloseHandle(job)
            except (OSError,AttributeError,TypeError):
                pass
    def close(self):
        if self._job is not None:
            import ctypes
            from ctypes import wintypes
            ctypes.windll.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            ctypes.windll.kernel32.CloseHandle(self._job)
            self._job = None
        elif self.process.poll() is None:
            if sys.platform == 'win32':
                try:
                    subprocess.run(['taskkill','/PID',str(self.process.pid),'/T','/F'], stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW,timeout=5)
                except (OSError,subprocess.TimeoutExpired):
                    self.process.kill()
            else:
                try:
                    os.killpg(self.process.pid,signal.SIGTERM)
                except ProcessLookupError:
                    pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
