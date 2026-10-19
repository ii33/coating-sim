"""Independent desktop assistant windows and background agent bridge."""

from __future__ import annotations

import html
import csv
import io
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Thread
from collections.abc import Callable
from pathlib import Path
from typing import cast
import re
import sqlite3
import os
import sys
import time
from uuid import uuid4
from urllib.parse import urlsplit, urlunsplit

from PySide6.QtCore import QObject, Property, QTimer, QUrl, Signal, Slot, Qt, QAbstractListModel, QModelIndex, QProcess, QLocale, QBuffer, QIODevice
from PySide6.QtGui import QGuiApplication, QDesktopServices

from coat_sim.ui.icons import window_icon
from coat_sim.ui.token_format import format_tokens
from coat_sim.ui.settings import (
    load_ui_settings, save_ui_settings, validate_assistant_url,
    model_discovery_bus, ASSISTANT_TASK_MODES, SettingsBridge,
)
from coat_sim.agent.modelclient import reasoning_capabilities, model_token_limits
from coat_sim.agent.storage import AssistantStore
from coat_sim.ui.assistant_rich import (safe_rich_text, referenced_files, file_view, authorized_file)
from coat_sim.ui.assistant_stream_render import RichRenderService
from coat_sim.ui.assistant_artifacts import (
    merge_artifact_files, referenced_artifacts,
)
from coat_sim.ui.assistant_run_coordinator import AssistantRunCoordinator


UI_DIR = Path(__file__).resolve().parent
QML_DIR = UI_DIR / "qml"
PORTRAIT = UI_DIR / "assets" / "assistant.png"
MAX_PROMPT_LENGTH = 8000
MAX_REPLY_BYTES = 2_000_000
MAX_QUEUED_MESSAGES = 16
MAX_STEERING_MESSAGES = 8

_safe_rich_text = safe_rich_text


def _source_name(source: str) -> str:
    return {"main": "看板娘", "analyst": "分析助手", "summary": "结果整理",
            "execute_design": "设计助手", "execute_windows": "窗口助手",
            "execute_simulation": "仿真助手", "execute_correction": "修正板助手"}.get(source, source or "看板娘")


def _copy_turn_text(turn: dict) -> str:
    """Export stored source text, including rows never instantiated by ListView."""
    chunks = [str(turn.get("name") or "看板娘")]
    steps = turn.get("steps") or []
    if turn.get("text"):
        chunks.append(str(turn["text"]))
    for thought in turn.get("thoughts") or []:
        chunks.append(_source_name(str(thought.get("source", "main")))
                      + " · 思考\n" + str(thought.get("text", "")))
    if steps:
        for step in steps:
            label = _source_name(str(step.get("source", "main")))
            if step.get("thinking"):
                chunks.append(label + " · 思考\n" + str(step["thinking"]))
            if step.get("output"):
                chunks.append(label + " · 输出\n" + str(step["output"]))
    for item in turn.get("interjections") or []:
        chunks.append(str(item.get("name") or "用户") + " · 插话 " + str(item.get("stateLabel", "")) + "\n" + str(item.get("text", "")))
        for file in item.get("files") or []:
            chunks.append("插话附件：" + str(file.get("reference") or file.get("path") or file.get("name", "")))
    for item in turn.get("allFiles", turn.get("files")) or []:
        reference = item.get("reference") or item.get("path") or item.get("name", "") if isinstance(item, dict) else item
        chunks.append("附件：" + str(reference))
    for key in ("notice", "footer"):
        if turn.get(key):
            chunks.append(str(turn[key]))
    return "\n\n".join(chunks)


def _visible_thoughts(values) -> list[dict]:
    """Only display reasoning text actually supplied by the service."""
    result = []
    for value in values or ():
        if not isinstance(value, dict):
            continue
        text = value.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        if value.get("unavailable") and text.strip() in (
                "此服务没有返回可显示的思考文字。", "等待服务返回思考文字…"):
            continue
        result.append(dict(value))
    return result


def _cache_summary(usage: dict) -> str:
    """Use only provider-reported requests as the cache hit-rate denominator."""
    cached = usage.get("cached_input_tokens")
    supported = usage.get("cache_reporting_supported")
    reported = int(usage.get("cache_reported_requests", bool(supported)) or 0)
    if cached is None or (not supported and not reported):
        return "缓存命中：服务未提供统计"
    denominator = int(usage.get("cache_reported_input_tokens", usage.get("input_tokens", 0)) or 0)
    if int(cached) > denominator:
        rate = "统计口径不一致"
    else:
        rate = f"{int(cached) / denominator * 100:.1f}%" if denominator > 0 else "无输入"
    return f"缓存读取 {format_tokens(cached)} Token · 命中率 {rate}"


class ConversationModel(QAbstractListModel):
    """Keep message delegates alive while a stream updates the current row."""

    TurnRole = int(Qt.UserRole) + 1

    def __init__(self, parent: QObject | None = None, *, role_name: bytes = b"turnData") -> None:
        super().__init__(parent)
        self._role_name = role_name
        self._rows: list[dict] = []
        self._key_rows: dict[str, int] = {}

    def roleNames(self) -> dict[int, bytes]:
        return {self.TurnRole: self._role_name}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if role == self.TurnRole and index.isValid() and 0 <= index.row() < len(self._rows):
            return self._rows[index.row()]
        return None

    def visible_index(self, key: str) -> int:
        """Return the current visible-model row for a stable message key."""
        return self._key_rows.get(str(key), -1)

    def _reindex(self) -> None:
        self._key_rows = {str(row.get("key", "")): index
                          for index, row in enumerate(self._rows)
                          if row.get("key")}

    def update_turns(self, rows: list[dict], start: int = 0) -> None:
        common = min(len(self._rows), len(rows))
        start = min(start, common)
        if any(self._rows[index]["key"] != rows[index]["key"] for index in range(start, common)):
            # A session switch changes row identities, but the visible ListView
            # delegates can be rebound in place. Replacing the shared prefix via
            # dataChanged avoids a full delegate teardown/model reset.
            changed = False
            for index in range(start, common):
                if self._rows[index] != rows[index]:
                    self._rows[index] = rows[index]
                    changed = True
            if changed:
                self.dataChanged.emit(self.index(start, 0), self.index(common - 1, 0), [self.TurnRole])
            old_count = len(self._rows)
            if len(rows) < old_count:
                self.beginRemoveRows(QModelIndex(), len(rows), old_count - 1)
                del self._rows[len(rows):]
                self.endRemoveRows()
            elif len(rows) > old_count:
                self.beginInsertRows(QModelIndex(), old_count, len(rows) - 1)
                self._rows.extend(rows[old_count:])
                self.endInsertRows()
            self._reindex()
            return
        old_count = len(self._rows)
        count_changed = len(rows) != old_count
        if len(rows) < old_count:
            self.beginRemoveRows(QModelIndex(), len(rows), old_count - 1)
            del self._rows[len(rows):]
            self.endRemoveRows()
        elif len(rows) > old_count:
            self.beginInsertRows(QModelIndex(), old_count, len(rows) - 1)
            self._rows.extend(rows[old_count:])
            self.endInsertRows()
        for index in range(start, common):
            if self._rows[index] != rows[index]:
                self._rows[index] = rows[index]
                item = self.index(index, 0)
                self.dataChanged.emit(item, item, [self.TurnRole])
        if count_changed:
            self._reindex()

    def prepend_turns(self, rows: list[dict]) -> None:
        if not rows:
            return
        self.beginInsertRows(QModelIndex(), 0, len(rows) - 1)
        self._rows[0:0] = rows
        self.endInsertRows()
        self._reindex()


def _plain_reusable_cached_turn(value: tuple) -> bool:
    """Keep only rows whose cached payload does not retain renderer objects."""
    row = value[2] if len(value) > 2 else None
    if (not isinstance(row, dict) or row.get("document") is not None
            or row.get("steps") or row.get("files") or row.get("interjections")):
        return False
    return not any(isinstance(thought, dict) and thought.get("document") is not None
                   for thought in (row.get("thoughts") or []))


