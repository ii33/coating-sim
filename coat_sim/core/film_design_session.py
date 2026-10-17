"""Qt-independent state and editing operations for a film-design draft.

The session is the sole owner of draft content, dirty/revision state, derived
thickness display values, pending confirmations, and library/file operations.
Adapters may snapshot it and subscribe to committed changes; no Qt type is
imported here.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
from pathlib import Path
import threading
import uuid
from typing import Any, Callable, Mapping

from . import film_design
from .film_design_reference_recipes import build_reference_recipe_preview
from .film_design_templates import build_preview as build_film_design_preview
from .optical_constants import prepare_optical_set


AIR_MATERIAL_ID = "builtin_ideal_air"
AIR_OPTICAL_SET_ID = "ideal_air_n1_k0"
SESSION_CHANGE_CATEGORIES = frozenset(
    {"design", "ui", "derived", "expression", "saved_designs",
     "expression_templates", "confirmation", "workflow_settings"}
)


@dataclass(frozen=True)
class SessionChange:
    revision: int
    categories: frozenset[str]


@dataclass(frozen=True)
class SessionSnapshot:
    revision: int
    design_revision: int
    design: Mapping[str, Any]
    path: str | None
    dirty: bool
    message: str
    error: str
    derived: Mapping[str, Any]
    material_options: Mapping[str, Any]
    expression_text: str
    expression_busy: bool
    expression_operation: str
    pending_confirmation: Mapping[str, Any] | None
    saved_designs: tuple[Mapping[str, Any], ...]
    expression_templates: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class ExpressionTaskRequest:
    token: int
    base_design_revision: int
    operation: str
    payload: Mapping[str, Any]


def _finite_positive(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite positive number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be a finite positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} must be a finite positive number")
    return number


def _range_text(values: object) -> str:
    if not isinstance(values, (list, tuple)) or not values:
        return ""
    finite: list[float] = []
    for value in values:
        if isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(number):
            finite.append(number)
    if not finite:
        return ""
    low, high = min(finite), max(finite)
    return format(low, ".6g") if low == high else f"{low:.6g}–{high:.6g}"


def _preferred_optical_set_id(material: Mapping[str, Any] | None,
                              existing_id: str = "") -> str:
    """Select a still-valid choice, then a declared default, then catalog order."""
    if not material:
        return ""
    available = [str(row.get("id", "")) for row in material.get("optical_sets", [])
                 if str(row.get("id", ""))]
    if existing_id and existing_id in available:
        return existing_id
    for key in ("preferred_optical_set_id", "default_optical_set_id"):
        preferred = str(material.get(key) or "")
        if preferred in available:
            return preferred
    return available[0] if available else ""


class FilmDesignEditSession:
    """One thread-safe, UI-independent film-design editing session.

    `material_options` in snapshots is deliberately a compact picker projection
    without wavelength/n/k sample arrays. Full catalog rows stay private for
    reference-index lookup and are never deep-copied on the snapshot hot path.
    Medium keys use ``incident_medium.<field>`` or ``exit_medium.<field>``.
    Symbol layer row IDs are their current A–Z names; ordinary layer IDs are
    their canonical design IDs.
    """

    def __init__(self, service: Any) -> None:
        self.service = service
        self._lock = threading.RLock()
        self._callbacks: dict[int, Callable[[SessionChange], None]] = {}
        self._next_callback_id = 1
        self._revision = 0
        self._design_revision = 0
        self._next_expression_token = 1
        self._active_expression: ExpressionTaskRequest | None = None
        self._expression_busy = False
        self._expression_operation = ""
        self._path: str | None = None
        self._dirty = False
        self._message = ""
        self._error = ""
        self._pending_confirmation: dict[str, Any] | None = None
        self._saved_designs: list[dict[str, Any]] = []
        self._expression_templates: list[dict[str, Any]] = []
        self._prepared_optical_cache: dict[tuple[str, str], Any] = {}
        self._reference_index_cache: dict[tuple[str, str, float], float | Exception] = {}
        self._generated_undo_state: dict[str, Any] | None = None
        self._generated_preview_source: tuple[dict[str, Any], int, Any] | None = None

        material_options_many = getattr(service, "material_options_many", None)
        if callable(material_options_many):
            options = material_options_many(("film", "substrate", "medium"))
            self._catalog_options = {
                purpose: options.get(purpose, [])
                for purpose in ("film", "substrate", "medium")
            }
        else:
            self._catalog_options = {
                purpose: service.material_options(purpose=purpose)
                for purpose in ("film", "substrate", "medium")
            }
        self._material_options = self._make_picker_options(self._catalog_options)
        self._design = self._default_design()
        self._derived: dict[str, dict[str, dict[str, Any]]] = {
            "front": {}, "back": {}, "symbol": {}
        }
        self._refresh_derived()

    @staticmethod
    def _picker_optical_set(optical: Mapping[str, Any]) -> dict[str, Any]:
        ranges = []
        n_range, k_range = _range_text(optical.get("n")), _range_text(optical.get("k"))
        if n_range:
            ranges.append("n " + n_range)
        if k_range:
            ranges.append("k " + k_range)
        return {
            "id": str(optical.get("id", "")),
            "name": str(optical.get("name") or optical.get("id") or ""),
            "kind": str(optical.get("kind", "")),
            "wavelength_min": optical.get("wavelength_min"),
            "wavelength_max": optical.get("wavelength_max"),
            "wavelength_unit": str(optical.get("wavelength_unit", "nm")),
            "value_range_text": "; ".join(ranges),
            "notes": str(optical.get("notes", "")),
        }

    @classmethod
    def _make_picker_options(cls, by_purpose: Mapping[str, list[dict]]) -> dict[str, list[dict]]:
        result: dict[str, list[dict]] = {}
        for purpose in ("film", "substrate", "medium"):
            rows = []
            for material in by_purpose.get(purpose, []):
                rows.append({
                    "id": str(material.get("id", "")),
                    "name": str(material.get("name") or material.get("id") or ""),
                    "design_ready": material.get("design_ready"),
                    "readonly": bool(material.get("readonly", False)),
                    "preferred_optical_set_id": str(material.get("preferred_optical_set_id") or ""),
                    "default_optical_set_id": str(material.get("default_optical_set_id") or ""),
                    "optical_sets": [cls._picker_optical_set(item)
                                     for item in material.get("optical_sets", [])
                                     if str(item.get("id", ""))],
                })
            result[purpose] = rows
        return result

    def _default_design(self) -> dict[str, Any]:
        design = self.service.new_design()
        substrate = next((row for row in self._catalog_options["substrate"]
                          if row.get("optical_sets")), None)
        if substrate:
            optical = substrate["optical_sets"][0]
            design["substrate"].update(material_id=substrate["id"],
                                       optical_set_id=optical["id"])
        self._bind_default_air(design)
        design["expression"]["symbols"] = {
            "H": self._default_symbol_record(), "L": self._default_symbol_record()
        }
        return design

    def _bind_default_air(self, design: dict[str, Any]) -> None:
        """Bind only a fresh draft to the catalog-owned ideal Air dataset."""
        air = next((row for row in self._catalog_options["medium"]
                    if row.get("id") == AIR_MATERIAL_ID), None)
        air_set = next((row for row in (air or {}).get("optical_sets", [])
                        if row.get("id") == AIR_OPTICAL_SET_ID), None)
        if air_set:
            medium = {"kind": "material", "material_id": AIR_MATERIAL_ID,
                      "optical_set_id": AIR_OPTICAL_SET_ID}
            design["incident_medium"] = dict(medium)
            design["exit_medium"] = dict(medium)

    @staticmethod
    def _default_symbol_record() -> dict[str, Any]:
        return {"material_id": "", "optical_set_id": "",
                "thickness": {"value": 1.0, "unit": "qw"}, "note": ""}

    def snapshot(self) -> SessionSnapshot:
        with self._lock:
            return SessionSnapshot(
                revision=self._revision,
                design_revision=self._design_revision,
                design=deepcopy(self._design),
                path=self._path,
                dirty=self._dirty,
                message=self._message,
                error=self._error,
                derived=deepcopy(self._derived),
                material_options=deepcopy(self._material_options),
                expression_text=str(self._design.get("expression", {}).get("text", "")),
                expression_busy=self._expression_busy,
                expression_operation=self._expression_operation,
                pending_confirmation=deepcopy(self._pending_confirmation),
                saved_designs=tuple(deepcopy(self._saved_designs)),
                expression_templates=tuple(deepcopy(self._expression_templates)),
            )

    def workflow_configuration(self, scope: str) -> dict[str, Any]:
        if scope not in ("simulation", "optimization"):
            raise ValueError("unknown workflow configuration scope")
        with self._lock:
            return deepcopy(self._design.get("workflow_settings", {}).get(scope, {}))

    def set_workflow_configuration(self, scope: str, configuration: Mapping[str, Any]) -> bool:
        """Commit page settings without changing the physical design revision."""
        with self._lock:
            try:
                normalized = film_design.validate_workflow_settings({scope: configuration})[scope]
            except (ValueError, TypeError) as exc:
                self._set_error(str(exc))
                return False
            existing = self._design.get("workflow_settings", {})
            if existing.get(scope) == normalized:
                return True
            self._design["workflow_settings"] = {**existing, scope: normalized}
            self._dirty = True
            self._error = ""
            self._notify("workflow_settings", "ui")
            return True

    def _preserve_workflow_configuration(self, candidate: Mapping[str, Any]) -> dict[str, Any]:
        result = deepcopy(dict(candidate))
        if "workflow_settings" in self._design:
            result["workflow_settings"] = deepcopy(self._design["workflow_settings"])
        else:
            result.pop("workflow_settings", None)
        return result

    def _generation_material_options(self) -> Mapping[str, Any]:
        return self._catalog_options

    def _generation_source(self) -> Any:
        return getattr(self.service, "materials", None)

    def _capture_generated_context(self) -> Any:
        return None

    def _prepare_generated_context(self, design: Mapping[str, Any]) -> Any:
        return None

    def _prepare_generated_undo_context(self, design: Mapping[str, Any], context: Any) -> Any:
        return context

    def _restore_generated_context(self, context: Any) -> None:
        pass

    def preview_generated_design(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        """Build an immutable candidate from the current design and catalog.

        This is read-only. The lock ensures the returned revision and candidate
        describe the same session design while the selected material records
        are evaluated.
        """
        return self._preview_generated_design(inputs, build_film_design_preview)

    def preview_reference_design(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        """Bind a reference recipe to the same candidate/source guard as templates."""
        return self._preview_generated_design(
            inputs, build_reference_recipe_preview, use_service_materials=True)

    def _preview_generated_design(self, inputs: Mapping[str, Any],
                                  builder: Callable[..., dict[str, Any]], *,
                                  use_service_materials: bool = False) -> dict[str, Any]:
        with self._lock:
            # A failed or superseding preview cannot leave an older candidate
            # eligible for application through the core API.
            self._generated_preview_source = None
            if self._pending_confirmation is not None:
                raise ValueError("存在待确认的替换或关闭操作，完成或取消后再生成预览。")
            if self._expression_busy:
                raise ValueError("表达式操作仍在进行，完成后再生成参数化设计。")
            material_source = self._generation_source()
            options = (self.service.material_options_many(("film", "substrate", "medium"))
                       if use_service_materials else self._generation_material_options())
            preview = builder(
                inputs=inputs,
                base_design=self._design,
                material_options=options,
            )
            if material_source is not self._generation_source():
                raise ValueError("材料目录已改变，请重新生成预览。")
            self._generated_preview_source = (
                deepcopy(preview["candidate_design"]), self._design_revision,
                material_source)
            preview["base_design_revision"] = self._design_revision
            return preview

    def apply_generated_design(self, candidate_design: Mapping[str, Any],
                               base_design_revision: int) -> bool:
        """Atomically apply a generated candidate if its source is still current."""
        with self._lock:
            if self._pending_confirmation is not None or self._expression_busy:
                return False
            if (type(base_design_revision) is not int
                    or base_design_revision != self._design_revision
                    or not isinstance(candidate_design, Mapping)):
                return False
            source = self._generated_preview_source
            if (source is None or source[1] != base_design_revision
                    or source[0] != candidate_design
                    or source[2] is not self._generation_source()):
                return False
            try:
                replacement = deepcopy(dict(candidate_design))
                # Apply replaces the current document contents. Its stable
                # identity/path stay owned by this edit session; explicit New
                # or Save Copy is the separate creation operation.
                replacement["id"] = self._design["id"]
                normalized = self.service.validate(replacement)
                normalized = self._preserve_workflow_configuration(normalized)
                generated_context = self._prepare_generated_context(normalized)
            except Exception:
                return False
            if not isinstance(normalized, Mapping):
                return False
            previous = {
                "design": deepcopy(self._design),
                "dirty": self._dirty,
                "path": self._path,
                "message": self._message,
                "error": self._error,
                "generated_context": self._capture_generated_context(),
            }
            self._restore_generated_context(generated_context)
            self._design = dict(normalized)
            self._design_revision += 1
            self._dirty = True
            self._message = "参数化设计已应用；可撤销本次生成。"
            self._error = ""
            self._refresh_derived()
            self._generated_undo_state = {
                "applied_revision": self._design_revision,
                **previous,
            }
            self._notify("design", "ui", "derived", "expression")
            return True

    def undo_generated_design(self, expected_design_revision: int) -> bool:
        """Restore the design replaced by the latest generator application once."""
        with self._lock:
            state = self._generated_undo_state
            if (state is None or self._pending_confirmation is not None
                    or self._expression_busy or type(expected_design_revision) is not int
                    or expected_design_revision != self._design_revision
                    or state.get("applied_revision") != expected_design_revision):
                return False
            configuration_changed = self._design.get("workflow_settings", {}) != state["design"].get("workflow_settings", {})
            restored_design = self._preserve_workflow_configuration(state["design"])
            try:
                restored_context = self._prepare_generated_undo_context(
                    restored_design, state.get("generated_context"))
            except (ValueError, TypeError, OSError) as exc:
                self._set_error(str(exc))
                return False
            self._restore_generated_context(restored_context)
            self._design = restored_design
            self._dirty = bool(state["dirty"]) or configuration_changed
            self._path = state["path"]
            self._message = str(state["message"])
            self._error = str(state["error"])
            self._design_revision += 1
            self._generated_undo_state = None
            self._refresh_derived()
            self._notify("design", "ui", "derived", "expression")
            return True

    def can_undo_generated_design(self, expected_design_revision: int) -> bool:
        with self._lock:
            state = self._generated_undo_state
            return bool(state is not None and self._pending_confirmation is None
                        and not self._expression_busy
                        and type(expected_design_revision) is int
                        and expected_design_revision == self._design_revision
                        and state.get("applied_revision") == expected_design_revision)

    def apply_optimized_design(self, candidate_design: Mapping[str, Any],
                               base_design_revision: int) -> bool:
        """Atomically replace the draft if an optimization's source is current.

        Optimizer output is a full candidate design, but the edit session stays
        the sole owner of the draft and revision check.  A stale result returns
        ``False`` without changing session state.  A valid result is one dirty
        draft edit and retains the current path so the existing save/overwrite
        flow remains authoritative.
        """
        with self._lock:
            if (type(base_design_revision) is not int
                    or base_design_revision != self._design_revision):
                return False
            if not isinstance(candidate_design, Mapping):
                self._set_error("optimized candidate must be a design mapping")
                return False
            try:
                normalized = self.service.validate(deepcopy(dict(candidate_design)))
            except Exception as exc:
                self._set_error(str(exc))
                return False
            if not isinstance(normalized, Mapping):
                self._set_error("optimized candidate validation returned no design")
                return False

            # Commit one complete validated value while holding the same lock
            # used by every draft mutation and by snapshot().
            self._generated_undo_state = None
            self._design = self._preserve_workflow_configuration(normalized)
            self._design_revision += 1
            self._dirty = True
            self._error = ""
            self._refresh_derived()
            categories = {"design", "ui", "derived"}
            expression = self._design.get("expression", {})
            if expression.get("text"):
                categories.add("expression")
            self._notify(*categories)
            return True

    def subscribe(self, callback: Callable[[SessionChange], None]) -> Callable[[], None]:
        with self._lock:
            identity = self._next_callback_id
            self._next_callback_id += 1
            self._callbacks[identity] = callback

        def unsubscribe() -> None:
            with self._lock:
                self._callbacks.pop(identity, None)

        return unsubscribe

    def _notify(self, *categories: str) -> None:
        if not categories:
            return
        self._revision += 1
        change = SessionChange(self._revision, frozenset(categories))
        callbacks = tuple(self._callbacks.values())
        for callback in callbacks:
            try:
                callback(change)
            except Exception:
                # A UI observer must not roll back a committed edit.
                continue

    def _set_error(self, message: str) -> None:
        message = str(message)
        if self._error == message:
            return
        self._error = message
        self._notify("ui")

    def clear_error(self) -> None:
        with self._lock:
            changed = bool(self._error)
            self._error = ""
            if self._pending_confirmation and self._pending_confirmation.get("kind") == "overwrite":
                self._pending_confirmation = None
                changed = True
                self._notify("confirmation", "ui")
            elif changed:
                self._notify("ui")

    def mark_editing_dirty(self) -> None:
        with self._lock:
            self._generated_undo_state = None
            if self._dirty:
                return
            self._dirty = True
            self._notify("ui")

    def _mark_dirty(self, *, invalidate_expression: bool = True) -> None:
        self._generated_undo_state = None
        self._dirty = True
        self._design_revision += 1
        self._error = ""
        categories = {"design", "ui"}
        expression = self._design.get("expression", {})
        if invalidate_expression and expression.get("text") and expression.get("in_sync"):
            expression["in_sync"] = False
            categories.add("expression")
        self._notify(*categories)

    def _material_record(self, purpose: str, material_id: str) -> dict[str, Any] | None:
        return next((row for row in self._catalog_options[purpose]
                     if str(row.get("id", "")) == str(material_id or "")), None)

    def update_material_options(self, purpose: str, rows: list[dict[str, Any]]) -> None:
        """Replace one service catalog projection and refresh derived values."""
        if purpose not in ("film", "substrate", "medium"):
            raise ValueError("unknown material purpose")
        with self._lock:
            self._generated_preview_source = None
            self._catalog_options[purpose] = list(rows)
            self._material_options = self._make_picker_options(self._catalog_options)
            self._prepared_optical_cache.clear()
            self._reference_index_cache.clear()
            self._refresh_derived()
            self._notify("derived", "ui")

    def _reference_index(self, material_id: str, optical_set_id: str,
                         reference: float) -> float:
        key = (str(material_id), str(optical_set_id), float(reference))
        cached = self._reference_index_cache.get(key)
        if isinstance(cached, Exception):
            raise ValueError(str(cached))
        if cached is not None:
            return cached
        material = self._material_record("film", material_id)
        if material is None:
            raise ValueError("无法计算 QW：请选择有效膜层材料。")
        optical = next((row for row in material.get("optical_sets", [])
                        if str(row.get("id", "")) == str(optical_set_id)), None)
        if optical is None:
            raise ValueError("无法计算 QW：请选择有效光学数据集。")
        try:
            prepared_key = (str(material_id), str(optical_set_id))
            evaluator = self._prepared_optical_cache.get(prepared_key)
            if evaluator is None:
                evaluator = prepare_optical_set(optical)
                self._prepared_optical_cache[prepared_key] = evaluator
            raw_n, _k, _details = evaluator(reference, "nm", include_k=False)
            index = float(raw_n)
        except (ValueError, TypeError, AttributeError, KeyError,
                IndexError, OverflowError) as exc:
            error = ValueError(f"无法读取 n({reference:g} nm)：{exc}")
            self._cache_reference_index(key, error)
            raise error from exc
        if not math.isfinite(index) or index <= 0:
            error = ValueError(f"n({reference:g} nm) 无效，不能用于 QW。")
            self._cache_reference_index(key, error)
            raise error
        self._cache_reference_index(key, index)
        return index

    def _cache_reference_index(self, key: tuple[str, str, float],
                               value: float | Exception) -> None:
        if len(self._reference_index_cache) >= 256 and key not in self._reference_index_cache:
            self._reference_index_cache.pop(next(iter(self._reference_index_cache)))
        self._reference_index_cache[key] = value

    def _derive_record(self, record: Mapping[str, Any], reference: float,
                       index_cache: dict[tuple[str, str, float], float | Exception]) -> dict[str, Any]:
        thickness = record.get("thickness", {})
        value = thickness.get("value", 100.0)
        unit = str(thickness.get("unit", "nm"))
        physical: float | None = None
        qw: float | None = float(value) if unit == "qw" else None
        reason = ""
        try:
            layer = {"thickness": {"value": value, "unit": unit}}
            if unit != "qw":
                physical = film_design.physical_thickness_nm(layer, reference, None)
            key = (str(record.get("material_id", "")),
                   str(record.get("optical_set_id", "")), reference)
            if key not in index_cache:
                try:
                    index_cache[key] = self._reference_index(*key)
                except (ValueError, TypeError, AttributeError, KeyError,
                        IndexError, OverflowError) as exc:
                    index_cache[key] = exc
            index = index_cache[key]
            if isinstance(index, Exception):
                raise ValueError(str(index)) from index
            if unit == "qw":
                physical = film_design.physical_thickness_nm(layer, reference, index)
            else:
                qw = 4.0 * index * physical / reference
            if physical is None or not math.isfinite(physical) or physical <= 0:
                raise ValueError("\u7269\u7406\u539a\u5ea6\u4e0d\u80fd\u7528\u4e8e\u663e\u793a\u7ed3\u6784\u56fe\u3002")
            if qw is None or not math.isfinite(qw) or qw <= 0:
                raise ValueError("\u5149\u5b66\u539a\u5ea6\u4e0d\u80fd\u7528\u4e8e\u663e\u793a\u7ed3\u6784\u56fe\u3002")
        except (ValueError, TypeError, AttributeError, KeyError,
                StopIteration, ZeroDivisionError, OverflowError) as exc:
            reason = str(exc)
        return {
            "physicalNmValue": physical,
            "physicalNmText": "n\u4e0d\u53ef\u7528" if physical is None else format(physical, ".3f"),
            "qwText": "n\u4e0d\u53ef\u7528" if qw is None else format(qw, ".12g"),
            "derivedReason": reason,
        }

    def _refresh_derived(self, only: tuple[str, str] | None = None) -> None:
        reference = float(self._design.get("reference_wavelength_nm", 550.0))
        if only is not None:
            side, row_id = only
            if side in ("front", "back"):
                record = next((row for row in self._design.get(side + "_layers", [])
                               if str(row.get("id", "")) == row_id), None)
            elif side == "symbol":
                record = self._design.get("expression", {}).get("symbols", {}).get(row_id)
            else:
                return
            updated = {name: dict(rows) for name, rows in self._derived.items()}
            updated.setdefault(side, {})
            if record is None:
                updated[side].pop(row_id, None)
            else:
                updated[side][row_id] = self._derive_record(record, reference, {})
            self._derived = updated
            return

        result: dict[str, dict[str, dict[str, Any]]] = {"front": {}, "back": {}, "symbol": {}}
        targets = (
            ("front", self._design.get("front_layers", [])),
            ("back", self._design.get("back_layers", [])),
            ("symbol", self._design.get("expression", {}).get("symbols", {})),
        )
        index_cache: dict[tuple[str, str, float], float | Exception] = {}
        for side, source_rows in targets:
            entries = source_rows.items() if side == "symbol" else (
                (str(row.get("id", "")), row) for row in source_rows
            )
            for row_id, record in entries:
                result[side][str(row_id)] = self._derive_record(record, reference, index_cache)
        self._derived = result

    def set_design_value(self, key: str, value: Any) -> bool:
        with self._lock:
            try:
                if key not in {"name", "summary", "note", "reference_wavelength_nm", "use_k", "assume_lossless", "first_surface"}:
                    raise ValueError("不支持的膜系字段：" + str(key))
                if key == "reference_wavelength_nm":
                    value = _finite_positive(value, "reference wavelength")
                elif key in ("use_k", "assume_lossless"):
                    if not isinstance(value, bool):
                        raise ValueError(key + " must be boolean")
                elif key == "first_surface":
                    if value not in ("front", "back"):
                        raise ValueError("first_surface must be front or back")
                elif not isinstance(value, str):
                    value = str(value)
                if self._design.get(key) == value:
                    return True
                self._design[key] = value
                categories = {"derived"} if key == "reference_wavelength_nm" else set()
                if key == "reference_wavelength_nm":
                    self._refresh_derived()
                self._mark_dirty_with_categories(categories)
                return True
            except (ValueError, TypeError, OverflowError) as exc:
                self._set_error(str(exc))
                return False

    def _mark_dirty_with_categories(self, extra: set[str] | None = None,
                                    *, invalidate_expression: bool = True) -> None:
        self._generated_undo_state = None
        self._dirty = True
        self._design_revision += 1
        self._error = ""
        categories = {"design", "ui"}
        categories.update(extra or ())
        expression = self._design.get("expression", {})
        if invalidate_expression and expression.get("text") and expression.get("in_sync"):
            expression["in_sync"] = False
            categories.add("expression")
        self._notify(*categories)

    def set_substrate_value(self, key: str, value: Any) -> bool:
        with self._lock:
            substrate = self._design["substrate"]
            before = deepcopy(substrate)
            try:
                if key == "material_id":
                    material_id = str(value or "")
                    material = self._material_record("substrate", material_id)
                    substrate["material_id"] = material_id
                    substrate["optical_set_id"] = _preferred_optical_set_id(
                        material, str(substrate.get("optical_set_id", "")))
                elif key == "optical_set_id":
                    substrate["optical_set_id"] = str(value or "")
                elif key == "thickness.value":
                    substrate["thickness"]["value"] = _finite_positive(value, "substrate thickness")
                elif key == "thickness.unit":
                    if value not in ("nm", "um", "mm"):
                        raise ValueError("substrate thickness unit must be nm, um, or mm")
                    substrate["thickness"]["unit"] = str(value)
                elif key == "sides":
                    if value not in ("single", "double"):
                        raise ValueError("substrate sides must be single or double")
                    substrate["sides"] = str(value)
                else:
                    raise ValueError("unsupported substrate field: " + str(key))
                if substrate != before:
                    substrate["mode"] = "auto"
                    self._mark_dirty()
                return True
            except (ValueError, TypeError, OverflowError) as exc:
                self._set_error(str(exc))
                return False

    def set_medium_value(self, key: str, value: Any) -> bool:
        with self._lock:
            try:
                medium, separator, field = str(key).partition(".")
                if not separator or medium not in ("incident_medium", "exit_medium"):
                    raise ValueError("未知的介质字段")
                record = self._design[medium]
                before = deepcopy(record)
                if field == "kind":
                    if value not in ("constant", "material"):
                        raise ValueError("medium kind must be constant or material")
                    if value == "constant":
                        record.clear()
                        record.update(kind="constant", n=1.0)
                    else:
                        record.clear()
                        record.update(kind="material", material_id="", optical_set_id="")
                elif field == "n" and record.get("kind") == "constant":
                    record["n"] = _finite_positive(value, "medium n")
                elif field == "material_id":
                    material_id = str(value or "")
                    material = self._material_record("medium", material_id)
                    previous = str(record.get("optical_set_id", ""))
                    record["kind"] = "material"
                    record.pop("n", None)
                    record.pop("k", None)
                    record["material_id"] = material_id
                    record["optical_set_id"] = _preferred_optical_set_id(material, previous)
                elif field == "optical_set_id" and record.get("kind") == "material":
                    record["optical_set_id"] = str(value or "")
                else:
                    raise ValueError("unsupported medium field: " + field)
                if record != before:
                    self._mark_dirty()
                return True
            except (ValueError, TypeError, OverflowError) as exc:
                self._set_error(str(exc))
                return False

    def _rows(self, side: str) -> list[dict[str, Any]]:
        if side in ("front", "back"):
            return self._design[side + "_layers"]
        if side == "symbol":
            return self._design["expression"]["symbols"]
        raise ValueError("side must be front, back, or symbol")

    def _layer_record(self, side: str, row_id: str) -> dict[str, Any]:
        rows = self._rows(side)
        if side == "symbol":
            if row_id not in rows:
                raise ValueError("symbol row is no longer available")
            return rows[row_id]
        record = next((row for row in rows if str(row.get("id", "")) == row_id), None)
        if record is None:
            raise ValueError("layer row is no longer available")
        return record

    def set_layer_value(self, side: str, row_id: str, key: str, value: Any) -> bool:
        with self._lock:
            try:
                records = self._rows(side)
                record = self._layer_record(side, row_id)
                if side == "symbol" and key == "symbol":
                    symbol = str(value).strip()
                    if len(symbol) == 1 and "a" <= symbol <= "z":
                        symbol = symbol.upper()
                    if len(symbol) != 1 or not "A" <= symbol <= "Z":
                        raise ValueError("expression symbols must be a single letter A-Z")
                    if symbol != row_id and symbol in records:
                        raise ValueError("expression symbol already exists: " + symbol)
                    if symbol != row_id:
                        items = list(records.items())
                        records.clear()
                        for old_name, old_record in items:
                            records[symbol if old_name == row_id else old_name] = old_record
                elif key in ("thickness.qw", "thickness.nm"):
                    self._set_display_thickness(record, key, value)
                elif key == "material_id":
                    material_id = str(value or "")
                    material = self._material_record("film", material_id)
                    record["material_id"] = material_id
                    record["optical_set_id"] = _preferred_optical_set_id(
                        material, str(record.get("optical_set_id", "")))
                elif key == "optical_set_id":
                    record["optical_set_id"] = str(value or "")
                elif key == "thickness.value":
                    record["thickness"]["value"] = _finite_positive(value, "thickness")
                elif key == "thickness.unit":
                    if value not in ("nm", "um", "qw"):
                        raise ValueError("layer thickness unit must be nm, um, or qw")
                    record["thickness"]["unit"] = str(value)
                elif key == "note":
                    record["note"] = str(value)
                elif key != "symbol":
                    raise ValueError("unsupported layer field: " + str(key))
                if side == "symbol" and key == "symbol":
                    self._refresh_derived()
                else:
                    self._refresh_derived(only=(side, row_id))
                self._mark_dirty()
                return True
            except (ValueError, TypeError, OverflowError) as exc:
                self._set_error(str(exc))
                return False

    def _set_display_thickness(self, record: dict[str, Any], key: str, value: Any) -> None:
        reference = float(self._design.get("reference_wavelength_nm", 550.0))
        n = self._reference_index(str(record.get("material_id", "")),
                                  str(record.get("optical_set_id", "")), reference)
        if key == "thickness.qw":
            source = _finite_positive(value, "optical thickness QW")
            physical = film_design.physical_thickness_nm(
                {"thickness": {"value": source, "unit": "qw"}}, reference, n)
            if not math.isfinite(physical) or physical <= 0:
                raise ValueError("QW 换算得到的物理厚度不能用于示意图。")
            unit = "qw"
        else:
            source = _finite_positive(value, "physical thickness nm")
            unit = "nm"
        record["thickness"] = {"value": source, "unit": unit}

    def add_layer(self, side: str) -> bool:
        with self._lock:
            if side not in ("front", "back"):
                self._set_error("side must be front or back")
                return False
            record = {"id": uuid.uuid4().hex, "material_id": "",
                      "optical_set_id": "",
                      "thickness": {"value": 100.0, "unit": "nm"}, "note": ""}
            self._rows(side).append(record)
            self._refresh_derived(only=(side, str(record["id"])))
            self._mark_dirty()
            return True

    def insert_layer_after(self, side: str, row_id: str) -> bool:
        with self._lock:
            if side not in ("front", "back"):
                self._set_error("side must be front or back")
                return False
            rows = self._rows(side)
            position = next((i + 1 for i, row in enumerate(rows)
                             if str(row.get("id", "")) == row_id), -1)
            if position <= 0:
                return False
            record = {"id": uuid.uuid4().hex, "material_id": "",
                      "optical_set_id": "",
                      "thickness": {"value": 100.0, "unit": "nm"}, "note": ""}
            rows.insert(position, record)
            self._refresh_derived(only=(side, str(record["id"])))
            self._mark_dirty()
            return True

    def delete_layer(self, side: str, row_id: str) -> bool:
        with self._lock:
            if side not in ("front", "back"):
                return False
            rows = self._rows(side)
            index = next((i for i, row in enumerate(rows)
                          if str(row.get("id", "")) == row_id), -1)
            if index < 0:
                return False
            rows.pop(index)
            self._refresh_derived(only=(side, row_id))
            self._mark_dirty()
            return True

    def move_layer(self, side: str, row_id: str, delta: int) -> bool:
        with self._lock:
            if side not in ("front", "back") or delta not in (-1, 1):
                return False
            rows = self._rows(side)
            source = next((i for i, row in enumerate(rows)
                           if str(row.get("id", "")) == row_id), -1)
            target = source + delta
            if source < 0 or not 0 <= target < len(rows):
                return False
            row = rows.pop(source)
            rows.insert(target, row)
            self._mark_dirty()
            return True

    def move_layer_to_index(self, side: str, row_id: str,
                            insertion_index: int) -> bool:
        """Move a layer to an insertion gap as one session edit.

        ``insertion_index`` addresses the gaps before, between, and after the
        current rows, so its accepted range is ``0..len(rows)``. Removing a
        row before its destination shifts the final row index left by one.
        """
        with self._lock:
            if side not in ("front", "back") or type(insertion_index) is not int:
                return False
            rows = self._rows(side)
            source = next((i for i, row in enumerate(rows)
                           if str(row.get("id", "")) == row_id), -1)
            if source < 0 or not 0 <= insertion_index <= len(rows):
                return False
            target = insertion_index - 1 if insertion_index > source else insertion_index
            if target == source:
                return True
            row = rows.pop(source)
            rows.insert(target, row)
            self._mark_dirty()
            return True

    def add_symbol(self) -> bool:
        with self._lock:
            symbols = self._design["expression"]["symbols"]
            symbol = next((letter for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if letter not in symbols), None)
            if symbol is None:
                self._set_error("最多支持 26 个 A–Z 符号")
                return False
            symbols[symbol] = self._default_symbol_record()
            self._refresh_derived(only=("symbol", symbol))
            self._mark_dirty()
            return True

    def delete_symbol(self, name: str) -> bool:
        with self._lock:
            symbols = self._design["expression"]["symbols"]
            if name not in symbols:
                return False
            symbols.pop(name)
            self._refresh_derived(only=("symbol", name))
            self._mark_dirty()
            return True

    def set_expression_text(self, value: str) -> bool:
        with self._lock:
            if not isinstance(value, str) or len(value) > 20000:
                self._set_error("表达式长度不能超过 20000 个字符")
                return False
            expression = self._design["expression"]
            if expression.get("text", "") == value:
                return True
            expression["text"] = value
            expression["in_sync"] = False
            self._dirty = True
            self._design_revision += 1
            self._error = ""
            self._notify("design", "ui", "expression")
            return True

    def normalize_expression(self) -> ExpressionTaskRequest | None:
        """Normalize only ASCII grammar immediately, preserving old focus-out behavior."""
        with self._lock:
            expression = self._design["expression"]
            raw = str(expression.get("text", ""))
            normalized = raw.translate(str.maketrans(
                "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
            if normalized == raw:
                return None
            expression["text"] = normalized
            expression["in_sync"] = False
            self._dirty = True
            self._design_revision += 1
            self._error = ""
            self._notify("design", "ui", "expression")
            return None

    def expand_expression(self, side: str = "front") -> ExpressionTaskRequest | None:
        with self._lock:
            if side not in ("front", "back"):
                self._set_error("请选择要展开到哪一面膜堆。")
                return None
            if self._expression_busy:
                return None
            expression = self._design.get("expression", {})
            request = ExpressionTaskRequest(
                token=self._next_expression_token,
                base_design_revision=self._design_revision,
                operation="expand",
                payload={"side": side, "text": expression.get("text", ""),
                         "symbols": deepcopy(expression.get("symbols", {})),
                         "design": deepcopy(self._design)},
            )
            self._next_expression_token += 1
            self._active_expression = request
            self._expression_busy = True
            self._expression_operation = "正在展开膜堆表达式…"
            self._notify("ui")
            return request

    def _begin_expression_task(self, operation: str, payload: Mapping[str, Any],
                               label: str) -> ExpressionTaskRequest | None:
        if self._expression_busy:
            return None
        request = ExpressionTaskRequest(
            token=self._next_expression_token,
            base_design_revision=self._design_revision,
            operation=operation,
            payload=deepcopy(dict(payload)),
        )
        self._next_expression_token += 1
        self._active_expression = request
        self._expression_busy = True
        self._expression_operation = label
        self._notify("ui")
        return request

    def request_refresh_expression_templates(self) -> ExpressionTaskRequest | None:
        with self._lock:
            return self._begin_expression_task(
                "refresh_templates", {}, "\u6b63\u5728\u52a0\u8f7d\u8868\u8fbe\u5f0f\u6a21\u677f..."
            )

    def request_save_expression_template(self, name: str, note: str = "") -> ExpressionTaskRequest | None:
        with self._lock:
            self.normalize_expression()
            expression = self._design["expression"]
            template = {
                "schema_version": 1,
                "template_id": uuid.uuid4().hex,
                "name": str(name),
                "note": str(note),
                "expression": {"text": expression["text"],
                               "symbols": deepcopy(expression["symbols"])},
            }
            return self._begin_expression_task(
                "save_template", {"template": template},
                "\u6b63\u5728\u4fdd\u5b58\u8868\u8fbe\u5f0f\u6a21\u677f...",
            )

    def request_apply_expression_template(self, template_id: str) -> ExpressionTaskRequest | None:
        with self._lock:
            return self._begin_expression_task(
                "apply_template", {"template_id": str(template_id)},
                "\u6b63\u5728\u5e94\u7528\u8868\u8fbe\u5f0f\u6a21\u677f...",
            )

    def execute_expression_task(self, request: ExpressionTaskRequest) -> Any:
        """Run immutable, CPU/service work for the Qt adapter's worker thread."""
        payload = request.payload
        if request.operation == "expand":
            design = deepcopy(payload["design"])
            side = str(payload["side"])
            if side not in ("front", "back"):
                raise ValueError("side must be front or back")
            layers = self.service.expand(payload["text"], payload["symbols"])
            design[side + "_layers"] = layers
            design["expression"]["in_sync"] = True
            return self.service.validate(design)
        if request.operation == "refresh_templates":
            return self.service.list_expression_templates()
        if request.operation == "save_template":
            saved = self.service.save_expression_template(payload["template"])
            try:
                templates = self.service.list_expression_templates()
                refresh_error = ""
            except (OSError, ValueError) as exc:
                templates = None
                refresh_error = str(exc)
            return {"saved": saved, "templates": templates,
                    "refresh_error": refresh_error}
        if request.operation == "apply_template":
            return self.service.load_expression_template(str(payload["template_id"]))
        raise ValueError("unknown expression task operation")

    def complete_expression_task(self, request: ExpressionTaskRequest, *,
                                 result: Any = None, error: str = "") -> bool:
        with self._lock:
            active = self._active_expression
            if active is None or active.token != request.token:
                return False
            self._active_expression = None
            self._expression_busy = False
            self._expression_operation = ""
            if (request.operation in ("expand", "apply_template")
                    and request.base_design_revision != self._design_revision):
                self._error = ""
                self._set_error("后台计算期间设计已经变化，旧结果未应用；请重新执行。")
                return False
            if error:
                self._error = ""
                self._set_error(error)
                return False
            if request.operation == "refresh_templates" and isinstance(result, (list, tuple)):
                self._expression_templates = deepcopy(list(result))
                self._error = ""
                self._notify("expression_templates", "ui")
                return True
            if request.operation == "save_template" and isinstance(result, Mapping):
                templates = result.get("templates")
                if templates is not None:
                    self._expression_templates = deepcopy(list(templates))
                saved = result.get("saved", {})
                self._message = "\u8868\u8fbe\u5f0f\u6a21\u677f\u5df2\u4fdd\u5b58\uff1a" + str(saved.get("name", ""))
                self._error = str(result.get("refresh_error", ""))
                self._notify("expression_templates", "ui")
                return True
            if request.operation == "apply_template" and isinstance(result, Mapping):
                expression = result.get("expression")
                if not isinstance(expression, Mapping):
                    self._error = "\u8868\u8fbe\u5f0f\u6a21\u677f\u6570\u636e\u683c\u5f0f\u65e0\u6548"
                    self._notify("ui")
                    return False
                self._design["expression"] = {
                    "text": str(expression.get("text", "")),
                    "symbols": deepcopy(expression.get("symbols", {})),
                    "in_sync": False,
                }
                self._design_revision += 1
                self._dirty = True
                self._error = ""
                self._message = "\u8868\u8fbe\u5f0f\u6a21\u677f\u5df2\u52a0\u8f7d\uff1b\u5c55\u5f00\u540e\u624d\u4f1a\u66f4\u65b0\u5f53\u524d\u819c\u5806\u3002"
                self._refresh_derived()
                self._notify("design", "ui", "expression", "derived")
                return True
            if request.operation != "expand" or not isinstance(result, Mapping):
                self._error = ""
                self._set_error("表达式后台任务返回了无效结果。")
                return False
            self._design = self._preserve_workflow_configuration(result)
            self._design_revision += 1
            self._dirty = True
            self._error = ""
            side = str(request.payload["side"])
            count = len(self._design.get(side + "_layers", []))
            label = "正面" if side == "front" else "背面"
            self._message = f"表达式已展开到{label}膜堆，共 {count} 层。"
            self._refresh_derived()
            self._notify("design", "ui", "expression", "derived")
            return True

    def save_expression_template(self, name: str, note: str = "") -> bool:
        with self._lock:
            try:
                self.normalize_expression()
                expression = self._design["expression"]
                template = {
                    "schema_version": 1,
                    "template_id": uuid.uuid4().hex,
                    "name": str(name),
                    "note": str(note),
                    "expression": {"text": expression["text"],
                                   "symbols": deepcopy(expression["symbols"])},
                }
                saved = self.service.save_expression_template(template)
                self._expression_templates = self.service.list_expression_templates()
                self._message = "表达式模板已保存：" + saved["name"]
                self._error = ""
                self._notify("expression_templates", "ui")
                return True
            except (ValueError, TypeError, OSError) as exc:
                self._set_error(str(exc))
                return False

    def refresh_expression_templates(self) -> bool:
        with self._lock:
            try:
                self._expression_templates = self.service.list_expression_templates()
                self._error = ""
                self._notify("expression_templates", "ui")
                return True
            except (OSError, ValueError) as exc:
                self._set_error(str(exc))
                return False

    def apply_expression_template(self, template_id: str) -> bool:
        with self._lock:
            try:
                template = self.service.load_expression_template(template_id)
                self._design["expression"] = {
                    "text": template["expression"]["text"],
                    "symbols": deepcopy(template["expression"]["symbols"]),
                    "in_sync": False,
                }
                self._design_revision += 1
                self._dirty = True
                self._error = ""
                self._message = "表达式模板已加载；展开后才会更新当前膜堆。"
                self._refresh_derived()
                self._notify("design", "ui", "expression", "derived")
                return True
            except (ValueError, OSError) as exc:
                self._set_error(str(exc))
                return False

    def refresh_saved_designs(self) -> bool:
        with self._lock:
            try:
                self._saved_designs = self.service.list_saved_designs()
                self._error = ""
                self._notify("saved_designs", "ui")
                return True
            except (OSError, ValueError) as exc:
                self._set_error(str(exc))
                return False

    def save_to_library(self, name: str) -> bool:
        with self._lock:
            try:
                candidate = deepcopy(self._design)
                if name:
                    candidate["name"] = str(name)
                normalized = self.service.validate(candidate)
                saved = self.service.save_saved_design(normalized)
                self._generated_undo_state = None
                self._design = saved
                self._design_revision += 1
                self._dirty = False
                self._error = ""
                self._message = "已保存到膜系 SQLite 库。"
                try:
                    self._saved_designs = self.service.list_saved_designs()
                except (OSError, ValueError) as refresh_exc:
                    self._error = str(refresh_exc)
                self._refresh_derived()
                self._notify("design", "ui", "derived", "saved_designs", "expression")
                return True
            except (ValueError, TypeError, OSError) as exc:
                self._set_error(str(exc))
                return False

    def load_saved_design(self, design_id: str) -> bool:
        with self._lock:
            try:
                self._replace_design(self.service.load_saved_design(design_id), path=None,
                                     message="已从膜系 SQLite 库加载设计。")
                return True
            except (ValueError, OSError) as exc:
                self._set_error(str(exc))
                return False

    def _replace_design(self, design: dict[str, Any], *, path: str | None,
                        message: str, dirty: bool = False) -> None:
        self._generated_undo_state = None
        self._design = design
        self._design_revision += 1
        self._path = path
        self._dirty = dirty
        self._error = ""
        self._message = message
        self._refresh_derived()
        self._notify("design", "ui", "derived", "expression", "workflow_settings")

    def request_imported_design(self, design: Mapping[str, Any],
                                base_design_revision: int) -> bool:
        """Stage an isolated candidate; all imports require a visible confirmation."""
        with self._lock:
            if self._pending_confirmation is not None or self._expression_busy:
                self._set_error("请先完成当前确认或表达式操作，再导入。")
                return False
            if type(base_design_revision) is not int or base_design_revision != self._design_revision:
                self._set_error("设计已改变，请重新生成导入预览。")
                return False
            try:
                candidate = self.service.validate(deepcopy(dict(design)))
            except (ValueError, TypeError) as exc:
                self._set_error(str(exc))
                return False
            self._pending_confirmation = {
                "kind": "import_replace", "action": "import_tfcalc",
                "message": "导入将替换所选面的膜层，并形成尚未保存的设计修改。当前未保存修改中，预览已明确沿用的字段会保留；所选面的原膜层将被替换。是否继续？",
                "payload": candidate, "base_design": deepcopy(self._design),
                "base_design_revision": self._design_revision,
            }
            self._notify("confirmation", "ui")
            return True

    def cancel_pending_import(self) -> None:
        with self._lock:
            if (self._pending_confirmation or {}).get("kind") == "import_replace":
                self._pending_confirmation = None
                self._notify("confirmation", "ui")

    def _request_action(self, action: str, payload: Any, message: str) -> bool:
        if not self._dirty:
            return self._run_action(action, payload)
        self._pending_confirmation = {
            "kind": "discard_changes", "action": action,
            "message": message, "payload": payload,
        }
        self._notify("confirmation", "ui")
        return False

    def request_new_design(self) -> bool:
        with self._lock:
            return self._request_action("new", None, "放弃当前未保存的修改并新建膜系？")

    def request_open_file(self, path: str) -> bool:
        with self._lock:
            if not path:
                self._set_error("请选择一个 .cfilm 膜系设计文件。")
                return False
            return self._request_action("open_file", str(path),
                                        "放弃当前未保存的修改并打开所选膜系？")

    def request_replace_design(self, design: Mapping[str, Any]) -> bool:
        with self._lock:
            try:
                candidate = self.service.validate(deepcopy(dict(design)))
            except (ValueError, TypeError) as exc:
                self._set_error(str(exc))
                return False
            return self._request_action("replace_design", candidate,
                                        "放弃当前未保存的修改并载入新膜系？")

    def request_load_saved_design(self, design_id: str) -> bool:
        with self._lock:
            if not design_id:
                self._set_error("请选择一个已保存膜系")
                return False
            return self._request_action("load_saved", str(design_id),
                                        "加载膜系库中的设计并替换当前内容？")

    def request_close(self) -> bool:
        with self._lock:
            if self._dirty:
                self._pending_confirmation = {
                    "kind": "discard_changes", "action": "close", "payload": None,
                    "message": "关闭会放弃当前未保存的膜系修改，仍要关闭吗？",
                }
                self._notify("confirmation", "ui")
                return False
            return True

    def resolve_pending_action(self, accepted: bool) -> bool:
        with self._lock:
            pending, self._pending_confirmation = self._pending_confirmation, None
            if pending is None:
                return False
            if pending.get("kind") == "import_replace":
                if not accepted:
                    self._notify("confirmation", "ui")
                    return False
                if (pending.get("base_design_revision") != self._design_revision
                        or pending.get("base_design") != self._design):
                    self._set_error("确认期间设计或设置已改变，请重新生成导入预览。")
                    self._notify("confirmation", "ui")
                    return False
                self._replace_design(deepcopy(pending["payload"]), path=None,
                                     message="已导入 TFCalc 显式层表；请另存为 .cfilm。", dirty=True)
                self._notify("confirmation", "ui")
                return True
            if pending.get("kind") == "overwrite":
                if not accepted:
                    self._notify("confirmation", "ui")
                    return False
                return self.save_file(str(pending.get("path", "")), overwrite=True)
            if pending.get("kind") != "discard_changes":
                return False
            self._notify("confirmation", "ui")
            if not accepted:
                return False
            return self._run_action(str(pending["action"]), pending.get("payload"))

    def _run_action(self, action: str, payload: Any) -> bool:
        try:
            if action == "new":
                self._design = self._default_design()
                self._design_revision += 1
                self._path = None
                self._dirty = False
                self._error = ""
                self._message = "新建膜系设计。"
                self._refresh_derived()
                self._notify("design", "ui", "derived", "expression", "workflow_settings")
                return True
            if action == "open_file":
                design = self.service.load(str(payload))
                self._replace_design(design, path=str(Path(str(payload))),
                                     message="已打开膜系设计。")
                return True
            if action == "replace_design":
                self._replace_design(deepcopy(dict(payload)), path=None,
                                     message="已载入膜系设计。")
                return True
            if action == "load_saved":
                design = self.service.load_saved_design(str(payload))
                self._replace_design(design, path=None,
                                     message="已从膜系 SQLite 库加载设计。")
                return True
            if action == "close":
                return True
            raise ValueError("unknown pending action")
        except (ValueError, OSError) as exc:
            self._set_error(str(exc))
            return False

    def save_file(self, path: str, *, overwrite: bool = False) -> bool:
        with self._lock:
            if not path:
                self._set_error("请选择保存路径")
                return False
            target = Path(path)
            if target.exists() and not overwrite:
                self._pending_confirmation = {
                    "kind": "overwrite", "action": "overwrite",
                    "path": str(target), "message": "目标文件已存在，是否覆盖？",
                }
                self._notify("confirmation", "ui")
                return False
            try:
                normalized = self.service.save(target, self._design, overwrite=bool(overwrite))
                self._generated_undo_state = None
                self._design = dict(normalized)
                self._path = str(target)
                self._dirty = False
                self._error = ""
                self._message = "Saved as .cfilm (UTF-8 JSON content)."
                self._pending_confirmation = None
                self._refresh_derived()
                self._notify("design", "ui", "derived", "expression", "confirmation")
                return True
            except (ValueError, TypeError, OSError) as exc:
                self._set_error(str(exc))
                return False

    def save_current(self) -> bool:
        with self._lock:
            if not self._path:
                self._set_error("请选择一个 .cfilm 膜系设计文件。")
                return False
            path = self._path
        return self.save_file(path, overwrite=True)


__all__ = [
    "ExpressionTaskRequest", "FilmDesignEditSession", "SessionChange",
    "SessionSnapshot", "SESSION_CHANGE_CATEGORIES",
]
