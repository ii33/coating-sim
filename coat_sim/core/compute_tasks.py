"""Cross-process computation estimates, trusted release policies and compact queue events.

Only desktop code calls ``decide``. No MCP tool exposes an approval boolean.
The database contains receipts and bounded summaries, never simulation arrays.
"""
from __future__ import annotations

from contextlib import closing, contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from uuid import uuid4


def _now():
    return datetime.now(timezone.utc).isoformat()


def _queue_finished_recent(finished, reference):
    try:
        timestamp = datetime.fromisoformat(finished)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp >= reference - timedelta(hours=24)
    except (TypeError, ValueError):
        return False


def queue_job_is_recent(job, now=None):
    """Shared queue display rule; missing or invalid terminal times are history."""
    if job.get("state") in ("queued", "running"):
        return True
    if job.get("state") not in ("succeeded", "failed", "cancelled"):
        return False
    return _queue_finished_recent(job.get("finished_at"), now or datetime.now(timezone.utc))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode("utf-8")).hexdigest()


def _validate_client_request_id(client_request_id):
    if not isinstance(client_request_id, str) or not 1 <= len(client_request_id) <= 512:
        raise ValueError("计算请求标识须为1–512个字符")


def _compact_progress(value):
    """Keep measured scalar progress, without candidate geometry or film arrays."""
    if not isinstance(value, dict):
        return None
    text_keys = {
        "stage", "objective", "score_scope", "estimate_basis", "algorithm", "status",
        "actual_backend", "dtype",
        "estimate_kind",
    }
    number_keys = {
        "evaluations", "max_evaluations", "completed_evaluations", "total_evaluations",
        "verification_evaluations", "verification_max_evaluations",
        "group_index", "group_count", "source_slot", "search_grid_size", "verification_grid_size",
        "best_score", "best_search_score", "best_search_cv_percent", "best_verified_score",
        "best_verified_cv_percent", "full_recipe_cv_percent", "original_full_recipe_cv_percent",
        "elapsed_s", "estimated_remaining_s", "fraction", "step_index", "step_count",
        "completed_wavelengths", "total_wavelengths",
          "cpu_workers", "capacity", "forward_calls", "cached_forward_calls",
          "cache_hits", "prepared_builds", "cache_evictions", "fallback_calls",
          "failed_forward_calls", "cancelled_forward_calls", "forward_seconds",
          "prepare_seconds",
    }
    result = {}
    for key in text_keys:
        if isinstance(value.get(key), str):
            result[key] = value[key][:240]
    for key in number_keys:
        number = value.get(key)
        if isinstance(number, (int, float)) and not isinstance(number, bool) and math.isfinite(number):
            result[key] = number
    if type(value.get("accepted")) is bool:
        result["accepted"] = value["accepted"]
    return result or None


# Resource release is desktop-owned. Discussion never authorizes computation,
# even when an old receipt labels a short local task as immediate.
COMPUTE_POLICY_BLOCKERS = (
    "compute_discussion_read_only:", "compute_ui_authority_missing:",
    "compute_ui_mode_changed:", "compute_release_reconfirmation_required:",
)


def compute_release_policy(value, context):
    if context.get("external", False):
        return "manual", "external_manual_confirmation"
    if context.get("authority_error"):
        return "blocked", context["authority_error"]
    if context["mode"] == "chat" or context.get("current_mode") == "chat":
        return "blocked", "compute_discussion_read_only: 讨论模式只读；请切换操作模式并重新明确请求计算"
    if context.get("generation") != context.get("current_generation"):
        return "blocked", "compute_ui_mode_changed: 会话模式已切换；请在操作模式重新明确请求计算"
    return {"operate": "countdown", "research": "immediate"}[context["mode"]], "session_mode"


class ComputeTaskStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / "artifacts/runtime/compute-tasks.sqlite3"

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS estimates (
                  estimate_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                  state TEXT NOT NULL, created_at TEXT NOT NULL,
                  input_digest TEXT NOT NULL, job_id TEXT);
                CREATE TABLE IF NOT EXISTS queue_jobs (
                  job_id TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL,
                  created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS queue_events (
                  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  job_id TEXT NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL,
                  UNIQUE(job_id,type));
                CREATE TABLE IF NOT EXISTS request_aliases (
                  client_request_id TEXT PRIMARY KEY, estimate_id TEXT NOT NULL,
                  input_digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS execution_lease (
                  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                  owner TEXT, expires_at REAL NOT NULL DEFAULT 0);
                INSERT OR IGNORE INTO execution_lease(singleton) VALUES(1);
            """)
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_estimate(self, kind, parameters, estimate, input_files):
        identity = {"kind": kind, "parameters": parameters, "input_files": input_files}
        payload = {**estimate, "estimate_id": uuid4().hex, "kind": kind,
                   "parameters": parameters, "input_files": input_files,
                   "input_digest": digest(identity), "state": "estimated",
                   "created_at": _now(), "origin": {}}
        with self.connection() as connection:
            connection.execute("INSERT INTO estimates VALUES(?,?,?,?,?,NULL)", (
                payload["estimate_id"], json.dumps(payload, ensure_ascii=False),
                "estimated", payload["created_at"], payload["input_digest"]))
        return self.public_estimate(payload)

    @staticmethod
    def public_estimate(payload):
        return {key: value for key, value in payload.items()
                if key not in ("parameters", "input_files")}

    def read_estimate(self, estimate_id):
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM estimates WHERE estimate_id=?", (estimate_id,)).fetchone()
        if row is None:
            raise ValueError("compute_estimate_not_found: 计算估算不存在")
        payload = json.loads(row["payload"])
        payload.update(state=row["state"], job_id=row["job_id"])
        return payload

    def _origin_context(self, origin, *, authority_connection=None, authority_database=None, check_stopped=True):
        """Read UI-owned authority, never a model's mode or progress metadata."""
        session = str(origin.get("session_id") or "")
        request = str(origin.get("request_id") or "")
        if not session and not request:
            return {"mode": "chat", "external": True}
        if not session or not request:
            raise ValueError("compute_origin_invalid: incomplete request origin")
        database = Path(authority_database).resolve() if authority_database is not None else self.root / "artifacts/assistant/state.sqlite3"
        if not database.is_file():
            raise ValueError("compute_origin_expired: original conversation no longer exists")
        def read(connection):
            if not connection.execute("SELECT 1 FROM conversations WHERE id=?", (session,)).fetchone():
                raise ValueError("compute_origin_expired: original conversation no longer exists")
            if check_stopped:
                rows = connection.execute("SELECT event,payload FROM task_events WHERE session=? AND request_id=? "
                    "ORDER BY id DESC LIMIT 80", (session, request)).fetchall()
                messages = connection.execute("SELECT metadata FROM messages WHERE session=? AND request_id=? "
                    "AND role='assistant'", (session, request)).fetchall()
                for row in rows:
                    try:
                        payload = json.loads(row["payload"])
                    except (TypeError, ValueError):
                        continue
                    if row["event"] == "cancelled" or (isinstance(payload, dict) and
                            payload.get("state") in ("cancelling", "cancelled", "interrupted")):
                        raise ValueError("compute_request_stopped: original request was stopped by the user")
                for row in messages:
                    try:
                        metadata = json.loads(row["metadata"])
                    except (TypeError, ValueError):
                        continue
                    if isinstance(metadata, dict) and metadata.get("status") in ("cancelled", "interrupted"):
                        raise ValueError("compute_request_stopped: original request was stopped by the user")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            authority = current = None
            if {"assistant_ui_requests", "assistant_ui_session_modes"} <= tables:
                authority = connection.execute("SELECT mode,generation FROM assistant_ui_requests "
                    "WHERE session=? AND request_id=?", (session, request)).fetchone()
                current = connection.execute("SELECT mode,generation FROM assistant_ui_session_modes "
                    "WHERE session=?", (session,)).fetchone()
            if authority is None or current is None:
                # Old received events are useful history, never authorization.
                return {"mode": "chat", "external": False, "authority_error":
                    "compute_ui_authority_missing: 历史请求缺少可信 UI 模式记录；请在操作模式重新明确请求计算"}
            if authority["mode"] not in ("chat", "operate", "research") or current["mode"] not in ("chat", "operate", "research"):
                raise ValueError("compute_origin_invalid: invalid UI authority mode")
            if any(type(row["generation"]) is not int or row["generation"] < 1 for row in (authority, current)):
                raise ValueError("compute_origin_invalid: invalid UI authority generation")
            return {"mode": authority["mode"], "generation": authority["generation"],
                    "current_mode": current["mode"], "current_generation": current["generation"], "external": False}
        if authority_connection is not None:
            return read(authority_connection)
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            return read(connection)

    def assistant_dispatch_authority(self, session_id, request_id, *, database=None, check_stopped=True):
        """Read-only dynamic gate for runtime mutations; never creates a DB/table.

        Root is the project root, or ``STATE_DIRECTORY.parent.parent`` for the
        shared assistant state directory. Missing provenance fails closed.
        """
        if not session_id or not request_id:
            return {"allowed": False, "error": "compute_ui_authority_missing: 缺少可信 UI 请求来源"}
        try:
            context = self._origin_context({"session_id": session_id, "request_id": request_id},
                authority_database=database, check_stopped=check_stopped)
            policy, reason = compute_release_policy({}, context)
            return {**context, "allowed": policy != "blocked", "error": reason if policy == "blocked" else ""}
        except (ValueError, OSError, sqlite3.Error) as exc:
            return {"allowed": False, "error": str(exc)}

    def _validate_pending(self, value, *, authority_connection=None):
        context = self._origin_context(value.get("origin", {}), authority_connection=authority_connection)
        policy, reason = compute_release_policy(value, context)
        if policy == "blocked":
            raise ValueError(reason)
        if value.get("release_policy") == "blocked" or value.get("requires_new_request"):
            raise ValueError("compute_release_reconfirmation_required: 此旧请求已暂停；请在操作模式重新明确请求计算")
        if value.get("request_mode", "chat") != context["mode"]:
            raise ValueError("compute_ui_mode_changed: persisted request mode no longer matches this receipt")
        if not context.get("external") and value.get("request_generation") != context.get("generation"):
            raise ValueError("compute_ui_authority_missing: 此旧确认缺少可信 UI 模式代际；请重新明确请求计算")
        expires = value.get("expires_at")
        if value.get("state") == "approved" and expires and datetime.now(timezone.utc) >= datetime.fromisoformat(expires):
            raise ValueError("compute_confirmation_expired: estimate release window has expired")
        return context

    def block_release(self, estimate_id, reason):
        """Retain a visible receipt and its inputs; never cancel an existing job."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload,state FROM estimates WHERE estimate_id=?", (estimate_id,)).fetchone()
            if row is None or row["state"] not in ("pending", "approved"):
                return
            value = json.loads(row["payload"])
            reason = str(reason)[:500]
            if (row["state"] == "pending" and value.get("release_policy") == "blocked"
                    and value.get("release_error") == reason and value.get("requires_new_request")
                    and value.get("release_at") is None):
                return
            value.update(state="pending", release_policy="blocked", release_at=None,
                         release_error=reason, requires_new_request=True)
            connection.execute("UPDATE estimates SET state='pending',payload=? WHERE estimate_id=?",
                               (json.dumps(value, ensure_ascii=False), estimate_id))

    def _reject_release(self, estimate_id, error):
        if str(error).startswith(COMPUTE_POLICY_BLOCKERS):
            self.block_release(estimate_id, str(error))
        else:
            self.invalidate(estimate_id, str(error))

    @contextmanager
    def submission_authority(self, estimate_id):
        """Serialize final assistant enqueue with UI mode changes, including WAL.

        Lock order is assistant authority BEGIN IMMEDIATE, then queue write.
        UI mode changes only lock the assistant DB. A submit that wins this lock
        is an existing task before the mode transition; later work is blocked.
        """
        value = self.read_estimate(estimate_id)
        if value["state"] == "submitted" or not value.get("origin", {}).get("session_id"):
            yield None
            return
        database = self.root / "artifacts/assistant/state.sqlite3"
        if not database.is_file():
            raise ValueError("compute_origin_expired: original conversation no longer exists")
        with closing(sqlite3.connect(database, timeout=15)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def invalidate(self, estimate_id, reason):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload,state FROM estimates WHERE estimate_id=?", (estimate_id,)).fetchone()
            if row is None or row["state"] not in ("estimated", "pending", "approved"):
                return
            value = json.loads(row["payload"])
            value.update(state="expired", release_error=str(reason)[:300])
            connection.execute("UPDATE estimates SET state='expired',payload=? WHERE estimate_id=?",
                               (json.dumps(value, ensure_ascii=False), estimate_id))

    def request(self, estimate_id, *, session_id="", request_id=""):
        origin = {"session_id": session_id, "request_id": request_id}
        context = self._origin_context(origin)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM estimates WHERE estimate_id=?", (estimate_id,)).fetchone()
            if row is None:
                raise ValueError("compute_estimate_not_found: estimate does not exist")
            value = json.loads(row["payload"])
            if row["state"] == "estimated":
                now = datetime.now(timezone.utc)
                policy, policy_reason = compute_release_policy(value, context)
                value.update(origin=origin, state="pending", request_mode=context["mode"],
                             release_policy=policy, release_reason=policy_reason, requested_at=now.isoformat(),
                             request_generation=context.get("generation"),
                             release_at=(now + timedelta(seconds=10)).isoformat() if policy == "countdown" else None,
                             expires_at=(now + timedelta(minutes=15)).isoformat())
                connection.execute("UPDATE estimates SET state='pending',payload=? WHERE estimate_id=?",
                                   (json.dumps(value, ensure_ascii=False), estimate_id))
            else:
                value.update(state=row["state"], job_id=row["job_id"])
                if row["state"] not in ("pending", "approved", "submitted"):
                    raise ValueError("compute_confirmation_not_pending: release was declined or expired")
                if value.get("origin", {}) != origin:
                    raise ValueError("compute_origin_conflict: release request belongs to another origin")
        if value["state"] in ("pending", "approved"):
            try:
                self._validate_pending(value)
            except ValueError as exc:
                self._reject_release(estimate_id, exc)
                if str(exc).startswith(COMPUTE_POLICY_BLOCKERS):
                    return self.public_estimate(self.read_estimate(estimate_id))
                raise
        return self.public_estimate(value)

    def decide(self, estimate_id, accepted, *, automatic=False):
        """Trusted desktop callback; approval and UI mode changes are serialized."""
        if type(accepted) is not bool:
            raise ValueError("confirmation decision must be boolean")
        authority_scope = self.submission_authority(estimate_id) if accepted else nullcontext(None)
        try:
            with authority_scope as authority, self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT payload,state,job_id FROM estimates WHERE estimate_id=?",
                                         (estimate_id,)).fetchone()
                if row is None:
                    raise ValueError("compute_estimate_not_found: estimate does not exist")
                if row["state"] not in ("pending", "approved", "submitted"):
                    raise ValueError("compute_confirmation_not_pending: request was rejected, expired or not requested")
                if row["state"] != "submitted":
                    # Re-read the complete receipt under the queue lock. An
                    # earlier snapshot may already have preserved a rejection;
                    # stale caller data must never erase those flags or errors.
                    value = {**json.loads(row["payload"]), "state": row["state"], "job_id": row["job_id"]}
                    if accepted:
                        self._validate_pending(value, authority_connection=authority)
                        if automatic:
                            policy = value.get("release_policy", "manual")
                            due = value.get("release_at")
                            if policy not in ("countdown", "immediate"):
                                raise ValueError("compute_manual_confirmation_required: explicit desktop confirmation is required")
                            if policy == "countdown" and (not due or datetime.now(timezone.utc) < datetime.fromisoformat(due)):
                                raise ValueError("compute_release_not_due: countdown has not elapsed")
                        if row["state"] == "pending":
                            released = datetime.now(timezone.utc)
                            value.update(approved_at=released.isoformat(),
                                         expires_at=(released + timedelta(minutes=15)).isoformat())
                            connection.execute("UPDATE estimates SET state='approved',payload=? WHERE estimate_id=?",
                                               (json.dumps(value, ensure_ascii=False), estimate_id))
                    else:
                        connection.execute("UPDATE estimates SET state='declined' WHERE estimate_id=?", (estimate_id,))
        except ValueError as exc:
            if not str(exc).startswith(("compute_release_not_due:", "compute_manual_confirmation_required:",
                                       "compute_confirmation_not_pending:", "compute_estimate_not_found:")):
                self._reject_release(estimate_id, exc)
            raise
        return self.public_estimate(self.read_estimate(estimate_id))

    def authorize(self, estimate_id, kind, parameters, input_files):
        value = self.read_estimate(estimate_id)
        if value["input_digest"] != digest({"kind": kind, "parameters": parameters, "input_files": input_files}):
            self.invalidate(estimate_id, "compute_estimate_changed")
            raise ValueError("compute_estimate_changed: parameters or files changed; request a new estimate")
        if value["state"] not in ("approved", "submitted"):
            raise ValueError("compute_confirmation_required: release has not been approved")
        if value["state"] != "submitted":
            try:
                self._validate_pending(value)
            except ValueError as exc:
                self._reject_release(estimate_id, exc)
                raise
        return value

    def bind_request(self, client_request_id, estimate_id):
        if client_request_id is None:
            return estimate_id
        _validate_client_request_id(client_request_id)
        value = self.read_estimate(estimate_id)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute("SELECT * FROM request_aliases WHERE client_request_id=?", (client_request_id,)).fetchone()
            if prior is not None:
                if prior["input_digest"] != value["input_digest"]:
                    raise ValueError("request_id_conflict: 该请求标识已用于另一组计算输入")
                return prior["estimate_id"]
            connection.execute("INSERT INTO request_aliases VALUES(?,?,?)", (client_request_id, estimate_id, value["input_digest"]))
        return estimate_id

    def claim_unbound_estimate(self, client_request_id: str, input_digest: str) -> str | None:
        """Atomically bind one exact, unbound estimate without granting release."""
        _validate_client_request_id(client_request_id)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute("SELECT * FROM request_aliases WHERE client_request_id=?",
                                       (client_request_id,)).fetchone()
            if prior is not None:
                if prior["input_digest"] != input_digest:
                    raise ValueError("request_id_conflict: 该请求标识已用于另一组计算输入")
                return prior["estimate_id"]
            row = connection.execute(
                "SELECT estimates.estimate_id FROM estimates WHERE state='estimated' "
                "AND input_digest=? AND NOT EXISTS (SELECT 1 FROM request_aliases "
                "WHERE request_aliases.estimate_id=estimates.estimate_id) "
                "ORDER BY created_at DESC LIMIT 1", (input_digest,)).fetchone()
            if row is None:
                return None
            connection.execute("INSERT INTO request_aliases VALUES(?,?,?)",
                               (client_request_id, row["estimate_id"], input_digest))
            return row["estimate_id"]

    def find_request(self, client_request_id):
        with self.connection() as connection:
            row = connection.execute("SELECT estimate_id FROM request_aliases WHERE client_request_id=?", (client_request_id,)).fetchone()
        return self.read_estimate(row["estimate_id"]) if row is not None else None

    def attach_job(self, estimate_id, job):
        estimate = self.read_estimate(estimate_id)
        payload = {"job_id": job["job_id"], "kind": job["kind"], "state": job["state"],
                   "summary": estimate["summary"], "estimated_s": estimate.get("estimated_s"),
                   "memory_estimate_label": estimate.get("memory_estimate_label", ""),
                   "memory_warning": estimate.get("memory_warning", False),
                   "origin": estimate.get("origin", {}), "created_at": job["created_at"],
                   "started_at": job.get("started_at"), "finished_at": job.get("finished_at"),
                   "progress": _compact_progress(job.get("progress")),
                   "estimate_id": estimate_id, "_owner_pid": job.get("_owner_pid"),
                   "_owner_start": job.get("_owner_start")}
        self._validate_pending(estimate)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT job_id,state FROM estimates WHERE estimate_id=?", (estimate_id,)).fetchone()
            if current is None or current["state"] not in ("approved", "submitted"):
                raise ValueError("compute_release_revoked: release was cancelled before queue attachment")
            if current["job_id"] and current["job_id"] != job["job_id"]:
                raise ValueError("compute_estimate_already_submitted: 此确认已用于另一项任务")
            connection.execute("UPDATE estimates SET state='submitted',job_id=? WHERE estimate_id=?", (job["job_id"], estimate_id))
            # Retried attachment is registration, not a state update. Preserve
            # both insertion order and any progress/result already persisted.
            connection.execute("INSERT INTO queue_jobs VALUES(?,?,?,?) ON CONFLICT(job_id) DO NOTHING", (
                job["job_id"], json.dumps(payload, ensure_ascii=False), job["state"], job["created_at"]))
            attached = connection.execute("SELECT payload,state FROM queue_jobs WHERE job_id=?", (job["job_id"],)).fetchone()
            payload = {**json.loads(attached["payload"]), "state": attached["state"]}
            if payload.get("estimate_id") != estimate_id:
                raise ValueError("compute_job_identity_conflict: 任务已属于另一项计算确认")
            self._event(connection, payload, "enqueued")
        return payload

    def register_desktop_job(self, job_id, *, summary, estimated_s=None, memory_estimate=None):
        """Register an explicitly clicked desktop calculation in the shared FIFO."""
        import os
        from .process_identity import _process_identity
        value = {"job_id": job_id, "kind": "process_recipe", "state": "queued",
                 "summary": summary, "estimated_s": estimated_s, "created_at": _now(),
                 "memory_estimate_label": (memory_estimate or {}).get("memory_estimate_label", ""),
                 "memory_warning": (memory_estimate or {}).get("memory_warning", False),
                 "started_at": None, "finished_at": None, "origin": {"surface": "desktop"},
                 "_owner_pid": os.getpid(), "_owner_start": _process_identity(os.getpid())[1],
                 "progress": {"fraction": 0, "stage": "queued"}}
        with self.connection() as connection:
            connection.execute("INSERT INTO queue_jobs VALUES(?,?,?,?)",
                (job_id, json.dumps(value, ensure_ascii=False), "queued", value["created_at"]))
            self._event(connection, value, "enqueued")
        return value

    def read_job(self, job_id):
        with self.connection() as connection:
            row = connection.execute("SELECT payload,state FROM queue_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("计算任务不存在")
        return {**json.loads(row["payload"]), "state": row["state"]}

    def result_asset_path(self, job_id):
        """Resolve only a finished job's persisted result; never submit work."""
        import re
        value = self.read_job(job_id)
        if value["state"] != "succeeded":
            raise ValueError("任务尚未成功完成，没有可打开的结果")
        result = value.get("result") or {}
        asset = result.get("asset") or {}
        run_id = asset.get("run_id") or result.get("run_id")
        if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise ValueError("此任务没有已持久化的仿真结果资产；不会重新计算")
        # Resolve from the immutable run identity, not editable current inputs.
        candidate = self.root / "data/tmp/simulations" / run_id / "result.coatasset"
        if candidate.is_file():
            return candidate
        # Saved copies can outlive temporary output; use the recorded asset path.
        database = self.root / "data/simulation_history.sqlite3"
        if database.is_file():
            with sqlite3.connect(database) as connection:
                saved = connection.execute("SELECT asset_path FROM simulations WHERE run_id=?", (run_id,)).fetchone()
            if saved and saved[0] and Path(saved[0]).is_file():
                return Path(saved[0])
        raise ValueError("已完成结果资产已移动或删除；不会重新计算，请重新定位已保存的资产包")

    def request_cancel(self, job_id):
        """Use the same cancellation marker consumed by desktop and MCP workers."""
        with self.connection() as connection:
            row = connection.execute("SELECT state FROM queue_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None or row["state"] not in ("queued", "running"):
            return False
        if not isinstance(job_id, str) or not job_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in job_id):
            raise ValueError("invalid job id")
        marker = self.root / "artifacts/runtime/jobs" / (job_id + ".cancel")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch(exist_ok=True)
        return True

    @staticmethod
    def _event(connection, payload, kind):
        event = {**{key: value for key, value in payload.items() if not key.startswith("_")}, "type": kind}
        connection.execute("INSERT OR IGNORE INTO queue_events(job_id,type,payload) VALUES(?,?,?)",
                           (payload["job_id"], kind, json.dumps(event, ensure_ascii=False)))

    def update_job(self, job):
        if not isinstance(job, dict) or not job.get("job_id") or job.get("state") not in ("queued", "running", "succeeded", "failed", "cancelled"):
            raise ValueError("invalid_compute_job_record: queue update requires job_id and job state")
        with self.connection() as connection:
            row = connection.execute("SELECT payload FROM queue_jobs WHERE job_id=?", (job["job_id"],)).fetchone()
            if row is None:
                return
            payload = json.loads(row["payload"])
            payload.update({key: job[key] for key in ("state", "started_at", "finished_at") if key in job})
            if payload.get("origin", {}).get("surface") == "desktop":
                if job.get("kind") in ("process_recipe", "mask_optimization"):
                    payload["kind"] = job["kind"]
                if isinstance(job.get("input_summary"), dict):
                    # Frozen, small typed manifest; never consult current filenames.
                    payload["input_summary"] = job["input_summary"]
            if isinstance(job.get("result"), dict):
                result = job["result"]
                payload["result"] = (result if job.get("kind") in ("process_recipe", "mask_optimization") else
                                     {"run_id": result.get("run_id"), "available": True})
            if "progress" in job:
                incoming = _compact_progress(job["progress"]) or {}
                previous = payload.get("progress") or {}
                if "fraction" in incoming:
                    incoming["fraction"] = max(previous.get("fraction", 0), min(1, max(0, incoming["fraction"])))
                payload["progress"] = incoming
            if job.get("error"):
                payload["error"] = str(job["error"])[:1000]
            connection.execute("UPDATE queue_jobs SET payload=?,state=? WHERE job_id=?", (
                json.dumps(payload, ensure_ascii=False), job["state"], job["job_id"]))
            if job["state"] in ("succeeded", "failed", "cancelled"):
                self._event(connection, payload, "completed")

    def acquire_execution(self, owner, *, ttl_s=30):
        import time
        now = time.time()
        self._repair_dead_owners()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM execution_lease WHERE singleton=1").fetchone()
            active = connection.execute("SELECT job_id FROM queue_jobs WHERE state IN ('queued','running') ORDER BY rowid").fetchall()
            active_ids = [job["job_id"] for job in active]
            if owner not in active_ids:
                return False
            # An existing live owner may have been selected by the legacy
            # timestamp scheduler. Preserve it across upgrades and clock jumps.
            # Expiry alone is not proof that its native child stopped: allowing
            # takeover could run two GPU workers together. Dead process repair
            # above, a terminal job state, or explicit release ends ownership.
            if row["owner"] in active_ids:
                if row["owner"] != owner:
                    return False
            # New selection uses committed insertion order, not wall clocks or
            # timestamps captured before a delayed concurrent registration.
            elif active_ids[0] != owner:
                return False
            connection.execute("UPDATE execution_lease SET owner=?,expires_at=? WHERE singleton=1", (owner, now + ttl_s))
        return True

    def release_execution(self, owner):
        with self.connection() as connection:
            connection.execute("UPDATE execution_lease SET owner=NULL,expires_at=0 WHERE singleton=1 AND owner=?", (owner,))

    def _repair_dead_owners(self):
        from .process_identity import _process_identity
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("SELECT payload FROM queue_jobs WHERE state IN ('queued','running')").fetchall()
            for row in rows:
                value = json.loads(row["payload"])
                if value.get("scheduler_managed"):
                    continue
                state, creation = _process_identity(value.get("_owner_pid"))
                stored = value.get("_owner_start")
                dead = state == "dead" or (state == "alive" and stored and creation and stored != creation)
                if not dead:
                    continue
                value.update(state="failed", finished_at=_now(), interrupted=True,
                             error="owner_exited: 计算服务已退出；保留任务记录，不自动重启")
                connection.execute("UPDATE queue_jobs SET payload=?,state='failed' WHERE job_id=? AND state IN ('queued','running')",
                                   (json.dumps(value, ensure_ascii=False), value["job_id"]))
                self._event(connection, value, "completed")

    def snapshot(self, *, after_event=0, limit=100):
        self._repair_dead_owners()
        limit = min(100, max(1, int(limit)))
        with self.connection() as connection:
            rows = connection.execute("SELECT rowid AS queue_order,payload FROM queue_jobs WHERE state IN ('queued','running') "
                "OR job_id IN (SELECT job_id FROM queue_jobs ORDER BY created_at DESC LIMIT ?) ORDER BY created_at DESC", (limit,)).fetchall()
            # Blocked receipts are durable history, not a permanent barrier in
            # front of new executable prompts. Fresh pending requests outrank
            # stale approved records that have not yet been revalidated.
            pending = connection.execute("SELECT payload,state FROM estimates WHERE state IN ('pending','approved') "
                "ORDER BY CASE WHEN json_extract(payload,'$.release_policy')='blocked' THEN 2 "
                "WHEN state='pending' THEN 0 ELSE 1 END, rowid DESC LIMIT 16").fetchall()
            lease_owner = connection.execute("SELECT owner FROM execution_lease WHERE singleton=1").fetchone()[0]
            latest = connection.execute("SELECT COALESCE(MAX(event_id),0) FROM queue_events").fetchone()[0]
            events = connection.execute("SELECT event_id,payload FROM queue_events WHERE event_id>? ORDER BY event_id LIMIT ?", (int(after_event), limit)).fetchall()
        valid_pending = []
        for row in pending:
            value = {**json.loads(row["payload"]), "state": row["state"]}
            try:
                self._validate_pending(value)
            except ValueError as exc:
                self._reject_release(value["estimate_id"], exc)
                if str(exc).startswith(COMPUTE_POLICY_BLOCKERS):
                    valid_pending.append(self.public_estimate(self.read_estimate(value["estimate_id"])))
            else:
                if value["state"] == "pending":
                    valid_pending.append(self.public_estimate(value))
        jobs = [{**{key: value for key, value in json.loads(row["payload"]).items() if not key.startswith("_")}, "queue_order": row["queue_order"]} for row in rows]
        order = {json.loads(row["payload"])["job_id"]: row["queue_order"] for row in rows}
        def queue_order(item):
            return (item["job_id"] != lease_owner, order[item["job_id"]])
        expected = datetime.now(timezone.utc)
        for job in sorted(jobs, key=queue_order):
            seconds = job.get("estimated_s")
            progress_remaining = (job.get("progress") or {}).get("estimated_remaining_s")
            measured_remaining = (job["state"] == "running" and
                                  isinstance(progress_remaining, (int, float)) and
                                  math.isfinite(progress_remaining))
            if measured_remaining:
                seconds = max(0.0, progress_remaining)
            if job["state"] in ("succeeded", "failed", "cancelled"):
                job["expected_finish_at"] = job.get("finished_at")
                continue
            if seconds is None or not math.isfinite(seconds) or expected is None:
                expected = None
            else:
                remaining = seconds
                if job["state"] == "running" and job.get("started_at") and not measured_remaining:
                    remaining = max(0, seconds - (datetime.now(timezone.utc) - datetime.fromisoformat(job["started_at"])).total_seconds())
                try:
                    expected += timedelta(seconds=remaining)
                except (OverflowError, ValueError):
                    expected = None
            job["expected_finish_at"] = expected.isoformat() if expected is not None else None
        event_values = [{**json.loads(row["payload"]), "event_id": row["event_id"]} for row in events]
        active = sorted((job for job in jobs if job["state"] in ("queued", "running")),
                        key=queue_order)
        recent = [job for job in jobs if job["state"] not in ("queued", "running")]
        # Keep every active task visible even when history exceeds the display limit.
        display_jobs = active + recent[:max(0, limit - len(active))]
        return {"jobs": display_jobs, "confirmations": valid_pending,
                "events": event_values, "latest_event_id": latest, "cursor": event_values[-1]["event_id"] if event_values else int(after_event)}

    def queue_jobs_page(self, bucket, *, before=None, limit=50, now=None):
        """Read one stable, newest-first page from the shared desktop job queue.

        ``recent`` contains every active job and terminal jobs finished in the
        preceding 24 hours. ``history`` contains older terminal jobs. The
        monotonically increasing SQLite rowid is the cursor, so pagination
        reads the complete queue without materializing the whole database.
        The optional ``now`` is only for deterministic callers and tests.
        """
        if bucket not in ("recent", "history"):
            raise ValueError("queue bucket must be 'recent' or 'history'")
        limit = min(100, max(1, int(limit)))
        reference = now or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        self._repair_dead_owners()
        if bucket == "recent":
            predicate = "(state IN ('queued','running') OR " \
                        "(state IN ('succeeded','failed','cancelled') AND " \
                        "queue_finished_recent(json_extract(payload,'$.finished_at')) = 1))"
        else:
            predicate = "(state IN ('succeeded','failed','cancelled') AND " \
                        "queue_finished_recent(json_extract(payload,'$.finished_at')) = 0)"
        params = []
        if before is not None:
            try:
                before = int(before)
            except (TypeError, ValueError) as exc:
                raise ValueError("queue page cursor must be an integer") from exc
            predicate += " AND rowid < ?"
            params.append(before)
        params.append(limit + 1)
        with self.connection() as connection:
            # SQLite julianday rounds sub-millisecond boundaries. Use the same
            # ISO timestamp parser as the view, including offsets and invalids.
            connection.create_function("queue_finished_recent", 1,
                lambda finished: int(_queue_finished_recent(finished, reference)))
            rows = connection.execute(
                "SELECT rowid AS queue_order,payload FROM queue_jobs WHERE " + predicate +
                " ORDER BY rowid DESC LIMIT ?", params).fetchall()
            active_rows = connection.execute(
                "SELECT rowid AS queue_order,payload FROM queue_jobs "
                "WHERE state IN ('queued','running') ORDER BY rowid"
            ).fetchall() if bucket == "recent" and before is None else []
        has_more = len(rows) > limit
        rows = rows[:limit]
        def public_job(row):
            job = {key: value for key, value in json.loads(row["payload"]).items()
                   if not key.startswith("_")}
            job["queue_order"] = row["queue_order"]
            return job
        next_cursor = rows[-1]["queue_order"] if has_more and rows else None
        return {"jobs": [public_job(row) for row in rows],
                "active_jobs": [public_job(row) for row in active_rows],
                "as_of": reference.isoformat(),
                "has_more": has_more, "next_cursor": next_cursor}
