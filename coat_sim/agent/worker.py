"""GUI-free, cancellable JSONL host for the engineering assistant.

The launcher owns this process.  stdout is exclusively the protocol; library
output goes to stderr.  Secrets arrive through stdin or Windows credentials,
never command line arguments, environment variables, or persisted settings.
"""
from __future__ import annotations

import asyncio
import json
import re
import stat
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4
from collections.abc import Mapping
from typing import Any, TextIO

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_MAX_LINE = 1_048_576
_STREAM_BATCH_SECONDS = .040
_STREAM_BATCH_CHARS = 4096
_EVENT_BATCH_SECONDS = .100
_TOOLKIT_SETTING_KEYS = (
    "assistant_toolkit_enabled", "assistant_toolkit_workspace_root",
    "assistant_toolkit_capabilities", "assistant_toolkit_profile_capabilities",
    "assistant_toolkit_allow_model_context",
)


class ProtocolWriter:
    def __init__(self, stream: TextIO) -> None:
        from coat_sim.agent.toolkit_config import normalized_toolkit_settings

        self.stream = stream
        self.secret = ""
        self.quota_config: dict[str, Any] = {}
        self._toolkit_config = normalized_toolkit_settings({})
        self._toolkit_generation = 0
        self._toolkit_lock = threading.Lock()
        self.sessions: dict[str, str] = {}
        self.event_store: Any = None
        self._steering: dict[str, list[dict[str, Any]]] = {}
        self._steering_seen: dict[str, set[str]] = {}
        self._steering_applied: dict[str, set[str]] = {}
        self._steering_rejected: dict[str, set[str]] = {}
        self._steering_withdrawn: dict[str, set[str]] = {}
        self._issue_configs: dict[str, dict[str, Any]] = {}
        self._decisions: dict[str, dict[str, Any]] = {}
        self._decision_counts: dict[str, int] = {}
        self._last_optional_decision: dict[str, float] = {}
        self._decision_answers: dict[str, list[dict[str, Any]]] = {}
        self._compaction_pending: set[str] = set()
        self._stream_pending: dict[str, Any] | None = None
        self._stream_timer: asyncio.TimerHandle | None = None
        self._event_pending: list[tuple[str, str, str, dict[str, Any], float]] = []
        self._event_timer: asyncio.TimerHandle | None = None

    def request_compaction(self, request: str) -> None:
        duplicate = request in self._compaction_pending
        self._compaction_pending.add(request)
        self.emit("context_compaction", request, state="queued", source="main", reason="manual",
                  duplicate=duplicate, text="已安排在下一次安全请求边界整理上下文；当前任务继续。")

    def consume_compaction(self, request: str) -> bool:
        if request not in self._compaction_pending:
            return False
        self._compaction_pending.remove(request)
        return True

    def has_compaction(self, request: str) -> bool:
        return request in self._compaction_pending

    def _flush_stream(self) -> None:
        if self._stream_timer is not None:
            self._stream_timer.cancel()
            self._stream_timer = None
        pending, self._stream_pending = self._stream_pending, None
        if pending is not None:
            values = {**pending["values"], "text": "".join(pending["pieces"])}
            self._emit_now(pending["event"], pending["request_id"], **values)

    def _flush_events(self) -> None:
        if self._event_timer is not None:
            self._event_timer.cancel()
            self._event_timer = None
        pending, self._event_pending = self._event_pending, []
        if pending and self.event_store is not None:
            try:
                self.event_store.record_events(pending)
            except Exception:
                sys.stderr.write("助手任务事件未能持久化。\n")

    def flush(self) -> None:
        self._flush_stream()
        self._flush_events()

    async def ask_decision(self, request: str, question: str, options: list[dict[str, str]],
                           *, optional: bool = False, countdown_s: int = 15) -> dict[str, Any]:
        """Wait for the desktop's real answer; an optional default is labeled separately."""
        if (not question.strip() or len(question) > 500 or not 2 <= len(options) <= 3
                or any(not isinstance(item, dict) or not isinstance(item.get("label"), str)
                       or not item["label"].strip() or len(item["label"]) > 40
                       or not isinstance(item.get("description"), str)
                       or not item["description"].strip() or len(item["description"]) > 160
                       for item in options)):
            return {"ok": False, "error": "问题须简短，提供 2–3 个选项；每个选项包含 label 和简短利弊 description。推荐项放第一位。"}
        if self._decision_counts.get(request, 0) >= 3:
            return {"ok": False, "status": "question_limit", "error": "本轮已询问 3 次。请合并说明剩余必要条件后结束本轮等待；不得猜测需要用户明确确认的答案。"}
        if any(row["request_id"] == request for row in self._decisions.values()):
            return {"ok": False, "status": "already_pending", "error": "已有问题等待答复，请先处理现有问题。"}
        if optional and time.monotonic() - self._last_optional_decision.get(request, -60.0) < 30:
            return {"ok": False, "status": "optional_question_throttled",
                    "error": "常规可选决定刚刚询问过。请在现有授权内采用合理默认值并说明，不要连续弹窗。"}
        identity = uuid4().hex
        seconds = max(10, min(30, int(countdown_s))) if optional else 0
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        row = {"decision_id": identity, "request_id": request,
               "session_id": self.sessions.get(request, ""), "question": question.strip(),
               "options": [{"label": item["label"].strip(), "description": item["description"].strip()}
                           for item in options], "optional": optional, "countdown_s": seconds,
               "default_index": 0, "deadline_ms": int(time.time() * 1000) + seconds * 1000 if optional else 0,
               "future": future}
        self._decisions[identity] = row
        self._decision_counts[request] = self._decision_counts.get(request, 0) + 1
        if optional:
            self._last_optional_decision[request] = time.monotonic()
        self.emit("decision_requested", request, **{key: value for key, value in row.items()
                  if key not in ("future", "request_id")})
        try:
            return await future
        finally:
            self._decisions.pop(identity, None)

    def receive_decision(self, request: str, command: Mapping[str, Any]) -> None:
        identity = command.get("decision_id")
        row = self._decisions.get(identity) if isinstance(identity, str) else None
        if row is None or row["request_id"] != request or row["future"].done():
            self.emit("decision_rejected", request, decision_id=str(identity or "")[:128],
                      text="该问题已结束或不属于当前任务。")
            return
        index = command.get("selection_index")
        supplement = command.get("free_text", "")
        automatic = command.get("automatic") is True
        if (not isinstance(index, int) or isinstance(index, bool) or index < -1
                or index >= len(row["options"]) or not isinstance(supplement, str)
                or len(supplement) > 4000 or (index == -1 and not supplement.strip())
                or (automatic and (not row["optional"] or index != 0 or supplement))):
            self.emit("decision_rejected", request, decision_id=identity, text="答复参数无效，问题仍等待答复。")
            return
        # A timeout can choose only the explicitly optional first option. It is
        # never represented as a human grant or a generic execution approval.
        if automatic and int(time.time() * 1000) < row["deadline_ms"]:
            self.emit("decision_rejected", request, decision_id=identity, text="默认选项倒计时尚未结束。")
            return
        answer = {"ok": True, "decision_id": identity, "question": row["question"],
                  "selection_index": index, "selected_label": row["options"][index]["label"] if index >= 0 else "补充输入",
                  "free_text": supplement.strip(), "source": "default_countdown" if automatic else "user",
                  "notice": "可选事项采用预先设定的默认值；不是用户明确授权。" if automatic else "这是用户对本次问题的真实答复。"}
        if not automatic:
            self._decision_answers.setdefault(request, []).append(dict(answer))
        self.emit("decision_answered", request, **{key: value for key, value in answer.items() if key != "ok"})
        row["future"].set_result(answer)

    def consume_decision_answers(self, request: str) -> list[dict[str, Any]]:
        return self._decision_answers.pop(request, [])

    def clear_decisions(self, request: str) -> None:
        for identity, row in list(self._decisions.items()):
            if row["request_id"] == request:
                if not row["future"].done():
                    row["future"].cancel()
                self._decisions.pop(identity, None)
                self.emit("decision_closed", request, decision_id=identity)
        self._decision_counts.pop(request, None)
        self._last_optional_decision.pop(request, None)
        self._decision_answers.pop(request, None)

    def configure_issues(self, request: str, config: Mapping[str, Any]) -> None:
        # Retain only the opt-in flag, not provider credentials or request text.
        self._issue_configs[request] = {"developer_enabled": config.get("developer_enabled") is True}

    def record_issue(self, request: str, error: Any, *, stage: str, tool: str = "", source: str = "worker",
                     error_type: str = "", recovery: str = "", occurrence_id: str = "",
                     fatal: bool = False) -> dict[str, Any] | None:
        try:
            from coat_sim.agent.environment_issues import record_issue
            recorded = record_issue(error, stage=stage, tool=tool, source=source,
                request_id=request, session_id=self.sessions.get(request, ""), error_type=error_type,
                recovery=recovery, config=self._issue_configs.get(request, {"developer_enabled": False}),
                redact=self.redact, occurrence_id=occurrence_id)
            if recorded:
                if fatal and isinstance(error, BaseException):
                    error._coat_environment_issue_logged = True
                if not recorded.get("duplicate"):
                    self.emit("diagnostic", request, phase="issue_recorded", source=source,
                              fingerprint=recorded.get("fingerprint"),
                              category=recorded.get("category"),
                              report_reference=recorded.get("reference"),
                              readable_report_saved=recorded.get("reports_saved") is True)
            else:
                from coat_sim.agent.environment_issues import issue_report_status
                status = issue_report_status()
                if status.get("enabled") and status.get("last_recording_error"):
                    self.emit("diagnostic", request, phase="issue_record_failed", source=source,
                              detail=status["last_recording_error"],
                              text="开发问题自动记录失败，可调用 developer_status 检查原因。")
            return recorded
        except Exception:
            # A missing optional recorder cannot mask the original task failure.
            return None

    def bind_request(self, request: str, session: str) -> None:
        from coat_sim.agent.storage import AssistantStore
        if self.event_store is None:
            self.event_store = AssistantStore()
        self.sessions[request] = session
        self._steering.setdefault(request, [])
        self._steering_seen.setdefault(request, set())
        self._steering_applied.setdefault(request, set())
        self._steering_rejected.setdefault(request, set())
        self._steering_withdrawn.setdefault(request, set())

    def receive_steering(self, request: str, message: dict[str, Any]) -> None:
        identity = message["message_id"]
        if identity in self._steering_seen.setdefault(request, set()):
            event = ("steering_applied" if identity in self._steering_applied.get(request, set()) else
                     "steering_withdrawn" if identity in self._steering_withdrawn.get(request, set()) else
                     "steering_rejected" if identity in self._steering_rejected.get(request, set()) else
                     "steering_received")
            self.emit(event, request, message_id=identity, duplicate=True,
                      text="此插话此前未能加入当前任务，请作为下一轮发送。" if event == "steering_rejected" else "")
            return
        pending = self._steering.setdefault(request, [])
        if len(pending) >= 8:
            self.emit("steering_rejected", request, message_id=identity,
                      text="当前任务已有 8 条待处理插话，请等待或保留到任务结束后发送。")
            return
        pending.append(message)
        self._steering_seen[request].add(identity)
        self.emit("steering_received", request, message_id=identity,
                  text="插话已接收，将在下一次主助手请求前处理；当前任务继续。")

    def pending_messages(self, request: str) -> list[dict[str, Any]]:
        return list(self._steering.get(request, []))

    def withdraw_steering(self, request: str, identity: str) -> None:
        if identity in self._steering_withdrawn.get(request, set()):
            self.emit("steering_withdrawn", request, message_id=identity, duplicate=True)
            return
        if identity in self._steering_applied.get(request, set()):
            self.emit("steering_withdraw_rejected", request, message_id=identity,
                      reason="already_applied", text="此插话已加入当前任务，无法撤回；任务继续。")
            return
        pending = self._steering.get(request, [])
        if not any(item["message_id"] == identity for item in pending):
            self.emit("steering_withdraw_rejected", request, message_id=identity,
                      reason="not_pending", text="未找到待处理的插话，请核对当前任务状态。")
            return
        self._steering[request] = [item for item in pending if item["message_id"] != identity]
        self._steering_withdrawn.setdefault(request, set()).add(identity)
        self.emit("steering_withdrawn", request, message_id=identity,
                  text="待处理插话已撤回，当前任务继续。")

    def apply_steering(self, request: str, identity: str) -> None:
        self._steering[request] = [item for item in self._steering.get(request, []) if item["message_id"] != identity]
        self._steering_applied.setdefault(request, set()).add(identity)
        self.emit("steering_applied", request, message_id=identity,
                  text="插话已加入当前任务的上下文。")

    def reject_steering(self, request: str, identity: str, text: str) -> None:
        self._steering[request] = [item for item in self._steering.get(request, []) if item["message_id"] != identity]
        self._steering_rejected.setdefault(request, set()).add(identity)
        self.emit("steering_rejected", request, message_id=identity, text=text)

    def configure_quota(self, config: Mapping[str, Any]) -> None:
        for key in ("assistant_daily_token_limit", "assistant_period_token_limit", "assistant_period_minutes"):
            value = config.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                self.quota_config[key] = value

    def configure_toolkit(self, config: Mapping[str, Any], *, replace: bool = False) -> bool:
        """Revoke old request grants on meaningful, credential-free scope changes.

        Live configuration can be partial. A new accepted request instead uses
        its own complete settings/defaults. Neither path grants more authority
        to an already-running request, even if settings change back later.
        """
        from coat_sim.agent.toolkit_config import CAPABILITIES, normalized_toolkit_settings

        updates = {key: config[key] for key in _TOOLKIT_SETTING_KEYS if key in config}
        with self._toolkit_lock:
            source = updates if replace else {**self._toolkit_config, **updates}
            normalized = normalized_toolkit_settings(source)
            # Match the adapter's fail-closed handling of malformed explicit
            # capability grants, rather than silently retaining known entries.
            for key in ("assistant_toolkit_capabilities", "assistant_toolkit_profile_capabilities"):
                value = source.get(key, normalized[key])
                if not isinstance(value, (list, tuple)) or any(item not in CAPABILITIES for item in value):
                    normalized["assistant_toolkit_capabilities"] = []
                    normalized["assistant_toolkit_profile_capabilities"] = []
                    break
            if normalized == self._toolkit_config:
                return False
            self._toolkit_config = normalized
            self._toolkit_generation += 1
            return True

    def toolkit_grant_snapshot(self) -> int:
        with self._toolkit_lock:
            return self._toolkit_generation

    def toolkit_generation_is_current(self, generation: object) -> bool:
        with self._toolkit_lock:
            return type(generation) is int and generation == self._toolkit_generation

    def redact(self, text: str) -> str:
        return text.replace(self.secret, "[凭据已隐藏]") if self.secret else text

    def emit(self, event: str, request_id: str = "", **values: Any) -> None:
        # Merge adjacent deltas before stdout crosses into the Qt event queue.
        # A different source, step, text kind or any control event is a barrier,
        # so thinking/text interleaving and tool/decision order stay exact.
        mergeable = (event in ("token", "thinking") and isinstance(values.get("text"), str)
                     and not values.get("reset") and not values.get("done")
                     and (event == "token" or values.get("append") is True))
        if mergeable and values["text"]:
            key = (event, request_id, values.get("source"), values.get("step_id"))
            if self._stream_pending is not None and self._stream_pending["key"] != key:
                self._flush_stream()
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self._flush_stream()
                self._emit_now(event, request_id, **values)
                return
            if self._stream_pending is None:
                self._stream_pending = {"key": key, "event": event, "request_id": request_id,
                                        "values": {k: v for k, v in values.items() if k != "text"},
                                        "pieces": [], "chars": 0}
                self._stream_timer = loop.call_later(_STREAM_BATCH_SECONDS, self._flush_stream)
            self._stream_pending["pieces"].append(values["text"])
            self._stream_pending["chars"] += len(values["text"])
            if self._stream_pending["chars"] >= _STREAM_BATCH_CHARS:
                self._flush_stream()
            return
        self._flush_stream()
        if event in ("completed", "error", "cancelled") and self.consume_compaction(request_id):
            self._emit_now("context_compaction", request_id, state="skipped", source="main", reason="manual",
                           text="任务已结束，待执行的整理已停止；历史和任务状态已保留。")
        self._emit_now(event, request_id, **values)

    def _emit_now(self, event: str, request_id: str = "", **values: Any) -> None:
        if event == "usage" and self.event_store is not None:
            # The Qt renderer can cache these totals instead of opening SQLite
            # for each stream repaint. Usage is emitted only at settled steps.
            try:
                values.setdefault("round_usage", self.event_store.round_usage(request_id))
            except Exception:
                pass
        if event in ("completed", "error", "cancelled"):
            # The UI owns follow-up scheduling. Messages received just before the
            # final response must return to it instead of silently disappearing.
            values.setdefault("remaining_steering", self.pending_messages(request_id))
        payload = {"event": event, "request_id": request_id, **values}

        def sanitize(value: Any) -> Any:
            if isinstance(value, str):
                return self.redact(value)
            if isinstance(value, dict):
                return {self.redact(str(key)): sanitize(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [sanitize(item) for item in value]
            return value

        safe = sanitize(payload)
        if (self.event_store is not None and request_id in self.sessions
                and (event in ("progress", "diagnostic", "capabilities", "completed", "error", "cancelled", "context", "artifact",
                              "steering_received", "steering_applied", "steering_rejected",
                              "steering_withdrawn", "steering_withdraw_rejected", "model_step_started", "model_step_finished", "background_registered",
                              "decision_requested", "decision_answered", "decision_closed", "decision_rejected", "context_compaction")
                     or (event == "thinking" and values.get("done")))):
            self._event_pending.append((self.sessions[request_id], request_id, event, safe, time.time()))
            if len(self._event_pending) >= 32 or event in ("completed", "error", "cancelled", "context_compaction"):
                self._flush_events()
            elif self._event_timer is None:
                try:
                    self._event_timer = asyncio.get_running_loop().call_later(_EVENT_BATCH_SECONDS, self._flush_events)
                except RuntimeError:
                    self._flush_events()
        encoded = json.dumps(safe, ensure_ascii=False, default=str)
        self.stream.write(encoded + "\n")
        self.stream.flush()
        if event in ("completed", "error", "cancelled"):
            self._steering.pop(request_id, None)
            self._steering_seen.pop(request_id, None)
            self._steering_applied.pop(request_id, None)
            self._steering_rejected.pop(request_id, None)
            self._steering_withdrawn.pop(request_id, None)


class RedactedLog:
    def __init__(self, stream: TextIO, writer: ProtocolWriter) -> None:
        self.stream, self.writer = stream, writer

    def write(self, text: str) -> int:
        self.stream.write(self.writer.redact(text))
        return len(text)

    def flush(self) -> None:
        self.stream.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


_MAX_RESEARCH_LIBRARY_SIDECAR_BYTES = 8192


def _research_library_sidecar_path() -> Path:
    """Return the one permitted local research routing file."""
    return Path(__file__).absolute().parents[2] / "config" / "assistant_research_library.json"


def _ensure_research_path_safe(path: Path) -> Path:
    from coat_sim.agent.research_library import ensure_no_reparse_components
    return ensure_no_reparse_components(path)


def _validated_research_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("research library metadata must be an object")
    from coat_sim.agent.research_library import validate_research_library_metadata
    validated = validate_research_library_metadata(value)
    if validated is None:
        raise ValueError("research library metadata is required")
    return validated

def _read_research_library_sidecar() -> tuple[str, dict[str, Any]] | None:
    """Read only the fixed, size-limited research route; never inspect other settings."""
    sidecar = _research_library_sidecar_path()
    try:
        _ensure_research_path_safe(sidecar.parent)
        _ensure_research_path_safe(sidecar)
    except FileNotFoundError:
        return None
    sidecar_info = sidecar.lstat()
    if not stat.S_ISREG(sidecar_info.st_mode):
        raise ValueError("research library sidecar must be a regular file")
    if sidecar_info.st_size > _MAX_RESEARCH_LIBRARY_SIDECAR_BYTES:
        raise ValueError("research library sidecar exceeds its size limit")
    with sidecar.open("rb") as stream:
        payload = stream.read(_MAX_RESEARCH_LIBRARY_SIDECAR_BYTES + 1)
    if len(payload) > _MAX_RESEARCH_LIBRARY_SIDECAR_BYTES:
        raise ValueError("research library sidecar exceeds its size limit")
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("research library sidecar must be an object")
    root = value.get("root")
    if not isinstance(root, str) or not root.strip():
        raise ValueError("research library sidecar has no root")
    return root.strip(), _validated_research_metadata(value)

def _configured_research_library(
        config: Mapping[str, Any]) -> tuple[Any | None, str, str, dict[str, Any] | None]:
    """Return a read-only provider only for an explicit, readable absolute root.

    Checking root metadata does not enumerate its contents. If no explicit root
    is configured, only the fixed small sidecar is consulted. The provider reads
    supported text files on demand after the assistant calls its search tool.
    """
    from coat_sim.agent.research_library import RESEARCH_LIBRARY_SETUP
    raw_root = config.get("assistant_research_library_root")
    metadata = None
    if not isinstance(raw_root, str) or not raw_root.strip():
        try:
            route = _read_research_library_sidecar()
        except Exception:
            return None, "unreadable", "本地研究库路由配置无效或不可读；请检查 JSON、快照元数据及文件权限。" + RESEARCH_LIBRARY_SETUP, None
        if route is None:
            return None, "not_configured", "本轮未配置本地研究库；研究库检索/阅读工具未启用。" + RESEARCH_LIBRARY_SETUP, None
        raw_root, metadata = route

    try:
        configured_path = Path(raw_root.strip())
        from coat_sim.agent.research_library import TextFileResearchLibrary, validate_research_library_root
        validate_research_library_root(raw_root.strip())
        if metadata is None:
            provider = TextFileResearchLibrary(configured_path)
        else:
            provider = TextFileResearchLibrary(configured_path, metadata=metadata)
        if metadata and metadata.get("snapshot_kind") != "complete":
            message = (
                "本地研究库已连接（只读、部分覆盖）："
                f"已纳入 {metadata['included_files']}/{metadata['repository_files']} 个仓库文件，"
                f"排除 {metadata['excluded_files']} 个；snapshot {metadata['snapshot_kind']}，"
                f"commit {metadata['revision']}。{metadata['coverage_note']} "
                "仅按需检索，聊天上下文不会自动注入整库。")
            state = "connected_read_only_partial"
        else:
            message = ("本地研究库已连接（只读）；仅在明确需要时按需检索，"
                       "聊天上下文不会自动注入整库。")
            state = "connected_read_only"
        return provider, state, message, metadata
    except Exception:
        return None, "unreadable", "本地研究库目录无效或不可读；请检查目录是否存在、权限、绝对路径及 symlink/reparse 限制。" + RESEARCH_LIBRARY_SETUP, None

async def _run_task_with_research_library(command: dict[str, Any], writer: Any,
                                          run_task: Any) -> None:
    provider, state, message, metadata = _configured_research_library(command.get("config", {}))
    writer.emit("progress", str(command.get("request_id", "")), stage="executing",
                research_library_status=state, research_library_metadata=metadata, text=message)
    await run_task(command, writer, research_library=provider,
                   allow_research_library_context=provider is not None,
                   research_library_readiness={"status": state, "message": message, "metadata": metadata})


async def serve(writer: ProtocolWriter) -> None:
    loop = asyncio.get_running_loop()
    commands: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    def read_commands() -> None:
        # A daemon reader cannot keep shutdown waiting on an executor's readline.
        while True:
            line = sys.stdin.readline(_MAX_LINE + 1)
            if not line:
                loop.call_soon_threadsafe(commands.put_nowait, None)
                return
            if len(line) > _MAX_LINE or not line.endswith("\n"):
                loop.call_soon_threadsafe(commands.put_nowait, {"op": "invalid"})
                if len(line) > _MAX_LINE:
                    while line and not line.endswith("\n"):
                        line = sys.stdin.readline(_MAX_LINE + 1)
                continue
            try:
                command = json.loads(line)
                if not isinstance(command, dict):
                    raise ValueError("object required")
            except (ValueError, TypeError):
                command = {"op": "invalid"}
            loop.call_soon_threadsafe(commands.put_nowait, command)

    threading.Thread(target=read_commands, name="assistant-stdin", daemon=True).start()
    active: asyncio.Task[None] | None = None
    active_id = ""
    active_kind = "run"

    async def execute(command: Mapping[str, Any]) -> None:
        request_id = str(command["request_id"])
        compacting = command.get("op") == "compact"
        try:
            writer.configure_issues(request_id, command["config"])
            temporary_secret = command.get("api_key")
            if isinstance(temporary_secret, str):
                writer.secret = temporary_secret
            # Keep the session identity available even if storage initialization fails.
            writer.sessions[request_id] = str(command["session_id"])
            writer.bind_request(request_id, str(command["session_id"]))
            writer.configure_quota(command["config"])
            # Optional dependencies are imported only when a task is submitted.
            from coat_sim.agent.runtime import run_task, compact_context
            if compacting:
                await compact_context(dict(command), writer)
            else:
                await _run_task_with_research_library(dict(command), writer, run_task)
        except asyncio.CancelledError:
            if compacting:
                writer.emit("context_compaction", request_id, state="failed", source="main", reason="manual",
                            text="上下文整理已停止；历史和任务状态已保留。")
            else:
                writer.emit("cancelled", request_id, text="任务已停止。对话历史已保留；新的消息不会自动重启此任务。")
        except ImportError as exc:
            # ImportError may arise during client initialization (for example
            # an optional SOCKS transport), long after runtime imports succeed.
            # Preserve its actual cause rather than imply every dependency is
            # absent. Redaction applies before diagnostics cross the protocol.
            detail = writer.redact(str(exc))[:1600]
            missing = getattr(exc, "name", None)
            if not getattr(exc, "_coat_environment_issue_logged", False):
                writer.record_issue(request_id, exc, stage="worker_initialization",
                                    occurrence_id="terminal", fatal=True)
            writer.emit("diagnostic", request_id, phase="initialization_failed",
                        error_type=type(exc).__name__, module=missing, detail=detail)
            explanation = ("当前配置了 SOCKS 代理，但运行环境缺少 socksio 传输组件；请安装项目依赖后重试。"
                           if "socksio" in detail and "SOCKS" in detail else
                           "模块 " + str(missing) + " 未能载入：" + detail if missing else
                           detail or "模块载入失败，请查看任务诊断。")
            writer.emit("context_compaction" if compacting else "error", request_id,
                        **({"state": "failed", "source": "main", "reason": "manual"} if compacting else {}),
                        error_type=type(exc).__name__, module=missing, text="助手初始化失败：" + explanation)
        except Exception as exc:
            if compacting:
                writer.emit("context_compaction", request_id, state="failed", source="main", reason="manual",
                            text=writer.redact(str(exc))[:2000] or "上下文整理失败；历史和任务状态已保留。")
                return
            # A cost boundary is a recoverable task status, not an environment fault.
            from coat_sim.agent.tool_results import TaskLimitReached
            text = writer.redact(str(exc))
            if isinstance(exc, TaskLimitReached):
                writer.emit("diagnostic", request_id, phase="task_limit", text=text[:2000])
                writer.emit("error", request_id, error_type="TaskLimitReached", task_limited=True,
                            text=text[:2000] or "达到本轮开销保护；历史和后台作业已保留。")
            else:
                # No traceback or request objects cross the protocol or log boundary.
                if not getattr(exc, "_coat_environment_issue_logged", False):
                    writer.record_issue(request_id, exc, stage="worker_task", occurrence_id="terminal", fatal=True)
                writer.emit("error", request_id, output_incomplete=bool(getattr(exc, "_coat_response_incomplete", False)),
                            text=(text[:2000] or "助手任务未完成，请检查连接和任务状态。"))
        finally:
            writer.clear_decisions(request_id)
            writer.flush()
            writer.secret = ""
            writer._issue_configs.pop(request_id, None)

    while True:
        command = await commands.get()
        if command is None or command.get("op") == "shutdown":
            if active is not None and not active.done():
                active.cancel()
                await active
            writer.flush()
            return
        op = command.get("op")
        request_id = command.get("request_id", "")
        if op == "configure":
            if isinstance(command.get("config"), dict):
                writer.configure_quota(command["config"])
                toolkit_changed = writer.configure_toolkit(command["config"])
                if active is not None and not active.done():
                    writer.emit("status", active_id, text=(
                        "工具集设置已更新，本轮旧授权已失效；新的工具集授权从下一轮生效。"
                        if toolkit_changed else "额度设置已更新，将用于下一次模型请求。"))
            continue
        if op == "cancel":
            if active is not None and not active.done() and (not request_id or request_id == active_id):
                writer.emit("progress", active_id, stage="cancelling", text="正在停止当前任务并核对后台作业。")
                active.cancel()
            continue
        if op == "compact":
            session_id = command.get("session_id")
            if (not isinstance(request_id, str) or not _IDENTIFIER.fullmatch(request_id)
                    or not isinstance(session_id, str) or not _IDENTIFIER.fullmatch(session_id)):
                writer.emit("context_compaction", str(request_id)[:128], state="failed", source="main", reason="manual",
                            text="上下文整理参数无效。")
                continue
            if active is not None and not active.done():
                if (active_kind == "run" and request_id == active_id
                        and session_id == writer.sessions.get(active_id)):
                    writer.request_compaction(active_id)
                else:
                    writer.emit("context_compaction", request_id, state="failed", source="main", reason="manual",
                                text="已有后台操作，请等待结束后整理当前会话。")
                continue
            if not isinstance(command.get("config"), dict):
                writer.emit("context_compaction", request_id, state="failed", source="main", reason="manual",
                            text="上下文整理缺少模型配置。")
                continue
            active_id, active_kind = request_id, "compact"
            writer.configure_toolkit(command["config"], replace=True)
            command["_toolkit_grant_generation"] = writer.toolkit_grant_snapshot()
            active = asyncio.create_task(execute(command), name=f"compact-{request_id}")
            continue
        if op == "decision_response":
            if isinstance(request_id, str) and request_id == active_id and active is not None and not active.done():
                writer.receive_decision(request_id, command)
            else:
                writer.emit("decision_rejected", str(request_id)[:128],
                            decision_id=str(command.get("decision_id", ""))[:128], text="当前任务已结束，问题无需继续答复。")
            continue
        if op == "steer":
            message_id = command.get("message_id", "")
            prompt = command.get("prompt")
            attachments = command.get("attachments", [])
            if (not isinstance(request_id, str) or request_id != active_id
                    or active is None or active.done()):
                writer.emit("steering_rejected", str(request_id)[:128], message_id=str(message_id)[:128],
                            text="当前任务已结束，请将此消息作为下一轮发送。")
                continue
            if (not isinstance(message_id, str) or not _IDENTIFIER.fullmatch(message_id)
                    or not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8000
                    or not isinstance(attachments, list) or len(attachments) > 8
                    or any(not isinstance(item, dict) or not isinstance(item.get("path"), str)
                           for item in attachments)):
                writer.emit("steering_rejected", request_id, message_id=str(message_id)[:128],
                            text="插话参数无效；当前任务继续。")
                continue
            writer.receive_steering(request_id, {"message_id": message_id, "prompt": prompt,
                                                  "attachments": attachments})
            continue
        if op in ("withdraw", "withdraw_steer"):
            message_id = command.get("message_id", "")
            if (not isinstance(request_id, str) or request_id != active_id
                    or active is None or active.done()):
                writer.emit("steering_withdraw_rejected", str(request_id)[:128], message_id=str(message_id)[:128],
                            reason="task_finished", text="当前任务已结束，请在待发送列表中修改消息。")
            elif not isinstance(message_id, str) or not _IDENTIFIER.fullmatch(message_id):
                writer.emit("steering_withdraw_rejected", request_id, message_id=str(message_id)[:128],
                            reason="invalid_id", text="插话标识无效；当前任务继续。")
            else:
                writer.withdraw_steering(request_id, message_id)
            continue
        if op != "run":
            writer.emit("error", str(request_id)[:128], text="无法识别助手请求。")
            continue
        if active is not None and not active.done():
            writer.emit("error", str(request_id)[:128], text="已有任务正在运行，请等待完成或先取消。")
            continue
        session_id = command.get("session_id")
        prompt = command.get("prompt")
        config = command.get("config")
        if (not isinstance(request_id, str) or not _IDENTIFIER.fullmatch(request_id)
                or not isinstance(session_id, str) or not _IDENTIFIER.fullmatch(session_id)
                or not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8000
                or not isinstance(config, dict)
                or (command.get("background_notification") is not None and (
                    not isinstance(command["background_notification"], dict)
                    or not isinstance(command["background_notification"].get("origin"), dict)
                    or command["background_notification"]["origin"].get("session_id") != session_id
                    or not isinstance(command["background_notification"].get("job_id"), str)
                    or not _IDENTIFIER.fullmatch(command["background_notification"]["job_id"])))
                or not isinstance(command.get("attachments", []), list)
                or len(command.get("attachments", [])) > 8
                or any(not isinstance(item, dict) or not isinstance(item.get("path"), str)
                       for item in command.get("attachments", []))):
            writer.emit("error", str(request_id)[:128], text="任务参数无效。")
            continue
        active_id, active_kind = request_id, "run"
        # Bind before scheduling: a queued configure may run before execute or
        # while runtime initialization awaits an external provider. Never let
        # that newer generation authorize this request's older settings.
        writer.configure_toolkit(config, replace=True)
        command["_toolkit_grant_generation"] = writer.toolkit_grant_snapshot()
        active = asyncio.create_task(execute(command), name=f"assistant-{request_id}")


def main() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    writer = ProtocolWriter(sys.stdout)
    sys.stderr = RedactedLog(sys.stderr, writer)  # type: ignore[assignment]
    sys.stdout = sys.stderr
    async def host() -> None:
        try:
            await serve(writer)
        finally:
            runtime = sys.modules.get("coat_sim.agent.runtime")
            if runtime is not None:
                await runtime.shutdown_runtime()

    asyncio.run(host())


if __name__ == "__main__":
    main()
