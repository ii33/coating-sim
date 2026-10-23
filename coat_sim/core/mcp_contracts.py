"""MCP transport validation and conservative discovery effects.

Hints are descriptive, never authorization. Domain schemas own nested openness;
only the tool argument envelope is unconditionally closed.
"""
from __future__ import annotations

import math
from importlib import import_module
from typing import Any, Callable, Iterable, Iterator, Protocol, cast
from mcp.types import ToolAnnotations

from .capabilities import CapabilityError, POLICIES

# The pinned jsonschema runtime has no bundled typing metadata. Keep its
# dynamic import behind the exact interface used here, without disabling checks.
class _ValidationError(Protocol):
    absolute_path: Iterable[str | int]
    validator: str | None


class _Validator(Protocol):
    def iter_errors(self, instance: object) -> Iterator[_ValidationError]: ...


class _TypeChecker(Protocol):
    def redefine(self, type_name: str,
                 predicate: Callable[[object, object], bool]) -> "_TypeChecker": ...


class _ValidatorFactory(Protocol):
    TYPE_CHECKER: _TypeChecker

    def __call__(self, schema: dict[str, Any]) -> _Validator: ...


class _Validators(Protocol):
    def extend(self, validator: _ValidatorFactory, *,
               type_checker: _TypeChecker) -> _ValidatorFactory: ...


class _JsonSchema(Protocol):
    Draft202012Validator: _ValidatorFactory
    validators: _Validators


_jsonschema = cast(_JsonSchema, import_module("jsonschema"))
Draft202012Validator = _jsonschema.Draft202012Validator
validators = _jsonschema.validators

FILM_SYNTHESIS_PLAN_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["payload"],
    "properties": {"payload": {
        "type": "object", "additionalProperties": False,
        "required": ["schema_version", "kind", "request_id", "request", "physical_structures"],
        "properties": {
            "schema_version": {"const": 1},
            "kind": {"const": "coat_sim.film_synthesis_request"},
            "request_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "request": {"type": "object", "additionalProperties": True,
                "properties": {
                    "parallel_batch_size": {
                        "type": ["integer", "null"], "minimum": 1,
                        "description": "Hybrid logical candidate batch; null=AUTO resolved at freeze from requested/available CPU. Not a promise of this many concurrent workers: candidate, memory, frame and budget admission can reduce actual parallelism."},
                    "strict_upper_bound_objective_indices": {
                        "type": "array", "uniqueItems": True,
                        "items": {"type": "integer", "minimum": 0},
                        "description": "Synthesis-only zero-based range-objective indices using strict < at the original upper bound; omitted indices retain inclusive <=."},
                    "acceptable_normalized_error": {
                        "type": "number", "minimum": 0,
                        "description": "Solver acceptability tolerance only; does not relax nominal targets_met or add a manufacturing margin."}}},
            "physical_structures": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        }}}
}

FILM_SYNTHESIS_SUBMIT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["payload"],
    "properties": {"payload": FILM_SYNTHESIS_PLAN_INPUT_SCHEMA["properties"]["payload"],
        "client_request_id": {"type": ["string", "null"], "minLength": 1, "maxLength": 512}},
}

