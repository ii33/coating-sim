"""Explicit scope-bound, recoverable asset removal; never permanent deletion.

Browser tokens name saved assets, not arbitrary paths or foreground editors.
Only one selected asset is admitted; its material/optics projections are shown
together. Immutable project inputs remain available to their frozen consumers.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from .project_dependencies import (MAX_FILE_BYTES, ProjectError, identifier, json_bytes,
                                   normalize_document, read_json, reject_links)
from .project_paths import managed_document_path
from .resource_information import resource_display_name


def _digest(value: Any) -> str:
    return hashlib.sha256(json_bytes(value)).hexdigest()


def _block(code: str, message: str, **details) -> dict:
    return {"code": code, "message": message, **details}


def _fsync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def library_view_owner_matches(service, controller) -> bool:
    """Validate an internal presentation registration using object identity."""
    host: Any = service._session_host
    binding = service.binding
    if host is None or binding is None:
        return False
    resources: Any = host.resource_for(binding, "resource_workspace")
    if resources is None:
        return False
    window = getattr(controller, "window", None)
    controller_service = getattr(controller, "service", None)
    registered_window = resources.windows.get("materials")
    owns_route = any(route.module_key == "project_resources" and host.window_for(route) is window
                     for route in host.routes(binding.project_id))
    if (resources.controllers.get("materials") is not controller
            or getattr(controller, "owner_binding", None) != binding
            or getattr(controller, "resource_workspace", None) is not resources
            or window is None or registered_window is not window
            or getattr(controller_service, "_session_host", None) is not host
            or getattr(controller_service, "binding", None) != binding
            or getattr(controller_service, "store", None) is not service.store):
        return False
    return owns_route


def _selection(snapshot: dict, selected_ids: list[str]) -> dict:
    if not isinstance(selected_ids, list) or len(selected_ids) != 1:
        raise ProjectError("invalid_selection", "每次确认只回收一个资产；不会批量或级联删除。")
    identity = identifier(selected_ids[0])
    records = {row["id"]: row for row in snapshot["manifest"]["documents"]}
    if identity not in records:
        raise ProjectError("invalid_selection", "所选资产不属于当前已载入资源库。")
    affected = {identity}
    blockers = []
    kind = records[identity]["kind"]
    if kind in {"material_profile", "optical_material"}:
        material_id = snapshot["documents"][identity]["material"]["id"]
        companions = {key for key, row in records.items()
                      if row["kind"] in {"material_profile", "optical_material"}
                      and snapshot["documents"][key]["material"]["id"].casefold() == material_id.casefold()}
        if kind == "optical_material" and any(records[key]["kind"] == "material_profile" for key in companions):
            blockers.append(_block("paired_material", "此光学数据属于材料资产，请选择材料资产并审阅其组成后回收。",
                                   resource_ids=sorted(companions)))
        elif kind == "material_profile":
            affected.update(companions)
    default = snapshot["manifest"].get("default_configuration_id")
    if default in affected:
        blockers.append(_block("default_asset", "当前默认沉积配置不能回收，请先明确切换默认配置。",
                               resource_ids=[default]))
    for key, row in records.items():
        referenced = sorted(affected.intersection(row["dependencies"]))
        if key not in affected and referenced:
            blockers.append(_block("referenced_asset", "仍有已保存资产引用所选资产；不会级联删除。",
                                   resource_id=key, kind=row["kind"],
                                   name=resource_display_name(row["kind"], snapshot["documents"][key]),
                                   references=referenced))
    rows = [{"id": key, "kind": records[key]["kind"],
             "name": resource_display_name(records[key]["kind"], snapshot["documents"][key]),
             "selected": key == identity, "same_asset_component": key != identity}
            for key in sorted(affected)]
    return {"selected_ids": [identity], "affected_ids": sorted(affected), "rows": rows,
            "blockers": blockers, "action": "recycle_asset"}


class AssetDeletionService:
    """Per browser-service plans, invalidated by catalog close/replacement."""

    def __init__(self, service):
        self.service = service
        self.scope = "project" if getattr(service, "is_project_service", False) else "global"
        self._lock = RLock()
        self._plans: dict[str, dict] = {}

    @property
    def _exchange(self):
        return (self.service.resource_exchange if self.scope == "project"
                else self.service._exchange)

    def _catalog(self, catalog_id: str):
        if not isinstance(catalog_id, str):
            raise ProjectError("invalid_selection", "需要当前资源库的已载入目录。")
        self._assert_owner()
        self._exchange._assert_live()
        catalog = self._exchange._catalog(catalog_id)
        expected = str(self.service.store.project_file) if self.scope == "project" else "global"
        if catalog.source_type != self.scope or catalog.source_path != expected:
            raise ProjectError("wrong_scope", "只能回收当前资源库自身的资产，不能删除导入来源或另一工程。")
        return catalog

    def _assert_owner(self) -> None:
        if getattr(self.service, "_asset_deletion_closed", False):
            raise ProjectError("project_closed", "资产库服务已经关闭。")
        if self.scope == "project":
            service = self.service
            if service.store.manifest["id"] != service.project_id:
                raise ProjectError("wrong_scope", "工程资产所有者身份已变化。")
            if (service._session_host is not None
                    and service._session_host.store_for(service.binding) is not service.store):
                raise ProjectError("wrong_scope", "工程资产服务不属于当前会话。")
        else:
            from . import data_files
            if self.service.root != data_files.data_path("data"):
                raise ProjectError("wrong_scope", "全局资产库目录已变化，请重新打开资源库。")

    def _live_blockers(self, affected: list[str], snapshot: dict) -> list[dict]:
        if self.scope != "project" or self.service._session_host is None:
            return []
        host, binding = self.service._session_host, self.service.binding
        resources = host.resource_for(binding, "resource_workspace")
        blockers = []
        affected_set = set(affected)
        records = {row["id"]: row for row in snapshot["manifest"]["documents"]}
        if resources is not None:
            for module, record in resources._pending_recovery.items():
                if record.get("dirty") or record.get("nested_material", {}).get("dirty"):
                    blockers.append(_block("unsaved_recovery", "此工程仍有未处理的恢复编辑，请先保存或明确放弃。", module=module))
            for module, controller in tuple(resources.controllers.items()):
                bridge = controller.bridge
                nested = getattr(bridge, "_material_library", None)
                dirty = (bool(getattr(bridge, "_dirty", False)) or bool(getattr(nested, "_dirty", False))
                         or controller._pending_ui is not None or controller._close_pending_ui is not None
                         or controller._pending_information_edit is not None)
                if dirty:
                    blockers.append(_block("unsaved_editor", "此编辑窗口仍有未保存内容，无法证明其依赖可安全移除。", module=module))
                    continue
                # Read plain controller/model ownership only, never Qt native
                # text properties from an exchange worker thread.
                path = getattr(bridge, "_path", None)
                current = self.service._identity_for_path(path) if path else None
                material_id = getattr(bridge, "_saved_id", None)
                if material_id:
                    current = next((key for key, row in records.items() if row["kind"] == "material_profile"
                                    and snapshot["documents"][key]["material"]["id"] == material_id), None)
                related = current in affected_set or bool(affected_set.intersection(records.get(current, {}).get("dependencies", [])))
                own_clean_details = (controller is getattr(self.service, "_asset_library_view_owner", None)
                                     and library_view_owner_matches(self.service, controller))
                if controller.window is not None and related and not own_clean_details:
                    blockers.append(_block("active_editor", "另一编辑窗口正在使用此资产，请先关闭该窗口或切换其资源。",
                                           module=module, resource_id=current))
        film = host.resource_for(binding, "film_workspace")
        editor = getattr(film, "_editor", None)
        if editor is not None and (editor.snapshot().dirty or getattr(editor, "_pending_information_edit", None)):
            blockers.append(_block("unsaved_editor", "膜系窗口仍有未保存编辑，请先保存或明确放弃。", module="film_design"))
        compute = host.resource_for(binding, "compute_workspace")
        if compute is not None:
            jobs = compute.active_jobs()
            if jobs:
                blockers.append(_block("active_jobs", "此工程仍有运行任务，无法证明删除对其输入无影响，请等待明确终态。",
                                       job_ids=[str(row.get("id", row.get("job_id", ""))) for row in jobs]))
            if compute.has_pending_draft:
                blockers.append(_block("unsaved_compute", "此工程仍有待处理的计算输入恢复内容。"))
        return blockers

    def preview(self, catalog_id: str, selected_ids: list[str]) -> dict:
        with self.service._operation():
            catalog = self._catalog(catalog_id)
            snapshot = deepcopy(catalog.snapshot)
            if self.scope == "global":
                from .global_asset_recycle import prepare_global_deletion
                selection = prepare_global_deletion(snapshot, selected_ids)
                records = {row["id"]: row for row in snapshot["manifest"]["documents"]}
                rows = [{"id": key, "kind": records[key]["kind"],
                         "name": resource_display_name(records[key]["kind"], snapshot["documents"][key]),
                         "selected": key in selection["selected_ids"],
                         "same_asset_component": key not in selection["selected_ids"]}
                        for key in selection["affected_ids"]]
            else:
                selection = _selection(snapshot, selected_ids)
                selection["blockers"].extend(self._live_blockers(selection["affected_ids"], snapshot))
                rows = selection["rows"]
            plan_id = uuid4().hex
            public = {key: deepcopy(selection[key]) for key in
                      ("selected_ids", "affected_ids", "blockers", "action")}
            public["rows"] = deepcopy(rows)
            public.update(plan_id=plan_id, scope=self.scope, can_apply=not public["blockers"])
            public["snapshot_sha256"] = _digest(snapshot)
            if self.scope == "global":
                public.update(owner_root=selection["owner_root"], backend=selection["backend"])
                messages = {
                    "protected_builtin": "原始内置材料没有可回收的用户覆盖，不能删除内置来源。",
                    "unsupported_payload_ownership": "暂不能证明此二进制资产的共享载荷归属，未开放回收。",
                    "incoming_dependency": "仍有已保存的全局资产引用所选资产；不会级联删除。",
                    "default_resource": "当前默认资源不能回收，请先明确切换默认项。",
                }
                for blocker in public["blockers"]:
                    blocker.setdefault("message", messages.get(blocker["code"], "此资产存在回收阻塞。"))
                    resource_id = blocker.get("id")
                    if resource_id in records:
                        blocker.update(resource_id=resource_id, kind=records[resource_id]["kind"],
                                       name=resource_display_name(records[resource_id]["kind"], snapshot["documents"][resource_id]))
            if self.scope == "project":
                public.update(project_id=self.service.project_id,
                              session_generation=getattr(self.service.binding, "generation", None),
                              revision=snapshot["manifest"]["revision"], owner_root=str(self.service.store.root))
            with self._lock:
                if len(self._plans) >= 16:
                    self._plans.pop(next(iter(self._plans)))
                self._plans[plan_id] = {"catalog_id": catalog_id, "snapshot": snapshot,
                                       "selection": selection, "public": public}
            return deepcopy(public)

    def cancel(self, plan_id: str) -> None:
        with self._lock:
            self._plans.pop(plan_id, None)

    def apply(self, plan_id: str) -> dict:
        identifier(plan_id)
        with self.service._operation(mutate=True), self._lock:
            plan = self._plans.get(plan_id)
            if plan is None:
                raise ProjectError("expired_plan", "回收确认已失效，请重新预览。")
            catalog = self._catalog(plan["catalog_id"])
            if _digest(catalog.snapshot) != _digest(plan["snapshot"]):
                raise ProjectError("stale_source", "资源库已变化，请重新审阅回收计划。")
            if plan["public"]["blockers"]:
                raise ProjectError("deletion_blocked", "所选资产存在明确阻塞，未作任何移除。")
            if self.scope == "global":
                from .global_asset_recycle import apply_global_deletion
                result = apply_global_deletion(plan["snapshot"], plan["selection"])
                result["recoverable"] = result.get("state") == "active"
            else:
                blockers = self._live_blockers(plan["selection"]["affected_ids"], plan["snapshot"])
                if blockers:
                    raise ProjectError("deletion_blocked", "编辑/任务状态已变化，请重新预览回收计划。")
                result = self._apply_project(plan)
            self._plans.pop(plan_id, None)
            return result

    def _base(self) -> Path:
        # Existing imports is already an owned layout directory; this adds no
        # new project root entry or input-package inclusion.
        root = self.service.store.root / "imports" / ".asset-recycle"
        reject_links(root)
        if root.exists() and not root.is_dir():
            raise ProjectError("unsafe_path", "工程回收区必须是普通目录。")
        return root

    def _receipt_path(self, receipt_id: str) -> Path:
        identifier(receipt_id)
        path = self._base() / receipt_id / "receipt.json"
        reject_links(path)
        return path

    def _read_receipt(self, receipt_id: str) -> dict:
        path = self._receipt_path(receipt_id)
        value, content = read_json(path)
        if (len(content) > MAX_FILE_BYTES or not isinstance(value, dict)
                or set(value) != {"kind", "version", "scope", "receipt_id", "owner_id", "owner_root",
                                  "created_at", "records", "documents", "content_sha256"}
                or value["kind"] != "coat_sim.asset_recycle" or type(value["version"]) is not int
                or value["version"] != 1 or value["scope"] != "project" or value["receipt_id"] != receipt_id
                or value["owner_id"] != self.service.project_id or value["owner_root"] != str(self.service.store.root)
                or value["content_sha256"] != _digest([value["records"], value["documents"]])):
            raise ProjectError("invalid_archive", "回收记录损坏或不属于当前工程。")
        records, documents = value["records"], value["documents"]
        if not isinstance(records, list) or not 1 <= len(records) <= 2 or not isinstance(documents, dict):
            raise ProjectError("invalid_archive", "回收记录组成无效。")
        identities = set()
        for row in records:
            if not isinstance(row, dict) or set(row) != {"id", "kind", "path", "sha256", "dependencies"}:
                raise ProjectError("invalid_archive", "回收记录字段无效。")
            identity = identifier(row["id"])
            managed_document_path(row["path"], row["kind"], identity)
            if identity in identities or identity not in documents:
                raise ProjectError("invalid_archive", "回收资产身份无效。")
            identities.add(identity)
            data = normalize_document(row["kind"], documents[identity])
            if data != documents[identity] or hashlib.sha256(json_bytes(data)).hexdigest() != row["sha256"]:
                raise ProjectError("invalid_archive", "回收资产内容校验失败。")
            if not isinstance(row["dependencies"], list):
                raise ProjectError("invalid_archive", "回收资产依赖无效。")
            for dependency in row["dependencies"]:
                identifier(dependency)
        if set(documents) != identities:
            raise ProjectError("invalid_archive", "回收记录含未声明资产。")
        return value

    def _apply_project(self, plan: dict) -> dict:
        store = self.service.store
        expected_fingerprint = store._fingerprint
        current = store.snapshot_saved_inputs()
        if _digest(current) != _digest(plan["snapshot"]):
            raise ProjectError("stale_project", "工程已变化，请重新预览回收。")
        selection = _selection(current, plan["selection"]["selected_ids"])
        if selection["blockers"] or selection["affected_ids"] != plan["selection"]["affected_ids"]:
            raise ProjectError("deletion_blocked", "工程引用关系已变化，未作移除。")
        affected = set(selection["affected_ids"])
        records = {row["id"]: deepcopy(row) for row in current["manifest"]["documents"]}
        documents = deepcopy(current["documents"])
        archived_records = [records[key] for key in sorted(affected)]
        archived_data = {key: documents[key] for key in sorted(affected)}
        receipt_id = uuid4().hex
        receipt = {"kind": "coat_sim.asset_recycle", "version": 1, "scope": "project",
                   "receipt_id": receipt_id, "owner_id": self.service.project_id,
                   "owner_root": str(store.root), "created_at": datetime.now(timezone.utc).isoformat(),
                   "records": archived_records, "documents": archived_data,
                   "content_sha256": _digest([archived_records, archived_data])}
        encoded = json_bytes(receipt)
        if len(encoded) > MAX_FILE_BYTES:
            raise ProjectError("capacity_exceeded", "回收资产记录超出安全容量，未作移除。")
        base = self._base()
        base.mkdir(exist_ok=True)
        reject_links(base)
        path = self._receipt_path(receipt_id)
        path.parent.mkdir()
        with path.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        # Persist both the receipt entry and newly-created archive directories
        # before the manifest can stop naming the active asset.
        for directory in (path.parent, base, base.parent):
            _fsync_directory(directory)
        # Archive first. A failed publication leaves an inert recoverable copy,
        # never a missing asset or a permanent-delete fallback.
        for key in affected:
            records.pop(key)
            documents.pop(key)
        store._publish(records, documents, expected_fingerprint=expected_fingerprint)
        return {"receipt_id": receipt_id, "scope": "project", "recovery_path": str(path.parent),
                "affected_ids": sorted(affected), "action": "recycle_asset", "recoverable": True}

    def list_receipts(self) -> list[dict]:
        with self.service._operation():
            self._assert_owner()
            self._exchange._assert_live()
            if self.scope == "global":
                from .global_asset_recycle import list_global_deletions
                return [row | {"recoverable": row.get("state") == "active"}
                        for row in list_global_deletions()]
            base = self._base()
            if not base.exists():
                return []
            active = {row["id"] for row in self.service.store.manifest["documents"]}
            rows = []
            for folder in sorted(base.iterdir()):
                try:
                    receipt = self._read_receipt(folder.name)
                except (OSError, ValueError, KeyError, TypeError):
                    continue  # Untrusted/incomplete records are never restorable UI entries.
                ids = [row["id"] for row in receipt["records"]]
                if not active.intersection(ids):
                    rows.append({"receipt_id": folder.name, "scope": "project", "affected_ids": ids,
                                 "created_at": receipt["created_at"], "recovery_path": str(folder),
                                 "recoverable": True})
            return rows

    def restore(self, receipt_id: str) -> dict:
        with self.service._operation(mutate=True), self._lock:
            self._assert_owner()
            self._exchange._assert_live()
            if self.scope == "global":
                from .global_asset_recycle import restore_global_deletion
                result = restore_global_deletion(receipt_id)
                return result | {"restored": result.get("state") == "restored"}
            receipt = self._read_receipt(receipt_id)
            expected_fingerprint = self.service.store._fingerprint
            current = self.service.store.snapshot_saved_inputs()
            affected = [row["id"] for row in receipt["records"]]
            blockers = self._live_blockers(affected, current)
            if blockers:
                raise ProjectError("deletion_blocked", "当前工程仍有相关编辑或任务，暂不能恢复资产。")
            records = {row["id"]: row for row in current["manifest"]["documents"]}
            if set(affected).intersection(records):
                raise ProjectError("destination_exists", "同身份资产已存在；恢复不会覆盖当前内容。")
            documents = deepcopy(current["documents"])
            records.update({row["id"]: row for row in receipt["records"]})
            documents.update(receipt["documents"])
            self.service.store._publish(records, documents, expected_fingerprint=expected_fingerprint)
            return {"receipt_id": receipt_id, "scope": "project", "affected_ids": affected,
                    "restored": True, "recovery_path": str(self._receipt_path(receipt_id).parent)}
