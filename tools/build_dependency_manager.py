#!/usr/bin/env python3
"""Build the selected PyInstaller bundle; retain prior native audit evidence."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import sysconfig

ROOT = Path(__file__).resolve().parents[1]


def package_auditor():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from tools.dependency_manager import package_audit

    return package_audit


def release_policy() -> dict:
    data = json.loads((ROOT / "tools/dependency_manager/release_policy.json").read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise RuntimeError("Unsupported runtime release policy schema")
    policy = data.get("platforms", {}).get(sys.platform, {})
    if policy.get("redistribution_approved"):
        evidence = policy.get("evidence_files", [])
        if not evidence or any(not (ROOT / name).is_file() for name in evidence):
            raise RuntimeError("Runtime distribution approval lacks archived evidence")
        requirements = policy.get("requirements", {})
        if requirements and any(value is not True for value in requirements.values()):
            raise RuntimeError("Runtime distribution approval has unmet explicit requirements")
        for name, digest in policy.get("evidence_sha256", {}).items():
            if name not in evidence or hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest:
                raise RuntimeError("Runtime distribution evidence hash mismatch: " + name)
    return policy


def copy_required(candidates: list[Path], destination: Path) -> Path:
    """Missing full runtime notices block publication instead of silently omitting them."""
    for source in candidates:
        if source.is_file() and source.stat().st_size:
            shutil.copy2(source, destination)
            return source
    raise RuntimeError(f"Required licence notice missing: {destination.name}")


def distribution_license(name: str) -> Path:
    distribution = importlib.metadata.distribution(name)
    candidates = [
        Path(str(distribution.locate_file(file)))
        for file in distribution.files or []
        if "/licenses/" in str(file) and file.name in ("LICENSE", "LICENSE.md")
    ]
    if len(candidates) != 1:
        raise RuntimeError(f"Cannot unambiguously locate full {name} licence")
    return candidates[0]


def verify_payload(output: Path) -> None:
    """Build-only packages and previous packagers must never leak into this bundle."""
    package_auditor().audit_file_names(output)
    if not (output / "frozen_application_license.txt").is_file():
        raise RuntimeError("cx_Freeze startup licence is absent from generated payload")


def bundled_doctor(output: Path) -> dict:
    executable = output / ("deposition-deps.exe" if sys.platform == "win32" else "deposition-deps")
    result = subprocess.run([str(executable), "doctor", "--json"], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("Bundled Tcl/Tk doctor failed: " + result.stderr[:2000])
    data = json.loads(result.stdout)
    if data.get("status") != "success" or not data.get("frozen"):
        raise RuntimeError("Bundled doctor did not verify its independent runtime")
    return data


def collect_licenses(output: Path, doctor: dict, msvc_license: Path | None, native_audit: dict | None = None) -> list[dict]:
    licenses = output / "licenses"
    licenses.mkdir(exist_ok=True)
    entries = []
    python_version = doctor["tool_python"].split()[0]
    exact_python = ROOT / "tools/dependency_manager/licenses" / (
        "CPython-3.12.15-LICENSE.txt" if sys.platform == "linux" else "CPython-3.13.16-Windows-LICENSE.txt"
    )
    source = copy_required([
        exact_python,
        Path(sysconfig.get_path("stdlib")) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE.txt", Path(sys.base_prefix) / "LICENSE",
    ], licenses / "CPython-LICENSE.txt")
    entries.append({"component": "CPython and bundled stdlib notices", "version": python_version, "notice": "CPython-LICENSE.txt", "source": source.name})
    for name, filename, license_id in (("cx-Freeze", "cx_Freeze-LICENSE.md", "PSF-2.0"), ("freeze-core", "freeze-core-LICENSE.txt", "MIT")):
        copy_required([distribution_license(name)], licenses / filename)
        entries.append({"component": name, "version": importlib.metadata.version(name), "license": license_id, "notice": filename})
    # The core Tcl/Tk terms apply to all core versions. Keep the full notices;
    # report the actual frozen runtime version, not this build host's version.
    for component, version in (("Tcl", doctor["tcl_patchlevel"]), ("Tk", doctor["tk_version"])):
        filename = f"{component}-license.terms"
        copy_required([ROOT / "tools/dependency_manager/licenses" / filename], licenses / filename)
        entries.append({"component": component, "version": version, "notice": filename, "source": "https://www.tcl-lang.org/software/tcltk/license.html", "notice_provenance": "core-9-0-4 upstream core terms, applicable to all core versions"})
    research = ROOT / "tools/dependency_manager/licenses/research-inventory.json"
    copy_required([research], licenses / research.name)
    notice_rows = {row["notice"]: row for row in json.loads(research.read_text(encoding="utf-8"))["components"]}
    selected = {notice for component in (native_audit or {}).get("components", []) for notice in component["notices"]}
    # Research includes notices for excluded candidates. Do not present them as
    # actual runtime components; publish only the exact reviewed profile closure.
    for name in sorted(selected - {"freeze-core-LICENSE.txt", "FreeType-credit.txt"}):
        row = notice_rows[name]
        source = ROOT / "tools/dependency_manager/licenses" / row["notice"]
        if row["status"] != "unresolved":
            if hashlib.sha256(source.read_bytes()).hexdigest() != row["sha256"]:
                raise RuntimeError("Archived upstream notice hash changed: " + source.name)
            copy_required([source], licenses / source.name)
    (licenses / "FreeType-credit.txt").write_text(
        "This software uses the FreeType library. Portions of this software are copyright © 2017 The FreeType Project (www.freetype.org). All rights reserved.\n"
        "The FreeType License (FTL) branch is selected; the GPL branch is not selected. Full FTL terms are in FreeType-2.8-FTL.TXT.\n",
        encoding="utf-8",
    )
    if native_audit:
        entries.extend(native_audit["components"])
        for name in ("native-provenance.json", native_audit["profile"]):
            copy_required([ROOT / "tools/dependency_manager/licenses" / name], licenses / name)
    if sys.platform == "win32":
        if msvc_license is None:
            raise RuntimeError("Bundled MSVC runtime requires --msvc-license with its complete redistributable notice")
        copy_required([msvc_license], licenses / "Microsoft-VC-runtime-license.txt")
        entries.append({"component": "Microsoft Visual C++ runtime", "notice": "Microsoft-VC-runtime-license.txt", "source": msvc_license.name, "license": "Microsoft redistributable terms"})
    (licenses / "inventory.json").write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    return entries


def legacy_cxfreeze_validation(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-build", action="store_true", help="Internal functional evaluation only; never a redistribution approval")
    parser.add_argument("--msvc-license", type=Path, help="Windows: full Microsoft VC redistributable licence notice")
    args = parser.parse_args(argv)
    staging = ROOT / "build/dependency_manager/cxfreeze-bundle"
    command = [sys.executable, str(ROOT / "tools/dependency_manager/freeze_setup.py"), "build_exe", "--build-exe", str(staging)]
    if args.dry_run:
        print(json.dumps({"argv": command, "requires": "cx-Freeze 8.7.1 / freeze-core 0.8.0 + native CPython Tk", "self_contained": True, "excluded": ["project venv", "Qt", "GPU", "other", "data", "credentials", "build-only packages", "GNU readline/GDBM", "vendored Linux libuuid"], "licenses": {"cx_Freeze": "PSF-2.0", "freeze_core": "MIT", "linux_patchelf": "GPL-3.0 build-only, not distributed", "windows_msvc": "only pinned CPython DLLs; authority/downstream acceptance/final Tk closure gate remains"}}, ensure_ascii=False))
        return 0
    if importlib.util.find_spec("cx_Freeze") is None:
        print("Build environment lacks cx_Freeze. Follow docs/dependency-manager.md; no automatic installation.", file=sys.stderr)
        return 2
    policy = release_policy()
    if not args.test_build and not policy.get("redistribution_approved"):
        print("Binary redistribution blocked: " + policy.get("reason", "unreviewed runtime licences"), file=sys.stderr)
        return 2
    if sys.platform == "win32" and (args.msvc_license is None or not args.msvc_license.is_file()):
        print("Missing --msvc-license; Windows runtime distribution is blocked until its full notice is supplied.", file=sys.stderr)
        return 2
    if staging.exists():
        shutil.rmtree(staging)
    code = subprocess.run(command, cwd=ROOT).returncode
    if code:
        return code
    try:
        auditor = package_auditor()
        native_audit = None
        if sys.platform == "linux":
            profile = auditor.load_profile()
            auditor.prepare_linux_payload(staging, profile)
            native_audit = auditor.audit_linux_payload(staging, profile=profile)
        verify_payload(staging)
        doctor = bundled_doctor(staging)
        if sys.platform == "linux":
            native_audit = auditor.audit_linux_payload(staging, doctor)
        license_inventory = collect_licenses(staging, doctor, args.msvc_license, native_audit)
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    shutil.copy2(ROOT / "docs/dependency-manager.md", staging / "README.md")
    files = [{"path": str(path.relative_to(staging)), "size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in sorted(staging.rglob("*")) if path.is_file()]
    (staging / "build-manifest.json").write_text(json.dumps({"schema_version": 1, "platform": sys.platform, "machine": platform.machine(), "build_python": sys.version, "cx_freeze_version": importlib.metadata.version("cx-Freeze"), "freeze_core_version": importlib.metadata.version("freeze-core"), "bundled_runtime": doctor, "native_payload_audit": native_audit, "license_inventory": license_inventory, "redistribution_policy": policy, "internal_test_build": args.test_build, "build_only_tools": {"patchelf": "GPL-3.0; ELF editing only, not bundled"} if sys.platform == "linux" else {}, "gui_rendering_verified": False, "files": files}, indent=2, ensure_ascii=False), encoding="utf-8")
    output = ROOT / "dist/deposition-dependency-manager"
    if output.exists():
        shutil.rmtree(output)
    output.parent.mkdir(exist_ok=True)
    shutil.copytree(staging, output)
    print(output)
    return 0


def main(argv=None):
    print("EXE packaging retired: use the source dependency manager and Python bootstrap launcher.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
