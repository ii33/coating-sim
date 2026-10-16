"""Qt Quick bridge for the appearance-first optical workspace.

The controller owns page/document state. The shared scheduler owns numerical
jobs and cancellation; this window uses a worker pool only for file operations,
scheduler RPCs, and result download, reporting through queued Qt signals.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import json
import math
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Mapping
from coat_sim.project_session import OperationBinding

from PySide6.QtCore import QObject, Property, QTimer, QUrl, Signal, Slot

from coat_sim.ui.appearance import AppearanceWindowController, _profile_geometry

from coat_sim.core.atomic_io import replace_with_retry
from coat_sim.core.geometric_raytrace import (SCHEMA, SCHEMA_VERSION,
                                              request_limits)
from coat_sim.core.geometric_raytrace_result import (
    geometric_raytrace_summary_matches_result,
    validate_geometric_raytrace_result,
)
from coat_sim.core.ray_trace_job import (inspect_layer_fields_asset,
                                         freeze_raytrace_spec)
from coat_sim.scheduler import SchedulerClient
from coat_sim.scheduler.result_capability import is_geometric_raytrace_result


UI_DOCUMENT_SCHEMA = "coat_sim.geometric_raytrace_ui_document"
UI_DOCUMENT_VERSION = 1
MAX_REQUEST_BYTES = 20 * 1024 * 1024
MAX_RESULT_BYTES = 512 * 1024 * 1024
MAX_UI_RAYS = 512
MAX_DISPLAY_RAYS = 400


class _WorkerRelay(QObject):
    completed = Signal(int, str, object, str)
    progress = Signal(int, object)


def _as_local_path(value: Any, *, resolve: bool = True) -> Path:
    if isinstance(value, QUrl):
        value = value.toLocalFile() or value.toString()
    text = str(value or "").strip()
    if text.startswith("file:"):
        url = QUrl(text)
        text = url.toLocalFile() or text
    if not text:
        raise ValueError("未选择文件")
    path = Path(text).expanduser()
    return path.resolve() if resolve else path.absolute()


def _read_json_object(path: Path, *, max_bytes: int = MAX_REQUEST_BYTES) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"文件不存在：{path}")
    if path.stat().st_size > max_bytes:
        raise ValueError(f"文件超过 {max_bytes // (1024 * 1024)} MiB 上限")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("JSON 根节点必须是对象")
    return value


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def _finite_float(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label}须为有限数值")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}须为有限数值") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label}须为有限数值")
    return number


def generate_source_samples(mode: str, count: int, x_mm: float, z_mm: float,
                            extent: float) -> list[dict[str, Any]]:
    """Build deterministic point/angular or extended/collimated source rays.

    ``point`` samples directions uniformly by angle over the inclusive field;
    ``extended`` samples origins uniformly along an x-line and travels parallel
    to +z. Weights are equal relative-energy weights and are not normalized.
    """
    mode = str(mode).strip().lower()
    if mode not in ("point", "extended"):
        raise ValueError("光源类型须为 point 或 extended")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_UI_RAYS:
        raise ValueError(f"光线数须为 1–{MAX_UI_RAYS} 的整数")
    x = _finite_float(x_mm, "光源 x")
    z = _finite_float(z_mm, "光源 z")
    span = _finite_float(extent, "光源角度/宽度")
    if mode == "point":
        if span < 0 or span >= 89.0:
            raise ValueError("点光源半视场须在 0 到 89 度之间")
        angles = ([0.0] if count == 1 else
                  [-span + 2.0 * span * i / (count - 1) for i in range(count)])
        prefix = "point"
        samples = []
        for i, angle in enumerate(angles, 1):
            radians = math.radians(angle)
            samples.append({
                "sample_id": f"{prefix}-{i:03d}",
                "origin_xz_mm": [x, z],
                "direction_xz": [math.sin(radians), math.cos(radians)],
                "weight": 1.0,
            })
        return samples
    if span < 0:
        raise ValueError("扩展光源宽度不能为负数")
    positions = ([x] if count == 1 else
                 [x - span / 2.0 + span * i / (count - 1) for i in range(count)])
    return [{
        "sample_id": f"extended-{i:03d}",
        "origin_xz_mm": [position, z],
        "direction_xz": [0.0, 1.0],
        "weight": 1.0,
    } for i, position in enumerate(positions, 1)]


def _index_for_design_reference(reference: Mapping[str, Any], wavelength_nm: float,
                                design: Mapping[str, Any], materials, samplers,
                                warnings: list[str]) -> complex:
    if reference.get("kind") == "constant":
        return complex(float(reference["n"]), 0.0)
    if "material_id" not in reference or "optical_set_id" not in reference:
        raise ValueError("膜系材料引用缺少 material_id / optical_set_id")
    material_id = str(reference["material_id"])
    set_id = str(reference["optical_set_id"])
    key = (material_id, set_id)
    if key not in samplers:
        from coat_sim.core.optical_constants import prepare_optical_set

        material = materials.get(material_id)
        optical_set = next((item for item in material["optical_sets"]
                            if item["id"] == set_id), None)
        if optical_set is None:
            raise ValueError(f"{material_id}: 找不到光学常数组 {set_id}")
        samplers[key] = prepare_optical_set(
            optical_set, k_sampling_policy="source_only_zero_defaults")
    n, k, details = samplers[key](
        wavelength_nm, "nm", extrapolation_policy="error",
        include_k=bool(design.get("use_k", True)))
    if k is None:
        if not bool(design.get("assume_lossless", False)):
            raise ValueError(f"{material_id}/{set_id} 的 k 未知，膜系未声明无损假设")
        k = 0.0
    if details.get("k_default_reason"):
        warning = (f"{material_id}/{set_id} @ {wavelength_nm:g} nm："
                   f"k 按膜系仿真的零缺省规则处理（{details['k_default_reason']}）")
        if warning not in warnings:
            warnings.append(warning)
    return complex(float(n), float(k))


def _build_film_design_coating(root: Path, design_path: Path,
                               request: Mapping[str, Any], surface_id: str,
                               side: str, *, service=None) -> tuple[dict[str, Any], list[str]]:
    """Sample one existing cfilm face with the project's optical-set evaluator."""
    from coat_sim.core.film_design import load_design, physical_thickness_nm
    from coat_sim.core.film_materials import FilmMaterialLibrary

    design = service.load(design_path) if service is not None else load_design(design_path)
    side = str(side).lower()
    if side == "front":
        stored_layers = design["front_layers"]
        ordered_layers = list(reversed(stored_layers))  # ambient -> substrate
        before_ref, after_ref = design["incident_medium"], design["substrate"]
    elif side == "back":
        if design["substrate"]["sides"] != "double":
            raise ValueError("所选膜系只有单面；背面导入需要双面膜系")
        stored_layers = design["back_layers"]
        ordered_layers = list(stored_layers)  # substrate -> exit medium
        before_ref, after_ref = design["substrate"], design["exit_medium"]
    else:
        raise ValueError("膜系表面须为 front 或 back")
    if not ordered_layers:
        raise ValueError(f"所选膜系 {side} 面没有膜层")

    surfaces = request.get("surfaces")
    surface = next((row for row in surfaces if isinstance(row, dict) and
                    row.get("surface_id") == surface_id), None) if isinstance(surfaces, list) else None
    if surface is None:
        raise ValueError(f"请求中找不到光学面 {surface_id!r}")
    spectrum = request.get("spectrum")
    if not isinstance(spectrum, list) or not spectrum:
        raise ValueError("先在请求中提供至少一个波长，再导入膜系")
    waves = [_finite_float(row.get("wavelength_nm"), "谱线波长")
             for row in spectrum if isinstance(row, dict)]
    if len(waves) != len(spectrum):
        raise ValueError("请求谱线结构无效")

    library = service.materials if service is not None else FilmMaterialLibrary(root)
    samplers: dict[tuple[str, str], Any] = {}
    warnings: list[str] = []
    indices: list[list[dict[str, float]]] = []
    for wave in waves:
        row = [_index_for_design_reference(ref, wave, design, library,
                                           samplers, warnings)
               for ref in (before_ref, *ordered_layers, after_ref)]
        if abs(row[0].imag) > 1e-14 or abs(row[-1].imag) > 1e-14:
            raise ValueError("几何传播只支持透明体介质；膜层可保留非零 k")
        indices.append([{"n": value.real, "k": value.imag} for value in row])

    actual_before = surface.get("medium_before")
    actual_after = surface.get("medium_after")
    if not isinstance(actual_before, list) or not isinstance(actual_after, list) or \
            len(actual_before) != len(waves) or len(actual_after) != len(waves):
        raise ValueError("目标光学面的前后介质数量须与请求谱线数一致")
    for i, row in enumerate(indices):
        for actual, expected, name in ((actual_before[i], row[0], "入射端口"),
                                       (actual_after[i], row[-1], "出射端口")):
            if (not isinstance(actual, Mapping) or
                    abs(complex(float(actual.get("n")), float(actual.get("k"))) -
                        complex(expected["n"], expected["k"])) > 1e-12):
                raise ValueError(f"膜系 {name} 与光学面 {surface_id} 的介质不一致；请先调整请求端口")

    optical_layers = []
    for layer in ordered_layers:
        reference_n = _index_for_design_reference(
            layer, design["reference_wavelength_nm"], design, library,
            samplers, warnings)
        thickness_nm = physical_thickness_nm(
            layer, design["reference_wavelength_nm"], reference_n.real)
        optical_layers.append({"layer_id": layer["id"],
                               "thickness_nm": float(thickness_nm)})

    updated = copy.deepcopy(dict(request))
    destination = next(row for row in updated["surfaces"]
                       if row.get("surface_id") == surface_id)
    destination["coating"] = {"indices": indices, "layers": optical_layers}
    return updated, warnings


