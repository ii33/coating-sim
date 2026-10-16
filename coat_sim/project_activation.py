"""Exact existing-owner activation; never creates a writable project session."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from coat_sim.core.project_store import ProjectStore
from coat_sim.ui.control import UiControlError


ACTIVATION_FIELDS = frozenset({"client_id", "client_generation", "project_id",
                               "session_generation", "root", "project_file"})


def canonical_path(value: str | Path) -> str:
    return os.path.normcase(str(Path(value).expanduser().resolve()))


def activation_target(client: dict[str, Any], project: dict[str, Any]) -> dict[str, str]:
    return {"client_id": client["client_id"], "client_generation": client["generation"],
            "project_id": project["project_id"], "session_generation": project["generation"],
            "root": project["root"], "project_file": project["project_file"]}


def remote_project_entries(clients: list[dict[str, Any]], own_generation: str) -> list[dict[str, Any]]:
    """Detached UI rows, including unavailable owners without claiming ownership."""
    rows = []
    for client in clients:
        if client.get("generation") == own_generation:
            continue
        for project in client.get("project_sessions", []):
            if any(not isinstance(project.get(key), str) or not project[key]
                   for key in ("project_id", "generation", "root", "project_file")):
                continue
            if any(not isinstance(client.get(key), str) or not client[key]
                   for key in ("client_id", "generation")):
                continue
            rows.append({"projectId": project["project_id"], "name": project.get("name", ""),
                         "root": project["root"], "descriptorFile": project["project_file"],
                         "active": False, "defaultState": "remote_session", "defaultFilmSnapshot": False,
                         "defaultFilmDraft": False, "remote": True,
                         "available": client.get("state") == "alive",
                         "sessionGeneration": project["generation"],
                         "activationTarget": activation_target(client, project)})
    return rows


def activate_shared_project(client: Any, target: dict[str, str]) -> dict[str, Any]:
    """Only an explicit successful, identity-matching owner response is success."""
    try:
        result = client.request("activate_project", **target)
    except UiControlError as exc:
        if exc.code in {"invalid_request", "invalid_operation", "unsupported_operation"}:
            raise UiControlError("activation_unsupported", "当前共享助手版本不支持工程会话激活，请从原工程窗口或助手窗口菜单进入。") from exc
        raise UiControlError(exc.code, "无法激活已有工程会话；原窗口和当前内容仍保留。" + str(exc)) from exc
    if (not isinstance(result, dict) or result.get("activated") is not True
            or not isinstance(result.get("instance_id"), str) or not result["instance_id"].strip()
            or any(result.get(key) != target[key]
                   for key in ("project_id", "session_generation", "client_generation"))):
        raise UiControlError("activation_unconfirmed", "已有工程窗口尚未确认激活，请稍后重试；未创建第二个可写会话。")
    return result


def find_shared_project_owner(client: Any, store: ProjectStore, own_generation: str) -> dict[str, str] | None:
    """Match ID, root and the selected descriptor; copied IDs never reroute."""
    try:
        clients = client.list_clients()
    except Exception as exc:
        raise UiControlError("project_discovery_unavailable", "暂时无法查询已有工程会话，请稍后重试；未改变工程当前内容。") from exc
    matches = []
    for candidate in clients:
        if candidate.get("generation") == own_generation:
            continue
        for project in candidate.get("project_sessions", []):
            same_id = project.get("project_id") == store.manifest["id"]
            same_root = canonical_path(project["root"]) == canonical_path(store.root)
            if not same_id and not same_root:
                continue
            if not same_id or not same_root:
                raise UiControlError("project_identity_conflict", "此工程标识或目录已由另一个工程会话使用，请在原工程中处理副本身份。")
            if canonical_path(project["project_file"]) != canonical_path(store.project_file):
                raise UiControlError("project_descriptor_conflict", "此目录已由另一个工程文件打开，请从原工程窗口进入。")
            if candidate.get("state") != "alive":
                raise UiControlError("project_owner_unavailable", "此工程仍有已登记的会话，但当前无法联系原窗口；请保留原窗口并稍后重试。")
            matches.append(activation_target(candidate, project))
    if len(matches) > 1:
        raise UiControlError("project_owner_conflict", "此工程有多个已登记会话，暂不能安全激活；原窗口和当前内容均保留。")
    return matches[0] if matches else None
