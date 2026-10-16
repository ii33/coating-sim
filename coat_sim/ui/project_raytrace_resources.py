"""Asynchronous managed inputs for the existing optical-workspace controller.

Only detached JSON and verified owned file paths cross the worker boundary.
Numerical requests, appearance parameters and results keep their existing owners.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QTimer, Slot

from .project_simulation_resources import ProjectSimulationResources, _information_token

_INPUT_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="project-simulation-input")
_RETAINED_INPUT_WRITES: set[ProjectManagedSimulationResources] = set()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _signature(state, information):
    return hashlib.sha256(_canonical([state, information]).encode("utf-8")).hexdigest()


def _native_projection(kind, state):
    if kind == "appearance_configuration":
        return deepcopy(state)
    from .raytrace import UI_DOCUMENT_SCHEMA, UI_DOCUMENT_VERSION
    request = json.loads(state["request_text"])
    return {"schema": UI_DOCUMENT_SCHEMA, "schema_version": UI_DOCUMENT_VERSION,
            "request": request, "deposition_asset": deepcopy(state["deposition_asset"])}


def _open_input(service, kind, identity):
    from .raytrace import _layer_asset_summary
    document = service.load_document(identity)
    if document.kind != kind or document.issues:
        raise ValueError("所选工程输入类型不匹配或校验失败。")
    payload = document.runtime["payload"]
    asset_ref = (payload.get("deposition_asset") if kind == "raytrace_request" else payload.get("context"))
    asset = None
    if asset_ref:
        path = asset_ref["path"] if kind == "raytrace_request" else asset_ref["asset_path"]
        digest = asset_ref["sha256"] if kind == "raytrace_request" else asset_ref["asset_sha256"]
        asset = _layer_asset_summary(Path(path), service.root, expected_sha256=digest)
        if kind == "appearance_configuration":
            actual = asset.get("appearance_context")
            if not actual or actual["process_profile_sha256"] != asset_ref["process_profile_sha256"]:
                raise ValueError("外观配置的冻结工艺与受管逐层资产不一致。")
            asset["appearance_context"] = deepcopy(asset_ref)
    revision = service._record(identity)["sha256"]
    return {"document": document, "payload": payload, "asset": asset,
            "identity": identity, "revision": revision}


def _prepare_restored_input(service, kind, record):
    from .raytrace import _layer_asset_summary
    state = record["state"]
    reference = state.get("deposition_asset") if kind == "raytrace_request" else state.get("context")
    asset = None
    if reference is not None:
        path = reference["path"] if kind == "raytrace_request" else reference["asset_path"]
        digest = reference["sha256"] if kind == "raytrace_request" else reference["asset_sha256"]
        asset = _layer_asset_summary(Path(path), service.root, expected_sha256=digest)
        if kind == "appearance_configuration":
            actual = asset.get("appearance_context")
            if not actual or actual["process_profile_sha256"] != reference["process_profile_sha256"]:
                raise ValueError("恢复配置的冻结工艺与受管资产不一致。")
            asset["appearance_context"] = deepcopy(reference)
    if kind == "appearance_configuration":
        from coat_sim.core.project_binary_assets import metadata_for_file, read_scalar_spectrum
        for target in ("area", "environment"):
            spectrum = state["draft"].get(target + "_spectrum")
            if spectrum is not None:
                metadata_for_file(spectrum["path"], "spd_csv",
                    Path(spectrum["path"]).relative_to(service.root).as_posix(),
                    expected_sha256=spectrum["sha256"])
                actual = read_scalar_spectrum(spectrum["path"])
                if any(spectrum[key] != actual[key] for key in ("wavelength_nm", "value")):
                    raise ValueError("恢复光谱的采样与工程 CSV 输入不一致。")
    return {"record": deepcopy(record), "asset": asset}


def _save_input(service, kind, state, information, identity, revision):
    payload = _native_projection(kind, state)
    document = service.save_simulation_resource(kind, payload, document_id=identity or None,
        expected_revision=revision, name=information["name"],
        description=information["description"], notes=information["notes"])
    identity = Path(document.path).stem
    return {"document": document, "identity": identity,
            "revision": document.runtime["revision"]}


def _run_input(function):
    try:
        return function(), ""
    except Exception as exc:
        return None, str(exc)


def _capture_input(service, role, path, cancellation):
    captured = service.capture_simulation_asset(role, path, cancel_event=cancellation)
    if role == "deposition_asset":
        from .raytrace import _layer_asset_summary
        # Publication has completed: do not call a cancellable read and later
        # misreport the reusable owned input as never imported.
        captured["asset"] = _layer_asset_summary(Path(captured["path"]), service.root,
            expected_sha256=captured["sha256"])
    else:
        from coat_sim.core.project_binary_assets import read_scalar_spectrum
        captured["spectrum"] = {"path": captured["path"], "sha256": captured["sha256"],
                                 **read_scalar_spectrum(captured["path"])}
    return captured


class ProjectManagedSimulationResources(ProjectSimulationResources):
    """A request-ID guarded facade around one explicitly bound ray/appearance owner."""

    def __init__(self, workspace, bridge, module_key, parent=None):
        kind = "raytrace_request" if module_key == "ray_trace" else "appearance_configuration"
        super().__init__(workspace, kind, parent)
        if bridge.project_context is not workspace:
            raise ValueError("Saved optical inputs require their exact project owner")
        self.bridge = bridge
        self.compute = bridge._project_compute
        bridge._managed_input_id = ""
        bridge._managed_input_revision = None
        bridge._managed_input_information = {"name": "光追请求" if kind == "raytrace_request" else "外观配置",
                                              "description": "", "notes": ""}
        bridge._pending_information_edit = None
        bridge._managed_input_selected = False
        bridge._managed_input_baseline = self._working_signature()
        self._resource_tasks: dict[str, tuple[Any, str, dict]] = {}
        self._dispose_after_resource = False
        self._detached_input_snapshot = None
        self._restore_record = None
        self._draft_module_key = "ray_trace" if kind == "raytrace_request" else "appearance"
        self._draft_registered = False
        self._resource_timer = QTimer(self)
        self._resource_timer.setInterval(20)
        self._resource_timer.timeout.connect(self._poll_resources)
        bridge.changed.connect(self.changed)
        self.compute.register_input_draft_provider(self._draft_module_key, self)
        self._draft_registered = True
        if not self._resource_busy and bridge._asset is not None:
            # Existing deposition-output launch contexts predate managed binary
            # inputs. Import that explicitly bound input without resetting it.
            self._capture_selected_input("deposition_asset", bridge._asset["path"], preserve_draft=True)

    def snapshot_input_draft(self):
        if self._detached_input_snapshot is not None:
            return deepcopy(self._detached_input_snapshot)
        record = {"module_key": self._draft_module_key, "document_id": self._current_id() or None,
            "revision": self.bridge._managed_input_revision, "information": self._metadata(),
            "model_dirty": self._is_dirty(), "dirty": self._is_dirty() or self._has_information_edit(),
            "state": deepcopy(self._input_state()),
            "pending_information": deepcopy(self.bridge._pending_information_edit)}
        record["operation"] = ({"request_id": self._resource_operation_id, "kind": self._resource_operation_kind}
            if self._resource_busy else None)
        return record

    def restore_input_draft(self, record):
        if not self._can_replace():
            return False
        source = deepcopy(record)
        service, kind = self.service, self.module_key
        context = {"signature": self._working_signature()}
        if not self._start_input("restore", lambda: _prepare_restored_input(service, kind, source), context):
            return False
        self._restore_record = source
        return self._resource_operation_id

    def save_input_draft(self):
        return self._resource_operation_id if self.saveCurrent() and self._resource_busy else None

    def input_draft_operation_id(self) -> str:
        return self._resource_operation_id

    def _unregister_draft(self):
        if not self._draft_registered:
            return
        self._draft_registered = False
        try:
            self.compute.unregister_input_draft_provider(self._draft_module_key, self)
        except (ValueError, RuntimeError):
            pass  # A closed generation cannot retain or mutate a new owner.

    def _complete_draft_operation(self, request_id, kind, success, error):
        if not self._draft_registered:
            return
        try:
            if kind in {"save", "copy"}:
                self.compute.complete_input_draft_save(self._draft_module_key, self, request_id, success, error)
            elif kind == "restore":
                self.compute.complete_input_draft_restore(self._draft_module_key, self, request_id, success, error)
        except (ValueError, RuntimeError):
            pass  # A completion never attaches itself to another generation.

    def _information_edit_owner(self):
        return self.bridge

    def _can_export_document(self):
        return False  # Full owned binary closure is available through .cproject.

    def captureLayerAsset(self, path):
        return self._capture_selected_input("deposition_asset", path)

    def captureSpectrum(self, path, target, appearance):
        if target not in {"area", "environment"} or appearance is not self.bridge._appearance:
            return False
        return self._capture_selected_input("spd_csv", path, target=target, appearance=appearance)

    def _capture_selected_input(self, role, path, **extra):
        service, cancellation = self.service, Event()
        context = {"signature": self._working_signature(), "capture": role,
                   "cancellation": cancellation, **extra}
        accepted = self._start_input("open", lambda: _capture_input(service, role, path, cancellation), context)
        if accepted:
            self._status = "正在导入并校验工程输入；导入成功后配置仍需保存…"
            self.changed.emit()
        return accepted

    def _adopt_captured_input(self, result, context):
        if context["capture"] == "deposition_asset":
            self.bridge._asset = result["asset"]
            self.bridge._asset_error = ""
            appearance = self.bridge._appearance
            if context.get("preserve_draft") and appearance is not None:
                payload = appearance.projectInputConfiguration()
                payload["context"] = deepcopy(result["asset"]["appearance_context"])
                appearance.rebindProjectInputAssets(payload)
                self.compute.set_appearance_context(deepcopy(payload["context"]))
            else:
                self.bridge._sync_appearance_context()
            self.bridge._result = None
            self.bridge._result_path = ""
        else:
            appearance = context["appearance"]
            if appearance is not self.bridge._appearance:
                raise ValueError("输入已导入工程，当前外观配置已更换；请选择已导入输入重试。")
            appearance._draft[context["target"] + "_spectrum"] = result["spectrum"]
            appearance._error = ""
            appearance._status = "输入已导入工程，配置未保存。"
            appearance.changed.emit()
        if self.bridge._appearance is not None:
            self.bridge._appearance._status = "输入已导入工程，配置未保存。"
        self.bridge._error = ""
        self.bridge._emit_document()

    def _pending_information_changed(self):
        self.bridge.changed.emit()
        self.changed.emit()

    def _metadata(self):
        return deepcopy(self.bridge._managed_input_information)

    def _information(self):
        metadata = self._metadata()
        values = {"name": metadata["name"], "summary": metadata["description"], "notes": metadata["notes"]}
        return {**values, "editable_fields": ["name", "summary", "notes"],
                "name_limit": 120, "token": _information_token(self._current_id(), values)}

    def _update_information(self, name, summary, notes):
        if not name.strip() or len(name) > 120 or len(summary) > 20000 or len(notes) > 20000:
            raise ValueError("名称须为 1 至 120 字，概述和备注须不超过 20000 字。")
        self.bridge._managed_input_information = {"name": name.strip(), "description": summary, "notes": notes}
        self.bridge.changed.emit()
        return True

    def _documents(self):
        return self.service.list_documents(self.module_key)

    def _current_id(self):
        return self.bridge._managed_input_id

    def _input_state(self):
        if self.module_key == "raytrace_request":
            asset = self.bridge._asset
            return {"request_text": self.bridge._request_text, "deposition_asset":
                    None if not asset else {"path": asset["path"], "sha256": asset["sha256"]}}
        appearance = self.bridge._appearance
        if appearance is None:
            return {"schema": "coat_sim.appearance_configuration", "version": 1,
                    "context": None, "draft": {}, "geometry_thickness_mode": "inherited",
                    "backend": "cpu", "precision": "native"}
        return appearance.projectInputConfiguration()

    def _working_signature(self):
        return _signature(self._input_state(), self._metadata())

    def _is_dirty(self):
        return self._working_signature() != self.bridge._managed_input_baseline

    def _owner_status(self):
        return self.bridge.status

    def _can_replace(self):
        return not (self._resource_busy or self._transfer_busy or self.compute.editing_frozen
                    or self.bridge.busy or self.bridge.appearanceRunning or self._detached)

    def _can_save(self):
        return self._can_replace() and (self.module_key == "appearance_configuration"
                                        or bool(self.bridge._request_text.strip()))

    def _start_input(self, kind, function, context):
        if not self._can_replace():
            return False
        request_id = uuid4().hex
        self._resource_operation_id = request_id
        self._resource_operation_kind = kind
        self._resource_operation_error = ""
        self._resource_busy = True
        self._status = ("正在读取并校验工程输入…" if kind in {"open", "restore"} else
                        "正在保存工程输入与依赖，提交开始后无法取消…")
        self._resource_tasks[request_id] = (_INPUT_EXECUTOR.submit(_run_input, function), kind, context)
        self._resource_timer.start()
        self.changed.emit()
        return True

    def _perform_replace(self, action, identity):
        if action == "new":
            if self.module_key == "raytrace_request":
                self.bridge.newRequest()
            elif self.bridge._appearance is not None:
                self.bridge._appearance.resetProjectInputConfiguration()
            self.bridge._managed_input_id = ""
            self.bridge._managed_input_revision = None
            self.bridge._managed_input_selected = True
            self.bridge._managed_input_information = {"name": "光追请求" if self.module_key == "raytrace_request" else "外观配置",
                                                       "description": "", "notes": ""}
            self.bridge._managed_input_baseline = ""
            self.bridge._pending_information_edit = None
            self._status = "已新建未保存的工程输入。"
            self._resource_operation_id = uuid4().hex
            self._resource_operation_kind = "new"
            self.bridge._emit_document()
            self.resourceOperationFinished.emit(self._resource_operation_id, "new", True, "")
            return True
        service, kind = self.service, self.module_key
        context = {"signature": self._working_signature()}
        return self._start_input("open", lambda: _open_input(service, kind, identity), context)

    def _save(self, copy):
        state, information = deepcopy(self._input_state()), self._metadata()
        original_signature = self._working_signature()
        if copy:
            information["name"] = information["name"][:117] + " 副本"
        identity = "" if copy else self._current_id()
        revision = None if copy else self.bridge._managed_input_revision
        service, kind = self.service, self.module_key
        context = {"signature": original_signature, "state": state, "information": information}
        return self._start_input("copy" if copy else "save",
            lambda: _save_input(service, kind, state, information, identity, revision), context)

    @Slot(result=bool)
    def cancelResourceOperation(self):
        if not self._resource_busy:
            return True
        if self._resource_operation_kind not in {"open", "restore"}:
            self._status = "输入已开始保存，无法取消；请等待提交结果。"
            self.changed.emit()
            return False
        request_id = self._resource_operation_id
        task = self._resource_tasks.get(request_id)
        if task is not None and "capture" in task[2]:
            task[2]["cancellation"].set()
            self._status = "已请求取消导入，正在等待发布边界；已发布输入将保留为工程资源。"
            self.changed.emit()
            return False
        task = self._resource_tasks.pop(request_id, None)
        if task is not None:
            task[0].cancel()
        self._resource_busy = False
        self._resource_operation_id = ""
        self._resource_operation_error = "已取消打开，当前输入未改变。"
        self._status = self._resource_operation_error
        self._resource_timer.stop()
        self.changed.emit()
        kind = self._resource_operation_kind
        self._complete_draft_operation(request_id, kind, False, self._status)
        self.resourceOperationFinished.emit(request_id, kind, False, self._status)
        return True

    def _adopt_open(self, result):
        payload, asset, document = result["payload"], result["asset"], result["document"]
        if self.service._record(result["identity"])["sha256"] != result["revision"]:
            raise ValueError("读取期间资源已变化，请重新打开。")
        if self.module_key == "appearance_configuration":
            candidate = None
            if payload["context"] is not None:
                from .appearance import AppearanceWindowController
                candidate = AppearanceWindowController(deepcopy(payload["context"]), root=self.compute.root,
                    parent=self.bridge, project_context=self.project_context)
                try:
                    candidate.adoptProjectInputConfiguration(payload)
                except Exception:
                    candidate.shutdown()
                    candidate.deleteLater()
                    raise
            old, self.bridge._appearance = self.bridge._appearance, candidate
            if candidate is not None:
                candidate.changed.connect(self.bridge.changed.emit)
                candidate.closeReady.connect(self.bridge._appearance_close_ready)
            if old is not None:
                old.shutdown()
                old.deleteLater()
            self.bridge._asset = asset
            self.bridge._asset_error = ""
            self.bridge._workspace_mode = "appearance"
        else:
            self.bridge._request = deepcopy(payload["request"])
            self.bridge._request_text = json.dumps(payload["request"], ensure_ascii=False, indent=2, allow_nan=False)
            self.bridge._request_path = document.path
            self.bridge._asset = asset
            self.bridge._asset_error = ""
            self.bridge._sync_appearance_context()
            self.bridge._selected_surface = self.bridge._selected_layer = self.bridge._selected_step = ""
            self.bridge._result = None
            self.bridge._result_path = ""
            self.bridge._workspace_mode = "geometric"
        self.bridge._managed_input_id = result["identity"]
        self.bridge._managed_input_revision = result["revision"]
        self.bridge._managed_input_selected = True
        self.bridge._managed_input_information = {"name": document.data["name"],
            "description": document.data.get("description", ""), "notes": document.data.get("notes", "")}
        self.bridge._managed_input_baseline = self._working_signature()
        self.bridge._saved_snapshot = self.bridge._current_snapshot()
        self.bridge._phase = "ready"
        self.bridge._status = "已打开受管工程输入；历史结果未包含在输入配置中。"
        self.bridge._error = ""
        self.bridge._emit_document()

    def _adopt_restored(self, result):
        record, asset = result["record"], result["asset"]
        state = record["state"]
        if self.module_key == "appearance_configuration":
            candidate = None
            if state["context"] is not None:
                from .appearance import AppearanceWindowController
                candidate = AppearanceWindowController(deepcopy(state["context"]), root=self.compute.root,
                    parent=self.bridge, project_context=self.project_context)
                try:
                    candidate.adoptProjectInputConfiguration(state, allow_invalid=True)
                except Exception:
                    candidate.shutdown()
                    candidate.deleteLater()
                    raise
            old, self.bridge._appearance = self.bridge._appearance, candidate
            if candidate is not None:
                candidate.changed.connect(self.bridge.changed.emit)
                candidate.closeReady.connect(self.bridge._appearance_close_ready)
            if old is not None:
                old.shutdown()
                old.deleteLater()
            self.bridge._workspace_mode = "appearance"
        else:
            try:
                request = json.loads(state["request_text"])
            except (ValueError, TypeError):
                request = None
            self.bridge._request = request if isinstance(request, dict) else None
            self.bridge._request_text = state["request_text"]
            self.bridge._request_path = (str(self.service.document_handle(record["document_id"]))
                if record["document_id"] else "")
            self.bridge._workspace_mode = "geometric"
            self.bridge._result = None
            self.bridge._result_path = ""
            self.bridge._selected_surface = self.bridge._selected_layer = self.bridge._selected_step = ""
        self.bridge._asset = asset
        self.bridge._asset_error = ""
        self.bridge._managed_input_id = record["document_id"] or ""
        self.bridge._managed_input_revision = record["revision"]
        self.bridge._managed_input_information = deepcopy(record["information"])
        self.bridge._managed_input_recovery_origin = deepcopy(record.get("origin"))
        self.bridge._managed_input_selected = True
        self.bridge._managed_input_baseline = "" if record["model_dirty"] else self._working_signature()
        self.bridge._pending_information_edit = deepcopy(record.get("pending_information"))
        if self.bridge._pending_information_edit is not None:
            information = self._information()
            self.bridge._pending_information_edit.update(document_id=self._current_id() or None,
                token=information["token"], model_dirty=record["model_dirty"])
        self.bridge._information_edit_presentation = None
        self.bridge._saved_snapshot = "" if record["model_dirty"] else self.bridge._current_snapshot()
        self.bridge._phase = "ready" if self.module_key == "appearance_configuration" or self.bridge._request is not None else "empty"
        self.bridge._error = ""
        self.bridge._status = "工程输入已恢复；尚未确认的信息编辑仍未应用。"
        self._restore_record = None
        self.bridge._emit_document()

    def _update_detached_snapshot(self, result, kind, context, error):
        snapshot = self._detached_input_snapshot
        if snapshot is None:
            return
        snapshot["operation"] = None
        if error or kind not in {"save", "copy"}:
            return
        unchanged = _signature(snapshot["state"], snapshot["information"]) == context["signature"]
        snapshot.update(document_id=result["identity"], revision=result["revision"])
        if unchanged:
            payload = result["document"].runtime["payload"]
            snapshot["state"] = (payload if self.module_key == "appearance_configuration" else
                {"request_text": context["state"]["request_text"], "deposition_asset": payload["deposition_asset"]})
            snapshot["information"] = deepcopy(context["information"])
            snapshot["model_dirty"] = False
            snapshot["dirty"] = snapshot["pending_information"] is not None
        else:
            snapshot["model_dirty"] = snapshot["dirty"] = True
        pending = snapshot.get("pending_information")
        if pending is not None:
            metadata = snapshot["information"]
            pending.update(document_id=snapshot["document_id"], token=_information_token(snapshot["document_id"],
                {"name": metadata["name"], "summary": metadata["description"], "notes": metadata["notes"]}))

    def _rebind_saved_assets(self, payload):
        """Adopt only captured file locations, retaining numerical/result state."""
        if self.module_key == "appearance_configuration":
            if self.bridge._appearance is not None and payload["context"] is not None:
                self.bridge._appearance.rebindProjectInputAssets(payload)
                if self.bridge._asset is not None:
                    context = payload["context"]
                    self.bridge._asset.update(path=context["asset_path"], sha256=context["asset_sha256"],
                                               appearance_context=deepcopy(context))
                    self.compute.set_appearance_context(deepcopy(context))
        else:
            reference = payload["deposition_asset"]
            if reference is not None and self.bridge._asset is not None:
                self.bridge._asset.update(path=reference["path"], sha256=reference["sha256"])
                context = self.bridge._asset.get("appearance_context")
                if context is not None:
                    context.update(asset_path=reference["path"], asset_root=str(self.compute.root),
                                   asset_sha256=reference["sha256"])
                    self.compute.set_appearance_context(deepcopy(context))

    def _poll_resources(self):
        if not self._detached and self.compute.editing_frozen:
            return
        for request_id, (future, kind, context) in tuple(self._resource_tasks.items()):
            if not future.done():
                continue
            self._resource_tasks.pop(request_id, None)
            if request_id != self._resource_operation_id:
                continue
            result, error = future.result()
            self._resource_busy = False
            unchanged = False
            if not self._detached and not error:
                try:
                    with self.project_context.host.operation_scope(self.owner_binding):
                        unchanged = self._working_signature() == context["signature"]
                        if kind in {"open", "restore"}:
                            if not unchanged:
                                raise ValueError("输入已导入工程，期间的新修改已保留，配置未保存；请重新选择输入。"
                                    if "capture" in context else "读取期间当前输入已修改，已保留当前内容；请重新打开。")
                            if "capture" in context:
                                self._adopt_captured_input(result, context)
                            elif kind == "restore":
                                if not self.compute.input_draft_restore_is_current(
                                        self._draft_module_key, self, request_id):
                                    raise ValueError("恢复准备期间输入、信息编辑或操作已变化，已保留较新修改。")
                                self._adopt_restored(result)
                            else:
                                self._adopt_open(result)
                        else:
                            self.bridge._managed_input_id = result["identity"]
                            self.bridge._managed_input_revision = result["revision"]
                            self.bridge._managed_input_selected = True
                            if unchanged:
                                self.bridge._managed_input_information = deepcopy(context["information"])
                                if self.module_key == "raytrace_request":
                                    self.bridge._request_path = result["document"].path
                                self._rebind_saved_assets(result["document"].runtime["payload"])
                                self.bridge._managed_input_baseline = self._working_signature()
                                self.bridge._saved_snapshot = self.bridge._current_snapshot()
                            else:
                                self.bridge._managed_input_baseline = _signature(context["state"], context["information"])
                            self.bridge._emit_document()
                except Exception as exc:
                    error = str(exc)
            if self._detached:
                self._update_detached_snapshot(result, kind, context, error)
            self._resource_operation_error = error
            if error:
                self._status = "输入操作未完成；当前修改已保留：" + error
            elif not self._detached:
                self._status = ("输入已导入工程，配置未保存。" if "capture" in context else
                    "工程输入已恢复，尚未保存。" if kind == "restore" else "工程输入已打开。" if kind == "open" else
                    "工程输入与依赖已保存；结果未包含在输入配置中。" if unchanged else
                    "输入快照已保存；保存期间的新修改仍保留为未保存修改。")
            if not self._detached:
                self.bridge._status = self._status
                self.bridge.changed.emit()
            self.changed.emit()
            completed_signature = self._working_signature() if not self._detached and not error else ""
            self._complete_draft_operation(request_id, kind, not bool(error), error)
            self.resourceOperationFinished.emit(request_id, kind, not bool(error), error)
            if kind in {"save", "copy"} and not self._detached:
                still_current = (request_id == self._resource_operation_id and
                                 self._working_signature() == completed_signature and
                                 not self._has_information_edit())
                continue_replace = not bool(error) and unchanged and still_current
                self._after_async_replacement_save(request_id, continue_replace,
                    error or ("保存期间输入或操作已变化，请重新确认切换。" if not continue_replace else ""))
        if not self._resource_tasks:
            self._resource_timer.stop()
        if self._dispose_after_resource and not self._resource_busy:
            self.dispose()

    def detach(self):
        if self._detached_input_snapshot is None:
            self._detached_input_snapshot = self.snapshot_input_draft()
        if self._resource_busy and self._resource_operation_kind in {"open", "restore"}:
            self.cancelResourceOperation()
        if self._resource_busy:
            self._dispose_after_resource = True
            self.setParent(None)
            _RETAINED_INPUT_WRITES.add(self)
        if not self._resource_busy:
            self._detached_input_snapshot["operation"] = None
            self._unregister_draft()
        super().detach()

    def dispose(self):
        if self._resource_busy:
            self.detach()
            return
        self._unregister_draft()
        _RETAINED_INPUT_WRITES.discard(self)
        self._resource_timer.stop()
        super().dispose()