class AssistantBridge(QObject):
    """QML and conversation state for runs owned by AssistantRunCoordinator."""

    configurationChanged = Signal()
    configurationSaved = Signal()
    transcriptChanged = Signal()
    conversationChanged = Signal()
    busyChanged = Signal()
    statusChanged = Signal()
    openRequested = Signal()
    sessionsRequested = Signal()
    statisticsRequested = Signal()
    closeRequested = Signal()
    sessionsChanged = Signal()
    usageChanged = Signal()
    workerEvent = Signal(object)
    streamingChanged = Signal()
    progressChanged = Signal()
    capabilitiesChanged = Signal()
    attachmentsChanged = Signal()
    contextChanged = Signal()
    previewRequested = Signal("QVariantMap")
    toggleRequested = Signal()
    settingsRequested = Signal()
    attachmentRequested = Signal()
    applicationRequested = Signal(str)
    queueChanged = Signal()
    draftRestored = Signal(str)
    decisionsChanged = Signal()
    decisionRequested = Signal(object)
    taskSettled = Signal(str, str)
    storageReady = Signal(object)
    compactionChanged = Signal()
    historyProjectionChanged = Signal()
    historyProjectionBatchAboutToInsert = Signal(int)
    historyProjectionBatchInserted = Signal()
    projectContextsChanged = Signal()
    projectContextsReady = Signal(object)

    @property
    def _worker(self):
        return self._run_coordinator.worker

    @property
    def _request_id(self):
        return self._run_coordinator.request_id

    @property
    def _stop_pending(self):
        return self._run_coordinator.stop_pending

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config = load_ui_settings()
        self._application_exit_frozen = False
        self._discovery_bus = model_discovery_bus()
        self._discovered_models = self._discovery_bus.models_for(
            str(self._config.get("assistant_api_url", "")))
        self._discovery_bus.modelsChanged.connect(self.receiveDiscoveredModels)
        self._discovery_bus.reasoningChanged.connect(self.receiveReasoningProbe)
        self._capabilities = reasoning_capabilities(
            str(self._config.get("assistant_api_url", "")),
            str(self._config.get("assistant_model", "")), self._reasoning_probe())
        self._store = AssistantStore()
        sessions = self._store.sessions()
        self._sessions_cache = sessions
        self._session = sessions[0]["id"] if sessions else self._store.new_session()
        for session in sessions or [{"id": self._session}]:
            self._store.set_ui_session_mode(session["id"], self.taskMode)
        self._display = [(m["role"], m["content"]) for m in self._store.messages(self._session)]
        self._history = [{"role": role, "content": text} for role, text in self._display
                         if role in ("user", "assistant")]
        self._background_job_id = ""
        self._final_message_request = ""
        self._streaming_text = ""
        self._streaming_source = ""
        self._task_started = 0.0
        self._task_events = self._store.task_events(self._session, limit=400)
        self._stream_timer = QTimer(self)
        self._stream_timer.setSingleShot(True)
        self._stream_timer.setInterval(120)
        self._stream_timer.timeout.connect(self.streamingChanged.emit)
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(1000)
        self._progress_timer.timeout.connect(self.progressChanged.emit)
        self._run_coordinator = AssistantRunCoordinator(self)
        self.workerEvent.connect(self._on_worker_event)
        self._run_coordinator.eventAccepted.connect(self._on_accepted_worker_event)
        self._run_coordinator.transportFailed.connect(self._worker_transport_failed)
        self._run_coordinator.cancelTimedOut.connect(self._cancel_timeout)
        self._closed = False
        self._project_context_rows: list[dict] = []
        self._project_context_target: dict | None = None
        self._project_context_generation = 0
        self._active_project_context_generation = 0
        self._project_context_loading = False
        self._project_context_status = "未选择工程，使用全局记忆。"
        self.projectContextsReady.connect(self._project_contexts_ready)
        self._project_context_timer = QTimer(self)
        self._project_context_timer.setInterval(2000)
        self._project_context_timer.timeout.connect(self._poll_project_context)
        self._project_context_timer.start()
        self._busy = False
        self._status = "准备就绪。"
        self._attachments = []
        self._pending_messages: list[dict] = []
        self._decisions: dict[str, dict] = {}
        self._answered_decisions: set[str] = set()
        self._task_notice_request = ""
        self._steering: dict[str, dict] = {}
        self._interjections: list[dict] = []
        self._turn_cache = {}
        self._body_sources: dict[str, tuple[str, bool]] = {}
        self._stable_turns = None
        self._view_stable_source = None
        self._stable_model_rows = []
        self._storage_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chat-storage")
        self._storage_generation = 0
        self._history_projection_generation = 0
        self._history_projection = None
        self._projection_requested = False
        self._history_loading = False
        self._history_error = ""
        self._history_session_cache: dict[str, dict] = {}
        self._history_anchor_key = ""
        self._storage_read_session = ""
        self._storage_overlay_session = ""
        self._storage_overlay_messages: list[dict] = []
        self._history_projection_timer = QTimer(self)
        self._history_projection_timer.setSingleShot(True)
        self._history_projection_timer.timeout.connect(self._advance_history_projection)
        self.storageReady.connect(self._storage_ready)
        self._usage_summary = self._store.usage_summary(self._config)
        self._session_usage = self._store.session_usage(self._session)
        self._round_usage = {}
        self._compaction_request = ""
        self._compaction_state = {}
        self._turn_view_cache = {}
        self._artifact_placement_cache = {}
        self._body_reference_cache = {}
        self._renderer = RichRenderService(self)
        self._step_models: dict[str, ConversationModel] = {}
        self._rich_text_color = ""
        self._thoughts = {}
        self._steps: list[dict] = []
        self._withdraw_pending: set[str] = set()
        self._round_files = []
        self._context = {}
        self._output_audio = False
        self._ensure_intro()
        self._reload_messages()
        self._stream_timer.timeout.connect(self.transcriptChanged.emit)
        self._turn_model = ConversationModel(self)
        self.transcriptChanged.connect(self._refresh_turn_model)
        self._refresh_turn_model()
        # Keep the shared settings probe owner alive even when Settings is closed.
        # ModelDiscoveryBus deduplicates it against visible Settings windows.
        self._prefs = SettingsBridge(self)

    @Property(list, notify=projectContextsChanged)
    def projectContexts(self) -> list[dict]:
        # QML receives presentation only; authority stays in the bridge's rows.
        return [{"text": "全局"}] + [{"text": row["text"]} for row in self._project_context_rows]

    @Property(int, notify=projectContextsChanged)
    def projectContextIndex(self) -> int:
        return next((index + 1 for index, row in enumerate(self._project_context_rows)
                     if row["target"] == self._project_context_target), 0)

    @Property(bool, notify=projectContextsChanged)
    def projectContextLoading(self) -> bool:
        return self._project_context_loading

    @Property(str, notify=projectContextsChanged)
    def projectContextStatus(self) -> str:
        return self._project_context_status

    def _project_message_context(self) -> dict:
        target = self._project_context_target
        return {"assistant_project_target": dict(target) if target is not None else None,
                "project_context_generation": self._project_context_generation}

    def _project_message_is_current(self, row: dict) -> bool:
        return (row.get("project_context_generation", 0) == self._project_context_generation
                and row.get("assistant_project_target") == self._project_context_target)

    def _set_project_context(self, target: dict | None, status: str) -> None:
        if target != self._project_context_target:
            self._project_context_target = dict(target) if target is not None else None
            self._project_context_generation += 1
            # Never rebind queued or returned steering messages, including A -> B -> A.
            for row in self._pending_messages:
                row["paused"] = True
            self.queueChanged.emit()
            if self._worker is not None and not self._configure_worker():
                # A failed revocation must not leave the old worker authoritative.
                self._stop_worker()
        self._project_context_status = status
        self.projectContextsChanged.emit()
        if not self.busy:
            QTimer.singleShot(0, self._drain_queue)

    @Slot(int, result=bool)
    def selectProjectContext(self, index: int) -> bool:
        if self._closed or getattr(self, "_application_exit_frozen", False):
            return False
        if not 0 <= index <= len(self._project_context_rows):
            return False
        target = self._project_context_rows[index - 1]["target"] if index else None
        label = self._project_context_rows[index - 1]["text"] if index else "全局"
        self._set_project_context(target, "当前记忆上下文：" + label + "。")
        return True

    def _poll_project_context(self) -> None:
        if self._project_context_target is not None:
            self.refreshProjectContexts()

    @Slot()
    def refreshProjectContexts(self) -> None:
        if self._closed or self._project_context_loading:
            return
        self._project_context_loading = True
        self.projectContextsChanged.emit()

        def fetch() -> None:
            from coat_sim.assistant_process import AssistantProcessClient
            from coat_sim.project_activation import activation_target
            try:
                rows = []
                for client in AssistantProcessClient(timeout=3.0).list_clients():
                    if client.get("state") != "alive":
                        continue
                    for project in client.get("project_sessions", []):
                        target = activation_target(client, project)
                        if any(not isinstance(value, str) or not value.strip()
                               for value in target.values()):
                            continue
                        rows.append({"text": str(project.get("name") or Path(target["root"]).name),
                                     "target": target})
                result = {"rows": rows, "error": ""}
            except Exception:
                result = {"rows": [], "error": "无法核验已打开工程；已切回全局记忆，请刷新后重新选择。"}
            if not self._closed:
                try:
                    self.projectContextsReady.emit(result)
                except RuntimeError:
                    pass  # The QObject may have been deleted while IPC completed.

        Thread(target=fetch, name="assistant-project-context", daemon=True).start()

    @Slot(object)
    def _project_contexts_ready(self, result: dict) -> None:
        if self._closed:
            return
        self._project_context_loading = False
        self._project_context_rows = result["rows"]
        error = result["error"]
        if error:
            self._set_project_context(None, error)
        elif self._project_context_target is not None and not any(
                row["target"] == self._project_context_target for row in self._project_context_rows):
            self._set_project_context(None, "原工程会话已关闭或更新；已切回全局记忆，请重新选择。")
        elif self._project_context_target is None:
            self._project_context_status = ("请选择工程以使用工程记忆；未选择时仅用全局记忆。"
                                            if self._project_context_rows else "暂无已打开工程，使用全局记忆。")
            self.projectContextsChanged.emit()
        else:
            self.projectContextsChanged.emit()

    def _configure_worker(self) -> bool:
        return self._write_worker({"op": "configure", "config": self._worker_config(),
                                   "assistant_project_target": self._project_message_context()["assistant_project_target"]})

    @Property(QObject, constant=True)
    def turnModel(self) -> QObject:
        return self._turn_model

    @Property(bool, notify=historyProjectionChanged)
    def historyProjectionPending(self) -> bool:
        return bool(self._history_projection and self._history_projection.get("pending"))

    @Property(bool, notify=historyProjectionChanged)
    def hasOlderHistory(self) -> bool:
        state = self._history_projection
        return bool(state and (state.get("cursor", 0) > 0 or not state.get("tail_ready")))

    @Property(bool, notify=historyProjectionChanged)
    def historyLoading(self) -> bool:
        return self._history_loading or bool(
            self._history_projection and self._history_projection.get("pending"))

    @Property(str, notify=historyProjectionChanged)
    def historyError(self) -> str:
        return self._history_error

    @Slot(str, result=int)
    def visibleTurnIndex(self, key: str) -> int:
        return self._turn_model.visible_index(key)

    @Slot(str)
    def rememberHistoryAnchor(self, key: str) -> None:
        """Remember the stable visible turn key for session-cache restoration."""
        key = str(key or "")
        if key and self._turn_model.visible_index(key) < 0:
            return
        self._history_anchor_key = key

    @Slot(result=bool)
    def loadOlderHistory(self) -> bool:
        state = self._history_projection
        if self._closed or self._history_loading:
            return False
        if state is None:
            if len(self._messages) <= 48 or not hasattr(self, "_turn_model"):
                return False
            return self._start_history_projection()
        if state.get("pending"):
            return False
        if state.get("tail_ready") and state.get("cursor", 0) <= 0:
            return False
        self._history_error = ""
        state["pending"] = True
        self.historyProjectionChanged.emit()
        self._history_projection_timer.start(0)
        return True

    @Slot(result=bool)
    def retryHistoryLoad(self) -> bool:
        if self._closed or self._history_loading or not self._history_error:
            return False
        if self._history_projection is not None:
            return self.loadOlderHistory()
        self._reload_messages()
        return True

    @Slot(str, str, result=QObject)
    def documentForTurn(self, key: str, style_key: str = "") -> QObject | None:
        """Build a message document only when its virtualized row enters the view."""
        source = self._body_sources.get(key)
        if source is None:
            return None
        text, streaming = source
        # Referencing style_key from QML makes appearance changes re-evaluate the
        # binding while preserving the document's stable identity and height cache.
        _ = style_key
        return self._renderer.document(key + "/body", text, streaming=streaming,
                                       color=self._rich_text_color)

    def _refresh_turn_model(self) -> None:
        if self._history_projection is not None:
            if self._history_projection.get("tail_ready"):
                self._refresh_projected_tail()
            return
        sources = self.turns
        incremental = self._view_stable_source is self._stable_turns
        prefix = len(self._stable_turns) if incremental else 0
        rows = list(self._stable_model_rows) if incremental else []
        projected, retained_documents, retained_steps = self._shape_turn_sources(sources[prefix:])
        rows.extend(projected)
        self._turn_model.update_turns(rows, start=prefix)
        if incremental:
            return
        self._view_stable_source = self._stable_turns
        self._stable_model_rows = rows[:len(self._stable_turns)]
        self._finish_turn_view(retained_documents, retained_steps)

    def _refresh_projected_tail(self) -> None:
        state = self._history_projection
        if state is None or not state["tail_ready"]:
            return
        appended = [message for message in self._messages
                    if str(message.get("id")) not in state["base_message_ids"]]
        current = [message for message in appended
                   if self.busy and message.get("role") == "assistant"
                   and message.get("request_id") == self._request_id and self._request_id]
        current_ids = {str(message.get("id")) for message in current}
        stable_appended = [message for message in appended
                           if str(message.get("id")) not in current_ids]
        appended_rows = self._build_turn_rows(stable_appended, live=False)
        live_rows = self._build_turn_rows(current, live=True) if self.busy else []

        # A newly published assistant row is newer than every message in the
        # snapshot. Let it own repeated file cards and prevent later old batches
        # from claiming those same artifacts.
        newest_artifacts = set()
        for row in appended_rows + live_rows:
            if row.get("role") != "assistant":
                continue
            newest_artifacts.update(str(identity) for identity in row.get("inlineFileIds", []))
            newest_artifacts.update(str(item.get("artifact_id")) for item in row.get("files", [])
                                    if item.get("artifact_id"))
            newest_artifacts.update(str(item.get("artifact_id")) for item in row.get("allFiles", [])
                                    if item.get("artifact_id"))
        if newest_artifacts:
            state["seen_artifacts"].update(newest_artifacts)
            changed_keys = set()
            changed_older_sources = []
            changed_older_indices = []
            older_keys = {str(row.get("key", "")) for row in state["older_source_rows"]}
            for source_index, row in enumerate(
                    state["older_source_rows"] + state["tail_source_rows"]):
                if row.get("role") != "assistant":
                    continue
                files = row.get("files", [])
                filtered = [item for item in files
                            if str(item.get("artifact_id") or "") not in newest_artifacts]
                if len(filtered) != len(files):
                    row["files"] = filtered
                    key = str(row.get("key", ""))
                    changed_keys.add(key)
                    if key in older_keys:
                        changed_older_sources.append(row)
                        changed_older_indices.append(source_index)
            for key in changed_keys:
                self._turn_view_cache.pop(key, None)
                self._artifact_placement_cache.pop(key, None)
            if changed_keys:
                for identity, entry in list(self._turn_cache.items()):
                    cached_row = entry[2] if len(entry) > 2 else None
                    if isinstance(cached_row, dict) and cached_row.get("key") in changed_keys:
                        self._turn_cache.pop(identity, None)
                if changed_older_sources:
                    reshaped, documents, step_keys = self._shape_turn_sources(changed_older_sources)
                    by_key = {row["key"]: row for row in reshaped}
                    state["older_model_rows"] = [by_key.get(row["key"], row)
                                                  for row in state["older_model_rows"]]
                    state["documents"].update(documents)
                    state["step_keys"].update(step_keys)

        sources = state["tail_source_rows"] + appended_rows + live_rows
        model_rows, documents, step_keys = self._shape_turn_sources(sources)
        stable_count = len(state["tail_source_rows"]) + len(appended_rows)
        state["tail_source_rows"].extend(appended_rows)
        state["base_message_ids"].update(
            str(message.get("id")) for message in stable_appended
        )
        state["snapshot_message_ids"].update(
            str(message.get("id")) for message in stable_appended
        )
        state["loaded_message_signatures"].update({
            self._message_turn_key(self._session, message): message
            for message in stable_appended
            if message.get("role") in ("user", "assistant")
        })
        state["tail_model_rows"] = model_rows[:stable_count]
        # Update existing visible tail rows in place and add/remove only the
        # live tail. This keeps an artifact de-duplication change in an older
        # visible row from being skipped while streamed rows still update only
        # the rows whose data actually changed.
        older_prefix = state["older_model_rows"]
        model_start = (min(changed_older_indices) if newest_artifacts and changed_older_indices
                       else len(older_prefix))
        self._turn_model.update_turns(older_prefix + model_rows,
                                      start=model_start)
        state["loaded_older_count"] = len(older_prefix)
        state["appended_rows"] = []
        state["current_messages"] = current
        state["live_rows"] = live_rows
        state["documents"].update(documents)
        state["step_keys"].update(step_keys)
        state["newest_reference_ids"] = {
            str(message.get("id")) for message in self._messages[-512:]
        }
        self._finish_turn_view(state["documents"], state["step_keys"])

    def _shape_turn_sources(self, sources: list[dict]) -> tuple[list[dict], set[str], set[str]]:
        rows = []
        retained_documents = set()
        retained_steps = set()
        for source in sources:
            cached = self._turn_view_cache.get(source["key"])
            if cached is not None and cached[0] is source:
                rows.append(cached[1])
                retained_documents.update(cached[2])
                retained_steps.add(source["key"])
                continue
            row = dict(source)
            row.pop("_fileViews", None)
            documents = set()
            model = self._step_models.get(row["key"])
            views = []
            for step in row.get("steps", []):
                view = {key: value for key, value in step.items() if key not in ("output", "html")}
                view["key"] = step["stepId"]
                view["hasOutput"] = bool(step.get("output"))
                views.append(view)
                documents.add(step["document"].identity)
                documents.add(step["thoughtDocument"].identity)
            if views or model is not None:
                if model is None:
                    model = ConversationModel(self, role_name=b"stepData")
                    self._step_models[row["key"]] = model
                model.update_turns(views)
            row["stepModel"] = model if views else None
            row["hasSteps"] = bool(views)
            row["steps"] = []  # Source data stays in turns/copy APIs, not QML array models.
            if views:
                row["document"] = None
                row["text"] = ""
                row["html"] = ""
                row["thoughts"] = []
            else:
                document = row.get("document")
                if document is not None:
                    documents.add(document.identity)
                elif not row.get("hasSteps"):
                    # Lazy body documents keep their cache while their row stays
                    # in the model, but are not created for unseen history.
                    documents.add(row["key"] + "/body")
                if row["role"] == "assistant":
                    row["text"] = ""  # Full source is exposed only by explicit copy APIs.
            for item in row.get("interjections", []):
                if item.get("document") is not None:
                    documents.add(item["document"].identity)
            for thought in row.get("thoughts", []):
                thought["document"] = self._renderer.plain_document(row["key"] + "/legacy-thought/" + str(thought.get("stepId", "")), str(thought.get("text", "")))
                documents.add(thought["document"].identity)
            self._turn_view_cache[row["key"]] = (source, row, documents)
            retained_documents.update(documents)
            rows.append(row)
            retained_steps.add(row["key"])
        return rows, retained_documents, retained_steps

    def _finish_turn_view(self, retained_documents: set[str], retained_steps: set[str]) -> None:
        # Session caches retain only their bounded loaded ranges. Keep their
        # renderer and step-model identities alive across a normal active-view
        # refresh, while clear/delete still release them after cache eviction.
        if hasattr(self, "_history_session_cache"):
            cached_documents, cached_steps = self._cached_history_retention()
            retained_documents = set(retained_documents) | cached_documents
            retained_steps = set(retained_steps) | cached_steps
        self._renderer.retain(retained_documents)
        for key in list(self._step_models):
            if key not in retained_steps:
                self._step_models.pop(key).deleteLater()
        if len(self._turn_view_cache) > len(retained_steps):
            self._turn_view_cache = {key: value for key, value in self._turn_view_cache.items()
                                     if key in retained_steps}
        if len(self._artifact_placement_cache) > len(retained_steps):
            self._artifact_placement_cache = {key: value for key, value in self._artifact_placement_cache.items()
                                              if key in retained_steps}

    @Property(bool, notify=configurationChanged)
    def enabled(self) -> bool:
        return bool(self._config.get("assistant_enabled"))

    @Property(int, notify=configurationChanged)
    def iconSize(self) -> int:
        return int(self._config.get("assistant_icon_size", 96))

    @Property("QVariantList", notify=sessionsChanged)
    def sessions(self) -> list[dict]:
        return self._sessions_cache

    @Property(str, notify=sessionsChanged)
    def currentSessionId(self) -> str:
        return self._session

    @Property("QStringList", notify=configurationChanged)
    def discoveredModels(self) -> list[str]:
        return [str(row["id"]) for row in self._discovered_models if row.get("id")]

    @Property("QStringList", notify=configurationChanged)
    def modelOptions(self) -> list[str]:
        return list(dict.fromkeys(self.discoveredModels + ([self.activeModel] if self.activeModel else [])))

    @Property(str, notify=configurationChanged)
    def activeModel(self) -> str:
        return str(self._config.get("assistant_model", ""))

    @Property(str, notify=configurationChanged)
    def taskMode(self) -> str:
        return str(self._config.get("assistant_task_mode", "chat"))

    @Property(str, notify=capabilitiesChanged)
    def reasoningStrength(self) -> str:
        selected = str(self._config.get("assistant_reasoning_strength", "auto"))
        return selected if selected in self.reasoningOptions else "auto"

    @Property("QStringList", notify=capabilitiesChanged)
    def reasoningOptions(self) -> list[str]:
        return list(self._capabilities.get("supported_strengths", ["auto"]))

    @Property(bool, notify=capabilitiesChanged)
    def reasoningSupported(self) -> bool:
        return bool(self._capabilities.get("reasoning_supported"))

    @Property(str, notify=capabilitiesChanged)
    def reasoningDescription(self) -> str:
        detail = str(self._capabilities.get("detail", ""))
        effective = self._capabilities.get("effective_reasoning")
        if effective:
            detail += f"  本轮实际设置：{effective}。"
        output = self._capabilities.get("output_budget")
        if output:
            detail += f"  本轮输出预算：{format_tokens(output)} Token。"
        return detail

    @Property(str, notify=streamingChanged)
    def streamingText(self) -> str:
        return self._streaming_text

    @Property(str, notify=streamingChanged)
    def streamingSource(self) -> str:
        return self._streaming_source

    @Property(bool, notify=progressChanged)
    def cancelling(self) -> bool:
        return self._stop_pending

    @Property("QVariantList", notify=progressChanged)
    def progressCards(self) -> list[dict]:
        cards: dict[str, dict] = {}
        for row in self._task_events:
            request = str(row.get("request_id", ""))
            payload = row.get("payload", {})
            if not request or not isinstance(payload, dict):
                continue
            created = float(row.get("created_at", 0))
            card = cards.setdefault(request, {
                "requestId": request, "mode": payload.get("mode", payload.get("task_mode", "")),
                "model": payload.get("model", ""), "state": "running", "stage": "",
                "text": "", "diagnostic": "", "jobId": "", "elapsedSeconds": 0.0,
                "startedAt": created, "updatedAt": created,
                "evaluations": -1, "maxEvaluations": -1,
                "bestScore": None, "estimatedRemainingSeconds": -1,
                "phases": [],
            })
            card["updatedAt"] = created
            kind = row.get("event")
            if kind == "progress":
                for source, target in (("mode", "mode"), ("model", "model"), ("stage", "stage"),
                                       ("text", "text"), ("state", "state"), ("job_id", "jobId")):
                    if source in payload:
                        card[target] = str(payload[source])
                if isinstance(payload.get("elapsed_s"), (int, float)):
                    card["elapsedSeconds"] = max(0.0, float(payload["elapsed_s"]))
                details = payload.get("job_progress", {})
                metrics = {**details, **payload} if isinstance(details, dict) else payload
                for source, target in (
                        ("evaluations", "evaluations"),
                        ("max_evaluations", "maxEvaluations"),
                        ("best_score", "bestScore"),
                        ("estimated_remaining_s", "estimatedRemainingSeconds")):
                    if isinstance(metrics.get(source), (int, float)):
                        card[target] = metrics[source]
                phase = {"stage": card["stage"], "text": card["text"]}
                if phase["text"]:
                    if card["phases"] and card["phases"][-1]["stage"] == phase["stage"]:
                        card["phases"][-1] = phase
                    else:
                        card["phases"].append(phase)
                        card["phases"] = card["phases"][-8:]
            elif kind in ("completed", "cancelled", "error", "exited"):
                card["state"] = "interrupted" if kind == "exited" else kind
                card["stage"] = card["state"]
                card["text"] = str(payload.get("text", card["text"]))
                if isinstance(payload.get("elapsed_s"), (int, float)):
                    card["elapsedSeconds"] = max(0.0, float(payload["elapsed_s"]))
                else:
                    card["elapsedSeconds"] = max(0.0, created - card["startedAt"])
            elif kind == "diagnostic":
                card["diagnostic"] = str(payload.get("text", ""))
            elif kind == "capabilities":
                card["effectiveReasoning"] = str(payload.get("effective_reasoning", "auto"))
            if request == self._request_id and self.busy and self._task_started:
                card["elapsedSeconds"] = max(
                    card["elapsedSeconds"], time.monotonic() - self._task_started)
        result = sorted(cards.values(), key=lambda card: card["startedAt"])
        for card in result:
            if card["state"] in ("queued", "running", "received", "cancelling") and (
                    card["requestId"] != self._request_id or not self.busy):
                card["state"] = "unknown"
        return result[-20:]

    @Slot(str, object)
    def receiveDiscoveredModels(self, api_url: str, models: object) -> None:
        if api_url != self._discovery_bus.api_key(str(self._config.get("assistant_api_url", ""))):
            return
        self._discovered_models = [dict(row) for row in models if isinstance(row, dict)]
        self.configurationChanged.emit()
        self.contextChanged.emit()

    def _reasoning_probe(self) -> dict | None:
        return self._discovery_bus.reasoning_for(
            str(self._config.get("assistant_api_url", "")), self.activeModel)

    @Slot(str, str, object)
    def receiveReasoningProbe(self, api_url: str, model: str, probe: object) -> None:
        current_url = str(self._config.get("assistant_api_url", ""))
        if (self._discovery_bus.api_key(api_url) == self._discovery_bus.api_key(current_url)
                and model == self.activeModel):
            self._refresh_capabilities()
            self.configurationChanged.emit()

    def _refresh_capabilities(self) -> None:
        self._capabilities = reasoning_capabilities(
            str(self._config.get("assistant_api_url", "")), self.activeModel, self._reasoning_probe())
        self.capabilitiesChanged.emit()

    def _save_preferences(self, updates: dict) -> bool:
        """Merge assistant controls into the latest shared settings document."""
        try:
            latest = load_ui_settings()
            latest.update(updates)
            capabilities = reasoning_capabilities(
                str(latest.get("assistant_api_url", "")),
                str(latest.get("assistant_model", "")),
                self._discovery_bus.reasoning_for(str(latest.get("assistant_api_url", "")), str(latest.get("assistant_model", ""))))
            if latest.get("assistant_reasoning_strength", "auto") not in capabilities.get(
                    "supported_strengths", ["auto"]):
                latest["assistant_reasoning_strength"] = "auto"
            for session in self._store.sessions():
                self._store.set_ui_session_mode(session["id"], str(latest.get("assistant_task_mode", "chat")))
            saved = save_ui_settings(latest)
        except (OSError, ValueError, sqlite3.Error):
            self._set_status("无法保存看板娘设置，请检查设置文件是否可写。")
            return False
        self._config = saved
        self._discovered_models = self._discovery_bus.models_for(
            str(self._config.get("assistant_api_url", "")))
        self._refresh_capabilities()
        self._prefs.reloadAssistantSettings()
        self.configurationChanged.emit()
        self.contextChanged.emit()
        self.usageChanged.emit()
        self.configurationSaved.emit()
        return True

    def _worker_config(self) -> dict:
        return {**self._config, "task_mode": self.taskMode,
                "reasoning_strength": self.reasoningStrength,
                "assistant_system_language": QLocale.system().bcp47Name(),
                "assistant_context_tokens": self._context_capacity(),
                "assistant_reasoning_probe": self._reasoning_probe(),
                "output_audio": bool(self._output_audio)}

    def _clear_stream(self) -> None:
        self._stream_timer.stop()
        self._streaming_text = ""
        self._streaming_source = ""
        self.streamingChanged.emit()

    def _load_progress(self) -> None:
        self._task_events = []
        self._run_coordinator.reset_run()
        self._compaction_request = ""
        self._task_started = 0.0
        self._thoughts = {}
        self._steps = []
        self._withdraw_pending.clear()
        self._round_files = []
        self._interjections = []
        self._context = {}
        self._attachments = []
        self.attachmentsChanged.emit()
        self.queueChanged.emit()
        self.contextChanged.emit()
        self.usageChanged.emit()
        self._clear_stream()
        self.progressChanged.emit()

    def _record_event(self, event: str, payload: dict, *, persist: bool = True) -> None:
        if not self._request_id:
            return
        value = {**payload, "request_id": self._request_id}
        if persist:
            try:
                self._store.record_event(self._session, self._request_id, event, value)
            except (sqlite3.Error, OSError, ValueError):
                self._set_status("任务进度暂时无法保存，当前任务仍可继续。")
        self._task_events.append({"request_id": self._request_id, "event": event,
                                  "payload": value, "created_at": time.time()})
        self._task_events = self._task_events[-600:]
        self.progressChanged.emit()

    def _finish_progress(self, state: str, text: str, *, persist: bool = True) -> None:
        if self._request_id:
            elapsed = max(0.0, time.monotonic() - self._task_started) if self._task_started else 0.0
            self._record_event("progress", {"stage": state, "state": state,
                                            "text": text, "elapsed_s": elapsed}, persist=persist)

    @Property(str, notify=usageChanged)
    def tokenSummary(self) -> str:
        summary = self._usage_summary
        session = self._session_usage
        remaining = "不限" if summary["remaining"] is None else format_tokens(summary["remaining"]) + " Token"
        current = next((row for row in summary["models"] if str(row.get("model", "")) == self.activeModel), {})
        lines = [f"当前模型：{self.activeModel or '未配置'}",
                 "按模型汇总，所有 API 服务合计",
                 f"累计输入：{format_tokens(current.get('input_tokens', 0))} Token",
                 f"累计输出：{format_tokens(current.get('output_tokens', 0))} Token"]
        if current.get("estimated_requests"):
            lines.append(f"其中 {current['estimated_requests']} 次按保守估算记账")
        lines.extend(["", "当前会话（所有已使用模型）",
                 f"输入：{format_tokens(session.get('input_tokens', 0))} Token",
                 f"输出：{format_tokens(session.get('output_tokens', 0))} Token",
                 _cache_summary(session)])
        if session.get("cache_creation_input_tokens") is not None:
            lines.append(f"缓存写入：{format_tokens(session['cache_creation_input_tokens'])} Token")
        reported = int(session.get("cache_reported_requests", 0) or 0)
        requests = int(session.get("requests", session.get("total_requests", 0)) or 0)
        if reported:
            lines.append(f"缓存统计对应输入：{format_tokens(session.get('cache_reported_input_tokens', 0))} Token")
        if reported and requests:
            lines.append(f"提供缓存统计的请求：{reported} / {requests}")
        lines.extend(["", "共享额度（所有模型）",
                      f"今日总消耗：{format_tokens(summary['today_input'] + summary['today_output'])} Token",
                      f"最近 {summary['period_minutes']} 分钟：{format_tokens(summary['period_tokens'])} Token",
                      f"当前剩余额度：{remaining}"])
        lines.append("")
        lines.append("有 usage 时按服务器返回值统计；未返回或请求中断时按预留预算估算。")
        lines.append("缓存命中率 = 已报告的缓存读取 Token / 对应请求的实际输入 Token；未提供统计的请求不计入命中率。")
        return "\n".join(lines)

    @Property("QVariantList", notify=usageChanged)
    def otherModelTokenUsage(self) -> list[dict]:
        models = self._usage_summary["models"]
        result = []
        for row in sorted(models, key=lambda value: str(value.get("model", "")).casefold()):
            model = str(row.get("model", ""))
            if model == self.activeModel:
                continue
            text = (f"{model or '未标注模型'}\n"
                    f"累计输入：{format_tokens(row.get('input_tokens', 0))} Token\n"
                    f"累计输出：{format_tokens(row.get('output_tokens', 0))} Token")
            if row.get("estimated_requests"):
                text += f"\n其中 {row['estimated_requests']} 次按保守估算记账"
            result.append({"model": model, "summary": text})
        return result

    @Slot("QVariant", result=str)
    def tokenText(self, value) -> str:
        return format_tokens(value)

    @Property(QUrl, constant=True)
    def portraitUrl(self) -> QUrl:
        return QUrl.fromLocalFile(str(PORTRAIT))

    @Property(str, notify=transcriptChanged)
    def transcriptHtml(self) -> str:
        if not self._display:
            return "<p>你好！配置好服务器与模型后，就可以开始对话。</p>"
        blocks = []
        for role, content in self._display:
            label = {"user": "你", "assistant": "看板娘", "event": "进度"}.get(role, role)
            blocks.append(
                f"<p><b>{label}</b><br>{_safe_rich_text(content)}</p><hr>"
            )
        return "<html><body>" + "".join(blocks) + "</body></html>"

    @Property(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._busy

    @Property(str, notify=statusChanged)
    def statusMessage(self) -> str:
        return self._status

    def _set_status(self, message: str) -> None:
        if message != self._status:
            self._status = message
            self.statusChanged.emit()
            if self._busy:
                self.transcriptChanged.emit()

    def _set_busy(self, value: bool) -> None:
        if value != self._busy:
            self._stable_turns = None
            self._busy = value
            if value:
                self._progress_timer.start()
            else:
                self._progress_timer.stop()
            self.busyChanged.emit()
            self.progressChanged.emit()
            self.transcriptChanged.emit()

    @Slot()
    def reload_configuration(self) -> None:
        settings = load_ui_settings()
        try:
            for session in self._store.sessions():
                self._store.set_ui_session_mode(session["id"], str(settings.get("assistant_task_mode", "chat")))
        except (OSError, ValueError, sqlite3.Error):
            self._set_status("无法记录可信会话模式；设置尚未切换，请重试。")
            return
        self._config = settings
        self._turn_cache.clear()
        self._stable_turns = None
        self._reload_messages()
        self._prefs.reloadAssistantSettings()
        # A key change also invalidates cached metadata, even at the same URL/model.
        self._discovered_models = self._discovery_bus.models_for(
            str(self._config.get("assistant_api_url", "")))
        self._refresh_capabilities()
        self.configurationChanged.emit()
        self.transcriptChanged.emit()
        self.contextChanged.emit()
        self.usageChanged.emit()
        if self.enabled:
            self._configure_worker()
        else:
            self._stop_worker()

    @Slot()
    def openChat(self) -> None:
        self.openRequested.emit()

    @Slot()
    def showConversations(self) -> None:
        self.openRequested.emit()
        self.sessionsRequested.emit()

    @Slot()
    def showTokenStatistics(self) -> None:
        self._reload_messages()
        self.usageChanged.emit()
        self.openRequested.emit()
        self.statisticsRequested.emit()

    @Slot()
    def closeAssistant(self) -> None:
        self._stop_worker()
        if not self._save_preferences({"assistant_enabled": False}):
            self._config["assistant_enabled"] = False
            self.configurationChanged.emit()
        self.closeRequested.emit()

    @Slot(str, result=bool)
    def setModel(self, model: str) -> bool:
        if self.busy:
            self._set_status("请先停止当前任务，再切换模型。")
            return False
        selected = model.strip()
        if not selected or len(selected) > 120 or "\n" in selected or "\r" in selected:
            self._set_status("请填写有效模型名（最多 120 个字符）。")
            return False
        return self._save_preferences({"assistant_model": selected})

    @Slot(str)
    def setTaskMode(self, mode: str) -> None:
        if self.busy or mode not in ASSISTANT_TASK_MODES:
            return
        self._save_preferences({"assistant_task_mode": mode})

    @Slot(str)
    def setReasoningStrength(self, strength: str) -> None:
        if self.busy or strength not in self.reasoningOptions:
            return
        self._save_preferences({"assistant_reasoning_strength": strength})

    @Slot()
    def newConversation(self) -> None:
        if self.busy:
            self._set_status("请先停止当前任务，再新建会话。")
            return
        self._save_history_session_cache(self._session)
        self._session = self._store.new_session()
        self._store.set_ui_session_mode(self._session, self.taskMode)
        self._projection_requested = True
        self._reset_message_view()
        self._ensure_intro()
        self._reload_messages()
        self._load_progress()
        self.sessionsChanged.emit()
        self.transcriptChanged.emit()
        self.conversationChanged.emit()

    @Slot(str)
    def selectConversation(self, session: str) -> None:
        if self.busy:
            self._set_status("请先停止当前任务，再切换会话。")
            return
        if not any(row["id"] == session for row in self.sessions):
            return
        if session == self._session:
            return
        previous_session = self._session
        self._save_history_session_cache(previous_session)
        self._session = session
        self._store.set_ui_session_mode(self._session, self.taskMode)
        self._projection_requested = True
        self._reset_message_view()
        self._restore_history_session_cache(session)
        self._reload_messages()
        self._load_progress()
        self.sessionsChanged.emit()
        self.transcriptChanged.emit()
        self.conversationChanged.emit()
        QTimer.singleShot(0, self._drain_queue)

    @Slot("QVariantMap")
    def backgroundJobCompleted(self, event: dict) -> None:
        """Accept one trusted queue completion, routed to its original session."""
        if self._closed or not isinstance(event, dict):
            return
        origin = event.get("origin")
        if not isinstance(origin, dict):
            return
        session = str(origin.get("session_id") or "")
        request = str(origin.get("request_id") or "")
        job_id = str(event.get("job_id") or "")
        state = str(event.get("state") or "")
        if (not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", job_id)
                or state not in ("succeeded", "failed", "cancelled")
                or not session or not request):
            return
        payload = {"job_id": job_id, "state": state,
                   "summary": str(event.get("summary") or "后台计算")[:300],
                   "origin": {"session_id": session, "request_id": request},
                   "estimate_id": str(event.get("estimate_id") or "")}
        try:
            payload["workflow_run_id"] = str(event.get("workflow_run_id") or self._store.background_workflow(
                session, request, job_id=job_id, estimate_id=payload["estimate_id"]))
            accepted = self._store.receive_background_notification(session, request, job_id, payload)
        except (sqlite3.Error, OSError, ValueError):
            self._set_status("后台计算已结束，完成通知暂未保存；可在计算队列查看结果。")
            return
        if accepted:
            self.sessionsChanged.emit()
            if session == self._session and self.enabled:
                QTimer.singleShot(0, self._drain_queue)

    @Property("QVariantList", notify=decisionsChanged)
    def pendingDecisions(self) -> list[dict]:
        return [dict(row) for row in self._decisions.values()]

    def _clear_decisions(self, request_id: str = "") -> None:
        changed = False
        for identity, row in list(self._decisions.items()):
            if not request_id or row.get("request_id") == request_id:
                self._decisions.pop(identity, None)
                changed = True
        if changed:
            self.decisionsChanged.emit()

    def _emit_task_settled(self, state: str, text: str, *, request_id: str | None = None) -> None:
        request = self._request_id if request_id is None else request_id
        if request and self._task_notice_request != request:
            self._task_notice_request = request
            self.taskSettled.emit(state, text)

    def _answer_decision(self, identity: str, index: int, supplement: str, *, automatic: bool = False) -> bool:
        row = self._decisions.get(identity)
        if (row is None or row.get("submitting") or not self.busy
                or row.get("request_id") != self._request_id or len(supplement) > 4000
                or index < -1 or index >= len(row.get("options", []))
                or (index == -1 and not supplement.strip())
                or (automatic and (row.get("optional") is not True or index != 0 or supplement))):
            return False
        if not self._write_worker({"op": "decision_response", "request_id": self._request_id,
                                   "decision_id": identity, "selection_index": index,
                                   "free_text": supplement, "automatic": automatic}):
            return False
        row["submitting"] = True
        self.decisionsChanged.emit()
        return True

    @Slot(str, int, str, result=bool)
    def answerDecision(self, identity: str, index: int, supplement: str) -> bool:
        """Only the desktop's actual selected answer is forwarded to the worker."""
        return self._answer_decision(identity, index, supplement)

    @Slot(str, result=bool)
    def applyDecisionDefault(self, identity: str) -> bool:
        """Optional countdown defaults are distinguishable from human answers."""
        return self._answer_decision(identity, 0, "", automatic=True)

    @Slot()
    def cancelTask(self) -> None:
        if self.busy and self._run_coordinator.begin_cancel():
            self._pause_queue()
            message = ("正在停止讨论；已有计算保持运行…" if self.taskMode == "chat"
                       else "正在停止任务并核对计算取消结果…")
            self._set_status(message)
            self._record_event("progress", {"stage": "cancelling", "state": "cancelling",
                                            "text": message})
            self._write_worker({"op": "cancel", "request_id": self._request_id})

    def _cancel_timeout(self) -> None:
        if self.busy:
            self._stop_worker()
            self._set_status("任务已停止，待发送消息已暂停；再次对话不会自动继续旧任务。")

    def shutdown(self) -> None:
        self._closed = True
        self._storage_generation += 1
        self._project_context_timer.stop()
        self._cancel_history_projection()
        self._stop_worker()
        self._run_coordinator.close()
        self._renderer.close()
        self._storage_pool.shutdown(wait=False, cancel_futures=True)

    def _write_worker(self, value: dict) -> bool:
        return self._run_coordinator.write_worker(value)

    @Slot()
    def _worker_transport_failed(self) -> None:
        if self._closed:
            return
        request_id = self._request_id
        self._return_unapplied_steering("error")
        self._pause_queue()
        if self.busy:
            self._complete_round("error", "助手后台连接已断开。")
        self._clear_stream()
        self._run_coordinator.clear_cancel()
        self._finish_progress("error", "助手后台连接已断开。")
        self._set_status("助手后台连接已断开。")
        self._clear_decisions(request_id)
        if not self._run_coordinator.finish_run(request_id):
            return
        self._set_busy(False)
        self._emit_task_settled("error", "助手后台连接已断开。", request_id=request_id)

    def _ensure_worker(self) -> None:
        previous = self._worker
        self._run_coordinator.ensure_worker(self.workerEvent)
        if self._worker is not previous and not self._configure_worker():
            raise OSError("助手后台未接收工程上下文，请重试。")

    @Slot(object)
    def _on_worker_event(self, envelope: dict) -> None:
        """Keep the worker envelope as the bridge's stable Qt slot contract."""
        if self._closed:
            return
        auxiliary = (self._compaction_request,) if self._compaction_request else ()
        self._run_coordinator.accept_worker_event(envelope, auxiliary)

    @Slot(object)
    def _on_accepted_worker_event(self, value: dict) -> None:
        if self._closed:
            return
        event = value.get("event")
        if event == "context_compaction":
            self._compaction_state = dict(value)
            self.compactionChanged.emit()
            if value.get("state") in ("completed", "skipped", "failed"):
                self._compaction_request = ""
            return
        if event == "context" and value.get("request_id") == self._compaction_request:
            if value.get("source", "main") == "main":
                self._context = dict(value)
                self.contextChanged.emit()
            return
        if event == "background_registered":
            return
        text = str(value.get("text", ""))
        if event == "decision_requested":
            identity = str(value.get("decision_id") or "")
            if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", identity) and identity not in self._answered_decisions:
                row = {**value, "submitting": False}
                self._decisions[identity] = row
                self.decisionsChanged.emit()
                self.decisionRequested.emit(dict(row))
                self._set_status("有一个问题等待你的选择，当前任务已保留。")
        elif event in ("decision_answered", "decision_closed", "decision_rejected"):
            identity = str(value.get("decision_id") or "")
            row = self._decisions.get(identity)
            if event == "decision_rejected":
                if row is not None:
                    row["submitting"] = False
                    self.decisionsChanged.emit()
                self._set_status(text or "问题答复未被接收，请重试。")
            else:
                self._decisions.pop(identity, None)
                self.decisionsChanged.emit()
                if event == "decision_answered" and identity not in self._answered_decisions:
                    self._answered_decisions.add(identity)
                    selected = str(value.get("selected_label") or "补充输入")
                    supplement = str(value.get("free_text") or "")
                    automatic = value.get("source") == "default_countdown"
                    content = ("可选事项倒计时结束，采用默认：" if automatic else "已选择：") + selected
                    if supplement:
                        content += "\n" + supplement
                    self._append_message("event" if automatic else "user", content,
                        request_id=self._request_id, metadata={"decision_id": identity,
                            "question": str(value.get("question") or ""), "source": value.get("source")})
                    self._reload_messages()
                    self.transcriptChanged.emit()
                    self._set_status("已收到选择，任务继续。")
        elif event in ("model_step_started", "model_step_finished"):
            step = self._model_step(value)
            if step is not None:
                step["done"] = event == "model_step_finished"
                step["status"] = str(value.get("status", "running"))
                if value.get("model"):
                    step["model"] = str(value["model"])
                self.transcriptChanged.emit()
        elif event == "token":
            if str(value.get("source", "main")) == "summary":
                return
            step = self._model_step(value)
            if step is not None:
                step["output"] = (step["output"] + text)[:MAX_REPLY_BYTES]
            if str(value.get("source", "main")) == "main" and value.get("reset"):
                # A new model step starts another text segment in this round.
                # Keep earlier visible output; only a new round clears it.
                if self._streaming_text and not self._streaming_text.endswith("\n\n"):
                    self._streaming_text += "\n\n"
            self._streaming_source = str(value.get("source", self._streaming_source))
            if str(value.get("source", "main")) == "main" and len(self._streaming_text) < MAX_REPLY_BYTES:
                self._streaming_text = (self._streaming_text + text)[:MAX_REPLY_BYTES]
            if not self._stream_timer.isActive():
                self._stream_timer.start()
        elif event == "thinking":
            if str(value.get("source", "main")) == "summary":
                return
            step = str(value.get("step_id", value.get("source", "main")))
            thought = self._thoughts.setdefault(step, {"stepId": step, "source": str(value.get("source", "main")), "text": "", "done": False, "unavailable": False})
            if value.get("append"):
                thought["text"] = (thought["text"] + text)[:MAX_REPLY_BYTES]
            elif text or value.get("reset"):
                thought["text"] = text[:MAX_REPLY_BYTES]
            thought["done"] = bool(value.get("done", thought["done"]))
            thought["unavailable"] = bool(value.get("unavailable", False))
            model_step = self._model_step(value)
            if model_step is not None:
                model_step["thinking"] = thought["text"] if _visible_thoughts([thought]) else ""
                model_step["thinkingDone"] = thought["done"]
            if not self._stream_timer.isActive(): self._stream_timer.start()
        elif event == "artifact":
            info = value.get("file")
            if isinstance(info, dict):
                self._round_files = merge_artifact_files(self._round_files, [info])
                self._stable_turns = None
                self.transcriptChanged.emit()
        elif event == "context":
            if value.get("source", "main") != "main":
                return
            self._context = dict(value)
            self.contextChanged.emit()
        elif event in ("steering_withdrawn", "steering_withdraw_rejected"):
            message_id = str(value.get("message_id", ""))
            self._withdraw_pending.discard(message_id)
            if event == "steering_withdrawn":
                row = self._steering.pop(message_id, None)
                if row is not None:
                    self._set_interjection_state(message_id, "withdrawn")
                    if not self._restore_draft_row(row):
                        row["paused"] = True
                        self._pending_messages.append(row)
                        self.queueChanged.emit()
            else:
                self._set_interjection_state(message_id, "received" if message_id in self._steering else "applied")
                self._set_status(text or "插话已经进入模型请求，无法撤回；可再次插话更正。")
            self.transcriptChanged.emit()
        elif event in ("steering_received", "steering_applied", "steering_rejected"):
            message_id = str(value.get("message_id", ""))
            row = self._steering.get(message_id)
            if row is None:
                return
            state = {"steering_received": "received", "steering_applied": "applied",
                     "steering_rejected": "paused"}[event]
            self._set_interjection_state(message_id, state)
            if event == "steering_applied":
                self._withdraw_pending.discard(message_id)
                self._steering.pop(message_id, None)
            elif event == "steering_rejected":
                row = self._steering.pop(message_id)
                row["paused"] = True
                self._pending_messages.append(row)
                self.queueChanged.emit()
                self._set_status(text or "插话未能加入本轮，已保留为暂停的待发送消息。")
            self.transcriptChanged.emit()
        elif event == "message" and (text or value.get("files")):
            if self._final_message_request == self._request_id:
                return
            self._final_message_request = self._request_id
            step = self._model_step(value)
            if step is not None and text:
                if not step["output"] or text.startswith(step["output"]):
                    step["output"] = text[:MAX_REPLY_BYTES]
                elif not step["output"].endswith(text):
                    step["output"] = (step["output"] + "\n\n" + text)[:MAX_REPLY_BYTES]
            text = self._reply_content(text)
            self._round_files = merge_artifact_files(self._round_files, value.get("files") or [])
            self._append_message("assistant", text, request_id=self._request_id, metadata=self._round_metadata("running"))
            self._reload_messages()
            self.transcriptChanged.emit()
            self.sessionsChanged.emit()
        elif event == "progress":
            self._record_event("progress", value, persist=False)
            if text:
                self._set_status(text)
        elif event == "diagnostic":
            self._record_event("diagnostic", value, persist=False)
            if text:
                self._set_status(text)
        elif event == "capabilities":
            self._capabilities = {**self._capabilities, **value}
            if not self._capabilities.get("reasoning_supported"):
                self._capabilities["supported_strengths"] = ["auto"]
            self.capabilitiesChanged.emit()
            self._record_event("capabilities", value, persist=False)
        elif event == "status":
            self._set_status(text)
        elif event == "usage":
            if isinstance(value.get("round_usage"), dict):
                self._round_usage = dict(value["round_usage"])
            self.usageChanged.emit()
        elif event in ("completed", "cancelled", "error", "exited"):
            request_id = self._request_id
            active = self.busy and self._run_coordinator.run_active
            self._return_unapplied_steering(event)
            terminal_state = ("limited" if value.get("task_limited") else
                              "incomplete" if value.get("incomplete") or value.get("output_incomplete") else
                              "interrupted" if event == "exited" else event)
            if terminal_state != "completed":
                self._pause_queue()
            terminal_text = text or {"completed": "任务完成。", "cancelled": "任务已停止。",
                                     "error": "任务执行失败。", "exited": "助手后台已退出。"}[event]
            if event != "exited" or active:
                self._finish_progress(terminal_state,
                                      terminal_text, persist=event == "exited")
            if event == "exited":
                self._store.settle_owner(self._worker.pid)
            if active:
                self._complete_round(terminal_state, terminal_text)
                if self._background_job_id:
                    self._store.settle_background_notification(self._background_job_id,
                        "responded" if terminal_state == "completed" else "failed")
                    self._background_job_id = ""
            if event != "completed":
                self._clear_stream()
            self._run_coordinator.clear_cancel()
            self._clear_decisions(request_id)
            self.usageChanged.emit()
            self._set_status(terminal_text)
            if event == "exited":
                self._store.settle_owner(self._worker.pid)
                self.usageChanged.emit()
            if terminal_state == "completed" and active:
                QTimer.singleShot(0, self._drain_queue)
            if active:
                # Settle before busyChanged: a direct Qt slot may synchronously
                # start another run when the bridge becomes idle.
                if not self._run_coordinator.finish_run(request_id):
                    return
            self._set_busy(False)
            if active:
                self._emit_task_settled(terminal_state, terminal_text, request_id=request_id)

    def _stop_worker(self) -> None:
        was_busy = self.busy
        self._clear_decisions()
        self._return_unapplied_steering("cancelled")
        self._pause_queue()
        if self.busy:
            self._finish_progress("interrupted", "任务已停止，未完成的操作需要核对。")
        if was_busy:
            self._emit_task_settled("interrupted", "任务已停止，未完成的操作需要核对。")
        process = self._run_coordinator.stop_worker(finish=False)
        if process is not None:
            self._store.settle_owner(process.pid)
        if self.busy:
            self._complete_round("interrupted", "任务已停止，未完成的操作需要核对。")
        self._clear_stream()
        self._task_events = self._store.task_events(self._session, limit=400)
        self._run_coordinator.reset_run()
        self._compaction_request = ""
        self._set_busy(False)
        self.usageChanged.emit()

    @Slot()
    def clearConversation(self) -> None:
        if self._busy:
            return
        self._history_session_cache.pop(self._session, None)
        self._pending_messages = [row for row in self._pending_messages if row["session_id"] != self._session]
        self._session = self._store.clear_session(self._session)
        self._store.set_ui_session_mode(self._session, self.taskMode)
        self._reset_message_view()
        self._ensure_intro()
        self._reload_messages()
        self._load_progress()
        self.sessionsChanged.emit()
        self.transcriptChanged.emit()
        self.conversationChanged.emit()
        self._set_status("对话已清空，Token 统计和工程结果保留。")

    @Property("QVariantList", notify=queueChanged)
    def pendingMessages(self) -> list[dict]:
        return [{"id": row["id"], "prompt": row["prompt"],
                 "files": [file_view(item) for item in row["attachments"]],
                 "paused": bool(row.get("paused"))}
                for row in self._pending_messages if row["session_id"] == self._session]

    @Property(int, notify=queueChanged)
    def queuedCount(self) -> int:
        return sum(row["session_id"] == self._session for row in self._pending_messages)

    @Slot(str)
    def removeQueuedMessage(self, message_id: str) -> None:
        self._pending_messages = [row for row in self._pending_messages
                                  if not (row["id"] == message_id and row["session_id"] == self._session)]
        self.queueChanged.emit()

    def _restore_draft_row(self, row: dict) -> bool:
        merged = {str(item.get("path")): dict(item) for item in self._attachments}
        for item in row.get("attachments", []):
            merged.setdefault(str(item.get("path")), dict(item))
        if len(merged) > 8 or sum(int(item.get("size", 0)) for item in merged.values()) > 32 * 1024 * 1024:
            self._set_status("请先移除输入框中的部分附件，再撤回；原消息及附件会保留。")
            return False
        self._attachments = list(merged.values())
        self.attachmentsChanged.emit()
        self.draftRestored.emit(str(row["prompt"]))
        self._set_status("消息已撤回到输入框，原有内容和附件均已保留。")
        return True

    @Slot(str, result=bool)
    def withdrawQueuedMessage(self, message_id: str) -> bool:
        row = next((item for item in self._pending_messages
                    if item["id"] == message_id and item["session_id"] == self._session), None)
        if row is None or not self._restore_draft_row(row):
            return False
        self._pending_messages.remove(row)
        self.queueChanged.emit()
        return True

    @Slot(str, result=bool)
    def withdrawSteering(self, message_id: str) -> bool:
        if message_id not in self._steering or message_id in self._withdraw_pending or not self.busy:
            self._set_status("插话已应用或不在当前待处理列表中，无法撤回。")
            return False
        if not self._write_worker({"op": "withdraw_steer", "request_id": self._request_id,
                                   "message_id": message_id}):
            return False
        self._withdraw_pending.add(message_id)
        self._set_interjection_state(message_id, "withdrawing")
        self.transcriptChanged.emit()
        return True

    @Slot(result=bool)
    def resumeQueuedMessages(self) -> bool:
        if self.busy or self._closed:
            return False
        for row in self._pending_messages:
            if row["session_id"] == self._session:
                row["paused"] = False
        self.queueChanged.emit()
        QTimer.singleShot(0, self._drain_queue)
        return True

    def _pause_queue(self) -> None:
        for row in self._pending_messages:
            if row["session_id"] == self._session:
                row["paused"] = True
        self.queueChanged.emit()

    def set_application_exit_frozen(self, frozen: bool) -> None:
        self._application_exit_frozen = bool(frozen)
        if not frozen and not self._closed:
            QTimer.singleShot(0, self._drain_queue)

    def _drain_queue(self) -> None:
        if getattr(self, "_application_exit_frozen", False) or self.busy or self._closed:
            return
        row = next((item for item in self._pending_messages
                    if item["session_id"] == self._session and not item.get("paused")), None)
        if row is not None:
            if self._start_message(row):
                self._pending_messages.remove(row)
            else:
                self._pause_queue()
            self.queueChanged.emit()
            return
        if not self.enabled:
            return
        from coat_sim.agent.worker import ProjectMemoryBinding
        # This comparison does not select a project. A matching notification
        # may only use the target the user has already explicitly selected.
        memory_scope = (ProjectMemoryBinding(self._project_context_target, lambda: True).memory_project_id
                        or "global")
        for notification in self._store.pending_background_notifications(self._session):
            origin_scope = self._store.request_memory_scope(self._session, notification["request_id"])
            if origin_scope != memory_scope:
                continue  # Keep pending until the user selects the original scope.
            payload = notification["payload"]
            job_id = notification["job_id"]
            if self._store.request_was_stopped(self._session, notification["request_id"]):
                self._store.settle_background_notification(job_id, "suppressed")
                continue
            if not payload.get("workflow_run_id"):
                payload["workflow_run_id"] = self._store.background_workflow(
                    self._session, notification["request_id"], job_id=job_id,
                    estimate_id=str(payload.get("estimate_id") or ""))
            row = {"id": uuid4().hex, "session_id": self._session,
                   "prompt": "请简洁汇报这条后台计算的真实结果，并引用已有图像与资产。",
                   "attachments": [], "paused": False, "output_audio": False,
                   "background_notification": payload,
                   **self._project_message_context(),
                   "config_overrides": {"task_mode": "chat", "reasoning_strength": "auto"}}
            if self._start_message(row):
                self._store.settle_background_notification(job_id, "started")
            else:
                # Avoid a notification-triggered model retry loop. The queue
                # still exposes its result if the model/provider is unavailable.
                self._store.settle_background_notification(job_id, "failed")
            return

    def _prepare_message(self, prompt: str) -> tuple[dict | None, bool]:
        if getattr(self, "_application_exit_frozen", False):
            self._set_status("应用正在等待退出确认，消息尚未发送。")
            return None, False
        text, handled = self._command(prompt.strip())
        if handled:
            return None, True
        if (not text and not self._attachments) or self._closed:
            return None, False
        references = re.findall(r"@\{([^}]+)\}", text)
        if references:
            try:
                references = [str(authorized_file(value)) for value in references]
            except (OSError, ValueError):
                self._set_status("@ 引用仅允许 data 内文件；外部文件请通过附件按钮选择。")
                return None, False
            if not self.addAttachments(references):
                return None, False
        if not text:
            text = "请分析这些附件。"
        if len(text) > MAX_PROMPT_LENGTH:
            self._set_status(f"消息最多 {MAX_PROMPT_LENGTH} 个字符。")
            return None, False
        if not self.enabled:
            self._set_status("请先在设置中打开看板娘开关。")
            return None, False
        return {"id": uuid4().hex, "session_id": self._session, "prompt": text,
                "attachments": [dict(row) for row in self._attachments],
                "output_audio": bool(self._output_audio), "paused": False,
                **self._project_message_context()}, False

    def _consume_draft(self) -> None:
        self._attachments = []
        self._output_audio = False
        self.attachmentsChanged.emit()

    @Slot(str, result=bool)
    def sendMessage(self, prompt: str) -> bool:
        row, handled = self._prepare_message(prompt)
        if row is None:
            return handled
        if self.busy:
            if len(self._pending_messages) + len(self._steering) >= MAX_QUEUED_MESSAGES:
                self._set_status(f"待发送和插话消息合计最多 {MAX_QUEUED_MESSAGES} 条；请删除或等待处理。")
                return False
            row["paused"] = self._stop_pending
            self._pending_messages.append(row)
            self._consume_draft()
            self.queueChanged.emit()
            self._set_status("消息已排队；当前任务结束后发送。" if not row["paused"] else "消息已保留为暂停的待发送项。")
            return True
        accepted = self._start_message(row)
        if accepted:
            self._consume_draft()
        return accepted

    @Slot(str, result=bool)
    def sendDecisionSupplement(self, prompt: str) -> bool:
        """Send popup text without taking attachments or clearing the chat draft."""
        text = prompt.strip()
        if getattr(self, "_application_exit_frozen", False):
            self._set_status("应用正在等待退出确认，补充内容尚未发送。")
            return False
        if self._closed or not self.enabled:
            self._set_status("看板娘当前未启用，补充内容尚未发送。")
            return False
        if not text or len(text) > MAX_PROMPT_LENGTH:
            self._set_status(f"补充内容须为 1–{MAX_PROMPT_LENGTH} 个字符。")
            return False
        row = {"id": uuid4().hex, "session_id": self._session, "prompt": text,
               "attachments": [], "output_audio": False, "paused": False,
               **self._project_message_context()}
        if not self.busy:
            return self._start_message(row)
        if self._active_project_context_generation != self._project_context_generation:
            self._set_status("工程上下文已改变；请等待当前任务结束后重新发送补充内容。")
            return False
        if self._stop_pending:
            self._set_status("当前任务正在停止，补充内容尚未发送；请稍后重试。")
            return False
        if (len(self._steering) >= MAX_STEERING_MESSAGES
                or len(self._pending_messages) + len(self._steering) >= MAX_QUEUED_MESSAGES):
            self._set_status("待处理插话过多，补充内容尚未发送；请稍后重试。")
            return False
        if not self._write_worker({"op": "steer", "request_id": self._request_id,
                                   "message_id": row["id"], "prompt": text, "attachments": []}):
            self._set_status("助手后台未接收补充内容，请稍后重试。")
            return False
        self._steering[row["id"]] = row
        self._interjections.append({"id": row["id"], "name": self.userName,
            "text": text, "files": [], "state": "sent"})
        self._record_event("steering_sent", {"message_id": row["id"], "prompt": text,
            "attachments": [], "source": "decision_supplement"})
        self.transcriptChanged.emit()
        self._set_status("补充已作为插话发送；当前任务继续，模型将在下一次请求处理。")
        return True

    @Slot(str, result=bool)
    def steerMessage(self, prompt: str) -> bool:
        if getattr(self, "_application_exit_frozen", False):
            self._set_status("应用正在等待退出确认，消息尚未发送。")
            return False
        if not self.busy:
            return self.sendMessage(prompt)
        if self._active_project_context_generation != self._project_context_generation:
            self._set_status("工程上下文已改变；请将消息排队，不能插入原工程任务。")
            return False
        if self._stop_pending:
            self._set_status("当前任务正在停止；请发送为待处理消息。")
            return False
        row, handled = self._prepare_message(prompt)
        if row is None:
            return handled
        if (len(self._steering) >= MAX_STEERING_MESSAGES
                or len(self._pending_messages) + len(self._steering) >= MAX_QUEUED_MESSAGES):
            self._set_status("待处理插话过多，请等待当前任务吸收已有消息。")
            return False
        if not self._write_worker({"op": "steer", "request_id": self._request_id,
                                   "message_id": row["id"], "prompt": row["prompt"],
                                   "attachments": row["attachments"]}):
            return False
        self._steering[row["id"]] = row
        self._interjections.append({"id": row["id"], "name": self.userName,
                                     "text": row["prompt"], "files": row["attachments"], "state": "sent"})
        self._record_event("steering_sent", {"message_id": row["id"], "prompt": row["prompt"],
                                              "attachments": row["attachments"]})
        self._consume_draft()
        self.transcriptChanged.emit()
        self._set_status("插话已发送；当前任务继续，在下一次模型决策时处理。")
        return True

    def _set_interjection_state(self, message_id: str, state: str) -> None:
        for item in self._interjections:
            if item["id"] == message_id:
                item["state"] = state

    def _return_unapplied_steering(self, terminal: str) -> None:
        for message_id, row in self._steering.items():
            row["paused"] = terminal != "completed"
            self._pending_messages.append(row)
            self._set_interjection_state(message_id, "paused" if row["paused"] else "queued")
        self._steering.clear()
        self._withdraw_pending.clear()
        self.queueChanged.emit()

    def _reply_content(self, final: str) -> str:
        stream = self._streaming_text.strip()
        final = final.strip()
        if not stream or final.startswith(stream):
            return final or stream
        if not final or stream.endswith(final):
            return stream
        return (stream + "\n\n" + final)[:MAX_REPLY_BYTES]

    def _start_message(self, row: dict) -> bool:
        if getattr(self, "_application_exit_frozen", False):
            return False
        if self.busy or self._closed or row["session_id"] != self._session:
            return False
        if not self._project_message_is_current(row):
            self._set_status("工程上下文已改变；原消息未发送，请撤回输入框后重新发送。")
            return False
        text = row["prompt"]
        try:
            validate_assistant_url(str(self._config.get("assistant_api_url", "")))
            if not self.activeModel:
                raise ValueError("请先选择模型")
            if self._store.remaining_tokens(self._config) == 0:
                raise ValueError("已达到 Token 限额")
            self._ensure_worker()
        except (OSError, ValueError) as exc:
            self._set_status(str(exc))
            return False
        request_id = self._run_coordinator.begin_run()
        self._active_project_context_generation = self._project_context_generation
        run_config = {**self._worker_config(), **row.get("config_overrides", {}),
                      "output_audio": row.get("output_audio", False)}
        run_mode = str(run_config.get("task_mode", "chat"))
        try:
            self._store.set_ui_session_mode(self._session, self.taskMode)
            self._store.bind_ui_request(self._session, request_id, run_mode)
        except (OSError, ValueError, sqlite3.Error) as exc:
            self._run_coordinator.finish_run(request_id)
            self._set_status(f"无法记录可信请求模式：{exc}")
            return False
        background = row.get("background_notification")
        self._background_job_id = str(background.get("job_id") or "") if isinstance(background, dict) else ""
        self._final_message_request = ""
        self._task_started = time.monotonic()
        self._round_usage = {}
        self._thoughts = {}
        self._steps = []
        self._round_files = []
        self._interjections = []
        self._clear_stream()
        self._refresh_capabilities()
        selected_attachments = [dict(item) for item in row["attachments"]]
        self._append_message("event" if self._background_job_id else "user", text,
            request_id=request_id, metadata={"attachments": selected_attachments,
                                                 "background_job_id": self._background_job_id})
        self._reload_messages()
        self.sessionsChanged.emit()
        self.transcriptChanged.emit()
        self._set_busy(True)
        self._set_status("任务已收到，正在连接助手后台…")
        self._record_event("progress", {"stage": "received", "state": "queued",
                                        "text": "任务已收到，正在连接助手后台…",
                                        "mode": run_mode, "model": self.activeModel,
                                        "elapsed_s": 0.0})
        if not self._write_worker({"op": "run", "request_id": request_id,
                                  "session_id": self._session, "prompt": text,
                                  "attachments": selected_attachments,
                                  "background_notification": background,
                                  "assistant_project_target": row.get("assistant_project_target"),
                                  "config": run_config}):
            if (self._run_coordinator.request_id != request_id
                    or not self._run_coordinator.run_active):
                return False
            self._finish_progress("error", "助手后台未接收任务，请检查后台状态后重试。")
            self._complete_round("error", "助手后台未接收任务，请检查后台状态后重试。")
            self._set_status("助手后台未接收任务，请检查后台状态后重试。")
            if not self._run_coordinator.finish_run(request_id):
                return False
            self._set_busy(False)
            self._emit_task_settled("error", "助手后台未接收任务，请检查后台状态后重试。",
                                    request_id=request_id)
            return False
        return True

    def _ensure_intro(self) -> None:
        if not self._store.messages(self._session):
            self._store.add_message(self._session, "assistant",
                "你好，我是看板娘，叫我板娘就好。\n\n"
                "我会陪你整理设计、分析膜厚、调整修正板和运行本项目的沉积仿真。"
                "工程任务会先核对软件支持的结构和计算条件，再推进操作；超出模型能力时会说清楚。\n\n"
                "在下方选择讨论、操作或攻关；用 @ 引用文件，用 / 查看常用命令。"
                "也可以在授权的数据目录中整理文件、编写沙箱脚本和绘图。"
                "图片、音频和文档是否可理解，取决于当前服务与模型支持。",
                metadata={"introduction": True})

    def _reset_message_view(self) -> None:
        self._storage_generation += 1
        self._cancel_history_projection()
        self._history_anchor_key = ""
        self._storage_read_session = ""
        self._storage_overlay_session = ""
        self._storage_overlay_messages = []
        self._history_error = ""
        self._history_loading = False
        self._messages = []
        self._display = []
        self._history = []
        self._turn_cache = {
            key: value for key, value in self._turn_cache.items()
            if _plain_reusable_cached_turn(value)
        }
        self._body_sources.clear()
        self._stable_turns = None
        retained_documents, retained_steps = self._cached_history_retention()
        self._finish_turn_view(retained_documents, retained_steps)
        self._session_usage = {}
        self._compaction_request = ""
        self._compaction_state = {}
        self.compactionChanged.emit()

    @staticmethod
    def _message_turn_key(session: str, message: dict) -> str:
        role = message.get("role")
        if role == "assistant" and message.get("request_id"):
            return session + ":round:" + str(message["request_id"])
        return session + ":message:" + str(message.get("id", ""))

    def _cached_history_retention(self) -> tuple[set[str], set[str]]:
        documents: set[str] = set()
        steps: set[str] = set()
        for cached in self._history_session_cache.values():
            documents.update(cached.get("documents", set()))
            steps.update(cached.get("step_keys", set()))
        state = self._history_projection
        if state is not None:
            documents.update(state.get("documents", set()))
            steps.update(state.get("step_keys", set()))
        return documents, steps

    def _save_history_session_cache(self, session: str) -> None:
        state = self._history_projection
        if state is None or not state.get("tail_ready"):
            return
        old_sources = list(state["older_source_rows"])
        tail_sources = list(state["tail_source_rows"])
        old_models = list(state["older_model_rows"])
        tail_models = list(state["tail_model_rows"])
        source_rows = old_sources + tail_sources
        model_rows = old_models + tail_models
        if not source_rows or len(source_rows) != len(model_rows):
            return

        message_by_key = {}
        index_by_key = {}
        for index, message in enumerate(self._messages):
            if message.get("role") in ("user", "assistant"):
                key = self._message_turn_key(session, message)
                message_by_key[key] = message
                index_by_key[key] = index

        # Keep ordinary session caches small. When the current viewport is
        # reading deeper history, extend this contiguous suffix to retain its
        # stable anchor instead of silently dropping the visible row.
        cache_start = max(0, len(source_rows) - 512)
        anchor_key = str(self._history_anchor_key or "")
        anchor_index = next((index for index, row in enumerate(source_rows)
                             if str(row.get("key", "")) == anchor_key), -1)
        if anchor_index >= 0:
            cache_start = min(cache_start, max(0, anchor_index - 128))
        dropped = cache_start
        if dropped:
            source_rows = source_rows[dropped:]
            model_rows = model_rows[dropped:]
        old_count = max(0, len(old_sources) - dropped)
        if old_count > len(source_rows):
            old_count = len(source_rows)
        old_sources, tail_sources = source_rows[:old_count], source_rows[old_count:]
        old_models, tail_models = model_rows[:old_count], model_rows[old_count:]
        boundary_key = str(source_rows[0].get("key", ""))
        cursor = int(state.get("cursor", 0))
        if dropped and boundary_key in index_by_key:
            cursor = index_by_key[boundary_key]

        documents: set[str] = set()
        step_keys = set()
        for row in source_rows:
            key = str(row.get("key", ""))
            step_keys.add(key)
            entry = self._turn_view_cache.get(key)
            if entry is not None and entry[0] is row:
                documents.update(entry[2])
        snapshots = {str(row["key"]): message_by_key.get(str(row["key"]))
                     for row in source_rows if str(row.get("key", "")) in message_by_key}
        cache = {
            "cursor": cursor,
            "boundary_key": boundary_key,
            "anchor_key": anchor_key if anchor_index >= dropped else "",
            "older_source_rows": old_sources,
            "tail_source_rows": tail_sources,
            "older_model_rows": old_models,
            "tail_model_rows": tail_models,
            "loaded_older_count": len(old_models),
            "loaded_message_signatures": snapshots,
            "snapshot_message_ids": {str(message.get("id")) for message in self._messages},
            "documents": documents,
            "step_keys": step_keys,
            "seen_artifacts": self._loaded_artifact_ids(model_rows),
        }
        self._history_session_cache.pop(session, None)
        self._history_session_cache[session] = cache
        while len(self._history_session_cache) > 3:
            self._history_session_cache.pop(next(iter(self._history_session_cache)))

    @staticmethod
    def _loaded_artifact_ids(rows: list[dict]) -> set[str]:
        seen: set[str] = set()
        for row in rows:
            if row.get("role") != "assistant":
                continue
            seen.update(str(value) for value in row.get("inlineFileIds", []))
            for item in row.get("files", []) + row.get("allFiles", []):
                if isinstance(item, dict) and item.get("artifact_id"):
                    seen.add(str(item["artifact_id"]))
        return seen

    def _restore_history_session_cache(self, session: str) -> None:
        cached = self._history_session_cache.pop(session, None)
        if cached is None:
            return
        self._history_session_cache[session] = cached
        self._history_projection_generation += 1
        self._history_projection = {
            "generation": self._history_projection_generation,
            "messages": (),
            "base_message_ids": set(cached.get("snapshot_message_ids", set())),
            "cursor": int(cached["cursor"]),
            "pending": False,
            "tail_ready": True,
            "loaded_older_count": int(cached["loaded_older_count"]),
            "newest_reference_ids": set(),
            "tail_source_rows": list(cached["tail_source_rows"]),
            "tail_model_rows": list(cached["tail_model_rows"]),
            "older_source_rows": list(cached["older_source_rows"]),
            "older_model_rows": list(cached["older_model_rows"]),
            "appended_rows": [], "current_messages": [], "live_rows": [],
            "seen_artifacts": set(cached["seen_artifacts"]),
            "documents": set(cached["documents"]),
            "step_keys": set(cached["step_keys"]),
            "boundary_key": str(cached.get("boundary_key", "")),
            "loaded_message_signatures": dict(cached.get("loaded_message_signatures", {})),
            "snapshot_message_ids": set(cached.get("snapshot_message_ids", set())),
        }
        self._history_anchor_key = str(cached.get("anchor_key", ""))
        for source in (self._history_projection["older_source_rows"]
                       + self._history_projection["tail_source_rows"]):
            if source.get("document") is None and not source.get("steps"):
                self._body_sources[str(source.get("key", ""))] = (
                    str(source.get("text", "")), False)
        self._turn_model.update_turns(
            self._history_projection["older_model_rows"]
            + self._history_projection["tail_model_rows"])
        self.historyProjectionChanged.emit()

    def _reload_messages(self) -> None:
        if not hasattr(self, "_turn_model"):
            self._apply_messages(self._store.messages(self._session))
            self._context = self._store.context_state(self._session)
            return
        self._storage_generation += 1
        generation, session, config = self._storage_generation, self._session, dict(self._config)
        self._storage_read_session = session
        self._history_loading = True
        self._history_error = ""
        self.historyProjectionChanged.emit()
        def read_snapshot():
            return {"generation": generation, "session": session,
                    "messages": self._store.messages(session),
                    "sessions": self._store.sessions(),
                    "usage": self._store.usage_summary(config),
                    "session_usage": self._store.session_usage(session),
                    "context": self._store.context_state(session)}
        future = self._storage_pool.submit(read_snapshot)
        def ready(done):
            try:
                value = done.result()
            except Exception as exc:
                value = {"generation": generation, "session": session,
                         "error": f"无法加载对话历史：{exc}"}
            if not self._closed:
                self.storageReady.emit(value)
        future.add_done_callback(ready)

    def _append_message(self, role: str, content: str, *, request_id: str = "", metadata: dict | None = None) -> None:
        """Publish the committed row immediately; async reads never blank output."""
        identity = self._store.add_message(self._session, role, content,
                                          request_id=request_id, metadata=metadata)
        message = {"id": identity, "role": role, "content": content,
                   "created": time.time(), "request_id": request_id,
                   "metadata": metadata or {}}
        self._messages.append(message)
        if self._history_loading and self._storage_read_session == self._session:
            if self._storage_overlay_session != self._session:
                self._storage_overlay_session = self._session
                self._storage_overlay_messages = []
            self._storage_overlay_messages.append(message)
        self._display.append((role, content))
        if role in ("user", "assistant"):
            self._history.append({"role": role, "content": content})
        self._stable_turns = None

    def _cancel_history_projection(self) -> None:
        was_pending = self._history_projection is not None
        self._history_projection_generation += 1
        self._history_projection_timer.stop()
        self._history_projection = None
        if was_pending:
            self.historyProjectionChanged.emit()

    def _start_history_projection(self, *, allow_busy: bool = False) -> bool:
        # Small conversations stay on the existing immediate path. Longer
        # histories publish the latest 24 rows first, then prepend older rows.
        if (self.busy and not allow_busy) or len(self._messages) <= 48 or not hasattr(self, "_turn_model"):
            return False
        if self._history_projection is not None:
            self._cancel_history_projection()
        self._history_projection_timer.stop()
        self._history_projection_generation += 1
        generation = self._history_projection_generation
        self._history_projection = {
            "generation": generation,
            "messages": tuple(self._messages),
            "base_message_ids": {str(message.get("id")) for message in self._messages},
            "cursor": len(self._messages),
            "pending": True,
            "tail_ready": False,
            "loaded_older_count": 0,
            "newest_reference_ids": {
                str(message.get("id")) for message in self._messages[-512:]
            },
            "tail_source_rows": [],
            "tail_model_rows": [],
            "older_source_rows": [],
            "older_model_rows": [],
            "appended_rows": [],
            "current_messages": [],
            "live_rows": [],
            "seen_artifacts": set(),
            "documents": set(),
            "step_keys": set(),
            "loaded_message_signatures": {},
            "snapshot_message_ids": {str(message.get("id")) for message in self._messages},
            "boundary_key": "",
        }
        self.historyProjectionChanged.emit()
        self._history_projection_timer.start(0)
        return True

    def _advance_history_projection(self) -> None:
        state = self._history_projection
        if (self._closed or state is None
                or not state.get("pending")
                or state["generation"] != self._history_projection_generation):
            return
        end = state["cursor"] if state["tail_ready"] else len(state["messages"])
        batch_size = 32 if state["tail_ready"] else 24
        start = max(0, end - batch_size)
        try:
            seen_artifacts = set(state["seen_artifacts"])
            source_rows = self._build_turn_rows(
                list(state["messages"][start:end]), live=False,
                _artifact_seen=seen_artifacts)
            model_rows, documents, step_keys = self._shape_turn_sources(source_rows)
            if not state["tail_ready"]:
                state["tail_source_rows"] = source_rows
                state["tail_model_rows"] = model_rows
                state["tail_ready"] = True
                self._turn_model.update_turns(model_rows)
                self.historyProjectionBatchInserted.emit()
            else:
                if model_rows:
                    self.historyProjectionBatchAboutToInsert.emit(len(model_rows))
                    self._turn_model.prepend_turns(model_rows)
                    self.historyProjectionBatchInserted.emit()
                state["older_source_rows"] = source_rows + state["older_source_rows"]
                state["older_model_rows"] = model_rows + state["older_model_rows"]
                state["loaded_older_count"] = len(state["older_model_rows"])
            for message in state["messages"][start:end]:
                if message.get("role") in ("user", "assistant"):
                    state["loaded_message_signatures"][
                        self._message_turn_key(self._session, message)] = message
            loaded_sources = state["older_source_rows"] + state["tail_source_rows"]
            if loaded_sources:
                state["boundary_key"] = str(loaded_sources[0].get("key", ""))
            state["seen_artifacts"] = seen_artifacts
            state["cursor"] = start
            state["documents"].update(documents)
            state["step_keys"].update(step_keys)
        except Exception:
            # Preserve already-visible rows and the uncommitted cursor so a
            # later explicit retry can replay this same bounded page.
            state["pending"] = False
            self._history_error = "无法加载更早的对话记录，请重试。"
            self.historyProjectionChanged.emit()
            return
        state["pending"] = False
        self._history_error = ""
        self._finish_turn_view(state["documents"], state["step_keys"])
        self.historyProjectionChanged.emit()
        self.transcriptChanged.emit()

    @Slot(object)
    def _storage_ready(self, value: dict) -> None:
        if self._closed or value.get("session") != self._session or value.get("generation") != self._storage_generation:
            return
        self._history_loading = False
        if value.get("error"):
            self._history_error = str(value["error"])
            self.historyProjectionChanged.emit()
            return
        messages = list(value["messages"])
        if self._storage_overlay_session == self._session:
            known_ids = {str(row.get("id", "")) for row in messages}
            messages.extend(row for row in self._storage_overlay_messages
                            if str(row.get("id", "")) not in known_ids)
            self._storage_overlay_messages = []
            self._storage_overlay_session = ""
        self._apply_messages(messages)
        self._sessions_cache = value["sessions"]
        self._usage_summary, self._session_usage = value["usage"], value["session_usage"]
        if not self.busy and value.get("context"):
            self._context = value["context"]
            self.contextChanged.emit()
        self.sessionsChanged.emit()
        self.usageChanged.emit()
        projection_requested = self._projection_requested
        self._projection_requested = False
        if self._history_projection is not None and self._history_projection.get("tail_ready"):
            if not self._rebase_history_projection():
                self._cancel_history_projection()
                self._turn_model.update_turns([])
                if self._start_history_projection(allow_busy=projection_requested):
                    self.historyProjectionChanged.emit()
                    return
            self.transcriptChanged.emit()
        elif not self._start_history_projection(allow_busy=projection_requested):
            self.transcriptChanged.emit()
        self.historyProjectionChanged.emit()

    def _rebase_history_projection(self) -> bool:
        """Reattach a loaded suffix to a fresh snapshot without replaying old pages."""
        state = self._history_projection
        if state is None or not state.get("tail_ready"):
            return False
        by_key = {}
        index_by_key = {}
        for index, message in enumerate(self._messages):
            if message.get("role") in ("user", "assistant"):
                key = self._message_turn_key(self._session, message)
                by_key[key] = message
                index_by_key[key] = index
        signatures = state.get("loaded_message_signatures", {})
        changed_messages = {}
        for key, previous in signatures.items():
            current = by_key.get(key)
            if current is None:
                # A removed loaded message changes the contiguous projected range;
                # let the caller rebuild from the latest suffix in that case.
                return False
            if current != previous:
                changed_messages[key] = current

        older_sources = list(state["older_source_rows"])
        tail_sources = list(state["tail_source_rows"])
        older_models = list(state["older_model_rows"])
        tail_models = list(state["tail_model_rows"])
        loaded_sources = older_sources + tail_sources
        source_index_by_key = {
            str(source.get("key", "")): index
            for index, source in enumerate(loaded_sources)
        }
        if any(key not in source_index_by_key for key in changed_messages):
            return False
        earliest_changed_index = None
        changed_artifact_ids: set[str] = set()
        try:
            # Rebuild only changed rows, newest first. _build_turn_rows uses
            # this set to keep a duplicate artifact attached to its newest
            # loaded owner. The raw allFiles list lets unchanged and rebuilt
            # source rows contribute references without re-rendering them.
            seen_artifacts: set[str] = set()

            def source_artifact_ids(source: dict) -> set[str]:
                identities = set()
                for field in ("_fileViews", "allFiles", "files"):
                    for item in source.get(field, []) or []:
                        if isinstance(item, dict) and item.get("artifact_id"):
                            identities.add(str(item["artifact_id"]))
                identities.update(str(value) for value in source.get("inlineFileIds", [])
                                  if value)
                return identities

            def add_artifact_ids(source: dict) -> None:
                seen_artifacts.update(source_artifact_ids(source))

            for source in state.get("live_rows", []):
                if source.get("role") == "assistant":
                    add_artifact_ids(source)
            if self.busy:
                seen_artifacts.update(str(item.get("artifact_id")) for item in self._round_files
                                      if item.get("artifact_id"))

            changed_keys = set(changed_messages)
            for source_index in range(len(loaded_sources) - 1, -1, -1):
                source = loaded_sources[source_index]
                key = str(source.get("key", ""))
                if key in changed_keys:
                    if source.get("role") == "assistant":
                        changed_artifact_ids.update(source_artifact_ids(source))
                    message = changed_messages[key]
                    replacements = self._build_turn_rows(
                        [message], live=False, _artifact_seen=set(seen_artifacts))
                    if len(replacements) != 1:
                        return False
                    replacement_source = replacements[0]
                    if str(replacement_source.get("key", "")) != key:
                        return False
                    if source_index < len(older_sources):
                        older_sources[source_index] = replacement_source
                    else:
                        tail_index = source_index - len(older_sources)
                        tail_sources[tail_index] = replacement_source
                    loaded_sources[source_index] = replacement_source
                    if replacement_source.get("role") == "assistant":
                        changed_artifact_ids.update(source_artifact_ids(replacement_source))
                    if not replacement_source.get("steps"):
                        body_identity = key + "/body"
                        if body_identity in self._renderer.documents:
                            self._renderer.document(
                                body_identity, str(message.get("content", "")),
                                streaming=bool(self.busy and message.get("request_id") == self._request_id),
                                color=self._rich_text_color)
                    source = replacement_source
                if source.get("role") == "assistant":
                    add_artifact_ids(source)

            # Recompute presentation ownership using the full per-source file
            # snapshots. A stored-row update can move ownership in either
            # direction: a newer row may claim an older card, or release it.
            placement_seen: set[str] = set()
            for source in state.get("live_rows", []):
                if source.get("role") == "assistant":
                    placement_seen.update(source_artifact_ids(source))
            if self.busy:
                placement_seen.update(str(item.get("artifact_id")) for item in self._round_files
                                      if item.get("artifact_id"))
            placement_changed_keys = set()
            for source_index in range(len(loaded_sources) - 1, -1, -1):
                source = loaded_sources[source_index]
                if source.get("role") != "assistant":
                    continue
                source_ids = source_artifact_ids(source)
                # The newest-to-oldest pass still refreshes the visible seen
                # set for every loaded source, but only sources referencing an
                # artifact whose ownership may have moved need file regrouping.
                if not (source_ids & changed_artifact_ids):
                    placement_seen.update(source_ids)
                    continue
                full_views = source.get("_fileViews")
                if not full_views:
                    full_views = source.get("files", [])
                inline = {str(value) for value in source.get("inlineFileIds", []) if value}
                visible_files = []
                for item in full_views or []:
                    if not isinstance(item, dict):
                        continue
                    identity = str(item.get("artifact_id") or "")
                    if not identity:
                        continue
                    if identity not in placement_seen and identity not in inline:
                        visible_files.append(item)
                # allFiles/inlineFileIds are the complete source of ownership
                # IDs; _fileViews is the already-authorized presentation
                # snapshot used to restore a suppressed card without rescanning
                # or normalizing the artifact during a cache rebase.
                placement_seen.update(source_ids)
                if visible_files != source.get("files", []):
                    key = str(source.get("key", ""))
                    replacement_source = {**source, "files": visible_files}
                    loaded_sources[source_index] = replacement_source
                    if source_index < len(older_sources):
                        older_sources[source_index] = replacement_source
                    else:
                        tail_sources[source_index - len(older_sources)] = replacement_source
                    placement_changed_keys.add(key)
            seen_artifacts = placement_seen
        except Exception:
            return False

        state["older_source_rows"] = older_sources
        state["tail_source_rows"] = tail_sources
        state["older_model_rows"] = older_models
        state["tail_model_rows"] = tail_models
        state["loaded_older_count"] = len(older_models)
        state["seen_artifacts"] = seen_artifacts
        state["loaded_message_signatures"] = {
            **signatures,
            **changed_messages,
        }
        boundary_key = str(state.get("boundary_key", ""))
        if boundary_key and boundary_key in index_by_key:
            state["cursor"] = index_by_key[boundary_key]
        state["messages"] = tuple(self._messages)
        state["snapshot_message_ids"] = {
            str(message.get("id")) for message in self._messages
        }
        state["newest_reference_ids"] = {
            str(message.get("id")) for message in self._messages[-512:]
        }
        model_update_keys = set(changed_messages) | placement_changed_keys
        if model_update_keys:
            for key in model_update_keys:
                self._artifact_placement_cache.pop(key, None)
            for identity, entry in list(self._turn_cache.items()):
                cached_row = entry[2] if len(entry) > 2 else None
                if isinstance(cached_row, dict) and str(cached_row.get("key", "")) in model_update_keys:
                    self._turn_cache.pop(identity, None)
            for source_index, source in enumerate(loaded_sources):
                key = str(source.get("key", ""))
                if key not in model_update_keys:
                    continue
                replacement_model, _, _ = self._shape_turn_sources([source])
                if len(replacement_model) != 1:
                    return False
                if source_index < len(older_sources):
                    older_models[source_index] = replacement_model[0]
                else:
                    tail_models[source_index - len(older_sources)] = replacement_model[0]
                if earliest_changed_index is None or source_index < earliest_changed_index:
                    earliest_changed_index = source_index

            retained_sources = (older_sources + tail_sources + list(state.get("live_rows", [])))
            retained_documents: set[str] = set()
            retained_steps: set[str] = set()
            for source in retained_sources:
                cached = self._turn_view_cache.get(str(source.get("key", "")))
                if cached is not None and cached[0] is source:
                    retained_documents.update(cached[2])
                    retained_steps.add(str(source.get("key", "")))
            state["documents"] = retained_documents
            state["step_keys"] = retained_steps
            self._turn_model.update_turns(
                older_models + tail_models,
                start=earliest_changed_index if earliest_changed_index is not None else 0)
            self._finish_turn_view(retained_documents, retained_steps)
        return True

    def _apply_messages(self, messages: list[dict]) -> None:
        # A final message reload must not invalidate every completed message's
        # renderer/model cache just because SQLite returned new dictionaries.
        previous = {str(row.get("id")): row for row in getattr(self, "_messages", ())}
        self._messages = [previous.get(str(row.get("id")), row)
                          if previous.get(str(row.get("id"))) == row else row
                          for row in messages]
        keep = {str(row.get("id")) for row in self._messages}
        reusable = {}
        for key, value in self._turn_cache.items():
            if key in keep or _plain_reusable_cached_turn(value):
                reusable[key] = value
        # Keep a bounded set of plain rendered rows around. Rows
        # with live documents, steps, or artifacts are rebuilt so ownership and
        # file freshness retain their existing rules.
        if len(reusable) > 512:
            reusable = dict(list(reusable.items())[-512:])
        self._turn_cache = reusable
        self._display = [(m["role"], m["content"]) for m in self._messages]
        self._history = [{"role": role, "content": text} for role, text in self._display
                         if role in ("user", "assistant")]
        self._stable_turns = None

    def _context_capacity(self) -> int:
        configured = cast(int, model_token_limits(self._config,
                                                  str(self._config.get("assistant_api_url", "")),
                                                  cast(str, self.activeModel))["context_window_tokens"])
        if configured > 0:
            return configured
        for row in self._discovered_models:
            if str(row.get("id", "")) == self.activeModel:
                for key in ("context_length", "context_window", "max_context_tokens"):
                    value = row.get(key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                        return int(value)
        return 0

    @Property(str, notify=configurationChanged)
    def userName(self) -> str:
        return str(self._config.get("assistant_user_name", "用户") or "用户")

    @Slot()
    @Slot(str)
    def refreshAppearance(self, color: str = "") -> None:
        color = str(color or self._rich_text_color)
        if color == self._rich_text_color and self._turn_cache:
            return
        self._rich_text_color = color
        self._turn_cache.clear()
        self._stable_turns = None
        self.transcriptChanged.emit()

    @Property(int, constant=True)
    def wheelScrollLines(self) -> int:
        hints = QGuiApplication.styleHints()
        return max(1, hints.wheelScrollLines()) if hints is not None else 3

    @Property(QUrl, constant=True)
    def headUrl(self) -> QUrl:
        return QUrl.fromLocalFile(str(UI_DIR / "assets" / "head.png"))

    @Property(QUrl, constant=True)
    def attachmentDirectory(self) -> QUrl:
        from coat_sim.core.data_files import DATA_ROOT
        return QUrl.fromLocalFile(str(DATA_ROOT))

    @Property("QVariantList", notify=attachmentsChanged)
    def attachments(self) -> list[dict]:
        return [file_view(row) for row in self._attachments]

    @Property("QVariantMap", notify=contextChanged)
    def contextUsage(self) -> dict:
        result = dict(self._context)
        if (result.get("source", "main") != "main" or result.get("model") != self.activeModel or not result.get("api_url")
                or self._discovery_bus.api_key(str(result["api_url"]))
                != self._discovery_bus.api_key(str(self._config.get("assistant_api_url", "")))):
            # An estimate belongs to the request's provider/model, including on replay.
            # Older history without this identity remains unknown until a new request.
            result = {}
        if "total_capacity" in result:
            total_capacity = result["total_capacity"]
        elif "capacity" in result:
            total_capacity = result["capacity"]
        else:
            total_capacity = self._context_capacity()
        total = int(total_capacity or 0)
        budget = int(result.get("input_budget", 0) or 0)
        used = int(result.get("current_tokens", result.get("input_tokens", 0)) or 0)
        known = budget > 0
        result.update(capacity=total, input_budget=budget, used=used,
                      ratio=min(1.0, used / budget) if known else 0.0,
                      known=known,
                      label=f"{format_tokens(used)} / {format_tokens(budget)}" if known else f"{format_tokens(used)} · 上限未知")
        return result

    @Property(str, notify=compactionChanged)
    def compactionStatus(self) -> str:
        state = self._compaction_state
        labels = {"queued": "已排队，将在下一次模型请求前压缩。", "running": "正在压缩运行上下文…",
                  "skipped": "当前上下文无需压缩。", "failed": "上下文压缩失败，历史记录已保留。"}
        if state.get("state") == "completed":
            return ("上下文已压缩：" + format_tokens(state.get("before_tokens", 0)) + " → "
                    + format_tokens(state.get("after_tokens", 0)) + " Token。显示历史保留。")
        label = labels.get(state.get("state"), "")
        return label + (" " + str(state.get("text", ""))[:320] if state.get("state") == "failed" else "")

    @Property(bool, notify=compactionChanged)
    def compacting(self) -> bool:
        return self._compaction_state.get("state") in ("queued", "running")

    @Slot(result=bool)
    def compactContext(self) -> bool:
        if self.compacting:
            return False
        if self.busy and self._active_project_context_generation != self._project_context_generation:
            self._set_status("工程上下文已改变；请等待原任务结束后再整理当前上下文。")
            return False
        request = self._request_id if self.busy else uuid4().hex
        payload = {"op": "compact", "request_id": request, "session_id": self._session,
                   "assistant_project_target": self._project_message_context()["assistant_project_target"]}
        if not self.busy:
            payload.update(config=self._worker_config())
        self._compaction_request = request
        self._compaction_state = {"state": "queued"}
        self.compactionChanged.emit()
        try:
            self._ensure_worker()
            if not self._write_worker(payload):
                raise OSError("助手后台未接收请求")
        except Exception as exc:
            self._compaction_request = ""
            self._compaction_state = {"state": "failed"}
            self.compactionChanged.emit()
            self._set_status("无法提交上下文压缩：" + str(exc))
            return False
        return True

    @Property("QVariantList", notify=transcriptChanged)
    def turns(self) -> list[dict]:
        if self._history_projection is not None:
            # Preserve the legacy complete-data getter without allowing a
            # diagnostic/export read to cancel or replace the visible page.
            current = [m for m in self._messages if self.busy and m["role"] == "assistant"
                       and m.get("request_id") == self._request_id and self._request_id]
            current_ids = {str(m["id"]) for m in current}
            stable = self._build_turn_rows(
                [m for m in self._messages if str(m["id"]) not in current_ids], live=False)
            return stable + (self._build_turn_rows(current, live=True) if self.busy else [])
        if self._stable_turns is None:
            self._stable_current_messages = [m for m in self._messages if self.busy and m["role"] == "assistant"
                                            and m.get("request_id") == self._request_id and self._request_id]
            current_ids = {m["id"] for m in self._stable_current_messages}
            self._stable_turns = self._build_turn_rows([m for m in self._messages if m["id"] not in current_ids], live=False)
        if self.busy:
            return self._stable_turns + self._build_turn_rows(self._stable_current_messages, live=True)
        return self._stable_turns

    def _build_turn_rows(self, messages: list[dict], *, live: bool,
                         _artifact_seen: set[str] | None = None) -> list[dict]:
        rows = []
        now = time.monotonic()
        for message in messages:
            role = message["role"]
            if role not in ("user", "assistant"):
                continue
            current = (role == "assistant" and message.get("request_id") == self._request_id
                       and bool(self._request_id))
            cached = self._turn_cache.get(str(message.get("id")))
            if (not (current and self.busy) and cached and len(cached) >= 6
                    and (cached[0] is message or cached[0] == message)
                    and cached[1] == self.userName
                    and str(cached[2].get("key", "")).startswith(self._session + ":")):
                # File stat/preview work has its own cadence, independent of
                # 120ms streamed text and of messages containing no artifacts.
                versions = cached[4]
                if cached[3] and now - cached[5] >= 2.0:
                    snapshot = merge_artifact_files(cached[3])
                    versions = tuple((item["artifact_id"], item["modified_ns"], item["size"]) for item in snapshot)
                    self._turn_cache[str(message.get("id"))] = (*cached[:5], now)
                if versions == cached[4]:
                    cached_row = cached[2]
                    if cached_row.get("document") is None and not cached_row.get("steps"):
                        self._body_sources[str(cached_row["key"])] = (
                            str(message.get("content", "")), bool(current and self.busy))
                    rows.append(cached[2])
                    continue
            metadata = dict(message.get("metadata") or {})
            if current and self.busy:
                metadata.update(self._round_metadata("running"))
            text_parts = [message["content"]] + [str(item.get("output", ""))
                for item in metadata.get("steps", []) if isinstance(item, dict)]
            body_files = self._body_files_for_view(str(message.get("id")), "\n\n".join(text_parts), running=current and self.busy)
            files = list(metadata.get("files") or metadata.get("attachments") or []) + body_files
            snapshot = merge_artifact_files(files)
            versions = tuple((item["artifact_id"], item["modified_ns"], item["size"]) for item in snapshot)
            views = ([{**item, **file_view(item)} for item in snapshot] if role == "assistant"
                     else [file_view(item) for item in files])
            unique = {}
            for view in views:
                unique.setdefault(view.get("artifact_id") or view.get("path") or view.get("name"), view)
            elapsed = float(metadata.get("elapsed_s", 0) or 0)
            footer = ""
            if role == "assistant" and metadata.get("status") not in (None, "running"):
                status = {"completed": "完成", "cancelled": "已停止", "error": "失败",
                          "limited": "已暂停", "incomplete": "未完整",
                          "interrupted": "中断"}.get(metadata.get("status"), "")
                footer = f"{status} · {elapsed:.1f} 秒"
            message_key = (self._session + ":round:" + str(message["request_id"])
                           if role == "assistant" and message.get("request_id")
                           else self._session + ":message:" + str(message.get("id", len(rows))))
            step_views = self._step_views(metadata.get("steps", []), owner=message_key)
            document = None
            if not step_views:
                self._body_sources[message_key] = (message["content"], bool(current and self.busy))
                if current and self.busy:
                    # Keep the active streaming row eager so token updates retain
                    # their existing path; completed history is created on demand.
                    document = self._renderer.document(
                        message_key + "/body", message["content"],
                        streaming=bool(current and self.busy), color=self._rich_text_color)
            else:
                self._body_sources.pop(message_key, None)
            rows.append({"key": message_key,
                         "role": role, "name": self.userName if role == "user" else "看板娘",
                         "text": message["content"],
                         "html": "",
                         "document": document,
                         "documentStyle": self._rich_text_color,
                         "thoughts": _visible_thoughts(metadata.get("thoughts")),
                         "steps": step_views,
                         "interjections": self._interjection_views(metadata.get("interjections", [])),
                         "files": list(unique.values()), "_fileViews": list(unique.values()),
                         "allFiles": files,
                         "inlineFileIds": [item["artifact_id"] for item in body_files] if role == "assistant" else [],
                         "footer": footer,
                         "notice": (str(metadata.get("status_message", "")) if metadata.get("status") not in (None, "running", "completed") and metadata.get("status_message") != message["content"] else ""),
                         "running": bool(current and self.busy),
                         "progress": self._status if current and self.busy else ""})
            if not (current and self.busy):
                self._turn_cache[str(message.get("id"))] = (message, self.userName, rows[-1], files, versions, now)
        if live and self.busy and self._final_message_request != self._request_id:
            body_files = self._body_files_for_view(self._request_id, "\n\n".join([self._streaming_text] +
                [str(item.get("output", "")) for item in self._steps]), running=True)
            files = merge_artifact_files(self._round_files, body_files)
            round_key = self._session + ":round:" + self._request_id
            step_views = self._step_views(self._steps, owner=round_key)
            self._body_sources[round_key] = (self._streaming_text, True)
            body_document = (None if step_views else self._renderer.document(
                round_key + "/body", self._streaming_text, streaming=True,
                color=self._rich_text_color))
            live_file_views = [{**item, **file_view(item)} for item in files]
            rows.append({"key": round_key, "role": "assistant", "name": "看板娘",
                         "text": self._streaming_text,
                         "html": "",
                         "document": body_document,
                         "documentStyle": self._rich_text_color,
                         "thoughts": _visible_thoughts(self._thoughts.values()),
                         "steps": step_views,
                         "interjections": self._interjection_views(self._interjections),
                         "files": live_file_views, "_fileViews": live_file_views,
                         "allFiles": files, "inlineFileIds": [item["artifact_id"] for item in body_files],
                         "footer": "", "notice": "", "running": True, "progress": self._status})
        # Rows already contain authorized file snapshots. Choosing the latest
        # owner must not normalize and stat every file a second time per frame.
        seen = (_artifact_seen if _artifact_seen is not None else
                ({item["artifact_id"] for item in self._round_files if item.get("artifact_id")}
                 if not live and self.busy else set()))
        for index in range(len(rows) - 1, -1, -1):
            source = rows[index]
            if source["role"] != "assistant":
                continue
            inline = set(source.get("inlineFileIds", []))
            group = []
            for item in source["files"]:
                identity = item["artifact_id"]
                if identity not in seen:
                    if identity not in inline:
                        group.append(item)
                    seen.add(identity)
            cached = self._artifact_placement_cache.get(source["key"])
            if cached is not None and cached[0] is source and cached[1] == group:
                rows[index] = cached[2]
            else:
                row = source if group == source["files"] else {**source, "files": group}
                self._artifact_placement_cache[source["key"]] = (source, group, row)
                rows[index] = row
        return rows

    def _body_files_for_view(self, identity: str, text: str, *, running: bool) -> list[dict]:
        now = time.monotonic()
        previous = self._body_reference_cache.get(identity)
        if running and previous and now - previous[0] < 0.9:
            return previous[1]
        files = referenced_artifacts(text)
        self._body_reference_cache[identity] = (now, files)
        if len(self._body_reference_cache) > 512:
            projection = self._history_projection
            if projection is None:
                victim = next(iter(self._body_reference_cache))
            else:
                keep = projection["newest_reference_ids"]
                victim = next((key for key in self._body_reference_cache if key not in keep),
                              next(iter(self._body_reference_cache)))
            self._body_reference_cache.pop(victim, None)
        return files

    def _round_metadata(self, state: str) -> dict:
        return {"thoughts": _visible_thoughts(self._thoughts.values()),
                "steps": [dict(item) for item in self._steps],
                "interjections": [dict(item) for item in self._interjections],
                "elapsed_s": max(0.0, time.monotonic() - self._task_started) if self._task_started else 0.0,
                "usage": dict(self._round_usage),
                "files": list(self._round_files), "context": dict(self._context), "status": state,
                "background_job_id": self._background_job_id}

    def _interjection_views(self, values: list[dict]) -> list[dict]:
        labels = {"sent": "已发送", "received": "已收到，等待下一次决策", "applied": "已纳入本轮请求",
                  "queued": "转为下一轮待发送", "paused": "待发送已暂停",
                  "withdrawing": "撤回中", "withdrawn": "已撤回"}
        return [{**item, "html": "",
                 "document": self._renderer.document(self._session + "/interjection/" + str(item.get("id", "")), str(item.get("text", "")), streaming=False, color=self._rich_text_color),
                 "files": [file_view(value) for value in item.get("files", [])],
                 "canWithdraw": self.busy and item.get("id") in self._steering and item.get("id") not in self._withdraw_pending,
                 "stateLabel": labels.get(item.get("state"), "")}
                for item in values if isinstance(item, dict)]

    def _model_step(self, value: dict) -> dict | None:
        source = str(value.get("source", "main"))
        if source == "summary":
            return None
        step_id = str(value.get("step_id") or (self._request_id + ":" + source))
        step = next((item for item in self._steps if item["stepId"] == step_id), None)
        if step is None:
            step = {"stepId": step_id, "source": source, "model": str(value.get("model", "")),
                    "thinking": "", "output": "", "thinkingDone": False, "done": False, "status": "running"}
            self._steps.append(step)
        return step

    def _step_views(self, values: list[dict], *, owner: str) -> list[dict]:
        return [{**item, "html": "",
                 "thoughtDocument": self._renderer.plain_document(owner + "/thought/" + str(item["stepId"]), str(item.get("thinking", ""))),
                 "document": self._renderer.document(owner + "/step/" + str(item["stepId"]),
                    str(item.get("output", "")), streaming=not bool(item.get("done")), color=self._rich_text_color)}
                for item in values if isinstance(item, dict) and item.get("source") != "summary"
                and (item.get("thinking") or item.get("output"))]

    @Slot(str, result=bool)
    def copyText(self, text: str) -> bool:
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            return False
        clipboard.setText(text)
        self._set_status("已复制完整文本。")
        return True

    @Slot(str, result=bool)
    def copyTurn(self, key: str) -> bool:
        turn = self._copy_source_turn(str(key))
        return self.copyText(_copy_turn_text(turn)) if turn is not None else False

    @Property(str, notify=transcriptChanged)
    def conversationText(self) -> str:
        rows = [self._copy_message_turn(message) for message in self._messages
                if message.get("role") in ("user", "assistant")]
        if self.busy and self._final_message_request != self._request_id:
            rows.append(self._copy_message_turn({
                "id": self._request_id, "role": "assistant",
                "content": self._streaming_text, "request_id": self._request_id,
                "metadata": self._round_metadata("running"),
            }))
        return ("\n\n" + "─" * 32 + "\n\n").join(_copy_turn_text(item) for item in rows)

    def _copy_source_turn(self, key: str) -> dict | None:
        message_marker = self._session + ":message:"
        round_marker = self._session + ":round:"
        if key.startswith(message_marker):
            message_id = key[len(message_marker):]
            message = next((row for row in self._messages
                            if str(row.get("id")) == message_id), None)
            return self._copy_message_turn(message) if message is not None else None
        if key.startswith(round_marker):
            request_id = key[len(round_marker):]
            message = next((row for row in reversed(self._messages)
                            if row.get("role") == "assistant"
                            and str(row.get("request_id") or "") == request_id), None)
            if message is not None:
                return self._copy_message_turn(message)
            if self.busy and request_id == self._request_id:
                return self._copy_message_turn({
                    "id": request_id, "role": "assistant",
                    "content": self._streaming_text, "request_id": request_id,
                    "metadata": self._round_metadata("running"),
                })
        return None

    def _copy_message_turn(self, message: dict) -> dict:
        """Shape full source text for clipboard without touching the visible model."""
        role = str(message.get("role", "assistant"))
        text = str(message.get("content", ""))
        metadata = dict(message.get("metadata") or {})
        current = (role == "assistant" and self.busy
                   and message.get("request_id") == self._request_id
                   and bool(self._request_id))
        if current:
            metadata.update(self._round_metadata("running"))
        steps = [item for item in metadata.get("steps", []) if isinstance(item, dict)]
        text_parts = [text] + [str(item.get("output", "")) for item in steps]
        files = list(metadata.get("files") or metadata.get("attachments") or [])
        files.extend(referenced_artifacts("\n\n".join(text_parts)))
        elapsed = float(metadata.get("elapsed_s", 0) or 0)
        footer = ""
        if role == "assistant" and metadata.get("status") not in (None, "running"):
            status = {"completed": "完成", "cancelled": "已停止", "error": "失败",
                      "limited": "已暂停", "incomplete": "未完整",
                      "interrupted": "中断"}.get(metadata.get("status"), "")
            footer = f"{status} · {elapsed:.1f} 秒"
        labels = {"sent": "已发送", "received": "已收到，等待下一次决策", "applied": "已纳入本轮请求",
                  "queued": "转为下一轮待发送", "paused": "待发送已暂停",
                  "withdrawing": "撤回中", "withdrawn": "已撤回"}
        interjections = [{**item, "stateLabel": labels.get(item.get("state"), "")}
                         for item in metadata.get("interjections", []) if isinstance(item, dict)]
        status_message = str(metadata.get("status_message", ""))
        notice = (status_message if metadata.get("status") not in (None, "running", "completed")
                  and status_message != text else "")
        return {"name": self.userName if role == "user" else "看板娘",
                "text": text, "steps": steps, "thoughts": _visible_thoughts(metadata.get("thoughts")),
                "interjections": interjections, "allFiles": files,
                "footer": footer, "notice": notice}

    @Slot(result=bool)
    def copyConversation(self) -> bool:
        return self.copyText(self.conversationText)

    def _complete_round(self, state: str, text: str) -> None:
        if not self._request_id or not self._task_started:
            return
        for thought in self._thoughts.values():
            if not thought.get("done"):
                thought.update(done=True, interrupted=True, unavailable=not bool(thought.get("text")))
        for step in self._steps:
            step.update(done=True, thinkingDone=True)
        metadata = {**self._round_metadata(state), "status_message": text}
        if self._final_message_request != self._request_id:
            partial = self._streaming_text.strip()
            content = partial or text or "本轮没有返回文字。"
            self._append_message("assistant", content,
                                    request_id=self._request_id, metadata=metadata)
            self._final_message_request = self._request_id
        session, request = self._session, self._request_id
        for index in range(len(self._messages) - 1, -1, -1):
            row = self._messages[index]
            if row["role"] == "assistant" and row.get("request_id") == request:
                self._messages[index] = {**row, "metadata": {**row.get("metadata", {}), **metadata}}
                self._stable_turns = None
                break
        def persist_final():
            settled = {**metadata, "usage": self._store.round_usage(request)}
            self._store.update_message_metadata(session, request, settled)
        self._storage_pool.submit(persist_final)
        self._reload_messages()
        self.transcriptChanged.emit()

    @Slot()
    def toggleChat(self) -> None:
        self.toggleRequested.emit()

    @Slot(str)
    def deleteConversation(self, session: str) -> None:
        if self.busy:
            self._set_status("请先停止当前任务，再删除会话。")
            return
        if not any(row["id"] == session for row in self.sessions):
            return
        self._history_session_cache.pop(session, None)
        self._store.delete_session(session)
        self._pending_messages = [row for row in self._pending_messages if row["session_id"] != session]
        self.queueChanged.emit()
        if self._session == session:
            remaining = self._store.sessions()
            self._session = remaining[0]["id"] if remaining else self._store.new_session()
            self._store.set_ui_session_mode(self._session, self.taskMode)
            self._projection_requested = True
            self._reset_message_view()
            self._ensure_intro()
            self._reload_messages()
            self._load_progress()
            self.transcriptChanged.emit()
            self.conversationChanged.emit()
        else:
            self._reload_messages()
        self.sessionsChanged.emit()
        self._set_status("会话已删除，工程文件与 Token 记账保留。")

    @Slot("QVariantList", result=bool)
    def addAttachments(self, values: list) -> bool:
        from coat_sim.core.data_files import import_attachment
        try:
            imported = []
            seen = {str(row.get("path")) for row in self._attachments}
            total = sum(int(row.get("size", 0)) for row in self._attachments)
            for value in values:
                value = value.toLocalFile() if isinstance(value, QUrl) else str(value)
                if value.startswith("file:"):
                    value = QUrl(value).toLocalFile()
                row = import_attachment(value)
                if str(row["path"]) in seen:
                    continue
                if len(self._attachments) + len(imported) >= 8:
                    raise ValueError("每轮最多 8 个附件。")
                total += int(row.get("size", 0))
                if total > 32 * 1024 * 1024:
                    raise ValueError("每轮附件合计不能超过 32 MiB。")
                if str(row.get("mime", "")).startswith("video/"):
                    raise ValueError("当前聊天接口不支持直接输入视频，请先提取需要分析的画面。")
                imported.append(row)
                seen.add(str(row["path"]))
            self._attachments.extend(imported)
            self.attachmentsChanged.emit()
            self._set_status("附件已加入；发送后将交给当前模型处理。")
            return True
        except (OSError, ValueError) as exc:
            self._set_status(str(exc))
            return False

    @Slot(int)
    def removeAttachment(self, index: int) -> None:
        if 0 <= index < len(self._attachments):
            self._attachments.pop(index)
            self.attachmentsChanged.emit()

    @Slot(result=bool)
    def pasteClipboardImage(self) -> bool:
        """A user paste imports pixels only; never execute clipboard HTML/URLs."""
        clipboard = QGuiApplication.clipboard()
        mime = clipboard.mimeData() if clipboard is not None else None
        if mime is None or not mime.hasImage():
            return False  # Let TextArea handle ordinary text/other paste formats.
        target = None
        try:
            if len(self._attachments) >= 8:
                raise ValueError("每条消息最多 8 个附件，请先移除附件。")
            image = clipboard.image()
            if image.isNull():
                raise ValueError("无法读取剪贴板图片。")
            if image.width() * image.height() > 16 * 1024 * 1024:
                raise ValueError("剪贴板图片超过 16 M 像素，请缩小后粘贴。")
            buffer = QBuffer()
            if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
                raise OSError("无法建立图片缓冲区。")
            try:
                if not image.save(buffer, "PNG"):
                    raise ValueError("剪贴板图片不能转换为 PNG。")
                payload = bytes(buffer.data())
            finally:
                buffer.close()
            available = 32 * 1024 * 1024 - sum(int(row.get("size", 0)) for row in self._attachments)
            if len(payload) > available:
                raise ValueError("图片使附件合计超过 32 MiB。")
            from coat_sim.core.data_files import data_path, file_info
            target = data_path("attachments/" + uuid4().hex + "/clipboard.png", root_allowed=False)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                output.write(payload)
            self._attachments.append(file_info(target))
            self.attachmentsChanged.emit()
            self._set_status("剪贴板图片已加入附件，可预览、发送或插话。")
            return True
        except (OSError, ValueError) as exc:
            if target is not None:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
            self._set_status(str(exc))
            return False

    @Slot(str, result=bool)
    def isPreviewableImageLink(self, value: str) -> bool:
        if not value or value.startswith(("http://", "https://")):
            return False
        try:
            path = authorized_file(value)
            return path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tif", ".tiff"}
        except (OSError, ValueError):
            return False

    @Slot(str)
    def activateLink(self, value: str) -> None:
        if value.startswith(("http://", "https://")):
            QDesktopServices.openUrl(QUrl(value))
        else:
            self.previewFile(value)

    @Slot(str)
    def previewFile(self, value: str) -> None:
        try:
            path = authorized_file(value)
            view = file_view(str(path))
            if not view.get("valid"):
                raise ValueError(view.get("error", "此文件不能预览"))
            mime = str(view.get("mime", ""))
            suffix = path.suffix.lower()
            text_suffixes = {".csrc", ".csub", ".cmsk", ".cmac", ".cproc", ".cfilm", ".txt", ".log", ".py", ".json", ".yaml", ".yml", ".xml", ".ini", ".cfg", ".toml"}
            rich_suffixes = {".md", ".markdown", ".html", ".htm"}
            view["previewKind"] = ("image" if view["image"] else "table" if suffix in {".csv", ".tsv"}
                                   else "richtext" if suffix in rich_suffixes
                                   else "plaintext" if suffix in text_suffixes or mime.startswith("text/") else "external")
            if path.is_dir() or view["previewKind"] == "external":
                self.openFile(str(path), "default")
                return
            view["content"] = ""
            if view["previewKind"] != "image":
                with path.open("rb") as stream:
                    raw = stream.read(512 * 1024 + 1)
                truncated = len(raw) > 512 * 1024
                try:
                    content = raw[:512 * 1024].decode("utf-8-sig", errors="replace" if truncated else "strict")
                except UnicodeError as exc:
                    self._record_file_issue(exc, "preview_file")
                    self.openFile(str(path), "default")
                    return
                if "\x00" in content:
                    self.openFile(str(path), "default")
                    return
                if view["previewKind"] == "table":
                    rows = []
                    for index, row in enumerate(csv.reader(io.StringIO(content), delimiter="\t" if suffix == ".tsv" else ",")):
                        if index >= 500:
                            truncated = True
                            break
                        rows.append("<tr>" + "".join("<td>" + html.escape(cell) + "</td>" for cell in row[:50]) + "</tr>")
                        truncated = truncated or len(row) > 50
                    view["content"] = '<table border="1" cellspacing="0" cellpadding="4">' + "".join(rows) + "</table>"
                elif view["previewKind"] == "richtext":
                    view["content"] = safe_rich_text(content, color=self._rich_text_color or None, base_path=path)
                else:
                    view["content"] = content
                view["truncated"] = truncated
            self.previewRequested.emit(view)
        except (OSError, ValueError, csv.Error) as exc:
            self._record_file_issue(exc, "preview_file")
            self._set_status(str(exc))

    def _record_file_issue(self, error: Exception, stage: str) -> None:
        # The optional development recorder must not change normal file flows.
        try:
            from coat_sim.agent.environment_issues import record_issue
            record_issue(error, stage=stage, tool=stage, request_id=self._request_id,
                         session_id=self._session, source="ui", config=self._config,
                         error_type=type(error).__name__)
        except Exception:
            pass

    @Slot(str, str)
    def openFile(self, value: str, mode: str = "default") -> None:
        try:
            path = authorized_file(value)
            if mode == "folder":
                opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(path if path.is_dir() else path.parent)))
            else:
                opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            if not opened:
                raise ValueError("系统未能打开此文件，请检查文件关联和默认应用。")
        except (OSError, ValueError) as exc:
            self._record_file_issue(exc, "open_file")
            self._set_status(str(exc))

    def debug_status(self) -> dict:
        endpoint = urlsplit(str(self._config.get("assistant_api_url", "")))
        host = endpoint.hostname or ""
        if endpoint.port:
            host += ":" + str(endpoint.port)
        provider = urlunsplit((endpoint.scheme, host, endpoint.path, "", ""))
        usage = self.contextUsage
        return {"pid": os.getpid(), "session_id": self._session, "request_id": self._request_id,
                "busy": self.busy, "cancelling": self.cancelling, "enabled": self.enabled,
                "provider": provider, "model": self.activeModel, "mode": self.taskMode,
                "status": self._status, "queued_count": self.queuedCount,
                "pending_messages": self.pendingMessages,
                "steering": [{"id": key, "withdrawing": key in self._withdraw_pending} for key in self._steering],
                "context": {key: usage.get(key) for key in ("capacity", "used", "ratio", "known", "label", "actual")},
                "messages": [{**{k: v for k, v in row.items() if k != "document"},
                    "steps": [{k: v for k, v in item.items() if k != "document"} for item in row.get("steps", [])],
                    "interjections": [{k: v for k, v in item.items() if k != "document"} for item in row.get("interjections", [])]}
                    for row in self.turns], "events": list(self._task_events)}

    def debug_submit(self, prompt: str, options: dict | None = None) -> dict:
        from coat_sim.agent.development import require_development
        require_development()
        if getattr(self, "_application_exit_frozen", False):
            return {"accepted": False, "reason": "应用正在等待退出确认，消息尚未发送。"}
        options = dict(options or {})
        if self.busy and not options.get("queue", False):
            return {"accepted": False, "reason": "当前任务仍在运行；显式 queue=true 才能排队。", "session_id": self._session}
        if options.get("new_session") and self.busy:
            return {"accepted": False, "reason": "任务运行中不能新建调试会话。"}
        mode = str(options.get("mode", self.taskMode))
        reasoning = str(options.get("reasoning", self.reasoningStrength))
        if mode not in ASSISTANT_TASK_MODES or reasoning not in self.reasoningOptions:
            return {"accepted": False, "reason": "调试模式或推理档位不受当前模型支持。"}
        limits = {"max_output_tokens": 4096, "request_limit": 12, "total_tokens_limit": 60000}
        for key, value in dict(options.get("limits") or {}).items():
            maximum = {"max_output_tokens": 64000, "request_limit": 100, "total_tokens_limit": 2_000_000}.get(key)
            if maximum is None or isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                return {"accepted": False, "reason": "无效调试预算：" + key}
            limits[key] = value
        row, handled = self._prepare_message(prompt)
        if row is None:
            return {"accepted": handled, "session_id": self._session, "reason": "本地命令已处理。" if handled else self._status}
        try:
            validate_assistant_url(str(self._config.get("assistant_api_url", "")))
            if not self.activeModel:
                raise ValueError("请先选择模型")
            if self._store.remaining_tokens(self._config) == 0:
                raise ValueError("已达到 Token 限额")
            if not self.busy:
                self._ensure_worker()
        except (OSError, ValueError) as exc:
            self._set_status(str(exc))
            return {"accepted": False, "session_id": self._session, "reason": self._status}
        if options.get("new_session"):
            # Failed validation must leave the selected conversation untouched.
            self.newConversation()
            row["session_id"] = self._session
        row["config_overrides"] = {"task_mode": mode, "reasoning_strength": reasoning, "_debug_limits": limits}
        if self.busy:
            if len(self._pending_messages) + len(self._steering) >= MAX_QUEUED_MESSAGES:
                return {"accepted": False, "reason": "待处理消息已达到上限。"}
            row["paused"] = self._stop_pending
            self._pending_messages.append(row)
            self._consume_draft()
            self.queueChanged.emit()
            return {"accepted": True, "session_id": self._session, "message_id": row["id"], "queued": True, "limits": limits}
        accepted = self._start_message(row)
        if accepted:
            self._consume_draft()
        return {"accepted": accepted, "session_id": self._session,
                "request_id": self._request_id if accepted else "", "reason": "" if accepted else self._status, "limits": limits}

    def debug_steer(self, prompt: str) -> dict:
        from coat_sim.agent.development import require_development
        require_development()
        if getattr(self, "_application_exit_frozen", False):
            return {"accepted": False, "reason": "应用正在等待退出确认，消息尚未发送。"}
        before = set(self._steering)
        accepted = self.steerMessage(prompt)
        message_id = next(iter(set(self._steering) - before), "")
        return {"accepted": accepted, "session_id": self._session, "request_id": self._request_id,
                "message_id": message_id, "reason": "" if accepted else self._status}

    def debug_withdraw(self, message_id: str) -> dict:
        from coat_sim.agent.development import require_development
        require_development()
        queued = any(row["id"] == message_id for row in self._pending_messages)
        accepted = self.withdrawQueuedMessage(message_id) if queued else self.withdrawSteering(message_id)
        return {"accepted": accepted, "pending_ack": accepted and not queued,
                "message_id": message_id, "session_id": self._session, "request_id": self._request_id,
                "reason": "" if accepted else self._status}

    @Slot(str, str)
    def openWithApplication(self, value: str, application_url: str) -> None:
        """Run only the program explicitly chosen in the native Qt file picker."""
        try:
            path = authorized_file(value)
            url = QUrl(application_url)
            if not url.isLocalFile():
                raise ValueError("请选择本地应用程序")
            application = Path(url.toLocalFile()).resolve(strict=True)
            if not application.is_file():
                raise ValueError("请选择应用程序可执行文件")
            started, _pid = QProcess.startDetached(str(application), [str(path)])
            if not started:
                raise OSError("无法启动所选应用程序，请确认它可执行并支持此文件格式")
        except (OSError, ValueError) as exc:
            self._set_status(str(exc))

    @Slot(str, result="QVariantList")
    def completions(self, prefix: str) -> list[dict]:
        if prefix.startswith("/"):
            commands = {"/help": "常用命令", "/new": "新会话", "/clear": "清空本会话",
                        "/compact": "压缩运行上下文，保留显示历史",
                        "/stop": "停止任务", "/plan": "讨论模式（只读规划）", "/chat": "讨论模式（兼容命令）", "/operate": "操作模式",
                        "/research": "攻关模式", "/settings": "打开设置", "/attach": "添加附件",
                        "/audio": "下一轮请求语音输出", "/text": "恢复文字输出"}
            return [{"label": key, "insert": key + " ", "detail": value}
                    for key, value in commands.items() if key.startswith(prefix.lower())]
        if not prefix.startswith("@"):
            return []
        from coat_sim.core.data_files import list_data_files
        query = prefix[1:].strip("{}").lower()
        aliases = {"文件": "", "机器": "machines", "工艺": "processes", "源": "sources",
                   "基片": "substrates", "修正板": "masks", "结果": "results"}
        result = []
        result += [{"label": "@" + key, "insert": "@" + key,
                    "detail": "引用 " + (value or "data") + " 内的文件"}
                   for key, value in aliases.items() if key != query and key.startswith(query)]
        try:
            values = list_data_files("data/" + aliases.get(query, ""), recursive=True, limit=1000)
            for row in values["entries"]:
                if row.get("directory") or row.get("name") == ".gitkeep":
                    continue
                reference = row["reference"]
                if query not in aliases and query and query not in reference.lower():
                    continue
                result.append({"label": row["name"], "insert": "@{" + reference + "} ",
                               "detail": reference})
                if len(result) >= 30:
                    break
        except (OSError, ValueError):
            pass
        return result

    def _local_reply(self, text: str) -> None:
        self._append_message("assistant", text, metadata={"local": True})
        self._reload_messages()
        self.transcriptChanged.emit()

    def _command(self, text: str) -> tuple[str, bool]:
        if not text.startswith("/"):
            return text, False
        command, _, rest = text.partition(" ")
        command = command.lower()
        if command == "/help":
            self._local_reply(
                "**常用命令**\n\n"
                "| 命令 | 用途 |\n| --- | --- |\n"
                "| /new、/clear | 新建会话、清空本会话 |\n"
                "| /stop | 停止当前任务 |\n"
                "| /compact | 手动压缩运行上下文，保留显示历史 |\n"
                "| /plan（兼容 /chat）、/operate、/research | 只读讨论、操作、攻关 |\n"
                "| /settings、/attach | 打开设置、选择附件 |\n"
                "| /audio 问题、/text | 请求语音回复、恢复文字输出 |\n\n"
                "输入 @ 可选择 data 内的文件；引用格式为 @{data/目录/文件名}。"
                "外部文件请通过附件按钮明确选择，文件会复制到 data/attachments。"
                "图片链接不会从远程网站自动加载。")
            return "", True
        if command == "/new":
            self.newConversation(); return "", True
        if command == "/clear":
            self.clearConversation(); return "", True
        if command == "/stop":
            self.cancelTask(); return "", True
        if command == "/compact":
            self.compactContext(); return "", True
        if command in ("/plan", "/chat", "/operate", "/research"):
            self.setTaskMode("chat" if command == "/plan" else command[1:])
            return rest.strip(), not bool(rest.strip())
        if command == "/settings":
            self.settingsRequested.emit(); return "", True
        if command == "/attach":
            self.attachmentRequested.emit(); return "", True
        if command == "/text":
            self._output_audio = False
            self._set_status("已恢复文字输出。")
            return rest.strip(), not bool(rest.strip())
        if command == "/audio":
            self._output_audio = True
            self._set_status("下一轮将请求语音回复；需要当前模型与服务支持。")
            return rest.strip(), not bool(rest.strip())
        return text, False


from coat_sim.ui.assistant_window_controller import AssistantController
