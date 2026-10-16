"""Qt Quick bridge for coating recipes; physics and file rules live in CoatService."""

from __future__ import annotations

from copy import deepcopy
from functools import wraps
from pathlib import Path
import math
import threading
import json
import hashlib
from uuid import uuid4
from typing import Any, TYPE_CHECKING, cast
from coat_sim.scheduler import SchedulerClient, freeze_spec
from coat_sim.scheduler.desktop import RemoteCancelEvent, observe_job

from PySide6.QtCore import QObject, Property, QUrl, Signal, Slot, QThread

from coat_sim.core.design_files import default_design_path
from coat_sim.core.api import CoatService
from coat_sim.ui.machine_mask import _checked_reload_document
from coat_sim.core.data_files import design_directory

if TYPE_CHECKING:
    from coat_sim.core.project_resource_service import ProjectResourceService


_DETACHED_PROCESS_WORKERS = {}
_DETACHED_PROCESS_WORKERS_LOCK = threading.Lock()


def _reap_process_worker(worker):
    """Keep a detached QThread alive until it exits, without joining on the UI thread."""
    key = id(worker)
    with _DETACHED_PROCESS_WORKERS_LOCK:
        _DETACHED_PROCESS_WORKERS[key] = worker

    def wait_and_release():
        try:
            worker.wait()
        finally:
            with _DETACHED_PROCESS_WORKERS_LOCK:
                _DETACHED_PROCESS_WORKERS.pop(key, None)

    threading.Thread(target=wait_and_release, name="process-observer-reaper", daemon=True).start()


def _local_path(value: QUrl | str) -> Path:
    if isinstance(value, QUrl):
        value = value.toLocalFile()
    return Path(str(value)).expanduser().resolve()


def _project_edit(denied_result=None):
    """Block bridge writebacks while a project session is frozen for exit."""
    def decorate(method):
        @wraps(method)
        def guarded(self, *args, **kwargs):
            if self._project_mutations_frozen():
                self._set_status("项目正在关闭或被冻结，当前修改未应用。")
                return denied_result
            return method(self, *args, **kwargs)
        return guarded
    return decorate