def _layer_asset_summary(path: Path, root: Path, *, cancel_event=None,
                         expected_sha256: str | None = None) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"沉积资产不存在：{path}")
    inspected = inspect_layer_fields_asset(
        path, root=root, expected_sha256=expected_sha256,
        cancel_event=cancel_event)
    manifest = inspected["manifest"]
    fields = inspected["fields"]
    appearance_context = None
    appearance_error = "所选资产没有冻结的 v7 膜系快照。"
    if manifest.get("captured") is True and fields:
        try:
            appearance_context = _appearance_context_from_asset(
                path, root, inspected["sha256"], cancel_event=cancel_event)
            appearance_error = ""
        except (OSError, ValueError, TypeError, KeyError) as exc:
            appearance_error = str(exc)
    return {"path": str(path), "sha256": inspected["sha256"], "fields": fields,
            "coordinates": manifest.get("coordinates", {}),
            "step_count": len(fields), "appearance_context": appearance_context,
            "appearance_error": appearance_error}


def _appearance_context_from_asset(path: Path, root: Path, asset_sha256: str,
                                   *, cancel_event=None) -> dict[str, Any]:
    """Keep the UI entry point while resolving asset data without Qt imports."""
    from coat_sim.ui.appearance_worker import _appearance_context_from_asset as resolve
    return resolve(path, root, asset_sha256, cancel_event=cancel_event)

def _referenced_steps(request: Mapping[str, Any]) -> set[str]:
    result = set()
    for surface in request.get("surfaces", []):
        if not isinstance(surface, Mapping):
            continue
        coating = surface.get("coating")
        if not isinstance(coating, Mapping):
            continue
        for layer in coating.get("layers", []):
            if isinstance(layer, Mapping) and isinstance(layer.get("deposition_step_id"), str):
                result.add(layer["deposition_step_id"])
    return result


def _find_registered_deposition_bundle(client: SchedulerClient,
                                       expected_sha256: str) -> dict[str, str] | None:
    """Find a selected bundle only through the scheduler's public queue pages."""
    for bucket in ("recent", "history"):
        cursor = None
        for _page_number in range(100):
            page = client.queue_jobs_page(bucket, before=cursor, limit=100)
            rows = list(page.get("jobs", []))
            if bucket == "recent":
                rows.extend(page.get("active_jobs", []))
            for job in rows:
                result = job.get("result")
                if not isinstance(result, Mapping):
                    continue
                for artifact in result.get("result_artifacts", []):
                    if (isinstance(artifact, Mapping)
                            and artifact.get("role") == "bundle"
                            and artifact.get("sha256") == expected_sha256
                            and isinstance(artifact.get("artifact_id"), str)):
                        return {"artifact_id": artifact["artifact_id"],
                                "sha256": expected_sha256}
            if not page.get("has_more") or page.get("next_cursor") is None:
                break
            cursor = page["next_cursor"]
    return None


def _request_document_payload(request: Mapping[str, Any], asset: Mapping[str, Any] | None,
                              document_path: Path | None) -> dict[str, Any]:
    asset_ref = None
    if asset and asset.get("path") and asset.get("sha256"):
        asset_path = Path(asset["path"])
        if document_path is not None:
            try:
                saved_path = os.path.relpath(asset_path, document_path.parent)
            except ValueError:
                saved_path = str(asset_path)
        else:
            saved_path = str(asset_path)
        asset_ref = {"path": saved_path, "sha256": str(asset["sha256"])}
    return {"schema": UI_DOCUMENT_SCHEMA,
            "schema_version": UI_DOCUMENT_VERSION,
            "request": copy.deepcopy(dict(request)),
            "deposition_asset": asset_ref}


