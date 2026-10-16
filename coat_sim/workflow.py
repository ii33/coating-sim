"""A small, JSON-checkpointed workflow runner with no third-party dependencies.

Workflows are Python code registered by the host application. Checkpoints store
only data, never executable code, so a host must register the matching
workflow_id and version again when resuming a run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import contextmanager
from contextlib import nullcontext
from copy import deepcopy
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, is_dataclass
import inspect
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Protocol
from uuid import uuid4


_UNSET = object()
NodeHandler = Callable[["WorkflowContext"], Any | Awaitable[Any]]


class WorkflowError(ValueError):
    """Invalid workflow definition, state, or transition."""


def _json_copy(value: Any) -> Any:
    """Validate and detach a value at the persistence boundary."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    elif is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, Path):
        value = str(value)
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise WorkflowError(f"工作流状态必须可转换为有限 JSON 数据：{exc}") from exc


@dataclass(frozen=True)
class StepResult:
    """A node result, optional state patch, and optional dynamic transition."""

    output: Any = None
    update: Mapping[str, Any] = field(default_factory=dict)
    next_node: str | None | object = _UNSET
    wait_for_input: str | None = None


@dataclass(frozen=True)
class WorkflowNode:
    node_id: str
    handler: NodeHandler
    next_node: str | None = None
    kind: str = "program"
    skills: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.node_id.strip():
            raise WorkflowError("工作流节点 ID 不能为空")
        if self.kind not in ("program", "agent"):
            raise WorkflowError("节点 kind 只能是 program 或 agent")
        if not callable(self.handler):
            raise WorkflowError("节点 handler 必须可以调用")


@dataclass(frozen=True)
class Workflow:
    workflow_id: str
    version: str
    start_node: str
    nodes: Sequence[WorkflowNode]
    max_steps: int = 1000

    def node_map(self) -> dict[str, WorkflowNode]:
        if not self.workflow_id.strip() or not self.version.strip():
            raise WorkflowError("workflow_id 与 version 不能为空")
        if type(self.max_steps) is not int or self.max_steps < 1:
            raise WorkflowError("max_steps 必须大于 0")
        result = {node.node_id: node for node in self.nodes}
        if len(result) != len(self.nodes):
            raise WorkflowError("工作流节点 ID 不能重复")
        if self.start_node not in result:
            raise WorkflowError(f"起始节点不存在：{self.start_node}")
        for node in result.values():
            if node.next_node is not None and node.next_node not in result:
                raise WorkflowError(f"节点 {node.node_id} 的后继节点不存在：{node.next_node}")
        return result


