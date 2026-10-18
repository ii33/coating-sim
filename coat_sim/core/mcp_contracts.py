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

# Shared domain operations take their effects directly from POLICIES. These
# explicit classifications cover the remaining MCP adapters, including optional
# development tools. New tools must be classified deliberately.
_READ = frozenset('''project_management_catalog project_list project_inspect
project_resolve project_bbs_list project_bbs_read project_bbs_history project_research_search project_research_read developer_status
developer_files python_sandbox_status list_data_files read_data_file
read_software_manual algorithm_reference_catalog algorithm_reference_search
algorithm_reference_read project_knowledge_search material_data_readiness
preview_film_stack_import read_process_layers
film_design_schema source_quadrature convert_gear_geometry sample_substrate_outline
sample_substrate_surface calculation_history estimate_calculation convert_time_fields
list_windows read_window project_editor_snapshot inspect_appearance_asset
wait_calculation_job mask_optimization_groups'''.split())
_WRITE = frozenset('''project_bbs_import_attachment project_bbs_comment project_bbs_write project_open project_research_save project_manage
 developer_report_issue developer_issue_reports developer_access developer_checks
 developer_persona run_python_script write_data_file move_data_path delete_data_path
 make_data_directory import_mask_dxf export_mask_dxf export_mask_drawing
 build_film_stack import_film_stack update_design_fields visualize_calculation_result
 render_design render_design_workflow save_design prepare_temporary_design
 save_generated_design save_simulation_asset export_simulation_asset create_material
 update_material copy_material simulate_single_source simulate_process_recipe
 correction_board_weights correction_board_field selected_point_weights
 planet_center_occlusion_ring export_pdf open_window update_window window_action
 close_window estimate_compute_task estimate_film_optimization submit_film_optimization
 estimate_appearance_render submit_appearance_render request_compute_confirmation
 submit_mask_weights submit_mask_field submit_point_weights submit_python_script
 submit_single_source submit_process_recipe create_default_workflow create_design_workflow
 advance_design_workflow design_workflow_status cancel_design_workflow
 estimate_mask_optimization submit_mask_optimization read_calculation_result'''.split())


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
