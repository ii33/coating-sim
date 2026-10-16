"""User-facing metadata aliases without changing numerical input semantics."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from .project_dependencies import ProjectError, normalize_document

# The aliases are presentation vocabulary, not a new interpretation of the
# design's physical fields. Optional additions stay absent in older records.
INFORMATION_FIELDS = ("name", "summary", "notes")
_INFORMATION_PATHS = {
    **{kind: ("name", "description", "notes") for kind in
       ("source", "substrate", "mask", "machine", "process", "deposition_configuration",
        "raytrace_request", "appearance_configuration")},
    "film": ("name", "summary", "note"),
    "expression_template": ("name", "summary", "note"),
}


def resource_display_name(kind: str, data: Mapping) -> str:
    """Names never borrow overview text, file paths or stable identities."""
    if data.get("name"):
        return str(data["name"])
    if kind in {"material_profile", "optical_material"} and data.get("material", {}).get("name"):
        return str(data["material"]["name"])
    labels = {"source": "源", "substrate": "基片", "mask": "修正板", "machine": "机器",
              "process": "工艺", "film": "膜系", "expression_template": "表达式模板",
              "deposition_configuration": "计算配置", "raytrace_request": "光追配置",
              "appearance_configuration": "外观配置", "binary_asset": "输入资产",
              "material_profile": "材料", "optical_material": "光学材料"}
    return "未命名" + labels.get(kind, "资源")


def resource_information(kind: str, data: Mapping, *, token: str = "", fallback_name="") -> dict:
    fields = _INFORMATION_PATHS.get(kind)
    if fields is None:
        raise ProjectError("unsupported_resource", "This resource has no editable design information")
    result = {alias: data.get(field, "") for alias, field in zip(INFORMATION_FIELDS, fields, strict=True)}
    if not result["name"]:
        result["name"] = fallback_name
    return {**result, "editable_fields": list(INFORMATION_FIELDS), "token": token}


def with_resource_information(kind: str, data: Mapping, information: Mapping) -> dict:
    fields = _INFORMATION_PATHS.get(kind)
    if fields is None:
        raise ProjectError("unsupported_resource", "This resource has no editable design information")
    if not isinstance(information, Mapping) or set(information) - set(INFORMATION_FIELDS):
        raise ProjectError("invalid_document", "Information accepts name, summary and notes only")
    updated = deepcopy(dict(data))
    for alias, field in zip(INFORMATION_FIELDS, fields, strict=True):
        if alias in information:
            value = information[alias]
            maximum = 100 if alias == "name" and kind in {"process", "deposition_configuration"} else 120 if alias == "name" else 20000
            if not isinstance(value, str) or len(value) > maximum or (alias == "name" and not value.strip()):
                raise ProjectError("invalid_document", alias + " must be valid text up to " + str(maximum) + " characters")
            updated[field] = value.strip() if alias == "name" else value
    return normalize_document(kind, updated)
