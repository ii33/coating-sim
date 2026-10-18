"""Application source/resource staging and byte validation, without installation or runtime provisioning."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
STATIC_MANIFEST = Path(__file__).with_name("distribution_static_manifest.json")


def sha256(path: Path) -> str:
    _validate_path(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_package_relative_path(relative: Path | PurePosixPath | str, *, context: str) -> None:
    """Reject private design paths from any source or payload tree."""
    parts = PurePosixPath(str(relative).replace("\\", "/")).parts
    folded = tuple(part.casefold() for part in parts)
    if any(folded[index:index + 2] == ("data", "research") for index in range(len(folded) - 1)):
        raise RuntimeError("Refusing to package local research evidence path in " + context)
    if any(part.casefold() == "sheji" for part in parts):
        raise RuntimeError(f"Refusing to package private design path in {context}: {PurePosixPath(*parts).as_posix()}")


def _validate_path(path: Path) -> None:
    """Reject links/reparse points in every existing ancestor, without following them."""
    if ".." in path.parts:
        raise RuntimeError("Refusing parent traversal in a package path")
    absolute = Path(os.path.abspath(path))
    for item in (*reversed(absolute.parents), absolute):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RuntimeError("Refusing to package a link or reparse point")
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise RuntimeError("Refusing to package a non-regular resource")


def validate_package_tree(root: Path, *, context: str) -> None:
    """Validate roots, ancestors and members before copying, hashing or archiving."""
    _validate_path(root)
    if not root.exists():
        return
    for item in root.rglob("*"):
        _validate_path(item)
        _validate_package_relative_path(item.relative_to(root), context=context)


def copy_tree(source: Path, destination: Path) -> None:
    validate_package_tree(source, context=f"source tree {source}")
    validate_package_tree(destination, context="copy destination")
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
    """Freeze application sources for distribution resource validation."""
    _validate_path(destination)
    _validate_path(ROOT)
    destination.mkdir(parents=True, exist_ok=False)
    copy_tree(ROOT / "coat_sim", destination / "coat_sim")
    for relative in (
        Path("launcher.py"), Path("launcher.pyw"), Path("README.md"), Path("LICENSE"),
        Path("tools") / "launch_desktop.pyw", Path("tools") / "build_desktop_host.py",
        Path("docs") / "algorithms.html", Path("docs") / "software_manual.html",
        # Preserve the local targets for the package README and software-manual
        # help entry; these are distinct from the portable-runtime ZIP itself.
        Path("docs") / "portable-bundle.md", Path("docs") / "portable-assets.md",
        Path("docs") / "installation.md",
        Path("docs") / "assistant-knowledge-contract.md",
        Path("docs") / "assistant-memory-research-contract.md",
        Path("docs") / "refractiveindex-local-contract.md",
        Path("docs") / "refractiveindex-tools-contract.md",
        Path("docs") / "refractiveindex-browser-contract.md",
        Path("docs") / "vendor-optical-catalog-contract.md",
        Path("docs") / "reference-sources.md",
    ):
        source = ROOT / relative
        _validate_path(source)
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


def _static_resources() -> dict[str, str]:
    _validate_path(STATIC_MANIFEST)
    manifest = json.loads(STATIC_MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("schema") != 1 or not isinstance(manifest.get("files"), dict):
        raise RuntimeError("Invalid static resource manifest")
    files = manifest["files"]
    for name, digest in files.items():
        relative = PurePosixPath(name)
        if (relative.is_absolute() or not relative.parts or ".." in relative.parts
                or "\\" in name or ":" in name or relative.as_posix() != name
                or not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
            raise RuntimeError("Invalid static resource manifest entry")
        _validate_package_relative_path(name, context="static manifest")
    if not files:
        raise RuntimeError("Empty static resource manifest")
    return files


def _reference_digest(path: Path) -> str:
    _validate_path(path)
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def prepare_static_data(stage: Path, source_root: Path = ROOT) -> list[str]:
    """Stage only reviewed, content-pinned public seeds; never reuse existing data.

    The manifest is maintained with the packaging code, not taken from an input
    installation. Writable materials and overrides are intentionally absent.
    Existing staging data is rejected without deleting or changing its contents.
    """
    data = source_root / "data"
    target = stage / "data"
    _validate_path(data)
    _validate_path(target)
    if target.exists():
        raise FileExistsError("Static data staging requires a fresh data directory")
    resources = _static_resources()
    # Reject aliases even in otherwise unselected writable material storage.
    for folder in ("materials", "optical_catalog_sources", "film_design/cie-illuminants-20261006"):
        validate_package_tree(data / folder, context="static resource inputs")
    for relative, digest in resources.items():
        source = data / relative
        _validate_path(source)
        if not source.is_file() or _reference_digest(source) != digest:
            raise RuntimeError("Missing or modified reviewed static resource: " + relative)
    stage.mkdir(parents=True, exist_ok=True)
    # Build privately and publish only a complete validated data tree.
    with tempfile.TemporaryDirectory(prefix=".static-data-", dir=stage) as temporary:
        prepared = Path(temporary) / "data"
        prepared.mkdir()
        for relative, digest in resources.items():
            source = data / relative
            _validate_path(source)
            destination = prepared / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination, follow_symlinks=False)
            if _reference_digest(destination) != digest:
                raise RuntimeError("Static resource changed during staging")
        for folder in ("materials", "machines", "masks", "processes", "sources", "substrates", "results", "tmp"):
            (prepared / folder).mkdir(parents=True, exist_ok=True)
        validate_package_tree(prepared, context="staged static resources")
        _validate_path(target)
        if target.exists():
            raise FileExistsError("Static data stage appeared during preparation")
        prepared.rename(target)
    return ["data/" + relative for relative in resources]


def add_qt_and_source_data_arguments(command: list[str], data_stage: Path, source_root: Path) -> None:
    from PySide6.QtCore import QLibraryInfo
    qml = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.QmlImportsPath))
    if not qml.is_dir():
        raise FileNotFoundError("PySide6 Qt QML resources are not installed in the project environment")
    # The offline reference reader loads PyYAML lazily to keep status read-only.
    command += ["--hidden-import", "yaml"]
    mappings = [
        (source_root / "coat_sim" / "version.txt", "coat_sim"),
        (source_root / "coat_sim" / "ui" / "qml", "coat_sim/ui/qml"),
        (source_root / "coat_sim" / "ui" / "assets", "coat_sim/ui/assets"),
        (source_root / "coat_sim" / "agent" / "resources", "coat_sim/agent/resources"),
        (source_root / "coat_sim" / "core" / "resources", "coat_sim/core/resources"),
        (source_root / "docs" / "algorithms.html", "docs"),
        (source_root / "docs" / "software_manual.html", "docs"),
        (source_root / "docs" / "portable-bundle.md", "docs"),
        (source_root / "docs" / "portable-assets.md", "docs"),
        (source_root / "docs" / "installation.md", "docs"),
        (source_root / "docs" / "assistant-knowledge-contract.md", "docs"),
        (source_root / "docs" / "assistant-memory-research-contract.md", "docs"),
        (source_root / "docs" / "refractiveindex-local-contract.md", "docs"),
        (source_root / "docs" / "refractiveindex-tools-contract.md", "docs"),
        (source_root / "docs" / "refractiveindex-browser-contract.md", "docs"),
        (source_root / "docs" / "vendor-optical-catalog-contract.md", "docs"),
        (source_root / "docs" / "reference-sources.md", "docs"),
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
        validate_package_tree(source, context="package resource mapping")
        if not source.exists():
            if source.name == "software_manual.html":
                continue
            raise FileNotFoundError(f"Required package resource is missing: {source.name}")
        command += ["--add-data", str(source) + os.pathsep + destination]


def write_resource_zip(payload_zip: Path, app_root: Path, assets: Path) -> tuple[int, list[dict[str, object]]]:
    """Validate both trees and atomically publish a complete resource archive."""
    validate_package_tree(app_root, context="final application payload")
    validate_package_tree(assets, context="final package assets")
    _validate_path(payload_zip)
    for root in (app_root, assets):
        if payload_zip.absolute().is_relative_to(root.absolute()):
            raise RuntimeError("Resource archive must be outside input trees")
    payload_bytes = 0
    file_manifest: list[dict[str, object]] = []
    descriptor, name = tempfile.mkstemp(prefix=".resource-", suffix=".zip", dir=payload_zip.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
            for root, prefix in ((app_root, ""), (assets, "package-info/")):
                for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix().casefold()):
                    _validate_path(path)
                    relative = prefix + path.relative_to(root).as_posix()
                    _validate_package_relative_path(relative, context="final resource archive")
                    if path.is_dir():
                        archive.writestr(relative.rstrip("/") + "/", b"")
                    else:
                        archive.write(path, relative)
                        size = path.stat().st_size
                        payload_bytes += size
                        file_manifest.append({"path": relative, "size_bytes": size, "sha256": sha256(path)})
        _validate_path(payload_zip)
        temporary.replace(payload_zip)
    finally:
        temporary.unlink(missing_ok=True)
    return payload_bytes, file_manifest
