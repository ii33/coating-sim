"""Finite application capabilities shared by local adapters; no UI or model imports.

The catalog describes registered methods, never grants permissions. In particular
it has no generic submit, approve, shell, file mutation, or desktop invocation.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import inspect
import json
from pathlib import Path
from threading import RLock
from typing import Any, Literal, get_type_hints

from pydantic import ConfigDict, ValidationError, create_model

from .api import ServiceError
from .errors import safe_error_text

MCP_ERROR_MARKER = "\n[coat_capability_error]"


@dataclass(frozen=True)
class OperationPolicy:
    owner: str
    effect: str = "read"
    boundary: str = "Project root and existing domain validation"
    lifecycle: str = "synchronous; no job or cancellation"


_FILM = OperationPolicy("CoatService / FilmDesignService")
_DESIGN = OperationPolicy("CoatService")
_MATERIAL = OperationPolicy("MaterialLibrary")
_JOB = OperationPolicy("SchedulerClient", boundary="Existing job IDs; no submit or approval",
                       lifecycle="synchronous receipt of existing asynchronous job; scheduler owns terminal state")

# Each entry must have a corresponding implementation; checked before discovery.
POLICIES = {
    "film_materials": _FILM,
    "validate_film_design": _FILM,
    "load_film_design": _FILM,
    "save_film_design": OperationPolicy(_FILM.owner, "write", "Project root; explicit overwrite boolean"),
    "expand_film_expression": _FILM,
    "simulate_film_design": OperationPolicy("film_result_service / FilmDesignService", "write",
        "JSON directly under project data/results; explicit overwrite; may preserve CSV on JSON failure"),
    "input_schema": _DESIGN,
    "read_design": _DESIGN,
    "validate_design": _DESIGN,
    "preview_design": _DESIGN,
    "resolve_design_dependencies": _DESIGN,
    "check_machine_structure": _DESIGN,
    "list_material_library": _MATERIAL,
    "read_material": _MATERIAL,
    "validate_material_optics": _MATERIAL,
    "read_simulation_asset": OperationPolicy("simulation_assets", boundary="Checksum-verified asset; no extraction"),
    "simulation_summaries": OperationPolicy("simulation_assets / history"),
    "calculation_queue_snapshot": _JOB,
    "calculation_job_status": _JOB,
    "find_calculation_job": _JOB,
    "calculation_job_result": _JOB,
    "cancel_calculation_job": OperationPolicy(_JOB.owner, "cancel", _JOB.boundary,
        "Cancel request receipt; query original job for actual terminal state"),
}

QUEUE_OPERATIONS = frozenset({"calculation_queue_snapshot", "calculation_job_status",
    "find_calculation_job", "calculation_job_result", "cancel_calculation_job"})

_PARAMETER_MODEL_CACHE_MAXSIZE = 128
_PARAMETER_MODEL_CACHE = OrderedDict()
_PARAMETER_MODEL_CACHE_LOCK = RLock()


class CapabilityError(ValueError):
    def __init__(self, code: str, message: str, *, effect: str = "none"):
        self.code = code
        self.message = safe_error_text(message)
        self.effect = effect
        super().__init__(f"{code}: {self.message}")

    def as_dict(self):
        return {"code": self.code, "message": self.message, "effect": self.effect}

    def transport_text(self):
        """Readable MCP error plus a bounded machine-readable local contract."""
        return str(self) + MCP_ERROR_MARKER + json.dumps(self.as_dict(), ensure_ascii=False)


def decode_capability_error(operation: str, detail: str):
    """Decode only shared local tools; other tools keep their historical rules."""
    if operation not in POLICIES or MCP_ERROR_MARKER not in detail:
        return None
    try:
        value = json.loads(detail.rsplit(MCP_ERROR_MARKER, 1)[1])
    except (ValueError, TypeError):
        return None
    if (not isinstance(value, dict) or set(value) != {"code", "message", "effect"}
            or not isinstance(value["code"], str) or not isinstance(value["message"], str)
            or not isinstance(value["effect"], str)
            or value["effect"] not in {"none", "unknown"}):
        return None
    return value


class CapabilityService:
    """Adapt existing owners, without caching drafts, results, or credentials."""

    def __init__(self, service, scheduler=None, *, film_json_publisher=None):
        self.service = service
        self._scheduler = scheduler
        self._scheduler_injected = scheduler is not None
        self._film_json_publisher = film_json_publisher
        names = {name.removeprefix("_op_") for name in dir(type(self)) if name.startswith("_op_")}
        if names != set(POLICIES):
            raise RuntimeError("capability implementation/policy mismatch")
        self._models = {}
        self._model_declarations = {}

    @property
    def scheduler(self):
        if self._scheduler is None:
            from coat_sim.scheduler import SchedulerClient
            self._scheduler = SchedulerClient(self.service.root)
        return self._scheduler

    def _model(self, operation):
        declaration = getattr(type(self), "_op_" + operation)
        model = self._models.get(operation)
        if model is not None and self._model_declarations.get(operation) is declaration:
            return model

        method = getattr(self, "_op_" + operation)
        cache_key = (operation, declaration)
        with _PARAMETER_MODEL_CACHE_LOCK:
            # Another thread may have populated this instance while we waited.
            model = self._models.get(operation)
            if model is not None and self._model_declarations.get(operation) is declaration:
                return model

            model = _PARAMETER_MODEL_CACHE.get(cache_key)
            if model is not None:
                _PARAMETER_MODEL_CACHE.move_to_end(cache_key)
            else:
                hints = get_type_hints(method)
                fields = {name: (hints.get(name, Any),
                                ... if arg.default is inspect.Parameter.empty else arg.default)
                          for name, arg in inspect.signature(method).parameters.items()}
                model = create_model("Capability_" + operation,
                    __config__=ConfigDict(extra="forbid", strict=True), **fields)
                _PARAMETER_MODEL_CACHE[cache_key] = model
                if len(_PARAMETER_MODEL_CACHE) > _PARAMETER_MODEL_CACHE_MAXSIZE:
                    _PARAMETER_MODEL_CACHE.popitem(last=False)

            self._models[operation] = model
            self._model_declarations[operation] = declaration
            return model

    def catalog(self):
        return {"contract_version": 1, "operations": [
            {"name": name, "input_schema": self._model(name).model_json_schema(),
             "owner": policy.owner, "effect": policy.effect, "boundary": policy.boundary,
             "lifecycle": policy.lifecycle, "availability": "available",
             "authorization": "Caller preserves existing entrypoint authorization; discovery grants none",
             **({"route": self.queue_route()} if name in QUEUE_OPERATIONS else {})}
            for name, policy in POLICIES.items()]}

    def queue_route(self):
        """Describe selection without initializing a scheduler or exposing its endpoint."""
        return {"selection": "injected" if self._scheduler_injected else "default_local",
                "target": "caller_owned" if self._scheduler_injected else "local",
                "remote_selection_from_cli": False}

    def _preflight_queue_route(self, operation):
        if operation not in QUEUE_OPERATIONS or not self._scheduler_injected:
            return
        root = getattr(self._scheduler, "root", None)
        if root is not None:
            try:
                matches = Path(root).resolve() == Path(self.service.root).resolve()
            except (OSError, TypeError, ValueError):
                matches = False
            if not matches:
                raise CapabilityError("invalid_queue_route",
                    "Injected scheduler root must match the application project root", effect="none")

    def validate_arguments(self, operation: str, arguments: dict[str, Any] | None = None):
        """Validate without executing, for transports that otherwise coerce inputs."""
        if not isinstance(operation, str) or operation not in POLICIES:
            raise CapabilityError("unavailable_operation", "Choose a registered operation from catalog")
        arguments = {} if arguments is None else arguments
        if not isinstance(arguments, dict):
            raise CapabilityError("invalid_arguments", "arguments must be a JSON object")
        try:
            json.dumps(arguments, allow_nan=False)
            validated = self._model(operation).model_validate(arguments).model_dump()
        except ValidationError as exc:
            detail = "; ".join(f"{'.'.join(map(str, item['loc'])) or 'arguments'}: {item['msg']}"
                               for item in exc.errors(include_url=False, include_input=False))
            raise CapabilityError("invalid_arguments", detail) from exc
        except (ValueError, TypeError) as exc:
            raise CapabilityError("invalid_arguments", "arguments must contain finite JSON values") from exc
        return validated

    def call(self, operation: str, arguments: dict[str, Any] | None = None):
        validated = self.validate_arguments(operation, arguments)
        self._preflight_queue_route(operation)
        invoke_arguments = self._preflight_write(operation, validated)
        policy = POLICIES[operation]
        effect = "none" if policy.effect == "read" else "unknown"
        try:
            return getattr(self, "_op_" + operation)(**invoke_arguments)
        except CapabilityError as exc:
            # The operation has started. A nested error's default "none" is
            # not evidence that a write/cancel did not begin.
            raise CapabilityError(exc.code, exc.message, effect=effect) from exc
        except ServiceError as exc:
            detail = exc.message + ("\n" + "\n".join(exc.details) if exc.details else "")
            raise CapabilityError(exc.code, detail, effect=effect) from exc
        except OSError as exc:
            raise CapabilityError("io_error", str(exc), effect=effect) from exc
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise CapabilityError("invalid_request", str(exc), effect=effect) from exc
        except Exception as exc:
            # No retries after potentially partial effects; unexpected faults are
            # reported without transporting stack traces or request contents.
            raise CapabilityError("operation_failed", type(exc).__name__, effect=effect) from exc

    def _preflight_write(self, operation: str, arguments: dict[str, Any]):
        """Run only existing, side-effect-free guards before a write is dispatched.

        Failures here have a locally known stage and can safely report
        ``effect=none``. Once the operation method is entered, ``call`` treats
        every ordinary failure as unknown, regardless of its code or message.
        """
        if operation not in {"save_film_design", "simulate_film_design"}:
            return arguments

        try:
            if operation == "save_film_design":
                from .design_files import validate_design_path
                from .film_design import validate_design

                target = self.service._path(arguments["path"])
                validate_design_path("film", target)
                if target.exists() and not arguments["overwrite"]:
                    raise ValueError("film design already exists; set overwrite=true to replace it")
                validate_design(arguments["data"])
                return arguments

            from .film_result_service import film_artifact_path
            from .film_design import validate_design

            target = film_artifact_path(self.service, arguments["output_path"])
            csv_target = target.with_suffix(".csv")
            if (not arguments["overwrite"]
                    and (target.exists() or csv_target.exists())):
                raise ValueError(
                    "film result artifact already exists; set overwrite=true to replace it"
                )
            validate_design(arguments["data"])

            # Preserve the one resolved target (including the single UUID
            # chosen when output_path was omitted) for the actual operation.
            # Keep the validated caller arguments untouched for adapter identity.
            invoke_arguments = dict(arguments)
            invoke_arguments["output_path"] = str(target)
            return invoke_arguments
        except CapabilityError as exc:
            raise CapabilityError(exc.code, exc.message, effect="none") from exc
        except ServiceError as exc:
            detail = exc.message + ("\n" + "\n".join(exc.details) if exc.details else "")
            raise CapabilityError(exc.code, detail, effect="none") from exc
        except OSError as exc:
            raise CapabilityError("io_error", str(exc), effect="none") from exc
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise CapabilityError("invalid_request", str(exc), effect="none") from exc
        except Exception as exc:
            # This is still a known pre-dispatch stage; avoid leaking internals.
            raise CapabilityError("operation_failed", type(exc).__name__, effect="none") from exc

    def execute(self, operation: str, arguments: dict[str, Any] | None = None):
        try:
            return {"ok": True, "operation": operation, "result": self.call(operation, arguments)}
        except CapabilityError as exc:
            return {"ok": False, "operation": operation, "error": exc.as_dict()}

    def _op_film_materials(self, purpose: Literal["film", "substrate"] = "film"):
        items = []
        for record in self.service.film_material_options(purpose=purpose):
            optical_sets = []
            for optical in record.get("optical_sets", []):
                if not isinstance(optical, dict):
                    continue
                item = {key: optical[key] for key in ("id", "name", "kind", "wavelength_unit",
                        "wavelength_min", "wavelength_max", "source", "notes")
                        if key in optical and not isinstance(optical[key], (list, dict))}
                item["k_known"] = bool(optical.get("k_known", optical.get("k") is not None))
                optical_sets.append(item)
            items.append({key: record.get(key) for key in ("id", "name", "readonly", "design_ready", "notes")
                          if key in record} | {"optical_sets": optical_sets})
        return {"purpose": purpose, "materials": items}

    def _op_validate_film_design(self, data: dict[str, Any]):
        return {"ok": True, "design": self.service.validate_film_design(data)}

    def _op_load_film_design(self, path: str):
        return {"path": str(self.service._path(path)), "design": self.service.load_film_design(path)}

    def _op_save_film_design(self, path: str, data: dict[str, Any], overwrite: bool = False):
        saved = self.service.save_film_design(path, data, overwrite=overwrite)
        return {"ok": True, **saved} if isinstance(saved, dict) else {"ok": True, "path": str(saved)}

    def _op_expand_film_expression(self, expression: str, symbols: dict[str, Any]):
        return {"layers": self.service.expand_film_expression(expression, symbols)}

    def _op_simulate_film_design(self, data: dict[str, Any], settings: dict[str, Any] | None = None,
                               output_path: str | None = None, overwrite: bool = False):
        from .film_result_service import simulate_film_result
        return simulate_film_result(self.service, data, settings, output_path, overwrite,
                                    publish_json=self._film_json_publisher)

    def _op_input_schema(self, kind: Literal["source", "substrate", "mask", "machine", "process",
                                            "settings", "mask_optimization", "film_stack"]):
        from .schemas import design_json_schema
        from .schemas import SimulationSettings, MaskOptimizationOptions
        if kind == "film_stack":
            from .film_stack_schema import film_stack_json_schema
            schema = film_stack_json_schema()
        elif kind in ("settings", "mask_optimization"):
            schema = (SimulationSettings if kind == "settings" else MaskOptimizationOptions).model_json_schema()
        else:
            schema = design_json_schema(kind)
        from .schemas import InputSchemaResponse, output
        return output(InputSchemaResponse, {"kind": kind, "schema": schema})

    def _op_read_design(self, kind: Literal["source", "substrate", "mask", "machine", "process"],
                        path: str, detail: Literal["summary", "full"] = "summary"):
        document = self.service.load(kind, path)
        data = document.data
        if kind == "process" and detail == "summary":
            from .result_summary import process_summary
            data = process_summary(data)
        from .schemas import DocumentResponse, output
        return output(DocumentResponse, {"kind": document.kind, "path": document.path, "data": data,
                "valid": document.valid, "errors": list(document.errors)})

    def _op_validate_design(self, kind: Literal["source", "substrate", "mask", "machine", "process"],
                            data: dict[str, Any] | None = None, path: str | None = None,
                            include_data: bool = False):
        result = self.service.validate(kind, data, path=path)
        from .schemas import ValidationResponse, design_data, output
        if result.data is not None:
            design_data(kind, result.data)
        return output(ValidationResponse, {"kind": result.kind, "valid": result.valid,
            "data": result.data if include_data else None, "errors": list(result.errors)})

    def _op_preview_design(self, kind: Literal["source", "substrate", "mask", "machine", "process"],
                           design_path: str | None = None, data: dict[str, Any] | None = None,
                           intended_path: str | None = None):
        if (design_path is None) == (data is None):
            raise ValueError("Supply exactly one of design_path or data")
        from .schemas import PreviewResponse, design_data, output
        value = self.service.preview(kind, design_path) if design_path is not None else self.service.preview(
            kind, design_data(kind, data), path=intended_path)
        return output(PreviewResponse, value)

    def _op_resolve_design_dependencies(self, kind: Literal["source", "substrate", "mask", "machine", "process"],
                                       path: str, data: dict[str, Any]):
        from .schemas import DependenciesResponse, design_data, output
        return output(DependenciesResponse, self.service.dependencies(kind, design_data(kind, data), path=path))

    def _op_check_machine_structure(self, machine_path: str | None = None,
                                    machine_data: dict[str, Any] | None = None,
                                    substrate_path: str | None = None,
                                    substrate_data: dict[str, Any] | None = None):
        return self.service.check_machine_structure(machine_path=machine_path, machine_data=machine_data,
            substrate_path=substrate_path, substrate_data=substrate_data)

    def _op_list_material_library(self, offset: int = 0, limit: int = 20):
        return self.service.material_library().list(offset, limit)

    def _op_read_material(self, identifier: str):
        return self.service.material_library().read(identifier)

    def _op_validate_material_optics(self, datasets: list[dict[str, Any]], wavelength: float | None = None,
                                    wavelength_unit: Literal["nm", "um"] | None = None):
        return self.service.validate_material_optics(datasets, wavelength=wavelength, wavelength_unit=wavelength_unit)

    def _op_read_simulation_asset(self, path: str,
            detail: Literal["summary", "configuration", "layers", "images", "designs", "full"] = "summary",
            offset: int = 0, limit: int = 20):
        return self.service.read_simulation_asset(path, detail=detail, offset=offset, limit=limit)

    def _op_simulation_summaries(self, limit: int = 20, offset: int = 0):
        return self.service.simulation_summaries(limit=limit, offset=offset)

    def _op_calculation_queue_snapshot(self, after_event: int = 0, limit: int = 100):
        return self.scheduler.snapshot(after_event=after_event, limit=limit)

    def _op_calculation_job_status(self, job_id: str):
        return self.scheduler.status(job_id)

    def _op_find_calculation_job(self, client_request_id: str):
        return self.scheduler.find_request(client_request_id)

    def _op_cancel_calculation_job(self, job_id: str):
        return self.scheduler.cancel(job_id)

    def _op_calculation_job_result(self, job_id: str):
        return self.scheduler.result(job_id)
