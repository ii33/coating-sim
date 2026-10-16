"""Strict, headless project document parsing and dependency discovery.

Only explicit typed references are followed. Global libraries, the current
working directory and previous sessions are never dependency sources.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import unicodedata
from typing import Any

from .data_files import MAX_FILE_BYTES
from .design_files import DESIGN_EXTENSIONS, design_kind_from_path
from .film_design import validate_design
from .machine_profiles import validate_machine_profile, validate_mask_profile
from .source_profiles import validate_source_profile
from .substrate_profiles import validate_substrate_profile
from .process_profiles import validate_process_profile
from .optical_constants import validate_optical_set

MAX_PROJECT_DOCUMENTS = 1000
MAX_PROJECT_BYTES = 256 * 1024 * 1024
KINDS = frozenset((*DESIGN_EXTENSIONS, "deposition_configuration", "optical_material", "material_profile", "expression_template",
                   "raytrace_request", "appearance_configuration", "binary_asset"))
EXTENSIONS = {**DESIGN_EXTENSIONS, "deposition_configuration": ".cdep", "optical_material": ".json", "material_profile": ".json", "expression_template": ".json",
              "raytrace_request": ".cray", "appearance_configuration": ".cappearance", "binary_asset": ".casset"}


class ProjectError(ValueError):
    """Stable error code for GUI/CLI adapters, without implicit error logging."""
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def json_bytes(value: Any) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                           allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProjectError("invalid_json", "Project data must be finite UTF-8 JSON") from exc


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProjectError("invalid_json", "Duplicate JSON key: " + key)
        result[key] = value
    return result


def _reject_constant(value):
    raise ProjectError("invalid_json", "Non-finite JSON number: " + value)


def parse_json(content: bytes):
    if len(content) > MAX_FILE_BYTES:
        raise ProjectError("capacity_exceeded", "Project JSON document exceeds size limit")
    try:
        result = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object,
                            parse_constant=_reject_constant)
        # JSON exponent overflow (1e999) is not handled by parse_constant.
        json_bytes(result)
        return result
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectError("invalid_json", "Invalid UTF-8 JSON document") from exc


def reject_links(path: Path) -> None:
    """Check every existing ancestor before resolution can erase a link."""
    absolute = Path(os.path.abspath(path))
    for candidate in (*reversed(absolute.parents), absolute):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ProjectError("unsafe_path", "Project paths cannot use links or reparse points: " + str(candidate))


def read_json(path: Path) -> tuple[dict, bytes]:
    reject_links(path)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ProjectError("unsafe_path", "Project document is not a regular file: " + str(path))
        with path.open("rb") as stream:
            content = stream.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError as exc:
        raise ProjectError("missing_dependency", "Project document is missing: " + str(path)) from exc
    except OSError as exc:
        raise ProjectError("io_error", "Cannot read project document: " + str(path)) from exc
    value = parse_json(content)
    if not isinstance(value, dict):
        raise ProjectError("invalid_document", "Project document must be a JSON object")
    return value, content


def portable_path(value: Any, *, objects: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ProjectError("unsafe_path", "Invalid project-relative path")
    parts = value.split("/")
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
        p + str(n) for p in ("COM", "LPT") for n in range(1, 10)}
    if (PurePosixPath(value).is_absolute() or PureWindowsPath(value).drive or
            any(part in ("", ".", "..") or part.endswith((".", " ")) or
                unicodedata.normalize("NFKC", part).split(".", 1)[0].rstrip(" ").upper() in reserved for part in parts) or
            any(ord(c) < 32 or c in '\\<>:"|?*' for c in value) or
            (objects and (len(parts) != 3 or parts[0] != "objects"))):
        raise ProjectError("unsafe_path", "Unsafe project-relative path: " + value)
    return value


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ProjectError("invalid_manifest", "Project identity must be a lowercase UUID hex value")
    return value


def normalize_optical_material(value: Any) -> dict:
    """The existing frozen-film record shape, not a global library profile."""
    if not isinstance(value, Mapping) or set(value) - {"id", "name", "optical_sets", "design_ready", "notes"}:
        raise ProjectError("invalid_document", "Invalid project optical material record")
    material_id, name = value.get("id"), value.get("name")
    if not isinstance(material_id, str) or not material_id or len(material_id) > 160:
        raise ProjectError("invalid_document", "Optical material ID is missing or too long")
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise ProjectError("invalid_document", "Optical material name is missing or too long")
    rows = value.get("optical_sets")
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ProjectError("invalid_document", "Optical material needs a bounded optical_sets list")
    try:
        normalized = [validate_optical_set(deepcopy(row)) for row in rows]
    except (TypeError, ValueError, KeyError) as exc:
        raise ProjectError("invalid_document", str(exc)) from exc
    ids = [row["id"].casefold() for row in normalized]
    if len(ids) != len(set(ids)):
        raise ProjectError("dependency_conflict", "Optical material has duplicate optical-set IDs")
    notes = value.get("notes", "")
    ready = value.get("design_ready")
    if not isinstance(notes, str) or len(notes) > 20000 or (ready is not None and type(ready) is not bool):
        raise ProjectError("invalid_document", "Optical material notes/design_ready are invalid")
    return {"id": material_id, "name": name, "optical_sets": normalized,
            "notes": notes, **({"design_ready": ready} if "design_ready" in value else {})}


def normalize_document(kind: str, data: Any) -> dict:
    try:
        if kind == "binary_asset":
            from .project_binary_assets import normalize_binary_asset
            return normalize_binary_asset(data)
        if kind in {"raytrace_request", "appearance_configuration"}:
            from .project_simulation_resources import normalize_simulation_document
            return normalize_simulation_document(kind, data)
        if kind == "deposition_configuration":
            required = {"kind", "version", "name", "process_document_id", "parameters"}
            if (not isinstance(data, Mapping) or not required.issubset(data) or
                    set(data) - required - {"description", "notes"} or
                    data.get("kind") != "coat_sim.deposition_configuration"):
                raise ProjectError("invalid_document", "Invalid deposition configuration")
            if type(data.get("version")) is not int or data["version"] != 1:
                raise ProjectError("unsupported_version", "Unsupported deposition configuration version")
            name = data.get("name")
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
                raise ProjectError("invalid_document", "Configuration name must be 1–100 characters")
            from .api import recipe_algorithm_options
            from .schemas import settings_data
            parameters = data.get("parameters")
            if not isinstance(parameters, Mapping) or set(parameters) - {
                    "settings", "backend", "fast_cycle", "fast_cycle_residual_percent",
                    "algorithm", "algorithm_options", "time_step_s"}:
                raise ProjectError("invalid_document", "Unsupported recipe calculation parameters")
            parameters = deepcopy(dict(parameters))
            # Keep omitted settings omitted: execution owns timing/default rules.
            if "settings" in parameters:
                from .schemas import SimulationSettings
                settings_data(parameters["settings"])
                parameters["settings"] = SimulationSettings.model_validate(parameters["settings"]).model_dump(exclude_unset=True)
            backend = parameters.get("backend", "自动")
            if backend not in ("CPU", "自动", "GPU", "DirectML"):
                raise ProjectError("invalid_document", "Unsupported compute backend")
            if "fast_cycle" in parameters and type(parameters["fast_cycle"]) is not bool:
                raise ProjectError("invalid_document", "fast_cycle must be boolean")
            for key, low, high in (("fast_cycle_residual_percent", 0, 100), ("time_step_s", 0, float("inf"))):
                if key in parameters:
                    import math
                    number = parameters[key]
                    if (isinstance(number, bool) or not isinstance(number, (int, float)) or
                            not math.isfinite(number) or not low < number <= high):
                        raise ProjectError("invalid_document", key + " is outside its valid range")
            if "time_step_s" in parameters and "time_steps" in parameters.get("settings", {}):
                raise ProjectError("invalid_document", "time_step_s and time_steps are mutually exclusive")
            recipe_algorithm_options(parameters.get("algorithm", "fast"), parameters.get("algorithm_options"),
                                     backend, parameters.get("settings", {}).get("carrier_rpm", 15.0))
            metadata = {}
            for field in ("description", "notes"):
                if field in data:
                    if not isinstance(data[field], str) or len(data[field]) > 20000:
                        raise ProjectError("invalid_document", field + " must be text up to 20000 characters")
                    metadata[field] = data[field]
            return {"kind": data["kind"], "version": 1, "name": name.strip(),
                    "process_document_id": identifier(data["process_document_id"]),
                    "parameters": parameters, **metadata}
        if kind == "expression_template":
            if isinstance(data, Mapping):
                version = data.get("schema_version", 1)
                if type(version) is not int or version != 1:
                    raise ProjectError("unsupported_version", "Unsupported expression template version")
            from .film_expression_store import _normalize_template
            return _normalize_template(deepcopy(data))
        if kind == "material_profile":
            if (not isinstance(data, Mapping) or set(data) != {"kind", "version", "material"} or
                    data.get("kind") != "coat_sim.project_material_profile"):
                raise ProjectError("invalid_document", "Invalid project material profile envelope")
            if type(data.get("version")) is not int or data["version"] != 1:
                raise ProjectError("unsupported_version", "Unsupported project material profile version")
            from .materials import validate_material_profile
            return {"kind": data["kind"], "version": 1,
                    "material": validate_material_profile(deepcopy(data["material"]))}
        if kind == "optical_material":
            if (not isinstance(data, Mapping) or set(data) != {"kind", "version", "material"} or
                    data.get("kind") != "coat_sim.project_optical_material"):
                raise ProjectError("invalid_document", "Invalid project optical material envelope")
            if type(data.get("version")) is not int or data["version"] != 1:
                raise ProjectError("unsupported_version", "Unsupported project optical material version")
            return {"kind": data["kind"], "version": 1, "material": normalize_optical_material(data["material"])}
        validators = {"process": validate_process_profile, "machine": validate_machine_profile,
                      "source": validate_source_profile, "mask": validate_mask_profile,
                      "substrate": validate_substrate_profile, "film": validate_design}
        if kind not in validators:
            raise ProjectError("invalid_document", "Unsupported project document kind")
        if kind == "source" and (not isinstance(data, Mapping) or type(data.get("schema_version")) is not int or data["schema_version"] != 2):
            raise ProjectError("invalid_document", "Standalone source must use schema version 2")
        return validators[kind](deepcopy(data))
    except ProjectError:
        raise
    except (TypeError, ValueError, KeyError) as exc:
        raise ProjectError("invalid_document", str(exc)) from exc


def file_references(kind: str, data: dict):
    if kind in {"raytrace_request", "appearance_configuration"}:
        from .project_simulation_resources import simulation_asset_references
        for owner, field, _role, _slot in simulation_asset_references(kind, data):
            yield owner, field, "binary_asset"
    elif kind == "process":
        yield data, "machine_file", "machine"
        yield data, "substrate_file", "substrate"
    elif kind == "machine":
        for collection, child_kind in (("sources", "source"), ("masks", "mask")):
            for entry in data[collection]:
                yield entry, "file", child_kind


def film_references(data: dict) -> list[tuple[str, str]]:
    rows = [data["substrate"], *data["front_layers"], *data["back_layers"],
            *data["expression"]["symbols"].values()]
    rows.extend(data[key] for key in ("incident_medium", "exit_medium") if data[key]["kind"] == "material")
    optimization = data.get("workflow_settings", {}).get("optimization", {})
    if "structure_materials" in optimization:
        if type(optimization.get("version")) is not int or optimization["version"] != 1:
            raise ProjectError("unsupported_version", "Unsupported film optimization material-reference configuration version")
        candidates = optimization["structure_materials"]
        if not isinstance(candidates, list) or len(candidates) > 64:
            raise ProjectError("invalid_document", "Film structure_materials must be a list of at most 64 typed references")
        selected = set()
        for candidate in candidates:
            if (not isinstance(candidate, dict) or set(candidate) != {"material_id", "optical_set_id"}
                    or any(not isinstance(candidate[key], str) or not candidate[key] or len(candidate[key]) > 128
                           for key in ("material_id", "optical_set_id"))):
                raise ProjectError("invalid_document", "Invalid film structure candidate material reference")
            pair = (candidate["material_id"], candidate["optical_set_id"])
            if pair in selected:
                raise ProjectError("dependency_conflict", "Duplicate film structure candidate material reference")
            selected.add(pair)
        rows.extend(candidates)
    result = []
    for row in rows:
        material_id, set_id = row.get("material_id"), row.get("optical_set_id")
        if bool(material_id) != bool(set_id):
            raise ProjectError("missing_dependency", "Film material and optical-set IDs must be bound together")
        if material_id and (material_id, set_id) not in result:
            result.append((material_id, set_id))
    return result


def external_document(path: Path, expected_kind: str | None = None) -> tuple[str, dict, int]:
    kind = design_kind_from_path(path)
    if kind is None:
        from .project_simulation_resources import SIMULATION_EXTENSIONS
        kind = next((key for key, suffix in SIMULATION_EXTENSIONS.items()
                     if path.suffix.casefold() == suffix), None)
    if kind is None or (expected_kind is not None and kind != expected_kind):
        raise ProjectError("dependency_conflict", "Unsupported or mismatched design suffix: " + str(path))
    data, content = read_json(path)
    return kind, normalize_document(kind, data), len(content)


def document_material_references(kind: str, data: dict) -> list[tuple[str, str]]:
    if kind == "film":
        return film_references(data)
    if kind != "expression_template":
        return []
    result = []
    for symbol in data["expression"]["symbols"].values():
        material_id, set_id = symbol.get("material_id"), symbol.get("optical_set_id")
        if bool(material_id) != bool(set_id):
            raise ProjectError("missing_dependency", "Template material and optical-set IDs must be bound together")
        if material_id and (material_id, set_id) not in result:
            result.append((material_id, set_id))
    return result
