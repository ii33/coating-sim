"""File, calculation, and preview services shared by GUI and MCP clients.

The pure modules in this package own geometry and physics. This service owns
paths, dependency checks, overwrite policy, and error translation.
It deliberately does not discover or import third-party plugins.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace, asdict
import json
import math
import os
from pathlib import Path
import tempfile
import traceback
from typing import TYPE_CHECKING, Any

from .errors import new_error_id, record_error
from .atomic_io import replace_with_retry
from .design_files import design_extension, validate_design_path, default_design_path

from .machine_profiles import (read_machine_profile, read_mask_profile,
                              resolve_machine_profile, validate_machine_profile,
                              validate_mask_profile)
from .process_profiles import (read_process_profile, resolve_process_profile,
                              validate_process_profile, step_mask_slots)
from .calculation_history import CalculationHistory, format_elapsed
from .source_profiles import (read_source_profile, source_samples,
                             validate_source_profile)
from .substrate_profiles import (effective_radius_mm, outline_polygon_points,
                                read_substrate_profile, sample_curve_points,
                                validate_substrate_profile)
from .substrate_surface import (surface_height_and_normal,
                               validate_substrate_surface)

from .plugins import PluginRegistry


if TYPE_CHECKING:
    from .simulator import Settings


KINDS = frozenset({"source", "substrate", "mask", "machine", "process"})


def recipe_algorithm_options(algorithm, options, backend, carrier_rpm):
    """Validate the public execution contract before freezing or starting a worker."""
    if algorithm not in ("fast", "reference", "optimize"):
        raise ValueError("algorithm must be fast, reference or optimize")
    if options is not None and not isinstance(options, Mapping):
        raise ValueError("algorithm_options must be an object")
    value = dict(options or {})
    allowed = {"budget_s", "max_evaluations", "tolerance_percent", "rpm_scale_bounds",
               "sampling_policy", "periodic_integration"}
    if set(value) - allowed:
        raise ValueError("unknown algorithm_options: " + ", ".join(sorted(set(value) - allowed)))
    if algorithm != "fast" and backend not in ("CPU", "自动"):
        raise ValueError("reference/optimize run on CPU; GPU/DirectML is not a valid combination")
    policy = value.get("sampling_policy", "automatic" if algorithm == "reference" else "requested")
    periodic = value.get("periodic_integration", algorithm == "reference")
    if policy not in ("automatic", "requested") or type(periodic) is not bool:
        raise ValueError("sampling_policy must be automatic/requested; periodic_integration must be boolean")
    if algorithm != "reference" and (policy != "requested" or periodic):
        raise ValueError("automatic sampling and periodic integration require reference")
    budget = value.get("budget_s")
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, (float, int))
            or not math.isfinite(budget) or not 0 < budget <= 300):
        raise ValueError("budget_s must be finite and in (0, 300]")
    count = value.get("max_evaluations", 6 if algorithm == "reference" and policy == "automatic"
                      else 4 if algorithm == "reference" else 9)
    if type(count) is not int or not 1 <= count <= 100:
        raise ValueError("max_evaluations must be an integer in 1..100")
    tolerance = value.get("tolerance_percent", .1)
    if isinstance(tolerance, bool) or not isinstance(tolerance, (float, int)) or not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError("tolerance_percent must be positive and finite")
    bounds = value.get("rpm_scale_bounds", [.8, 1.2])
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 2 or any(
            isinstance(x, bool) or not isinstance(x, (float, int)) or not math.isfinite(x) for x in bounds):
        raise ValueError("rpm_scale_bounds must contain two finite numbers")
    lo, hi = bounds
    if not 0 < lo <= 1 <= hi or lo == hi or (algorithm == "optimize" and abs(carrier_rpm) * hi > 300):
        raise ValueError("rpm_scale_bounds must contain 1 with positive distinct endpoints; execution model requires |rpm| <= 300")
    # Keep defaults absent so legacy fast input identities and behavior remain stable.
    return value


def canonical_compute_parameters(service, kind, supplied):
    from .compute_estimation import canonical_parameters
    value = dict(supplied)
    if kind != "process":
        return canonical_parameters(service, kind, value)
    algorithm = value.pop("algorithm", "fast")
    options = value.pop("algorithm_options", None)
    normalized = canonical_parameters(service, kind, value)
    options = recipe_algorithm_options(algorithm, options, normalized["backend"],
                                       normalized["settings"]["carrier_rpm"])
    if algorithm != "fast" or options:
        normalized.update(algorithm=algorithm, algorithm_options=options)
    return normalized
_READERS = {
    "source": read_source_profile,
    "substrate": read_substrate_profile,
    "mask": read_mask_profile,
    "machine": read_machine_profile,
    "process": read_process_profile,
}
_VALIDATORS = {
    "source": validate_source_profile,
    "substrate": validate_substrate_profile,
    "mask": validate_mask_profile,
    "machine": validate_machine_profile,
    "process": validate_process_profile,
}


@dataclass(frozen=True)
class ServiceError(ValueError):
    code: str
    message: str
    path: str | None = None
    details: tuple[str, ...] = ()
    occurrence_id: str = field(default_factory=new_error_id, init=False, compare=False)
    source: str = field(default="", init=False, compare=False)

    def __post_init__(self) -> None:
        caller = traceback.extract_stack(limit=4)[-3]
        object.__setattr__(self, "source", f"{caller.filename}:{caller.lineno} ({caller.name})")

    def _record(self) -> None:
        record_error("service." + self.code, self.message + "\n" + "\n".join(self.details),
                     occurrence_id=self.occurrence_id, source=self.source)

    def __str__(self) -> str:
        self._record()
        return (self.message + ("\n" + "\n".join(self.details) if self.details else "")
                + f"\n[service.{self.code} · {self.occurrence_id}]")

    def as_dict(self) -> dict[str, Any]:
        self._record()
        return {"code": self.code, "message": self.message,
                "path": self.path, "details": list(self.details),
                "occurrence_id": self.occurrence_id, "source": self.source}


@dataclass(frozen=True)
class ValidationResult:
    kind: str
    valid: bool
    data: dict[str, Any] | None
    issues: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.valid

    @property
    def errors(self) -> tuple[str, ...]:
        return self.issues


@dataclass(frozen=True)
class Document:
    kind: str
    path: str
    data: dict[str, Any]
    issues: tuple[str, ...] = ()
    runtime: dict[str, Any] | None = None

    @property
    def valid(self) -> bool:
        return not self.issues

    @property
    def ok(self) -> bool:
        return self.valid

    @property
    def errors(self) -> tuple[str, ...]:
        return self.issues


class CoatService:
    """A path-aware boundary around the project's pure algorithms.

    ``root`` is the directory for relative client paths. By default, the
    project directory is the permitted boundary too. A desktop file picker may
    opt into paths outside that directory with ``restrict_to_root=False``;
    automation clients should keep the default restriction.
    """

    def __init__(self, root: str | os.PathLike[str] | None = None, *,
                 restrict_to_root: bool = True,
                 plugins: PluginRegistry | None = None) -> None:
        self.root = Path(root or Path(__file__).resolve().parents[2]).expanduser().resolve()
        self.restrict_to_root = bool(restrict_to_root)
        self.plugins = plugins if plugins is not None else PluginRegistry()
        self.history = CalculationHistory(self.root / "history" / "computations.jsonl")
        self._film_design_service = None

    @property
    def film_design_service(self):
        """Lazily create the film-design service with this instance's path policy."""
        if self._film_design_service is None:
            from .film_design_service import FilmDesignService
            self._film_design_service = FilmDesignService(
                root=self.root, restrict_to_root=self.restrict_to_root, plugins=self.plugins)
        return self._film_design_service

    def new_film_design(self):
        return self.film_design_service.new_design()

    def film_material_options(self, purpose="film"):
        return self.film_design_service.material_options(purpose=purpose)

    def validate_film_design(self, data):
        return self.film_design_service.validate(data)

    def load_film_design(self, path):
        return self.film_design_service.load(path)

    def save_film_design(self, path, data, *, overwrite=False):
        design = self.film_design_service.save(path, data, overwrite=overwrite)
        return {"path": str(self.film_design_service.paths._path(path)),
                "id": design["id"], "name": design["name"]}

    def expand_film_expression(self, expression, symbols):
        return self.film_design_service.expand(expression, symbols)

    def list_film_design_library(self):
        return self.film_design_service.list_saved_designs()

    def save_film_design_library(self, data):
        return self.film_design_service.save_saved_design(data)

    def load_film_design_library(self, design_id):
        return self.film_design_service.load_saved_design(design_id)

    def simulate_film_design(self, data, settings=None):
        return self.film_design_service.simulate(data, settings=settings)

    def export_film_csv(self, path, result, *, overwrite=False):
        return self.film_design_service.export_csv(path, result, overwrite=overwrite)

    def estimate_compute_task(self, kind, parameters):
        from .compute_estimation import estimate, input_file_fingerprints
        from .compute_tasks import ComputeTaskStore
        if kind == "film_synthesis":
            from .film_synthesis_io import film_synthesis_request_from_payload
            _, synthesis_request, _ = film_synthesis_request_from_payload(
                parameters["payload"], require_frozen=False)
            if synthesis_request.randomize_template_search and synthesis_request.structure_search_seed is None:
                raise ValueError("freeze randomized template search before estimation to resolve structure_search_seed")
        values = canonical_compute_parameters(self, kind, parameters)
        legacy = {key: value for key, value in values.items() if key not in ("algorithm", "algorithm_options")}
        algorithm = values.get("algorithm", "fast")
        if kind == "process" and algorithm != "fast":
            legacy.update(backend="CPU", fast_cycle=False)
        calculation = estimate(self, kind, legacy)
        if kind == "process":
            calculation["summary"] += " / algorithm=" + algorithm
            calculation["algorithm"] = algorithm
            calculation["algorithm_options"] = values.get("algorithm_options", {})
            if algorithm != "fast":
                calculation.update(estimated_s=None, sample_count=0,
                    basis="No matching multi-evaluation history; runtime is unknown and an explicit budget is a stop limit, not a prediction",
                    budget_s=values.get("algorithm_options", {}).get("budget_s"))
        files = input_file_fingerprints(self, kind, values)
        receipt = ComputeTaskStore(self.root).create_estimate(kind, values, calculation, files)
        from coat_sim.scheduler import SchedulerClient
        SchedulerClient(self.root).refresh_estimates()
        return receipt

    def request_compute_confirmation(self, estimate_id, *, session_id="", request_id="",
                                     _release_immediate=True):
        from .compute_tasks import ComputeTaskStore
        receipt = ComputeTaskStore(self.root).request(estimate_id, session_id=session_id, request_id=request_id)
        from coat_sim.scheduler import SchedulerClient
        SchedulerClient(self.root).refresh_estimates()
        if receipt.get("job_id") and _release_immediate:
            return self.release_due_compute_estimate(estimate_id)
        if receipt.get("state") in ("pending", "approved"):
            if receipt.get("release_policy") == "immediate" and _release_immediate:
                return self.release_due_compute_estimate(estimate_id)
            return {**receipt, "state": "awaiting_confirmation",
                    "needs_user_confirmation": receipt.get("release_policy", "manual") == "manual"}
        return receipt

    def confirm_compute_estimate(self, estimate_id, accepted):
        """Trusted desktop decision; the model cannot inject an approval boolean."""
        from coat_sim.scheduler.submission import confirm_and_submit
        return confirm_and_submit(self, estimate_id, accepted)

    def release_due_compute_estimate(self, estimate_id):
        """Trusted desktop timer/policy entry; validates the persisted release deadline."""
        from coat_sim.scheduler.submission import confirm_and_submit
        return confirm_and_submit(self, estimate_id, True, automatic=True)

    def calculation_queue_snapshot(self, *, after_event=0, limit=100):
        from coat_sim.scheduler import SchedulerClient
        return SchedulerClient(self.root).snapshot(after_event=after_event, limit=limit)

    def _refresh_history(self) -> None:
        """Include successful runs written by other local application processes."""
        self.history = CalculationHistory(self.root / "history" / "computations.jsonl")

    def _path(self, value: str | os.PathLike[str], *,
              base: Path | None = None) -> Path:
        if value is None or not str(value).strip():
            raise ServiceError("invalid_path", "文件路径不能为空")
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = (base or self.root) / path
        path = path.resolve()
        if self.restrict_to_root and not path.is_relative_to(self.root):
            raise ServiceError("path_outside_root", "文件路径超出允许目录", str(path))
        return path

    def generate_film_stack(self, specification, path, *, overwrite=False):
        """Generate layers in-program and save one validated process; return a short receipt."""
        from .film_stack import build_film_stack
        target = self._path(path)
        from .film_stack_schema import film_stack_spec_data
        specification = film_stack_spec_data(dict(specification))
        machine = self.load("machine", self.resolve_reference(target, specification["machine_file"]))
        generated = build_film_stack(specification, machine=machine.runtime)
        document = self.save("process", target, generated["profile"], overwrite=overwrite)
        return {"ok": True, "path": document.path, "summary": generated["summary"],
                "step_count": len(document.data["steps"])}

    def prepare_simulation_asset(self, *, result, settings, machine=None, process=None,
                                 elapsed_s, requested_device, run_id=None,
                                 requested_settings=None, input_snapshot=None,
                                 record_history=True):
        """Store successful simulation products in temporary storage; never auto-save an asset."""
        from .simulation_assets import prepare_simulation_asset
        return prepare_simulation_asset(self.root, result=result, settings=settings,
            machine=machine, process=process, elapsed_s=elapsed_s,
            requested_device=requested_device, run_id=run_id,
            requested_settings=requested_settings, input_snapshot=input_snapshot,
            record_history=record_history)

    def save_simulation_asset(self, run_id, name, *, confirmed, path=None, overwrite=False, include_results=True):
        from .simulation_assets import save_simulation_asset
        target = self._path(path) if path is not None else None
        return save_simulation_asset(self.root, run_id, name, confirmed=confirmed,
                                     path=target, overwrite=overwrite, include_results=include_results)

    def save_simulation_asset_copy(self, source, name, *, confirmed, path=None,
                                   overwrite=False, include_results=True):
        from .simulation_assets import save_simulation_asset_copy
        source_path = self._path(source)
        target = self._path(path) if path is not None else None
        return save_simulation_asset_copy(self.root, source_path, name, confirmed=confirmed,
            path=target, overwrite=overwrite, include_results=include_results)

    def export_simulation_asset(self, path, destination, **options):
        from .simulation_assets import export_simulation_asset
        return export_simulation_asset(self._path(path), self._path(destination), **options)

    def material_library(self):
        from .material_business import MaterialLibrary
        return MaterialLibrary(self.root)

    def check_machine_structure(self, *, machine_path=None, machine_data=None,
                                substrate_path=None, substrate_data=None):
        """Read-only engineering review using the existing machine/surface validators.

        Warnings are unresolved engineering questions, not new numerical hard limits.
        Drafts are checked before creation; paths also check actual dependencies after creation.
        """
        if (machine_path is None) == (machine_data is None):
            raise ValueError("supply exactly one of machine_path or machine_data")
        if substrate_path is not None and substrate_data is not None:
            raise ValueError("supply substrate_path or substrate_data, not both")
        warnings = ["Chamber, holders, shields, support clearance and thermal limits are unknown; this is not a real-machine clearance certificate."]
        errors = []
        masks = []
        try:
            if machine_path is not None:
                target = self._path(machine_path)
                if target.suffix.lower() == ".coatasset":
                    # A machine Document from an asset has no filesystem runtime.
                    # Resolve its boards from the already validated package graph,
                    # never from rebased names beside the package on disk.
                    from .simulation_assets import _relative_member
                    content = self.read_simulation_asset(target, detail="full")
                    designs = {row["path"]: row for row in content["designs"]}
                    machines = [row for row in designs.values() if row["kind"] == "machine"]
                    if len(machines) != 1:
                        raise ValueError("资产包中该类型设计不唯一，请在包内设计选择器中选择后导出打开")
                    row = machines[0]
                    machine = validate_machine_profile(row["data"])
                    masks = [{"profile": validate_mask_profile(
                        designs[_relative_member(row["path"], entry["file"])]["data"],
                        substrate_z_mm=machine["substrate_z_mm"])}
                        for entry in machine["masks"]]
                else:
                    document = self.load("machine", target)
                    machine = document.data
                    errors.extend(document.issues)
                    masks = document.runtime.get("masks", []) if document.runtime else []
            else:
                machine = validate_machine_profile(machine_data)
                warnings.append("Draft dependency files have not been resolved; check the saved machine_path after creation.")
            if substrate_path is not None:
                substrate_data = self.load("substrate", substrate_path).data
            if machine["gear_module_mm"] == 0:
                warnings.append("Zero gear module/orbit is a degenerate model choice, not a physical directly meshing gear design.")
            minimum = None
            if substrate_data is not None:
                from .simulator import Settings
                from .mask_analysis import _active_surface, _minimum_substrate_z
                substrate = validate_substrate_profile(substrate_data)
                keys = ("carrier_x_mm", "carrier_y_mm", "planet_offset_mm", "planet_tilt_deg",
                        "substrate_z_mm", "sun_teeth", "planet_teeth", "gear_module_mm")
                local = replace(Settings(), **{key: machine[key] for key in keys},
                                substrate_profile=substrate, substrate_radius_mm=effective_radius_mm(substrate))
                minimum = _minimum_substrate_z(local, _active_surface(local))
                for slot, source in enumerate(machine["sources"], 1):
                    if source["z_mm"] >= minimum:
                        warnings.append(f"Source {slot} installation height is at/above minimum moving substrate height; review source orientation and this model's applicability.")
                for slot, entry in enumerate(masks):
                    if entry.get("profile") is not None:
                        try:
                            validate_mask_profile(entry["profile"], substrate_z_mm=minimum)
                        except ValueError as exc:
                            errors.append(f"mask[{slot}]: {exc}")
            else:
                warnings.append("Substrate geometry is missing; minimum motion height and correction-board clearance are not checked.")
            return {"valid": not errors, "errors": errors, "warnings": warnings,
                    "topology": "fixed external sun / direct external planet mesh",
                    "orbit_radius_mm": machine["planet_offset_mm"],
                    "absolute_spin_per_carrier": 1 + machine["sun_teeth"] / machine["planet_teeth"],
                    "minimum_substrate_z_mm": minimum, "modified": False,
                    "unsupported_topologies": ["single rotation", "internal ring", "idler gears"]}
        except (ValueError, TypeError, ServiceError) as exc:
            return {"valid": False, "errors": [str(exc)], "warnings": warnings, "modified": False}

    def validate_material_optics(self, datasets, *, wavelength=None, wavelength_unit=None):
        from .optical_constants import validate_optical_sets, evaluate_optical_set
        records = validate_optical_sets(datasets)
        if (wavelength is None) != (wavelength_unit is None):
            raise ValueError("wavelength and wavelength_unit must be supplied together")
        if wavelength is not None and (isinstance(wavelength, bool)
                or not isinstance(wavelength, (int, float))
                or not math.isfinite(wavelength) or wavelength <= 0):
            raise ValueError("wavelength must be a positive finite number")
        if wavelength_unit is not None and wavelength_unit not in ("nm", "um"):
            raise ValueError("wavelength_unit must be nm or um")
        evaluations = []
        if wavelength is not None:
            for item in records:
                n, k = evaluate_optical_set(item, wavelength, wavelength_unit)
                evaluations.append({"dataset_id": item["id"], "n": n, "k": k,
                                    "wavelength": wavelength, "wavelength_unit": wavelength_unit})
        return {"valid": True, "datasets": records, "evaluations": evaluations}

    def read_simulation_asset(self, path, *, detail="summary", offset=0, limit=20,
                              include_arrays=False):
        from .simulation_assets import read_simulation_asset
        return read_simulation_asset(self._path(path), detail=detail, offset=offset,
                                     limit=limit, include_arrays=include_arrays)

    def load_simulation_asset(self, path):
        """Desktop/API reader with local NumPy arrays; no array transport to the model."""
        content = self.read_simulation_asset(path, detail="full", include_arrays=True)
        configuration = content["configuration"]
        return {"run_id": content["run_id"], "asset_path": content["asset_path"],
                "result": {**content["result_metadata"], **content["arrays"]},
                "settings": configuration["settings"], "machine": configuration["machine"],
                "process": configuration["process"], "requested_settings": configuration["requested_settings"],
                "elapsed_s": content["summary"].get("elapsed_s", 0),
                "input_snapshot": configuration.get("input_snapshot"), "has_results": content.get("has_results", True),
                "requested_device": content["summary"].get("requested_device", "CPU"), "name": content["name"]}

    def simulation_summaries(self, *, limit=20, offset=0):
        from .simulation_assets import list_simulation_summaries
        return list_simulation_summaries(self.root, limit=limit, offset=offset)

    def record_simulation_attempt(self, *, status, run_id=None, settings=None,
                                   process_path=None, requested_device=None, error=None, elapsed_s=None):
        from .simulation_assets import record_simulation_attempt
        return record_simulation_attempt(self.root, status=status, run_id=run_id,
            settings=settings, process_path=process_path, requested_device=requested_device,
            error=error, elapsed_s=elapsed_s)

    def prepare_temporary_design(self, kind, data, *, name_hint=""):
        """Create a new agent draft in tmp; a later explicit action publishes it."""
        from uuid import uuid4
        from .data_files import DESIGN_FOLDERS
        self._kind(kind)
        directory = self.root / "data" / "tmp" / "designs" / uuid4().hex
        directory.mkdir(parents=True)
        target = directory / (kind + design_extension(kind))
        document = self.save(kind, target, data, overwrite=False)
        suggestion = str(name_hint or document.data.get("name") or
                         {"source": "蒸发源设计", "substrate": "基片设计", "mask": "修正板设计",
                          "machine": "机器结构设计", "process": "膜系工艺设计"}[kind])[:80]
        return {"ok": True, "kind": kind, "path": document.path, "temporary": True,
                "name_suggestion": suggestion, "save_tool": "save_generated_design",
                "save_requires_user_confirmation": True,
                "default_save_directory": str(self.root / "data" / DESIGN_FOLDERS[kind])}

    def save_generated_design(self, kind, path, name, *, confirmed, overwrite=False):
        """Publish a named design and copy its dependencies; original files stay in place."""
        from copy import deepcopy
        from .data_files import DESIGN_FOLDERS
        from .simulation_assets import asset_name
        if confirmed is not True:
            raise ServiceError("confirmation_required", "请先向用户确认保存意愿与名称")
        name = asset_name(name)
        original = self.load(kind, path)
        if original.issues:
            raise ServiceError("dependency_error", "设计依赖无效", original.path, original.issues)
        destination = self.root / "data" / DESIGN_FOLDERS[kind] / (name + design_extension(kind))
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and not overwrite:
            raise ServiceError("already_exists", "同名资产已存在，请明确允许覆盖", str(destination))
        copied = {}
        dependency_dir = destination.parent / (name + " 依赖")

        def clone(child_kind, source, target=None):
            source = self._path(source)
            key = (child_kind, str(source))
            if key in copied:
                return copied[key]
            document = self.load(child_kind, source)
            if document.issues:
                raise ServiceError("dependency_error", "设计依赖无效", document.path, document.issues)
            if target is None:
                dependency_dir.mkdir(parents=True, exist_ok=True)
                target = dependency_dir / f"{child_kind}-{len(copied) + 1}{design_extension(child_kind)}"
            if target.exists() and overwrite:
                _READERS[child_kind](target)
            copied[key] = target
            value = deepcopy(document.data)
            if child_kind == "machine":
                for dep_kind, field in (("source", "sources"), ("mask", "masks")):
                    for entry in value[field]:
                        child = clone(dep_kind, self.resolve_reference(source, entry["file"]))
                        entry["file"] = self.relative_reference(target, child)
            elif child_kind == "process":
                for child_kind_name, field in (("machine", "machine_file"), ("substrate", "substrate_file")):
                    child = clone(child_kind_name, self.resolve_reference(source, value[field]))
                    value[field] = self.relative_reference(target, child)
                value["name"] = name
            self.save(child_kind, target, value, overwrite=overwrite)
            return target

        saved = clone(kind, original.path, destination)
        return {"ok": True, "kind": kind, "name": name, "path": str(saved),
                "temporary_source_retained": True, "dependency_count": len(copied) - 1}

    def preview_film_stack_import(self, path, **options):
        from .film_stack import preview_film_stack_import
        source = self._path(path)
        if source.stat().st_size > 16 * 1024 * 1024:
            raise ServiceError("input_too_large", "层表文件不能超过 16 MiB", str(source))
        return {"path": str(source), **preview_film_stack_import(source.name, source.read_bytes(), **options)}

    def import_film_stack(self, path, specification, output_path, *, overwrite=False, **options):
        from .film_stack import parse_film_stack_file, build_imported_film_stack
        source, target = self._path(path), self._path(output_path)
        if source.stat().st_size > 16 * 1024 * 1024:
            raise ServiceError("input_too_large", "层表文件不能超过 16 MiB", str(source))
        table = parse_film_stack_file(source.name, source.read_bytes(), **options)
        from .film_stack_schema import film_stack_spec_data
        specification = film_stack_spec_data(dict(specification))
        machine = self.load("machine", self.resolve_reference(target, specification["machine_file"]))
        generated = build_imported_film_stack(table, specification, machine=machine.runtime)
        document = self.save("process", target, generated["profile"], overwrite=overwrite)
        return {"ok": True, "path": document.path, "imported_from": str(source),
                "summary": generated["summary"], "step_count": len(document.data["steps"])}

    def update_design_fields(self, kind, path, patches, *, output_path=None, overwrite=False):
        from .design_patch import apply_design_patches
        original = self.load(kind, path)
        target = self._path(output_path or path)
        draft = apply_design_patches(original.data, patches)
        if target != Path(original.path):
            if kind == "process":
                for key in ("machine_file", "substrate_file"):
                    draft[key] = self.relative_reference(target, self.resolve_reference(original.path, draft[key]))
            elif kind == "machine":
                for entry in (*draft.get("sources", []), *draft.get("masks", [])):
                    entry["file"] = self.relative_reference(target, self.resolve_reference(original.path, entry["file"]))
        document = self.save(kind, target, draft, overwrite=overwrite)
        return {"ok": True, "kind": kind, "path": document.path, "updated_fields": [item["path"] for item in patches],
                "step_count": len(document.data.get("steps", []))}

    def import_mask_dxf(self, path, *, units="auto", curve_tolerance_mm=0.05,
                        join_tolerance_mm=0.01, base_mask=None) -> dict[str, Any]:
        """Import a planar DXF to an unsaved correction-board draft."""
        from .mask_cad import import_mask_dxf
        source = self._path(path)
        if source.suffix.lower() != ".dxf":
            raise ServiceError("invalid_path", "CAD 导入须选择 .dxf 文件", str(source))
        try:
            return import_mask_dxf(source, units=units, curve_tolerance_mm=curve_tolerance_mm,
                                   join_tolerance_mm=join_tolerance_mm, base_mask=base_mask)
        except (ValueError, TypeError, OSError) as exc:
            raise ServiceError("invalid_cad", str(exc), str(source)) from exc

    def export_mask_dxf(self, data, path, *, overwrite=False) -> dict[str, Any]:
        """Export mm closed polylines without modifying the design JSON."""
        from .mask_cad import export_mask_dxf
        target = self._path(path)
        if target.suffix.lower() != ".dxf":
            raise ServiceError("invalid_path", "CAD 导出须使用 .dxf 扩展名", str(target))
        try:
            return export_mask_dxf(self._normalize("mask", data), target, overwrite=overwrite)
        except (ValueError, TypeError, OSError) as exc:
            raise ServiceError("cad_export_failed", str(exc), str(target)) from exc

    def export_mask_drawing(self, data, path, *, options=None, overwrite=False) -> dict[str, Any]:
        """Export a dimensional PDF drawing; annotations do not affect physics."""
        from .mask_cad import export_mask_drawing
        target = self._path(path)
        if target.suffix.lower() != ".pdf":
            raise ServiceError("invalid_path", "加工图纸须使用 .pdf 扩展名", str(target))
        try:
            return export_mask_drawing(self._normalize("mask", data), target,
                                       options=options, overwrite=overwrite)
        except (ValueError, TypeError, OSError) as exc:
            raise ServiceError("drawing_export_failed", str(exc), str(target)) from exc

    def relative_reference(self, owner_path: str | os.PathLike[str],
                           target_path: str | os.PathLike[str]) -> str:
        """Make a dependency reference relative to the owning JSON file."""
        owner = self._path(owner_path)
        target = self._path(target_path)
        try:
            return Path(os.path.relpath(target, owner.parent)).as_posix()
        except ValueError:
            # Windows cannot express a relative reference across drives.
            # A desktop design may live on any drive, so retain the absolute
            # path in that case.  Restricted service instances still guard it.
            return target.as_posix()

    def resolve_reference(self, owner_path: str | os.PathLike[str],
                          reference: str | os.PathLike[str]) -> str:
        """Resolve a design-file dependency under the service's path policy."""
        owner = self._path(owner_path)
        return str(self._path(reference, base=owner.parent))

    @staticmethod
    def convert_gear(sun_teeth: int, planet_teeth: int, *,
                     module_mm: float | None = None,
                     center_distance_mm: float | None = None) -> dict[str, float]:
        """Convert the machine's gear module and planet-center distance."""
        from .gear_geometry import (center_distance_from_module,
                                    module_from_center_distance)

        if (module_mm is None) == (center_distance_mm is None):
            raise ServiceError("invalid_gear", "须且只须提供模数或盘心距之一")
        try:
            if module_mm is not None:
                module = float(module_mm)
                center = float(center_distance_from_module(
                    module, int(sun_teeth), int(planet_teeth)))
            else:
                center = float(center_distance_mm)
                module = float(module_from_center_distance(
                    center, int(sun_teeth), int(planet_teeth)))
        except (ValueError, TypeError, OverflowError) as exc:
            raise ServiceError("invalid_gear", str(exc)) from exc
        return {"gear_module_mm": module, "planet_offset_mm": center}

    @staticmethod
    def _kind(kind: str) -> str:
        if kind not in KINDS:
            raise ServiceError("invalid_kind", f"未知方案类型：{kind}")
        return kind

    @staticmethod
    def _settings(value: Settings | Mapping[str, Any]) -> Settings:
        from .simulator import Settings

        if isinstance(value, Settings):
            settings = value
        elif isinstance(value, Mapping):
            try:
                settings = Settings(**value)
            except (TypeError, ValueError) as exc:
                raise ServiceError("invalid_settings", f"计算参数无效：{exc}") from exc
        else:
            raise ServiceError("invalid_settings", "计算参数须为 Settings 或参数字典")
        try:
            settings.validate()
        except (ValueError, TypeError) as exc:
            raise ServiceError("invalid_settings", str(exc)) from exc
        return settings

    @staticmethod
    def _normalize(kind: str, data: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return _VALIDATORS[kind](data)
        except (ValueError, TypeError, KeyError) as exc:
            raise ServiceError("invalid_data", str(exc)) from exc

    def _guard_references(self, kind: str, data: Mapping[str, Any],
                          path: Path) -> None:
        """Check referenced paths before invoking the legacy resolvers."""
        if kind == "machine":
            for child_kind, field in (("source", "sources"), ("mask", "masks")):
                for entry in data[field]:
                    validate_design_path(child_kind, self._path(entry["file"], base=path.parent))
        elif kind == "process":
            machine = validate_design_path("machine", self._path(data["machine_file"], base=path.parent))
            validate_design_path("substrate", self._path(data["substrate_file"], base=path.parent))
            if machine.is_file():
                try:
                    machine_data = read_machine_profile(machine)
                except (OSError, ValueError, TypeError) as exc:
                    raise ServiceError("dependency_error", "机器依赖文件无效",
                                       str(machine), (str(exc),)) from exc
                self._guard_references("machine", machine_data, machine)

    def _dependencies(self, kind: str, data: dict[str, Any],
                      path: Path) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
        if kind not in ("machine", "process"):
            return None, ()
        self._guard_references(kind, data, path)
        if kind == "machine":
            runtime, errors = resolve_machine_profile(data, path.parent)
        else:
            runtime, errors = resolve_process_profile(data, path.parent)
        return runtime, tuple(str(error) for error in errors)

    def dependencies(self, kind: str, data: Mapping[str, Any], *,
                     path: str | os.PathLike[str]) -> dict[str, Any]:
        """Resolve a draft's dependencies relative to its intended file path.

        The returned runtime preserves source and monitor slot order. Invalid
        references are listed in ``errors`` and must block a save.
        """
        self._kind(kind)
        target = self._path(path)
        validate_design_path(kind, target)
        normalized = self._normalize(kind, data)
        try:
            runtime, errors = self._dependencies(kind, normalized, target)
        except ServiceError as exc:
            runtime, errors = None, (str(exc),)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            runtime, errors = None, (str(exc),)
        return {"kind": kind, "path": str(target), "data": normalized,
                "runtime": runtime, "errors": list(errors), "ok": not errors}

    def validate(self, kind: str, data: Mapping[str, Any] | None = None, *,
                 path: str | os.PathLike[str] | None = None) -> ValidationResult:
        """Validate a draft, or load and validate an existing file by ``path``.

        ``data=None`` requires ``path`` and checks the saved file plus its
        dependency graph. With explicit data, path is only the draft's intended
        location for resolving relative dependencies; it need not exist.
        """
        self._kind(kind)
        try:
            if data is None:
                if path is None:
                    raise ServiceError("missing_design", "请提供待校验设计 data 或已保存方案文件 path")
                document = self.load(kind, path)
                return ValidationResult(kind, document.valid, document.data, document.issues)
            normalized = self._normalize(kind, data)
            issues: tuple[str, ...] = ()
            if path is not None:
                target = validate_design_path(kind, self._path(path))
                _, issues = self._dependencies(kind, normalized, target)
            return ValidationResult(kind, not issues, normalized, issues)
        except ServiceError as exc:
            return ValidationResult(kind, False, None, (str(exc),))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            return ValidationResult(kind, False, None, (str(exc),))

    def load(self, kind: str, path: str | os.PathLike[str]) -> Document:
        """Load one design; broken dependencies remain visible in ``issues``."""
        self._kind(kind)
        if isinstance(path, Mapping) and path.get("kind") == "coat_sim.input_snapshot":
            if kind != "process":
                raise ValueError("工艺快照只可作为 process 输入")
            from .input_snapshot import runtime_from_snapshot
            runtime = runtime_from_snapshot(path)
            return Document(kind, str(path["process_path"]), runtime["profile"], (), runtime)
        target = self._path(path)
        if target.suffix.lower() == ".coatasset":
            content = self.read_simulation_asset(target, detail="full")
            snapshot = content["configuration"].get("input_snapshot")
            matches = [row for row in content.get("designs", []) if row["kind"] == kind]
            if len(matches) != 1:
                raise ValueError("资产包中该类型设计不唯一，请在包内设计选择器中选择后导出打开")
            runtime = self.load("process", snapshot).runtime if kind == "process" else None
            return Document(kind, str(target), matches[0]["data"], (), runtime)
        try:
            validate_design_path(kind, target)
            data = _READERS[kind](target)
        except FileNotFoundError as exc:
            raise ServiceError("not_found", "方案文件不存在", str(target)) from exc
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
            raise ServiceError("invalid_file", f"无法加载{kind}方案：{exc}",
                               str(target)) from exc
        try:
            runtime, issues = self._dependencies(kind, data, target)
        except ServiceError as exc:
            runtime, issues = None, (str(exc),)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            runtime, issues = None, (str(exc),)
        return Document(kind, str(target), data, issues, runtime)

    def save(self, kind: str, path: str | os.PathLike[str],
             data: Mapping[str, Any], *, overwrite: bool = False) -> Document:
        """Save normalized JSON. Existing files require ``overwrite=True``."""
        self._kind(kind)
        target = self._path(path)
        try:
            target = default_design_path(kind, target)
        except ValueError as exc:
            raise ServiceError("invalid_path", str(exc), str(target)) from exc
        if not target.parent.is_dir():
            raise ServiceError("missing_directory", "目标目录不存在", str(target.parent))
        if target.exists() and not overwrite:
            raise ServiceError("already_exists", "目标文件已存在，请明确允许覆盖", str(target))
        if target.exists():
            try:
                _READERS[kind](target)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise ServiceError("incompatible_overwrite", "已有文件无法读取或与当前设计类型不符，原文件未覆盖；请使用其他文件名", str(target)) from exc
        normalized = self._normalize(kind, data)
        try:
            runtime, issues = self._dependencies(kind, normalized, target)
        except ServiceError:
            raise
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ServiceError("dependency_error", str(exc), str(target)) from exc
        if issues:
            raise ServiceError("dependency_error", "依赖文件无效，无法保存",
                               str(target), issues)
        if kind == "process" and runtime is not None:
            normalized = runtime["profile"]
        content = json.dumps(normalized, ensure_ascii=False, indent=2) + "\n"
        temporary: Path | None = None
        try:
            # Publish only a complete, flushed document. A hard link provides
            # atomic no-clobber creation on POSIX and normal Windows NTFS. If
            # the filesystem cannot link, fail safely instead of risking an
            # existing target with an unsafe rename fallback.
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                             prefix=".coat-", dir=target.parent,
                                             encoding="utf-8", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if overwrite:
                replace_with_retry(temporary, target)
            else:
                os.link(temporary, target)
        except FileExistsError as exc:
            raise ServiceError("already_exists", "目标文件已存在，请明确允许覆盖",
                               str(target)) from exc
        except OSError as exc:
            raise ServiceError("write_error", f"无法保存方案：{exc}", str(target)) from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return Document(kind, str(target), normalized, (), runtime)

    def render_design(self, kind: str, path: str | os.PathLike[str]) -> dict[str, Any]:
        """Deterministic local geometry/detail artifact; no large document reply."""
        from coat_sim.core.design_visualization import render_design_visualization, render_process_detail_html
        from coat_sim.core.data_files import data_path
        target = data_path(str(path), root_allowed=False)
        document = self.load(kind, target)
        if kind == "process":
            return render_process_detail_html(self, document.data, path=str(target))
        return render_design_visualization(self, kind, document.data, path=str(target))

    def preview(self, kind: str, data: Mapping[str, Any] | str | os.PathLike[str], *,
                path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
        """Return serializable design geometry without GUI-specific objects."""
        if isinstance(data, (str, os.PathLike)):
            document = self.load(kind, data)
            normalized = document.data
            issues = document.issues
            runtime = document.runtime
        else:
            result = self.validate(kind, data, path=path)
            if result.data is None:
                raise ServiceError("invalid_data", "预览数据无效", details=result.issues)
            normalized = result.data
            issues = result.issues
            runtime = (self._dependencies(kind, normalized, self._path(path))[0]
                       if path is not None and not issues else None)
        output: dict[str, Any] = {"kind": kind, "data": normalized,
                                  "issues": list(issues)}
        if kind == "substrate":
            radius = effective_radius_mm(normalized)
            output["radius_mm"] = radius
            outline = normalized["outline"]
            if outline["kind"] in ("polygon", "curve", "rectangle"):
                output["outline_points_xy"] = [list(point)
                                                for point in outline_polygon_points(outline)]
            output["surface_profile_xy"] = self.surface_profile(
                normalized["surface"], radius)
        elif kind == "machine" and runtime is not None:
            output["sources"] = runtime["sources"]
            output["masks"] = runtime["masks"]
            output["monitors"] = runtime["monitors"]
        elif kind == "process" and runtime is not None:
            output["machine"] = runtime.get("machine")
            output["substrate"] = runtime.get("substrate_profile")
        return output

    @staticmethod
    def sample_substrate_curve(control_points: Sequence[Sequence[float]]) -> list[list[float]]:
        """Expose the existing Catmull–Rom outline sampler for editors."""
        try:
            points = sample_curve_points(control_points)
            return [[float(x), float(y)] for x, y in points]
        except (ValueError, TypeError, IndexError) as exc:
            raise ServiceError("invalid_curve", str(exc)) from exc

    def source_sample_points(self, source: Mapping[str, Any] | str | os.PathLike[str]
                             ) -> dict[str, Any]:
        """Expose source emission quadrature nodes and weights in local XY mm."""
        from .simulator import Settings

        if isinstance(source, (str, os.PathLike)):
            profile = self.load("source", source).data
        else:
            profile = self._normalize("source", source)
        local_settings = replace(Settings(), source_x_mm=0.0, source_y_mm=0.0)
        try:
            positions, weights = source_samples(local_settings, profile)
        except (ValueError, TypeError) as exc:
            raise ServiceError("invalid_source", str(exc)) from exc
        return {"points_xy": positions[:, :2].tolist(), "weights": weights.tolist(),
                "count": len(weights), "unit": "mm", "coordinate_system": "source_local"}

    def source_material_reference(self, source):
        """Read-only literature-reference report, separate from deposition simulation.

        A saved source or in-memory source draft carries its own immutable
        reference data and calculator inputs. Nothing changes process rates.
        """
        from .material_physics import evaluate_source_thermochemistry
        profile = (self.load("source", source).data if isinstance(source, (str, os.PathLike))
                   else self._normalize("source", source))
        try:
            return evaluate_source_thermochemistry(profile)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise ServiceError("invalid_material_reference", str(exc)) from exc

    @staticmethod
    def surface_profile(surface: Mapping[str, Any], radius_mm: float,
                        count: int = 181) -> list[list[float]]:
        """Sample the existing height model along the substrate X diameter."""
        import numpy as np

        if type(count) is not int or not 3 <= count <= 2001:
            raise ServiceError("invalid_sampling", "面型采样点数须在 3–2001 之间")
        try:
            normalized = validate_substrate_surface(surface, radius_mm)
            x = np.linspace(-float(radius_mm), float(radius_mm), count)
            heights = surface_height_and_normal(x, np.zeros_like(x),
                                                float(radius_mm), normalized)[0]
            if not np.isfinite(heights).all():
                raise ValueError("面型高度超出有效范围")
            return [[float(px), float(height)] for px, height in zip(x, heights)]
        except (ValueError, TypeError, OverflowError) as exc:
            raise ServiceError("invalid_surface", str(exc)) from exc

    def _machine_context(self, settings: Settings, machine_path: str | os.PathLike[str],
                         source_slot: int, substrate_path: str | os.PathLike[str] | None
                         ) -> tuple[Settings, dict[str, Any], dict[str, Any]]:
        document = self.load("machine", machine_path)
        if document.issues or document.runtime is None:
            raise ServiceError("dependency_error", "机器依赖无效",
                               document.path, document.issues)
        machine = document.runtime
        if type(source_slot) is not int or not 1 <= source_slot <= len(machine["sources"]):
            raise ServiceError("invalid_slot", "蒸发源位次无效", document.path)
        source = machine["sources"][source_slot - 1]
        profile = source.get("profile")
        if profile is None:
            raise ServiceError("dependency_error", "蒸发源文件无效", document.path)
        geometry = {key: machine[key] for key in (
            "carrier_x_mm", "carrier_y_mm", "planet_offset_mm", "substrate_z_mm",
            "planet_tilt_deg",
            "sun_teeth", "planet_teeth", "gear_module_mm")}
        settings = replace(settings, **geometry, source_x_mm=source["x_mm"],
                           source_y_mm=source["y_mm"], source_z_mm=source["z_mm"],
                           source_exponent=profile["source_exponent"])
        if substrate_path is not None:
            substrate = self.load("substrate", substrate_path).data
            settings = replace(settings, substrate_profile=substrate,
                               substrate_radius_mm=effective_radius_mm(substrate))
        try:
            settings.validate()
        except (ValueError, TypeError) as exc:
            raise ServiceError("invalid_settings", str(exc)) from exc
        return settings, machine, profile

    def simulate_single(self, settings: Settings | Mapping[str, Any], *,
                        machine_path: str | os.PathLike[str] | None = None,
                        source_slot: int = 1,
                        substrate_path: str | os.PathLike[str] | None = None,
                        source_path: str | os.PathLike[str] | None = None,
                        mask_paths: Sequence[str | os.PathLike[str]] = (),
                        backend: str = "自动", cancel_event: Any = None) -> dict[str, Any]:
        """Return the legacy normalized preview dictionary.

        Shape: ``field`` is a 2-D NumPy array; ``x`` and ``y`` are axis arrays
        in mm; ``device`` names the actual backend. Other keys are preserved
        from :func:`simulator.simulate` for metrics and PDF export.
        """
        from .simulator import simulate

        local = self._settings(settings)
        mask: list[dict[str, Any]] = []
        profile = None
        if machine_path is not None:
            local, machine, profile = self._machine_context(
                local, machine_path, source_slot, substrate_path)
            mask = [entry["profile"] for entry in machine["masks"]]
        elif substrate_path is not None:
            substrate = self.load("substrate", substrate_path).data
            local = replace(local, substrate_profile=substrate,
                            substrate_radius_mm=effective_radius_mm(substrate))
        if source_path is not None:
            profile = self.load("source", source_path).data
            local = replace(local, source_exponent=profile["source_exponent"])
        mask.extend(self.load("mask", path).data for path in mask_paths)
        try:
            from .isolated_compute import simulate_single_isolated
            result = simulate_single_isolated(
                local, mask=mask or None, backend=backend,
                cancel_event=cancel_event, source_profile=profile)
            result["requested_settings"] = asdict(self._settings(settings))
            result["effective_settings"] = asdict(local)
            result["settings_scope"] = "single_source_effective_parameters"
            return result
        except (InterruptedError, TimeoutError):
            raise
        except MemoryError as exc:
            raise ServiceError("memory_exhausted", str(exc) or "内存不足，隔离计算已失败") from exc
        except (ValueError, TypeError, RuntimeError, OSError) as exc:
            raise ServiceError("simulation_error", str(exc)) from exc

    def simulate_recipe(self, settings: Settings | Mapping[str, Any], *,
                        process_path: str | os.PathLike[str],
                        backend: str = "自动", cancel_event: Any = None,
                        fast_cycle: bool = False,
                        fast_cycle_residual_percent: float = 5.0,
                        algorithm: str = "fast", algorithm_options: Mapping[str, Any] | None = None,
                        progress: Any = None,
                        include_layer_fields: bool = False,
                        layer_field_spool_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
        """Return the legacy process dictionary after dependency validation.

        Shape: ``field_nm`` is a 2-D NumPy array of thickness in nm; ``x`` and
        ``y`` are axis arrays in mm; ``device`` names the actual backend.
        Other keys are preserved from :func:`process_simulator.simulate_process`.
        """
        from .process_simulator import simulate_process

        local = self._settings(settings)
        try:
            algorithm_options = recipe_algorithm_options(algorithm, algorithm_options, backend, local.carrier_rpm)
        except (ValueError, TypeError) as exc:
            raise ServiceError("simulation_error", str(exc), str(process_path)) from exc
        document = self.load("process", process_path)
        if document.issues:
            raise ServiceError("dependency_error", "工艺依赖无效",
                               document.path, document.issues)
        try:
            from .isolated_compute import simulate_recipe_isolated
            result = simulate_recipe_isolated(
                local, document.runtime, algorithm=algorithm,
                options=algorithm_options, backend=backend,
                cancel_event=cancel_event, fast_cycle=fast_cycle,
                fast_cycle_residual_percent=fast_cycle_residual_percent,
                capture_layer_fields=include_layer_fields,
                layer_field_spool_path=layer_field_spool_path,
                progress=progress)
            result.update(execution_algorithm=algorithm, execution_algorithm_options=dict(algorithm_options))
            ignored = ({"max_evaluations", "tolerance_percent", "rpm_scale_bounds"}
                       if algorithm == "fast" else {"rpm_scale_bounds"}
                       if algorithm == "reference" else {"tolerance_percent"})
            warnings = [f"{key} is retained for legacy compatibility but does not affect {algorithm} execution"
                        for key in sorted(ignored & algorithm_options.keys())]
            if algorithm == "fast" and "max_evaluations" in algorithm_options:
                warnings.append("fast always performs one evaluation")
            if warnings:
                result["execution_option_warnings"] = warnings
            return result
        except (InterruptedError, TimeoutError):
            raise
        except MemoryError as exc:
            raise ServiceError("memory_exhausted", str(exc) or "内存不足，隔离计算已失败") from exc
        except (ValueError, TypeError, RuntimeError, OSError) as exc:
            raise ServiceError("simulation_error", str(exc), document.path) from exc

    def mask_weights(self, settings: Settings | Mapping[str, Any], *,
                     machine_path: str | os.PathLike[str], source_slot: int = 1,
                     mask_slot: int = 1, mask_data: Mapping[str, Any] | None = None,
                     substrate_path: str | os.PathLike[str] | None = None,
                     radii_mm: Sequence[float] | None = None) -> dict[str, Any]:
        """Estimate blockage for an installed or unsaved correction board."""
        from .mask_analysis import correction_board_blocking_weights

        local, machine, profile = self._machine_context(
            self._settings(settings), machine_path, source_slot, substrate_path)
        if mask_data is None:
            if type(mask_slot) is not int or not 1 <= mask_slot <= len(machine["masks"]):
                raise ServiceError("invalid_slot", "修正板位次无效", str(machine_path))
            board = machine["masks"][mask_slot - 1]["profile"]
        else:
            try:
                board = validate_mask_profile(dict(mask_data),
                                              substrate_z_mm=local.substrate_z_mm)
            except (ValueError, TypeError, KeyError) as exc:
                raise ServiceError("invalid_data", str(exc)) from exc
        try:
            result = correction_board_blocking_weights(
                local, board, radii_mm=radii_mm, source_profile=profile)
        except (ValueError, TypeError, RuntimeError) as exc:
            raise ServiceError("analysis_error", str(exc)) from exc
        return {key: value.tolist() if hasattr(value, "tolist") else value
                for key, value in result.items()}

    def mask_field(self, settings: Settings | Mapping[str, Any], *,
                   machine_path: str | os.PathLike[str], source_slot: int = 1,
                   mask_slot: int = 1, mask_data: Mapping[str, Any] | None = None,
                   substrate_path: str | os.PathLike[str] | None = None,
                   ring_radii_mm: Sequence[float] | None = None) -> dict[str, Any]:
        """Preview board blockage by material-point angle and across the disk."""
        from .mask_analysis import correction_board_blocking_field

        local, machine, profile = self._machine_context(
            self._settings(settings), machine_path, source_slot, substrate_path)
        if mask_data is None:
            if type(mask_slot) is not int or not 1 <= mask_slot <= len(machine["masks"]):
                raise ServiceError("invalid_slot", "修正板位次无效", str(machine_path))
            board = machine["masks"][mask_slot - 1]["profile"]
        else:
            try:
                board = validate_mask_profile(dict(mask_data),
                                              substrate_z_mm=local.substrate_z_mm)
            except (ValueError, TypeError, KeyError) as exc:
                raise ServiceError("invalid_data", str(exc)) from exc
        try:
            result = correction_board_blocking_field(
                local, board, source_profile=profile,
                ring_radii_mm=ring_radii_mm)
        except (ValueError, TypeError, RuntimeError) as exc:
            raise ServiceError("analysis_error", str(exc)) from exc
        def json_value(value: Any) -> Any:
            if hasattr(value, "tolist"):
                return json_value(value.tolist())
            if isinstance(value, list):
                return [json_value(part) for part in value]
            if isinstance(value, float) and not math.isfinite(value):
                return None
            return value

        return {key: json_value(value) for key, value in result.items()}

    def point_weights(self, settings: Settings | Mapping[str, Any], *,
                      machine_path: str | os.PathLike[str],
                      mask_z_mm: float, point_xy_mm: Sequence[float],
                      patch_radius_mm: float, source_slot: int = 1,
                      substrate_path: str | os.PathLike[str] | None = None,
                      radii_mm: Sequence[float] | None = None) -> dict[str, Any]:
        """Estimate one board-plane patch's potential blockage by disk radius."""
        from .mask_analysis import radial_blocking_weights

        local, _, profile = self._machine_context(
            self._settings(settings), machine_path, source_slot, substrate_path)
        try:
            result = radial_blocking_weights(
                local, mask_z_mm, point_xy_mm, patch_radius_mm,
                radii_mm=radii_mm, source_profile=profile)
        except (ValueError, TypeError, RuntimeError) as exc:
            raise ServiceError("analysis_error", str(exc)) from exc
        return {key: value.tolist() if hasattr(value, "tolist") else value
                for key, value in result.items()}

    def source_trajectory(self, settings: Settings | Mapping[str, Any], *,
                          machine_path: str | os.PathLike[str],
                          mask_z_mm: float, source_slot: int = 1) -> dict[str, Any]:
        """Show one complete planet-center obstruction ring at a board plane."""
        from .mask_analysis import center_cut_trajectory

        local, _, _ = self._machine_context(
            self._settings(settings), machine_path, source_slot, None)
        # A one-revolution view is geometric, including when current rpm is 0.
        ring_settings = replace(local,
                                carrier_rpm=-60.0 if local.carrier_rpm < 0 else 60.0,
                                duration_s=1.0)
        try:
            result = center_cut_trajectory(ring_settings, mask_z_mm)
        except (ValueError, TypeError) as exc:
            raise ServiceError("analysis_error", str(exc)) from exc
        return {key: value.tolist() if hasattr(value, "tolist") else value
                for key, value in result.items()}

    def export_result(self, path: str | os.PathLike[str], *,
                      result: Mapping[str, Any], settings: Settings | Mapping[str, Any],
                      machine: Mapping[str, Any] | None = None,
                      process: Mapping[str, Any] | None = None,
                      elapsed_s: float, requested_device: str,
                      overwrite: bool = False, sections: Sequence[str] | None = None) -> str:
        """Export PDF; overwriting an existing report must be explicit."""
        from .result_pdf import export_result_pdf

        target = self._path(path)
        if target.suffix.lower() != ".pdf":
            raise ServiceError("invalid_path", "结果文件须使用 .pdf 扩展名", str(target))
        if not target.parent.is_dir():
            raise ServiceError("missing_directory", "目标目录不存在", str(target.parent))
        if target.exists() and not overwrite:
            raise ServiceError("already_exists", "结果文件已存在，请明确允许覆盖", str(target))
        try:
            return export_result_pdf(target, result=result,
                                     settings=self._settings(settings),
                                     machine=machine, process=process,
                                     elapsed_s=elapsed_s,
                                     requested_device=requested_device, sections=sections)
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            raise ServiceError("export_error", str(exc), str(target)) from exc

    def work_index(self, settings: Settings | Mapping[str, Any], *,
                   process_path: str | os.PathLike[str] | None = None,
                   fast_cycle: bool = False,
                   fast_cycle_residual_percent: float = 5.0,
                   machine_path: str | os.PathLike[str] | None = None,
                   source_slot: int = 1,
                   substrate_path: str | os.PathLike[str] | None = None,
                   source_path: str | os.PathLike[str] | None = None,
                   mask_paths: Sequence[str | os.PathLike[str]] = ()) -> float:
        """Use the existing workload formula for a single source or recipe."""
        from .simulator import workload

        local = self._settings(settings)
        if process_path is not None:
            from .process_simulator import process_sampling_plan
            from .source_profiles import source_rate_cache_key

            document = self.load("process", process_path)
            if document.issues or document.runtime is None:
                raise ServiceError("dependency_error", "工艺依赖无效",
                                   document.path, document.issues)
            machine = document.runtime["machine"]
            substrate = document.runtime["substrate_profile"]
            geometry = {key: machine[key] for key in (
                "carrier_x_mm", "carrier_y_mm", "planet_offset_mm", "substrate_z_mm",
                "planet_tilt_deg", "sun_teeth", "planet_teeth", "gear_module_mm")}
            local = replace(local, **geometry, substrate_profile=substrate,
                            substrate_radius_mm=effective_radius_mm(substrate))
            try:
                plan = process_sampling_plan(
                    local, document.data, fast_cycle=fast_cycle,
                    fast_cycle_residual_percent=fast_cycle_residual_percent)
            except (ValueError, TypeError) as exc:
                raise ServiceError("invalid_sampling", str(exc), document.path) from exc
            total_work = 0.0
            cached_cycle_slots = set()
            for step, sample in zip(document.data["steps"], plan["steps"]):
                source_slot = step["source_slot"]
                mask_slots = step_mask_slots(step, machine)
                masks = [machine["masks"][slot]["profile"] for slot in mask_slots]
                source = machine["sources"][source_slot - 1]
                profile = source["profile"]
                cycle_key = (source_slot, mask_slots, source_rate_cache_key(profile, step["rate_nm_s"]))
                if sample["algorithm"] == "cycle":
                    if cycle_key in cached_cycle_slots:
                        continue
                    cached_cycle_slots.add(cycle_key)
                total_work += workload(
                    replace(local, time_steps=sample["time_steps"],
                            source_rate_nm_s=step["rate_nm_s"],
                            source_x_mm=source["x_mm"], source_y_mm=source["y_mm"],
                            source_z_mm=source["z_mm"], source_exponent=profile["source_exponent"]),
                    masks, profile)
            return float(total_work)
        if machine_path is not None:
            local, machine, profile = self._machine_context(
                local, machine_path, source_slot, substrate_path)
            masks = [entry["profile"] for entry in machine["masks"]]
            if source_path is not None:
                profile = self.load("source", source_path).data
            masks.extend(self.load("mask", path).data for path in mask_paths)
            return float(workload(local, masks, profile))
        if substrate_path is not None:
            substrate = self.load("substrate", substrate_path).data
            local = replace(local, substrate_profile=substrate,
                            substrate_radius_mm=effective_radius_mm(substrate))
        profile = self.load("source", source_path).data if source_path is not None else None
        masks = [self.load("mask", path).data for path in mask_paths]
        return float(workload(local, masks or None, profile))

    def freeze_process(self, data, *, base_dir=None):
        """Capture fresh typed dependency contents without creating design copies."""
        from .input_snapshot import freeze_process
        return freeze_process(self, data, base_dir or self.root)

    def snapshot_process(self, data: Mapping[str, Any], *,
                         base_dir: str | os.PathLike[str] | None = None,
                         output_directory: str | os.PathLike[str] | None = None) -> dict[str, Any]:
        """Copy the full dependency graph into a new, self-contained directory."""
        from copy import deepcopy
        from uuid import uuid4
        base = self._path(base_dir or self.root)
        recipe = validate_process_profile(data)
        self._guard_references("process", recipe, base / "process.cproc")
        runtime, errors = resolve_process_profile(recipe, base_dir=base)
        if errors or runtime is None:
            raise ServiceError("dependency_error", "工艺依赖无效", details=tuple(errors))
        parent = self._path(output_directory or self.root / "data" / "tmp" / "optimizations")
        parent.mkdir(parents=True, exist_ok=True)
        destination = parent / uuid4().hex
        destination.mkdir()
        copied = {}

        def clone(kind, path):
            original = self._path(path)
            key = (kind, str(original))
            if key in copied:
                return copied[key]
            document = self.load(kind, original)
            if document.issues:
                raise ServiceError("dependency_error", "设计依赖无效", document.path, document.issues)
            value = deepcopy(document.data)
            target = destination / f"{kind}-{len(copied) + 1}{design_extension(kind)}"
            copied[key] = target
            if kind == "machine":
                for entry in value["sources"]:
                    child = clone("source", self._path(entry["file"], base=original.parent))
                    entry["file"] = child.name
                # Board placements must be independent even if the original
                # machine references the same mask file at both positions.
                for index, entry in enumerate(value["masks"]):
                    mask = self.load("mask", self._path(entry["file"], base=original.parent))
                    child = destination / f"mask-placement-{index + 1}.cmsk"
                    self.save("mask", child, mask.data)
                    # Preserve loaded/fixed geometry and metadata byte-for-byte.
                    child.write_bytes(Path(mask.path).read_bytes())
                    entry["file"] = child.name
            self.save(kind, target, value)
            return target

        machine_path = clone("machine", runtime["machine_path"])
        substrate_path = clone("substrate", runtime["substrate_path"])
        recipe = deepcopy(runtime["profile"])
        recipe["machine_file"] = machine_path.name
        recipe["substrate_file"] = substrate_path.name
        process_path = destination / "process.cproc"
        self.save("process", process_path, recipe)
        machine = self.load("machine", machine_path).data
        return {"directory": str(destination), "process_path": str(process_path),
                "machine_path": str(machine_path), "substrate_path": str(substrate_path),
                "mask_paths": [str(destination / entry["file"]) for entry in machine["masks"]]}

    def mask_optimization_options(self, settings, runtime, options=None) -> dict[str, Any]:
        """Validate public options, then make omitted geometric defaults explicit."""
        from .schemas import MaskOptimizationOptions
        from .mask_optimization import _options
        local = self._settings(settings)
        supplied = dict(options or {})
        supplied.pop("source_groups", None)
        supplied.pop("new_mask_slots", None)
        value = MaskOptimizationOptions.model_validate(supplied).model_dump(exclude_none=True)
        from .mask_optimization_workflow import source_board_groups, prepare_optimization_runtime
        runtime, prepared = prepare_optimization_runtime(runtime, value, local)
        value.update({key: item for key, item in prepared.items() if item is not None})
        machine = runtime["machine"]
        recipe = runtime["profile"]
        try:
            groups = source_board_groups(machine, recipe, value)
        except ValueError as exc:
            raise ServiceError("invalid_source_mask_group", str(exc)) from exc
        if value["objective"]["kind"] == "target_map" and len(groups) > 1:
            raise ServiceError("target_map_group_required", "目标厚度图优化须明确一个source_slot；不同源不能共用一份未分配的目标图")
        value["mask_slots"] = sorted({slot for group in groups for slot in group["mask_slots"]})
        if "mask_bounds" not in value:
            bounds = []
            for slot in value["mask_slots"]:
                if not 0 <= slot < len(machine["masks"]):
                    raise ServiceError("invalid_options", "优化板位次不在机器范围内")
                profile = machine["masks"][slot].get("profile")
                if profile is None:
                    raise ServiceError("dependency_error", "优化修正板依赖无效")
                points = profile["outer"]
                bounds.append({"slot": slot, "x_min_mm": min(p[0] for p in points),
                               "x_max_mm": max(p[0] for p in points),
                               "y_min_mm": min(p[1] for p in points),
                               "y_max_mm": max(p[1] for p in points)})
            value["mask_bounds"] = bounds
        value.setdefault("search_grid_size", min(81, local.grid_size))
        _options(local, machine, value)
        value["source_groups"] = groups
        return value

    def estimate_mask_optimization(self, settings, process_path, *, options=None) -> dict[str, Any]:
        """Conservative budget estimate; running jobs refine it with measured evaluations."""
        local = self._settings(settings)
        document = self.load("process", process_path)
        if document.issues or document.runtime is None:
            raise ServiceError("dependency_error", "工艺依赖无效", document.path, document.issues)
        config = self.mask_optimization_options(local, document.runtime, options)
        from .mask_optimization_workflow import prepare_optimization_runtime, optimization_work_index
        runtime, _ = prepare_optimization_runtime(document.runtime, config, local)
        self._refresh_history()
        def estimate_for(grid):
            index = optimization_work_index(replace(local, grid_size=grid), runtime, config)
            measured = self.history.estimate(index, config["backend"], algorithm="fast_cycle" if config["fast_cycle"] else "precise")
            seconds, device, count = measured if measured is not None else (None, None, 0)
            return {"work_index": index, "estimated_s": float(seconds) if seconds is not None else None,
                    "actual_device": device, "sample_count": int(count)}
        coarse = estimate_for(config["search_grid_size"])
        final = estimate_for(local.grid_size)
        from .mask_optimization import SEARCH_SHORTLIST_SIZE
        full_verifications = 2 + ((SEARCH_SHORTLIST_SIZE + 1) * len(config["source_groups"])
                                 if config["search_grid_size"] != local.grid_size else 0)
        seconds = (coarse["estimated_s"] * config["max_evaluations"] * len(config["source_groups"]) + final["estimated_s"] * full_verifications
                   if coarse["estimated_s"] is not None and final["estimated_s"] is not None else None)
        from .compute_resources import estimate_memory
        memory = estimate_memory(local, layer_count=len(document.data.get("steps", [])))
        return {"estimated_s": seconds, **memory,
                "formatted": format_elapsed(seconds) if seconds is not None else None,
                "sample_count": coarse["sample_count"], "actual_device": coarse["actual_device"],
                "max_evaluations": config["max_evaluations"] * len(config["source_groups"]),
                "source_groups": config["source_groups"],
                "work_index": coarse["work_index"] * config["max_evaluations"] * len(config["source_groups"]) + final["work_index"] * full_verifications,
                "full_grid_verifications": full_verifications,
                "search_grid_size": config["search_grid_size"],
                "basis": "按含新板/固定板的完整工艺与候选最大轮廓采样估算；搜索预算加前四候选/原板原网格验证及至多两次全工艺检查，运行时以实测更新",
                "options": config}

    def optimize_masks(self, settings, process_path, *, options=None,
                       progress_callback=None, cancel_event=None, pending_only=True,
                       checkpoint_path=None) -> dict[str, Any]:
        """Run a complete optimization in one disposable, memory-bounded worker."""
        from .isolated_compute import optimize_masks_isolated
        local = self._settings(settings)
        if pending_only and not isinstance(process_path, Mapping):
            document = self.load("process", process_path)
            if document.issues or document.runtime is None:
                raise ServiceError("dependency_error", "工艺依赖无效", document.path, document.issues)
            process_path = (self.load_simulation_asset(document.path)["input_snapshot"]
                            if Path(document.path).suffix.lower() == ".coatasset" else
                            self.freeze_process(document.data, base_dir=Path(document.path).parent))
        try:
            return optimize_masks_isolated(local, process_path, root=self.root,
                restrict_to_root=self.restrict_to_root, options=options,
                pending_only=pending_only, cancel_event=cancel_event, progress=progress_callback,
                checkpoint_path=checkpoint_path)
        except (InterruptedError, TimeoutError):
            raise
        except MemoryError as exc:
            raise ServiceError("memory_exhausted", str(exc) or "内存不足，隔离优化已失败") from exc
        except (ValueError, TypeError, RuntimeError, OSError) as exc:
            raise ServiceError("optimization_error", str(exc)) from exc

    def _optimize_masks_worker(self, settings, process_path, *, options=None,
                       progress_callback=None, cancel_event=None, pending_only=True,
                       checkpoint_path=None) -> dict[str, Any]:
        """Optimize frozen inputs and return new mask designs awaiting explicit save."""
        from .mask_optimization import optimize_correction_boards, _quality
        local = self._settings(settings)
        document = self.load("process", process_path)
        if document.issues or document.runtime is None:
            raise ServiceError("dependency_error", "工艺依赖无效", document.path, document.issues)
        if pending_only and not isinstance(process_path, Mapping):
            process_path = (self.load_simulation_asset(document.path)["input_snapshot"]
                            if Path(document.path).suffix.lower() == ".coatasset" else
                            self.freeze_process(document.data, base_dir=Path(document.path).parent))
            document = self.load("process", process_path)
        config = self.mask_optimization_options(local, document.runtime, options)
        from copy import deepcopy
        import time
        import math
        from .process_simulator import simulate_process
        original_machine = deepcopy(document.runtime["machine"])
        frozen_inputs = process_path if pending_only and isinstance(process_path, Mapping) else None
        from .mask_optimization_workflow import prepare_optimization_runtime
        optimization_runtime, _ = prepare_optimization_runtime(document.runtime, config, local)
        working_machine = deepcopy(optimization_runtime["machine"])
        optimization_recipe = optimization_runtime["profile"]
        initialization = config["variable_mode"] if config["mode"] == "fixed_plus_variable" else config["mode"]
        objective_kind = config["objective"]["kind"]
        group_reports = []
        candidate_masks = {}
        original_simulation = None
        latest_progress = {}
        started = time.perf_counter()

        def checkpoint_candidate(candidate_simulation):
            if not checkpoint_path or not pending_only or not frozen_inputs or not candidate_masks:
                return
            from dataclasses import asdict
            from uuid import uuid4
            from .input_snapshot import candidate_snapshot
            from .result_summary import compact_result_metadata
            from .simulation_assets import plain
            from coat_sim.scheduler.result_capability import (
                is_best_candidate_result, load_best_candidate_checkpoint,
            )

            score = _quality(candidate_simulation, "uniformity", None)["score"]
            if not isinstance(score, (int, float)) or not math.isfinite(float(score)):
                return
            pointer = Path(checkpoint_path).resolve()
            if not pointer.is_relative_to(Path(self.root).resolve()):
                raise ValueError("optimization checkpoint escaped its workspace")
            previous, _ = load_best_candidate_checkpoint(Path(self.root), pointer.parent.name)
            if (is_best_candidate_result(previous)
                    and previous["partial_result"]["quality"]["metric"] == "full_recipe_uniformity_score"
                    and float(previous["partial_result"]["quality"]["value"]) <= float(score)):
                return

            candidate_input = candidate_snapshot(
                frozen_inputs, candidate_masks, optimization_recipe["steps"])
            candidate_input["parameters"] = {
                "settings": asdict(local), "options": deepcopy(config),
                "process_path": candidate_input["process_path"],
                "backend": config["backend"], "fast_cycle": config["fast_cycle"],
                "fast_cycle_residual_percent": config["fast_cycle_residual_percent"],
            }
            candidate_document = self.load("process", candidate_input)
            if candidate_document.issues or candidate_document.runtime is None:
                raise ValueError("verified candidate snapshot is invalid")
            candidate_run_id = uuid4().hex
            asset = self.prepare_simulation_asset(
                result=candidate_simulation, settings=local,
                machine=candidate_document.runtime["machine"],
                process=candidate_document.runtime["profile"],
                elapsed_s=time.perf_counter() - started,
                requested_device=config["backend"], run_id=candidate_run_id,
                input_snapshot=candidate_input, record_history=False)
            field_name = "field_nm" if "field_nm" in candidate_simulation else "field"
            result = {
                "run_id": candidate_run_id,
                "result_file": asset["result_file"],
                "field_name": field_name,
                "field_unit": "nm" if field_name == "field_nm" else "relative",
                "field_shape": list(candidate_simulation[field_name].shape),
                "elapsed_s": time.perf_counter() - started,
                "metadata": plain(compact_result_metadata(candidate_simulation)),
                "files": asset["files"], "asset": asset,
                "temporary_asset": {
                    "run_id": pointer.parent.name,
                    "directory": asset["directory"],
                    "save_requires_user_confirmation": True,
                },
                "pending_masks": [
                    {"slot": slot, "profile": deepcopy(profile)}
                    for slot, profile in sorted(candidate_masks.items())
                ],
                "partial_result": {
                    "schema_version": 1, "kind": "best_candidate",
                    "best_so_far": True, "candidate_verified": True,
                    "quality": {"metric": "full_recipe_uniformity_score", "value": float(score)},
                },
            }
            loaded = self.load_simulation_asset(asset["temporary_bundle_path"])
            if (loaded.get("run_id") != candidate_run_id
                    or not isinstance(loaded.get("input_snapshot"), Mapping)
                    or not loaded["input_snapshot"].get("files")):
                raise ValueError("candidate bundle failed its snapshot validation")
            pointer.parent.mkdir(parents=True, exist_ok=True)
            temporary = pointer.with_name(pointer.name + "." + uuid4().hex + ".tmp")
            try:
                temporary.write_text(json.dumps({"result": result}, ensure_ascii=False,
                                                allow_nan=False), encoding="utf-8")
                temporary.replace(pointer)
            finally:
                temporary.unlink(missing_ok=True)

        for group_index, group in enumerate(config["source_groups"], 1):
            group_options = {**config, "source_slot": group["source_slot"], "mask_slots": group["mask_slots"],
                             "mask_bounds": [entry for entry in config["mask_bounds"] if entry["slot"] in group["mask_slots"]]}
            def group_progress(value):
                completed = sum(item["search_evaluations"] for item in group_reports)
                update = {**value, "source_slot": group["source_slot"],
                          "group_index": group_index, "group_count": len(config["source_groups"]),
                          "completed_evaluations": completed + value["evaluations"],
                          "total_evaluations": config["max_evaluations"] * len(config["source_groups"]),
                          "elapsed_s": time.perf_counter() - started}
                if value["stage"] == "complete":
                    update["stage"] = "group_complete"
                if value.get("estimated_remaining_s") is not None and value["evaluations"]:
                    future_groups = len(config["source_groups"]) - group_index
                    factor = (local.grid_size / config["search_grid_size"]) ** 2
                    future_verifications = 5 if config["search_grid_size"] != local.grid_size else 0
                    measured = value["elapsed_s"] / value["evaluations"]
                    update["estimated_remaining_s"] += measured * (
                        future_groups * (config["max_evaluations"] + future_verifications * factor) +
                        (2 * factor if objective_kind == "target_map" else 0))
                latest_progress.update(update)
                if progress_callback is not None:
                    progress_callback(update)
            optimized = optimize_correction_boards(local, optimization_recipe, working_machine,
                        group_options, progress_callback=group_progress,
                        cancel_callback=cancel_event.is_set if cancel_event is not None else None)
            group_reports.append({**optimized.report, "source_slot": group["source_slot"]})
            if group_index == 1 and not config["new_mask_slots"]:
                original_simulation = optimized.original_simulation
            for board in optimized.candidate_masks:
                candidate_masks[board["slot"]] = board["profile"]
                working_machine["masks"][board["slot"]]["profile"] = deepcopy(board["profile"])
            if checkpoint_path and pending_only:
                try:
                    if objective_kind == "uniformity":
                        checkpoint_simulation = optimized.simulation
                    else:
                        checkpoint_simulation = simulate_process(
                            local, {**optimization_runtime, "machine": working_machine},
                            backend=config["backend"], cancel_event=None,
                            fast_cycle=config["fast_cycle"],
                            fast_cycle_residual_percent=config["fast_cycle_residual_percent"])
                    checkpoint_candidate(checkpoint_simulation)
                except Exception as checkpoint_error:
                    if progress_callback is not None:
                        progress_callback({**latest_progress, "stage": "checkpoint_warning",
                                           "warning": str(checkpoint_error)})
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("修正板优化已取消")
        verify_started = time.perf_counter()
        if progress_callback is not None:
            progress_callback({**latest_progress, "stage": "verify_full"})
        selected_simulation_elapsed = group_reports[-1]["selected_simulation_elapsed_s"]
        if objective_kind == "uniformity":
            proposed_simulation = optimized.simulation
        else:
            proposed_simulation = simulate_process(local, {**optimization_runtime, "machine": working_machine},
                        backend=config["backend"], cancel_event=cancel_event,
                        fast_cycle=config["fast_cycle"], fast_cycle_residual_percent=config["fast_cycle_residual_percent"])
            selected_simulation_elapsed = time.perf_counter() - verify_started
            original_simulation = None  # A source contribution cannot stand in for the full film stack.
        proposed_full_quality = _quality(proposed_simulation, "uniformity", None)
        original_failure = None
        original_simulation_elapsed = group_reports[0].get("original_simulation_elapsed_s")
        if original_simulation is None:
            original_started = time.perf_counter()
            try:
                original_simulation = simulate_process(local, {**document.runtime, "machine": original_machine},
                            backend=config["backend"], cancel_event=cancel_event,
                            fast_cycle=config["fast_cycle"], fast_cycle_residual_percent=config["fast_cycle_residual_percent"])
                original_simulation_elapsed = time.perf_counter() - original_started
            except ValueError as exc:
                original_failure = str(exc)
        original_full_quality = (_quality(original_simulation, "uniformity", None)
                                 if original_simulation is not None else None)
        original_quality = (original_full_quality if objective_kind == "uniformity" else
                            group_reports[0]["original_boards_verified"])
        proposed_quality = (proposed_full_quality if objective_kind == "uniformity" else
                            group_reports[-1]["best_verified"])
        changed = any(slot >= len(original_machine["masks"]) or profile != original_machine["masks"][slot]["profile"]
                      for slot, profile in candidate_masks.items())
        improved = original_quality is None or proposed_quality["score"] < original_quality["score"] - 1e-12
        accepted = (initialization == "auto_design") or (changed and improved)
        proposed_masks = deepcopy(candidate_masks)
        simulation = proposed_simulation
        selected_quality = proposed_quality
        if not accepted:
            if original_simulation is None:
                raise ValueError("原方案无有效全膜系结果，候选也未得到可采纳改善")
            simulation = original_simulation
            working_machine = original_machine
            candidate_masks = {slot: deepcopy(original_machine["masks"][slot]["profile"])
                               for slot in config["mask_slots"]}
            selected_quality = original_quality
            if original_simulation_elapsed is not None:
                selected_simulation_elapsed = original_simulation_elapsed
        full_quality = _quality(simulation, "uniformity", None)
        search_best_masks = deepcopy(proposed_masks)
        for entry in group_reports[-1]["search_best_masks"]:
            search_best_masks[entry["slot"]] = entry["profile"]
        verification_elapsed = (time.perf_counter() - verify_started +
                                sum(item["verification_elapsed_s"] for item in group_reports))
        if pending_only:
            from .input_snapshot import candidate_snapshot
            from uuid import uuid4
            frozen = process_path if isinstance(process_path, Mapping) else self.freeze_process(
                document.data, base_dir=Path(document.path).parent)
            candidate_input = candidate_snapshot(frozen, candidate_masks, optimization_recipe["steps"])
            candidate_input["parameters"] = {"settings": asdict(local), "options": deepcopy(config),
                "process_path": candidate_input["process_path"], "backend": config["backend"],
                "fast_cycle": config["fast_cycle"], "fast_cycle_residual_percent": config["fast_cycle_residual_percent"]}
            candidate = self.load("process", candidate_input)
            directory = self.root / "data" / "tmp" / "optimizations" / uuid4().hex
            directory.mkdir(parents=True, exist_ok=True)
            bundle = {"directory": str(directory), "mask_paths": [], "pending_save": True}
        else:
            bundle = self.snapshot_process(document.runtime["profile"], base_dir=Path(document.path).parent)
            output_machine = self.load("machine", bundle["machine_path"]).data
            for slot, profile in sorted(candidate_masks.items()):
                if slot < len(bundle["mask_paths"]):
                    target = bundle["mask_paths"][slot]
                    original = self.load("mask", target).data
                    self.save("mask", target, {**profile, "description": original.get("description", ""),
                               "notes": original.get("notes", "")}, overwrite=True)
                else:
                    target = str(Path(bundle["directory"]) / f"mask-placement-{slot + 1}.cmsk")
                    self.save("mask", target, profile)
                    bundle["mask_paths"].append(target)
                    output_machine["masks"].append({"file": Path(target).name})
            self.save("machine", bundle["machine_path"], output_machine, overwrite=True)
            output_recipe = self.load("process", bundle["process_path"]).data
            output_recipe["steps"] = deepcopy(optimization_recipe["steps"])
            self.save("process", bundle["process_path"], output_recipe, overwrite=True)
            candidate = self.load("process", bundle["process_path"])
            if candidate.issues:
                raise ServiceError("dependency_error", "优化工艺依赖无效", candidate.path, candidate.issues)
        report = dict(group_reports[-1], artifacts=bundle, options=config, simulation_settings=asdict(local),
                      source_groups=config["source_groups"], group_reports=group_reports,
                      mask_slots=config["mask_slots"],
                      best_verified=selected_quality, candidate_verified=proposed_quality,
                      selected_search=group_reports[-1]["selected_search"] if accepted else group_reports[0]["original_boards_search"],
                      original_boards_search=group_reports[0]["original_boards_search"],
                      search_best_masks=[{"slot": slot, "profile": profile} for slot, profile in search_best_masks.items()],
                      selected_is_search_best=candidate_masks == search_best_masks,
                      baseline_without_selected_boards=group_reports[0]["baseline_without_selected_boards"] if len(group_reports) == 1 else None,
                      verification_evaluations=sum(item["verification_evaluations"] for item in group_reports),
                      verification_simulations=sum(item["verification_simulations"] for item in group_reports),
                      original_boards_verified=original_quality,
                      full_recipe_quality=full_quality, original_full_recipe_quality=original_full_quality,
                      candidate_full_recipe_quality=proposed_full_quality, original_full_recipe_error=original_failure,
                      accepted=accepted, candidate_status="accepted" if accepted else ("rejected" if changed else "unchanged"),
                      selection_reason=("from_scratch_verified_design" if initialization == "auto_design" else
                                        "original_grid_objective_improvement" if accepted else "no_original_grid_improvement"),
                      proposed_masks=[{"slot": slot, "profile": profile} for slot, profile in proposed_masks.items()],
                      best_is_original=not accepted,
                      verification_objective="full_recipe_uniformity" if objective_kind == "uniformity" else "source_contribution_target_map",
                      target_map_group_scores=[{"source_slot": item["source_slot"], "best_verified": item["best_verified"]}
                                               for item in group_reports] if config["objective"]["kind"] == "target_map" else [],
                      verification_elapsed_s=verification_elapsed, elapsed_s=time.perf_counter() - started,
                      selected_simulation_elapsed_s=selected_simulation_elapsed,
                      improved_on_verification_grid=bool(accepted and improved and original_quality is not None),
                      score_improvement_vs_original_percent=((original_quality["score"] - selected_quality["score"]) /
                                                            original_quality["score"] * 100.0)
                          if original_quality is not None and original_quality["score"] > 0 else None,
                      search_evaluations=sum(item["search_evaluations"] for item in group_reports),
                      max_evaluations=config["max_evaluations"] * len(group_reports))
        report["constraints"] = {**report["constraints"], "mask_bounds": config["mask_bounds"]}
        report_path = Path(bundle["directory"]) / "optimization-report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        if progress_callback is not None:
            progress_callback({**latest_progress, "stage": "complete", "accepted": accepted,
                               "best_verified_score": selected_quality["score"],
                               "best_verified_cv_percent": selected_quality["cv_percent"],
                               "full_recipe_cv_percent": full_quality["cv_percent"],
                               "original_full_recipe_cv_percent": original_full_quality["cv_percent"] if original_full_quality else None,
                               "elapsed_s": time.perf_counter() - started, "estimated_remaining_s": 0.0})
        return {"report": report, "report_path": str(report_path),
                "artifacts": bundle, "simulation": simulation,
                "machine": candidate.runtime["machine"], "process": candidate.runtime["profile"],
                "input_snapshot": candidate_input if pending_only else None,
                "pending_masks": [{"slot": slot, "profile": deepcopy(profile)}
                                  for slot, profile in sorted(candidate_masks.items())] if pending_only else []}

    def time_fields(self, *, process_path: str | os.PathLike[str] | None = None,
                    time_steps: int | None = None,
                    time_step_s: float | None = None) -> dict[str, float | int]:
        """Convert global sampling controls using the existing process rule.

        Without a process the single-source preview uses the established 1 s
        sampling window. Exactly one of ``time_steps`` and ``time_step_s`` may
        be specified; with neither, the default count is 600.
        """
        if time_steps is not None and time_step_s is not None:
            raise ServiceError("invalid_sampling", "时间步数与时间步长一次只能指定一个")
        if process_path is not None:
            from .process_simulator import process_timing

            document = self.load("process", process_path)
            if document.issues:
                raise ServiceError("dependency_error", "工艺依赖无效",
                                   document.path, document.issues)
            try:
                timing = process_timing(document.data, time_steps=time_steps,
                                        time_step_s=time_step_s)
            except (ValueError, TypeError) as exc:
                raise ServiceError("invalid_sampling", str(exc), document.path) from exc
            return {key: timing[key] for key in (
                "time_steps", "time_step_s", "total_duration_s")}
        if time_step_s is not None:
            if isinstance(time_step_s, bool):
                raise ServiceError("invalid_sampling", "时间步长必须为有限正数")
            try:
                requested_step = float(time_step_s)
            except (ValueError, TypeError, OverflowError) as exc:
                raise ServiceError("invalid_sampling", "时间步长必须为有限正数") from exc
            if not math.isfinite(requested_step) or requested_step <= 0:
                raise ServiceError("invalid_sampling", "时间步长必须为有限正数")
            ratio = 1.0 / requested_step
            if not math.isfinite(ratio):
                raise ServiceError("invalid_sampling", "时间步长产生非有限采样点数")
            count = max(1, math.ceil(ratio))
        else:
            requested = 600 if time_steps is None else time_steps
            if isinstance(requested, bool):
                raise ServiceError("invalid_sampling", "时间步数必须为整数")
            try:
                numeric = float(requested)
            except (ValueError, TypeError, OverflowError) as exc:
                raise ServiceError("invalid_sampling", "时间步数必须为整数") from exc
            if not math.isfinite(numeric) or not numeric.is_integer():
                raise ServiceError("invalid_sampling", "时间步数必须为整数")
            count = int(numeric)
            if count < 1:
                raise ServiceError("invalid_sampling", "单源预览的时间步数须为正整数")
        return {"time_steps": count, "time_step_s": 1.0 / count,
                "total_duration_s": 1.0}

    def estimate_runtime(self, settings: Settings | Mapping[str, Any], *,
                         requested_device: str,
                         process_path: str | os.PathLike[str] | None = None,
                         fast_cycle: bool = False,
                         fast_cycle_residual_percent: float = 5.0,
                         machine_path: str | os.PathLike[str] | None = None,
                         source_slot: int = 1,
                         substrate_path: str | os.PathLike[str] | None = None,
                         source_path: str | os.PathLike[str] | None = None,
                         mask_paths: Sequence[str | os.PathLike[str]] = ()
                         ) -> dict[str, Any]:
        """Return a same-device empirical duration estimate, if available."""
        index = self.work_index(settings, process_path=process_path,
                                fast_cycle=fast_cycle,
                                fast_cycle_residual_percent=fast_cycle_residual_percent,
                                machine_path=machine_path,
                                source_slot=source_slot,
                                substrate_path=substrate_path,
                                source_path=source_path,
                                mask_paths=mask_paths)
        self._refresh_history()
        algorithm = (("fast_cycle" if fast_cycle else "precise")
                     if process_path is not None else "single_source")
        estimate = self.history.estimate(index, requested_device,
                                         algorithm=algorithm)
        from .compute_resources import estimate_memory
        layer_count = (len(self.load("process", process_path).data.get("steps", []))
                       if process_path is not None else 1)
        memory = estimate_memory(self._settings(settings), layer_count=layer_count)
        if estimate is None:
            return {"work_index": index, "estimated_s": None,
                    "formatted": None, "actual_device": None,
                    "sample_count": 0, **memory}
        seconds, actual_device, count = estimate
        return {"work_index": index, "estimated_s": float(seconds),
                "formatted": format_elapsed(seconds),
                "actual_device": actual_device, "sample_count": int(count), **memory}

    def record_success(self, *, requested_device: str,
                       result: Mapping[str, Any], work_index: float,
                       elapsed_s: float) -> dict[str, Any]:
        """Record a completed calculation; cancelled or failed runs never call this."""
        actual = result.get("device")
        if not isinstance(actual, str) or not actual.strip():
            raise ServiceError("invalid_result", "结果缺少实际计算设备")
        algorithm = result.get("algorithm", "single_source")
        if algorithm not in ("precise", "fast_cycle", "single_source"):
            raise ServiceError("invalid_result", "结果包含未知计算算法")
        try:
            seconds = float(elapsed_s)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ServiceError("invalid_result", "计算用时须为有限正数") from exc
        if not math.isfinite(seconds) or seconds <= 0:
            raise ServiceError("invalid_result", "计算用时须为有限正数")
        try:
            self._refresh_history()
            if result.get("algorithm_report", {}).get("algorithm") in ("reference", "optimize"):
                return {"recorded": False, "reason": "multi-evaluation timing is not a legacy fast/precise sample"}
            return self.history.append(requested_device, actual,
                                       work_index, seconds,
                                       algorithm=algorithm)
        except (OSError, ValueError, TypeError) as exc:
            raise ServiceError("history_error", str(exc)) from exc

    def recent_history(self, count: int = 100) -> list[dict[str, Any]]:
        if type(count) is not int or not 1 <= count <= 10000:
            raise ServiceError("invalid_count", "历史记录数须在 1–10000 之间")
        self._refresh_history()
        return self.history.recent(count)
