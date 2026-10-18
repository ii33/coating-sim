"""Qt-free, bounded contract for an authenticated project presentation target."""
from __future__ import annotations

from typing import Annotated, Any
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from coat_sim.project_modules import PROJECT_MODULES
from coat_sim.control_errors import UiControlError

PROJECT_WINDOW_MODULES = frozenset(key for key, _label in PROJECT_MODULES)
# Module-specific readable collections; this does not grant editor mutations.
_FILM_SNAPSHOT_DETAILS = frozenset({"summary", "layers", "materials", "objectives", "variables"})
PROJECT_EDITOR_DETAILS = {
    "film_design": _FILM_SNAPSHOT_DETAILS,
    "film_simulation": _FILM_SNAPSHOT_DETAILS,
    "film_optimization": _FILM_SNAPSHOT_DETAILS,
    "source": frozenset({"summary", "apertures", "materials"}),
    "substrate": frozenset({"summary", "points", "materials"}),
    "machine": frozenset({"summary", "sources", "masks", "monitors"}),
    "mask": frozenset({"summary", "contour_points"}),
    "process": frozenset({"summary", "steps", "sources"}),
    "main": frozenset({"summary"}),
    "materials": frozenset({"summary", "catalog", "optical_sets"}),
    "ray_trace": frozenset({"summary", "samples", "surfaces", "spectrum", "layers"}),
    "film_appearance": frozenset({"summary", "captured_steps"}),
    "compute_queue": frozenset({"summary", "jobs"}),
    "project_resources": frozenset({"summary", "resources"}),
    "project_bbs": frozenset({"summary"}),
}
PROJECT_WINDOW_NAVIGATION = ("list_windows", "open_window", "read_window", "close_window")
PROJECT_WINDOW_OPERATIONS = frozenset({"list_windows", "open_window", "read_window",
                                      "update_window", "window_action", "close_window",
                                      "project_editor_snapshot"})


NonemptyStrictString = Annotated[StrictStr, Field(min_length=1)]


class ProjectTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    client_id: NonemptyStrictString
    client_generation: NonemptyStrictString
    project_id: NonemptyStrictString
    session_generation: NonemptyStrictString
    root: NonemptyStrictString
    project_file: NonemptyStrictString

    @field_validator("*")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("Project identity fields cannot be empty or contain NUL")
        return value


def project_target_data(value: Any) -> dict[str, str]:
    try:
        target = ProjectTarget.model_validate(value.model_dump() if isinstance(value, ProjectTarget) else value)
        return target.model_dump()
    except (ValueError, TypeError) as exc:
        raise UiControlError("invalid_arguments", "工程窗口需要完整且精确的会话身份。") from exc


def project_window_arguments(operation: str, arguments: Any) -> dict[str, Any]:
    if not isinstance(operation, str) or operation not in PROJECT_WINDOW_OPERATIONS:
        raise UiControlError("invalid_operation", "不支持的工程窗口操作。")
    if not isinstance(arguments, dict):
        raise UiControlError("invalid_arguments", "工程窗口参数须为对象。")
    common = {"window_type"}
    fields = {"fields", "design_path", "data", "intended_path", "pointers", "force"}
    allowed = {"list_windows": set(), "open_window": common | fields,
               "read_window": common, "update_window": common | fields | {"instance_id", "expected_snapshot_id"},
               "window_action": common | {"action", "arguments", "instance_id", "expected_snapshot_id"},
               "close_window": common | {"force"},
               "project_editor_snapshot": common | {"detail", "offset", "limit", "expected_snapshot_id"}}[operation]
    if set(arguments) - allowed:
        raise UiControlError("invalid_arguments", "未知工程窗口参数。")
    if operation != "list_windows" and (not isinstance(arguments.get("window_type"), str)
            or arguments["window_type"] not in PROJECT_WINDOW_MODULES):
        raise UiControlError("invalid_window", "此工程目标只接受已登记的工程模块。")
    if operation == "project_editor_snapshot":
        details = PROJECT_EDITOR_DETAILS.get(arguments["window_type"], frozenset())
        if not details:
            raise UiControlError("invalid_window", "该工程模块不支持读取当前编辑内容。")
        if not isinstance(arguments.get("detail", "summary"), str) or arguments.get("detail", "summary") not in details:
            raise UiControlError("invalid_arguments", "该工程模块不支持查看此类编辑详情。")
        for name, default, low, high in (("offset", 0, 0, 2 ** 31 - 1), ("limit", 50, 1, 100)):
            value = arguments.get(name, default)
            if type(value) is not int or not low <= value <= high:
                raise UiControlError("invalid_arguments", "编辑内容分页参数无效。")
        expected = arguments.get("expected_snapshot_id")
        if expected is not None and (not isinstance(expected, str) or len(expected) != 64
                                     or any(ch not in "0123456789abcdef" for ch in expected)):
            raise UiControlError("invalid_arguments", "expected_snapshot_id 须为返回的完整快照标识。")
    if operation in {"update_window", "window_action"}:
        if any(key in arguments for key in {"force", "data", "design_path", "intended_path", "pointers"}):
            raise UiControlError("invalid_arguments", "受控编辑不接受force或文档/路径替换。")
        expected = arguments.get("expected_snapshot_id")
        if not isinstance(expected, str) or len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
            raise UiControlError("requires_snapshot", "请先读取当前编辑器快照，并提交完整expected_snapshot_id。")
        if not isinstance(arguments.get("instance_id"), str) or not arguments["instance_id"].strip():
            raise UiControlError("window_identity_required", "请提交当前窗口的instance_id。")
    if "force" in arguments and type(arguments["force"]) is not bool:
        raise UiControlError("invalid_force", "force 须为布尔值。")
    if operation == "window_action" and (not isinstance(arguments.get("action"), str)
            or not arguments["action"] or arguments.get("arguments") is not None
            and not isinstance(arguments["arguments"], dict)):
        raise UiControlError("invalid_arguments", "窗口动作须有明确名称和对象参数。")
    return dict(arguments)


def project_window_capabilities() -> dict[str, Any]:
    return {"operations": list(PROJECT_WINDOW_NAVIGATION),
            "unsupported_operations": ["update_window", "window_action"],
            "content_scope": "window_status", "editor_snapshot": False,
            "current_context_tool": "project_editor_snapshot",
            "current_context_modules": [key for key, _label in PROJECT_MODULES if key in PROJECT_EDITOR_DETAILS],
            "force_bypasses_review": False}
