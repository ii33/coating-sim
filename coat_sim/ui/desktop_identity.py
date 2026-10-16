"""Generate and explicitly register this project's Linux desktop entries.

Normal application launch performs no desktop registration. Registration is
an explicit CLI action using the standard per-user applications directory; it
never changes environment variables, autostart or system-wide settings.

https://specifications.freedesktop.org/desktop-entry-spec/latest/
https://doc.qt.io/qt-6/qguiapplication.html#desktopFileName-prop
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

from coat_sim.ui.taskbar import APP_NAME, MODULE_NAMES, application_id
from coat_sim.core.atomic_io import replace_with_retry

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _value(text: str) -> str:
    return (text.replace("\\", "\\\\").replace("\n", "\\n")
            .replace("\r", "\\r").replace("\t", "\\t"))


def _exec_argument(text: str) -> str:
    # Desktop Exec is not a shell command. Apply its double-quote escaping,
    # then desktop-entry string escaping; percent must remain literal.
    escaped = "".join("\\" + char if char in '\\"`$' else char for char in text)
    return _value('"' + escaped.replace("%", "%%") + '"')


def _write_atomic(path: Path, contents: bytes) -> None:
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=path.name + ".",
                                         suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            stream.write(contents)
        replace_with_retry(temporary, path)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def write_project_desktop_entry(module: str, icon_path: Path,
                                *, root: Path = PROJECT_ROOT) -> Path:
    if module not in MODULE_NAMES:
        raise ValueError(f"Unknown application module: {module}")
    root = root.resolve()
    folder = root / "artifacts" / "desktop"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (application_id(module) + ".desktop")
    # Keep the virtualenv interpreter spelling instead of resolving its symlink.
    command = [str(Path(sys.executable).absolute()), str(root / "launcher.py")]
    if module != "launcher":
        command.extend(["--module", module])
    # Cloud desktop supports the software renderer on subsequent dock launches.
    if "--software" in sys.argv:
        command.append("--software")
    name = {"mask": "修正板", "main": "膜厚仿真"}.get(module, MODULE_NAMES.get(module, APP_NAME))
    contents = "\n".join([
        "[Desktop Entry]", "Type=Application", "Version=1.0",
        "Name=" + _value(name), "Exec=" + " ".join(map(_exec_argument, command)),
        "Path=" + _value(str(root)), "Icon=" + _value(str(icon_path.resolve())),
        "StartupWMClass=" + application_id(module),
        "Terminal=false", "Categories=Science;", "StartupNotify=false", "",
    ]).encode("utf-8")
    if path.is_file() and path.read_bytes() == contents:
        return path
    _write_atomic(path, contents)
    return path


def install_desktop_entries(modules: list[str], *, root: Path = PROJECT_ROOT,
                            data_home: Path | None = None) -> Path:
    """Explicit opt-in installer. Return the backup/rollback manifest path."""
    if not modules or any(module not in MODULE_NAMES for module in modules):
        raise ValueError("Select known application modules")
    root = root.resolve()
    home = data_home if data_home is not None else Path(
        os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    if not home.is_absolute():
        raise ValueError("The user data directory must be absolute")
    target_dir = home / "applications"
    if target_dir.is_symlink():
        raise ValueError("Refusing to replace entries through an applications-directory symlink")
    target_dir.mkdir(parents=True, exist_ok=True)
    backup_dir = root / "artifacts" / "desktop" / ("registration-" + uuid.uuid4().hex)
    backup_dir.mkdir(parents=True)
    manifest_path = backup_dir / "manifest.json"
    manifest = {"version": 1, "applications_directory": str(target_dir), "entries": []}
    _write_atomic(manifest_path,json.dumps(manifest,ensure_ascii=False,indent=2).encode())
    for module in dict.fromkeys(modules):
        source = write_project_desktop_entry(module, root / "coat_sim/ui/assets/icons/logolite.png", root=root)
        contents = source.read_bytes()
        destination = target_dir / source.name
        if destination.is_symlink():
            raise ValueError(f"Refusing to replace symlink: {destination}; manifest: {manifest_path}")
        backup = None
        if destination.exists():
            backup = backup_dir / source.name
            backup.write_bytes(destination.read_bytes())
        manifest["entries"].append({"path":str(destination),
            "backup":str(backup) if backup else None,
            "installed_sha256":hashlib.sha256(contents).hexdigest()})
        # Persist rollback information before installing each individual entry.
        _write_atomic(manifest_path,json.dumps(manifest,ensure_ascii=False,indent=2).encode())
        _write_atomic(destination,contents)
    return manifest_path


def restore_desktop_entries(manifest_path: Path) -> None:
    """Explicit rollback; refuse to overwrite a registration changed since install."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("version") != 1:
        raise ValueError("Unknown registration manifest")
    target_dir = Path(manifest["applications_directory"])
    entries = manifest["entries"]
    allowed = {application_id(key) + ".desktop" for key in MODULE_NAMES}
    for item in entries:
        destination = Path(item["path"])
        if destination.parent != target_dir or destination.name not in allowed or destination.is_symlink():
            raise ValueError("Invalid rollback target")
        if not destination.exists() or hashlib.sha256(destination.read_bytes()).hexdigest() != item["installed_sha256"]:
            raise ValueError(f"Registration changed since install; keep it untouched: {destination}")
        if item["backup"] and Path(item["backup"]).parent != manifest_path.parent:
            raise ValueError("Invalid backup path")
    for item in entries:
        destination = Path(item["path"])
        if item["backup"]:
            _write_atomic(destination,Path(item["backup"]).read_bytes())
        else:
            destination.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="Install only this project's per-user Linux desktop entries")
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--install",action="store_true")
    operation.add_argument("--restore",type=Path,metavar="MANIFEST")
    parser.add_argument("--module",choices=tuple(MODULE_NAMES),action="append")
    parser.add_argument("--software",action="store_true")
    options = parser.parse_args()
    if options.restore:
        restore_desktop_entries(options.restore)
        print("Restored desktop registrations from " + str(options.restore))
    else:
        manifest = install_desktop_entries(options.module or list(MODULE_NAMES))
        print("Installed project desktop entries; rollback manifest: " + str(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