FILM_SENSITIVITY_PLAN_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["payload"],
    "properties": {"payload": {
        "type": "object", "additionalProperties": True,
        "required": ["schema_version", "kind", "design_snapshot", "design_revision",
                     "objectives", "errors", "error_model", "robustness_objective"],
        "properties": {
            "schema_version": {"const": 1},
            "kind": {"const": "coat_sim.film_sensitivity_request"},
            "optimization_policy": {"type": "string",
                "enum": ["robustness_only", "nominal_preserving"],
                "default": "robustness_only",
                "description": "robustness_only is the legacy policy; nominal_preserving optimizes only an explicit finite-scenario objective while preserving the original nominal bounds."},
            "error_amplitude_semantics": {"type": "string",
                "enum": ["bounded_amplitude", "standard_deviation"],
                "default": "bounded_amplitude",
                "description": "bounded_amplitude keeps explicit scenario coefficients in [-1, 1]; standard_deviation interprets scales as sigma and permits unbounded Gaussian coefficients."},
            "error_model": {"type": "string",
                "enum": ["independent", "common_mode", "material_common_mode", "linear_drift"],
                "description": "Explicit layer-factor structure; this is not a measured process distribution."},
            "robustness_objective": {"type": "string",
                "enum": ["linear_sensitivity_norm", "finite_scenario_worst", "finite_scenario_expected"]},
            "errors": {"type": "array", "minItems": 1,
                "description": "Thickness amplitude is (unit=absolute_nm ? magnitude : 0) + absolute_nm + ((unit=relative_fraction ? magnitude : 0) + relative_fraction) times nominal thickness. Components share one signed layer factor, not RSS-combined.",
                "items": {"type": "object", "additionalProperties": False,
                    "required": ["layer_id"],
                    "properties": {
                        "layer_id": {"type": "string", "minLength": 1},
                        "magnitude": {"type": "number", "minimum": 0, "default": 0},
                        "unit": {"type": "string", "enum": ["absolute_nm", "relative_fraction"], "default": "absolute_nm"},
                        "absolute_nm": {"type": "number", "minimum": 0, "default": 0},
                        "relative_fraction": {"type": "number", "minimum": 0, "default": 0}}}},
            "scenarios": {"type": "array", "description": "Explicit signed coefficients follow errors order and are frozen into the request. Coefficients outside [-1, 1] require standard_deviation semantics; Gaussian coefficients are never clipped.",
                "items": {"type": "object", "additionalProperties": False,
                    "required": ["scenario_id", "coefficients"],
                    "properties": {
                        "scenario_id": {"type": "string", "minLength": 1},
                        "coefficients": {"type": "array", "items": {"type": "number"}},
                        "weight": {"type": "number", "exclusiveMinimum": 0, "default": 1}}}}}}},
}

FILM_SENSITIVITY_SUBMIT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["payload"],
    "properties": {"payload": FILM_SENSITIVITY_PLAN_INPUT_SCHEMA["properties"]["payload"],
        "client_request_id": {"type": ["string", "null"], "minLength": 1, "maxLength": 512}},
}

FILM_SYNTHESIS_ESTIMATE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["kind", "parameters"],
    "properties": {
        "kind": {"enum": ["single_source", "process", "mask_optimization", "film_optimization",
            "film_synthesis", "film_sensitivity", "mask_weights", "mask_field", "point_weights", "python_script"]},
        "parameters": {"type": "object"},
    },
    "allOf": [{"if": {"properties": {"kind": {"const": "film_synthesis"}}, "required": ["kind"]},
        "then": {"properties": {"parameters": {"type": "object", "additionalProperties": False,
            "required": ["payload"],
            "properties": {"payload": FILM_SYNTHESIS_PLAN_INPUT_SCHEMA["properties"]["payload"]}}}}}],
}

# Shared domain operations take their effects directly from POLICIES. These
# explicit classifications cover the remaining MCP adapters, including optional
# development tools. New tools must be classified deliberately.
_READ = frozenset('''project_management_catalog project_list project_inspect
project_resolve project_bbs_list project_bbs_read project_bbs_history project_research_search project_research_read developer_status
developer_files python_sandbox_status list_data_files read_data_file
read_software_manual algorithm_reference_catalog algorithm_reference_search
algorithm_reference_read project_knowledge_search material_data_readiness
refractiveindex_status refractiveindex_search refractiveindex_detail
refractiveindex_preview refractiveindex_convert
reference_sources_catalog reference_sources_detail library_status library_search library_read
preview_film_stack_import read_process_layers
film_design_schema source_quadrature convert_gear_geometry sample_substrate_outline
sample_substrate_surface calculation_history estimate_calculation convert_time_fields
list_windows read_window project_editor_snapshot inspect_appearance_asset
wait_calculation_job mask_optimization_groups ito_electrical_calculate
list_deleted_builtin_materials plan_film_synthesis plan_film_sensitivity'''.split())
_WRITE = frozenset('''project_bbs_import_attachment project_bbs_comment project_bbs_write project_open project_research_save project_manage
 developer_report_issue developer_issue_reports developer_access developer_checks
 developer_persona run_python_script write_data_file move_data_path delete_data_path
 make_data_directory import_mask_dxf export_mask_dxf export_mask_drawing
 build_film_stack import_film_stack update_design_fields visualize_calculation_result
 render_design render_design_workflow save_design prepare_temporary_design
 save_generated_design save_simulation_asset export_simulation_asset create_material
 update_material copy_material edit_builtin_material delete_builtin_material
 restore_builtin_material simulate_single_source simulate_process_recipe
 correction_board_weights correction_board_field selected_point_weights
 planet_center_occlusion_ring export_pdf open_window update_window window_action
 close_window estimate_compute_task estimate_film_optimization submit_film_optimization
 submit_film_synthesis submit_film_sensitivity
 estimate_appearance_render submit_appearance_render request_compute_confirmation
 submit_mask_weights submit_mask_field submit_point_weights submit_python_script
 submit_single_source submit_process_recipe create_default_workflow create_design_workflow
 advance_design_workflow design_workflow_status cancel_design_workflow
 estimate_mask_optimization submit_mask_optimization read_calculation_result'''.split())


