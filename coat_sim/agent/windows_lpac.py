"""Windows OS isolation for untrusted scripts; no unrestricted fallback.

The child has a fresh LPAC SID, only registryRead for OS loader startup,
no network, no GUI system
calls, exactly one process, and only the runtime/staging ACL grants below.
The host installs trusted runtime packages; scripts never invoke this module.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid


class IsolationUnavailable(RuntimeError):
    pass


@dataclass
class ProcessOutput:
    stdout: str
    stderr: str
    exit_code: int
    elapsed_s: float
    timed_out: bool = False
    output_limited: bool = False


def _require_windows():
    if os.name != "nt":
        raise IsolationUnavailable("脚本功能要求 Windows LPAC；此平台禁止普通进程降级执行")


SIZE_T = C.c_size_t
ULONG_PTR = C.c_size_t
HANDLE = W.HANDLE
PSID = C.c_void_p
LPVOID = C.c_void_p
RUNTIME_CAPABILITIES = ("registryRead",)


class SECURITY_ATTRIBUTES(C.Structure):
    _fields_ = [("nLength", W.DWORD), ("lpSecurityDescriptor", LPVOID), ("bInheritHandle", W.BOOL)]


class SID_AND_ATTRIBUTES(C.Structure):
    _fields_ = [("Sid", PSID), ("Attributes", W.DWORD)]


class SECURITY_CAPABILITIES(C.Structure):
    _fields_ = [("AppContainerSid", PSID), ("Capabilities", C.POINTER(SID_AND_ATTRIBUTES)),
                ("CapabilityCount", W.DWORD), ("Reserved", W.DWORD)]


class GENERIC_MAPPING(C.Structure):
    _fields_ = [(name, W.DWORD) for name in ("GenericRead", "GenericWrite", "GenericExecute", "GenericAll")]


class STARTUPINFO(C.Structure):
    _fields_ = [("cb", W.DWORD), ("lpReserved", W.LPWSTR), ("lpDesktop", W.LPWSTR),
                ("lpTitle", W.LPWSTR), ("dwX", W.DWORD), ("dwY", W.DWORD),
                ("dwXSize", W.DWORD), ("dwYSize", W.DWORD), ("dwXCountChars", W.DWORD),
                ("dwYCountChars", W.DWORD), ("dwFillAttribute", W.DWORD), ("dwFlags", W.DWORD),
                ("wShowWindow", W.WORD), ("cbReserved2", W.WORD), ("lpReserved2", C.POINTER(C.c_ubyte)),
                ("hStdInput", HANDLE), ("hStdOutput", HANDLE), ("hStdError", HANDLE)]


class STARTUPINFOEX(C.Structure):
    _fields_ = [("StartupInfo", STARTUPINFO), ("lpAttributeList", LPVOID)]


class PROCESS_INFORMATION(C.Structure):
    _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE), ("dwProcessId", W.DWORD), ("dwThreadId", W.DWORD)]


class IO_COUNTERS(C.Structure):
    _fields_ = [(name, C.c_ulonglong) for name in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOB_BASIC_LIMIT_INFORMATION(C.Structure):
    _fields_ = [("PerProcessUserTimeLimit", C.c_longlong), ("PerJobUserTimeLimit", C.c_longlong),
                ("LimitFlags", W.DWORD), ("MinimumWorkingSetSize", SIZE_T), ("MaximumWorkingSetSize", SIZE_T),
                ("ActiveProcessLimit", W.DWORD), ("Affinity", ULONG_PTR), ("PriorityClass", W.DWORD),
                ("SchedulingClass", W.DWORD)]


class JOB_EXTENDED_LIMIT_INFORMATION(C.Structure):
    _fields_ = [("BasicLimitInformation", JOB_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", SIZE_T), ("JobMemoryLimit", SIZE_T),
                ("PeakProcessMemoryUsed", SIZE_T), ("PeakJobMemoryUsed", SIZE_T)]


class TRUSTEE(C.Structure):
    _fields_ = [("pMultipleTrustee", LPVOID), ("MultipleTrusteeOperation", W.DWORD),
                ("TrusteeForm", W.DWORD), ("TrusteeType", W.DWORD), ("ptstrName", LPVOID)]


class EXPLICIT_ACCESS(C.Structure):
    _fields_ = [("grfAccessPermissions", W.DWORD), ("grfAccessMode", W.DWORD),
                ("grfInheritance", W.DWORD), ("Trustee", TRUSTEE)]


class _API:
    def __init__(self):
        _require_windows()
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.advapi = C.WinDLL("advapi32", use_last_error=True)
        self.userenv = C.WinDLL("userenv", use_last_error=True)
        self.base = C.WinDLL("KernelBase", use_last_error=True)
        signatures = [
            (self.kernel, "CreatePipe", W.BOOL, [C.POINTER(HANDLE), C.POINTER(HANDLE), C.POINTER(SECURITY_ATTRIBUTES), W.DWORD]),
            (self.kernel, "SetHandleInformation", W.BOOL, [HANDLE, W.DWORD, W.DWORD]),
            (self.kernel, "ReadFile", W.BOOL, [HANDLE, LPVOID, W.DWORD, C.POINTER(W.DWORD), LPVOID]),
            (self.kernel, "CreateFileW", HANDLE, [W.LPCWSTR, W.DWORD, W.DWORD, C.POINTER(SECURITY_ATTRIBUTES), W.DWORD, W.DWORD, HANDLE]),
            (self.kernel, "CloseHandle", W.BOOL, [HANDLE]),
            (self.kernel, "LocalFree", LPVOID, [LPVOID]),
            (self.kernel, "InitializeProcThreadAttributeList", W.BOOL, [LPVOID, W.DWORD, W.DWORD, C.POINTER(SIZE_T)]),
            (self.kernel, "UpdateProcThreadAttribute", W.BOOL, [LPVOID, W.DWORD, SIZE_T, LPVOID, SIZE_T, LPVOID, C.POINTER(SIZE_T)]),
            (self.kernel, "DeleteProcThreadAttributeList", None, [LPVOID]),
            (self.kernel, "CreateProcessW", W.BOOL, [W.LPCWSTR, W.LPWSTR, LPVOID, LPVOID, W.BOOL, W.DWORD, LPVOID, W.LPCWSTR, C.POINTER(STARTUPINFOEX), C.POINTER(PROCESS_INFORMATION)]),
            (self.kernel, "CreateJobObjectW", HANDLE, [LPVOID, W.LPCWSTR]),
            (self.kernel, "SetInformationJobObject", W.BOOL, [HANDLE, C.c_int, LPVOID, W.DWORD]),
            (self.kernel, "AssignProcessToJobObject", W.BOOL, [HANDLE, HANDLE]),
            (self.kernel, "TerminateJobObject", W.BOOL, [HANDLE, W.UINT]),
            (self.kernel, "TerminateProcess", W.BOOL, [HANDLE, W.UINT]),
            (self.kernel, "ResumeThread", W.DWORD, [HANDLE]),
            (self.kernel, "WaitForSingleObject", W.DWORD, [HANDLE, W.DWORD]),
            (self.kernel, "GetExitCodeProcess", W.BOOL, [HANDLE, C.POINTER(W.DWORD)]),
            (self.advapi, "FreeSid", LPVOID, [PSID]),
            (self.advapi, "GetLengthSid", W.DWORD, [PSID]),
            (self.advapi, "GetNamedSecurityInfoW", W.DWORD, [W.LPCWSTR, W.DWORD, W.DWORD, LPVOID, LPVOID, C.POINTER(LPVOID), LPVOID, C.POINTER(LPVOID)]),
            (self.advapi, "SetEntriesInAclW", W.DWORD, [W.ULONG, C.POINTER(EXPLICIT_ACCESS), LPVOID, C.POINTER(LPVOID)]),
            (self.advapi, "SetNamedSecurityInfoW", W.DWORD, [W.LPWSTR, W.DWORD, W.DWORD, PSID, PSID, LPVOID, LPVOID]),
            (self.advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW", W.BOOL, [W.LPCWSTR, W.DWORD, C.POINTER(LPVOID), C.POINTER(W.DWORD)]),
            (self.advapi, "GetSecurityDescriptorSacl", W.BOOL, [LPVOID, C.POINTER(W.BOOL), C.POINTER(LPVOID), C.POINTER(W.BOOL)]),
            (self.advapi, "OpenProcessToken", W.BOOL, [HANDLE, W.DWORD, C.POINTER(HANDLE)]),
            (self.advapi, "DuplicateToken", W.BOOL, [HANDLE, C.c_int, C.POINTER(HANDLE)]),
            (self.advapi, "AccessCheck", W.BOOL,
             [LPVOID, HANDLE, W.DWORD, C.POINTER(GENERIC_MAPPING), LPVOID,
              C.POINTER(W.DWORD), C.POINTER(W.DWORD), C.POINTER(W.BOOL)]),
            (self.advapi, "GetTokenInformation", W.BOOL, [HANDLE, C.c_int, LPVOID, W.DWORD, C.POINTER(W.DWORD)]),
            (self.userenv, "DeriveAppContainerSidFromAppContainerName", C.c_long, [W.LPCWSTR, C.POINTER(PSID)]),
            (self.userenv, "CreateAppContainerProfile", C.c_long,
             [W.LPCWSTR, W.LPCWSTR, W.LPCWSTR, C.POINTER(SID_AND_ATTRIBUTES), W.DWORD, C.POINTER(PSID)]),
            (self.userenv, "DeleteAppContainerProfile", C.c_long, [W.LPCWSTR]),
            (self.base, "DeriveCapabilitySidsFromName", W.BOOL,
             [W.LPCWSTR, C.POINTER(C.POINTER(PSID)), C.POINTER(W.DWORD),
              C.POINTER(C.POINTER(PSID)), C.POINTER(W.DWORD)]),
        ]
        for dll, name, result, args in signatures:
            try:
                function = getattr(dll, name)
            except AttributeError as exc:
                raise IsolationUnavailable(f"Windows 缺少必要隔离接口：{name}") from exc
            function.restype = result
            function.argtypes = args

    @staticmethod
    def check(ok, action):
        if not ok:
            error = C.get_last_error()
            raise IsolationUnavailable(f"{action}失败（Windows {error}: {C.FormatError(error).strip()}）；禁止降级执行")

    @staticmethod
    def check_code(code, action):
        if code:
            raise IsolationUnavailable(f"{action}失败（Windows {code}）；禁止降级执行")


def _runtime_capabilities(api: _API):
    """Fixed host policy: only HKLM read capability needed by the OS loader.

    No script/provider argument can add capabilities. Copy the resulting SID
    bytes into host-owned buffers and release every native SID/array. Python
    keeps these buffers alive for profile/process construction and execution.
    Microsoft: /windows/win32/secauthz/implementing-an-appcontainer and
    /windows/win32/api/securitybaseapi/nf-securitybaseapi-derivecapabilitysidsfromname.
    """
    buffers = []
    for name in RUNTIME_CAPABILITIES:
        groups, capabilities = C.POINTER(PSID)(), C.POINTER(PSID)()
        group_count, capability_count = W.DWORD(), W.DWORD()
        try:
            api.check(api.base.DeriveCapabilitySidsFromName(
                name, C.byref(groups), C.byref(group_count),
                C.byref(capabilities), C.byref(capability_count)), "解析固定运行时能力")
            if not capability_count.value:
                raise IsolationUnavailable("系统未返回必要运行时能力 SID；禁止降级执行")
            for index in range(capability_count.value):
                size = api.advapi.GetLengthSid(capabilities[index])
                if not 8 <= size <= 68:
                    raise IsolationUnavailable("运行时能力 SID 长度无效；禁止降级执行")
                buffers.append(C.create_string_buffer(C.string_at(capabilities[index], size), size))
        finally:
            for array, count in ((groups, group_count.value), (capabilities, capability_count.value)):
                if array:
                    for index in range(count):
                        if array[index]:
                            api.kernel.LocalFree(array[index])
                    api.kernel.LocalFree(C.cast(array, LPVOID))
    array = (SID_AND_ATTRIBUTES * len(buffers))(
        *(SID_AND_ATTRIBUTES(C.addressof(buffer), 4) for buffer in buffers))
    return array, buffers


def _grant(api: _API, path: Path, sid: PSID, access: int, *, revoke: bool = False):
    """Add/remove only this fresh SID; preserve user/system ACLs and inheritance."""
    descriptor, old_acl, new_acl = LPVOID(), LPVOID(), LPVOID()
    try:
        api.check_code(api.advapi.GetNamedSecurityInfoW(str(path), 1, 4, None, None,
                                                       C.byref(old_acl), None, C.byref(descriptor)), "读取隔离目录 ACL")
        entry = EXPLICIT_ACCESS(access, 4 if revoke else 1, 3,
                                TRUSTEE(None, 0, 0, 0, sid.value))
        api.check_code(api.advapi.SetEntriesInAclW(1, C.byref(entry), old_acl, C.byref(new_acl)), "构造隔离目录 ACL")
        # Inheritable grants propagate to existing children of this private runtime.
        api.check_code(api.advapi.SetNamedSecurityInfoW(str(path), 1, 4, None, None, new_acl, None), "设置隔离目录 ACL")
    finally:
        if descriptor:
            api.kernel.LocalFree(descriptor)
        if new_acl:
            api.kernel.LocalFree(new_acl)


def _low_integrity(api: _API, path: Path):
    descriptor, acl = LPVOID(), LPVOID()
    present, defaulted = W.BOOL(), W.BOOL()
    try:
        api.check(api.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            "S:(ML;OICI;NW;;;LW)", 1, C.byref(descriptor), None), "构造工作目录完整性标签")
        api.check(api.advapi.GetSecurityDescriptorSacl(
            descriptor, C.byref(present), C.byref(acl), C.byref(defaulted)), "读取工作目录完整性标签")
        api.check_code(api.advapi.SetNamedSecurityInfoW(str(path), 1, 0x10, None, None, None, acl),
                       "允许低完整性沙箱写入工作目录")
    finally:
        if descriptor:
            api.kernel.LocalFree(descriptor)


def _confirm_lpac_access(api: _API, token: HANDLE):
    """Independent, fail-closed check of documented LPAC access semantics.

    An inert memory-only ACL grants one bit to Everyone and all application
    packages. An LPAC must be denied by it; the equivalent all-restricted
    application packages ACL must pass as a positive control. This does not
    access a file or registry key or change any actual object permission.
    The Win32 class-46 query is rejected with error 87 on some current builds;
    do not interpret that absence as permission to skip independent checking.
    """
    duplicate, descriptor = HANDLE(), LPVOID()
    try:
        api.check(api.advapi.DuplicateToken(token, 2, C.byref(duplicate)), "复制沙箱令牌用于独立权限校验")
        results = []
        for principal in ("S-1-15-2-1", "S-1-15-2-2"):
            sddl = "O:WDG:WDD:(A;;0x1;;;WD)(A;;0x1;;;" + principal + ")"
            api.check(api.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, C.byref(descriptor), None), "构造内存权限校验对象")
            try:
                privileges = C.create_string_buffer(4096)
                size, granted, allowed = W.DWORD(len(privileges)), W.DWORD(), W.BOOL()
                mapping = GENERIC_MAPPING(1, 2, 4, 7)
                api.check(api.advapi.AccessCheck(descriptor, duplicate, 1, C.byref(mapping),
                          privileges, C.byref(size), C.byref(granted), C.byref(allowed)), "独立校验 LPAC 权限语义")
                results.append(bool(allowed.value))
            finally:
                api.kernel.LocalFree(descriptor)
                descriptor = LPVOID()
        if results != [False, True]:
            raise IsolationUnavailable("子进程未通过独立 LPAC 权限校验；禁止执行")
    finally:
        if descriptor:
            api.kernel.LocalFree(descriptor)
        if duplicate:
            api.kernel.CloseHandle(duplicate)


def _confirm_container(api: _API, process: HANDLE):
    token = HANDLE()
    api.check(api.advapi.OpenProcessToken(process, 0xA, C.byref(token)), "读取并独立校验沙箱令牌")
    try:
        actual, length = W.DWORD(), W.DWORD()
        api.check(api.advapi.GetTokenInformation(token, 29, C.byref(actual), C.sizeof(actual), C.byref(length)), "校验 AppContainer 令牌")
        if actual.value != 1:
            raise IsolationUnavailable("子进程没有 AppContainer 令牌；禁止执行")
        least_privileged = W.DWORD()
        queried = api.advapi.GetTokenInformation(token, 46, C.byref(least_privileged),
                                                C.sizeof(least_privileged), C.byref(length))
        if not queried and C.get_last_error() != 87:
            api.check(False, "校验 LPAC 令牌")
        if queried and not least_privileged.value:
            raise IsolationUnavailable("子进程没有 Less Privileged AppContainer 令牌；禁止执行")
        # Always check the actual token's access behavior, even when the
        # optional class-46 query succeeds. Unsupported queries never bypass
        # independent LPAC verification or the checked creation attribute.
        _confirm_lpac_access(api, token)
    finally:
        api.kernel.CloseHandle(token)


def run_isolated(executable: Path, arguments: list[str], *, runtime: Path, staging: Path,
                 timeout_s: float, cancel: threading.Event, memory_mb: int = 512,
                 max_output_bytes: int = 1024 * 1024, max_staging_bytes: int = 64 * 1024 * 1024) -> ProcessOutput:
    """Blocking host API. A cancelled async caller must set ``cancel`` and wait.

    No untrusted instruction executes before ACLs, LPAC attributes, token
    verification, and Job Object assignment have all succeeded.
    """
    api = _API()
    runtime, staging, executable = runtime.resolve(), staging.resolve(), executable.resolve()
    if not executable.is_relative_to(runtime) or not executable.is_file():
        raise IsolationUnavailable(f"runtime_files：私有解释器文件不存在或越界：{executable}；禁止降级执行")
    if not staging.is_dir() or not runtime.is_dir():
        raise IsolationUnavailable(f"runtime_files：运行时或工作目录不存在：runtime={runtime}，workspace={staging}；禁止降级执行")
    sid = PSID()
    capability_array, capability_buffers = _runtime_capabilities(api)
    # A derived SID alone does not register the per-user profile needed by
    # CreateProcessW. A fresh name must create a profile, not merely derive it.
    # See Microsoft: /windows/win32/secauthz/implementing-an-appcontainer.
    profile_name = "CoatScript." + uuid.uuid4().hex
    result = api.userenv.CreateAppContainerProfile(
        profile_name, "CoatScript sandbox", "Temporary isolated script workspace",
        capability_array, len(capability_array), C.byref(sid))
    if result < 0 or not sid:
        raise IsolationUnavailable(f"lpac_profile：无法创建临时 AppContainer 配置（HRESULT {result & 0xffffffff:#x}）；禁止降级执行")
    handles = []
    threads = []
    process = PROCESS_INFORMATION()
    attribute_list = None
    runtime_granted = staging_granted = False
    job = HANDLE()
    started = time.monotonic()
    timed_out = output_limited = False
    chunks = {"stdout": [], "stderr": []}
    captured = 0
    output_lock = threading.Lock()
    limit_hit = threading.Event()
    try:
        # FILE_GENERIC_READ | FILE_GENERIC_EXECUTE. No runtime writes.
        _grant(api, runtime, sid, 0x1200A9)
        runtime_granted = True
        # Read/write/execute/delete staging contents; no WRITE_DAC/WRITE_OWNER.
        _low_integrity(api, staging)
        _grant(api, staging, sid, 0x1301BF)
        staging_granted = True
        sa = SECURITY_ATTRIBUTES(C.sizeof(SECURITY_ATTRIBUTES), None, True)
        writes = []
        for label in ("stdout", "stderr"):
            read, write = HANDLE(), HANDLE()
            api.check(api.kernel.CreatePipe(C.byref(read), C.byref(write), C.byref(sa), 0), "建立沙箱输出管道")
            handles.extend([read, write])
            api.check(api.kernel.SetHandleInformation(read, 1, 0), "限制继承输出句柄")
            writes.append(write)

            def collect(pipe=read, key=label):
                nonlocal captured
                buffer = C.create_string_buffer(8192)
                while True:
                    count = W.DWORD()
                    if not api.kernel.ReadFile(pipe, buffer, len(buffer), C.byref(count), None) or not count.value:
                        break
                    with output_lock:
                        remaining = max(0, max_output_bytes - captured)
                        chunk = buffer.raw[:min(count.value, remaining)]
                        chunks[key].append(chunk)
                        captured += len(chunk)
                        if count.value > remaining:
                            limit_hit.set()

            thread = threading.Thread(target=collect, daemon=True)
            threads.append(thread)
        input_file = staging / ".stdin"
        input_file.write_bytes(b"")
        stdin = api.kernel.CreateFileW(str(input_file), 0x80000000, 1, C.byref(sa), 3, 0, None)
        if stdin in (None, C.c_void_p(-1).value):
            api.check(False, "建立沙箱输入句柄")
        handles.append(stdin)
        size = SIZE_T()
        api.kernel.InitializeProcThreadAttributeList(None, 4, 0, C.byref(size))
        if not size.value:
            api.check(False, "创建 LPAC 属性列表")
        attribute_buffer = C.create_string_buffer(size.value)
        attribute_list = C.cast(attribute_buffer, LPVOID)
        api.check(api.kernel.InitializeProcThreadAttributeList(attribute_list, 4, 0, C.byref(size)), "初始化 LPAC 属性")
        caps = SECURITY_CAPABILITIES(sid.value, capability_array, len(capability_array), 0)
        opt_out = W.DWORD(1)
        gui_disable = C.c_ulonglong(0x10000000)
        inherited = (HANDLE * 3)(stdin, writes[0].value, writes[1].value)
        attributes = [(0x20009, caps), (0x2000F, opt_out), (0x20007, gui_disable), (0x20002, inherited)]
        for number, value in attributes:
            api.check(api.kernel.UpdateProcThreadAttribute(attribute_list, 0, number, C.byref(value), C.sizeof(value), None, None), "启用 LPAC 限制")
        startup = STARTUPINFOEX()
        startup.StartupInfo.cb = C.sizeof(startup)
        startup.StartupInfo.dwFlags = 0x100
        startup.StartupInfo.hStdInput = stdin
        startup.StartupInfo.hStdOutput, startup.StartupInfo.hStdError = writes[0].value, writes[1].value
        startup.lpAttributeList = attribute_list
        environment = {
            "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
            "WINDIR": os.environ.get("SystemRoot", r"C:\Windows"),
            "PATH": str(executable.parent), "TEMP": str(staging / "tmp"),
            "TMP": str(staging / "tmp"), "USERPROFILE": str(staging),
            "APPDATA": str(staging / "tmp"), "LOCALAPPDATA": str(staging / "tmp"),
            "PYTHONNOUSERSITE": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
        }
        environment_block = C.create_unicode_buffer("\0".join(f"{key}={value}" for key, value in sorted(environment.items())) + "\0\0")
        command = C.create_unicode_buffer(subprocess.list2cmdline([str(executable), *arguments]))
        # EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT |
        # CREATE_SUSPENDED | CREATE_NO_WINDOW.
        created = api.kernel.CreateProcessW(str(executable), command, None, None, True,
                                            0x08080404, environment_block, str(staging),
                                            C.byref(startup), C.byref(process))
        if not created:
            # Preserve GetLastError before any further filesystem/API calls.
            error = C.get_last_error()
            raise IsolationUnavailable(
                f"lpac_create_process：创建隔离子进程失败（Windows {error}: {C.FormatError(error).strip()}）；"
                f"解释器={executable}；工作目录={staging}。固定文件已检查，"
                "该错误仍可能来自系统隔离策略或进程启动条件，不能直接判定未安装；禁止降级执行")
        handles.extend([process.hThread, process.hProcess])
        _confirm_container(api, process.hProcess)
        job = api.kernel.CreateJobObjectW(None, None)
        api.check(job, "创建沙箱 Job Object")
        limits = JOB_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x8 | 0x100 | 0x200
        limits.BasicLimitInformation.ActiveProcessLimit = 1
        limits.ProcessMemoryLimit = limits.JobMemoryLimit = max(64, memory_mb) * 1024 * 1024
        api.check(api.kernel.SetInformationJobObject(job, 9, C.byref(limits), C.sizeof(limits)), "设置进程与内存限额")
        api.check(api.kernel.AssignProcessToJobObject(job, process.hProcess), "将沙箱加入 Job Object")
        # Parent must close child pipe handles before collecting EOF.
        for handle in [stdin, *writes]:
            api.kernel.CloseHandle(handle)
            handles.remove(handle)
        for thread in threads:
            thread.start()
        if cancel.is_set():
            api.kernel.TerminateJobObject(job, 130)
        elif api.kernel.ResumeThread(process.hThread) == 0xFFFFFFFF:
            api.check(False, "恢复沙箱线程")
        while True:
            wait_status = api.kernel.WaitForSingleObject(process.hProcess, 100)
            if wait_status == 0:
                break
            if wait_status != 0x102:
                api.check(False, "等待沙箱进程")
            timed_out = time.monotonic() - started >= timeout_s
            output_limited = limit_hit.is_set()
            # Lazy bounded enumeration never traverses reparse points and
            # cannot allocate a list of millions of task-created filenames.
            disk_size = entry_count = 0
            pending = [staging]
            while pending and not output_limited:
                directory = pending.pop()
                if _reparse(directory):
                    continue
                try:
                    with os.scandir(directory) as items:
                        for item in items:
                            entry_count += 1
                            if entry_count > 1000:
                                output_limited = True
                                break
                            candidate = Path(item.path)
                            if _reparse(candidate):
                                continue
                            try:
                                info = item.stat(follow_symlinks=False)
                                if item.is_dir(follow_symlinks=False):
                                    pending.append(candidate)
                                else:
                                    disk_size += info.st_size
                            except OSError:
                                continue
                            if disk_size > max_staging_bytes:
                                output_limited = True
                                break
                except OSError:
                    continue
            if cancel.is_set() or timed_out or output_limited:
                api.check(api.kernel.TerminateJobObject(job, 130 if cancel.is_set() else 124), "终止沙箱任务")
                if api.kernel.WaitForSingleObject(process.hProcess, 5000) != 0:
                    raise IsolationUnavailable("沙箱进程未在终止后退出；不收集任何输出")
                break
        code = W.DWORD()
        api.check(api.kernel.GetExitCodeProcess(process.hProcess, C.byref(code)), "获取脚本退出状态")
        for thread in threads:
            thread.join(timeout=2)
        output_limited = output_limited or limit_hit.is_set()
        loader_failures = {
            0xC0000135: "本机 DLL 未找到或在隔离权限下不可读取",
            0xC0000142: "本机模块初始化失败",
            0xC000007B: "解释器/本机 DLL 架构或格式不匹配",
            0xC0000022: "操作系统拒绝本机映像访问",
        }
        if code.value in loader_failures:
            detail = b"".join(chunks["stderr"]).decode("utf-8", "replace")[-2000:]
            raise IsolationUnavailable(
                f"lpac_runtime_load：{loader_failures[code.value]}（NTSTATUS 0x{code.value:08X}）；"
                f"解释器={executable}；{detail}；禁止降级执行")
        return ProcessOutput(b"".join(chunks["stdout"]).decode("utf-8", "replace"),
                             b"".join(chunks["stderr"]).decode("utf-8", "replace"),
                             code.value, round(time.monotonic() - started, 3), timed_out, output_limited)
    finally:
        # KILL_ON_JOB_CLOSE kills all job members on cancellation, failure, or
        # a dropped caller; a suspended child also gets terminated on failure.
        if job:
            api.kernel.TerminateJobObject(job, 130)
            api.kernel.CloseHandle(job)
        elif process.hProcess:
            api.kernel.TerminateProcess(process.hProcess, 130)
        if process.hProcess:
            # Profile deletion must happen after the contained process has
            # exited, including failures while it was still suspended.
            api.kernel.WaitForSingleObject(process.hProcess, 5000)
        for handle in reversed(handles):
            api.kernel.CloseHandle(handle)
        if attribute_list:
            api.kernel.DeleteProcThreadAttributeList(attribute_list)
        cleanup_errors = []
        # The staging tree may contain task-created reparse points. Do not
        # propagate a privileged ACL update through it during cleanup; its
        # fresh, never reused SID grant disappears when the host deletes it.
        for path, granted in ((runtime, runtime_granted),):
            if granted:
                try:
                    _grant(api, path, sid, 0, revoke=True)
                except Exception as exc:
                    cleanup_errors.append(str(exc))
        if sid:
            api.advapi.FreeSid(sid)
        deletion = api.userenv.DeleteAppContainerProfile(profile_name)
        if deletion < 0:
            cleanup_errors.append(f"临时 AppContainer 配置清理失败（HRESULT {deletion & 0xffffffff:#x}）")
        if cleanup_errors:
            raise IsolationUnavailable("隔离 ACL 清理失败：" + "; ".join(cleanup_errors))


def _reparse(path: Path) -> bool:
    # Reject names that Win32 may normalize into a different parent/device.
    for component in path.parts[1:]:
        stem = component.split(".", 1)[0].upper()
        if component.endswith((" ", ".")) or ":" in component or stem in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            return True
    try:
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400) or path.is_symlink()
    except OSError:
        return True
