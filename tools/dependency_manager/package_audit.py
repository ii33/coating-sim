"""Build-time, fail-closed native payload audit. Never shipped in the tool."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "tools/dependency_manager/licenses/linux-x86_64-cp312-profile.json"
WINDOWS_PROVENANCE = ROOT / "tools/dependency_manager/licenses/windows-cpython-provenance.json"
FORBIDDEN = {
    "patchelf", "pyinstaller", "cx_freeze", "pip", "setuptools", "coat_sim",
    "pyside6", "freeze_setup", "package_audit", "readline", "_gdbm", "_dbm",
}


def load_profile(path: Path = PROFILE) -> dict:
    profile = json.loads(path.read_text(encoding="utf-8"))
    if profile.get("schema_version") != 1 or not profile.get("native_files"):
        raise RuntimeError("Unsupported or empty native licence profile")
    return profile


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit_file_names(output: Path) -> None:
    """Inspect archives too: a build-only/GPL extension cannot hide in a ZIP."""
    for path in output.rglob("*"):
        if path.is_symlink():
            raise RuntimeError("Symlink in distributable payload: " + str(path.relative_to(output)))
        names = [str(path.relative_to(output))]
        if path.is_file() and path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as archive:
                names.extend(archive.namelist())
        for name in names:
            parts = name.lower().replace("\\", "/").split("/")
            if any(part.split(".")[0] in FORBIDDEN or part.startswith(("libreadline", "libgdbm")) for part in parts):
                raise RuntimeError("Unexpected build-only/project/GPL extension payload: " + name)


def prepare_linux_payload(output: Path, profile: dict) -> None:
    """Use OS libuuid instead of redistributing the wheel's mixed BSD/LGPL copy.

    Check the exact original binaries before modifying DT_NEEDED, and check the
    result against the reviewed profile afterwards. No OS library is changed.
    """
    transition = profile["system_libuuid_transition"]
    library = output / transition["consumer"]
    removed = output / transition["remove"]
    if sha256(library) != transition["original_consumer_sha256"] or sha256(removed) != transition["original_removed_sha256"]:
        raise RuntimeError("Unreviewed fontconfig/libuuid input; refusing native relocation")
    editor = shutil.which("patchelf")
    if editor is None:
        raise RuntimeError("Build-only patchelf is required for system libuuid relocation")
    subprocess.run(
        [editor, "--replace-needed", removed.name, "libuuid.so.1", str(library)],
        check=True, capture_output=True, timeout=30,
    )
    removed.unlink()


def audit_linux_payload(output: Path, doctor: dict | None = None, profile: dict | None = None) -> dict:
    """Exact native hashes seal the reviewed DT_NEEDED and component identities.

    Unknown/missing/changed ELF files block; a SONAME or a retained GNU Build ID
    alone is not proof. The recorded direct system imports are prerequisites,
    not bundled licences or an assertion about their transitive OS closure.
    """
    profile = load_profile() if profile is None else profile
    expected = {row["path"]: row for row in profile["native_files"]}
    actual = {}
    for path in output.rglob("*"):
        if not path.is_file():
            continue
        with path.open("rb") as stream:
            magic = stream.read(4)
        if magic == b"\x7fELF":
            actual[path.relative_to(output).as_posix()] = path
    if set(actual) != set(expected):
        raise RuntimeError("Native payload differs from licence profile: " + json.dumps({
            "unreviewed": sorted(set(actual) - set(expected)), "missing": sorted(set(expected) - set(actual)),
        }))
    for name, path in actual.items():
        if sha256(path) != expected[name]["sha256"]:
            raise RuntimeError("Native payload hash differs from reviewed component: " + name)
    if doctor is not None:
        for key, value in profile["bundled_runtime"].items():
            actual_value = doctor.get(key)
            if key == "tool_python" and isinstance(actual_value, str):
                actual_value = actual_value.split()[0]
            if actual_value != value:
                raise RuntimeError("Bundled runtime differs from licence profile: " + key)
    names = {path.name for path in actual.values()}
    external = sorted({needed for row in expected.values() for needed in row["needed"] if needed not in names})
    if external != profile["direct_system_dependencies"]:
        raise RuntimeError("Unreviewed system dependency in native licence profile")
    return {"profile": PROFILE.name, "native_files_verified": len(actual),
            "direct_system_dependencies": external, "components": profile["components"]}


def windows_runtime_sources(python_root: Path, provenance_path: Path = WINDOWS_PROVENANCE) -> list[Path]:
    """Only the unchanged DLLs shipped by the pinned official CPython archive.

    No vc_redist download, arbitrary VS DLL, UCRT copy, or licence acceptance.
    This verifies bytes, not redistribution authority or the final Tk PE closure.
    """
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    if provenance.get("schema_version") != 1:
        raise RuntimeError("Unsupported official Windows runtime provenance")
    expected = provenance["cpython_runtime_files"]
    if set(expected) != {"vcruntime140.dll", "vcruntime140_1.dll"}:
        raise RuntimeError("Unreviewed Windows runtime file list")
    paths = []
    for name, row in expected.items():
        path = python_root / name
        if not path.is_file() or path.is_symlink() or sha256(path) != row["sha256"]:
            raise RuntimeError("Official CPython runtime file missing or hash mismatch: " + name)
        paths.append(path)
    return paths
