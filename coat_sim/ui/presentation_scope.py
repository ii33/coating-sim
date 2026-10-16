"""Small, Qt-free capability boundary for the three desktop host modes."""
from __future__ import annotations
from typing import Any, Iterable

GLOBAL_WINDOWS = frozenset({"launcher", "settings", "about"})


def presentation_scope(manager: Any) -> str:
    if getattr(manager, "project_runtime", None) is not None:
        return "project_runtime"
    if getattr(manager, "project_launcher", None) is not None:
        return "launcher"
    return "standalone"


def legacy_window_types(manager: Any, window_types: Iterable[str]) -> list[str]:
    scope = presentation_scope(manager)
    if scope == "project_runtime":
        return []
    return [key for key in window_types if scope == "standalone" or key in GLOBAL_WINDOWS]


def legacy_window_scope_error(manager: Any, key: str) -> tuple[str, str] | None:
    scope = presentation_scope(manager)
    if scope == "standalone" or (scope == "launcher" and key in GLOBAL_WINDOWS):
        return None
    if scope == "project_runtime" and key in GLOBAL_WINDOWS:
        return ("global_window_unavailable_in_runtime", "全局窗口由独立启动器承载，请打开启动器。")
    return ("project_identity_required", "工程窗口请求必须指定已验证的工程与会话身份。")
