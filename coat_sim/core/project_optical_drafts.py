"""Qt-free project optical draft ownership and request-ID completion barriers."""
from __future__ import annotations

from copy import deepcopy
import re
import threading
from typing import Any, Protocol
from uuid import uuid4

from coat_sim.project_session import ProjectSessionError

from .project_dependencies import ProjectError
from .project_optical_recovery import (
    MODULE_KINDS, detached, digest, native_record, snapshot_record,
    validate_raw, validate_record,
)


class OpticalDraftProvider(Protocol):
    """A disposable presentation for one project-owned editable input.

    Save/restore return their actual request IDs. The provider calls the
    workspace completion method only after the operation has really finished.
    Neither accepted booleans nor mismatched/late completions satisfy a barrier.
    """

    def snapshot_input_draft(self) -> dict: ...
    def restore_input_draft(self, record: dict) -> str | bool: ...
    def save_input_draft(self) -> str | None: ...
    def input_draft_operation_id(self) -> str: ...


class ProjectOpticalDrafts:
    def __init__(self, workspace):
        self.workspace = workspace
        self._lock = threading.RLock()
        self.providers: dict[str, OpticalDraftProvider] = {}
        self.held: dict[str, dict] = {}
        self._saves: dict[str, dict] = {}
        self._restores: dict[str, dict] = {}
        self._starting: dict[tuple[str, str], list[tuple]] = {}
        self._restore_status: dict[str, Any] = {"pending": False, "success": True, "error": "", "token": ""}
        self._restore_history: dict[str, dict] = {}
        self._restore_batch: dict | None = None

    def _live(self):
        # Generation checking belongs to the host, never to a provider's claim.
        return self.workspace.host.operation_scope(self.workspace.binding)

    def _is_live(self):
        try:
            return self.workspace.host.resource_for(self.workspace.binding, "compute_workspace") is self.workspace
        except ProjectSessionError:
            return False

    def records(self):
        with self._lock:
            providers, held = dict(self.providers), deepcopy(self.held)
        for module, provider in providers.items():
            held[module] = validate_raw(provider.snapshot_input_draft(), module)
        return held

    def register(self, module, provider):
        if module not in MODULE_KINDS:
            raise ProjectError("invalid_module", "Unknown optical draft module")
        if any(not callable(getattr(provider, name, None)) for name in
               ("snapshot_input_draft", "restore_input_draft", "save_input_draft", "input_draft_operation_id")):
            raise TypeError("optical draft provider does not implement its protocol")
        with self._live():
            validate_raw(provider.snapshot_input_draft(), module)
            with self._lock:
                previous = self.providers.get(module)
                if previous is not None and previous is not provider:
                    raise ProjectError("draft_owner_exists", "The project already has an optical draft owner")
                if previous is provider:
                    return
                if self._restore_batch is not None:
                    raise ProjectError("pending_operation", "Wait for project optical recovery before opening another draft owner")
                self.providers[module] = provider
                pending = deepcopy(self.held.get(module))
            if pending is not None:
                try:
                    self._start_restore(module, provider, pending, clear_held=True)
                except Exception:
                    with self._lock:
                        if self.providers.get(module) is provider:
                            self.providers.pop(module)
                    raise

    def unregister(self, module, provider: OpticalDraftProvider):
        with self._lock:
            if self.providers.get(module) is not provider:
                return False
        record = validate_raw(provider.snapshot_input_draft(), module)
        if record["operation"] is not None:
            raise ProjectError("pending_operation", "An optical input operation must finish before its owner is disposed")
        with self._lock:
            if self.providers.get(module) is not provider:
                return False
            self.held[module] = record
            self.providers.pop(module)
            # Completed entries must not retain a destroyed presentation.
            self._saves.pop(module, None)
            self._restores.pop(module, None)
            return True

    @property
    def dirty(self):
        live_dirty = any(row["dirty"] for row in self.records().values())
        with self._lock:
            return live_dirty or any(row["dirty"] for row in self.held.values())

    @property
    def pending(self):
        with self._lock:
            return bool(self.held)

    def signature(self):
        return digest(self.records())

    @property
    def operations_pending(self):
        records = self.records()
        with self._lock:
            return (bool(self._restores or self._starting)
                    or any(not row["done"] for row in self._saves.values())
                    or any(row["operation"] is not None for row in records.values()))

    def preflight(self):
        records = self.records()
        if self.operations_pending:
            raise ProjectError("pending_operation", "Wait for the optical input operation to finish before closing")
        return tuple({"module_key": module, "name": row["information"]["name"], "dirty": True}
                     for module, row in sorted(records.items()) if row["dirty"])

    def snapshot(self):
        return [snapshot_record(self.workspace.store, row)
                for _module, row in sorted(self.records().items())]

    def validate(self, records):
        if not isinstance(records, list):
            raise ProjectError("invalid_recovery", "Optical recovery inputs must be a list")
        checked = {}
        for row in records:
            value = validate_record(self.workspace.store, row)
            module = value["module_key"]
            if module in checked:
                raise ProjectError("invalid_recovery", "Optical recovery contains a duplicate module")
            checked[module] = value
        return checked

    def begin_save(self, module):
        with self._live():
            with self._lock:
                provider = self.providers.get(module)
                existing = self._saves.get(module)
                if existing is not None and not existing["done"]:
                    return existing["request_id"]
            if provider is None:
                raise ProjectError("draft_view_required", "Open the optical input before saving its retained draft")
            record = validate_raw(provider.snapshot_input_draft(), module)
            if record["pending_information"] is not None:
                raise ProjectError("unconfirmed_information", "Confirm or cancel optical information edits before saving")
            if record["operation"] is not None:
                raise ProjectError("pending_operation", "An optical input operation is already running")
            key = (module, "save")
            with self._lock:
                self._starting[key] = []
            try:
                request_id = provider.save_input_draft()
                if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
                    raise ProjectError("save_not_started", "Optical save did not return an operation request ID")
                with self._lock:
                    self._saves[module] = {"provider": provider, "request_id": request_id,
                                           "done": False, "success": False, "error": ""}
                    early = self._starting.pop(key)
                for completion in early:
                    self.complete_save(module, provider, *completion)
                return request_id
            except Exception:
                with self._lock:
                    self._starting.pop(key, None)
                raise

    def complete_save(self, module, provider, request_id, success, error=""):
        if not self._is_live():
            return False
        with self._lock:
            if self.providers.get(module) is not provider:
                return False
            if (module, "save") in self._starting:
                self._starting[(module, "save")].append((request_id, success, error))
                return False
            barrier = self._saves.get(module)
            if (barrier is None or barrier["provider"] is not provider
                    or barrier["request_id"] != request_id or barrier["done"]):
                # Ordinary toolbar saves do not create a close-save barrier.
                return False
            barrier.update(done=True, success=success is True, error=str(error))
            return True

    def save_status(self, module, request_id):
        with self._lock:
            barrier = self._saves.get(module)
            if barrier is None or barrier["request_id"] != request_id:
                raise ProjectError("stale_operation", "Optical save request does not match its completion barrier")
            result = {key: value for key, value in barrier.items() if key != "provider"}
            provider = barrier["provider"]
        result["draft_dirty"] = validate_raw(provider.snapshot_input_draft(), module)["dirty"]
        result["close_ready"] = result["done"] and result["success"] and not result["draft_dirty"]
        return result

    def _start_restore(self, module, provider, record, *, clear_held, rollback=False):
        with self._live():
            return self._dispatch_restore(module, provider, record, clear_held=clear_held, rollback=rollback)

    def _dispatch_restore(self, module, provider, record, *, clear_held, rollback):
        key = (module, "restore")
        previous = validate_raw(provider.snapshot_input_draft(), module)
        if previous["operation"] is not None:
            raise ProjectError("pending_operation", "Finish the optical operation before restoring its draft")
        with self._lock:
            self._starting[key] = []
            if not self._restores and self._restore_batch is None:
                self._restore_status = {"pending": False, "success": True, "error": "", "token": uuid4().hex}
        try:
            request_id = provider.restore_input_draft(deepcopy(record))
            if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
                raise ProjectError("restore_not_started", "Optical restore did not return an operation request ID")
            with self._lock:
                self._restores[module] = {"provider": provider, "request_id": request_id,
                    "record": deepcopy(record), "previous": previous, "clear_held": clear_held,
                    "rollback": rollback, "source_signature": self._state_signature(previous),
                    "target_signature": self._state_signature(record)}
                early = self._starting.pop(key)
                self._restore_status["pending"] = True
                self._restore_status["success"] = False
            for completion in early:
                self.complete_restore(module, provider, *completion)
            return request_id
        except Exception:
            with self._lock:
                self._starting.pop(key, None)
            raise

    def restore(self, records):
        checked = self.validate(records)
        self.preflight()
        candidates = {module: native_record(self.workspace.store, value, as_copy=True)
                      for module, value in checked.items()}
        # All format, identity, input and metadata validation precedes mutation.
        with self._lock:
            providers = dict(self.providers)
            previous = deepcopy(self.held)
            previous_live = {module: validate_raw(providers[module].snapshot_input_draft(), module)
                             for module in candidates if module in providers}
            self.held.update(deepcopy(candidates))
            self._restore_status = {"pending": False, "success": True, "error": "", "token": uuid4().hex}
            restore_token = self._restore_status["token"]
            self._restore_batch = {"previous": previous_live, "candidates": deepcopy(candidates),
                "applied": {}, "scheduling": True, "rollback": False}
        requests = {}
        try:
            for module, record in candidates.items():
                provider = providers.get(module)
                if provider is not None:
                    requests[module] = self._start_restore(module, provider, record, clear_held=True)
        except Exception as exc:
            # Failed admission leaves the complete detached candidate available
            # for retry; an async provider is required not to mutate on failure.
            with self._lock:
                self._restore_status.update(pending=bool(self._restores), success=False, error=str(exc))
                if self._restore_batch is not None:
                    self._restore_batch["scheduling"] = False
                for module, record in previous.items():
                    if module not in candidates:
                        self.held[module] = record
            self._settle_batch()
            raise
        with self._lock:
            if self._restore_batch is not None:
                self._restore_batch["scheduling"] = False
        self._settle_batch()
        return {"pending": self.restore_status()["pending"], "request_ids": requests, "token": restore_token}

    @staticmethod
    def _state_signature(record):
        return digest({key: value for key, value in record.items() if key != "operation"})

    def restore_is_current(self, module, provider: OpticalDraftProvider, request_id):
        """Authorize adoption before a provider mutates its editable owner.

        Both the operation and its complete source draft must still match.
        This also guards a rollback whose preparation finished after new edits.
        """
        if not self._is_live():
            return False
        with self._lock:
            barrier = self._restores.get(module)
            if (self.providers.get(module) is not provider or barrier is None
                    or barrier["provider"] is not provider or barrier["request_id"] != request_id):
                return False
            expected = barrier["source_signature"]
        current = validate_raw(provider.snapshot_input_draft(), module)
        operation = current["operation"]
        return (provider.input_draft_operation_id() == request_id and
                self._state_signature(current) == expected and
                (operation is None or operation == {"request_id": request_id, "kind": "restore"}))

    def complete_restore(self, module, provider, request_id, success, error=""):
        if not self._is_live():
            return False
        with self._lock:
            if self.providers.get(module) is not provider:
                return False
            if (module, "restore") in self._starting:
                self._starting[(module, "restore")].append((request_id, success, error))
                return False
            barrier = self._restores.get(module)
            if barrier is None or barrier["provider"] is not provider or barrier["request_id"] != request_id:
                return False
            self._restores.pop(module)
            if success is True:
                if barrier["clear_held"]:
                    self.held.pop(module, None)
                if self._restore_batch is not None and not barrier["rollback"]:
                    self._restore_batch["applied"][module] = {
                        "provider": provider, "request_id": request_id,
                        "signature": self._state_signature(validate_raw(provider.snapshot_input_draft(), module)),
                        "target_signature": barrier["target_signature"]}
            else:
                if not barrier["rollback"]:
                    self.held[module] = deepcopy(barrier["record"])
                else:
                    self._mark_partial(module)
                failure = ("Optical draft rollback failed (" + module + "): " if barrier["rollback"]
                           else module + ": ") + (str(error) or "Optical draft restore failed")
                self._restore_status["error"] = (self._restore_status["error"] + "; " + failure
                                                if self._restore_status["error"] else failure)
            self._restore_status["pending"] = bool(self._restores)
            self._restore_status["success"] = not bool(self._restore_status["error"] or self._restores)
            if self._restore_batch is None:
                self._remember_status()
        self._settle_batch()
        return True

    def _settle_batch(self):
        """An asynchronous multi-module failure restores prior live drafts too."""
        with self._lock:
            batch = self._restore_batch
            if batch is None or batch["scheduling"] or self._restores:
                return
            if self._restore_status["error"] and batch["applied"] and not batch["rollback"]:
                batch["rollback"] = batch["scheduling"] = True
                rollback = [(module, saved, batch["previous"].get(module))
                            for module, saved in sorted(batch["applied"].items())]
                # Keep every recovery candidate separately from the prior draft
                # being restored. Retry still uses the reviewed source record.
                for module, candidate in batch["candidates"].items():
                    self.held.setdefault(module, deepcopy(candidate))
                self._restore_status["pending"] = True
            else:
                self._restore_status["pending"] = False
                self._restore_status["success"] = not bool(self._restore_status["error"])
                self._restore_batch = None
                self._remember_status()
                return
        for module, saved, previous in rollback:
            provider = saved["provider"]
            with self._lock:
                same_owner = self.providers.get(module) is provider
            if not same_owner or previous is None:
                with self._lock:
                    self._mark_partial(module)
                    self._restore_status["error"] += "; " + module + " owner changed; current draft kept (partial recovery)"
                continue
            try:
                current = validate_raw(provider.snapshot_input_draft(), module)
                if (current["operation"] is not None
                        or provider.input_draft_operation_id() != saved["request_id"]
                        or saved["signature"] != saved["target_signature"]
                        or self._state_signature(current) != saved["signature"]):
                    with self._lock:
                        self._mark_partial(module)
                        self._restore_status["error"] += "; " + module + " has newer edits or an operation; current draft kept (partial recovery)"
                    continue
                self._start_restore(module, provider, previous, clear_held=False, rollback=True)
            except Exception as exc:
                with self._lock:
                    self._mark_partial(module)
                    self._restore_status["error"] += "; optical rollback could not start: " + str(exc)
        with self._lock:
            batch["scheduling"] = False
        self._settle_batch()

    def _mark_partial(self, module):
        self._restore_status["partial"] = True
        kept = self._restore_status.setdefault("kept_modules", [])
        if module not in kept:
            kept.append(module)

    def _remember_status(self):
        if not self._restore_status["pending"] and self._restore_status["token"]:
            self._restore_history[self._restore_status["token"]] = deepcopy(self._restore_status)

    def restore_status(self, token=None):
        with self._lock:
            if token is None or token == self._restore_status["token"]:
                return detached(self._restore_status)
            previous = self._restore_history.get(token)
            if previous is None:
                raise ProjectError("stale_operation", "Optical recovery operation token is no longer available")
            return detached(previous)
