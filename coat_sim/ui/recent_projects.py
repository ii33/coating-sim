"""Persistent, user-wide history of recently opened project descriptors.

This is deliberately separate from the live project-session list: entries remain
available after a window or process closes, and deleting a history row never
touches the project itself.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Callable, Iterator

from PySide6.QtCore import QLockFile, QSettings, QStandardPaths


_SETTINGS_GROUP = "recentProjects/v1"
_SETTINGS_KEY = "entries"
_LOCK_TIMEOUT_MS = 5000


def _canonical_descriptor(descriptor: str | os.PathLike[str]) -> str:
    raw = os.fspath(descriptor).strip()
    if not raw:
        raise ValueError("请选择 .coatproj 工程文件。")
    path = Path(raw).expanduser()
    try:
        path = path.resolve(strict=False)
    except (OSError, RuntimeError):
        path = Path(os.path.abspath(path))
    if path.suffix.casefold() != ".coatproj":
        raise ValueError("请选择 .coatproj 工程文件。")
    return str(path)


def _identity(descriptor: str) -> str:
    """Return a comparison key respecting platform path case semantics."""
    return os.path.normcase(os.path.normpath(descriptor))


class RecentProjects:
    """Read and update the launcher history stored in a dedicated QSettings group.

    `settings` is injectable so callers and tests can provide an explicitly
    isolated QSettings instance. Production instances share the application's
    existing LYX/coat-sim settings store, under `recentProjects/v1`.
    """

    def __init__(self, settings: QSettings | None = None):
        self._settings = settings if settings is not None else QSettings("LYX", "coat-sim")

    def worker_factory(self) -> Callable[[], RecentProjects]:
        """Capture metadata only; each worker creates its own QSettings QObject."""
        settings = self._settings
        format_ = settings.format()
        organization, application = settings.organizationName(), settings.applicationName()
        if organization:
            scope = settings.scope()
            return lambda: RecentProjects(QSettings(format_, scope, organization, application))
        filename = settings.fileName()
        return lambda: RecentProjects(QSettings(filename, format_))

    def _lock_filename(self) -> str:
        # File-backed QSettings (including the INI settings used in tests) can
        # share an adjacent lock. Native registry/plist backends use the same
        # per-app config directory on every process instead.
        filename = ""
        try:
            filename = str(self._settings.fileName())
        except (AttributeError, RuntimeError):
            pass
        if filename:
            path = Path(filename)
            if path.is_absolute():
                return str(path.with_name(path.name + ".recent-projects.lock"))
        location = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.GenericConfigLocation)
        config_dir = Path(location) if location else Path.home() / ".config"
        return str(config_dir / "LYX" / "coat-sim" / "recent-projects.lock")

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        lock_path = Path(self._lock_filename())
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(lock_path))
        lock.setStaleLockTime(30_000)
        if not lock.tryLock(_LOCK_TIMEOUT_MS):
            raise RuntimeError("最近项目列表正由另一个进程更新，请稍后重试。")
        try:
            yield
        finally:
            lock.unlock()

    def _sync(self) -> None:
        self._settings.sync()
        if self._settings.status() != QSettings.Status.NoError:
            raise OSError("无法读取或保存最近项目设置。")

    def _read_records(self) -> list[dict[str, str]]:
        self._sync()
        self._settings.beginGroup(_SETTINGS_GROUP)
        try:
            raw: Any = self._settings.value(_SETTINGS_KEY, [])
        finally:
            self._settings.endGroup()
        if not isinstance(raw, (list, tuple)):
            return []

        records: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            descriptor = item.get("descriptorFile")
            name = item.get("name")
            last_opened = item.get("lastOpened")
            if not isinstance(descriptor, str) or not isinstance(last_opened, str):
                continue
            try:
                canonical = _canonical_descriptor(descriptor)
            except (TypeError, ValueError, OSError):
                continue
            identity = _identity(canonical)
            if identity in seen:
                continue
            seen.add(identity)
            records.append({
                "descriptorFile": canonical,
                "name": str(name or Path(canonical).stem),
                "lastOpened": last_opened,
            })
        records.sort(key=lambda row: row["lastOpened"], reverse=True)
        return records

    def _write_records(self, records: list[dict[str, str]]) -> None:
        # Rebuild the small value explicitly so no availability or other
        # session-derived state ever leaks into persistent user settings.
        stored = [{"descriptorFile": row["descriptorFile"],
                   "name": row["name"],
                   "lastOpened": row["lastOpened"]}
                  for row in records]
        self._settings.beginGroup(_SETTINGS_GROUP)
        try:
            self._settings.setValue(_SETTINGS_KEY, stored)
        finally:
            self._settings.endGroup()
        self._sync()

    def entries(self) -> list[dict[str, Any]]:
        """Return newest-first rows, with availability calculated from disk."""
        with self._exclusive():
            records = self._read_records()
        rows: list[dict[str, Any]] = []
        for record in records:
            descriptor = record["descriptorFile"]
            path = Path(descriptor)
            row: dict[str, Any] = {**record,
                "available": path.suffix.casefold() == ".coatproj" and path.is_file()}
            if row["available"]:
                try:
                    # Descriptor-only read on the history worker. Never opens a
                    # writable store, follows resource files or acquires a lease.
                    from coat_sim.core.project_dependencies import read_json
                    from coat_sim.core.project_store import _manifest, project_file_root
                    project_file_root(path)
                    manifest, _ = read_json(path)
                    manifest = _manifest(manifest)
                    row.update(name=manifest["name"], summary=manifest.get("summary", ""),
                               notes=manifest.get("notes", ""), projectId=manifest["id"],
                               informationAvailable=True)
                except Exception:
                    # Old/invalid entries retain their last successful name.
                    # Information availability never certifies runtime admission.
                    pass
            rows.append(row)
        return rows

    def record(self, descriptor: str | os.PathLike[str], name: str) -> None:
        """Promote a descriptor to the top of the history, deduplicating canonically."""
        canonical = _canonical_descriptor(descriptor)
        display_name = str(name).strip() or Path(canonical).stem
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        identity = _identity(canonical)
        with self._exclusive():
            records = [row for row in self._read_records()
                       if _identity(row["descriptorFile"]) != identity]
            records.insert(0, {"descriptorFile": canonical,
                                "name": display_name,
                                "lastOpened": now})
            self._write_records(records)

    def remove(self, descriptor: str | os.PathLike[str]) -> None:
        """Remove a descriptor from history only; the project file is never deleted."""
        canonical = _canonical_descriptor(descriptor)
        identity = _identity(canonical)
        with self._exclusive():
            records = [row for row in self._read_records()
                       if _identity(row["descriptorFile"]) != identity]
            self._write_records(records)

    def refresh(self) -> None:
        """Synchronize QSettings; availability is derived afresh by `entries()`."""
        with self._exclusive():
            self._sync()

