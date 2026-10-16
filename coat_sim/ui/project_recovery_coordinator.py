"""Explicit abnormal recovery and asynchronous project draft journalling."""
from __future__ import annotations

from concurrent.futures import Future
from copy import deepcopy
import json
from typing import Any, cast

from PySide6.QtCore import QObject, QTimer

from coat_sim.core.project_film_recovery import (
    film_recovery_current_token, restore_film_recovery,
    snapshot_film_recovery, validate_film_recovery,
)
from coat_sim.core.project_film_session import ProjectFilmWorkspace
from coat_sim.core.project_resource_workspace import ProjectResourceWorkspace
from coat_sim.core.project_compute import ProjectComputeWorkspace
from coat_sim.core.project_dependencies import ProjectError
from coat_sim.core.project_store import ProjectStore


class ProjectRecoveryCoordinator(QObject):
    def __init__(self, adapter: Any):
        super().__init__(adapter)
        self.adapter = adapter
        self.pending: dict[str, Future] = {}
        self._last: dict[tuple[str, str], str] = {}
        self._review: dict[str, tuple[dict, dict, str, str, int, str]] = {}
        self._pending_compute_restores: dict[str, tuple[tuple, str, str]] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(5000)
        self._timer.timeout.connect(self.checkpoint)
        self._timer.start()

    def _session(self, project_id: str):
        session = next((row for row in self.adapter.host.sessions()
                        if row.binding.project_id == project_id), None)
        if session is None:
            raise ProjectError("project_closed", "项目已关闭。")
        return session

    def capture(self, project_id: str) -> dict:
        session = self._session(project_id)
        host = self.adapter.host
        result: dict = {"kind": "coat_sim.project_recovery_checkpoint", "version": 1,
                        "project_id": project_id}
        for key, name in (("film_workspace", "film"), ("resource_workspace", "resources"),
                          ("compute_workspace", "compute")):
            workspace = host.resource_for(session.binding, key)
            if workspace is not None:
                if name == "film":
                    result[name] = snapshot_film_recovery(workspace)
                else:
                    snapshot = getattr(workspace, "snapshot_pending_edits", workspace.snapshot)
                    result[name] = snapshot()
        return result

    def _current_revision(self, session: Any) -> int:
        store = cast(ProjectStore, self.adapter.host.store_for(session.binding))
        return ProjectStore.open(store.project_file).manifest["revision"]

    def checkpoint(self) -> None:
        if self.adapter._stopped or self.adapter._busy or self.adapter.host.admission_frozen:
            return
        for project_id, lease in tuple(self.adapter._leases.items()):
            try:
                self._checkpoint_project(project_id, lease)
            except Exception as exc:
                pending = self._pending_compute_restores.pop(project_id, None)
                current_review = pending is None or self._review.get(project_id) is pending[0]
                if pending is not None and current_review:
                    self._review.pop(project_id, None)
                if current_review:
                    self.adapter._set_error(f"暂时无法记录或检查恢复内容：{exc}")
                # A stale/failed status for one project must not starve the
                # other projects' checkpoints or permanently block retry.

    def _checkpoint_project(self, project_id: str, lease: Any) -> None:
        session = self._session(project_id)
        cache_key = (project_id, session.binding.generation)
        if self.restore_pending(project_id):
            review, restore_token, _generation = self._pending_compute_restores[project_id]
            owner = ProjectComputeWorkspace.for_project(self.adapter.host, session.binding)
            status = owner.input_draft_restore_status(restore_token)
            if status["pending"]:
                return
            self._pending_compute_restores.pop(project_id, None)
            self._last.pop(cache_key, None)
            current_review = self._review.get(project_id) is review
            if not status["success"] and current_review:
                message = ("光学输入仅部分恢复，较新的未保存修改和恢复候选已保留："
                           if status.get("partial", False) else
                           "光学输入恢复失败，当前内容和恢复候选已保留：")
                self.adapter._set_error(message + status["error"])
            elif status["success"] and current_review:
                self.adapter._error = "所选类别的异常退出内容已恢复，尚未保存；计算任务不会自动重启。"
                signal = getattr(self.adapter, "projectsChanged", None)
                if signal is not None:
                    signal.emit()
            if current_review:
                self._review.pop(project_id, None)
        compute = self.adapter.host.resource_for(session.binding, "compute_workspace")
        if compute is not None and compute.input_draft_operations_pending:
            return  # Retain the last complete checkpoint while optical I/O runs.
        future = self.pending.get(project_id)
        if future is not None:
            if not future.done():
                return
            try:
                future.result()
            except Exception as exc:
                self._last.pop(cache_key, None)
                self.adapter._set_error(f"异常恢复检查点写入失败：{exc}")
            self.pending.pop(project_id, None)
        value = self.capture(project_id)
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
        if self._last.get(cache_key) == encoded:
            return
        self.pending[project_id] = self.adapter._executor.submit(lease.checkpoint, value)
        self._last[cache_key] = encoded

    def candidates(self, project_id: str) -> list[dict]:
        """List retained generations only when the user explicitly opens recovery."""
        session = self._session(project_id)
        lease = self.adapter._leases[project_id]
        rows = lease.recovery_candidates()
        current_revision = self._current_revision(session)
        pending = [{
            "generation": row["generation"],
            "revision": row["revision"],
            "updatedAt": row.get("updated_at", ""),
            "stale": bool(row["stale"]),
            "reviewed": False,
            "recordId": "",
            "sections": [key for key in ("film", "resources", "compute")
                         if key in row["checkpoint"]],
        } for row in rows]
        reviewed = [{
            "recordId": row["record_id"],
            "generation": row["generation"],
            "revision": row["revision"],
            "reviewedAt": row["reviewed_at"],
            "updatedAt": row["reviewed_at"],
            "stale": row["revision"] != current_revision,
            "reviewed": True,
            "sections": [key for key in ("film", "resources", "compute")
                         if key in row["checkpoint"]],
        } for row in lease.reviewed_recovery_candidates()]
        return pending + reviewed

    def review(self, project_id: str, generation: str, section: str, expected_revision: int,
               record_id: str = "") -> bool:
        session = self._session(project_id)
        lease = self.adapter._leases[project_id]
        # Explicit user action is the only path that examines prior-session data.
        if record_id:
            candidate = next((row for row in lease.reviewed_recovery_candidates()
                              if row["record_id"] == record_id), None)
            if candidate is not None:
                current_revision = self._current_revision(session)
                candidate = {**candidate, "stale": candidate["revision"] != current_revision}
        else:
            candidate = next((row for row in lease.recovery_candidates()
                              if row["generation"] == generation), None)
        if candidate is None or candidate["generation"] != generation:
            raise ProjectError("no_recovery", "没有可恢复的异常会话。")
        if type(expected_revision) is not int or candidate["revision"] != expected_revision:
            raise ProjectError("stale_recovery", "异常会话已变化，请重新选择。")
        if candidate["stale"]:
            raise ProjectError("stale_recovery", "异常会话对应的已保存工程版本已变化，不能恢复。")
        if section not in {"film", "resources", "compute"}:
            raise ProjectError("invalid_recovery", "未知恢复类别。")
        source = candidate["checkpoint"]
        if section not in source:
            raise ProjectError("no_recovery", "该异常会话没有此类别的可恢复内容。")
        value = {key: deepcopy(source[key]) for key in ("kind", "version", "project_id", section)}
        self._validate(session, value)
        self._review[project_id] = (value, self._section_token(session, section), section,
                                    generation, expected_revision, record_id)
        return True

    def _validate(self, session: Any, value: dict) -> None:
        if (not isinstance(value, dict) or value.get("kind") != "coat_sim.project_recovery_checkpoint"
                or value.get("version") != 1 or value.get("project_id") != session.binding.project_id
                or set(value) - {"kind", "version", "project_id", "film", "resources", "compute"}):
            raise ProjectError("invalid_recovery", "异常恢复检查点格式无效。")
        host, binding = self.adapter.host, session.binding
        if "film" in value:
            validate_film_recovery(ProjectFilmWorkspace.for_project(host, binding), value["film"])
        if "resources" in value:
            ProjectResourceWorkspace.for_project(host, binding).validate_recovery(value["resources"])
        if "compute" in value:
            ProjectComputeWorkspace.for_project(host, binding).validate_recovery(value["compute"])

    def _section_token(self, session: Any, section: str) -> dict:
        host, binding = self.adapter.host, session.binding
        if section == "film":
            return film_recovery_current_token(ProjectFilmWorkspace.for_project(host, binding))
        owner = (ProjectResourceWorkspace.for_project(host, binding) if section == "resources"
                 else ProjectComputeWorkspace.for_project(host, binding))
        return owner.snapshot()

    def restore(self, project_id: str) -> bool:
        if self.restore_pending(project_id):
            return False
        value, token, section, generation, expected_revision, record_id = self._review[project_id]
        session = self._session(project_id)
        lease = self.adapter._leases[project_id]
        if record_id:
            candidate = next((row for row in lease.reviewed_recovery_candidates()
                              if row["record_id"] == record_id), None)
            if candidate is not None:
                current_revision = self._current_revision(session)
                candidate = {**candidate, "stale": candidate["revision"] != current_revision}
        else:
            candidate = next((row for row in lease.recovery_candidates()
                              if row["generation"] == generation), None)
        if (candidate is None or candidate["generation"] != generation
                or candidate["revision"] != expected_revision or candidate["stale"]
                or candidate["checkpoint"].get(section) != value[section]):
            raise ProjectError("stale_recovery", "异常会话已变化，请重新检查恢复内容。")
        self._validate(session, value)
        host, binding = self.adapter.host, session.binding
        if self._section_token(session, section) != token:
            raise ProjectError("stale_recovery", "确认期间工程内容已变化，请重新检查恢复内容。")
        if section == "resources":
            ProjectResourceWorkspace.for_project(host, binding).restore_recovery(value[section])
        elif section == "compute":
            result = ProjectComputeWorkspace.for_project(host, binding).restore_recovery(value[section])
            if result.get("optical_pending", False):
                self._pending_compute_restores[project_id] = (
                    self._review[project_id], result["optical_restore_token"], binding.generation)
                return False
        else:
            film = ProjectFilmWorkspace.for_project(host, binding)
            restored = restore_film_recovery(film, value[section], approved=True,
                                 expected_current_design_revision=token["design_revision"],
                                 expected_current_session_revision=token["session_revision"],
                                 expected_current_document_id=token["managed_document_id"],
                                 expected_current_document_revision=token["managed_document_revision"])
            if not restored:
                raise ProjectError("recovery_rejected", "膜系内容恢复未能应用，请重新检查当前编辑状态。")
        self._last.pop((project_id, session.binding.generation), None)
        self._review.pop(project_id, None)
        return True

    def restore_pending(self, project_id: str) -> bool:
        pending = self._pending_compute_restores.get(project_id)
        if pending is None:
            return False
        try:
            generation = self._session(project_id).binding.generation
        except ProjectError:
            generation = None
        if pending[2] != generation:
            self._pending_compute_restores.pop(project_id, None)
            self._last.pop((project_id, pending[2]), None)
            if self._review.get(project_id) is pending[0]:
                self._review.pop(project_id, None)
            return False
        return True

    def archive_reviewed(self, project_id: str, generation: str, section: str,
                         expected_revision: int) -> dict:
        if section not in {"film", "resources", "compute"}:
            raise ProjectError("invalid_recovery", "请选择要归档的异常恢复类别。")
        result = self.adapter._leases[project_id].preserve_reviewed_recovery(
            generation, section, expected_revision=expected_revision)
        self.cancel_review(project_id)
        return result

    def cancel_review(self, project_id: str, *, generation: str | None = None) -> None:
        self._review.pop(project_id, None)
        if generation is not None:
            pending = self._pending_compute_restores.get(project_id)
            if pending is not None and pending[2] == generation:
                self._pending_compute_restores.pop(project_id, None)
            self._last.pop((project_id, generation), None)

    def stop(self) -> None:
        self._timer.stop()
        self._review.clear()
        self._pending_compute_restores.clear()