def _integer_controls(value, key=""):
    """Restore integer controls widened to doubles by QML QVariant maps."""
    integer_keys = {"time_steps", "grid_size", "search_grid_size", "max_evaluations",
                    "seed_grid_points", "control_points", "slot", "mask_slots", "fixed_mask_slots", "source_slot"}
    if isinstance(value, dict):
        return {name: _integer_controls(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_integer_controls(item, key) for item in value]
    if key in integer_keys and type(value) is float and value.is_integer():
        return int(value)
    return value


class _OptimizationWorker(QThread):
    progress = Signal(dict)
    resultReady = Signal(object)
    failed = Signal(str)
    def __init__(self, service, process_path, settings, options, parent=None, *, estimate=None):
        super().__init__(parent)
        self.service, self.process_path, self.settings, self.options = service, process_path, settings, options
        self.store = SchedulerClient(service.root)
        self.job_id = uuid4().hex
        from dataclasses import asdict
        parameters = {"settings": asdict(service._settings(settings)), "process_path": process_path, "options": options}
        document = service.load("process", process_path)
        frozen = freeze_spec(service, "mask_optimization", parameters)
        self.job = self.store.submit(frozen, job_id=self.job_id,
            metadata={"summary": "修正板优化 · " + document.data["name"], "origin": {"surface": "desktop"},
                      "estimated_s": (estimate or {}).get("estimated_s"),
                      "memory_estimate_label": (estimate or {}).get("memory_estimate_label", ""),
                      "memory_warning": (estimate or {}).get("memory_warning"),
                      "input_summary": {"process_name": document.data["name"], "process": deepcopy(document.data), "settings": parameters["settings"],
                                          "dependency_digest": __import__('coat_sim.scheduler.jobs', fromlist=['JobSpec']).JobSpec.parse(frozen).fingerprint}})
        self.cancel_event = RemoteCancelEvent(self.store, self.job_id)
    def fail_start(self, error):
        # Submission succeeded; observer-start errors do not destroy admitted work.
        return str(error)
    def run(self):
        try:
            reply = observe_job(self.store, self.job_id, self.progress.emit,
                                disconnected=self.isInterruptionRequested)
            from coat_sim.scheduler.worker import load_result
            import json
            simulation = load_result(reply, self.store)
            configuration = simulation.pop("_configuration")
            report_artifact = next(item for item in reply['result_artifacts'] if item['role'] == 'report')
            report_path = self.store.materialize_artifact(report_artifact['artifact_id'])
            result = {"simulation": simulation, "_asset": reply["asset"],
                "process": configuration["process"], "machine": configuration["machine"],
                "input_snapshot": configuration.get("input_snapshot"),
                "result_path": str(self.store.materialize_artifact(reply['artifact_id'])),
                "report": json.loads(report_path.read_text(encoding="utf-8")),
                "report_path": str(report_path), "artifacts": reply.get("artifacts", {}), "pending_masks": reply["pending_masks"]}
            self.resultReady.emit(result)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(str(exc))


class ProcessBridge(QObject):

    @Property(QObject, constant=True)
    def informationController(self):
        if not hasattr(self, "_information_controller"):
            from .project_resource_adapter import EditorResourceInformationController
            self._information_controller = EditorResourceInformationController(self, "process", self)
        return self._information_controller

    def detach_observers(self):
        workers = list(self._workers.values())
        for worker in workers:
            worker.setParent(None)
            worker.requestInterruption()
            _reap_process_worker(worker)
        self._workers.clear()
        self._worker = None
        self._pending_frozen_optimization_result = None
        self._optimization["busy"] = False

    changed = Signal()
    statusChanged = Signal()
    optimizationChanged = Signal()
    overwriteRequested = Signal(QUrl)
    maskOverwriteRequested = Signal(QUrl, int)
    reloadRequested = Signal()
    documentReplaceRequested = Signal()
    filmDesignImportRequested = Signal()
    filmDesignImportReady = Signal()
    editingFrozenChanged = Signal()

    @Property(bool, notify=editingFrozenChanged)
    def editingFrozen(self):
        return self._project_mutations_frozen()

    @Slot(bool)
    def setEditingFrozen(self, frozen: bool) -> None:
        self._editing_frozen = bool(frozen)
        self.editingFrozenChanged.emit()
        if not frozen:
            self._resume_frozen_optimization_result()

    def _project_mutations_frozen(self) -> bool:
        if not getattr(self.service, "is_project_service", False):
            return False
        gate = getattr(self.service, "_gate", None)
        gate_frozen = getattr(gate, "frozen", False) if gate is not None else False
        if callable(gate_frozen):
            gate_frozen = gate_frozen()
        close_save_threads: set[int] = getattr(gate, "close_save_threads", set()) if gate is not None else set()
        coordinated_save = threading.get_ident() in close_save_threads
        return bool(getattr(self, "_editing_frozen", False) or (gate_frozen and not coordinated_save))

    def _resume_frozen_optimization_result(self) -> None:
        pending = getattr(self, "_pending_frozen_optimization_result", None)
        if pending is None or not getattr(self.service, "is_project_service", False):
            return
        gate = getattr(self.service, "_gate", None)
        if getattr(gate, "frozen", False) or self._editing_frozen:
            return
        self._pending_frozen_optimization_result = None
        self._optimization_result(pending)

    @Property(QObject, constant=True)
    def assetBrowser(self):
        return self._asset_browser

    @Property(QObject, constant=True)
    def projectNavigation(self):
        return self._project_navigation

    @Property(QUrl, constant=True)
    def defaultDirectory(self):
        if getattr(self.service, "is_project_service", False):
            return QUrl.fromLocalFile(str(cast("ProjectResourceService", self.service).default_directory("process")))
        return QUrl.fromLocalFile(str(design_directory("process")))

    @Slot(str, result=QUrl)
    def dependencyDirectory(self, kind):
        if getattr(self.service, "is_project_service", False):
            return QUrl.fromLocalFile(str(cast("ProjectResourceService", self.service).default_directory(kind)))
        return QUrl.fromLocalFile(str(design_directory(kind)))

    @Property(str, notify=changed)
    def description(self):
        return self._data.get("description", "")

    @Property(str, notify=changed)
    def notes(self):
        return self._data.get("notes", "")

    @Slot(str, str)
    @_project_edit()
    def setMetadata(self, key, value):
        if key in ("description", "notes"):
            self._data[key] = value
            self._dirty = True
            self._edit()

    def __init__(self, service: CoatService | None = None, initial_path: str | None = None):
        super().__init__()
        # The desktop user explicitly chooses paths in the file dialogs.
        self.service = service or CoatService(restrict_to_root=False)
        self._editing_frozen = False
        self._project_navigation = getattr(self.service, "project_navigation", None)
        from .asset_browser import AssetBrowserBridge
        import weakref
        weak_owner = weakref.ref(self)
        def open_asset(path):
            editor = weak_owner()
            if editor is not None:
                editor.openPath(QUrl.fromLocalFile(str(path)))
        self._asset_browser = AssetBrowserBridge(self.service, 'process', open_asset, self)
        self._path: Path | None = None
        self._reload_origin: Path | None = None
        self._pending_reload = None
        self._package_runtime = None
        self._package_signature = None
        self._data = self._blank()
        self._step_page = 0
        self._dependency_paths: dict[str, Path] = {}
        self._worker = None
        self._pending_masks = []
        self._pending_frozen_optimization_result = None
        self._workers = {}
        self._optimization = {"busy": False, "message": "", "stage": "", "artifacts": {}}
        self._dirty = False
        self._pending_save_as: Path | None = None
        self._pending_document_replace: tuple[str, Any, Path | None, dict] | None = None
        self._ok = False
        self._errors: list[str] = []
        self._status = "选择机器和基片，然后设置镀膜步骤。"
        self._source_preview: dict = {"sources": [], "message": "请选择机器文件。"}
        self._machine_runtime: dict | None = None
        self._machine_runtime_path: Path | None = None
        self._machine_runtime_signature: tuple | None = None
        self._preview_geometry_requested = False
        from .film_stack import FilmStackController
        self._film_stack = FilmStackController(self)
        self._film_import_snapshot = None
        self._film_import_preview = None
        self._film_import_surface = ""
        self._film_import_source_map: dict[str, int] = {}
        self._film_import_error = ""
        self._film_import_source_label = ""
        if initial_path:
            self.openPath(QUrl.fromLocalFile(str(_local_path(initial_path))))
        else:
            self._refresh()

    @Property(QObject, constant=True)
    def filmStack(self):
        return self._film_stack

    @Property(bool, notify=changed)
    def hasFilmDesignImportCandidate(self) -> bool:
        return self._film_import_snapshot is not None

    @Property("QVariantMap", notify=changed)
    def filmDesignImportPreview(self) -> dict:
        return deepcopy(self._film_import_preview or {})

    @Property("QVariantMap", notify=changed)
    def filmDesignImportSummary(self) -> dict:
        snapshot = self._film_import_snapshot or {}
        design = snapshot.get("design", {})
        substrate = design.get("substrate", {})
        return {
            "name": design.get("name", ""), "id": design.get("id", ""),
            "sha256": snapshot.get("sha256", ""),
            "sides": substrate.get("sides", ""),
            "front_layer_count": len(design.get("front_layers", [])),
            "back_layer_count": len(design.get("back_layers", [])),
        }

    @Property(str, notify=changed)
    def filmDesignImportSourceLabel(self) -> str:
        return self._film_import_source_label

    @Property(str, notify=changed)
    def filmDesignImportSurface(self) -> str:
        return self._film_import_surface

    @Property("QVariantList", notify=changed)
    def filmDesignImportRows(self) -> list:
        rows = deepcopy((self._film_import_preview or {}).get("rows", []))
        source_map = self._film_import_source_map
        existing = {}
        for step in self._data.get("steps", []):
            reference = step.get("film_layer_ref")
            if isinstance(reference, dict):
                existing[(reference.get("surface"), reference.get("layer_id"))] = step
        sources = {item.get("slot"): item for item in self._source_preview.get("sources", [])}
        for row in rows:
            slot = source_map.get(row["material_id"], 0)
            row["source_slot"] = slot
            prior = existing.get((row["surface"], row["layer_id"]))
            if prior is None:
                row["change_note"] = "新增"
                continue
            changes = []
            next_target = row["physical_thickness_nm"]
            try:
                old_target = float(prior.get("target_nm", 0.0))
                same_target = math.isclose(old_target, next_target,
                                           rel_tol=1e-12, abs_tol=1e-12)
            except (TypeError, ValueError, OverflowError):
                old_target, same_target = prior.get("target_nm", "?"), False
            if not same_target:
                changes.append(f"厚度 {old_target} → {next_target:g} nm")
            if prior.get("source_slot") != slot:
                changes.append(f"源位 {prior.get('source_slot', '?')} → {slot or '未映射'}")
            source = sources.get(slot)
            if source and source.get("valid"):
                rate = float(source.get("default_rate_nm_s", 0.0))
                try:
                    old_rate = float(prior.get("rate_nm_s", 0.0))
                    same_rate = math.isclose(old_rate, rate,
                                             rel_tol=1e-12, abs_tol=1e-12)
                except (TypeError, ValueError, OverflowError):
                    old_rate, same_rate = prior.get("rate_nm_s", "?"), False
                if not same_rate:
                    changes.append(f"速率 {old_rate} → {rate:g} nm/s")
            row["change_note"] = "；".join(changes) if changes else "保持"
        return rows

    @Property(str, notify=changed)
    def filmDesignImportDiffSummary(self) -> str:
        preview = self._film_import_preview or {}
        new_refs = {(row.get("surface"), row.get("layer_id"))
                    for row in preview.get("rows", [])}
        previous_refs = []
        ordinary_steps = 0
        for step in self._data.get("steps", []):
            reference = step.get("film_layer_ref")
            if isinstance(reference, dict):
                previous_refs.append((reference.get("surface"), reference.get("layer_id")))
            else:
                ordinary_steps += 1
        added = sum(1 for reference in new_refs if reference not in previous_refs)
        removed = sum(1 for reference in previous_refs if reference not in new_refs)
        changed = sum(1 for row in self.filmDesignImportRows
                      if row.get("change_note") not in ("新增", "保持"))
        kept = len(new_refs) - added - changed
        return (f"与当前 {len(self._data.get('steps', []))} 步对比：新增 {added} 层，"
                f"更新 {changed} 层，保持 {max(0, kept)} 层；移除 {removed} 个旧膜系映射，"
                f"并替换 {ordinary_steps} 个无膜系来源步骤。")

    @Property("QVariantList", notify=changed)
    def filmDesignImportSources(self) -> list:
        return deepcopy(self._source_preview.get("sources", []))

    @Property(str, notify=changed)
    def filmDesignImportError(self) -> str:
        return self._film_import_error

    @Property(bool, notify=changed)
    def filmDesignImportReadyToApply(self) -> bool:
        preview = self._film_import_preview or {}
        rows = preview.get("rows", [])
        if not self._film_import_snapshot or not rows or self._film_import_error:
            return False
        materials = {row.get("material_id") for row in rows}
        if not materials or any(not self._film_import_source_map.get(item) for item in materials):
            return False
        valid_slots = {row.get("slot") for row in self._source_preview.get("sources", [])
                       if row.get("valid")}
        return all(self._film_import_source_map.get(item) in valid_slots for item in materials)

    @Property(int, notify=changed)
    def filmDesignImportMissingSourceCount(self) -> int:
        rows = (self._film_import_preview or {}).get("rows", [])
        valid_slots = {row.get("slot") for row in self._source_preview.get("sources", [])
                       if row.get("valid")}
        materials = {row.get("material_id") for row in rows}
        return sum(1 for item in materials
                   if self._film_import_source_map.get(item) not in valid_slots)

    @Property(int, notify=changed)
    def stepCount(self):
        return len(self._data["steps"])

    @Property(int, notify=changed)
    def stepPage(self):
        return self._step_page

    @Property(int, notify=changed)
    def stepPageCount(self):
        return max(1, (len(self._data["steps"]) + 39) // 40)

    @Property("QVariantList", notify=changed)
    def visibleSteps(self):
        choices = {source["slot"]: source for source in self._source_preview.get("sources", [])}
        return [{**deepcopy(step),
                 "source_file": choices.get(step["source_slot"], {}).get("file", ""),
                 "material_name": choices.get(step["source_slot"], {}).get("material_name", ""),
                 "rate_angstrom_s": (float(step["rate_nm_s"]) * 10.0 if isinstance(step["rate_nm_s"], (int, float)) else step["rate_nm_s"])}
                for step in self._data["steps"][self._step_page * 40:(self._step_page + 1) * 40]]

    @Slot(int)
    def setStepPage(self, value):
        self._step_page = max(0, min(value, self.stepPageCount - 1))
        self.changed.emit()

    @staticmethod
    def _blank() -> dict:
        from coat_sim.core.process_profiles import PROCESS_SCHEMA_VERSION
        return {
            "schema_version": PROCESS_SCHEMA_VERSION,
            "name": "新工艺",
            "machine_file": "",
            "substrate_file": "",
            "steps": [{"source_slot": 1, "target_nm": 100.0,
                       "rate_nm_s": 1.0, "target_mode": "mean"}],
        }

    @Slot()
    @_project_edit()
    def requestFilmDesignImport(self) -> None:
        """Ask the application window registry for the active in-memory design."""
        self._film_import_snapshot = None
        self._film_import_preview = None
        self._film_import_surface = ""
        self._film_import_source_map = {}
        self._film_import_error = ""
        self._film_import_source_label = ""
        if not str(self._data.get("machine_file", "")).strip():
            self.failFilmDesignImport(
                "请先在“方案文件”中浏览或新建机器，配置有效蒸发源后再从膜系导入。")
            return
        if self._package_runtime is None:
            self._refresh_source_preview()
        elif self._package_signature != self._path_version_signature(self._reload_origin):
            # Import must never implicitly replace the current recipe with a
            # changed asset package through the preview's reload shortcut.
            self.failFilmDesignImport("资产包已变化，请刷新并重新载入工艺后再从膜系导入。")
            return
        if not isinstance(self._machine_runtime, dict):
            detail = self._source_preview.get("message", "")
            self.failFilmDesignImport(
                "请先选择并验证机器及其蒸发源后再从膜系导入。"
                + (f"\n{detail}" if detail else ""))
            return
        if not any(row.get("valid") for row in self._source_preview.get("sources", [])):
            self.failFilmDesignImport(
                "当前机器没有有效蒸发源。请先编辑机器并配置已保存的蒸发源后再从膜系导入。")
            return
        self.changed.emit()
        self.filmDesignImportRequested.emit()

    @Slot(str)
    @_project_edit()
    def failFilmDesignImport(self, message: str) -> None:
        """Publish a retrieval error without changing the process document."""
        self._film_import_snapshot = None
        self._film_import_preview = None
        self._film_import_surface = ""
        self._film_import_source_map = {}
        self._film_import_source_label = ""
        self._film_import_error = str(message or "无法取得当前膜系设计。")
        self._set_status(self._film_import_error)
        self.changed.emit()
        self.filmDesignImportReady.emit()

    @Slot(object, str)
    @_project_edit()
    def receiveFilmDesignDraft(self, design, source_label: str = "当前内存膜系") -> bool:
        """Freeze a draft delivered by the active FilmDesign window, without saving it."""
        try:
            from coat_sim.core.film_process_import import freeze_complete_design
            frozen = freeze_complete_design(self.service.film_design_service, design)
        except Exception as exc:
            self._film_import_snapshot = None
            self._film_import_preview = None
            self._film_import_source_map = {}
            self._film_import_error = f"无法冻结当前膜系：{exc}"
            self._set_status(self._film_import_error)
            self.changed.emit()
            self.filmDesignImportReady.emit()
            return False
        self._film_import_snapshot = frozen
        self._film_import_preview = None
        self._film_import_surface = ""
        self._film_import_source_map = {}
        self._film_import_source_label = str(source_label or "当前内存膜系")
        self._film_import_error = "请明确选择 front 或 back 面。"
        self.changed.emit()
        self.filmDesignImportReady.emit()
        return True

    @Slot(str, result=bool)
    @_project_edit()
    def setFilmDesignImportSurface(self, surface: str) -> bool:
        if self._film_import_snapshot is None:
            self._film_import_error = "尚未取得膜系设计。"
            self.changed.emit()
            return False
        try:
            from coat_sim.core.film_process_import import preview_film_design_import
            preview = preview_film_design_import(self._film_import_snapshot, surface)
        except (ValueError, TypeError) as exc:
            self._film_import_surface = ""
            self._film_import_preview = None
            self._film_import_source_map = {}
            self._film_import_error = str(exc)
            self.changed.emit()
            return False
        self._film_import_surface = surface
        self._film_import_preview = preview
        self._film_import_source_map = {}
        self._film_import_error = ""
        self.changed.emit()
        return True

    @Slot(str, int)
    @_project_edit()
    def setFilmDesignImportSource(self, material_id: str, slot: int) -> None:
        if not self._film_import_preview:
            return
        material_ids = {row["material_id"] for row in self._film_import_preview.get("rows", [])}
        if material_id not in material_ids:
            return
        if slot == 0:
            self._film_import_source_map.pop(material_id, None)
        elif type(slot) is int and slot > 0:
            option = next((row for row in self._source_preview.get("sources", [])
                           if row.get("slot") == slot and row.get("valid")), None)
            if option is None:
                self._film_import_error = f"第 {slot} 位蒸发源无效。"
            else:
                self._film_import_source_map[material_id] = slot
                self._film_import_error = ""
        self.changed.emit()

    @Slot(bool, result=bool)
    @_project_edit()
    def applyFilmDesignImport(self, confirm_replace: bool) -> bool:
        if type(confirm_replace) is not bool or not confirm_replace:
            self._film_import_error = "请确认替换当前步骤后再导入。"
            self.changed.emit()
            return False
        if not self.filmDesignImportReadyToApply:
            self._film_import_error = self._film_import_error or "请为每种材料明确选择有效蒸发源位。"
            self.changed.emit()
            return False
        if not isinstance(self._machine_runtime, dict):
            self._film_import_error = "请先选择并验证机器及其蒸发源。"
            self.changed.emit()
            return False
        try:
            from coat_sim.core.film_process_import import build_film_process_import
            result = build_film_process_import(
                self._film_import_snapshot, self._film_import_surface,
                self._film_import_source_map, self._machine_runtime)
            # Replace recipe steps and provenance atomically, preserving the
            # process name, machine, substrate, and environment selections.
            candidate = deepcopy(self._data)
            candidate["steps"] = deepcopy(result["steps"])
            candidate["film_design_snapshot"] = deepcopy(result["film_design_snapshot"])
            candidate["film_design_import"] = deepcopy(result["film_design_import"])
            from coat_sim.core.process_profiles import validate_process_profile
            candidate = validate_process_profile(candidate)
            self._data = candidate
            self._dirty = True
            other = result["preview"]["other_surface"]
            self._set_status(
                f"已导入 {result['preview']['surface']} 面 {len(result['steps'])} 层；"
                f"{other} 面保留在快照中但本工艺未执行（不支持翻面）。")
            self._film_import_error = ""
            self._edit()
            return True
        except (ValueError, TypeError) as exc:
            self._film_import_error = str(exc)
            self.changed.emit()
            return False

    def _base(self) -> Path:
        if self._path:
            return self._path.parent
        if getattr(self.service, "is_project_service", False):
            return Path(cast("ProjectResourceService", self.service).default_directory("process"))
        return Path.cwd()

    def applyDocument(self, data: dict, intended_path: str = "") -> None:
        """Fill an automation draft through the normal document state signals."""
        if self._project_mutations_frozen():
            self._set_status("项目正在关闭或被冻结，当前修改未应用。")
            return
        self._pending_save_as = None
        self._pending_reload = None
        self._reload_origin = None
        self._package_runtime = None
        self._package_signature = None
        self._machine_runtime = None
        self._machine_runtime_path = None
        self._machine_runtime_signature = None
        self._data = deepcopy(data)
        self._path = Path(intended_path).resolve() if intended_path else None
        owner = self._path or self._base() / "new_process.cproc"
        self._dependency_paths = {key: Path(self.service.resolve_reference(owner, self._data[key]))
                                  for key in ("machine_file", "substrate_file")}
        self._dirty = True
        self._set_status("已填入当前工艺，尚未保存；请确认后保存。")
        self._refresh_source_preview()
        self._refresh()

    def _refresh(self) -> None:
        self._step_page = min(self._step_page, self.stepPageCount - 1)
        try:
            validation_path = (None if self._path and self._path.suffix.lower() == ".coatasset"
                               else self._path or self._base() / "new_process.cproc")
            result = self.service.validate("process", self._data, path=validation_path)
            self._ok = bool(result.ok)
            self._errors = [str(issue) for issue in result.errors]
        except Exception as exc:
            self._ok = False
            self._errors = [str(exc)]
        if self._ok and result.data is not None:
            self._data = deepcopy(result.data)
        self.changed.emit()

    def _edit(self) -> None:
        self._pending_reload = None
        self._dirty = True
        self._refresh()

    def _set_status(self, message: str) -> None:
        self._status = message
        self.statusChanged.emit()

    @Property("QVariantMap", notify=changed)
    def documentMetadata(self) -> dict:
        return {key: self._data.get(key, "") for key in ("name", "machine_file", "substrate_file")}

    @Property("QVariantMap", notify=changed)
    def document(self) -> dict:
        return deepcopy(self._data)

    @Property("QVariantList", notify=changed)
    def steps(self) -> list:
        return deepcopy(self._data["steps"])

    @Property(str, notify=changed)
    def environmentDefaultsSummary(self) -> str:
        return self._environment_summary(self._data.get("environment", {}))

    @staticmethod
    def _environment_summary(environment) -> str:
        """Display the input contract, never infer pressure or a temperature law."""
        if not environment:
            return "未记录温压参数 · 兼容旧工艺，输运损失未启用"
        model = environment.get("transport_model") or "disabled"
        parts = ["零碰撞直达束损失" if model == "ballistic_loss" else "仅记录，输运损失未启用"]
        for key, label in (("base_pressure_pa", "本底"), ("operating_pressure_pa", "工作总压")):
            value = environment.get(key)
            if value is not None:
                parts.append(f"{label} {value:g} Pa" if isinstance(value, (float, int)) else f"{label} {value} Pa")
        gases = environment.get("gas_components")
        if gases:
            parts.append(f"{len(gases)} 种气体")
        return " · ".join(parts)

    @Slot(int, result="QVariantMap")
    def environmentEditor(self, index: int) -> dict:
        """Return detached state for the cancel-safe defaults/override dialog."""
        if index < -1 or index >= len(self._data["steps"]):
            return {"raw": {}, "inherited": {}, "effective": {}, "summary": "步骤不存在"}
        from coat_sim.core.process_environment import resolve_environment
        inherited = self._data.get("environment", {}) if index >= 0 else {}
        owner = self._data if index == -1 else self._data["steps"][index]
        raw = owner.get("environment", {})
        try:
            effective = resolve_environment(inherited, raw)
        except (TypeError, ValueError):
            effective = {}
        return {"raw": deepcopy(raw or {}), "inherited": deepcopy(inherited or {}),
                "effective": effective, "summary": self._environment_summary(effective)}

    @Slot(int, "QVariantMap", result="QVariantMap")
    @_project_edit()
    def applyEnvironment(self, index: int, environment: dict) -> dict:
        """Validate all affected effective environments before one atomic edit.

        Omitted fields inherit, null clears, and gas_components is replaced as a
        whole. Resetting every field removes the optional environment object.
        Other process/source/mask fields are deliberately untouched.
        """
        if index < -1 or index >= len(self._data["steps"]):
            return {"ok": False, "message": "步骤已不存在，请重新选择。"}
        from coat_sim.core.process_environment import (
            validate_environment, resolve_environment, environment_report)
        try:
            normalized = validate_environment(dict(environment))
            defaults = normalized if index == -1 else self._data.get("environment", {})
            affected = enumerate(self._data["steps"]) if index == -1 else [(index, self._data["steps"][index])]
            for step_index, step in affected:
                overrides = normalized if index >= 0 else step.get("environment", {})
                try:
                    environment_report(resolve_environment(defaults, overrides))
                except ValueError as exc:
                    raise ValueError(f"第 {step_index + 1} 层：{exc}") from exc
        except (ValueError, TypeError) as exc:
            return {"ok": False, "message": str(exc)}
        owner = self._data if index == -1 else self._data["steps"][index]
        if normalized == owner.get("environment", {}) and (normalized or "environment" not in owner):
            return {"ok": True, "message": ""}
        if normalized:
            owner["environment"] = normalized
        else:
            owner.pop("environment", None)
        self._edit()
        self._set_status("已更新工艺默认温压参数。" if index == -1 else f"已更新第 {index + 1} 层温压覆盖。")
        return {"ok": True, "message": ""}

    @Property(str, notify=changed)
    def currentPath(self) -> str:
        return str(self._path) if self._path else ""

    @Property(str, notify=changed)
    def fileName(self) -> str:
        return self._path.name if self._path else "未保存"

    @Slot(str, result=str)
    def dependencyPath(self, key: str) -> str:
        if key not in ("machine_file", "substrate_file"):
            return ""
        # A package member is not a host filesystem dependency. The embedded
        # runtime is the only valid source for a packaged process preview.
        if self._package_runtime is not None:
            return ""
        reference = str(self._data.get(key) or "").strip()
        if not reference:
            return ""
        try:
            path = self._dependency_paths.get(key)
            if path is None:
                owner = self._path or self._base() / "new_process.cproc"
                path = Path(self.service.resolve_reference(owner, reference))
            if getattr(self.service, "is_project_service", False):
                return str(path)
            return str(path.resolve()) if path.is_file() else ""
        except (OSError, TypeError, ValueError):
            return ""

    @Property(bool, notify=changed)
    def dirty(self) -> bool:
        return self._dirty

    @Property(bool, notify=changed)
    def canReload(self) -> bool:
        return self._reload_origin is not None

    @Slot(result=bool)
    @_project_edit()
    def reloadCurrent(self) -> bool:
        if not self.canReload:
            self._set_status("请先打开或保存文件，再刷新。")
            return False
        if self._dirty:
            pending = (self._reload_origin, deepcopy(self._data))
            if pending != self._pending_reload:
                self._pending_reload = pending
                self.reloadRequested.emit()
            return False
        self._pending_reload = None
        return self._reload_now()

    @Slot(bool, result=bool)
    @_project_edit(False)
    def confirmReload(self, confirmed: bool) -> bool:
        pending, self._pending_reload = self._pending_reload, None
        if not confirmed or pending is None:
            return False
        if pending != (self._reload_origin, self._data):
            self._set_status("文件或当前内容已更改，请重新刷新。")
            return False
        return self._reload_now()

    def _reload_now(self) -> bool:
        try:
            doc = _checked_reload_document(self.service, "process", self._reload_origin)
            data = deepcopy(doc.data)
            path = Path(doc.path)
            package = path.suffix.lower() == ".coatasset"
            dependencies = ({} if package else
                            {key: Path(self.service.resolve_reference(path, data[key]))
                             for key in ("machine_file", "substrate_file")})
            # Use the checked dependency graph, so preview and document are
            # committed together and a failed load cannot clear the preview.
            runtime = doc.runtime or {}
            machine = runtime.get("machine")
            if machine is None:
                raise ValueError("工艺依赖中没有可用的机器数据")
            preview = self._source_preview_from_machine(
                machine, machine, include_geometry=self._preview_geometry_requested)
            package_signature = self._path_version_signature(path) if package else None
            machine_path = runtime.get("machine_path")
            machine_runtime_path = (None if package or not machine_path else
                                    Path(machine_path).resolve())
            machine_signature = (self._machine_file_signature(machine_runtime_path, machine)
                                 if machine_runtime_path is not None else None)
            # Resolve all external signatures before replacing the live view;
            # a failed validation must leave the old document intact.
            self._data, self._path = data, path
            self._dependency_paths = dependencies
            self._source_preview = preview
            self._package_runtime = deepcopy(doc.runtime) if package else None
            self._package_signature = package_signature
            self._machine_runtime = machine
            self._machine_runtime_path = machine_runtime_path
            if machine_runtime_path is not None:
                self._dependency_paths["machine_file"] = machine_runtime_path
            self._machine_runtime_signature = machine_signature
            self._step_page = min(self._step_page, self.stepPageCount - 1)
            self._dirty, self._ok, self._errors = False, True, []
            self._pending_save_as = None
            self.changed.emit()
            self._set_status(f"已刷新：{self._reload_origin.name}")
            return True
        except Exception as exc:
            self._set_status(f"刷新失败，已保留当前内容：{exc}")
            return False

    @Property(bool, notify=changed)
    def valid(self) -> bool:
        return self._ok

    @Property("QStringList", notify=changed)
    def errors(self) -> list[str]:
        return list(self._errors)

    @Property(str, notify=statusChanged)
    def status(self) -> str:
        return self._status

    @Property("QVariantMap", notify=changed)
    def sourcePreview(self) -> dict:
        """Fixed-frame machine XY positions for the process editor's top view."""
        return deepcopy(self._source_preview)

    def _refresh_source_preview(self, *, force: bool = False,
                                include_geometry: bool | None = None) -> None:
        if include_geometry is None:
            include_geometry = self._preview_geometry_requested
        if self._package_runtime is not None:
            package_changed = (
                self._package_signature != self._path_version_signature(self._reload_origin))
            if package_changed:
                if not self._dirty and self._reload_origin and self._reload_now():
                    return
                self._source_preview = {
                    "sources": [],
                    "message": "资产包已变化，请重新载入后更新预览。",
                }
                return
            if force and not self._dirty and self._reload_origin:
                self._preview_geometry_requested = bool(include_geometry)
                if self._reload_now():
                    return
                self._source_preview = {
                    "sources": [],
                    "message": "无法重新载入资产包，请检查文件后重试。",
                }
                return
            machine = self._package_runtime["machine"]
            self._machine_runtime = machine
            self._machine_runtime_path = None
            self._machine_runtime_signature = None
            self._source_preview = self._source_preview_from_machine(
                machine, machine, include_geometry=include_geometry)
            return
        reference = str(self._data.get("machine_file", "")).strip()
        if not reference:
            self._machine_runtime = None
            self._machine_runtime_path = None
            self._machine_runtime_signature = None
            self._source_preview = {"sources": [], "message": "请选择机器文件。"}
            return
        try:
            path = self._dependency_paths.get("machine_file")
            if path is None:
                owner = self._path or self._base() / "new_process.cproc"
                path = Path(self.service.resolve_reference(owner, reference))
            path = path.resolve()
            runtime = self._machine_runtime
            signature = (self._machine_file_signature(path, runtime)
                         if runtime is not None else None)
            if (force or runtime is None or self._machine_runtime_path != path
                    or signature != self._machine_runtime_signature):
                machine_doc = self.service.load("machine", path)
                runtime = machine_doc.runtime
                if runtime is None:
                    raise ValueError("机器文件的依赖尚未解析")
                self._machine_runtime = runtime
                self._machine_runtime_path = path
                self._machine_runtime_signature = self._machine_file_signature(path, runtime)
            self._source_preview = self._source_preview_from_machine(
                runtime, runtime, include_geometry=include_geometry)
        except Exception as exc:
            self._machine_runtime = None
            self._machine_runtime_path = None
            self._machine_runtime_signature = None
            self._source_preview = {"sources": [], "message": f"无法预览机器源位置：{exc}"}

    def _machine_file_signature(self, machine_path: Path, machine) -> tuple:
        """Version stamp the validated machine graph before reusing its runtime."""
        if getattr(self.service, "is_project_service", False):
            managed_paths = [str(machine_path)]
            for collection in ("sources", "masks"):
                for item in (machine or {}).get(collection, []):
                    reference = str(item.get("file", "")).strip()
                    if reference:
                        try:
                            managed_paths.append(str(self.service.resolve_reference(machine_path, reference)))
                        except (OSError, TypeError, ValueError):
                            managed_paths.append(reference)
            # Project documents are immutable managed snapshots; stable handles
            # identify the graph without touching host filesystem metadata.
            return tuple(managed_paths)
        paths = [Path(machine_path)]
        for collection in ("sources", "masks"):
            for item in (machine or {}).get(collection, []):
                reference = str(item.get("file", "")).strip()
                if not reference:
                    continue
                try:
                    paths.append(Path(self.service.resolve_reference(machine_path, reference)))
                except (OSError, TypeError, ValueError):
                    paths.append(Path(reference))
        stamps: list[tuple[str, int | None, int | None, int | None]] = []
        for path in paths:
            try:
                resolved = path.resolve()
                info = resolved.stat()
                stamps.append((str(resolved), info.st_mtime_ns, info.st_ctime_ns,
                               info.st_size))
            except (OSError, RuntimeError, ValueError):
                stamps.append((str(path), None, None, None))
        return tuple(stamps)

    @staticmethod
    def _path_version_signature(path: Path | None) -> tuple | None:
        if path is None:
            return None
        try:
            resolved = Path(path).resolve()
            info = resolved.stat()
            return str(resolved), info.st_mtime_ns, info.st_ctime_ns, info.st_size
        except (OSError, RuntimeError, ValueError):
            return str(Path(path)), None, None, None

    @staticmethod
    def _source_preview_from_machine(machine, runtime, *, include_geometry: bool = True) -> dict:
        from coat_sim.core.process_profiles import source_selection_options
        masks = []
        for index, mask in enumerate(machine["masks"]):
            entry = {"slot": index, "file": Path(mask["file"]).name}
            profile = runtime["masks"][index].get("profile") if runtime else None
            if profile:
                points = profile["outer"]
                entry.update(z_mm=profile["z_mm"], installed=True, x_min_mm=min(p[0] for p in points),
                    x_max_mm=max(p[0] for p in points), y_min_mm=min(p[1] for p in points),
                    y_max_mm=max(p[1] for p in points))
                if include_geometry:
                    entry.update(outer=points, holes=profile.get("holes", []))
            masks.append(entry)
        return {
            "sources": [{**option, "x_mm": source["x_mm"], "y_mm": source["y_mm"],
                         "z_mm": source.get("z_mm", 0.0)}
                        for option, source in zip(source_selection_options(runtime), machine["sources"], strict=False)],
            "monitors": [{**deepcopy(monitor), "slot": index, "name": monitor.get("name", f"监测点 {index}")}
                         for index, monitor in enumerate(machine.get("monitors", []), 1)],
            "carrier_x_mm": machine["carrier_x_mm"], "carrier_y_mm": machine["carrier_y_mm"],
            "planet_offset_mm": machine["planet_offset_mm"], "masks": masks,
            "substrate_z_mm": machine["substrate_z_mm"], "message": "",
        }

    @Property(int, notify=changed)
    def maskCount(self):
        return len(self._source_preview.get("masks", []))

    @Property("QVariantList", notify=changed)
    def optimizationMasks(self):
        """Editable design domains; unused slots need no dummy mask file."""
        existing = deepcopy(self._source_preview.get("masks", []))
        sources = self._source_preview.get("sources", [])
        source = sources[0] if sources else {"x_mm": 0, "y_mm": 0, "z_mm": 0}
        source_z = max((item.get("z_mm", 0) for item in sources), default=0)
        z = (source_z + self._source_preview.get("substrate_z_mm", 1000)) / 2
        extent = max(50.0, 0.5 * (self._source_preview.get("planet_offset_mm", 100) + 100))
        cx = (source["x_mm"] + self._source_preview.get("carrier_x_mm", 0)) / 2
        cy = (source["y_mm"] + self._source_preview.get("carrier_y_mm", 0)) / 2
        for _ in range(max(2, len(existing) + 1) - len(existing)):
            existing.append({"slot": len(existing), "installed": False, "z_mm": z,
                             "x_min_mm": cx-extent, "x_max_mm": cx+extent,
                             "y_min_mm": cy-extent, "y_max_mm": cy+extent})
        return existing

    @Property("QVariantMap", notify=optimizationChanged)
    def optimization(self):
        return deepcopy(self._optimization)

    @Property(str, notify=changed)
    def optimizationAvailability(self):
        if not self._ok:
            return "优化前须补全有效依赖：" + (self._errors[0] if self._errors else "请选择机器和基片。")
        if not self.maskCount:
            return "尚无已安装修正板；可选择从零自动设计，并确认新板高度与 XY 包络。"
        if not any((list(range(self.maskCount)) if step.get("mask_slots") is None else step["mask_slots"]) for step in self._data["steps"]):
            return "所有层均未启用修正板；请为需要优化的层选择修正板。"
        return "可优化；结果生成独立副本。"

    @Slot(int, int, bool)
    @_project_edit()
    def setStepMask(self, index, slot, enabled):
        if not 0 <= index < len(self._data["steps"]) or not 0 <= slot < self.maskCount:
            return
        step = self._data["steps"][index]
        selected = set(range(self.maskCount) if step.get("mask_slots") is None else step["mask_slots"])
        selected.add(slot) if enabled else selected.discard(slot)
        step["mask_slots"] = sorted(selected)
        self._edit()

    @Slot("QVariantMap", "QVariantMap")
    @_project_edit()
    def startOptimization(self, settings, options):
        worker = None
        job_key = None
        previous_worker = self._worker
        try:
            self._refresh_source_preview()
            self._refresh()
            if not self._ok:
                raise ValueError(self.optimizationAvailability)
            settings = _integer_controls(dict(settings))
            options = _integer_controls(dict(options))
            inputs = self.service.freeze_process(self._data, base_dir=self._base())
            step_s = settings.pop("time_step_s", None)
            if step_s is not None:
                settings["time_steps"] = self.service.time_fields(
                    process_path=inputs, time_step_s=step_s)["time_steps"]
            job_key = hashlib.sha256(json.dumps({"files": [(f["path"], f["sha256"]) for f in inputs["files"]],
                "settings": settings, "options": options}, sort_keys=True).encode()).hexdigest()
            if job_key in self._workers:
                self._optimization["message"] = "相同输入已在队列中；可继续编辑后提交新任务。"
                self.optimizationChanged.emit()
                return
            estimate = self.service.estimate_mask_optimization(settings, inputs, options=options)
            self._optimization = {"busy": True, "stage": "queued", "artifacts": {},
                "message": ("已冻结输入并加入计算队列；可以继续编辑。" if estimate["estimated_s"] is not None
                            else "已冻结输入并加入计算队列；首轮评分后给出预计剩余时间。"), "estimated_remaining_s": estimate["estimated_s"],
                "estimate_basis": estimate["basis"], "evaluations": 0,
                "memory_warning": estimate.get("memory_warning", False),
                "memory_estimate_label": estimate.get("memory_estimate_label", ""),
                "max_evaluations": options.get("max_evaluations", 48)}
            if estimate.get("memory_warning"):
                self._optimization["message"] += "；" + estimate.get("memory_estimate_label", "内存可能不足，计算仍会提交")
            worker = _OptimizationWorker(self.service, inputs, dict(settings), dict(options), self,
                                         estimate=estimate)
            self._worker = worker
            self._workers[job_key] = worker
            worker.progress.connect(lambda value, w=worker: self._optimization_progress(value) if w is self._worker else None)
            worker.resultReady.connect(lambda value, w=worker: self._optimization_result(value) if w is self._worker else None)
            worker.failed.connect(lambda message, w=worker: self._optimization_failed(message) if w is self._worker else None)
            worker.finished.connect(lambda w=worker, key=job_key: self._optimization_finished(w, key))
            self.optimizationChanged.emit()
            worker.start()
            # Qt may log an OS thread-creation failure without raising in Python.
            # A completed fast worker is valid; only the never-started state fails.
            if not worker.isRunning() and not worker.isFinished():
                raise RuntimeError("无法创建优化计算线程")
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            if worker is not None and not worker.isRunning() and not worker.isFinished():
                message = worker.fail_start(exc)
                self._workers.pop(job_key, None)
                if self._worker is worker:
                    self._worker = previous_worker
                worker.deleteLater()
            self._optimization.update(busy=bool(self._workers), stage="failed",
                                      message=f"无法开始优化：{message}")
            self.optimizationChanged.emit()

    @Slot(dict)
    def _optimization_progress(self, value):
        self._optimization.update(value)
        names = {"group_complete": "当前源板组完成", "verify_full": "全工艺复算", "baseline": "计算无遮板基线", "current": "计算原板基线", "response": "扫描初形位置",
                 "optimize": "调整轮廓", "verify": "原网格复算", "complete": "优化完成"}
        self._optimization["message"] = names.get(value.get("stage"), "正在优化")
        if self._optimization.get("memory_warning"):
            self._optimization["message"] += "；" + self._optimization.get("memory_estimate_label", "内存可能不足")
        self.optimizationChanged.emit()

    @Slot(object)
    def _optimization_result(self, value):
        if getattr(self.service, "is_project_service", False):
            gate = getattr(self.service, "_gate", None)
            if self._editing_frozen or getattr(gate, "frozen", False):
                # Retain only fields consumed below; worker-owned simulation
                # arrays can be released while this project's exit is paused.
                payload = {key: deepcopy(value[key]) for key in (
                    "pending_masks", "_asset", "result_path", "report",
                    "report_path", "artifacts", "_asset_warning") if key in value}
                payload["simulation"] = {}
                self._pending_frozen_optimization_result = payload
                return
        try:
            if not isinstance(value.get("simulation"), dict):
                raise ValueError("优化结果未包含仿真数据")
            self._pending_masks = deepcopy(value.get("pending_masks", []))
            self._optimization["asset"] = value.get("_asset", {})
            path = Path(value["result_path"])
            report = value["report"]
            groups = report.get("group_reports", [report])
            improved = sum(bool(group.get("improved_on_search_grid")) for group in groups)
            invalid_original = sum(group.get("original_boards_search") is None for group in groups)
            comparison = f" · {len(groups)} 组中 {improved} 组搜索改善"
            if invalid_original:
                comparison += f" · {invalid_original} 组原板不可计算，返回可行候选"
            self._optimization.update(artifacts=value["artifacts"], report_path=value["report_path"],
                result_path=str(path), message=f"优化完成 · 全工艺复算 CV {report['best_verified']['cv_percent']:.4f}%{comparison} · 新修正板设计待保存；原文件未修改")
            if value.get("_asset_warning"):
                self._optimization["message"] += "；" + value["_asset_warning"]
        except Exception as exc:
            self._optimization.update(message=f"优化结果已生成，导出信息失败：{exc}", artifacts=value.get("artifacts", {}))
        self.optimizationChanged.emit()

    @Slot(str)
    def _optimization_failed(self, message):
        self._optimization.update(message=message, stage="cancelled" if "取消" in message else "failed")
        self.optimizationChanged.emit()

    def _optimization_finished(self, worker=None, key=None):
        worker = worker or self._worker
        self._workers.pop(key, None)
        if worker is self._worker:
            self._worker = None
        self._optimization["busy"] = bool(self._workers)
        self.optimizationChanged.emit()
        if worker is not None:
            worker.deleteLater()

    @Slot()
    def cancelOptimization(self):
        if self._workers:
            for worker in self._workers.values():
                worker.cancel_event.set()
            self._optimization["message"] = "正在取消本窗口任务，等待计算检查点。"
            self.optimizationChanged.emit()

    def cancel(self):
        self.cancelOptimization()
        self._film_stack.close()

    @Property("QVariantList", notify=optimizationChanged)
    def pendingMasks(self):
        return deepcopy(self._pending_masks)

    @Slot(QUrl, int, bool)
    @_project_edit()
    def saveOptimizedMask(self, url, index, overwrite=False):
        try:
            if not 0 <= index < len(self._pending_masks):
                raise ValueError("没有待保存的修正板")
            path = default_design_path("mask", _local_path(url))
            managed_existing = bool(getattr(self.service, "is_managed_path", lambda _p: False)(path))
            existing = (managed_existing if getattr(self.service, "is_project_service", False)
                        else path.exists())
            if existing and not overwrite:
                self.service.load("mask", path)
                self.maskOverwriteRequested.emit(QUrl.fromLocalFile(str(path)), index)
                return
            document = self.service.save("mask", path, self._pending_masks[index]["profile"], overwrite=overwrite)
            self._optimization["message"] = f"新修正板已保存：{document.path}"
        except Exception as exc:
            self._optimization["message"] = f"保存修正板失败：{exc}"
        self.optimizationChanged.emit()

    @Slot()
    def openOptimizedProcess(self):
        path = self._optimization.get("artifacts", {}).get("process_path")
        if path and not self._optimization["busy"]:
            self.openPath(QUrl.fromLocalFile(path))

    @Slot()
    def refreshSourcePreview(self) -> None:
        self._preview_geometry_requested = True
        self._refresh_source_preview(force=True, include_geometry=True)
        self.changed.emit()

    @Slot()
    def prepareSourcePreview(self) -> None:
        """Build preview geometry after the first frame from the checked runtime."""
        if self._preview_geometry_requested:
            return
        self._preview_geometry_requested = True
        self._refresh_source_preview(include_geometry=True)
        self.changed.emit()

    @Slot()
    @_project_edit()
    def newDocument(self) -> None:
        if self._dirty:
            self._pending_document_replace = (
                "new", None, self._path, deepcopy(self._data))
            self.documentReplaceRequested.emit()
            return
        self._new_document_now()

    def _new_document_now(self) -> None:
        self._path = None
        self._reload_origin = None
        self._package_runtime = None
        self._package_signature = None
        self._machine_runtime = None
        self._machine_runtime_path = None
        self._machine_runtime_signature = None
        self._pending_reload = None
        self._pending_save_as = None
        self._data = self._blank()
        self._dependency_paths = {}
        self._dirty = False
        self._set_status("已新建工艺；保存前请确认机器和基片文件。")
        self._refresh_source_preview()
        self._refresh()

    @Slot(QUrl)
    @_project_edit(False)
    def openPath(self, url: QUrl) -> None:
        path = _local_path(url)
        if self._dirty:
            self._pending_document_replace = (
                "open", path, self._path, deepcopy(self._data))
            self.documentReplaceRequested.emit()
            return
        self._open_path_now(path)

    def _open_path_now(self, path: Path) -> None:
        self._pending_save_as = None
        if path.suffix.lower() == ".coatasset":
            previous_origin = self._reload_origin
            self._reload_origin = path
            if self._reload_now():
                self._pending_reload = None
                self._set_status(f"已打开：{path.name}")
            else:
                self._reload_origin = previous_origin
            return
        try:
            doc = self.service.load("process", path)
            self._data = deepcopy(doc.data)
            self._step_page = 0
            self._path = Path(doc.path)
            self._reload_origin = Path(doc.path)
            self._package_runtime = None
            self._package_signature = None
            self._pending_reload = None
            self._dependency_paths = {
                key: Path(self.service.resolve_reference(self._path, self._data[key]))
                for key in ("machine_file", "substrate_file")
            }
            runtime = doc.runtime or {}
            self._machine_runtime = runtime.get("machine")
            machine_path = runtime.get("machine_path")
            self._machine_runtime_path = Path(machine_path).resolve() if machine_path else None
            if self._machine_runtime_path:
                self._dependency_paths["machine_file"] = self._machine_runtime_path
            self._machine_runtime_signature = (
                self._machine_file_signature(self._machine_runtime_path,
                                             self._machine_runtime)
                if self._machine_runtime_path and self._machine_runtime else None)
            self._dirty = False
            self._set_status(f"已打开：{self._path.name}")
            if self._machine_runtime is None:
                issue = next(iter(doc.issues), "机器依赖无效")
                self._source_preview = {"sources": [], "message": f"无法预览机器源位置：{issue}"}
            else:
                self._refresh_source_preview(include_geometry=self._preview_geometry_requested)
            self._step_page = min(self._step_page, self.stepPageCount - 1)
            self._ok = bool(doc.valid)
            self._errors = [str(issue) for issue in doc.issues]
            self.changed.emit()
        except Exception as exc:
            self._set_status(f"打开失败：{exc}")

    @Slot(bool, result=bool)
    @_project_edit(False)
    def confirmDocumentReplace(self, confirmed: bool) -> bool:
        pending, self._pending_document_replace = self._pending_document_replace, None
        if not confirmed or pending is None:
            return False
        action, path, expected_path, expected_data = pending
        if expected_path != self._path or expected_data != self._data:
            self._set_status("工艺内容已变化；当前修改保留，请重新操作。")
            return False
        if action == "new":
            self._new_document_now()
        elif action == "open" and path is not None:
            self._open_path_now(path)
        return True

    @Slot(str)
    def openDocument(self, path: str) -> None:
        """Open the explicit path passed by the same-process window launcher."""
        self.openPath(QUrl.fromLocalFile(str(_local_path(path))))

    @Slot(str, str)
    @_project_edit()
    def setField(self, key: str, value: str) -> None:
        if key not in ("name", "machine_file", "substrate_file"):
            return
        self._data[key] = value.strip() if key != "name" else value
        if key in self._dependency_paths:
            del self._dependency_paths[key]
        if key == "machine_file":
            self._machine_runtime = None
            self._machine_runtime_path = None
            self._machine_runtime_signature = None
            self._refresh_source_preview()
        self._edit()

    @Slot(str, QUrl)
    @_project_edit()
    def chooseDependency(self, key: str, url: QUrl) -> None:
        if key not in ("machine_file", "substrate_file"):
            return
        path = _local_path(url)
        owner = self._path or self._base() / "new_process.cproc"
        reference = self.service.relative_reference(owner, path)
        managed_path = None
        machine_runtime = None
        if getattr(self.service, "is_project_service", False):
            # relative_reference imports/copies external dependencies; resolve
            # its UUID reference to the returned project-managed handle.
            managed_path = Path(self.service.resolve_reference(owner, reference))
            if key == "machine_file":
                machine_runtime = self.service.load("machine", managed_path).runtime
        self._data[key] = reference
        if getattr(self.service, "is_project_service", False):
            assert managed_path is not None
            self._dependency_paths[key] = managed_path
            if key == "machine_file":
                self._machine_runtime = machine_runtime
                self._machine_runtime_path = managed_path
                self._machine_runtime_signature = (
                    self._machine_file_signature(managed_path, machine_runtime)
                    if machine_runtime is not None else None)
        else:
            self._dependency_paths[key] = path
        if key == "machine_file":
            if not getattr(self.service, "is_project_service", False):
                self._machine_runtime = None
                self._machine_runtime_path = None
                self._machine_runtime_signature = None
            self._refresh_source_preview()
        self._edit()

    @Slot(int, int)
    @_project_edit()
    def chooseStepSource(self, index: int, slot: int) -> None:
        """Choose a saved design in its installed slot, preserving board choices."""
        if not 0 <= index < len(self._data["steps"]):
            return
        selected = next((source for source in self._source_preview.get("sources", [])
                         if source["slot"] == slot and source.get("valid")), None)
        if selected is None:
            self._set_status("请选择机器中有效的已保存蒸发源设计。")
            return
        self._data["steps"][index].update(source_slot=slot,
            rate_nm_s=selected["default_rate_nm_s"])
        self._sync_imported_material_source(index, slot)
        self._edit()

    def _sync_imported_material_source(self, index: int, slot: int) -> None:
        """Keep imported material-to-source provenance aligned with step edits."""
        if not 0 <= index < len(self._data["steps"]):
            return
        step = self._data["steps"][index]
        layer_ref = step.get("film_layer_ref")
        snapshot = self._data.get("film_design_snapshot")
        imported = self._data.get("film_design_import")
        if not isinstance(layer_ref, dict) or not isinstance(snapshot, dict) or not isinstance(imported, dict):
            return
        surface = layer_ref.get("surface")
        layer_id = layer_ref.get("layer_id")
        design = snapshot.get("design", {})
        layer = next((row for row in design.get(f"{surface}_layers", [])
                      if row.get("id") == layer_id), None)
        material_id = layer.get("material_id") if isinstance(layer, dict) else None
        source_map = imported.get("source_slots_by_material_id")
        if not isinstance(source_map, dict) or material_id not in source_map:
            return
        source_map[material_id] = slot
        for linked_step in self._data["steps"]:
            reference = linked_step.get("film_layer_ref")
            if not isinstance(reference, dict) or reference.get("surface") != surface:
                continue
            linked_layer = next((row for row in design.get(f"{surface}_layers", [])
                                 if row.get("id") == reference.get("layer_id")), None)
            if isinstance(linked_layer, dict) and linked_layer.get("material_id") == material_id:
                linked_step["source_slot"] = slot

    @Slot()
    @_project_edit()
    def addStep(self) -> None:
        sources = self._source_preview.get("sources", [])
        rate = sources[0]["default_rate_nm_s"] if sources else 1.0
        self._data["steps"].append({"source_slot": 1, "target_nm": 100.0,
                                    "rate_nm_s": rate, "target_mode": "mean",
                                    "mask_slots": list(range(self.maskCount))})
        self._edit()

    @Slot(int)
    @_project_edit()
    def deleteStep(self, index: int) -> None:
        if 0 <= index < len(self._data["steps"]):
            del self._data["steps"][index]
            self._edit()

    @Slot(int, int)
    @_project_edit()
    def moveStep(self, index: int, delta: int) -> None:
        target = index + delta
        steps = self._data["steps"]
        if 0 <= index < len(steps) and 0 <= target < len(steps):
            steps[index], steps[target] = steps[target], steps[index]
            self._edit()

    @Slot(int, str, str)
    @_project_edit()
    def setStep(self, index: int, key: str, value: str) -> None:
        if not 0 <= index < len(self._data["steps"]):
            return
        if key not in ("source_slot", "target_nm", "rate_nm_s", "rate_angstrom_s",
                       "target_mode", "monitor_slot"):
            return
        step = self._data["steps"][index]
        if key == "rate_angstrom_s":
            try:
                value = str(float(value) * 0.1)
            except ValueError:
                pass
            key = "rate_nm_s"
        if key == "target_mode":
            step[key] = value
            if value != "monitor":
                step.pop("monitor_slot", None)
            elif "monitor_slot" not in step:
                step["monitor_slot"] = 1
        elif key in ("source_slot", "monitor_slot"):
            try:
                step[key] = int(value)
            except ValueError:
                step[key] = value
            if key == "source_slot" and type(step[key]) is int:
                self._sync_imported_material_source(index, step[key])
        else:
            try:
                step[key] = float(value)
            except ValueError:
                step[key] = value
        self._edit()

    def _rebased(self, target: Path) -> dict:
        data = deepcopy(self._data)
        if getattr(self.service, "is_project_service", False):
            # Resource references are project-local UUID names. Moving a
            # document handle does not change their meaning.
            return data
        for key in ("machine_file", "substrate_file"):
            dependency = self._dependency_paths.get(key)
            if dependency is None:
                dependency = (self._base() / data[key]).resolve()
            data[key] = self.service.relative_reference(target, dependency)
        return data

    def _save_to(self, target: Path, *, overwrite: bool = False) -> None:
        try:
            data = self._rebased(target)
            doc = self.service.save("process", target, data,
                                    overwrite=overwrite)
            self._data = deepcopy(doc.data)
            self._path = Path(doc.path)
            self._reload_origin = self._path
            self._package_runtime = None
            self._package_signature = None
            runtime = doc.runtime or {}
            self._machine_runtime = runtime.get("machine")
            machine_path = runtime.get("machine_path")
            self._machine_runtime_path = Path(machine_path).resolve() if machine_path else None
            if self._machine_runtime_path:
                self._dependency_paths["machine_file"] = self._machine_runtime_path
            self._machine_runtime_signature = (
                self._machine_file_signature(self._machine_runtime_path,
                                             self._machine_runtime)
                if self._machine_runtime_path and self._machine_runtime else None)
            self._pending_reload = None
            self._dirty = False
            self._set_status(f"已保存：{self._path.name}")
            if self._machine_runtime is None:
                issue = next(iter(doc.issues), "机器依赖无效")
                self._source_preview = {"sources": [], "message": f"无法预览机器源位置：{issue}"}
            else:
                self._refresh_source_preview(include_geometry=self._preview_geometry_requested)
            self._ok = bool(doc.valid)
            self._errors = [str(issue) for issue in doc.issues]
            self.changed.emit()
        except Exception as exc:
            self._set_status(f"无法保存：{exc}")

    @Slot()
    @_project_edit()
    def save(self) -> None:
        if self._path is None:
            self._set_status("请先选择保存位置。")
            return
        self._save_to(self._path, overwrite=True)

    @Slot(QUrl)
    @_project_edit()
    def saveAsPath(self, url: QUrl) -> None:
        self._pending_save_as = None
        try:
            target = default_design_path("process", _local_path(url))
            managed_existing = bool(getattr(self.service, "is_managed_path", lambda _p: False)(target))
            existing = (managed_existing if getattr(self.service, "is_project_service", False)
                        else target.exists())
            if existing:
                self.service.load("process", target)
        except (ValueError, OSError) as exc:
            self._set_status(f"无法保存，原文件未覆盖；请使用其他文件名：{exc}")
            return
        managed_existing = bool(getattr(self.service, "is_managed_path", lambda _p: False)(target))
        existing = (managed_existing if getattr(self.service, "is_project_service", False)
                    else target.exists())
        if existing:
            self._pending_save_as = target
            self.overwriteRequested.emit(QUrl.fromLocalFile(str(target)))
            return
        self._save_to(target)

    @Slot(bool)
    @_project_edit(False)
    def confirmSaveAs(self, confirmed: bool) -> None:
        target, self._pending_save_as = self._pending_save_as, None
        if confirmed and target is not None:
            self._save_to(target, overwrite=True)