def _write_json_file(path: Path, value: Mapping[str, Any], *, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise FileExistsError(str(path))
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            replace_with_retry(temporary, path)
        else:
            # Match the design-document no-overwrite publication contract. Do
            # not fall back to a non-atomic write if hard-link publication is
            # unavailable in the current filesystem sandbox.
            os.link(temporary, path)
            os.unlink(temporary)
    finally:
        try:
            if os.path.exists(temporary):
                os.unlink(temporary)
        except OSError:
            pass


def _copy_result_file(source: Path, destination: Path, *, overwrite: bool) -> str:
    """Publish an explicitly requested preview export atomically."""
    import shutil
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + destination.stem + "-", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_stream:
            shutil.copyfileobj(input_stream, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        if overwrite:
            replace_with_retry(temporary, destination)
        else:
            os.link(temporary, destination)
            os.unlink(temporary)
        return str(destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class RayTraceWindowController(QObject):
    changed = Signal()
    documentChanged = Signal()
    taskCompleted = Signal(int, str, object, str)
    taskProgress = Signal(int, object)
    queueStatusReceived = Signal(int, object, str)
    closeConfirmationRequested = Signal()
    closeReady = Signal()
    requestSaveAsRequested = Signal()
    requestOverwriteRequested = Signal(str, str)
    filmImportConfirmationRequested = Signal(str)
    appearanceRequested = Signal(object)

    def __init__(self, root: str | Path | None = None, parent: QObject | None = None,
                 queue_client: SchedulerClient | None = None, *,
                 project_context=None, project_module_key: str = "ray_trace",
                 project_navigation=None):
        super().__init__(parent)
        self.project_context = project_context
        self.project_module_key = project_module_key
        self.project_navigation = project_navigation
        self._project_compute = None
        self._project_operation: OperationBinding | None = None
        self._queue_operation: OperationBinding | None = None
        self._pending_project_task: tuple[Any, ...] | None = None
        if project_context is not None:
            from coat_sim.core.project_compute import ProjectComputeWorkspace
            self._project_compute = ProjectComputeWorkspace.for_project(
                project_context.host, project_context.binding)
            if root is not None and Path(root).resolve() != self._project_compute.root:
                raise ValueError("Ray-trace root differs from its bound project")
            root = self._project_compute.root
        self.root = Path(root).resolve() if root is not None else Path(__file__).resolve().parents[2]
        self._queue_client = queue_client or SchedulerClient(self.root)
        self._relay = _WorkerRelay()
        self._relay.completed.connect(self._receive_task)
        self._relay.progress.connect(self._receive_progress)
        self.queueStatusReceived.connect(self._receive_queue_status)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="raytrace-ui")
        self._active_token = 0
        self._active_kind = ""
        self._active_cancel: threading.Event | None = None
        self._closed = False
        self._close_approved = False
        self._close_after_save = False
        self._managed_close_save_id = ""
        self._pending_overwrite: tuple[str, str] | None = None
        self._pending_film_import: tuple[str, str, str] | None = None
        self._request: dict[str, Any] | None = None
        self._request_text = ""
        self._request_error = ""
        self._request_path = ""
        self._saved_snapshot = ""
        self._asset: dict[str, Any] | None = None
        self._asset_error = ""
        self._workspace_mode = ("geometric" if project_context is not None
                                and project_module_key == "ray_trace" else "appearance")
        self._appearance: AppearanceWindowController | None = None
        self._close_after_appearance = False
        self._result: dict[str, Any] | None = None
        self._result_path = ""
        self._result_error = ""
        self._phase = "empty"
        self._status = "选择逐层工艺资产，设置光源和相机后渲染外观。"
        self._error = ""
        self._busy = False
        self._progress = 0.0
        self._selected_surface = ""
        self._selected_layer = ""
        self._selected_step = ""
        self._spectrum_index = 0
        self._queue_job_id = ""
        self._queue_job_state = ""
        self._queue_poll_inflight = False
        self._close_after_queue = False
        self._queue_timer = QTimer(self)
        self._queue_timer.setInterval(400)
        self._queue_timer.timeout.connect(self._poll_raytrace_job)
        if self._project_compute is not None:
            self._project_compute.register_thaw_callback(self._on_project_unfrozen)

    def _on_project_unfrozen(self) -> None:
        pending, self._pending_project_task = self._pending_project_task, None
        if pending is not None and not self._closed:
            self._receive_task(*pending)

    def _project_current(self, operation=None) -> bool:
        if self._project_compute is None:
            return True
        selected = operation if operation is not None else self._project_operation
        return selected is not None and self._project_compute.accepts(selected)

    def _project_input(self, path: Path, category: str) -> Path:
        return (self._project_compute.import_asset(path, category=category)
                if self._project_compute is not None else path)

    def _project_destination(self, path: Path) -> Path:
        return (self._project_compute.destination(path)
                if self._project_compute is not None else path)

    def _project_film_handle(self, path: Path) -> Path:
        if self.project_context is None:
            return path
        service = self.project_context.service
        store = service.project_store
        for row in store.manifest["documents"]:
            if row["kind"] == "film" and path in {
                    (store.root / row["path"]).resolve(),
                    (store.root / "documents" / (row["id"] + ".cfilm")).resolve()}:
                return service.document_handle(row["id"])
        materials = {row["id"]: row for row in service.materials._records()}
        return service.import_film(path, materials=materials).handle

    def _emit_document(self) -> None:
        self.documentChanged.emit()
        self.changed.emit()

    def _current_snapshot(self) -> str:
        asset = None
        if self._asset:
            asset = {"path": self._asset.get("path", ""),
                     "sha256": self._asset.get("sha256", "")}
        return _canonical({"request_text": self._request_text,
                           "asset": asset,
                           "request_path": self._request_path})

    def _parse_request_text(self) -> dict[str, Any] | None:
        self._request_error = ""
        if not self._request_text.strip():
            return None
        try:
            value = json.loads(self._request_text)
        except (json.JSONDecodeError, ValueError) as exc:
            self._request_error = f"请求 JSON 无效：{exc}"
            return None
        if not isinstance(value, dict):
            self._request_error = "请求 JSON 根节点必须是对象。"
            return None
        if value.get("schema") != SCHEMA or value.get("schema_version") != SCHEMA_VERSION:
            self._request_error = f"需要 {SCHEMA} v{SCHEMA_VERSION} 请求。"
            return None
        return value

    def _referenced_step_ids(self) -> set[str]:
        request = self._parse_request_text()
        return _referenced_steps(request) if request is not None else set()

    def _missing_asset_steps(self) -> set[str]:
        required = self._referenced_step_ids()
        available = {row.get("step_id") for row in (self._asset or {}).get("fields", [])}
        return required - available

    def _mark_request_changed(self) -> None:
        self._request = self._parse_request_text()
        self._result = None
        self._result_path = ""
        self._result_error = ""
        self._error = ""
        if self._busy:
            return
        if self._request is None:
            self._phase = "empty"
            self._status = self._request_error or "请求尚未准备好。"
        else:
            self._phase = "ready"
            self._status = "请求已更新；运行前会验证全部参数。"
        self._emit_document()

    def _start_task(self, kind: str, function, *, cancel_event=None,
                    status: str = "正在加载…") -> int:
        resources = getattr(self, "_project_input_resources", None)
        if self.busy or (resources is not None and resources.resourceBusy):
            self._error = "当前有一个后台操作正在运行。"
            self.changed.emit()
            return 0
        self._active_token += 1
        token = self._active_token
        if self._project_compute is not None:
            try:
                self._project_operation = self._project_compute.capture(f"raytrace-{token}")
            except ValueError as exc:
                self._error = str(exc)
                self.changed.emit()
                return 0
        self._active_kind = kind
        self._active_cancel = cancel_event
        self._busy = True
        self._phase = "running" if kind == "trace" else "loading"
        self._status = status
        self._error = ""
        self._progress = 0.0
        self.changed.emit()
        relay = self._relay

        future = self._executor.submit(function)

        def completed(item):
            try:
                value, error = item.result(), ""
            except InterruptedError as exc:
                value, error = None, f"__cancelled__:{exc}"
            except Exception as exc:  # worker error is returned to the UI as data
                value, error = None, str(exc)
            try:
                relay.completed.emit(token, kind, value, error)
            except RuntimeError:
                pass  # page/controller already closed

        future.add_done_callback(completed)
        return token

    @Property(str, notify=changed)
    def phase(self) -> str:
        if (self._workspace_mode == "appearance" or self.appearanceRunning) and self._appearance and not self._busy:
            return "failed" if self._error else self._appearance.phase
        return self._phase

    @Property(str, notify=changed)
    def status(self) -> str:
        if (self._workspace_mode == "appearance" or self.appearanceRunning) and self._appearance and not self._busy and not self._error:
            return self._appearance.status
        return self._status

    @Property(str, notify=changed)
    def error(self) -> str:
        if self._workspace_mode == "appearance":
            return self._error or self._asset_error or (
                self._appearance.errorText if self._appearance else "")
        if self._result is not None:
            return self._error or self._result_error
        return self._error or self._request_error or self._asset_error or self._result_error

    @Property(bool, notify=changed)
    def busy(self) -> bool:
        return self._busy or self.appearanceRunning

    @Property(str, notify=changed)
    def workspaceMode(self) -> str:
        return self._workspace_mode

    @Property(QObject, notify=changed)
    def appearanceController(self) -> QObject | None:
        return self._appearance

    @Property(bool, notify=changed)
    def appearanceRunning(self) -> bool:
        return bool(self._appearance and self._appearance.running)

    @Property(bool, notify=changed)
    def localWorkRunning(self) -> bool:
        return bool(self._appearance and self._appearance.localWorkRunning)

    @Slot()
    def cancelLocalWork(self) -> None:
        if self._appearance is not None:
            self._appearance.cancelLocalWork()

    @Property(bool, notify=changed)
    def geometricBusy(self) -> bool:
        return self._busy

    @Slot(str)
    def setWorkspaceMode(self, mode: str) -> None:
        if (str(mode) == "appearance" and self.project_context is not None
                and self.project_module_key == "ray_trace"):
            self._sync_appearance_context()
            opener = getattr(self.project_navigation, "openProjectModule", None)
            if callable(opener):
                opener("film_appearance")
            else:
                self._error = "项目外观窗口导航尚未绑定。"
                self.changed.emit()
            return
        if mode not in ("appearance", "geometric") or mode == self._workspace_mode:
            return
        # A mode switch is navigation only; it never mutates or cancels either job.
        self._workspace_mode = mode
        self.changed.emit()

    @Slot(result=bool)
    def refreshProjectAppearanceContext(self) -> bool:
        if (self._project_compute is None or self.project_module_key != "film_appearance"
                or getattr(self, "_managed_input_selected", False)):
            return False
        context = self._project_compute.appearance_context()
        if context is None:
            return False
        if self.appearanceRunning:
            self._error = "外观渲染进行时不能更换来源资产。"
            self.changed.emit()
            return False
        self._asset = {"path": context["asset_path"],
                       "sha256": context["asset_sha256"],
                       "appearance_context": context, "fields": []}
        self._asset_error = ""
        self._sync_appearance_context()
        self.changed.emit()
        return self._appearance is not None

    def _sync_appearance_context(self) -> None:
        """Reuse the real renderer controller with the newly frozen asset context."""
        context = (self._asset or {}).get("appearance_context")
        if isinstance(context, Mapping):
            try:
                AppearanceWindowController._validate_context(dict(context))
                _profile_geometry(dict(context))
            except (TypeError, ValueError, OverflowError) as exc:
                # A valid geometric asset need not support this local appearance
                # renderer. Show that capability boundary without a broken page.
                self._asset["appearance_error"] = str(exc)
                self._asset["appearance_context"] = None
                context = None
        if not isinstance(context, Mapping):
            old, self._appearance = self._appearance, None
            if old is not None:
                old.shutdown()
                old.deleteLater()
            return
        if self._project_compute is not None:
            self._project_compute.set_appearance_context(dict(context))
            if self.project_module_key == "ray_trace":
                return
        if self._appearance is None:
            self._appearance = AppearanceWindowController(
                copy.deepcopy(dict(context)), root=self.root, parent=self,
                project_context=self.project_context)
            self._appearance.changed.connect(self.changed.emit)
            self._appearance.closeReady.connect(self._appearance_close_ready)
        else:
            self._appearance.updateLaunchContext(copy.deepcopy(dict(context)))

    @Slot()
    def _appearance_close_ready(self) -> None:
        if self._close_after_appearance:
            self._close_after_appearance = False
            self._approve_close()

    @Property(float, notify=changed)
    def progress(self) -> float:
        return self._progress

    @Property(str, notify=changed)
    def requestText(self) -> str:
        return self._request_text

    @Property(str, notify=changed)
    def requestPath(self) -> str:
        return self._request_path

    @Property(str, notify=changed)
    def documentPath(self) -> str:
        return self._request_path

    @Property(str, notify=changed)
    def documentName(self) -> str:
        information = getattr(self, "_managed_input_information", None)
        if information is not None:
            return str(information["name"])
        return Path(self._request_path).name if self._request_path else "未命名光追请求"

    @Property(bool, notify=changed)
    def documentDirty(self) -> bool:
        resources = getattr(self, "_project_input_resources", None)
        if resources is not None:
            return bool(resources._is_dirty() or resources._has_information_edit())
        return bool(self._request is not None and self._current_snapshot() != self._saved_snapshot)

    @Property(bool, notify=changed)
    def hasRequest(self) -> bool:
        return self._request is not None

    @Property(bool, notify=changed)
    def requestValid(self) -> bool:
        return self._parse_request_text() is not None

    @Property(str, notify=changed)
    def validationMessage(self) -> str:
        if self._parse_request_text() is None:
            return self._request_error
        missing = self._missing_asset_steps()
        if missing:
            return "请求引用逐层厚度，但尚未载入包含这些步骤的资产：" + ", ".join(sorted(missing))
        return ""

    @Property(bool, notify=changed)
    def canRun(self) -> bool:
        return not self.busy and self._parse_request_text() is not None and not self._missing_asset_steps()

    @Property(bool, notify=changed)
    def canOpenAppearance(self) -> bool:
        return bool(not self._busy and self._asset and
                    isinstance(self._asset.get("appearance_context"), Mapping))

    @Property(str, notify=changed)
    def appearanceAvailabilityMessage(self) -> str:
        if self._busy:
            return "当前窗口正在读取或提交任务，请稍后再打开薄膜外观。"
        if not self._asset:
            if self._asset_error:
                return f"所选资产无法打开薄膜外观：{self._asset_error}"
            return "先加载带沉积仿真逐层厚度场的工艺结果资产。"
        if not self._asset.get("appearance_context"):
            return str(self._asset.get("appearance_error") or
                       "所选资产缺少完整 v7 膜系快照或逐层厚度场。")
        return "基片、v7 膜系快照和沉积仿真逐层厚度场将从所选工艺资产继承。"

    @Property(bool, notify=changed)
    def hasResult(self) -> bool:
        return self._result is not None

    @Property("QVariantMap", notify=changed)
    def result(self) -> dict[str, Any]:
        return self._result or {}

    @Property(str, notify=changed)
    def resultPath(self) -> str:
        return self._result_path

    @Property(str, notify=changed)
    def queueJobId(self) -> str:
        return self._queue_job_id

    @Property(str, notify=changed)
    def queueJobState(self) -> str:
        return self._queue_job_state

    @Property(QUrl, constant=True)
    def defaultDirectory(self) -> QUrl:
        if self._project_compute is not None:
            folder = self._project_compute.owned_path("data/raytrace", allow_missing=True)
            folder.mkdir(parents=True, exist_ok=True)
            return QUrl.fromLocalFile(str(folder))
        candidate = self.root / "data" / "film_design"
        if not candidate.is_dir():
            candidate = self.root / "data"
        if not candidate.is_dir():
            candidate = self.root
        return QUrl.fromLocalFile(str(candidate))

    @Property(QUrl, constant=True)
    def filmDesignDirectory(self) -> QUrl:
        if self._project_compute is not None:
            return QUrl.fromLocalFile(str(self.root / "designs"))
        candidate = self.root / "data" / "film_design"
        if not candidate.is_dir():
            candidate = self.root
        return QUrl.fromLocalFile(str(candidate))

    @Property(bool, notify=changed)
    def hasLayerAsset(self) -> bool:
        return self._asset is not None

    @Slot()
    def openAppearance(self) -> None:
        if not self.canOpenAppearance:
            self._error = self.appearanceAvailabilityMessage
            self.changed.emit()
            return
        if self._appearance is None:
            self._sync_appearance_context()
        self.setWorkspaceMode("appearance")
        self.changed.emit()

    @Property(str, notify=changed)
    def layerAssetPath(self) -> str:
        return str((self._asset or {}).get("path", ""))

    @Property(str, notify=changed)
    def layerAssetSummary(self) -> str:
        if self._asset_error:
            return self._asset_error
        if not self._asset:
            if self._referenced_step_ids():
                return "需要载入含逐层厚度网格的 .coatasset。总厚度场不能代替逐层数据。"
            return "未选择沉积资产；均匀膜层仍可计算。"
        rows = self._asset.get("fields", [])
        return f"已载入 {len(rows)} 个计入总厚度的逐步网格：{Path(self.layerAssetPath).name}"

    @Property("QVariantList", notify=changed)
    def surfaceIds(self) -> list[str]:
        request = self._parse_request_text()
        surfaces = request.get("surfaces", []) if request else []
        return [str(row.get("surface_id", "")) for row in surfaces
                if isinstance(row, dict) and row.get("surface_id")]

    @Property(str, notify=changed)
    def selectedSurfaceId(self) -> str:
        return self._selected_surface

    @Property("QVariantList", notify=changed)
    def layerIds(self) -> list[str]:
        request = self._parse_request_text()
        if request is None:
            return []
        surface = next((row for row in request.get("surfaces", [])
                        if isinstance(row, dict) and row.get("surface_id") == self._selected_surface), None)
        coating = surface.get("coating") if surface else None
        layers = coating.get("layers", []) if isinstance(coating, dict) else []
        return [str(row.get("layer_id", "")) for row in layers
                if isinstance(row, dict) and row.get("layer_id")]

    @Property(str, notify=changed)
    def selectedLayerId(self) -> str:
        return self._selected_layer

    @Property("QVariantList", notify=changed)
    def depositionStepChoices(self) -> list[dict[str, str]]:
        rows = []
        for field in (self._asset or {}).get("fields", []):
            step_id = str(field.get("step_id", ""))
            if not step_id:
                continue
            index = field.get("step_index", "?")
            slot = field.get("source_slot", "?")
            rows.append({"step_id": step_id,
                         "label": f"步骤 {index} · 源位 {slot} · {step_id}"})
        return rows

    @Property(str, notify=changed)
    def selectedStepId(self) -> str:
        return self._selected_step

    @Property("QVariantList", notify=changed)
    def wavelengthLabels(self) -> list[str]:
        rows = (self._result or {}).get("spectra", [])
        return [f"{row.get('wavelength_nm', 0):g} nm" for row in rows]

    @Property(int, notify=changed)
    def selectedSpectrumIndex(self) -> int:
        return self._spectrum_index

    @Property("QVariantList", notify=changed)
    def spectra(self) -> list[dict[str, Any]]:
        return list((self._result or {}).get("spectra", []))

    @Property("QVariantList", notify=changed)
    def displayPaths(self) -> list[dict[str, Any]]:
        if self._result is None:
            return []
        paths = self._result.get("paths", [])
        if not paths:
            return []
        index = min(max(0, self._spectrum_index), len(self._result.get("spectra", [])) - 1)
        wave = self._result.get("spectra", [])[index].get("wavelength_nm")
        request = self._request or {}
        origins = {row.get("sample_id"): row.get("origin_xz_mm")
                   for row in request.get("samples", []) if isinstance(row, dict)}
        detector = self._result.get("detector") or {}
        detector_z = detector.get("z_mm")
        display = []
        for path in paths:
            if path.get("wavelength_nm") != wave or len(display) >= MAX_DISPLAY_RAYS:
                continue
            origin = origins.get(path.get("sample_id"))
            points = []
            if isinstance(origin, (list, tuple)) and len(origin) == 2:
                points.append([float(origin[0]), float(origin[1])])
            events = path.get("events", [])
            if not points and not events:
                final_ray = path.get("final_ray")
                final_origin = final_ray.get("origin_xz_mm") if isinstance(final_ray, dict) else None
                if isinstance(final_origin, (list, tuple)) and len(final_origin) == 2:
                    points.append([float(final_origin[0]), float(final_origin[1])])
            for event in events:
                hit = event.get("hit_xz_mm")
                if isinstance(hit, (list, tuple)) and len(hit) == 2:
                    point = [float(hit[0]), float(hit[1])]
                    if not points or points[-1] != point:
                        points.append(point)
            final_ray = path.get("final_ray")
            hit_x = path.get("detector_hit_x_mm")
            if final_ray and detector_z is not None and hit_x is not None:
                points.append([float(hit_x), float(detector_z)])
            if len(points) >= 2:
                display.append({"sample_id": path.get("sample_id", ""),
                                "status": path.get("status", ""), "points": points})
        return display

    @Property("QVariantList", notify=changed)
    def detectorHistogram(self) -> list[float]:
        if self._result is None:
            return []
        spectra = ((self._result.get("detector") or {}).get("spectra") or [])
        if not spectra:
            return []
        index = min(max(0, self._spectrum_index), len(spectra) - 1)
        return list(spectra[index].get("ray_energy_histogram_weight", []))

    @Property("QVariantList", notify=changed)
    def detectorBinEdges(self) -> list[float]:
        return list(((self._result or {}).get("detector") or {}).get("x_bin_edges_mm", []))

    @Property(bool, notify=changed)
    def hasDetector(self) -> bool:
        return bool(self._result and self._result.get("detector"))

    @Property(str, notify=changed)
    def detectorInterpretation(self) -> str:
        value = str(((self._result or {}).get("detector") or {}).get(
            "interpretation", "几何光线能量分箱；不是衍射 PSF"))
        if value == "geometric ray-energy histogram; not a diffraction PSF":
            return "几何光线能量分箱；不是衍射点扩散函数（PSF）"
        return value

    @Slot(str)
    def setRequestText(self, text: str) -> None:
        text = str(text)
        if text == self._request_text or self._busy:
            return
        self._request_text = text
        self._selected_surface = ""
        self._selected_layer = ""
        self._mark_request_changed()

    @Slot(str)
    def setSelectedSurfaceId(self, value: str) -> None:
        value = str(value or "")
        if value == self._selected_surface:
            return
        self._selected_surface = value
        self._selected_layer = ""
        self.changed.emit()

    @Slot(str)
    def setSelectedLayerId(self, value: str) -> None:
        value = str(value or "")
        if value == self._selected_layer:
            return
        self._selected_layer = value
        self.changed.emit()

    @Slot(str)
    def setSelectedStepId(self, value: str) -> None:
        value = str(value or "")
        if value == self._selected_step:
            return
        self._selected_step = value
        self.changed.emit()

    @Slot(int)
    def setSelectedSpectrumIndex(self, value: int) -> None:
        self._spectrum_index = max(0, int(value))
        self.changed.emit()

    @Slot()
    def newRequest(self) -> None:
        if self.busy:
            return
        self._workspace_mode = "geometric"
        self._queue_job_id = ""
        self._queue_job_state = ""
        starter = {"schema": SCHEMA, "schema_version": SCHEMA_VERSION,
                   "units": {"length": "mm", "wavelength": "nm", "index": "n+i*k"},
                   "polarization": "unpolarized", "samples": [],
                   "spectrum": [{"wavelength_nm": 550.0, "weight": 1.0}],
                   "surfaces": [],
                   "detector": {"z_mm": 100.0, "aperture_radius_mm": 10.0,
                                "pixel_count": 101}}
        self._request_path = ""
        # The selected process asset belongs to the optical workspace, not to a
        # temporary geometric starter. Keep it available in both modes.
        self._result = None
        self._result_path = ""
        self._request_text = json.dumps(starter, ensure_ascii=False, indent=2)
        self._saved_snapshot = ""
        self._request = starter
        self._phase = "ready"
        self._status = "新请求已创建。生成点/扩展光源样本，并在请求 JSON 中设置有序光学面。"
        self._error = ""
        self._emit_document()

    @Slot(str, str, str, str, str, result=str)
    def generateSourceSamples(self, mode: str, count: str, x_mm: str,
                              z_mm: str, extent: str) -> str:
        request = self._parse_request_text()
        if request is None:
            self._error = self._request_error or "请先新建或载入几何光追请求。"
            self.changed.emit()
            return self._error
        try:
            count_number = int(str(count).strip())
            samples = generate_source_samples(
                mode, count_number, _finite_float(x_mm, "光源 x"),
                _finite_float(z_mm, "光源 z"), _finite_float(extent, "角度/宽度"))
            request["samples"] = samples
            self._request_text = json.dumps(request, ensure_ascii=False, indent=2, allow_nan=False)
            self._selected_surface = self._selected_surface if self._selected_surface in self.surfaceIds else ""
            self._mark_request_changed()
            self._status = f"已生成 {len(samples)} 条确定性光源样本；各条相对权重为 1，未归一化。"
            self.changed.emit()
            return ""
        except (TypeError, ValueError, OverflowError) as exc:
            self._error = str(exc)
            self._status = "光源样本未更改。"
            self.changed.emit()
            return self._error

    @Slot(str)
    def loadRequest(self, path_value: Any) -> None:
        try:
            path = _as_local_path(path_value, resolve=self._project_compute is None)
        except ValueError as exc:
            self._error = str(exc)
            self.changed.emit()
            return

        cancellation = threading.Event()

        def work():
            if cancellation.is_set():
                raise InterruptedError("cancelled")
            imported_path = self._project_input(path, "raytrace")
            payload = _read_json_object(imported_path)
            asset_ref = None
            if payload.get("schema") == UI_DOCUMENT_SCHEMA:
                if payload.get("schema_version") != UI_DOCUMENT_VERSION or set(payload) != {
                        "schema", "schema_version", "request", "deposition_asset"}:
                    raise ValueError("光追窗口文档格式无效或版本不受支持")
                request = payload["request"]
                asset_ref = payload.get("deposition_asset")
            else:
                request = payload
            if not isinstance(request, dict) or request.get("schema") != SCHEMA or \
                    request.get("schema_version") != SCHEMA_VERSION:
                raise ValueError(f"文件须包含 {SCHEMA} v{SCHEMA_VERSION} 请求")
            _canonical(request)  # reject NaN/Infinity and non-JSON values
            asset = None
            asset_error = ""
            if asset_ref:
                if not isinstance(asset_ref, dict) or set(asset_ref) != {"path", "sha256"}:
                    raise ValueError("沉积资产引用必须只有 path 和 sha256")
                raw_path = Path(str(asset_ref["path"]))
                asset_path = raw_path if raw_path.is_absolute() else path.parent / raw_path
                try:
                    asset_path = self._project_input(asset_path, "deposition")
                    asset = _layer_asset_summary(asset_path, self.root,
                                                 cancel_event=cancellation,
                                                 expected_sha256=str(asset_ref["sha256"]))
                    if asset["sha256"] != asset_ref["sha256"]:
                        raise ValueError("保存的沉积资产摘要与当前文件不一致")
                except InterruptedError:
                    raise
                except Exception as exc:
                    asset_error = str(exc)
            return {"request": request, "asset": asset, "asset_error": asset_error,
                    "path": str(imported_path)}

        self._start_task("request", work, cancel_event=cancellation, status="正在读取光追请求…")

    @Slot(str)
    def openRequestDialogResult(self, path_value: Any) -> None:
        self.loadRequest(path_value)

    @Slot(str)
    def loadResult(self, path_value: Any) -> None:
        try:
            path = _as_local_path(path_value, resolve=self._project_compute is None)
        except ValueError as exc:
            self._error = str(exc)
            self.changed.emit()
            return

        def work():
            imported_path = self._project_input(path, "raytrace-results")
            value = _read_json_object(imported_path, max_bytes=MAX_RESULT_BYTES)
            validate_geometric_raytrace_result(value)
            return {"result": value, "path": str(imported_path)}

        self._start_task("result", work, status="正在读取已保存结果…")

    @Slot(str, str, str)
    def attachFilmDesign(self, design_path_value: Any, surface_id: str, side: str) -> None:
        request = self._parse_request_text()
        if request is None:
            self._error = self._request_error or "请先新建或载入几何光追请求。"
            self.changed.emit()
            return
        try:
            path = _as_local_path(design_path_value, resolve=self._project_compute is None)
        except ValueError as exc:
            self._error = str(exc)
            self.changed.emit()
            return
        surface = next((row for row in request.get("surfaces", [])
                        if isinstance(row, dict) and row.get("surface_id") == surface_id), None)
        if surface is None:
            self._error = f"请求中找不到光学面 {surface_id!r}"
            self.changed.emit()
            return
        if surface.get("coating"):
            self._pending_film_import = (str(path), str(surface_id), str(side))
            self.filmImportConfirmationRequested.emit(
                f"光学面 {surface_id} 已有膜层。导入膜系会替换该面的 coating，是否继续？")
            return
        self._start_film_import(path, str(surface_id), str(side), request)

    @Slot(bool)
    def confirmFilmDesignImport(self, accepted: bool) -> None:
        pending, self._pending_film_import = self._pending_film_import, None
        if not accepted or pending is None:
            self.changed.emit()
            return
        path = Path(pending[0])
        request = self._parse_request_text()
        if request is None:
            self._error = self._request_error
            self.changed.emit()
            return
        self._start_film_import(path, pending[1], pending[2], request)

    def _start_film_import(self, path: Path, surface_id: str, side: str,
                           request: Mapping[str, Any]) -> None:
        snapshot = copy.deepcopy(dict(request))
        def work():
            managed = self._project_film_handle(path)
            return _build_film_design_coating(
                self.root, managed, snapshot, surface_id, side,
                service=(self.project_context.service if self.project_context is not None else None))
        self._start_task("film", work, status="正在采样现有膜系材料与厚度…")

    @Slot(str)
    def loadLayerAsset(self, path_value: Any) -> None:
        try:
            path = _as_local_path(path_value, resolve=self._project_compute is None)
        except ValueError as exc:
            self._error = str(exc)
            self.changed.emit()
            return
        resources = getattr(self, "_project_input_resources", None)
        if resources is not None:
            resources.captureLayerAsset(str(path))
            return
        cancellation = threading.Event()
        self._start_task("asset", lambda: _layer_asset_summary(
            self._project_input(path, "deposition"), self.root, cancel_event=cancellation), cancel_event=cancellation,
                         status="正在校验沉积逐层资产…")

    @Slot(str, str, str, str)
    def bindLayerToDepositionStep(self, layer_id: str, step_id: str,
                                  rotation_degrees: str, translation_xy: str) -> str:
        request = self._parse_request_text()
        if request is None:
            return self._request_error or "请先新建或载入几何光追请求。"
        if not self._asset:
            return "请先载入包含逐层厚度网格的 .coatasset。"
        if step_id not in {row.get("step_id") for row in self._asset.get("fields", [])}:
            return "所选步骤不在当前资产的计入总厚度网格中。"
        surface = next((row for row in request.get("surfaces", [])
                        if isinstance(row, dict) and row.get("surface_id") == self._selected_surface), None)
        if surface is None or not isinstance(surface.get("coating"), dict):
            return "请选中一个已有 coating 的光学面。"
        layer = next((row for row in surface["coating"].get("layers", [])
                      if isinstance(row, dict) and row.get("layer_id") == layer_id), None)
        if layer is None:
            return "所选膜层不在当前光学面的 coating 中。"
        try:
            rotation = math.radians(_finite_float(rotation_degrees, "坐标转角"))
            if abs(rotation) > 1e6:
                raise ValueError("坐标转角过大")
            parts = [item.strip() for item in str(translation_xy).split(",")]
            if len(parts) != 2:
                raise ValueError("平移需按 x,y 输入，单位 mm")
            translation = [_finite_float(parts[0], "网格平移 x"),
                           _finite_float(parts[1], "网格平移 y")]
            layer.pop("thickness_nm", None)
            layer["deposition_step_id"] = step_id
            layer["global_to_grid_xy"] = {
                "rotation_rad": rotation,
                "translation_xy_mm": translation,
            }
            self._request_text = json.dumps(request, ensure_ascii=False, indent=2, allow_nan=False)
            self._mark_request_changed()
            self._status = f"已把膜层 {layer_id} 绑定到沉积步骤 {step_id}。"
            self.changed.emit()
            return ""
        except (TypeError, ValueError, OverflowError) as exc:
            return str(exc)

    @Slot()
    def startCalculation(self) -> None:
        if self.busy:
            return
        request = self._parse_request_text()
        if request is None:
            self._error = self._request_error or "请先新建或载入几何光追请求。"
            self.changed.emit()
            return
        required = _referenced_steps(request)
        available = {row.get("step_id") for row in (self._asset or {}).get("fields", [])}
        missing = required - available
        if missing:
            self._error = "逐层资产缺少步骤：" + ", ".join(sorted(missing))
            self.changed.emit()
            return
        if required and not self._asset:
            self._error = "逐层膜层需要先选择捕获了逐步厚度的沉积资产。"
            self.changed.emit()
            return
        if self._project_compute is not None:
            try:
                from uuid import uuid4
                self._queue_operation = self._project_compute.capture(uuid4().hex)
            except ValueError as exc:
                self._error = str(exc)
                self.changed.emit()
                return
        request_snapshot = copy.deepcopy(request)
        # The appearance source remains selected across modes, but a uniform
        # geometric request must not acquire an unused artifact requirement.
        asset_snapshot = copy.deepcopy(self._asset) if required else None
        cancellation = threading.Event()
        def work():
            if cancellation.is_set():
                raise InterruptedError("cancelled")
            limits = request_limits(request_snapshot)
            if asset_snapshot:
                artifact = _find_registered_deposition_bundle(
                    self._queue_client, asset_snapshot["sha256"])
                if artifact is None:
                    raise ValueError(
                        "所选沉积资产尚未在共享队列中注册为资产包。"
                        "请使用队列生成的资产包，以引用稳定的资产 ID。")
            else:
                artifact = None
            spec = freeze_raytrace_spec(request_snapshot, artifact)
            if cancellation.is_set():
                raise InterruptedError("cancelled")
            if self._project_compute is not None:
                operation = self._queue_operation
                if operation is None:
                    raise ValueError("Project ray-trace operation is absent")
                metadata = self._project_compute.metadata(operation)
                receipt = self._queue_client.submit(
                    spec, job_id=operation.operation_id, metadata=metadata)
            else:
                receipt = self._queue_client.submit(spec)
            job_id = receipt.get("job_id") if isinstance(receipt, Mapping) else None
            if not isinstance(job_id, str) or not job_id:
                raise ValueError("scheduler_submit_did_not_return_a_job_id")
            if self._project_compute is not None:
                self._project_compute.register_job(
                    job_id, module_key="ray_trace", service_owned=True,
                    metadata=metadata)
            if cancellation.is_set():
                self._queue_client.request_cancel(job_id)
            return {"job_id": job_id, "limits": limits,
                    "cancel_requested": cancellation.is_set()}

        self._queue_job_id = ""
        self._queue_job_state = "submitting"
        self._start_task("queue_submit", work, cancel_event=cancellation,
                         status="正在向共享队列提交几何光追任务。")

    @Slot()
    def _poll_raytrace_job(self) -> None:
        if (self._closed or not self._busy or not self._queue_job_id
                or self._queue_poll_inflight):
            return
        self._queue_poll_inflight = True
        token = self._active_token
        future = self._executor.submit(self._queue_client.status, self._queue_job_id)

        def completed(item):
            try:
                value, error = item.result(), ""
            except Exception as exc:
                value, error = None, str(exc)
            try:
                self.queueStatusReceived.emit(token, value, error)
            except RuntimeError:
                pass

        future.add_done_callback(completed)

    @Slot(int, object, str)
    def _receive_queue_status(self, token: int, job: Any, error: str) -> None:
        if (self._closed or token != self._active_token
                or not self._project_current(self._queue_operation)):
            return
        self._queue_poll_inflight = False
        if error:
            self._queue_timer.stop()
            self._busy = False
            self._phase = "failed"
            self._error = f"共享队列状态读取失败：{error}"
            self._status = "队列跟踪已停止，已提交任务可能仍在运行。"
            self._active_cancel = None
            self._emit_document()
            if self._close_after_queue:
                self._close_after_queue = False
                self.closeReady.emit()
            return
        if not isinstance(job, Mapping):
            self._queue_timer.stop()
            self._busy = False
            self._phase = "failed"
            self._error = "scheduler_status_returned_invalid_job"
            self._active_cancel = None
            self._emit_document()
            if self._close_after_queue:
                self._close_after_queue = False
                self.closeReady.emit()
            return
        state = str(job.get("state", "unknown"))
        if self._project_compute is not None:
            self._project_compute.update_job(self._queue_job_id, state)
        self._queue_job_state = state
        progress = job.get("progress") if isinstance(job.get("progress"), Mapping) else {}
        try:
            self._progress = min(1.0, max(0.0, float(progress.get("fraction", 0.0))))
        except (TypeError, ValueError):
            self._progress = 0.0
        label = {"queued": "排队中", "running": "运行中", "succeeded": "已完成", "cancelled": "已取消", "failed": "失败"}.get(state, state)
        self._status = f"共享队列任务{label}：{progress.get('stage', '')}"
        if state in ("queued", "running"):
            self._phase = "cancelling" if job.get("cancel_requested") else "running"
            self.changed.emit()
            return

        self._queue_timer.stop()
        self._busy = False
        self._active_cancel = None
        if state == "succeeded":
            result = job.get("result") if isinstance(job.get("result"), Mapping) else {}
            result = dict(result)
            valid_summary = is_geometric_raytrace_result(result)
            artifacts = result.get("result_artifacts", [])
            record = next((item for item in artifacts
                           if isinstance(item, Mapping) and item.get("role") == "result"
                           and isinstance(item.get("artifact_id"), str)), None) if valid_summary else None
            if record is None:
                self._phase = "failed"
                self._error = ("successful_raytrace_job_has_invalid_summary" if not valid_summary
                               else "successful_raytrace_job_has_no_result_artifact")
                self._status = "队列任务已完成，但结果摘要或资产无效。"
            elif type(record.get("bytes")) is not int or not 0 <= record["bytes"] <= MAX_RESULT_BYTES:
                self._phase = "failed"
                self._error = "raytrace_result_artifact_exceeds_byte_limit"
                self._status = "队列结果资产缺失，或超过本地结果大小上限。"
            else:
                artifact_record = dict(record)
                summary = copy.deepcopy(result)
                job_id = self._queue_job_id

                def load_result_artifact():
                    path = self._queue_client.materialize_artifact(
                        artifact_record["artifact_id"], artifact_record)
                    if Path(path).stat().st_size != artifact_record["bytes"]:
                        raise ValueError("downloaded_artifact_size_mismatch")
                    value = _read_json_object(Path(path), max_bytes=MAX_RESULT_BYTES)
                    validate_geometric_raytrace_result(value)
                    if not geometric_raytrace_summary_matches_result(summary, value):
                        raise ValueError("downloaded_artifact_does_not_match_queue_summary")
                    return {"result": value, "path": str(path), "job_id": job_id}

                self._phase = "loading"
                self._start_task("queue_result", load_result_artifact,
                                 status="正在下载队列结果资产。")
                return
            self.changed.emit()
            if self._close_after_queue:
                self._close_after_queue = False
                self.closeReady.emit()
        elif state == "cancelled":
            self._phase = "ready" if self._request is not None else "empty"
            self._status = "队列任务已在计算边界取消。"
            self._error = ""
        else:
            self._phase = "failed"
            self._error = str(job.get("error") or job.get("error_code") or
                              f"raytrace_job_{state}")
            self._status = "共享队列任务失败，已保留请求供修改。"
        self.changed.emit()
        if self._close_after_queue:
            self._close_after_queue = False
            self.closeReady.emit()

    @Slot()
    def cancelCalculation(self) -> None:
        if not self._busy or self._active_cancel is None:
            return
        self._active_cancel.set()
        self._phase = "cancelling"
        self._status = "正在通过共享计算队列请求取消。"
        if self._queue_job_id:
            token = self._active_token
            future = self._executor.submit(self._queue_client.request_cancel,
                                           self._queue_job_id)

            def completed(item):
                try:
                    value, error = item.result(), ""
                except Exception as exc:
                    value, error = None, str(exc)
                try:
                    self._relay.completed.emit(token, "queue_cancel", value, error)
                except RuntimeError:
                    pass

            future.add_done_callback(completed)
        self.changed.emit()

    @Slot(str, bool, result=bool)
    def saveRequest(self, path_value: Any, overwrite: bool = False) -> bool:
        try:
            request = self._parse_request_text()
            if request is None:
                raise ValueError(self._request_error)
            path = self._project_destination(_as_local_path(path_value, resolve=self._project_compute is None))
        except (ValueError, OSError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return False
        if path.exists() and not overwrite:
            self._pending_overwrite = ("request", str(path))
            self.requestOverwriteRequested.emit("request", str(path))
            return False
        try:
            payload = _request_document_payload(request, self._asset, path)
            _write_json_file(path, payload, overwrite=overwrite)
        except Exception as exc:
            if isinstance(exc, FileExistsError) and not overwrite:
                self._pending_overwrite = ("request", str(path))
                self.requestOverwriteRequested.emit("request", str(path))
                return False
            self._error = f"请求保存失败：{exc}"
            self._status = "请求仍未保存。"
            self.changed.emit()
            return False
        self._request_path = str(path)
        self._saved_snapshot = self._current_snapshot()
        self._status = f"请求已保存：{path.name}"
        self._error = ""
        self._emit_document()
        if self._close_after_save:
            self._close_after_save = False
            self._approve_close()
        return True

    @Slot()
    def requestSaveAs(self) -> None:
        self.requestSaveAsRequested.emit()

    @Slot()
    def cancelSaveAs(self) -> None:
        self._close_after_save = False
        self.changed.emit()

    @Slot(str, bool, result=bool)
    def saveResult(self, path_value: Any, overwrite: bool = False) -> bool:
        if self._result is None:
            self._error = "没有可保存的光追结果。"
            self.changed.emit()
            return False
        try:
            path = self._project_destination(_as_local_path(path_value, resolve=self._project_compute is None))
        except ValueError as exc:
            self._error = str(exc)
            self.changed.emit()
            return False
        if path.exists() and not overwrite:
            self._pending_overwrite = ("result", str(path))
            self.requestOverwriteRequested.emit("result", str(path))
            return False
        try:
            _write_json_file(path, self._result, overwrite=overwrite)
        except Exception as exc:
            if isinstance(exc, FileExistsError) and not overwrite:
                self._pending_overwrite = ("result", str(path))
                self.requestOverwriteRequested.emit("result", str(path))
                return False
            self._error = f"结果保存失败：{exc}"
            self.changed.emit()
            return False
        self._result_path = str(path)
        self._status = f"结果已保存：{path.name}"
        self._error = ""
        self.changed.emit()
        return True

    @Slot(str, bool, result=bool)
    def exportAppearancePreview(self, path_value: Any, overwrite: bool = False) -> bool:
        if self.busy or self.appearanceRunning or self._appearance is None or not self._appearance._last_result:
            return False
        try:
            source = Path(self._appearance._last_result["image_path"])
            if self._project_compute is not None:
                source = self._project_compute.owned_path(source)
            target = self._project_destination(_as_local_path(path_value, resolve=self._project_compute is None))
            if source == target:
                raise ValueError("请选择与当前结果原件不同的导出位置。")
            if target.exists() and not overwrite:
                self._pending_overwrite = ("appearance", str(target))
                self.requestOverwriteRequested.emit("appearance", str(target))
                return False
            return bool(self._start_task("appearance_export",
                lambda: _copy_result_file(source, target, overwrite=overwrite), status="正在导出外观结果 PNG…"))
        except (ValueError, OSError, KeyError) as exc:
            self._error = "外观结果导出失败：" + str(exc)
            self.changed.emit()
            return False

    @Slot(bool)
    def resolveOverwrite(self, accepted: bool) -> None:
        pending, self._pending_overwrite = self._pending_overwrite, None
        if not accepted or pending is None:
            self.changed.emit()
            return
        kind, path = pending
        if kind == "request":
            self.saveRequest(path, True)
        elif kind == "appearance":
            self.exportAppearancePreview(path, True)
        else:
            self.saveResult(path, True)

    @Slot()
    def saveAndClose(self) -> None:
        resources = getattr(self, "_project_input_resources", None)
        if resources is not None:
            if resources.saveCurrent():
                self._managed_close_save_id = resources._resource_operation_id
            return
        if self._request_path:
            if self.saveRequest(self._request_path, True):
                self._approve_close()
        else:
            self._close_after_save = True
            self.requestSaveAsRequested.emit()

    @Slot(str, str, bool, str)
    def onProjectInputOperationFinished(self, request_id, kind, success, error):
        if request_id != self._managed_close_save_id or kind not in {"save", "copy"}:
            return
        self._managed_close_save_id = ""
        resources = getattr(self, "_project_input_resources", None)
        if success and resources is not None and not resources._is_dirty() and not resources._has_information_edit():
            self._approve_close()
        else:
            self._error = error or "保存期间输入已变化，请检查后再关闭。"
            self.changed.emit()

    @Slot()
    def requestClose(self) -> None:
        if self.documentDirty or self.busy:
            self.closeConfirmationRequested.emit()
            return
        self._approve_close()

    @Slot(bool)
    def resolveClose(self, discard: bool) -> None:
        if not discard:
            self._close_after_save = False
            self.changed.emit()
            return
        self._approve_close()

    @Slot()
    def _approve_close(self) -> None:
        self._close_approved = True
        if self.appearanceRunning:
            self._close_after_appearance = True
            self._appearance.resolveClose(True)
            return
        if (self.project_context is not None
                and self._active_kind in ("queue_submit", "queue", "queue_result")):
            self.closeReady.emit()
            return
        if self._active_kind in ("queue_submit", "queue", "queue_result"):
            self._close_after_queue = True
            self.cancelCalculation()
            return
        if self._active_cancel is not None:
            self._active_cancel.set()
        self.closeReady.emit()

    @Slot()
    def detach_observers(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pending_project_task = None
        if self._project_compute is not None:
            self._project_compute.unregister_thaw_callback(self._on_project_unfrozen)
        self._queue_timer.stop()
        if self._appearance is not None:
            self._appearance.detach_observers()
        self._active_token += 1
        if (self._active_cancel is not None and (self.project_context is None
                or self._active_kind not in ("queue_submit", "queue", "queue_result"))):
            self._active_cancel.set()
        self._executor.shutdown(wait=False, cancel_futures=True)

    @Slot(int, object)
    def _receive_progress(self, token: int, value: Any) -> None:
        if (self._closed or token != self._active_token or not isinstance(value, Mapping)
                or not self._project_current()):
            return
        self._progress = min(1.0, max(0.0, float(value.get("completed_fraction", 0.0))))
        self._status = ("追迹完成。" if value.get("stage") == "complete" else
                        f"正在追迹… {self._progress * 100:.0f}%")
        self.changed.emit()

    @Slot(int, str, object, str)
    def _receive_task(self, token: int, kind: str, value: Any, error: str) -> None:
        if (not self._closed and token == self._active_token
                and self._project_compute is not None and self._project_compute.editing_frozen):
            self._pending_project_task = (token, kind, value, error)
            return
        if (self._closed or token != self._active_token
                or not self._project_current()):
            return
        if kind == "queue_cancel":
            if error:
                self._error = f"队列取消请求失败：{error}"
                self._status = "队列任务可能仍在运行，将继续检查状态。"
            else:
                self._status = "已请求队列取消，正在等待计算边界。"
            self.changed.emit()
            return

        self._busy = False
        self._active_kind = ""
        self._active_cancel = None
        if error:
            if error.startswith("__cancelled__:"):
                self._phase = "ready" if self._request is not None else "empty"
                self._status = "计算已在安全边界取消。"
                self._error = ""
            else:
                self._phase = "failed"
                self._error = error
                self._status = self._operation_label(kind) + "失败，保留上次有效状态。"
                if kind == "asset" and self._asset is None:
                    self._asset_error = error
            if kind == "queue_submit":
                self._queue_job_state = "failed"
            self._emit_document()
            if self._close_after_queue:
                self._close_after_queue = False
                self.closeReady.emit()
            return
        if kind == "appearance_export":
            self._status = "外观结果已导出：" + Path(value).name
            self._error = ""
            self.changed.emit()
            return
        if kind == "queue_submit":
            self._queue_job_id = value["job_id"]
            self._queue_job_state = "queued"
            self._active_kind = "queue"
            self._active_cancel = threading.Event()
            self._busy = True
            self._phase = "running"
            self._progress = 0.0
            self._status = f"共享队列任务 {self._queue_job_id} 已排队。"
            self._queue_timer.start()
            self._emit_document()
            if value.get("cancel_requested") or self._close_after_queue:
                self.cancelCalculation()
            else:
                self._poll_raytrace_job()
            return
        if kind == "request":
            self._workspace_mode = "geometric"
            self._request = value["request"]
            self._request_text = json.dumps(self._request, ensure_ascii=False, indent=2, allow_nan=False)
            self._request_path = value["path"]
            self._asset = value.get("asset")
            self._asset_error = value.get("asset_error", "")
            self._sync_appearance_context()
            self._saved_snapshot = self._current_snapshot()
            self._selected_surface = ""
            self._selected_layer = ""
            self._result = None
            self._result_path = ""
            self._queue_job_id = ""
            self._queue_job_state = ""
            self._phase = "ready"
            self._status = "请求已载入。" + (f"沉积资产不可用：{self._asset_error}" if self._asset_error else "")
        elif kind == "result":
            self._workspace_mode = "geometric"
            self._result = value["result"]
            self._result_path = value["path"]
            self._phase = "complete"
            self._spectrum_index = 0
            self._status = "已载入保存的几何光追结果。"
        elif kind == "film":
            updated, warnings = value
            self._request = updated
            self._request_text = json.dumps(updated, ensure_ascii=False, indent=2, allow_nan=False)
            self._result = None
            self._result_path = ""
            self._phase = "ready"
            self._status = "已从膜系设计载入镀膜。" + (" " + " ".join(warnings) if warnings else "")
        elif kind == "asset":
            self._asset = value
            self._asset_error = ""
            self._sync_appearance_context()
            self._result = None
            self._result_path = ""
            self._phase = "ready" if self._request is not None else "empty"
            self._status = self.layerAssetSummary
        elif kind == "queue_result":
            self._result = value["result"]
            self._result_path = value["path"]
            self._phase = "complete"
            self._spectrum_index = 0
            self._status = f"共享队列任务 {value['job_id']} 已完成，结果资产已载入。"
        self._error = ""
        self._emit_document()
        if self._close_after_queue:
            self._close_after_queue = False
            self.closeReady.emit()

    @staticmethod
    def _operation_label(kind: str) -> str:
        return {"request": "请求加载", "result": "结果加载", "film": "膜系导入",
                "asset": "沉积资产加载", "trace": "光线追迹", "appearance_export": "外观结果导出"}.get(kind, "后台操作")


__all__ = ["RayTraceWindowController", "generate_source_samples"]
