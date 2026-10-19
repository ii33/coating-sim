"""Transactional Qt model for the local, user-owned material library.

Library drafts are separate from source snapshots. Closing the library retains
its draft; only explicit discard replaces it, and failed writes never clear it.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
from typing import Any
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import re
import tempfile
import threading
import uuid
import weakref

from PySide6.QtGui import QGuiApplication

from PySide6.QtCore import QObject, Property, Signal, Slot, QTimer, QUrl

from coat_sim.core.data_files import data_path
from coat_sim.core import materials, optical_constants
from .material_research import ROLE_LABELS, coverage_label, research_sections, role_label, prepare_reference_sections
from .optical_formula_renderer import render_optical_formula_svg
from .process_parameter_model import project_process_parameter_rows, serialize_process_parameter_rows
from .local_files import local_file_path


def _format(value: Any) -> str:
    if value is None:
        return ""
    return format(value, ".12g") if isinstance(value, (int, float)) else str(value)


def _float(value: str, label: str) -> float:
    import math
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}须为数值") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label}须为有限数值")
    return result


MODEL_NUMBERS = {
    "resistance": ("source_exponent", "reference_rate_nm_s", "rate_exponent_per_decade",
                   "active_fraction", "rate_active_fraction_per_decade", "center_weight",
                   "valid_rate_min_nm_s", "valid_rate_max_nm_s"),
    "electron_beam": ("source_exponent", "reference_rate_nm_s", "rate_exponent_per_decade",
                      "spot_sigma_mm", "rate_spot_sigma_per_decade",
                      "valid_rate_min_nm_s", "valid_rate_max_nm_s"),
}
RATE_KEYS = {"reference_rate_nm_s", "valid_rate_min_nm_s", "valid_rate_max_nm_s"}
_MATERIAL_PROFILE_FIELDS = {"schema_version", "id", "name", "description", "notes", "resistance_model",
                            "electron_beam_model", "reference_data", "research_data", "roles",
                            "optical_constants", "category"}

_OPTICAL_CONDITIONS = (
    "镀膜方法", "基片温度", "基片", "膜厚", "沉积速率", "氧分压", "反应气流量",
    "本底压力", "工作压力", "离子辅助", "退火", "测量温度", "晶向/偏振", "相态",
)


def _optical_condition_rows(process_parameters):
    """Expose only explicitly structured process metadata; never parse prose."""
    if not isinstance(process_parameters, list):
        return []
    rows = []
    for item in process_parameters:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "") or "").strip()
        value = str(item.get("value", "") or "").strip()
        unit = str(item.get("unit", "") or "").strip()
        if name or value or unit:
            rows.append({"name": name, "label": name, "value": value, "unit": unit})
    return rows


def _normalize_process_parameters(rows):
    """Validate explicit editable process rows without parsing legacy notes."""
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValueError("\u5de5\u827a\u53c2\u6570\u5fc5\u987b\u4e3a\u8868\u683c\u884c\u5217\u8868")
    result = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or set(row) - {"name", "value", "unit"}:
            raise ValueError(f"\u5de5\u827a\u53c2\u6570\u7b2c {index} \u884c\u683c\u5f0f\u65e0\u6548")
        clean = {key: str(row.get(key, "") if row.get(key) is not None else "").strip()
                 for key in ("name", "value", "unit")}
        if not any(clean.values()):
            continue
        if not clean["name"]:
            raise ValueError(f"\u5de5\u827a\u53c2\u6570\u7b2c {index} \u884c\u8bf7\u586b\u53c2\u6570\u540d")
        result.append(clean)
    return result


def _optical_curve_points(record, count=65):
    """Sample the existing optical evaluator for display; preserve unknown k."""
    low, high = float(record["wavelength_min"]), float(record["wavelength_max"])
    unit = record["wavelength_unit"]
    points = []
    has_k = (record.get("k") is not None if record["kind"] == "tabulated"
             else record.get("k_data") is not None)
    k_data = record.get("k_data") or {}
    if record["kind"] == "tabulated" and record.get("k") is not None:
        k_data = {"wavelength_unit": unit, "wavelength_min": record["wavelengths"][0],
                  "wavelength_max": record["wavelengths"][-1]}
    k_scale_nm = 1000.0 if k_data.get("wavelength_unit") == "um" else 1.0
    x_scale_nm = 1000.0 if unit == "um" else 1.0
    k_low_nm = float(k_data.get("wavelength_min", 0.0)) * k_scale_nm
    k_high_nm = float(k_data.get("wavelength_max", 0.0)) * k_scale_nm
    for index in range(count):
        wavelength = low + (high - low) * index / (count - 1)
        try:
            n_value, _unknown_k, _details = optical_constants.evaluate_optical_set_with_details(
                record, wavelength, unit, include_k=False)
        except (ValueError, TypeError, OverflowError):
            continue
        if not math.isfinite(n_value):
            continue
        point = {"wavelength": wavelength, "n": n_value, "k": None}
        k_in_domain = (unit != "um" or wavelength * x_scale_nm >= k_low_nm)
        k_in_domain = k_in_domain and (unit != "um" or wavelength * x_scale_nm <= k_high_nm)
        if unit == "nm":
            k_in_domain = wavelength * x_scale_nm >= k_low_nm and wavelength * x_scale_nm <= k_high_nm
        if has_k and k_in_domain:
            try:
                _n, k_value, _details = optical_constants.evaluate_optical_set_with_details(
                    record, wavelength, unit, include_k=True)
            except (ValueError, TypeError, OverflowError):
                k_value = None
            if k_value is not None and math.isfinite(k_value):
                point["k"] = k_value
        points.append(point)
    return points


def _table_value(wavelengths, values, x):
    """Linear interpolation helper for display-only tabulated ranges."""
    if x <= wavelengths[0]:
        return values[0]
    if x >= wavelengths[-1]:
        return values[-1]
    lo, hi = 0, len(wavelengths) - 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if wavelengths[mid] < x:
            lo = mid
        else:
            hi = mid
    fraction = (x - wavelengths[lo]) / (wavelengths[hi] - wavelengths[lo])
    return values[lo] + fraction * (values[hi] - values[lo])


def _range_text(values):
    return f"{min(values):.6g}–{max(values):.6g}" if values else "未知"


def _optical_summary(record):
    """Build a compact range summary without replacing the underlying data."""
    low, high = record["wavelength_min"], record["wavelength_max"]
    unit = record["wavelength_unit"]
    if record["kind"] == "tabulated":
        waves, n_values = record["wavelengths"], record["n"]
        n_range = [n_values[i] for i, x in enumerate(waves) if low <= x <= high]
        n_range.extend((_table_value(waves, n_values, low), _table_value(waves, n_values, high)))
        n_label = "n=" + _range_text(n_range)
    else:
        count = 129
        n_range = [optical_constants.evaluate_optical_set_with_details(
            record, low + (high - low) * i / (count - 1), unit, include_k=False)[0]
            for i in range(count)]
        n_label = "n≈" + _range_text(n_range) + f"（域内{count}点采样估计）"

    k_values = []
    if record["kind"] == "tabulated" and record.get("k") is not None:
        waves, samples = record["wavelengths"], record["k"]
        k_values = [samples[i] for i, x in enumerate(waves) if low <= x <= high]
        k_values.extend((_table_value(waves, samples, low), _table_value(waves, samples, high)))
    elif record.get("k_data") is not None:
        data = record["k_data"]
        scale = 1000.0 if data["wavelength_unit"] == "um" else 1.0
        n_scale = 1000.0 if unit == "um" else 1.0
        k_low = max(data["wavelength_min"] * scale, low * n_scale)
        k_high = min(data["wavelength_max"] * scale, high * n_scale)
        if k_low <= k_high:
            k_low /= scale
            k_high /= scale
            inside = [value for x, value in zip(data["wavelengths"], data["k"])
                      if k_low <= x <= k_high]
            k_values = inside + [_table_value(data["wavelengths"], data["k"], k_low),
                                 _table_value(data["wavelengths"], data["k"], k_high)]
    k_label = "k=" + _range_text(k_values) if k_values else "k 未知"
    return {
        "range": f"{low:g}–{high:g} {unit}",
        "summary": f"波段 {low:g}–{high:g} {unit}；{n_label}；{k_label}",
        "kStatus": "含 k" if k_values else "k 未知",
        "conditions": _optical_condition_rows(record.get("process_parameters", [])),
    }


def _parse_k_table(text):
    if not isinstance(text, str):
        raise ValueError("独立 k 表必须是文字")
    wavelengths, values = [], []
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cells = [cell.strip() for cell in line.split(",")] if "," in line else re.split(r"\s+", line)
        if not wavelengths and [cell.casefold() for cell in cells] in (("wavelength", "k"), ("wavelength", "n")):
            continue
        if len(cells) != 2:
            raise ValueError(f"第 {line_number} 行必须有 wavelength,k 两列")
        try:
            x, y = float(cells[0]), float(cells[1])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"第 {line_number} 行含无效数值") from exc
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError(f"第 {line_number} 行必须为有限数值")
        wavelengths.append(x)
        values.append(y)
    if len(wavelengths) < 2:
        raise ValueError("独立 k 表至少需要两个数据点")
    return wavelengths, values


def _editor_grid_to_text(rows, *, k_only=False):
    """Validate editable grid cells and serialize them as the existing TSV contract."""
    if not isinstance(rows, list):
        raise ValueError("数据表必须是行列表")
    keys = ("wavelength", "k") if k_only else ("wavelength", "n", "k")
    has_k = k_only or any(isinstance(row, dict) and str(row.get("k", "")).strip() for row in rows)
    columns = keys if k_only or has_k else keys[:2]
    output = []
    seen = {}
    previous = None
    for row_number, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {row_number} 行不是有效数据行")
        values = {key: str(row.get(key, "") if row.get(key) is not None else "").strip() for key in keys}
        if all(not value for value in values.values()):
            continue
        required = ("wavelength", "k") if k_only else (("wavelength", "n", "k") if has_k else ("wavelength", "n"))
        for key in required:
            if not values[key]:
                label = {"wavelength": "波长", "n": "n", "k": "k"}[key]
                hint = "；未知 k 请将整列 k 留空" if key == "k" and not k_only else ""
                raise ValueError(f"第 {row_number} 行 {label} 单元格为空{hint}")
        numbers = {}
        for key in required:
            label = {"wavelength": "波长", "n": "n", "k": "k"}[key]
            try:
                number = float(values[key])
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"第 {row_number} 行 {label} 单元格不是有效数字") from exc
            if not math.isfinite(number):
                raise ValueError(f"第 {row_number} 行 {label} 单元格必须是有限数值")
            numbers[key] = number
        wavelength = numbers["wavelength"]
        if wavelength <= 0:
            raise ValueError(f"第 {row_number} 行波长必须大于 0")
        if wavelength in seen:
            raise ValueError(f"第 {row_number} 行波长重复（与第 {seen[wavelength]} 行相同）")
        if previous is not None and wavelength < previous:
            raise ValueError(f"第 {row_number} 行波长必须按升序排列")
        previous = wavelength
        seen[wavelength] = row_number
        if "k" in numbers and numbers["k"] < 0:
            raise ValueError(f"第 {row_number} 行 k 单元格不能为负数")
        output.append("\t".join(values[key] for key in columns))
    if len(output) < 2:
        raise ValueError("数据表至少需要两行完整数据")
    return "\n".join(output)


def _optical_record_from_editor(form, existing=None):
    if not isinstance(form, dict):
        raise ValueError("光学编辑表单必须是 JSON 对象")
    allowed = {"id", "name", "notes", "source", "kind", "wavelength_unit", "wavelength_min", "wavelength_max",
               "table", "table_rows", "formula_number", "coefficients", "parameters", "has_independent_k",
               "k_table", "k_rows", "k_wavelength_unit", "k_wavelength_min", "k_wavelength_max", "k_notes",
               "process_parameters"}
    unknown = set(form) - allowed
    if unknown:
        raise ValueError("光学编辑表单包含未知字段：" + ", ".join(sorted(unknown)))
    if existing is not None and form.get("id", existing["id"]) != existing["id"]:
        raise ValueError("编辑数据集时必须保留原 ID")
    identity = existing["id"] if existing else form.get("id") or uuid.uuid4().hex
    name, notes = form.get("name", ""), form.get("notes", "")
    source = form.get("source", (existing or {}).get("source", "用户输入，未独立验证"))
    kind = form.get("kind")
    unit = form.get("wavelength_unit")
    low, high = form.get("wavelength_min"), form.get("wavelength_max")

    if existing is not None and kind == "tabulated" and existing.get("k_data") is not None:
        raise ValueError("带独立 k 数据的色散模型不能改为离散模型；请选择保留独立 k 的色散模型")

    if kind == "tabulated":
        table = (_editor_grid_to_text(form["table_rows"]) if "table_rows" in form else form.get("table", ""))
        record = optical_constants.import_optical_text(
            table, name=name, wavelength_unit=unit, notes=notes,
            source=source, identifier=identity)
        if low is not None and str(low).strip():
            record["wavelength_min"] = low
        if high is not None and str(high).strip():
            record["wavelength_max"] = high
    elif kind in ("cauchy", "sellmeier", "rii_formula"):
        record = {"id": identity, "name": name, "notes": notes, "source": source,
                  "kind": kind, "wavelength_unit": unit,
                  "wavelength_min": low, "wavelength_max": high}
        if kind == "rii_formula":
            record["formula_number"] = form.get("formula_number")
            record["coefficients"] = form.get("coefficients")
        else:
            record["parameters"] = form.get("parameters")
    else:
        raise ValueError("请选择受支持的光学模型")

    if kind != "tabulated" and form.get("has_independent_k"):
        k_table = (_editor_grid_to_text(form["k_rows"], k_only=True)
                   if "k_rows" in form else form.get("k_table", ""))
        waves, values = _parse_k_table(k_table)
        previous_k = (existing or {}).get("k_data") or {}
        record["k_data"] = {
            "wavelength_unit": form.get("k_wavelength_unit"),
            "wavelength_min": form.get("k_wavelength_min"),
            "wavelength_max": form.get("k_wavelength_max"),
            "wavelengths": waves, "k": values,
            "source": previous_k.get("source", "用户输入，未独立验证"),
            "notes": form.get("k_notes", previous_k.get("notes", "")),
        }
    validated = optical_constants.validate_optical_set(record)
    process_parameters = _normalize_process_parameters(
        form.get("process_parameters", (existing or {}).get("process_parameters", [])))
    if process_parameters:
        validated["process_parameters"] = process_parameters
    return validated


# Workers operate only on ordinary Python values, never QObject/QML objects.
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="material-data")
_MATERIAL_FUTURES_LOCK = threading.RLock()
_MATERIAL_FUTURES = set()
_MATERIAL_BRIDGES = []
_OPTICAL_CURVE_CACHE_LIMIT = 64
_OPTICAL_CURVE_JOB_LIMIT = 4
_OPTICAL_CURVE_LOCK = threading.RLock()
_OPTICAL_CURVE_CACHE: OrderedDict[tuple[str, int], tuple[dict, tuple[dict, ...]]] = OrderedDict()
_OPTICAL_CURVE_FUTURES = {}


def _submit_material_task(callback, *args):
    future = _EXECUTOR.submit(callback, *args)
    with _MATERIAL_FUTURES_LOCK:
        _MATERIAL_FUTURES.add(future)

    def forget(completed):
        with _MATERIAL_FUTURES_LOCK:
            _MATERIAL_FUTURES.discard(completed)

    future.add_done_callback(forget)
    return future


def wait_for_material_workers():
    """Cancel queued material tasks and wait until every submitted task exits."""
    _MATERIAL_BRIDGES[:] = [ref for ref in _MATERIAL_BRIDGES if ref() is not None]
    for bridge_ref in tuple(_MATERIAL_BRIDGES):
        bridge = bridge_ref()
        if bridge is None:
            continue
        try:
            bridge._cancel_background_work()
        except RuntimeError:
            pass
    while True:
        with _MATERIAL_FUTURES_LOCK:
            pending = tuple(_MATERIAL_FUTURES)
        if not pending:
            return
        for future in pending:
            future.cancel()
        for future in pending:
            try:
                future.result()
            except Exception:
                pass
        with _MATERIAL_FUTURES_LOCK:
            completed = [future for future in _MATERIAL_FUTURES if future.done()]
            _MATERIAL_FUTURES.difference_update(completed)


def _optical_curve_cache_key(record):
    # Records are immutable snapshots: edits replace the row object. Holding
    # the record in the bounded cache also prevents Python id reuse.
    return str(record["id"]), id(record)


def _cache_optical_curve_result(key, record, future):
    try:
        points = tuple(dict(point) for point in future.result())
    except Exception:
        points = None
    with _OPTICAL_CURVE_LOCK:
        if _OPTICAL_CURVE_FUTURES.get(key) is future:
            _OPTICAL_CURVE_FUTURES.pop(key, None)
        if points is not None:
            _OPTICAL_CURVE_CACHE[key] = (record, points)
            _OPTICAL_CURVE_CACHE.move_to_end(key)
            while len(_OPTICAL_CURVE_CACHE) > _OPTICAL_CURVE_CACHE_LIMIT:
                _OPTICAL_CURVE_CACHE.popitem(last=False)


def _submit_optical_curve(key, record):
    with _OPTICAL_CURVE_LOCK:
        cached = _OPTICAL_CURVE_CACHE.get(key)
        if cached is not None:
            _OPTICAL_CURVE_CACHE.move_to_end(key)
            return None, [dict(point) for point in cached[1]], ""
        future = _OPTICAL_CURVE_FUTURES.get(key)
        if future is not None:
            return future, None, ""
        if len(_OPTICAL_CURVE_FUTURES) >= _OPTICAL_CURVE_JOB_LIMIT:
            return None, None, "光学曲线任务繁忙，请稍后重试。"
        future = _submit_material_task(_optical_curve_points, record)
        _OPTICAL_CURVE_FUTURES[key] = future
        future.add_done_callback(lambda completed, cache_key=key, snapshot=record:
                                 _cache_optical_curve_result(cache_key, snapshot, completed))
        return future, None, ""


def _prepare_material(value):
    record = materials.validate_material_profile(value)
    return record, prepare_reference_sections(record.get("reference_data"), record.get("research_data"))


def _entry_rows(builtins, entries, overridden_ids=()):
    result = []
    for rows, prefix, builtin in ((builtins, "builtin:", True), (entries, "user:", False)):
        for row in rows:
            research = row.get("research_data") or {}
            roles = row.get("roles", [])
            result.append({"id": row["id"], "key": prefix + row["id"], "name": materials.material_display_name(row),
                "description": row.get("description", ""), "builtin": builtin,
                "overridden": builtin and row["id"] in overridden_ids,
                "opticalOnly": bool(row.get("optical_only", False)),
                "roles": list(roles), "category": materials.material_category(row), "roleLabel": role_label(roles),
                "sourceBindable": materials.source_bindable(row), "substrateBindable": materials.substrate_bindable(row), "coverage": "",
                "formula": research.get("formula") or "",
                "searchText": " ".join((row["id"], row["name"], row.get("description", ""),
                    research.get("formula") or "", " ".join(research.get("aliases", [])), role_label(roles), " ".join(roles))).casefold()})
    return result


def _read_catalog(folder, previous_entries):
    issues = []
    try:
        from coat_sim.core.material_business import MaterialLibrary
        service = MaterialLibrary(directory=folder)
        catalogue = service.catalog_records()
        builtins = [item["record"] for item in catalogue if item["builtin"]]
        entries = [item["record"] for item in catalogue if not item["builtin"]]
        overridden_ids = {item["record"]["id"] for item in catalogue if item["overridden"]}
        deleted_rows = service.deleted_builtins()
        return (builtins, entries, _entry_rows(builtins, entries, overridden_ids),
                issues, overridden_ids, deleted_rows)
    except (OSError, ValueError, TypeError) as exc:
        issues.append(f"material library could not be read: {exc}")
    try:
        builtins = materials.builtin_material_profiles()
    except (OSError, ValueError) as exc:
        builtins = []
        issues.append(f"内置文献参考数据不可用：{exc}")
    try:
        entries = materials.list_materials(folder)
    except (OSError, ValueError, TypeError) as exc:
        entries = previous_entries
        issues.append(f"用户材料库读取失败：{exc}；已保留原列表")
    return builtins, entries, _entry_rows(builtins, entries), issues


def _library_service(folder):
    from coat_sim.core.material_business import MaterialLibrary
    return MaterialLibrary(directory=folder)


def _save_library_record(folder, record, builtin, update):
    service = _library_service(folder)
    return (service.save_builtin_override(record) if builtin else
            service.write(record, update=update))


def _restore_library_builtin(folder, identifier):
    return _prepare_material(_library_service(folder).restore_builtin(identifier))


def _export_library_material(folder, identifier, path):
    return _library_service(folder).export_single_file(identifier, path)


def _export_library_selection(folder, path, identifiers):
    return _library_service(folder).export_library_file(path, identifiers)


def _preview_library_import(folder, path):
    service = _library_service(folder)
    return service.preview_import(service.parse_package_file(path))


def _apply_library_import(folder, plan, decisions):
    return _library_service(folder).apply_import(plan, decisions)


def _delete_library_material(folder, identifier):
    return materials.delete_material(folder, identifier)


def _delete_library_builtin(folder, identifier):
    return _library_service(folder).delete_builtin(identifier)


class MaterialLibraryBridge(QObject):

    @Property(QObject, constant=True)
    def opticalInversion(self):
        if not hasattr(self, "_optical_inversion_controller"):
            from .optical_inversion import OpticalInversionController
            self._optical_inversion_controller = OpticalInversionController(self)
        return self._optical_inversion_controller

    @Slot()
    def cancelOpticalInversion(self):
        if hasattr(self, "_optical_inversion_controller"):
            self._optical_inversion_controller.invalidate()

    @Property(QObject, constant=True)
    def informationController(self):
        if not hasattr(self, "_information_controller"):
            from .project_resource_adapter import EditorResourceInformationController
            self._information_controller = EditorResourceInformationController(self, "materials", self)
        return self._information_controller

    changed = Signal()
    catalogCommitted = Signal()
    draftChanged = Signal()
    discardRequested = Signal()
    deleteRequested = Signal()
    materialSelected = Signal(object)
    opticalCurveReady = Signal(str, int, object, str)
    importPreviewReady = Signal()
    importApplyFinished = Signal(bool)

    def __init__(self, parent=None, directory: Path | None = None, purpose="film"):
        super().__init__(parent)
        _MATERIAL_BRIDGES[:] = [ref for ref in _MATERIAL_BRIDGES if ref() is not None]
        _MATERIAL_BRIDGES.append(weakref.ref(self))
        self._purpose = "substrate" if purpose == "substrate" else "film"
        self._directory = Path(directory) if directory else None
        self._entries = []
        self._entry_cache = []
        self._sections = []
        self._optical_sets = []
        self._optical_preview_cache = OrderedDict()
        self._category = None
        self._optical_only = False
        self._source_bindable = False
        self._null_models = set()
        self._load_future = None
        self._catalog_future = None
        self._io_future = None
        self._io_context = None
        self._io_generation = 0
        self._operation_busy = False
        self._operation_kind = ""
        self._import_preview_plan = None
        self._import_preview_rows = []
        self._import_error = ""
        self._refresh_current_after_catalog = False
        self._load_token = 0
        self._optical_curve_generation = 0
        self._optical_curve_latest_generations = {}
        self._optical_curve_requests = {}
        self._optical_set_data_revision = 0
        self._loading = False
        self._open_after_catalog = False
        self._timer = QTimer(self)
        self._timer.setInterval(20)
        weak_self = weakref.ref(self)
        def poll():
            owner = weak_self()
            if owner is not None:
                owner._poll_workers()
        self._timer.timeout.connect(poll)
        self._builtins = []
        self._builtin = False
        self._builtin_overridden = False
        self._overridden_ids = set()
        self._deleted_builtins: list[dict[str, Any]] = []
        self._selected_key = ""
        self._reference_data = None
        self._research_data = None
        self._roles = None
        self._extra_profile_data = {}
        self._search_text = ""
        # A material-library view must always start on a visible, selectable
        # category. Source and substrate workflows still set their own filter
        # explicitly when opening the library.
        self._role_filter = "film"
        self._initial_default = True
        self._draft = {}
        self._dirty = False
        self._saved_id = None
        self._pending: tuple[str, Any] | None = None
        self._status = ""
        self._error = ""
        self._valid = False
        initial = materials.default_material_profile()
        if self._purpose == "substrate":
            initial["category"] = "substrate"
        self._load_draft(initial)
        self.reload()

    @Slot(QUrl, result=str)
    def localFilePath(self, url: QUrl) -> str:
        path = local_file_path(url)
        if not path:
            self._status = "请选择本地文件。"
            self.changed.emit()
        return path

    def _folder(self):
        return self._directory or data_path("materials")

    def _load_draft(self, value, sections=None, prepared=False):
        self._sections = sections if sections is not None else []
        # Preserve fields added by the validated material schema (for example
        # an explicit optical-only capability marker) through edit/copy/save.
        self._extra_profile_data = deepcopy({key: item for key, item in value.items()
                                             if key not in _MATERIAL_PROFILE_FIELDS})
        self._optical_only = bool(value.get("optical_only", False))
        self._source_bindable = materials.source_bindable(value)
        self._null_models = set()
        # Prepared worker records are privately owned immutable snapshots.
        # Edits below replace optical lists/maps, never mutate this evidence.
        self._optical_sets = (value.get("optical_constants") or []) if prepared else deepcopy(value.get("optical_constants") or [])
        self._invalidate_optical_curve_requests(data_changed=True)
        self._optical_preview_cache = OrderedDict()
        self._category = value.get("category")
        self._reference_data = value.get("reference_data") if prepared else deepcopy(value.get("reference_data"))
        self._research_data = value.get("research_data") if prepared else deepcopy(value.get("research_data"))
        self._roles = deepcopy(value.get("roles"))
        fields = {key: str(value.get(key, "")) for key in ("id", "name", "description", "notes")}
        for prefix, keys in MODEL_NUMBERS.items():
            model = value.get(prefix + "_model")
            if model is None:
                if not self._optical_only:
                    raise ValueError(f"{prefix}_model is missing on a non-optical-only material")
                self._null_models.add(prefix)
                for key in keys:
                    fields[prefix + "_" + key] = ""
                for key, fallback in (("status", "unknown"), ("reference", ""), ("notes", "")):
                    fields[prefix + "_provenance_" + key] = fallback
                continue
            if not isinstance(model, dict):
                raise ValueError(f"{prefix}_model must be an object or null on optical-only materials")
            for key in keys:
                number = (model.get("defaults") or {}).get(key)
                fields[prefix + "_" + key] = _format(number * 10 if key in RATE_KEYS and number is not None else number)
            provenance = model.get("provenance", {})
            for key in ("status", "reference", "notes"):
                fields[prefix + "_provenance_" + key] = str(provenance.get(key, "unvalidated" if key == "status" else ""))
        self._draft = fields
        self._dirty = False
        if prepared:
            self._valid, self._error = True, ""
        else:
            self._validate()
        self.draftChanged.emit()

    def draftData(self, include_evidence=True):
        data = deepcopy(self._extra_profile_data)
        data.update({"schema_version": 1, **{key: self._draft[key] for key in ("id", "name", "description", "notes")}})
        if include_evidence and self._reference_data is not None:
            data["reference_data"] = deepcopy(self._reference_data)
        if include_evidence and self._research_data is not None:
            data["research_data"] = deepcopy(self._research_data)
        if self._roles is not None:
            data["roles"] = deepcopy(self._roles)
        if self._category is not None:
            data["category"] = self._category
        if include_evidence and self._optical_sets:
            data["optical_constants"] = deepcopy(self._optical_sets)
        for prefix, keys in MODEL_NUMBERS.items():
            if prefix in self._null_models:
                data[prefix + "_model"] = None
                continue
            defaults = {}
            for key in keys:
                text = self._draft[prefix + "_" + key]
                if not text.strip() and key.startswith("valid_rate_"):
                    defaults[key] = None
                    continue
                value = _float(text, key)
                defaults[key] = value / 10 if key in RATE_KEYS else value
            provenance = {key: self._draft[prefix + "_provenance_" + key] for key in ("status", "reference", "notes")}
            data[prefix + "_model"] = {"defaults": defaults, "provenance": provenance}
        return materials.validate_material_profile(data)

    def _validate(self):
        try:
            self.draftData(include_evidence=False)
        except (ValueError, TypeError, KeyError) as exc:
            self._valid, self._error = False, str(exc)
        else:
            self._valid, self._error = True, ""
        self.changed.emit()

    @Property(list, notify=changed)
    def entries(self):
        return [dict(row, roles=list(row["roles"])) for row in self._entry_cache]

    @Property(list, notify=changed)
    def filmEntries(self):
        return [dict(row, roles=list(row["roles"])) for row in self._entry_cache if row["sourceBindable"]]

    @Property(list, notify=changed)
    def substrateEntries(self):
        return [dict(row, roles=list(row["roles"])) for row in self._entry_cache if row["substrateBindable"]]

    @Property(str, constant=True)
    def purpose(self):
        return self._purpose

    @Property(bool, notify=changed)
    def substrateBindable(self):
        roles = self._roles or []
        return bool({"substrate", "medium"}.intersection(roles)) or (
            not roles and self._category == "substrate"
        )

    @Property(bool, notify=changed)
    def loading(self):
        return self._loading or self._catalog_future is not None or self._operation_busy

    def hasMaterial(self, identifier):
        """True when this bridge's cached catalog already knows the material id.

        The lookup ignores case because the core library treats material ids as
        case-insensitive.
        """
        key = str(identifier or "").casefold()
        if not key:
            return False
        return any(str(row.get("id", "")).casefold() == key
                   for row in (*self._builtins, *self._entries))

    @Property(bool, notify=changed)
    def operationBusy(self):
        return self._operation_busy

    @Property(list, notify=changed)
    def importPreview(self):
        return deepcopy(self._import_preview_rows)

    @Property(str, notify=changed)
    def importError(self):
        return self._import_error

    @Property(bool, notify=changed)
    def importPreviewBusy(self):
        return self._operation_busy and self._operation_kind == "preview_import"

    @Property(str, notify=changed)
    def category(self):
        return materials.material_category({"roles": self._roles} if self._roles else {"category": self._category or "film"})

    @Slot(str)
    def setCategory(self, value):
        if self._builtin or self._roles or self._loading or value not in ("film", "substrate", "other"):
            return
        self._category = value
        self._dirty = True
        self._validate()

    def _optical_preview_entry(self, record):
        identity = record["id"]
        entry = self._optical_preview_cache.get(identity)
        if entry is None or entry[0] is not record:
            entry = [record, None, None]
            self._optical_preview_cache[identity] = entry
        self._optical_preview_cache.move_to_end(identity)
        while len(self._optical_preview_cache) > 48:
            self._optical_preview_cache.popitem(last=False)
        return entry

    def _invalidate_optical_curve_requests(self, data_changed=False):
        self._optical_curve_generation += 1
        self._optical_curve_latest_generations.clear()
        self._optical_curve_requests.clear()
        if data_changed:
            self._optical_set_data_revision += 1
        if (hasattr(self, "_timer") and self._catalog_future is None
                and self._load_future is None and self._io_future is None):
            self._timer.stop()

    @Property(int, notify=changed)
    def opticalSetDataRevision(self):
        return self._optical_set_data_revision

    def _optical_summary_for(self, record):
        entry = self._optical_preview_entry(record)
        if entry[1] is None:
            entry[1] = _optical_summary(record)
        return deepcopy(entry[1])

    def _optical_curve_for(self, record):
        entry = self._optical_preview_entry(record)
        return [] if entry[2] is None else deepcopy(entry[2])

    @Property(list, notify=changed)
    def opticalSets(self):
        catalog = optical_constants.optical_model_catalog()
        result = []
        for row in self._optical_sets:
            spec = next((item for item in catalog if item["kind"] == row["kind"] and
                         (row["kind"] != "rii_formula" or item.get("formula_number") == row.get("formula_number"))), {})
            try:
                summary = self._optical_summary_for(row)
            except (ValueError, TypeError, OverflowError):
                summary = {"range": f'{row["wavelength_min"]:g}–{row["wavelength_max"]:g} {row["wavelength_unit"]}',
                           "summary": f'波段 {row["wavelength_min"]:g}–{row["wavelength_max"]:g} {row["wavelength_unit"]}；n 范围不可估；k 未知',
                           "kStatus": "k 未知", "conditions": _optical_condition_rows(row.get("process_parameters", []))}
            result.append({"id": row["id"], "name": row["name"], "notes": row["notes"],
                           "kind": row["kind"], "kindLabel": spec.get("label", row["kind"]),
                           "process_parameters": _optical_condition_rows(row.get("process_parameters", [])),
                           **summary})
        return result

    @Slot(str, result="QVariantList")
    def opticalSetCurve(self, identity):
        row = next((item for item in self._optical_sets if item["id"] == identity), None)
        return [] if row is None else list(self._optical_curve_for(row))

    @Slot(str, result=int)
    def requestOpticalSetCurve(self, identity):
        identity = str(identity)
        self._optical_curve_generation += 1
        generation = self._optical_curve_generation
        previous = self._optical_curve_latest_generations.get(identity)
        if previous is not None:
            self._optical_curve_requests.pop(previous, None)
        self._optical_curve_latest_generations[identity] = generation
        row = next((item for item in self._optical_sets if item["id"] == identity), None)
        if self._loading:
            request = {"identity": identity, "record": None, "future": None,
                       "points": [], "error": "材料仍在加载，请稍后请求曲线。"}
        elif row is None:
            request = {"identity": identity, "record": None, "future": None,
                       "points": [], "error": "未找到所选光学数据集。"}
        else:
            entry = self._optical_preview_entry(row)
            if entry[2] is not None:
                future, points, error = None, deepcopy(entry[2]), ""
            else:
                cache_key = _optical_curve_cache_key(row)
                future, points, error = _submit_optical_curve(cache_key, row)
            request = {"identity": identity, "record": row, "future": future,
                       "points": points, "error": error}
        self._optical_curve_requests[generation] = request
        self._timer.start()
        return generation

    @Slot(str, int, result=bool)
    def cancelOpticalSetCurve(self, identity, generation):
        identity = str(identity)
        generation = int(generation)
        if self._optical_curve_latest_generations.get(identity) != generation:
            return False
        self._optical_curve_latest_generations.pop(identity, None)
        self._optical_curve_requests.pop(generation, None)
        if (not self._optical_curve_requests and hasattr(self, "_timer")
                and self._catalog_future is None and self._load_future is None
                and self._io_future is None):
            self._timer.stop()
        return True

    @Property(list, notify=changed)
    def opticalModelCatalog(self):
        return [dict(row) for row in optical_constants.optical_model_catalog()]

    @Slot(str, int, str, result=str)
    def opticalFormulaSvg(self, kind, formula_number, color):
        """Render the core catalog's canonical Mathtext equation to themed SVG."""
        spec = next((row for row in optical_constants.optical_model_catalog()
                     if row["kind"] == kind and
                     (kind != "rii_formula" or row.get("formula_number") == formula_number)), None)
        if not spec or not spec.get("formula_latex"):
            return ""
        path = render_optical_formula_svg(str(spec["formula_latex"]), color)
        return QUrl.fromLocalFile(path).toString() if path else ""

    @Slot(str, int, result=str)
    def opticalFormulaImage(self, kind, formula_number):
        """Keep the original formula-image entry point for external callers."""
        return self.opticalFormulaSvg(kind, formula_number, "#202124")

    @Slot("QVariantList", result="QVariantMap")
    def projectOpticalProcessParameterRows(self, rows):
        return project_process_parameter_rows(rows)

    @Slot("QVariantList", "QVariantList", result="QVariantList")
    def serializeOpticalProcessParameterRows(self, rows, originals):
        return serialize_process_parameter_rows(rows, originals)

    @Slot(str, result="QVariantMap")
    def opticalSetDetails(self, identity):
        row = next((item for item in self._optical_sets if item["id"] == identity), None)
        if row is None:
            return {}
        return {**deepcopy(row), "conditions": _optical_condition_rows(row.get("process_parameters", [])),
                }

    @Slot(str, str, str, result=str)
    def previewOpticalFile(self, path, name, unit):
        try:
            url = QUrl(path)
            path = url.toLocalFile() if url.isLocalFile() else path
            record = optical_constants.import_optical_file(path, name=name or Path(path).stem,
                wavelength_unit=unit, source=Path(path).name)
            record["wavelengths_text"] = "\n".join(
                "\t".join(_format(values[i]) for values in
                          ([record["wavelengths"], record["n"]] if record.get("k") is None else
                           [record["wavelengths"], record["n"], record["k"]]))
                for i in range(len(record["wavelengths"])))
            return json.dumps(record, ensure_ascii=False)
        except (ValueError, OSError, TypeError) as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    @Slot(result=str)
    def clipboardText(self):
        clipboard = QGuiApplication.clipboard()
        return clipboard.text() if clipboard is not None else ""

    @Slot(str)
    def setClipboardText(self, value):
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(str(value))

    def _reference_sections_with_optics(self):
        sections = [dict(row) for row in self._sections]
        for record in self._optical_sets:
            if record.get("notes", "").strip():
                sections.append({"title": f"\u5149\u5b66\u6570\u636e\u5907\u6ce8 \xb7 {record['name']}",
                    "body": record["notes"], "source": record.get("source", ""), "url": "", "warning": False})
            if record.get("source", "").strip():
                sections.append({"title": f"光学数据来源 · {record['name']}",
                    "body": record["source"], "source": record["source"], "url": "", "warning": False})
            k_data = record.get("k_data") or {}
            if k_data.get("notes", "").strip():
                sections.append({"title": f"\u72ec\u7acb k \u6570\u636e\u5907\u6ce8 \xb7 {record['name']}",
                    "body": k_data["notes"], "source": k_data.get("source", ""), "url": "", "warning": False})
            if k_data.get("source", "").strip():
                sections.append({"title": f"独立 k 数据来源 · {record['name']}",
                    "body": k_data["source"], "source": k_data["source"], "url": "", "warning": False})
        return sections

    def _optical_edit_blocked(self):
        if self._loading:
            self._status = "材料仍在载入；载入完成后再编辑光学数据集，当前材料未改变"
            self.changed.emit()
            return True
        return False

    def _add_optical(self, record):
        if self._optical_edit_blocked():
            return False
        try:
            self._optical_sets = optical_constants.validate_optical_sets(self._optical_sets + [record])
            self._invalidate_optical_curve_requests(data_changed=True)
            self._optical_preview_cache.clear()
        except ValueError as exc:
            self._status = str(exc); self.changed.emit(); return False
        self._dirty = True
        self._status = "已加入光学常数；保存材料后才会写入文件，不参与沉积计算"
        self._validate()
        self.changed.emit()
        return True

    @Slot(str, str, str, str, result=bool)
    def importOpticalFile(self, path, name, unit, notes):
        if self._optical_edit_blocked():
            return False
        try:
            url = QUrl(path)
            path = url.toLocalFile() if url.isLocalFile() else path
            record = optical_constants.import_optical_file(path, name=name, wavelength_unit=unit, notes=notes,
                                                            source=Path(path).name)
            return self._add_optical(record)
        except (ValueError, OSError) as exc:
            self._status = f"导入失败：{exc}"; self.changed.emit(); return False

    @Slot(str, str, str, str, str, str, str, result=bool)
    def addDispersionModel(self, name, kind, unit, low, high, parameters, notes):
        if self._optical_edit_blocked():
            return False
        try:
            p = json.loads(parameters)
            record_data = {"id": uuid.uuid4().hex, "name": name, "notes": notes,
                "source": "用户输入，未独立验证", "kind": kind, "wavelength_unit": unit,
                "wavelength_min": low, "wavelength_max": high}
            if kind == "rii_formula":
                record_data.update(formula_number=p.get("formula_number"), coefficients=p.get("coefficients"))
            else:
                record_data["parameters"] = p
            record = optical_constants.validate_optical_set(record_data)
            return self._add_optical(record)
        except (ValueError, TypeError) as exc:
            self._status = f"模型无效：{exc}"; self.changed.emit(); return False

    @Slot(str, str, str, result=bool)
    def editOpticalMetadata(self, identity, name, notes):
        row = self.opticalSetDetails(identity)
        if not row:
            return False
        form = {"id": identity, "name": name, "notes": notes, "source": row.get("source", ""),
                "kind": row["kind"], "wavelength_unit": row["wavelength_unit"],
                "wavelength_min": row["wavelength_min"], "wavelength_max": row["wavelength_max"]}
        if row["kind"] == "tabulated":
            values = [row["wavelengths"], row["n"]] + ([row["k"]] if row.get("k") is not None else [])
            form["table"] = "\n".join("\t".join(_format(column[i]) for column in values)
                                      for i in range(len(row["wavelengths"])))
        elif row["kind"] == "rii_formula":
            form.update(formula_number=row["formula_number"], coefficients=row["coefficients"])
        else:
            form["parameters"] = row["parameters"]
        if row.get("k_data"):
            k = row["k_data"]
            form.update(has_independent_k=True, k_table="\n".join(
                f"{_format(x)}\t{_format(y)}" for x, y in zip(k["wavelengths"], k["k"])),
                k_wavelength_unit=k["wavelength_unit"], k_wavelength_min=k["wavelength_min"],
                k_wavelength_max=k["wavelength_max"], k_notes=k.get("notes", ""))
        return self.updateOpticalSet(identity, json.dumps(form, ensure_ascii=False))

    @Slot(str, result=bool)
    def addOpticalSet(self, form_json):
        if self._optical_edit_blocked():
            return False
        try:
            form = json.loads(form_json)
            record = _optical_record_from_editor(form)
            rows = optical_constants.validate_optical_sets(self._optical_sets + [record])
        except (ValueError, TypeError, KeyError) as exc:
            self._status = f"光学数据集无效：{exc}；当前材料未改变"
            self.changed.emit()
            return False
        self._optical_sets = rows
        self._invalidate_optical_curve_requests(data_changed=True)
        self._optical_preview_cache.clear()
        self._dirty = True
        self._status = "数据集已加入当前材料；保存材料后才会写入文件"
        self._validate()
        self.changed.emit()
        return True

    @Slot(str, str, result=bool)
    def updateOpticalSet(self, identity, form_json):
        if self._optical_edit_blocked():
            return False
        try:
            old = next(row for row in self._optical_sets if row["id"] == identity)
            form = json.loads(form_json)
            record = _optical_record_from_editor(form, old)
            rows = deepcopy(self._optical_sets)
            rows[next(i for i, row in enumerate(rows) if row["id"] == identity)] = record
            rows = optical_constants.validate_optical_sets(rows)
        except (ValueError, TypeError, KeyError, StopIteration) as exc:
            self._status = f"数据集未更新：{exc}；原数据仍保留"
            self.changed.emit()
            return False
        self._optical_sets = rows
        self._invalidate_optical_curve_requests(data_changed=True)
        self._optical_preview_cache.clear()
        self._dirty = True
        self._status = "数据集修改已应用到当前材料；保存材料后才会写入文件"
        self._validate()
        self.changed.emit()
        return True

    @Slot(str)
    def removeOpticalSet(self, identity):
        if self._optical_edit_blocked():
            return
        rows = [row for row in self._optical_sets if row["id"] != identity]
        if len(rows) != len(self._optical_sets):
            self._optical_sets = rows
            self._invalidate_optical_curve_requests(data_changed=True)
            self._optical_preview_cache.clear()
            self._dirty = True
            self._validate()
            self.changed.emit()

    @Property(list, notify=changed)
    def filteredEntries(self):
        terms = self._search_text.casefold().split()
        return [dict(row, roles=list(row["roles"])) for row in self._entry_cache
                if (self._role_filter in row["roles"] or self._role_filter == row["category"] or
                    (self._role_filter == "substrate" and row["substrateBindable"]))
                and all(term in row["searchText"] for term in terms)]

    @Property(str, notify=changed)
    def searchText(self):
        return self._search_text

    @Property(str, notify=changed)
    def roleFilter(self):
        return self._role_filter

    @Slot(str)
    def setSearchText(self, value):
        if self._search_text != value:
            self._search_text = value
            self.changed.emit()

    @Slot(str)
    def setRoleFilter(self, value):
        if value in {"other", *ROLE_LABELS} and self._role_filter != value:
            self._role_filter = value
            self.changed.emit()

    @Property(list, notify=changed)
    def roles(self):
        return deepcopy(self._roles or [])

    @Property(str, notify=changed)
    def roleLabel(self):
        return role_label(self._roles)

    @Property(bool, notify=changed)
    def sourceBindable(self):
        return self._source_bindable

    @Property(bool, notify=changed)
    def opticalOnly(self):
        return self._optical_only

    @Property(bool, notify=changed)
    def canUseMaterial(self):
        return self._valid and (self.substrateBindable if self._purpose == "substrate" else self.sourceBindable) and not self.loading

    @Property(str, notify=changed)
    def readiness(self):
        binding = ("仅光学资料；工艺模型未提供，不能作为蒸发源" if self._optical_only else
                   "可作为蒸发膜料选用" if self.sourceBindable else "仅供资料浏览；此角色不能作为蒸发膜料")
        if self._reference_data:
            physics = "仅 ZnS 有界参考场景可计算；不预测本机沉积速率或热场"
        else:
            physics = "不支持材料热场、温度到蒸发量或沉积速率预测"
        return binding + "。" + physics

    @Property(str, notify=changed)
    def propertyCoverage(self):
        from .material_research import research_coverage
        return coverage_label(research_coverage(self._research_data))

    @Property(bool, notify=changed)
    def readOnly(self):
        # Builtins are edited through a separate reversible override layer.
        return False

    @Property(bool, notify=changed)
    def builtin(self):
        return self._builtin

    @Property(bool, notify=changed)
    def builtinOverridden(self):
        return self._builtin and self._builtin_overridden

    @Property(bool, notify=changed)
    def canRestoreBuiltin(self):
        return self._builtin and self._builtin_overridden and not self._dirty and not self.loading

    @Property(bool, notify=changed)
    def canDelete(self):
        # A custom record is deleted from the local library; a builtin is
        # deleted through a durable tombstone so upgrades cannot resurrect it.
        return (not self.loading and
                (self._saved_id is not None or self._builtin))

    @Property(list, notify=changed)
    def deletedBuiltins(self):
        return deepcopy(self._deleted_builtins)

    @Property(str, notify=changed)
    def currentKey(self):
        return self._selected_key

    @Property(bool, notify=changed)
    def hasReferenceData(self):
        return (self._reference_data is not None or self._research_data is not None or
                any(row.get("notes", "").strip() or row.get("source", "").strip() or
                    (row.get("k_data") or {}).get("notes", "").strip() or
                    (row.get("k_data") or {}).get("source", "").strip()
                    for row in self._optical_sets))

    @Property("QVariantMap", notify=changed)
    def referenceData(self):
        return deepcopy(self._reference_data or {})

    @Property("QVariantMap", notify=changed)
    def researchData(self):
        return deepcopy(self._research_data or {})

    @Property(list, notify=changed)
    def referenceSections(self):
        return self._reference_sections_with_optics()

    @Property("QVariantMap", notify=changed)
    def fields(self):
        return dict(self._draft)

    @Property(bool, notify=changed)
    def dirty(self):
        return self._dirty

    @Property(bool, notify=changed)
    def valid(self):
        return self._valid

    @Property(str, notify=changed)
    def error(self):
        return self._error

    @Property(str, notify=changed)
    def nameValidationError(self):
        """Visible, scoped name validation for the material name editor."""
        if not self._valid:
            return self._error
        conflict = self._same_scope_name_conflict(self._draft.get("name", ""))
        if conflict:
            return f"同一材料库已存在同名材料“{conflict}”；请使用不同名称后再保存"
        return ""

    @Property(str, notify=changed)
    def status(self):
        return self._status

    @Property(str, notify=changed)
    def completeness(self):
        try:
            info = materials.material_completeness(self.draftData(include_evidence=False))
        except (ValueError, TypeError, KeyError):
            return "模型参数尚不完整或无效；请修正提示后保存"
        labels = []
        for key, name in (("resistance_model", "电阻"), ("electron_beam_model", "电子束")):
            model = info["models"][key]
            labels.append(name + "：" + ("用户已声明分布标定" if model["calibration_declared"] else "待补充" + "、".join(model["missing"])))
        if not self.sourceBindable:
            return "此角色仅供研究查阅，不能绑定到当前蒸发源；研究物性不自动参与沉积计算。"
        note = ("。ZnS 文献参考计算为独立、限域的假设场景，不是沉积速率标定。" if self._reference_data else
                "。研究物性只供查阅；不提供温度到蒸发量的预测。" if self._research_data else
                "。未提供物性参考记录；不提供温度到蒸发量的预测。")
        return "；".join(labels) + note

    @Property(str, notify=changed)
    def currentId(self):
        return self._draft.get("id", "") if self._builtin else self._saved_id or ""

    @Property(str, notify=changed)
    def directory(self):
        return str(self._folder())

    @Slot()
    def reload(self):
        if self._catalog_future is not None:
            self._catalog_future.cancel()
        self._catalog_future = _submit_material_task(
            _read_catalog, self._folder(), list(self._entries))
        self._timer.start()
        self.changed.emit()

    @Slot()
    def cancelPendingLoad(self, notify=True):
        had_pending = (self._load_future is not None or self._loading or
                       self._open_after_catalog or self._pending is not None or
                       bool(self._optical_curve_requests))
        self._load_token += 1
        self._invalidate_optical_curve_requests()
        if self._load_future is not None:
            self._load_future.cancel()
        self._load_future = None
        self._loading = False
        self._open_after_catalog = False
        self._pending = None
        if notify and had_pending:
            self.changed.emit()

    def _start_library_operation(self, kind, worker, *args, context=None):
        if self.loading or self._operation_busy:
            return False
        self._io_generation += 1
        self._io_context = {"token": self._io_generation, "kind": kind,
                            "context": context}
        self._io_future = _submit_material_task(worker, *args)
        self._operation_busy = True
        self._operation_kind = kind
        self._status = {
            "save": "Saving material…",
            "restore_builtin": "Restoring builtin material…",
            "export_single": "Exporting material…",
            "export_library": "Exporting selected materials…",
            "preview_import": "Reading and validating package…",
            "apply_import": "Applying import decisions…",
            "delete": "Deleting material…",
            "delete_builtin": "Deleting builtin material…",
        }.get(kind, "Working…")
        self._timer.start()
        self.changed.emit()
        return True

    @Slot()
    def cancelImportPreview(self):
        self._import_preview_plan = None
        self._import_preview_rows = []
        self._import_error = ""
        if self._operation_kind == "preview_import" and self._io_future is not None:
            self._io_generation += 1
            self._io_future.cancel()
            self._io_future = None
            self._io_context = None
            self._operation_busy = False
            self._operation_kind = ""
        self.changed.emit()

    def waitForBackgroundTasks(self):
        """Stop this bridge's polling and join its catalog/material workers."""
        self._cancel_background_work()
        wait_for_material_workers()

    def _cancel_background_work(self):
        self.cancelPendingLoad(notify=False)
        if self._catalog_future is not None:
            self._catalog_future.cancel()
            self._catalog_future = None
        if self._operation_kind == "preview_import" and self._io_future is not None:
            self._io_future.cancel()
            self._io_future = None
            self._io_context = None
            self._operation_busy = False
            self._operation_kind = ""
        self._timer.stop()

    def _schedule_load(self, action, row, builtin=False):
        self.cancelPendingLoad(notify=False)
        self._load_token += 1
        self._load_context = (self._load_token, action, builtin)
        self._load_future = _submit_material_task(_prepare_material, row)
        self._loading = True
        self._status = "正在载入材料…"
        self._timer.start()
        self.changed.emit()
        return True

    def _finish_library_operation(self, kind, context, result=None, error=None):
        self._operation_busy = False
        self._operation_kind = ""
        if error is not None:
            self._status = f"{kind.replace('_', ' ').capitalize()} failed: {error}; the material draft is unchanged"
            if kind in ("preview_import", "apply_import"):
                self._import_error = str(error)
            if kind == "preview_import":
                self.importPreviewReady.emit()
            if kind == "apply_import":
                self.importApplyFinished.emit(False)
            self.changed.emit()
            return

        if kind == "save":
            record = result["record"]
            identity = record["id"]
            if context["builtin"]:
                self._builtin_overridden = True
                self._overridden_ids.add(identity)
                self._selected_key = "builtin:" + identity
                self._saved_id = None
                self._status = "Builtin override saved; saved source and film snapshots are unchanged"
            else:
                self._saved_id = identity
                self._selected_key = "user:" + identity
                self._status = "Material saved to the local library; existing snapshots are unchanged"
            self._dirty = False
            if context.get("discard_pending"):
                self.confirmDiscard(True)
            self.reload()
        elif kind == "restore_builtin":
            record, sections = result
            identity = record["id"]
            self._overridden_ids.discard(identity)
            self._builtin_overridden = False
            self._selected_key = "builtin:" + identity
            self._saved_id = None
            self._load_draft(record, sections, prepared=True)
            self._dirty = False
            self._status = "Builtin original restored; saved source and film snapshots are unchanged"
            self.reload()
        elif kind in ("export_single", "export_library"):
            self._status = f"Exported package to {result['path']} ({result['bytes']} bytes)"
        elif kind == "delete":
            self._saved_id = None
            self._selected_key = ""
            self._dirty = True
            self.reload()
            self._status = "Material deleted from the local library; the current draft and saved source/film snapshots are unchanged"
        elif kind == "delete_builtin":
            self._overridden_ids.discard(self.currentId)
            self._saved_id = None
            self._selected_key = ""
            self._builtin = False
            self._builtin_overridden = False
            self._dirty = True
            self.reload()
            self._status = ("Builtin material deleted from the library (durably, until restored); "
                            "existing sources and film snapshots keep their saved copies")
        elif kind == "preview_import":
            self._import_preview_plan = result["plan"]
            self._import_preview_rows = result["rows"]
            self._import_error = ""
            self._status = f"Validated {result['count']} material record(s); review import conflicts"
            self.importPreviewReady.emit()
        elif kind == "apply_import":
            plan = context["plan"]
            decisions = context["decisions"]
            affected = {row["id"].casefold() for row in plan["rows"]
                        if decisions.get(row["id"], "import") != "skip"}
            if self.currentId.casefold() in affected and not self._dirty:
                self._refresh_current_after_catalog = True
            self._import_preview_plan = None
            self._import_preview_rows = []
            self._import_error = ""
            self._status = (f"Import complete: {result['created']} added, "
                            f"{result['copied']} copied, {result['overwritten']} overwritten, "
                            f"{result['skipped']} skipped")
            self.reload()
            self.importApplyFinished.emit(True)
        elif kind == "delete":
            self._saved_id = None
            self._selected_key = ""
            self._dirty = True
            self._status = "Material deleted from the local library; existing snapshots are unchanged"
            self.reload()
        if kind in {"save", "restore_builtin", "apply_import", "delete", "delete_builtin"}:
            self.catalogCommitted.emit()
        self.changed.emit()

    @Slot()
    def _poll_workers(self):
        if self._catalog_future is not None and self._catalog_future.done():
            future, self._catalog_future = self._catalog_future, None
            try:
                catalog_result = future.result()
                self._builtins, self._entries, self._entry_cache, issues = catalog_result[:4]
                self._overridden_ids = set(catalog_result[4]) if len(catalog_result) > 4 else set()
                self._deleted_builtins = list(catalog_result[5]) if len(catalog_result) > 5 else []
                self._status = "；".join(issues) if issues else "材料库已刷新"
            except Exception as exc:
                self._status = f"读取材料库失败：{exc}"
            self.changed.emit()
            if self._open_after_catalog:
                self._open_after_catalog = False
                self.prepareOpen()
            if self._refresh_current_after_catalog:
                self._refresh_current_after_catalog = False
                if self._selected_key and not self._dirty:
                    self._replace("select", self._selected_key)
        if self._load_future is not None and self._load_future.done():
            future, self._load_future = self._load_future, None
            token, action, builtin = self._load_context
            self._loading = False
            try:
                row, sections = future.result()
                if token != self._load_token:
                    return
                self._builtin = builtin
                self._builtin_overridden = builtin and row["id"] in self._overridden_ids
                self._saved_id = row["id"] if not builtin and any(item["id"] == row["id"] for item in self._entries) else None
                self._selected_key = ("builtin:" if builtin else "user:") + row["id"]
                self._load_draft(row, sections, prepared=True)
                self._dirty = action in ("snapshot", "copy")
                if action == "copy":
                    self._selected_key = ""; self._saved_id = None
                self._status = "内置只读材料；复制后可编辑" if builtin else "材料已载入；已有源快照保持不变"
                if action in ("source_select", "substrate_select"):
                    self.materialSelected.emit(row)
                    self._status = "已选用材料到当前基片" if action == "substrate_select" else "已选用材料到当前源"
            except Exception as exc:
                self._status = f"材料载入失败：{exc}；当前内容已保留"
            self.changed.emit()
        for generation, request in list(self._optical_curve_requests.items()):
            future = request["future"]
            if future is not None and not future.done():
                continue
            self._optical_curve_requests.pop(generation, None)
            identity = request["identity"]
            points = request["points"]
            error = request["error"]
            if future is not None:
                try:
                    points = future.result()
                    error = ""
                except Exception as exc:
                    points = []
                    error = str(exc) or "光学曲线采样失败。"
            if self._optical_curve_latest_generations.get(identity) != generation:
                continue
            record = request["record"]
            if record is not None:
                current = next((item for item in self._optical_sets
                                if item["id"] == identity), None)
                if current is not record:
                    continue
                if not error:
                    self._optical_preview_entry(record)[2] = deepcopy(points)
            self.opticalCurveReady.emit(identity, generation, deepcopy(points), error)
        if self._io_future is not None and self._io_future.done():
            future, self._io_future = self._io_future, None
            context, self._io_context = self._io_context, None
            try:
                result = future.result()
            except Exception as exc:
                if context and context["token"] == self._io_generation:
                    self._finish_library_operation(context["kind"], context["context"], error=exc)
            else:
                if context and context["token"] == self._io_generation:
                    self._finish_library_operation(context["kind"], context["context"], result=result)
        if (self._catalog_future is None and self._load_future is None and self._io_future is None
                and not self._optical_curve_requests):
            self._timer.stop()

    @Slot(str, str)
    def setField(self, key, value):
        if self._loading or self._operation_busy or key not in self._draft or key == "id" or self._draft[key] == value:
            return
        self._initial_default = False
        self._draft[key] = value
        self._dirty = True
        self._validate()

    def _replace(self, action, value=None):
        self._initial_default = False
        if self._dirty:
            self._pending = (action, value)
            self.discardRequested.emit()
            return False
        if action == "new":
            self.cancelPendingLoad()
            self._saved_id = None; self._builtin = False; self._selected_key = ""
            self._builtin_overridden = False
            profile = materials.default_material_profile()
            if self._purpose == "substrate":
                profile["category"] = "substrate"
            self._load_draft(profile)
            self._status = "新材料尚未保存；保存后才会写入本地材料库"
            self.changed.emit()
            return True
        if action == "copy":
            row = deepcopy(value)
            row["id"] = uuid.uuid4().hex
            row["name"] = row["name"][:115] + " 副本"
            return self._schedule_load(action, row)
        if action == "snapshot":
            if any(item["id"] == value["id"] for item in self._builtins):
                return self._replace("copy", value)
            return self._schedule_load(action, value)
        prefix, separator, identity = value.partition(":")
        if not separator:
            identity = value
        rows = self._builtins if prefix == "builtin" else self._entries
        key = identity.casefold()
        row = next((row for row in rows if str(row["id"]).casefold() == key), None)
        builtin = prefix == "builtin"
        if row is None and not separator:
            row = next((row for row in self._builtins if str(row["id"]).casefold() == key), None)
            builtin = row is not None
        if row is None:
            self._status = "未找到材料；当前内容已保留"
            self.changed.emit()
            return False
        if action == "source_select" and not materials.source_bindable(row):
            self._status = "蒸发源只能选用膜料"; self.changed.emit(); return False
        if action == "substrate_select" and not materials.substrate_bindable(row):
            self._status = "基片设计只能选用基片材料"; self.changed.emit(); return False
        return self._schedule_load(action, row, builtin)

    def loadSnapshot(self, material):
        return self._replace("snapshot", deepcopy(material))

    @Slot()
    def prepareOpen(self):
        """First-open guidance only; never replace an intentional user draft."""
        if self._catalog_future is not None:
            self._open_after_catalog = True
            return
        if self._initial_default and not self._dirty and not self._saved_id and not self._selected_key and self._builtins:
            eligible = next((row for row in self._builtins if (materials.substrate_bindable(row) if self._purpose == "substrate" else materials.source_bindable(row))), None)
            if eligible:
                self._replace("select", "builtin:" + eligible["id"])
        self._initial_default = False

    @Slot(result=bool)
    def prepareForSource(self):
        self.setRoleFilter("film")
        self.prepareOpen()
        return True

    @Slot(result=bool)
    def prepareForSubstrate(self):
        self.setRoleFilter("substrate")
        self.prepareOpen()
        return True

    @Slot(str, result=bool)
    def selectSubstrateMaterial(self, material_id):
        return self._replace("substrate_select", material_id)

    @Slot(str, result=bool)
    def selectSourceMaterial(self, material_id):
        return self._replace("source_select", material_id)

    @Slot(result=bool)
    def newMaterial(self):
        return self._replace("new")

    @Slot(str, result=bool)
    def selectMaterial(self, material_id):
        if not self._loading and (material_id == self._selected_key or (not self._builtin and material_id == self._saved_id)):
            return True
        return self._replace("select", material_id)

    @Slot(result=bool)
    def copyMaterial(self):
        if self._loading:
            return False
        try:
            value = self.draftData()
        except (ValueError, TypeError, KeyError) as exc:
            self._status = str(exc)
            self.changed.emit()
            return False
        return self._replace("copy", value)

    @Slot(bool)
    def confirmDiscard(self, confirmed):
        pending, self._pending = self._pending, None
        if confirmed and pending:
            self._dirty = False
            self._replace(*pending)

    @Slot(result=bool)
    def save(self):
        """Compatibility save path for source close-confirmation flows."""
        if self.loading:
            return False
        was_builtin = self._builtin
        try:
            record = self.draftData()
            conflict = self._name_conflict_error(record)
            if conflict:
                self._status = conflict
                self.changed.emit()
                return False
            normalized = (
                _library_service(self._folder()).save_builtin_override(record)["record"]
                if was_builtin else
                materials.save_material(
                    self._folder(), record, overwrite=self._saved_id is not None)
            )
        except (ValueError, OSError, TypeError) as exc:
            self._status = f"Material save failed: {exc}; the draft is unchanged"
            self.changed.emit()
            return False
        identity = normalized["id"]
        if was_builtin:
            self._builtin_overridden = True
            self._overridden_ids.add(identity)
            self._saved_id = None
            self._selected_key = "builtin:" + identity
            self._status = "Built-in override saved; saved source and film snapshots are unchanged"
        else:
            self._saved_id = identity
            self._selected_key = "user:" + identity
            self._status = "Material saved to the local library; existing snapshots are unchanged"
        self._load_draft(normalized, self._sections)
        self._dirty = False
        self.reload()
        self.catalogCommitted.emit()
        self.changed.emit()
        return True

    def _scope_rows(self):
        """Records that share this draft's writable scope.

        Ordinary user records are compared only against other user records in
        the same library folder. Builtin overrides keep the seeded names, which
        legitimately repeat across builtin film/substrate variants, so they are
        never treated as duplicates.
        """
        return [] if self._builtin else self._entries

    def _same_scope_name_conflict(self, name):
        key = str(name or "").strip().casefold()
        if not key:
            return ""
        own = (str(self._draft.get("id", "")) if self._builtin
               else (self._saved_id or "")).casefold()
        for row in self._scope_rows():
            if str(row.get("id", "")).casefold() == own:
                continue
            if str(row.get("name", "")).strip().casefold() == key:
                return str(row.get("name", ""))
        return ""

    def _name_conflict_error(self, record):
        conflict = self._same_scope_name_conflict(record.get("name", ""))
        if conflict:
            return f"同一材料库已存在同名材料“{conflict}”；请使用不同名称后再保存"
        return ""

    def _begin_save(self, discard_pending=False):
        if self.loading or not self._valid:
            return False
        try:
            record = self.draftData()
        except (ValueError, TypeError, KeyError) as exc:
            self._status = str(exc)
            self.changed.emit()
            return False
        conflict = self._name_conflict_error(record)
        if conflict:
            self._status = conflict
            self.changed.emit()
            return False
        return self._start_library_operation(
            "save", _save_library_record, self._folder(), record,
            self._builtin, self._saved_id is not None,
            context={"builtin": self._builtin,
                     "discard_pending": discard_pending})

    @Slot(result=bool)
    def saveInBackground(self):
        return self._begin_save(False)

    @Slot(result=bool)
    def saveAndDiscardInBackground(self):
        return self._begin_save(True)

    @Slot(str, str, result=bool)
    def exportMaterial(self, path, identifier):
        if self.loading or not path or not identifier:
            return False
        return self._start_library_operation(
            "export_single", _export_library_material,
            self._folder(), identifier, path)

    @Slot(str, "QVariantList", result=bool)
    def exportLibrary(self, path, identifiers):
        if self.loading or not path or not identifiers:
            return False
        return self._start_library_operation(
            "export_library", _export_library_selection,
            self._folder(), path, list(identifiers))

    @Slot(str, result=bool)
    def previewImport(self, path):
        if self.loading or not path:
            return False
        self._import_preview_plan = None
        self._import_preview_rows = []
        self._import_error = ""
        return self._start_library_operation(
            "preview_import", _preview_library_import,
            self._folder(), path)

    @Slot("QVariantMap", result=bool)
    def applyImport(self, decisions):
        if self.loading or self._import_preview_plan is None:
            return False
        plan = self._import_preview_plan
        choices = dict(decisions or {})
        return self._start_library_operation(
            "apply_import", _apply_library_import,
            self._folder(), plan, choices,
            context={"plan": plan, "decisions": choices})

    @Slot(result=bool)
    def restoreBuiltinInBackground(self):
        if not self.canRestoreBuiltin:
            return False
        return self._start_library_operation(
            "restore_builtin", _restore_library_builtin,
            self._folder(), self.currentId)

    @Slot(result=bool)
    def deleteInBackground(self):
        if not self.canDelete:
            return False
        if self._builtin:
            return self._start_library_operation(
                "delete_builtin", _delete_library_builtin,
                self._folder(), self.currentId)
        return self._start_library_operation(
            "delete", _delete_library_material,
            self._folder(), self._saved_id)

    @Slot(str, result=bool)
    def restoreDeletedBuiltin(self, identifier):
        """Restore one tombstoned builtin to its packaged original definition."""
        if self.loading or self._operation_busy or not identifier:
            return False
        try:
            record, sections = _restore_library_builtin(self._folder(), identifier)
        except (ValueError, TypeError, OSError) as exc:
            self._status = "恢复内置材料失败：" + str(exc)
            self.changed.emit()
            return False
        identity = record["id"]
        self._deleted_builtins = [row for row in self._deleted_builtins
                                  if row.get("id") != identity]
        self._overridden_ids.discard(identity)
        self._builtin = True
        self._builtin_overridden = False
        self._saved_id = None
        self._selected_key = "builtin:" + identity
        self._load_draft(record, sections, prepared=True)
        self._dirty = False
        self._status = "已恢复内置材料原始定义；已有源和膜系快照不变"
        self.reload()
        self.catalogCommitted.emit()
        self.changed.emit()
        return True

    @Slot()
    def requestDelete(self):
        if self.canDelete:
            self.deleteRequested.emit()

    @Slot(bool, result=bool)
    def confirmDelete(self, confirmed):
        if not confirmed or not self.canDelete:
            return False
        try:
            if self._builtin:
                _delete_library_builtin(self._folder(), self.currentId)
            else:
                materials.delete_material(self._folder(), self._saved_id)
        except (OSError, ValueError) as exc:
            self._status = f"删除失败：{exc}；当前编辑内容和已有源快照不变"
            self.changed.emit()
            return False
        was_builtin = self._builtin
        self._saved_id = None
        self._selected_key = ""
        self._builtin = False
        self._builtin_overridden = False
        self._dirty = True
        self.reload()
        self._status = ("已删除内置材料（可恢复）；已有源和膜系快照不变" if was_builtin
                        else "已从材料库删除；当前编辑内容保留，可重新保存。已有源中的快照不受影响")
        self.changed.emit()
        return True

    @Slot(result=bool)
    def useMaterial(self):
        if self.loading:
            return False
        try:
            # Every evidence/optical object entered this bridge through strict
            # worker/load/import validation. They are immutable copy-on-write
            # values: validate only editable scalar/model fields on UI commit.
            # Public draftData() and save() still detach and fully validate.
            material = self.draftData(include_evidence=False)
            if self._reference_data is not None:
                material["reference_data"] = self._reference_data
            if self._research_data is not None:
                material["research_data"] = self._research_data
            if self._optical_sets:
                material["optical_constants"] = self._optical_sets
        except (ValueError, TypeError, KeyError) as exc:
            self._status = f"无法选用材料：{exc}"
            self.changed.emit()
            return False
        if not (materials.substrate_bindable(material) if self._purpose == "substrate" else materials.source_bindable(material)):
            self._status = "无法选用材料，当前角色不能绑定；此用途只能选用" + ("基片材料" if self._purpose == "substrate" else "膜料")
            self.changed.emit()
            return False
        self.materialSelected.emit(material)
        self._status = ("已复制完整材料快照到当前基片" if self._purpose == "substrate" else "已复制完整材料快照到当前源") + ("；材料库的未保存修改仍保留" if self._dirty or (not self._saved_id and not self._builtin) else "")
        self.changed.emit()
        return True
