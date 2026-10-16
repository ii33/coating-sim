"""Build the Windows desktop interpreter host without changing Python itself.

Only the copied PE icon/version resources are changed. The Qt/Python stack,
DLL imports, runtime location and Python licensing remain the same. A resource
update invalidates an upstream executable signature; this local development
host is not represented as a signed distribution.

Microsoft resource format/API references:
https://learn.microsoft.com/windows/win32/api/winbase/nf-winbase-updateresourcew
https://learn.microsoft.com/windows/win32/menurc/vs-versioninfo
https://learn.microsoft.com/windows/win32/menurc/newheader
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile

APP_NAME = "沉积仿真"
HOST_NAME = "沉积仿真.exe"
HOST_ROLES = {
    "main": ("沉积仿真", "沉积仿真.exe"),
    "launcher": ("光学生产仿真", "光学生产仿真.exe"),
    "source": ("蒸发源", "蒸发源.exe"),
    "substrate": ("基片", "基片.exe"),
    "process": ("工艺", "工艺.exe"),
    "machine": ("机器结构", "机器结构.exe"),
    "mask": ("修正板", "修正板.exe"),
    "settings": ("设置", "设置.exe"),
    "about": ("关于与帮助", "关于与帮助.exe"),
}
BUILD_FORMAT = 1
RT_ICON, RT_GROUP_ICON, RT_VERSION = 3, 14, 16
LANGUAGE = 0x0409


def _resource_key(value: int | str):
    return ctypes.c_void_p(value) if isinstance(value, int) else ctypes.cast(
        ctypes.c_wchar_p(value), ctypes.c_void_p)


class WindowsResources:
    """Load PE data only; never execute the copied host while building it."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Desktop PE hosts are built only on Windows")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.version = ctypes.WinDLL("version", use_last_error=True)
        pointer = ctypes.c_void_p
        self.name_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, pointer, pointer,
                                                pointer, ctypes.c_ssize_t)
        self.lang_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, pointer, pointer,
                                                pointer, wintypes.WORD, ctypes.c_ssize_t)
        signatures = {
            "LoadLibraryExW": ([wintypes.LPCWSTR, pointer, wintypes.DWORD], pointer),
            "FreeLibrary": ([pointer], wintypes.BOOL),
            "EnumResourceNamesW": ([pointer, pointer, self.name_callback, ctypes.c_ssize_t], wintypes.BOOL),
            "EnumResourceLanguagesW": ([pointer, pointer, pointer, self.lang_callback, ctypes.c_ssize_t], wintypes.BOOL),
            "FindResourceExW": ([pointer, pointer, pointer, wintypes.WORD], pointer),
            "SizeofResource": ([pointer, pointer], wintypes.DWORD),
            "LoadResource": ([pointer, pointer], pointer),
            "LockResource": ([pointer], pointer),
            "BeginUpdateResourceW": ([wintypes.LPCWSTR, wintypes.BOOL], pointer),
            "UpdateResourceW": ([pointer, pointer, pointer, wintypes.WORD, pointer, wintypes.DWORD], wintypes.BOOL),
            "EndUpdateResourceW": ([pointer, wintypes.BOOL], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result
        self.version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        self.version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, pointer]
        self.version.GetFileVersionInfoW.restype = wintypes.BOOL
        self.version.VerQueryValueW.argtypes = [pointer, wintypes.LPCWSTR, ctypes.POINTER(pointer), ctypes.POINTER(wintypes.UINT)]
        self.version.VerQueryValueW.restype = wintypes.BOOL

    @staticmethod
    def checked(value, operation: str):
        if not value:
            raise ctypes.WinError(ctypes.get_last_error(), operation)
        return value

    def resources(self, path: Path, resource_types: tuple[int, ...]) -> dict[tuple[int, int | str, int], bytes]:
        # AS_DATAFILE | AS_IMAGE_RESOURCE avoids imports, DllMain and execution.
        module = self.checked(self.kernel.LoadLibraryExW(str(path), None, 0x22), "Load PE resources")
        result = {}
        errors = []
        try:
            for kind in resource_types:
                def found_name(handle, type_pointer, name_pointer, parameter):
                    name = int(name_pointer) if int(name_pointer) <= 0xffff else ctypes.wstring_at(name_pointer)
                    def found_language(handle, type_pointer, name_pointer, language, parameter):
                        resource = self.kernel.FindResourceExW(handle, type_pointer, name_pointer, language)
                        if not resource:
                            errors.append(ctypes.WinError(ctypes.get_last_error()))
                            return False
                        size = self.kernel.SizeofResource(handle, resource)
                        loaded = self.kernel.LoadResource(handle, resource)
                        data = self.kernel.LockResource(loaded) if loaded else None
                        if not data or not size:
                            errors.append(OSError("PE resource has no readable data"))
                            return False
                        result[(kind, name, int(language))] = ctypes.string_at(data, size)
                        return True
                    callback = self.lang_callback(found_language)
                    if not self.kernel.EnumResourceLanguagesW(handle, type_pointer, name_pointer, callback, 0):
                        errors.append(ctypes.WinError(ctypes.get_last_error()))
                        return False
                    return True
                callback = self.name_callback(found_name)
                if not self.kernel.EnumResourceNamesW(module, _resource_key(kind), callback, 0):
                    error = ctypes.get_last_error()
                    if error not in (0, 1813):  # ERROR_RESOURCE_TYPE_NOT_FOUND
                        errors.append(ctypes.WinError(error))
            if errors:
                raise errors[0]
            return result
        finally:
            self.kernel.FreeLibrary(module)

    def replace(self, path: Path, values: dict[tuple[int, int | str, int], bytes]) -> None:
        old = self.resources(path, (RT_ICON, RT_GROUP_ICON, RT_VERSION))
        update = self.checked(self.kernel.BeginUpdateResourceW(str(path), False), "Begin PE resource update")
        committed = False
        try:
            for kind, name, language in old:
                self.checked(self.kernel.UpdateResourceW(update, _resource_key(kind), _resource_key(name),
                                                          language, None, 0), "Remove copied resource")
            for (kind, name, language), data in values.items():
                buffer = ctypes.create_string_buffer(data)
                self.checked(self.kernel.UpdateResourceW(update, _resource_key(kind), _resource_key(name),
                                                          language, buffer, len(data)), "Write copied resource")
            self.checked(self.kernel.EndUpdateResourceW(update, False), "Commit PE resource update")
            committed = True
        finally:
            if not committed:
                self.kernel.EndUpdateResourceW(update, True)

    def version_values(self, path: Path) -> tuple[bytes, dict[str, str]]:
        unused = wintypes.DWORD()
        size = self.checked(self.version.GetFileVersionInfoSizeW(str(path), ctypes.byref(unused)), "Read version size")
        data = ctypes.create_string_buffer(size)
        self.checked(self.version.GetFileVersionInfoW(str(path), 0, size, data), "Read version data")
        pointer, length = ctypes.c_void_p(), wintypes.UINT()
        self.checked(self.version.VerQueryValueW(data, "\\", ctypes.byref(pointer), ctypes.byref(length)), "Read fixed version")
        fixed = ctypes.string_at(pointer, length.value)
        values = {}
        for name in ("FileDescription", "ProductName", "OriginalFilename", "FileVersion"):
            if self.version.VerQueryValueW(data, "\\StringFileInfo\\040904b0\\" + name,
                                           ctypes.byref(pointer), ctypes.byref(length)):
                values[name] = ctypes.wstring_at(pointer, max(0, length.value - 1))
        return fixed, values


def _aligned(data: bytes) -> bytes:
    return data + b"\0" * (-len(data) % 4)


def _version_block(key: str, value: bytes = b"", value_length: int | None = None,
                   *, text: bool = False, children: tuple[bytes, ...] = ()) -> bytes:
    head = _aligned(struct.pack("<HHH", 0, len(value) if value_length is None else value_length,
                                int(text)) + (key + "\0").encode("utf-16le"))
    body = head + value
    for child in children:
        body = _aligned(body) + child
    if len(body) > 0xffff:
        raise ValueError("Version resource is too large")
    return struct.pack("<H", len(body)) + body[2:]


def version_resource(fixed: bytes, *, app_name: str = APP_NAME,
                     host_name: str = HOST_NAME) -> bytes:
    if len(fixed) != 52 or struct.unpack_from("<I", fixed)[0] != 0xFEEF04BD:
        raise ValueError("Source interpreter has no valid fixed version resource")
    words = struct.unpack("<13I", fixed)
    runtime_version = ".".join(str(number) for number in
                                (words[2] >> 16, words[2] & 0xffff, words[3] >> 16, words[3] & 0xffff))
    values = {
        "CompanyName": "LYX",
        "FileDescription": app_name,
        "ProductName": app_name,
        "OriginalFilename": host_name,
        "InternalName": Path(host_name).stem,
        # This executable is the Python runtime host, so keep its true runtime version.
        "FileVersion": runtime_version,
        "ProductVersion": runtime_version,
        "LegalCopyright": "Python runtime: Python Software Foundation; desktop application: LYX",
    }
    strings = tuple(_version_block(name, (value + "\0").encode("utf-16le"),
                                   len(value.encode("utf-16le")) // 2 + 1, text=True)
                    for name, value in values.items())
    string_info = _version_block("StringFileInfo", text=True,
                                children=(_version_block("040904b0", text=True, children=strings),))
    variables = _version_block("VarFileInfo", text=True,
                               children=(_version_block("Translation", struct.pack("<HH", LANGUAGE, 1200)),))
    return _version_block("VS_VERSION_INFO", fixed, children=(string_info, variables))


def icon_resources(path: Path) -> dict[tuple[int, int | str, int], bytes]:
    data = path.read_bytes()
    if len(data) < 6:
        raise ValueError("Application icon has no ICO header")
    reserved, kind, count = struct.unpack_from("<HHH", data)
    if reserved != 0 or kind != 1 or not 0 < count < 256 or len(data) < 6 + count * 16:
        raise ValueError("Application icon is not a supported ICO file")
    resources = {}
    group = struct.pack("<HHH", 0, 1, count)
    for index in range(count):
        width, height, colors, reserved, planes, bits, size, offset = struct.unpack_from("<BBBBHHII", data, 6 + index * 16)
        if not size or offset < 6 + count * 16 or offset + size > len(data):
            raise ValueError("Application icon entry is outside the file")
        resource_id = index + 1
        resources[(RT_ICON, resource_id, LANGUAGE)] = data[offset:offset + size]
        group += struct.pack("<BBBBHHIH", width, height, colors, reserved, planes, bits, size, resource_id)
    resources[(RT_GROUP_ICON, 1, LANGUAGE)] = group
    return resources


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inspect_host(path: Path, icon: Path, *, app_name: str = APP_NAME,
                 host_name: str = HOST_NAME) -> dict[str, object]:
    api = WindowsResources()
    _, values = api.version_values(path)
    expected = icon_resources(icon)
    actual = api.resources(path, (RT_ICON, RT_GROUP_ICON))
    if actual != expected:
        raise ValueError("Built host icon resources do not match the application icon")
    for key, value in (("FileDescription", app_name), ("ProductName", app_name), ("OriginalFilename", host_name)):
        if values.get(key) != value:
            raise ValueError("Built host version field does not match: " + key)
    return {"path": str(path), "name": values["FileDescription"], "original_filename": values["OriginalFilename"],
            "icon_images": len(expected) - 1, "runtime_version": values.get("FileVersion", ""), "sha256": _sha256(path)}


def _legacy_manifest_matches(cached: object, fingerprint: dict[str, str | int],
                             module: str, app_name: str, host_name: str) -> bool:
    """Accept only the immediately preceding role-aware cache schema."""
    if not isinstance(cached, dict):
        return False
    old = cached.get("fingerprint")
    if not isinstance(old, dict) or set(old) != {
            "format", "source_sha256", "icon_sha256", "name", "host_name", "module"}:
        return False
    role = old.get("module")
    return (all(old.get(key) == value for key, value in fingerprint.items())
            and role in HOST_ROLES
            and HOST_ROLES[role] == (app_name, host_name)
            and HOST_ROLES[module] == (app_name, host_name))


def build_host(project_root: Path, *, force: bool = False, module: str = "main") -> Path:
    if sys.platform != "win32":
        raise OSError("Windows PE host build does not apply to this platform")
    root = project_root.resolve()
    if module not in HOST_ROLES:
        raise ValueError("Unknown desktop host module role")
    app_name, host_name = HOST_ROLES[module]
    environment = (root / ".conda").resolve()
    if environment.parent != root:
        raise ValueError("The project interpreter directory must remain inside the project")
    source = environment / "pythonw.exe"
    target = environment / host_name
    icon = root / "coat_sim" / "ui" / "assets" / "icons" / "logolite.ico"
    if not source.is_file() or not icon.is_file():
        raise FileNotFoundError("The project Python desktop runtime or application icon is missing")
    source_hash = _sha256(source)
    fingerprint = {"format": BUILD_FORMAT, "source_sha256": source_hash, "icon_sha256": _sha256(icon),
                   "name": app_name, "host_name": host_name}
    manifest_path = environment / (Path(host_name).stem + ".host.json")
    try:
        cached = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not force and target.is_file():
            cached_hash = cached.get("host_sha256")
            actual_hash = _sha256(target)
            cache_matches = (cached.get("fingerprint") == fingerprint
                             or _legacy_manifest_matches(cached, fingerprint, module, app_name, host_name))
            if cache_matches and cached_hash == actual_hash:
                # A cache hit is accepted only after checking the live PE
                # metadata and icon against the requested role and source icon.
                inspect_host(target, icon, app_name=app_name, host_name=host_name)
                return target
    except (OSError, ValueError, TypeError):
        pass
    api = WindowsResources()
    fixed, _ = api.version_values(source)
    resources = icon_resources(icon)
    resources[(RT_VERSION, 1, LANGUAGE)] = version_resource(fixed, app_name=app_name, host_name=host_name)
    descriptor, temporary_name = tempfile.mkstemp(prefix="." + Path(host_name).stem + "-host-", suffix=".tmp.exe", dir=environment)
    os.close(descriptor)
    temporary = Path(temporary_name)
    manifest_temporary = temporary.with_suffix(".json")
    try:
        shutil.copy2(source, temporary)
        api.replace(temporary, resources)
        info = inspect_host(temporary, icon, app_name=app_name, host_name=host_name)
        if _sha256(source) != source_hash:
            raise OSError("Source Python runtime changed during host build")
        manifest_temporary.write_text(json.dumps({"fingerprint": fingerprint, "host_sha256": info["sha256"]},
                                                ensure_ascii=False, indent=2), encoding="utf-8")
        # A running host cannot be replaced on Windows; leave it intact on failure.
        os.replace(temporary, target)
        os.replace(manifest_temporary, manifest_path)
        return target
    finally:
        temporary.unlink(missing_ok=True)
        manifest_temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a branded local Windows desktop host")
    parser.add_argument("--force", action="store_true", help="Rebuild even when the source and icon have not changed")
    parser.add_argument("--module", choices=tuple(HOST_ROLES), default="main",
                        help="Desktop module role (default: main)")
    options = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    host = build_host(root, force=options.force, module=options.module)
    app_name, host_name = HOST_ROLES[options.module]
    print(json.dumps(inspect_host(host, root / "coat_sim/ui/assets/icons/logolite.ico",
                                  app_name=app_name, host_name=host_name), ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