@dataclass
class WorkflowRun:
    run_id: str
    workflow_id: str
    workflow_version: str
    input: Any
    data: dict[str, Any]
    outputs: dict[str, Any]
    agent_contexts: dict[str, Any]
    current_node: str | None
    status: str
    step_count: int = 0
    attempts: dict[str, int] = field(default_factory=dict)
    resume_data: Any = None
    waiting_for: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def snapshot(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "workflow_version": self.workflow_version,
            "input": _json_copy(self.input),
            "data": _json_copy(self.data),
            "outputs": _json_copy(self.outputs),
            "current_node": self.current_node,
            "status": self.status,
            "step_count": self.step_count,
            "waiting_for": self.waiting_for,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class CheckpointStore(Protocol):
    """Persistence contract hosts can adapt to their own database."""

    def get(self, run_id: str) -> WorkflowRun | None: ...

    def save(self, run: WorkflowRun, event: str,
             payload: Mapping[str, Any] | None = None) -> None: ...

    def events(self, run_id: str, *, after: int = 0,
               limit: int = 500) -> list[dict[str, Any]]: ...


class SQLiteCheckpointStore:
    """SQLite run snapshots and append-only lifecycle events."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._owners: ContextVar[dict[str, str]] = ContextVar("workflow_owners_" + uuid4().hex, default={})
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    run_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    workflow_version TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    outputs_json TEXT NOT NULL,
                    agent_contexts_json TEXT NOT NULL,
                    current_node TEXT,
                    status TEXT NOT NULL,
                    step_count INTEGER NOT NULL,
                    attempts_json TEXT NOT NULL,
                    resume_json TEXT,
                    waiting_for TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_events (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (run_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS workflow_leases (
                    run_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
            """)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def _connection(self):
        db = self._connect()
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, run_id: str) -> WorkflowRun | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        return WorkflowRun(
            run_id=row["run_id"], workflow_id=row["workflow_id"],
            workflow_version=row["workflow_version"], input=json.loads(row["input_json"]),
            data=json.loads(row["data_json"]), outputs=json.loads(row["outputs_json"]),
            agent_contexts=json.loads(row["agent_contexts_json"]),
            current_node=row["current_node"], status=row["status"],
            step_count=row["step_count"], attempts=json.loads(row["attempts_json"]),
            resume_data=json.loads(row["resume_json"]) if row["resume_json"] is not None else None,
            waiting_for=row["waiting_for"], error=row["error"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def save(self, run: WorkflowRun, event: str, payload: Mapping[str, Any] | None = None) -> None:
        run.updated_at = time.time()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            lease = db.execute("SELECT owner,expires_at FROM workflow_leases WHERE run_id=?",
                               (run.run_id,)).fetchone()
            owner = self._owners.get().get(run.run_id)
            if lease is not None and lease["expires_at"] > time.time() and lease["owner"] != owner:
                raise WorkflowError("该工作流正在由另一个执行器处理")
            if owner is not None and (lease is None or lease["owner"] != owner or lease["expires_at"] <= time.time()):
                raise WorkflowError("工作流执行租约已失效，不能覆盖检查点")
            db.execute("""
                INSERT INTO workflow_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET
                    workflow_id=excluded.workflow_id,
                    workflow_version=excluded.workflow_version,
                    input_json=excluded.input_json,
                    data_json=excluded.data_json,
                    outputs_json=excluded.outputs_json,
                    agent_contexts_json=excluded.agent_contexts_json,
                    current_node=excluded.current_node,
                    status=excluded.status,
                    step_count=excluded.step_count,
                    attempts_json=excluded.attempts_json,
                    resume_json=excluded.resume_json,
                    waiting_for=excluded.waiting_for,
                    error=excluded.error,
                    updated_at=excluded.updated_at
            """, (
                run.run_id, run.workflow_id, run.workflow_version,
                json.dumps(_json_copy(run.input), ensure_ascii=False, allow_nan=False),
                json.dumps(_json_copy(run.data), ensure_ascii=False, allow_nan=False),
                json.dumps(_json_copy(run.outputs), ensure_ascii=False, allow_nan=False),
                json.dumps(_json_copy(run.agent_contexts), ensure_ascii=False, allow_nan=False),
                run.current_node, run.status, run.step_count,
                json.dumps(_json_copy(run.attempts), ensure_ascii=False, allow_nan=False),
                json.dumps(_json_copy(run.resume_data), ensure_ascii=False, allow_nan=False)
                    if run.resume_data is not None else None,
                run.waiting_for, run.error, run.created_at, run.updated_at,
            ))
            sequence = db.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM workflow_events WHERE run_id=?",
                (run.run_id,),
            ).fetchone()[0]
            db.execute("INSERT INTO workflow_events VALUES(?,?,?,?,?)", (
                run.run_id, sequence, event,
                json.dumps(_json_copy(payload or {}), ensure_ascii=False, allow_nan=False),
                run.updated_at,
            ))

    @contextmanager
    def claim(self, run_id: str, *, lease_s: float = 60.0):
        """One cross-process writer, renewed even while a synchronous node runs.

        A terminated process leaves an expiring lease. Side effects still need
        the context's idempotency key: a lease cannot undo an external write.
        """
        owner = uuid4().hex
        duration = max(3.0, float(lease_s))
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            lease = db.execute("SELECT expires_at FROM workflow_leases WHERE run_id=?", (run_id,)).fetchone()
            if lease is not None and lease["expires_at"] > time.time():
                raise WorkflowError("该工作流正在执行；请查询已有运行，避免重复提交")
            db.execute("INSERT INTO workflow_leases VALUES(?,?,?) ON CONFLICT(run_id) DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at",
                       (run_id, owner, time.time() + duration))
        token = self._owners.set({**self._owners.get(), run_id: owner})
        stopped = threading.Event()

        def renew():
            while not stopped.wait(duration / 3):
                try:
                    with self._connection() as db:
                        changed = db.execute("UPDATE workflow_leases SET expires_at=? WHERE run_id=? AND owner=?",
                                             (time.time() + duration, run_id, owner)).rowcount
                    if not changed:
                        return
                except sqlite3.Error:
                    # A lost lease is detected by the next checkpoint write.
                    continue

        heartbeat = threading.Thread(target=renew, name="workflow-lease", daemon=True)
        heartbeat.start()
        try:
            yield
        finally:
            stopped.set()
            heartbeat.join(timeout=1)
            self._owners.reset(token)
            with self._connection() as db:
                db.execute("DELETE FROM workflow_leases WHERE run_id=? AND owner=?", (run_id, owner))

    def events(self, run_id: str, *, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute("""
                SELECT sequence,event,payload_json,created_at FROM workflow_events
                WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT ?
            """, (run_id, max(0, after), max(1, min(limit, 2000)))).fetchall()
        return [{"sequence": row["sequence"], "event": row["event"],
                 "payload": json.loads(row["payload_json"]), "created_at": row["created_at"]}
                for row in rows]


class WorkflowContext:
    """Mutable JSON data and node-scoped memory for one workflow invocation."""

    def __init__(self, run: WorkflowRun, node_id: str, attempt: int,
                 idempotency_key: str) -> None:
        self.run = run
        self.node_id = node_id
        self.attempt = attempt
        self.idempotency_key = idempotency_key
        self.input = run.input
        self.data = run.data
        self.outputs = run.outputs
        self.resume_data = run.resume_data

    def agent_context(self, key: str | None = None) -> dict[str, Any]:
        """Return persistent memory keyed by agent identity across loop visits."""
        identity = key or self.node_id
        value = self.run.agent_contexts.setdefault(identity, {})
        if not isinstance(value, dict):
            raise WorkflowError(f"Agent 上下文必须是 JSON 对象：{identity}")
        return value


class WorkflowEngine:
    """Execute registered nodes and checkpoint after every completed boundary.

    Calling run again with the same run_id resumes failed/interrupted work and
    returns completed/paused work as-is. resume() supplies a response to a
    paused node. A node being replayed after a crash receives a stable
    idempotency_key; side-effecting handlers should pass it to their service.
    """

    def __init__(self, store: CheckpointStore, *, default_max_steps: int = 1000) -> None:
        self.store = store
        self.default_max_steps = max(1, int(default_max_steps))
        self._locks: dict[str, asyncio.Lock] = {}
        self._active_tasks: dict[str, asyncio.Task[Any]] = {}
        self._cancel_requests: set[str] = set()

    async def run(self, workflow: Workflow, input_data: Any, *, run_id: str | None = None) -> WorkflowRun:
        nodes = workflow.node_map()
        clean_input = _json_copy(input_data)
        identity = run_id or uuid4().hex
        async with self._lock(identity), self._lease(identity):
            run = self.store.get(identity)
            if run is None:
                run = WorkflowRun(
                    run_id=identity, workflow_id=workflow.workflow_id,
                    workflow_version=workflow.version, input=clean_input,
                    data={}, outputs={}, agent_contexts={},
                    current_node=workflow.start_node, status="running",
                )
                self.store.save(run, "started", {"workflow_id": workflow.workflow_id})
            else:
                self._check_compatible(workflow, run, clean_input)
                if run.status in ("completed", "paused", "cancelled"):
                    return run
                run.status = "running"
                run.error = None
                self.store.save(run, "resumed")
            return await self._drive(workflow, nodes, run)

    async def resume(self, workflow: Workflow, run_id: str, *, response: Any = _UNSET) -> WorkflowRun:
        nodes = workflow.node_map()
        async with self._lock(run_id), self._lease(run_id):
            run = self.store.get(run_id)
            if run is None:
                raise WorkflowError(f"工作流运行不存在：{run_id}")
            self._check_compatible(workflow, run, run.input)
            if run.status == "completed":
                return run
            if run.status == "cancelled":
                raise WorkflowError("已取消的工作流不能恢复")
            if run.status == "paused" and response is _UNSET:
                raise WorkflowError("此工作流等待外部输入，请传入 response 后恢复")
            if response is not _UNSET:
                run.resume_data = _json_copy(response)
            run.status = "running"
            run.waiting_for = None
            run.error = None
            self.store.save(run, "resumed", {"has_response": response is not _UNSET})
            return await self._drive(workflow, nodes, run)

    def get(self, run_id: str) -> WorkflowRun | None:
        return self.store.get(run_id)

    def events(self, run_id: str, *, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        return self.store.events(run_id, after=after, limit=limit)

    async def cancel(self, run_id: str) -> bool:
        """Cancel an active run, or mark a saved nonterminal run cancelled."""
        task = self._active_tasks.get(run_id)
        if task is not None and not task.done():
            self._cancel_requests.add(run_id)
            task.cancel()
            return True
        async with self._lock(run_id), self._lease(run_id):
            run = self.store.get(run_id)
            if run is None or run.status in ("completed", "cancelled"):
                return False
            run.status = "cancelled"
            run.error = "工作流已取消。"
            self.store.save(run, "cancelled", {"node_id": run.current_node})
            return True

    def _lock(self, run_id: str) -> asyncio.Lock:
        return self._locks.setdefault(run_id, asyncio.Lock())

    def _lease(self, run_id: str):
        """Adapt a synchronous persistence lease to an async context manager."""
        engine = self
        class Lease:
            async def __aenter__(self):
                claim = getattr(engine.store, "claim", None)
                self.context = claim(run_id) if callable(claim) else nullcontext()
                self.context.__enter__()
            async def __aexit__(self, *exception):
                return self.context.__exit__(*exception)
        return Lease()

    @staticmethod
    def _check_compatible(workflow: Workflow, run: WorkflowRun, input_data: Any) -> None:
        if run.workflow_id != workflow.workflow_id or run.workflow_version != workflow.version:
            raise WorkflowError("工作流标识或版本与已保存运行不匹配")
        if _json_copy(run.input) != _json_copy(input_data):
            raise WorkflowError("相同 run_id 不能更换初始输入")

    async def _drive(self, workflow: Workflow, nodes: dict[str, WorkflowNode],
                     run: WorkflowRun) -> WorkflowRun:
        maximum = min(max(1, workflow.max_steps), self.default_max_steps)
        task = asyncio.current_task()
        if task is not None:
            self._active_tasks[run.run_id] = task
        try:
            while run.current_node is not None:
                if run.run_id in self._cancel_requests:
                    raise asyncio.CancelledError
                if sum(run.attempts.values()) >= maximum:
                    raise WorkflowError(f"工作流超过最大节点访问次数 {maximum}，可能存在未受控循环")
                node_id = run.current_node
                node = nodes.get(node_id)
                if node is None:
                    raise WorkflowError(f"当前节点不存在：{node_id}")
                attempt = run.attempts.get(node_id, 0) + 1
                run.attempts[node_id] = attempt
                provisional = deepcopy(run)
                context = WorkflowContext(
                    provisional, node_id, attempt,
                    f"{run.workflow_id}:{run.run_id}:{node_id}:{run.step_count}",
                )
                self.store.save(run, "node_started", {
                    "node_id": node_id, "kind": node.kind, "attempt": attempt,
                })
                value = node.handler(context)
                if inspect.isawaitable(value):
                    value = await value
                if run.run_id in self._cancel_requests:
                    raise asyncio.CancelledError
                result = value if isinstance(value, StepResult) else StepResult(output=value)
                result_output = _json_copy(result.output)
                updates = _json_copy(dict(result.update))
                if not isinstance(updates, dict):
                    raise WorkflowError("StepResult.update 必须是 JSON 对象")
                target = node.next_node if result.next_node is _UNSET else result.next_node
                if target is not None and (not isinstance(target, str) or target not in nodes):
                    raise WorkflowError(f"节点 {node_id} 跳转到不存在的节点：{target}")
                # Commit detached node memory only after all boundaries validate.
                clean_data = _json_copy(provisional.data)
                clean_contexts = _json_copy(provisional.agent_contexts)
                if not isinstance(clean_data, dict) or not isinstance(clean_contexts, dict):
                    raise WorkflowError("节点数据和 Agent 上下文必须为 JSON 对象")
                run.outputs[node_id] = result_output
                run.data = clean_data
                run.data.update(updates)
                run.agent_contexts = clean_contexts
                run.resume_data = None

                if result.wait_for_input:
                    run.status = "paused"
                    run.waiting_for = str(result.wait_for_input)[:1000]
                    self.store.save(run, "paused", {
                        "node_id": node_id, "prompt": run.waiting_for,
                    })
                    return run

                run.current_node = target
                run.step_count += 1
                run.status = "completed" if target is None else "running"
                self.store.save(run, "node_completed", {
                    "node_id": node_id, "next_node": target,
                    "kind": node.kind, "step_count": run.step_count,
                })
            if run.status != "completed":
                run.status = "completed"
                self.store.save(run, "completed")
            return run
        except asyncio.CancelledError:
            explicitly_cancelled = run.run_id in self._cancel_requests
            self._cancel_requests.discard(run.run_id)
            run.status = "cancelled" if explicitly_cancelled else "interrupted"
            run.error = ("工作流已取消。" if explicitly_cancelled else
                         "执行被中断；可以使用相同 run_id 恢复当前节点。")
            self.store.save(run, run.status, {"node_id": run.current_node})
            raise
        except Exception as exc:
            run.status = "failed"
            run.error = str(exc)[:4000]
            self.store.save(run, "failed", {
                "node_id": run.current_node,
                "error_type": type(exc).__name__, "error": run.error,
            })
            raise
        finally:
            self._cancel_requests.discard(run.run_id)
            if self._active_tasks.get(run.run_id) is task:
                self._active_tasks.pop(run.run_id, None)


def pydantic_agent_node(
    node_id: str,
    agent: Any,
    prompt: str | Callable[[WorkflowContext], str],
    *,
    next_node: str | None = None,
    agent_key: str | None = None,
    skills: Sequence[str] = (),
    deps: Any = _UNSET,
) -> WorkflowNode:
    """Adapt a PydanticAI Agent and persist its typed message history per node.

    Repeated visits to this node load the prior history from WorkflowRun and
    pass it back to Agent.run. PydanticAI remains an optional import for users
    who only use program nodes.
    """

    async def execute(context: WorkflowContext) -> StepResult:
        try:
            from pydantic_ai.messages import ModelMessagesTypeAdapter
        except ImportError as exc:
            raise WorkflowError("Agent 节点需要安装 pydantic-ai") from exc
        memory = context.agent_context(agent_key or node_id)
        saved = memory.get("message_history_json")
        history = ModelMessagesTypeAdapter.validate_json(saved) if saved else None
        instruction = prompt(context) if callable(prompt) else prompt
        arguments: dict[str, Any] = {
            "message_history": history,
            "conversation_id": f"{context.run.workflow_id}:{context.run.run_id}:{agent_key or node_id}",
        }
        if deps is not _UNSET:
            arguments["deps"] = deps(context) if callable(deps) else deps
        result = await agent.run(instruction, **arguments)
        memory["message_history_json"] = result.all_messages_json().decode("utf-8")
        return StepResult(output=result.output, next_node=next_node)

    return WorkflowNode(node_id=node_id, handler=execute, next_node=next_node,
                        kind="agent", skills=tuple(skills))