def _ito_quantity_schema(units: list[str]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False,
            "required": ["value", "unit", "provenance", "source"],
            "properties": {
                "value": {"type": "number", "exclusiveMinimum": 0},
                "unit": {"type": "string", "enum": units},
                "provenance": {"type": "string", "enum": ["theoretical", "measured", "assumed"]},
                "source": {"type": "string", "minLength": 1, "pattern": r"\S"},
                "standard_uncertainty": {"type": "number", "minimum": 0}}}


ITO_ELECTRICAL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["request"],
    "properties": {"request": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "operation": {"type": "string", "enum": ["solve"], "default": "solve"},
            "thickness": _ito_quantity_schema(["m", "cm", "mm", "um", "nm"]),
            "sheet_resistance": _ito_quantity_schema(["ohm/sq"]),
            "resistivity": _ito_quantity_schema(["ohm*m", "ohm*cm"]),
            "rectangle": {"type": "object", "additionalProperties": False,
                "required": ["resistance", "length", "width", "electrode_boundary", "contact_assumption"],
                "properties": {
                    "resistance": _ito_quantity_schema(["ohm"]),
                    "length": _ito_quantity_schema(["m", "cm", "mm", "um", "nm"]),
                    "width": _ito_quantity_schema(["m", "cm", "mm", "um", "nm"]),
                    "electrode_boundary": {"const": "full_width_opposite_edges"},
                    "contact_assumption": {"const": "negligible_or_corrected"}}},
            "uncertainty_assumption": {"const": "independent"},
            "consistency_relative_tolerance": {"type": "number", "minimum": 0, "maximum": 1, "default": 1e-6}}}}}


def tool_annotations(name: str) -> ToolAnnotations:
    if name in POLICIES:
        read_only = POLICIES[name].effect == 'read'
    elif name in _READ:
        read_only = True
    elif name in _WRITE:
        read_only = False
    else:
        raise ValueError(f'MCP tool needs an explicit effect classification: {name}')
    # Write-capable adapters may overwrite, cancel, replace or run scripts;
    # do not promise non-destructiveness or replay safety from their names.
    # OS-local tools can still invoke configured remote compute or scripts, so
    # conservatively retain an open-world hint for all operations.
    return ToolAnnotations.model_validate({
        'readOnlyHint': read_only, 'destructiveHint': not read_only,
        'idempotentHint': read_only, 'openWorldHint': True})


_StrictValidator = validators.extend(Draft202012Validator, type_checker=
    Draft202012Validator.TYPE_CHECKER.redefine('integer', lambda _, value: type(value) is int))


def _finite_json(value: Any) -> bool:
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    # Existing in-process domain serializers use tuples inside Any payloads;
    # json.dumps represents them as arrays. Declared array fields still obey
    # their published schema (JSON lists), without broad SDK coercion.
    if type(value) in (list, tuple):
        return all(_finite_json(item) for item in value)
    if type(value) is dict:
        return all(type(key) is str and _finite_json(item) for key, item in value.items())
    return False


def validate_mcp_arguments(schema: dict[str, Any], arguments: Any) -> dict[str, Any]:
    values = {} if arguments is None else arguments
    try:
        finite = type(values) is dict and _finite_json(values)
    except RecursionError:
        finite = False
    if not finite:
        raise CapabilityError('invalid_arguments', 'arguments must contain finite JSON values', effect='none')
    error = next(_StrictValidator(schema).iter_errors(values), None)
    if error is not None:
        # Do not echo request values (which can include private design content).
        path = '.'.join(map(str, error.absolute_path)) or 'arguments'
        raise CapabilityError('invalid_arguments',
                              f'{path}: violates published {error.validator} constraint', effect='none')
    return values
