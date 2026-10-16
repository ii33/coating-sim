#!/usr/bin/env python
"""Build the Windows x64 per-user offline setup executable.

Run with the project's verified Python 3.12 interpreter.

The script downloads only the pinned PyInstaller build tool and its wheels from
PyPI into an isolated build directory. It never installs or edits .conda.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from coat_sim.version import APP_VERSION  # noqa: E402


BUILD = ROOT / "build" / "offline_installer_windows_x64"
DIST = ROOT / "dist"
APP_NAME = "CoatSim"
PYINSTALLER_VERSION = "6.22.3"
CS_SOURCE = ROOT / "tools" / "offline_installer_bootstrap.cs"


def run(command: list[str], *, env: dict[str, str] | None = None, cwd: Path = ROOT) -> None:
    print("+", subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_size(path: Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:
                pass
    return total


def _validate_package_relative_path(relative: Path | PurePosixPath | str, *, context: str) -> None:
    """Reject private design paths from any source or payload tree."""
    parts = PurePosixPath(str(relative).replace("\\", "/")).parts
    if any(part.casefold() == "sheji" for part in parts):
        raise RuntimeError(f"Refusing to package private design path in {context}: {PurePosixPath(*parts).as_posix()}")


def validate_package_tree(root: Path, *, context: str) -> None:
    """Fail closed if a package input contains a path component named sheji."""
    if not root.exists():
        return
    for item in root.rglob("*"):
        _validate_package_relative_path(item.relative_to(root), context=context)


def copy_tree(source: Path, destination: Path) -> None:
    validate_package_tree(source, context=f"source tree {source}")
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        if any(part in {"__pycache__", ".pytest_cache", ".mypy_cache"} for part in relative.parts):
            continue
        if item.suffix.lower() in {".pyc", ".pyo", ".log", ".sqlite", ".sqlite3", ".db", ".lock"}:
            continue
        if item.name.lower().endswith(("-journal", "-wal", "-shm")) or item.name.startswith(".") and item.name.endswith(".tmp"):
            continue
        if item.is_symlink():
            raise RuntimeError(f"Refusing to package a symlink: {relative.as_posix()}")
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def stage_source_snapshot(destination: Path) -> list[dict[str, object]]:
    """Freeze executable source inputs before PyInstaller reads any module."""
    destination.mkdir(parents=True, exist_ok=False)
    copy_tree(ROOT / "coat_sim", destination / "coat_sim")
    for relative in (
        Path("launcher.py"), Path("launcher.pyw"), Path("README.md"), Path("LICENSE"),
        Path("tools") / "launch_desktop.pyw", Path("tools") / "build_desktop_host.py",
        Path("docs") / "algorithms.html", Path("docs") / "software_manual.html",
        # Preserve the local targets for the package README and software-manual
        # help entry; these are distinct from the portable-runtime ZIP itself.
        Path("docs") / "portable-bundle.md", Path("docs") / "portable-assets.md",
        Path("docs") / "offline-installer.md",
    ):
        source = ROOT / relative
        if source.is_file():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    # A frozen entry resolves the application root from _MEIPASS, then runs
    # the existing desktop bootstrap from its staged tools/ location.
    entry = destination / "tools" / "frozen_entry.pyw"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))\n"
        "sys.path.insert(0, str(root))\n"
        "from coat_sim.process_entry import run_frozen_entry\n"
        "run_frozen_entry(root / 'tools' / 'launch_desktop.pyw')\n",
        encoding="utf-8", newline="\n",
    )
    validate_package_tree(destination, context="source snapshot")
    records: list[dict[str, object]] = []
    for item in sorted(destination.rglob("*"), key=lambda path: path.relative_to(destination).as_posix().casefold()):
        if item.is_file():
            records.append({
                "path": item.relative_to(destination).as_posix(),
                "size_bytes": item.stat().st_size,
                "sha256": sha256(item),
            })
    return records


def snapshot_records(root: Path) -> list[dict[str, object]]:
    validate_package_tree(root, context="reused source snapshot")
    records: list[dict[str, object]] = []
    for item in sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix().casefold()):
        if item.is_file():
            records.append({
                "path": item.relative_to(root).as_posix(),
                "size_bytes": item.stat().st_size,
                "sha256": sha256(item),
            })
    return records


def prepare_static_data(stage: Path, source_root: Path = ROOT) -> list[str]:
    """Copy catalog inputs only; mutable app databases and user files stay out."""
    data = source_root / "data"
    target = stage / "data"
    target.mkdir(parents=True, exist_ok=True)
    validate_package_tree(target, context="existing staged static data")
    copied: list[str] = []

    def add_file(source: Path, relative: Path) -> None:
        _validate_package_relative_path(relative, context="selected static data")
        dest = target / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        copied.append((Path("data") / relative).as_posix())

    add_file(data / "README.md", Path("README.md"))
    add_file(data / "optical_catalog.json", Path("optical_catalog.json"))
    for item in sorted((data / "materials").glob("*.json")):
        add_file(item, Path("materials") / item.name)
    for relative_root in (Path("optical_catalog_sources"), Path("film_design") / "cie-illuminants-20261006"):
        source_root = data / relative_root
        if not source_root.is_dir():
            raise FileNotFoundError(f"Required reference data is missing: data/{relative_root.as_posix()}")
        before = len(copied)
        for item in sorted(source_root.rglob("*")):
            if item.is_file():
                rel = item.relative_to(data)
                _validate_package_relative_path(rel, context="selected static data source")
                if item.suffix.lower() in {".sqlite", ".sqlite3", ".db", ".log", ".lock", ".pyc", ".pyo"}:
                    continue
                if any(part in {"__pycache__", "tmp", "cache"} for part in rel.parts):
                    continue
                add_file(item, rel)
        if len(copied) == before:
            raise RuntimeError(f"No files were staged for data/{relative_root.as_posix()}")

    # Empty, application-owned locations are created on first use. The
    # installer's overlay policy preserves files later written into them.
    for folder in ("machines", "masks", "processes", "sources", "substrates", "results", "tmp"):
        (target / folder).mkdir(parents=True, exist_ok=True)
    return copied


def distribution_records() -> list[dict[str, str]]:
    records = []
    for dist in metadata.distributions():
        name = dist.metadata.get("Name")
        if not name:
            continue
        if name.lower() in {"pip", "setuptools", "wheel", "torchvision"}:
            continue
        records.append({
            "name": name,
            "version": dist.version,
            "license": dist.metadata.get("License-Expression") or dist.metadata.get("License") or "Not declared in installed metadata",
            "home_page": dist.metadata.get("Home-page") or "",
        })
    return sorted(records, key=lambda value: value["name"].casefold())


def stage_license_notices(stage: Path, distributions: list[dict[str, str]], build_packages: list[dict[str, str]]) -> None:
    license_dir = stage / "LICENSES"
    license_dir.mkdir(parents=True, exist_ok=True)
    package_roots = [Path(sys.prefix) / "Lib" / "site-packages", Path(sys.prefix) / "Lib"]
    copied: set[tuple[str, str]] = set()

    for dist in metadata.distributions():
        name = dist.metadata.get("Name")
        if not name:
            continue
        safe_name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)
        for file in dist.files or ():
            relative = PurePosixPath(str(file))
            lowered = relative.name.lower()
            if not (lowered.startswith(("license", "licence", "copying", "notice")) or "/licenses/" in ("/" + str(relative).lower())):
                continue
            source = Path(dist.locate_file(file))
            if not source.is_file() or source.stat().st_size > 8 * 1024 * 1024:
                continue
            # Keep license-bearing files; omit generated manuals and binary
            # resources which happen to contain the word "license".
            if source.suffix.lower() not in {"", ".txt", ".md", ".rst", ".html", ".xml", ".json"}:
                continue
            dest_name = safe_name + "__" + relative.name
            key = (name, dest_name)
            if key in copied:
                continue
            copied.add(key)
            shutil.copy2(source, license_dir / dest_name)

    # Qt's wheel places per-module and bundled third-party licenses outside
    # the dist-info directory.
    pyside_root = Path(sys.prefix) / "Lib" / "site-packages" / "PySide6"
    qt_license_root = pyside_root / "lib" / "LICENSES"
    if not qt_license_root.is_dir():
        qt_license_root = pyside_root / "LICENSES"
    if qt_license_root.is_dir():
        copy_tree(qt_license_root, license_dir / "Qt-LICENSES")
    python_license = Path(sys.prefix) / "LICENSE_PYTHON.txt"
    if python_license.is_file():
        shutil.copy2(python_license, license_dir / "Python-PSF-LICENSE.txt")

    lines = [
        "# Third-party runtime notices",
        "",
        "Generated for the CoatSim Windows x64 offline package. License values below come from the installed wheel metadata; bundled license text is in this directory where the package supplied it.",
        "The CIE, optical-catalog, and Noto font notices are shipped alongside their source data/assets.",
        "",
        "## Python runtime dependencies",
        "",
        "| Distribution | Version | Declared license | Project page |",
        "|---|---:|---|---|",
    ]
    for item in distributions:
        license_text = item["license"].replace("|", "\\|").replace("\n", " ").strip()
        home = f"[{item['home_page']}]({item['home_page']})" if item["home_page"].startswith("https://") else item["home_page"]
        lines.append(f"| {item['name']} | {item['version']} | {license_text} | {home} |")
    lines += ["", "## Build tools", "", "The installer payload was made with the following build tools. PyInstaller's bootloader license and notice are included when present in the installed wheel.", "", "| Distribution | Version | SHA-256 of downloaded wheel |", "|---|---:|---|"]
    for item in build_packages:
        lines.append(f"| {item['name']} | {item['version']} | `{item['sha256']}` |")
    lines += [
        "",
        "## External requirements and limitations",
        "",
        "- DirectML is an optional GPU backend. Its Python package is MIT-licensed; Microsoft documents that GPU execution needs DirectX 12-capable hardware and a compatible driver. The driver is supplied by Windows/the hardware vendor and is not bundled.",
        "- The application can use external language-model providers and MCP servers. Their account, API key, local CLI, network access, and model service are not included; no credentials are packaged.",
        "- CUDA/CuPy was not installed in the build environment and is not included. CPU operation remains available.",
        "- The installer uses the Windows .NET Framework/WinForms components available on supported Windows 10/11 systems; it does not install frameworks or alter system PATH/security settings.",
        "",
    ]
    (license_dir / "THIRD_PARTY_LICENSES.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")


def add_qt_and_source_data_arguments(command: list[str], data_stage: Path, source_root: Path) -> None:
    from PySide6.QtCore import QLibraryInfo
    qml = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.QmlImportsPath))
    if not qml.is_dir():
        raise FileNotFoundError("PySide6 Qt QML resources are not installed in the project environment")
    mappings = [
        (source_root / "coat_sim" / "version.txt", "coat_sim"),
        (source_root / "coat_sim" / "ui" / "qml", "coat_sim/ui/qml"),
        (source_root / "coat_sim" / "ui" / "assets", "coat_sim/ui/assets"),
        (source_root / "coat_sim" / "agent" / "resources", "coat_sim/agent/resources"),
        (source_root / "docs" / "algorithms.html", "docs"),
        (source_root / "docs" / "software_manual.html", "docs"),
        (source_root / "docs" / "portable-bundle.md", "docs"),
        (source_root / "docs" / "portable-assets.md", "docs"),
        (source_root / "docs" / "offline-installer.md", "docs"),
        # launch_desktop.pyw is run through runpy in frozen_entry.pyw, so its
        # dynamic import of the top-level launcher is invisible to PyInstaller.
        # Ship the exact frozen launcher source at the app root for PathFinder.
        (source_root / "launcher.py", "."),
        (source_root / "README.md", "."),
        (source_root / "LICENSE", "."),
        (source_root / "tools" / "launch_desktop.pyw", "tools"),
        (data_stage, "data"),
        (qml, "PySide6/Qt/qml"),
    ]
    for source, destination in mappings:
        if not source.exists():
            if source.name == "software_manual.html":
                continue
            raise FileNotFoundError(f"Required package resource is missing: {source.name}")
        command += ["--add-data", str(source) + os.pathsep + destination]


def write_payload_zip(payload_zip: Path, app_root: Path, assets: Path) -> tuple[int, list[dict[str, object]]]:
    """Create the installer payload only after validating both package trees."""
    validate_package_tree(app_root, context="final application payload")
    validate_package_tree(assets, context="final package assets")
    payload_bytes = 0
    file_manifest: list[dict[str, object]] = []
    with zipfile.ZipFile(payload_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
        for path in sorted(app_root.rglob("*"), key=lambda p: p.relative_to(app_root).as_posix().casefold()):
            if path.is_symlink():
                raise RuntimeError(f"PyInstaller emitted an unexpected link: {path.name}")
            relative = path.relative_to(app_root).as_posix()
            _validate_package_relative_path(relative, context="final application payload")
            if path.is_dir():
                archive.writestr(relative.rstrip("/") + "/", b"")
                continue
            archive.write(path, relative)
            size = path.stat().st_size
            payload_bytes += size
            file_manifest.append({"path": relative, "size_bytes": size, "sha256": sha256(path)})
        for path in sorted(assets.rglob("*"), key=lambda p: p.relative_to(assets).as_posix().casefold()):
            relative = path.relative_to(assets).as_posix()
            _validate_package_relative_path(relative, context="final package assets")
            full_relative = "package-info/" + relative
            if path.is_dir():
                archive.writestr(full_relative.rstrip("/") + "/", b"")
            else:
                archive.write(path, full_relative)
                size = path.stat().st_size
                payload_bytes += size
                file_manifest.append({"path": full_relative, "size_bytes": size, "sha256": sha256(path)})
    return payload_bytes, file_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, default=BUILD, help="New isolated work directory; it must not already exist.")
    parser.add_argument("--output", type=Path, default=DIST / "CoatSim-Offline-Setup.exe")
    parser.add_argument("--reuse-pyinstaller-runtime", type=Path, help="Reuse a previously downloaded PyInstaller target directory without another network/install step.")
    parser.add_argument("--reuse-pyinstaller-work", type=Path, help="Reuse a completed PyInstaller work directory from a prior build.")
    parser.add_argument("--reuse-pyinstaller-config", type=Path, help="Reuse PyInstaller hook/config cache from a prior build.")
    parser.add_argument("--source-snapshot", type=Path, help="Use an already frozen source snapshot; must be paired with --package-assets.")
    parser.add_argument("--package-assets", type=Path, help="Reuse curated package assets staged with the frozen source snapshot.")
    parser.add_argument("--reuse-app-dist", type=Path, help="Package an already collected onedir app tree; useful for repackaging a verified build without rerunning analysis.")
    parser.add_argument("--data-manifest", type=Path, help="Validate and record a static-data batch manifest without packaging its audit directory.")
    args = parser.parse_args()

    if os.name != "nt" or platform.machine().lower() not in {"amd64", "x86_64"} or sys.maxsize <= 2**32:
        raise SystemExit("Build this installer on 64-bit Windows with the project's 64-bit Python interpreter.")
    if sys.version_info[:2] != (3, 12):
        raise SystemExit(f"Expected Python 3.12, got {platform.python_version()}.")
    for package, expected in (("PySide6_Essentials", "6.11.2"), ("matplotlib", "3.11.2"), ("numpy", "2.5.3"), ("torch-directml", "0.2.5.dev240914")):
        actual = metadata.version(package)
        if actual != expected:
            raise SystemExit(f"Expected {package}=={expected}, found {actual}.")
    if not CS_SOURCE.is_file():
        raise FileNotFoundError(CS_SOURCE)
    if bool(args.source_snapshot) != bool(args.package_assets):
        raise SystemExit("--source-snapshot and --package-assets must be supplied together.")
    if args.reuse_app_dist and not args.source_snapshot:
        raise SystemExit("--reuse-app-dist requires --source-snapshot and --package-assets.")
    build_dir = args.build_dir.resolve()
    if build_dir.exists():
        raise SystemExit(f"Refusing to overwrite an existing build directory: {build_dir}")
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"Refusing to overwrite an existing installer: {output}")
    build_dir.mkdir(parents=True)
    output.parent.mkdir(parents=True, exist_ok=True)

    wheels = build_dir / "tool-wheels"
    tool_site = args.reuse_pyinstaller_runtime.resolve() if args.reuse_pyinstaller_runtime else build_dir / "pyinstaller-runtime"
    app_work = args.reuse_pyinstaller_work.resolve() if args.reuse_pyinstaller_work else build_dir / "pyinstaller-work"
    pyinstaller_config = args.reuse_pyinstaller_config.resolve() if args.reuse_pyinstaller_config else build_dir / "pyinstaller-config"
    app_dist = args.reuse_app_dist.resolve() if args.reuse_app_dist else build_dir / "app-dist"
    assets = build_dir / "package-assets"
    staged_data = assets / "data"
    snapshot = build_dir / "source-snapshot"
    if not args.package_assets:
        assets.mkdir()
    if args.reuse_app_dist:
        if not (app_dist / APP_NAME / f"{APP_NAME}.exe").is_file():
            raise FileNotFoundError(f"Reusable onedir application tree is incomplete: {app_dist}")
    else:
        app_dist.mkdir()

    env = os.environ.copy()
    # Do not let host MSYS/MinGW DLLs leak into a Windows app bundle. In this
    # build host, PyInstaller resolved Qt's icuuc.dll from MSYS; that file
    # shadowed Windows' own ICU and made QtCore fail to load with WinError 127.
    host_path = os.environ.get("PATH", "").split(os.pathsep)
    env["PATH"] = os.pathsep.join(
        entry for entry in host_path
        if not any(marker in entry.casefold() for marker in ("\\msys", "/msys", "\\mingw", "/mingw", "\\cygwin", "/cygwin"))
    )
    env["PIP_CACHE_DIR"] = str(build_dir / "pip-cache")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYINSTALLER_CONFIG_DIR"] = str(pyinstaller_config)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    # Resolve the pinned build tool from official PyPI only when no previous
    # isolated PyInstaller runtime was explicitly selected for reuse.
    if args.reuse_pyinstaller_runtime:
        if not (tool_site / "PyInstaller" / "__main__.py").is_file():
            raise RuntimeError(f"Reusable PyInstaller runtime is incomplete: {tool_site}")
        reused_wheels = tool_site.parent / "tool-wheels"
        if reused_wheels.is_dir():
            wheels = reused_wheels
    else:
        wheels.mkdir()
        run([sys.executable, "-m", "pip", "--isolated", "download", "--disable-pip-version-check", "--no-cache-dir", "--only-binary=:all:", "--index-url", "https://pypi.org/simple", "--dest", str(wheels), f"pyinstaller=={PYINSTALLER_VERSION}"], env=env)
        run([sys.executable, "-m", "pip", "--isolated", "install", "--disable-pip-version-check", "--no-cache-dir", "--no-index", "--find-links", str(wheels), "--target", str(tool_site), "--ignore-installed", f"pyinstaller=={PYINSTALLER_VERSION}"], env=env)
    if args.reuse_pyinstaller_work and not app_work.is_dir():
        raise RuntimeError(f"Reusable PyInstaller work directory does not exist: {app_work}")
    if args.reuse_pyinstaller_config and not pyinstaller_config.is_dir():
        raise RuntimeError(f"Reusable PyInstaller config cache does not exist: {pyinstaller_config}")

    build_packages = []
    for wheel in sorted(wheels.glob("*.whl")):
        name_version = wheel.name.split("-")[:2]
        build_packages.append({"name": name_version[0].replace("_", "-"), "version": name_version[1], "filename": wheel.name, "sha256": sha256(wheel)})
    if args.source_snapshot:
        snapshot = args.source_snapshot.resolve()
        assets = args.package_assets.resolve()
        staged_data = assets / "data"
        if not snapshot.is_dir() or not assets.is_dir():
            raise FileNotFoundError("The requested source snapshot or package-assets directory does not exist.")
        validate_package_tree(assets, context="reused package assets")
        if not (assets / "LICENSES" / "THIRD_PARTY_LICENSES.md").is_file():
            raise RuntimeError("Reused package assets are missing their third-party license inventory.")
        snapshot_created_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(snapshot.stat().st_ctime))
        frozen_snapshot_records = snapshot_records(snapshot)
        copied_data = sorted(
            item.relative_to(assets).as_posix()
            for item in staged_data.rglob("*") if item.is_file()
        )
    else:
        shutil.copy2(ROOT / "coat_sim" / "ui" / "assets" / "fonts" / "noto-cjk" / "LICENSE", assets / "Noto-CJK-LICENSE.txt")
        snapshot_created_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        frozen_snapshot_records = stage_source_snapshot(snapshot)
        copied_data = prepare_static_data(assets, source_root=ROOT)
        runtime_distributions = distribution_records()
        stage_license_notices(assets, runtime_distributions, build_packages)
        (assets / "OFFLINE_PACKAGE_README.txt").write_text(
            "CoatSim Windows x64 offline package\n\n"
            "This installation includes the Python runtime, Qt for Python/QML, CPU simulation libraries, DirectML backend, optical materials, and CIE data. CPU calculation and core GUI workflows work without network access.\n\n"
            "The default install is per-user under LocalAppData. No administrator rights, PATH changes, credentials, or system security changes are used. Choose another writable folder in the installer if needed. Reinstalling over a recognized install preserves the data, settings, history, and logs already in that folder.\n\n"
            "The assistant and external MCP/model services need their own user-provided account, API key, CLI or network service. These are not included. DirectML needs supported DirectX 12 hardware and its Windows/vendor driver; CPU remains available if no compatible device is present. CUDA/CuPy was not included.\n\n"
            "Minimum OS: Windows 10 version 1809 or later, or Windows 11. Qt uses the Windows ICU runtime (Microsoft documentation: https://learn.microsoft.com/windows/win32/intl/international-components-for-unicode--icu-).\n\n"
            "The optional SDK add-ons AG-UI, You.com, TensorBoard, the FastMCP OpenAPI adapter, and the OpenAI aiohttp transport were not installed in the source environment; the app's core GUI, CPU simulation, OpenAI-compatible assistant, and MCP server/client paths are packaged. The MCP SDK's standalone `mcp` command-line utility is not part of the app.\n\n"
            "The installer is unsigned. Windows may display its normal publisher warning. It does not bypass SmartScreen. The setup lets the current user choose a folder, needs no administrator rights, and changes no PATH or system security setting. Run `Uninstall-CoatSim.exe` from the installed folder to remove program files; it keeps `data`, `config`, `history`, and `artifacts` for reinstall or manual recovery.\n\n"
            "See LICENSES/THIRD_PARTY_LICENSES.md and the individual data/font license files for notices.\n",
            encoding="utf-8", newline="\n")
    validate_package_tree(snapshot, context="source snapshot")
    validate_package_tree(assets, context="package assets")
    runtime_distributions = distribution_records()

    data_revision = None
    manifest_relpath = None
    if args.data_manifest:
        manifest_arg = Path(args.data_manifest)
        if manifest_arg.is_absolute():
            raise RuntimeError("--data-manifest must be project-relative so no machine path enters the output manifest.")
        data_manifest_path = (ROOT / manifest_arg).resolve()
        try:
            data_manifest_path.relative_to(ROOT)
        except ValueError as exc:
            raise RuntimeError("--data-manifest must stay inside the project workspace.") from exc
        data_doc = json.loads(data_manifest_path.read_text(encoding="utf-8"))
        data_entries = data_doc.get("files")
        if not isinstance(data_entries, list) or not data_entries:
            raise RuntimeError("The static-data manifest contains no file entries.")
        for item in data_entries:
            relative = PurePosixPath(item["path"])
            if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != "data":
                raise RuntimeError(f"Unsafe or unexpected static-data manifest path: {item.get('path')}")
            staged_file = staged_data.joinpath(*relative.parts[1:])
            if not staged_file.is_file() or staged_file.stat().st_size != int(item["bytes"]) or sha256(staged_file) != item["sha256"]:
                raise RuntimeError(f"Packaged static data does not match its batch manifest: {item['path']}")
            if relative.as_posix() not in copied_data:
                raise RuntimeError(f"Batch manifest file was not staged: {item['path']}")
        manifest_relpath = manifest_arg.as_posix()
        data_revision = {
            "batch": data_doc.get("batch", "unspecified"),
            "manifest_sha256": sha256(data_manifest_path),
            "file_count": len(data_entries),
            "files": data_entries,
        }
        readme_path = assets / "OFFLINE_PACKAGE_README.txt"
        if readme_path.is_file():
            minimum_os_note = "Minimum OS: Windows 10 version 1809 or later, or Windows 11. Qt uses the Windows ICU runtime"
            current_readme = readme_path.read_text(encoding="utf-8")
            with readme_path.open("a", encoding="utf-8", newline="\n") as stream:
                if minimum_os_note not in current_readme:
                    stream.write("\n" + minimum_os_note + ". See Microsoft ICU on Windows documentation: https://learn.microsoft.com/windows/win32/intl/international-components-for-unicode--icu-.\n")
                if not args.source_snapshot:
                    stream.write("\nStatic data revision: " + str(data_revision["batch"]) + "\n")
                    stream.write("Batch manifest SHA-256: " + str(data_revision["manifest_sha256"]) + "\n")

    if not args.reuse_app_dist:
        cmd = [
            sys.executable, "-m", "PyInstaller", "--noconfirm",
            *([] if args.reuse_pyinstaller_work else ["--clean"]),
            "--onedir", "--windowed",
            "--contents-directory", ".", "--name", APP_NAME, "--distpath", str(app_dist),
            "--workpath", str(app_work), "--specpath", str(build_dir), "--paths", str(snapshot),
            "--collect-all", "coat_sim", "--collect-all", "torch_directml",
            # PyInstaller's maintained hooks collect torch, matplotlib and Qt
            # runtime DLLs/data. Static source analysis handles the explicit
            # imports in the agent, CAD, workbook and tokenization features.
            # Broad collect-all is intentionally avoided for these large SDKs;
            # it imports unrelated optional integrations and test modules.
            "--collect-submodules", "mcp.server",
            # The MCP SDK exposes an optional `mcp.cli` that calls sys.exit when
            # its optional `typer` extra is absent. The app uses its server APIs.
            "--collect-all", "keyring", "--collect-all", "tiktoken",
            "--hidden-import", "PySide6.QtCore",
            "--hidden-import", "PySide6.QtGui", "--hidden-import", "PySide6.QtWidgets",
            "--hidden-import", "PySide6.QtQml", "--hidden-import", "PySide6.QtQuick",
            "--hidden-import", "PySide6.QtQuickControls2", "--copy-metadata", "pydantic-ai-slim",
            "--copy-metadata", "pydantic-ai-harness", "--copy-metadata", "fastmcp-slim",
            "--copy-metadata", "mcp", "--copy-metadata", "torch-directml",
        ]
        add_qt_and_source_data_arguments(cmd, staged_data, snapshot)
        cmd.append(str(snapshot / "tools" / "frozen_entry.pyw"))
        py_env = env.copy()
        py_env["PYTHONPATH"] = str(tool_site)
        run(cmd, env=py_env, cwd=snapshot)

    app_root = app_dist / APP_NAME
    if not app_root.is_dir() or not (app_root / f"{APP_NAME}.exe").is_file():
        raise RuntimeError("PyInstaller did not produce the expected self-contained application directory.")
    if not (app_root / "data" / "optical_catalog.json").is_file():
        raise RuntimeError("Static optical catalog was not placed at the frozen application root.")
    if not (app_root / "coat_sim" / "ui" / "qml" / "LauncherWindow.qml").is_file():
        raise RuntimeError("The QML application files are missing from the frozen application directory.")
    launcher_module = app_root / "launcher.py"
    if not launcher_module.is_file():
        source_launcher = snapshot / "launcher.py"
        if not source_launcher.is_file():
            raise RuntimeError("The frozen launcher module is missing from both the application and source snapshot.")
        shutil.copy2(source_launcher, launcher_module)

    # This host's MSYS PATH exposes ICU 78.3 DLLs that PyInstaller otherwise
    # mistakes for Qt dependencies. PySide6 in the source environment loads
    # Windows' own ICU from System32; the foreign DLLs shadow that component.
    # Keep the OS-provided ICU dependency and remove the host copies.
    for incompatible_icu in [app_root / "icuuc.dll", *app_root.glob("icudt*.dll")]:
        if incompatible_icu.is_file():
            incompatible_icu.unlink()
    csc_candidates = [
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe",
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework" / "v4.0.30319" / "csc.exe",
    ]
    csc = next((candidate for candidate in csc_candidates if candidate.is_file()), None)
    if csc is None:
        raise RuntimeError("The Windows .NET Framework C# compiler was not found; the installer stub cannot be built.")
    uninstaller = app_root / "Uninstall-CoatSim.exe"
    run([str(csc), "/nologo", "/target:winexe", "/platform:x64", "/optimize+", "/out:" + str(uninstaller),
         "/reference:System.Windows.Forms.dll", "/reference:System.Drawing.dll", "/reference:System.IO.Compression.dll",
         str(CS_SOURCE)], env=env)
    if not uninstaller.is_file():
        raise RuntimeError("The uninstaller did not compile.")
    for item in runtime_distributions:
        # The manifest describes the exact installed runtime inventory from
        # which PyInstaller resolved the modules; it does not copy credentials.
        pass

    # Create a compressed payload ZIP, then append it and a fixed-size footer to
    # a tiny .NET Framework WinForms stub. This avoids a second large SFX tool.
    payload_zip = build_dir / "CoatSim-payload.zip"
    payload_bytes, file_manifest = write_payload_zip(payload_zip, app_root, assets)

    csc_candidates = [
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe",
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework" / "v4.0.30319" / "csc.exe",
    ]
    csc = next((candidate for candidate in csc_candidates if candidate.is_file()), None)
    if csc is None:
        raise RuntimeError("The Windows .NET Framework C# compiler was not found; the installer stub cannot be built.")
    stub = build_dir / "CoatSim-Setup-Stub.exe"
    csc_args = [str(csc), "/nologo", "/target:winexe", "/platform:x64", "/optimize+", "/out:" + str(stub),
                "/reference:System.Windows.Forms.dll", "/reference:System.Drawing.dll", "/reference:System.IO.Compression.dll",
                str(CS_SOURCE)]
    run(csc_args, env=env)
    if not stub.is_file():
        raise RuntimeError("The installer bootstrapper did not compile.")
    footer = b"CSOFSET1" + payload_zip.stat().st_size.to_bytes(8, "little", signed=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as dest, stub.open("rb") as source, payload_zip.open("rb") as payload:
        shutil.copyfileobj(source, dest, 4 * 1024 * 1024)
        shutil.copyfileobj(payload, dest, 4 * 1024 * 1024)
        dest.write(footer)

    metadata_doc = {
        "product": APP_NAME,
        "version": APP_VERSION,
        "platform": "Windows x64",
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "artifact": output.name,
        "artifact_size_bytes": output.stat().st_size,
        "artifact_sha256": sha256(output),
        "payload_zip_size_bytes": payload_zip.stat().st_size,
        "payload_uncompressed_bytes": payload_bytes,
        "python": platform.python_version(),
        "pyinstaller": PYINSTALLER_VERSION,
        "build_command": ".\\.conda\\python.exe tools\\build_offline_installer.py --build-dir " + build_dir.relative_to(ROOT).as_posix() + " --reuse-pyinstaller-runtime " + tool_site.relative_to(ROOT).as_posix() + (
            " --source-snapshot " + snapshot.relative_to(ROOT).as_posix() + " --package-assets " + assets.relative_to(ROOT).as_posix()
            if args.source_snapshot else ""
        ) + (
            " --reuse-pyinstaller-work " + app_work.relative_to(ROOT).as_posix()
            if args.reuse_pyinstaller_work else ""
        ) + (
            " --reuse-pyinstaller-config " + pyinstaller_config.relative_to(ROOT).as_posix()
            if args.reuse_pyinstaller_config else ""
        ) + (
            " --reuse-app-dist " + app_dist.relative_to(ROOT).as_posix()
            if args.reuse_app_dist else ""
        ) + (
            " --data-manifest " + manifest_relpath
            if manifest_relpath else ""
        ),
        "build_inputs": {
            "installer_bootstrap_sha256": sha256(CS_SOURCE),
            "builder_sha256": sha256(Path(__file__).resolve()),
        },
        "build_tool_wheels": build_packages,
        "runtime_distributions": runtime_distributions,
        "source_snapshot": {
            "root": os.path.relpath(snapshot, build_dir).replace("\\", "/"),
            "created_utc": snapshot_created_utc,
            "files": frozen_snapshot_records,
        },
        "static_data_files": copied_data,
        "static_data_revision": data_revision,
        "included_features": ["offline CPU GUI and deposition simulation", "Qt/QML frontend", "film-stack design resources", "optical material catalog", "CIE illuminant/observer data", "optional DirectML backend", "assistant/MCP client code", "per-user path selection", "uninstaller preserves user data"] + (["RII public substrate optical data"] if data_revision else []),
        "minimum_windows_version": "Windows 10 version 1809 or later, or Windows 11",
        "external_requirements": ["Windows 10 version 1809 or later, or Windows 11; Qt uses the Windows ICU runtime", "DirectML GPU execution requires Windows DirectX 12-compatible hardware and a vendor driver", "assistant/model/MCP integrations require their own credentials and, depending on provider, an external CLI or network"],
        "not_included": ["CUDA/CuPy was absent from the source environment", "optional SDK extras absent from the source environment: AG-UI, You.com, TensorBoard, FastMCP OpenAPI adapter, OpenAI aiohttp transport, and standalone MCP CLI", "user runtime databases, data/tmp, results, attachments, logs, personal settings and credentials", "external provider accounts/API keys/CLI tools", "GPU hardware drivers"],
        "signature": "unsigned; no SmartScreen bypass",
        "clean_windows_vm_tested": False,
        "payload_files": file_manifest,
    }
    manifest_path = output.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(metadata_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    checksum_path = output.with_suffix(".sha256")
    checksum_path.write_text(f"{metadata_doc['artifact_sha256']}  {output.name}\n", encoding="ascii")

    print("\nInstaller:", output)
    print("Bytes:", output.stat().st_size)
    print("Payload (uncompressed):", payload_bytes)
    print("SHA-256:", metadata_doc["artifact_sha256"])
    print("Manifest:", manifest_path)
    print("Build tree:", build_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
