"""OS-held project ownership and explicitly reviewed abnormal-session recovery.

Importable without Qt. A lock file is never unlinked: removing a locked inode
would let a second POSIX writer enter. Lock release is always handle-based.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import hashlib
import importlib
import os
from pathlib import Path
import tempfile
from threading import RLock
from typing import Any, BinaryIO
from uuid import uuid4

from coat_sim.core.atomic_io import replace_with_retry
from coat_sim.core.project_store import ProjectError, ProjectStore
from coat_sim.core.project_dependencies import reject_links

MAX_JOURNAL_BYTES = 32 * 1024 * 1024
MAX_RECOVERY_GENERATIONS = 4


def project_owner_runtime_root() -> Path:
    """Fixed per-user local ownership scope; tests can inject/patch this seam."""
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "CoatSim" / "project-owners"
    return Path("/tmp") / f"coat-sim-project-owners-{os.getuid()}"


class HeldFileLock:
    """Cross-platform nonblocking OS lock, released on normal exit or crash."""

    def __init__(self, path: Path):
        self.path = path
        self._stream: BinaryIO | None = None

    def acquire(self) -> bool:
        if self._stream is not None:
            return True
        reject_links(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                msvcrt: Any = importlib.import_module("msvcrt")
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            stream.close()
            return False
        self._stream = stream
        return True

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                if os.name == "nt":
                    msvcrt: Any = importlib.import_module("msvcrt")
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()

    def __enter__(self):
        if not self.acquire():
            raise ProjectError("active_project_owner", "Project already has an active writable owner")
        return self

    def __exit__(self, *_args):
        self.close()


def _read_journal(path: Path) -> dict[str, Any] | None:
    reject_links(path)
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_JOURNAL_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(raw) > MAX_JOURNAL_BYTES:
        raise ProjectError("invalid_recovery", "Session journal exceeds its size limit")
    try:
        value = json.loads(raw)
        _validate_journal(value)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ProjectError("invalid_recovery", "Invalid session recovery journal") from exc
    return value


def _validate_journal(value: Any) -> None:
    if (not isinstance(value, dict) or value.get("kind") != "coat_sim.session_journal"
            or type(value.get("version")) is not int or value["version"] != 1
            or type(value.get("clean")) is not bool
            or not isinstance(value.get("project_id"), str) or not value["project_id"]
            or not isinstance(value.get("generation"), str) or not value["generation"]
            or type(value.get("revision")) is not int or value["revision"] < 0
            or not isinstance(value.get("checkpoint"), dict)):
        raise ValueError("invalid fields")
    json.dumps(value, allow_nan=False)


def _write_recovery_json(path: Path, value: dict[str, Any], *, capacity_code: str = "invalid_recovery",
                         capacity_message: str = "Recovery metadata exceeds its bounded size") -> None:
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError) as exc:
        raise ProjectError("invalid_recovery", "Recovery checkpoint must contain finite JSON") from exc
    if len(raw) > MAX_JOURNAL_BYTES:
        raise ProjectError(capacity_code, capacity_message)
    reject_links(path)
    fd, name = tempfile.mkstemp(prefix=".project-session-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)



class ProjectOwnerLease:
    """Lifetime writable ownership, independent of launcher/assistant registry.

    Acquisition never reads recovery by default. Explicit recovery candidate
    methods can inspect prior journals only after ownership is established.
    """

    def __init__(self, store: ProjectStore, locks: tuple[HeldFileLock, ...], generation: str):
        self.root, self.project_id = store.root, store.manifest["id"]
        self.project_file = store.project_file
        self.generation, self._locks = generation, locks
        self._journal = self.root / ".project-session.json"
        self._archive = self.root / ".project-session-recovery.json"
        self._reviewed_archive = self.root / ".project-session-reviewed-recovery.json"
        self._previous: dict[str, Any] | None = None
        self._previous_read = False
        self._closed = False
        self._metadata_lock = RLock()
        self._recording = False

    @classmethod
    def acquire(cls, project_file: Path | str, project_id: str, generation: str | None = None, *,
                owner_runtime_root: Path | str | None = None):
        store = ProjectStore.open(project_file)
        if not isinstance(project_id, str) or store.manifest["id"] != project_id:
            raise ProjectError("project_identity_mismatch", "Explicit project ID does not match this root")
        # Persisted ID is stable across directory moves and copied roots must
        # not become independent writers until explicitly reidentified. Keep
        # ownership metadata outside user documents: a rejected operation does
        # not add/alter any project bytes. This scope does not use the assistant
        # test/runtime override and survives an assistant restart.
        owner_root = Path(owner_runtime_root) if owner_runtime_root is not None else project_owner_runtime_root()
        reject_links(owner_root)
        owner_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        keys = ["id:" + project_id, "root:" + os.path.normcase(str(store.root.resolve()))]
        locks = tuple(HeldFileLock(owner_root / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".lock"))
                      for key in keys)
        acquired: list[HeldFileLock] = []
        try:
            for lock in locks:
                if not lock.acquire():
                    raise ProjectError("active_project_owner", "This project ID or canonical root has an active writable owner; use its session")
                acquired.append(lock)
        except Exception:
            for previous in reversed(acquired):
                previous.close()
            raise
        try:
            # Reopen after lock admission; an earlier owner may have committed.
            current = ProjectStore.open(store.project_file)
            if current.manifest["id"] != project_id:
                raise ProjectError("project_identity_mismatch", "Project identity changed during admission")
            return cls(current, locks, generation or uuid4().hex)
        except Exception:
            for lock in reversed(locks):
                lock.close()
            raise

    def _require_open(self) -> None:
        if self._closed:
            raise ProjectError("project_closed", "Project ownership has ended")

    def _capture_previous(self) -> None:
        if not self._previous_read:
            self._previous = _read_journal(self._journal)
            self._previous_read = True

    def _read_archive(self) -> list[dict[str, Any]]:
        reject_links(self._archive)
        try:
            with self._archive.open("rb") as stream:
                raw = stream.read(MAX_JOURNAL_BYTES + 1)
        except FileNotFoundError:
            return []
        if len(raw) > MAX_JOURNAL_BYTES:
            raise ProjectError("invalid_recovery", "Recovery archive exceeds its size limit")
        try:
            value = json.loads(raw)
            records = value.get("records") if isinstance(value, dict) else None
            if (not isinstance(value, dict) or value.get("kind") != "coat_sim.session_recovery_archive"
                    or type(value.get("version")) is not int or value["version"] != 1
                    or value.get("project_id") != self.project_id or not isinstance(records, list)
                    or len(records) > MAX_RECOVERY_GENERATIONS):
                raise ValueError("invalid archive")
            for row in records:
                _validate_journal(row)
                if row["project_id"] != self.project_id or row["clean"]:
                    raise ValueError("wrong archive project/clean state")
            if len({row["generation"] for row in records}) != len(records):
                raise ValueError("duplicate archive generation")
            json.dumps(value, allow_nan=False)
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ProjectError("invalid_recovery", "Invalid preserved session recovery archive") from exc
        return records

    def _write_archive(self, records: list[dict[str, Any]]) -> None:
        if len(records) > MAX_RECOVERY_GENERATIONS:
            raise ProjectError("recovery_archive_full", "Review/discard preserved recovery candidates before adding another session")
        _write_recovery_json(self._archive, {"kind": "coat_sim.session_recovery_archive", "version": 1,
                                          "project_id": self.project_id, "records": records}, capacity_code="recovery_archive_full")

    def _read_reviewed_archive(self) -> list[dict[str, Any]]:
        reject_links(self._reviewed_archive)
        try:
            with self._reviewed_archive.open("rb") as stream:
                raw = stream.read(MAX_JOURNAL_BYTES + 1)
        except FileNotFoundError:
            return []
        if len(raw) > MAX_JOURNAL_BYTES:
            raise ProjectError("invalid_recovery", "Reviewed recovery archive exceeds its size limit")
        try:
            value = json.loads(raw)
            records = value.get("records") if isinstance(value, dict) else None
            if (not isinstance(value, dict) or value.get("kind") != "coat_sim.reviewed_session_recovery_archive"
                    or type(value.get("version")) is not int or value["version"] != 1
                    or value.get("project_id") != self.project_id or not isinstance(records, list)):
                raise ValueError("invalid reviewed archive")
            for row in records:
                if (not isinstance(row, dict) or row.get("kind") != "coat_sim.reviewed_session_recovery"
                        or type(row.get("version")) is not int or row["version"] != 1
                        or row.get("project_id") != self.project_id
                        or not isinstance(row.get("record_id"), str) or not row["record_id"]
                        or not isinstance(row.get("generation"), str) or not row["generation"]
                        or type(row.get("revision")) is not int or row["revision"] < 0
                        or not isinstance(row.get("reviewed_at"), str)
                        or row.get("section") not in {None, "film", "resources", "compute"}
                        or not isinstance(row.get("checkpoint"), dict)):
                    raise ValueError("invalid reviewed record")
                timestamp = datetime.fromisoformat(row["reviewed_at"])
                if timestamp.tzinfo is None:
                    raise ValueError("reviewed timestamp lacks timezone")
                if row["section"] is not None:
                    checkpoint = row["checkpoint"]
                    if (checkpoint.get("kind") != "coat_sim.project_recovery_checkpoint"
                            or type(checkpoint.get("version")) is not int or checkpoint["version"] != 1
                            or checkpoint.get("project_id") != self.project_id
                            or row["section"] not in checkpoint):
                        raise ValueError("invalid reviewed section payload")
            if len({row["record_id"] for row in records}) != len(records):
                raise ValueError("duplicate reviewed record")
            json.dumps(value, allow_nan=False)
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ProjectError("invalid_recovery", "Invalid reviewed recovery archive") from exc
        return records

    def _write_reviewed_archive(self, records: list[dict[str, Any]]) -> None:
        _write_recovery_json(
            self._reviewed_archive,
            {"kind": "coat_sim.reviewed_session_recovery_archive", "version": 1,
             "project_id": self.project_id, "records": records},
            capacity_code="reviewed_recovery_archive_full",
            capacity_message=("Reviewed recovery archive is full; no draft was changed. "
                              "Back up this project's .project-session-reviewed-recovery.json before retrying."),
        )

    @staticmethod
    def _checkpoint_has_recovery_sections(checkpoint: dict[str, Any]) -> bool:
        return any(key not in {"kind", "version", "project_id"} for key in checkpoint)

    def recovery_candidates(self) -> list[dict[str, Any]]:
        """Review all retained abnormal generations, oldest first; never apply."""
        with self._metadata_lock:
            self._require_open()
            self._capture_previous()
            records = self._read_archive()
            prior = self._previous
            if prior is not None and not prior["clean"] and prior["generation"] != self.generation:
                if prior["project_id"] != self.project_id:
                    raise ProjectError("stale_recovery", "Recovery metadata belongs to another project identity")
                if not any(row["generation"] == prior["generation"] for row in records):
                    records.append(prior)
            current = ProjectStore.open(self.project_file).manifest
            return [{**deepcopy(row), "stale": row["revision"] != current["revision"]} for row in records]

    def recovery_candidate(self) -> dict[str, Any] | None:
        """Compatibility view: newest pending generation; stale data is rejected."""
        with self._metadata_lock:
            records = self.recovery_candidates()
            if not records:
                return None
            value = records[-1]
            if value.pop("stale"):
                raise ProjectError("stale_recovery", "Session recovery does not match the saved project revision")
            return value

    def reviewed_recovery_candidates(self) -> list[dict[str, Any]]:
        """Return previously reviewed data for explicit inspection; never apply it."""
        with self._metadata_lock:
            self._require_open()
            return deepcopy(self._read_reviewed_archive())

    def preserve_reviewed_recovery(self, generation: str, section: str | None, *,
                                   expected_revision: int) -> dict[str, Any]:
        """Archive a reviewed candidate or one section before removing it from pending recovery.

        The reviewed archive is written first. If a later source rewrite fails,
        the candidate remains available as well, so interruption can duplicate
        data but never lose it. A section-specific action preserves its siblings.
        """
        with self._metadata_lock:
            self._require_open()
            if (not isinstance(generation, str) or not generation or generation == self.generation
                    or type(expected_revision) is not int or expected_revision < 0
                    or (section is not None and (not isinstance(section, str)
                                                  or section not in {"film", "resources", "compute"}))):
                raise ProjectError("invalid_recovery", "Choose a retained prior recovery generation and section")

            candidates = self.recovery_candidates()
            candidate = next((row for row in candidates if row["generation"] == generation), None)
            if candidate is None or candidate["revision"] != expected_revision:
                raise ProjectError("stale_recovery", "The selected recovery generation changed; review it again")
            checkpoint = candidate["checkpoint"]
            if section is not None:
                if (checkpoint.get("kind") != "coat_sim.project_recovery_checkpoint"
                        or type(checkpoint.get("version")) is not int or checkpoint["version"] != 1
                        or checkpoint.get("project_id") != self.project_id or section not in checkpoint):
                    raise ProjectError("no_recovery", "The selected recovery section is no longer available")
                archived_checkpoint = {key: deepcopy(checkpoint[key])
                                       for key in ("kind", "version", "project_id", section)}
            else:
                archived_checkpoint = deepcopy(checkpoint)

            reviewed = self._read_reviewed_archive()
            duplicate = next((row for row in reviewed
                              if row["generation"] == generation
                              and row["revision"] == expected_revision
                              and row["section"] == section
                              and row["checkpoint"] == archived_checkpoint), None)
            if duplicate is None:
                record = {
                    "kind": "coat_sim.reviewed_session_recovery", "version": 1,
                    "record_id": uuid4().hex, "project_id": self.project_id,
                    "generation": generation, "revision": expected_revision,
                    "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "section": section, "checkpoint": archived_checkpoint,
                }
                # Commit the retained copy before changing either source file.
                self._write_reviewed_archive([*reviewed, record])

            archived_rows = self._read_archive()
            archive_index = next((index for index, row in enumerate(archived_rows)
                                  if row["generation"] == generation), None)
            prior = self._previous
            journal = _read_journal(self._journal)
            journal_matches = journal is not None and journal["generation"] == generation and not journal["clean"]
            if archive_index is None and not journal_matches:
                raise ProjectError("stale_recovery", "The selected recovery generation is no longer retained")

            updated_checkpoint = deepcopy(checkpoint)
            if section is None:
                updated_checkpoint = None
            else:
                updated_checkpoint.pop(section, None)
                if not self._checkpoint_has_recovery_sections(updated_checkpoint):
                    updated_checkpoint = None

            # Remove/update the archive copy first. If a journal copy of the same
            # generation also exists, update it afterward; a crash between writes
            # can only leave an extra recoverable copy.
            if archive_index is not None:
                if updated_checkpoint is None:
                    archived_rows.pop(archive_index)
                else:
                    archived_rows[archive_index] = {**archived_rows[archive_index],
                                                    "checkpoint": updated_checkpoint}
                self._write_archive(archived_rows)

            if journal_matches and journal is not None:
                if updated_checkpoint is None:
                    _write_recovery_json(self._journal, {**journal, "clean": True, "checkpoint": {}})
                else:
                    _write_recovery_json(self._journal, {**journal, "checkpoint": updated_checkpoint})

            if prior is not None and prior["generation"] == generation:
                if updated_checkpoint is None:
                    self._previous = None
                else:
                    self._previous = {**prior, "checkpoint": updated_checkpoint}

            return {
                "record_id": duplicate["record_id"] if duplicate is not None else record["record_id"],
                "generation": generation, "revision": expected_revision, "section": section,
            }

    def discard_recovery(self, generation: str) -> bool:
        """Compatibility alias that moves a full generation to reviewed storage.

        The legacy name is retained for callers that acknowledge an abnormal
        candidate, but no recovery data is permanently discarded.
        """
        with self._metadata_lock:
            self._require_open()
            if not isinstance(generation, str) or not generation or generation == self.generation:
                raise ProjectError("invalid_recovery", "Choose a retained prior session generation")
            candidate = next((row for row in self.recovery_candidates()
                              if row["generation"] == generation), None)
            if candidate is None:
                return False
            self.preserve_reviewed_recovery(
                generation, None, expected_revision=candidate["revision"])
            return True

    def checkpoint(self, checkpoint: dict[str, Any]) -> None:
        with self._metadata_lock:
            if self._closed:
                raise ProjectError("project_closed", "Project ownership has ended")
            if not isinstance(checkpoint, dict):
                raise ProjectError("invalid_recovery", "Recovery checkpoint must be an explicit object")
            # Validate before archiving anything: a rejected checkpoint changes no
            # recovery bytes and cannot consume bounded archive capacity.
            manifest = ProjectStore.open(self.project_file).manifest
            if manifest["id"] != self.project_id:
                raise ProjectError("project_identity_mismatch", "Project identity changed")
            value = {"kind": "coat_sim.session_journal", "version": 1,
                     "project_id": self.project_id, "revision": manifest["revision"],
                     "generation": self.generation, "clean": False, "checkpoint": checkpoint}
            try:
                raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (ValueError, TypeError) as exc:
                raise ProjectError("invalid_recovery", "Recovery checkpoint must contain finite JSON") from exc
            if len(raw) > MAX_JOURNAL_BYTES:
                raise ProjectError("invalid_recovery", "Recovery checkpoint exceeds its size limit")
            # Keep prior abnormal drafts durably before publishing this session.
            self._capture_previous()
            prior = self._previous
            if prior is not None and not prior["clean"] and prior["generation"] != self.generation:
                if prior["project_id"] != self.project_id:
                    raise ProjectError("stale_recovery", "Previous journal belongs to another project identity")
                records = self._read_archive()
                if not any(row["generation"] == prior["generation"] for row in records):
                    self._write_archive([*records, deepcopy(prior)])
            self._write(clean=False, checkpoint=checkpoint)
            self._recording = True

    def _write(self, *, clean: bool, checkpoint: dict[str, Any]) -> None:
        manifest = ProjectStore.open(self.project_file).manifest
        if manifest["id"] != self.project_id:
            raise ProjectError("project_identity_mismatch", "Project identity changed")
        value = {"kind": "coat_sim.session_journal", "version": 1,
                 "project_id": self.project_id, "revision": manifest["revision"],
                 "generation": self.generation, "clean": clean, "checkpoint": checkpoint,
                 "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        try:
            json.dumps(checkpoint, allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise ProjectError("invalid_recovery", "Recovery checkpoint must contain finite JSON") from exc
        _write_recovery_json(self._journal, value)

    def close(self, *, clean: bool = True) -> None:
        with self._metadata_lock:
            if self._closed:
                return
            try:
                if clean and self._recording:
                    self._write(clean=True, checkpoint={})
            finally:
                self._closed = True
                for lock in reversed(self._locks):
                    lock.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_args):
        self.close(clean=exc_type is None)
