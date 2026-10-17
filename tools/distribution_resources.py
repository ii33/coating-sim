"""Application source/resource staging and byte validation, without installation or runtime provisioning."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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
    """Freeze application sources for distribution resource validation."""
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
    # resource selection never copies existing mutable contents.
    for folder in ("machines", "masks", "processes", "sources", "substrates", "results", "tmp"):
        (target / folder).mkdir(parents=True, exist_ok=True)
    return copied


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
        (source_root / "docs" / "installation.md", "docs"),
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


def write_resource_zip(payload_zip: Path, app_root: Path, assets: Path) -> tuple[int, list[dict[str, object]]]:
    """Create a resource archive only after validating both application trees."""
    validate_package_tree(app_root, context="final application payload")
    validate_package_tree(assets, context="final package assets")
    payload_bytes = 0
    file_manifest: list[dict[str, object]] = []
    with zipfile.ZipFile(payload_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
        for path in sorted(app_root.rglob("*"), key=lambda p: p.relative_to(app_root).as_posix().casefold()):
            if path.is_symlink():
                raise RuntimeError(f"Application resources contain an unexpected link: {path.name}")
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
