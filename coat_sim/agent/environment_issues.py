"""Passive, bounded development issue records from actual failures.

This module calls no model or tool and never enables the development gate. The
SQLite ledger keeps deduplicated records; JSON/Markdown are recent read views.
Failures in this optional recorder cannot turn a completed action into a failure.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit, urlunsplit

from coat_sim.agent.development import development_enabled
from coat_sim.core.data_files import data_path
from coat_sim.core.atomic_io import replace_with_retry

LOG_BASE = "data/tmp/ENVIRONMENT_ISSUES"
RECENT_LIMIT = 200
MAX_DETAIL = 3000
MAX_RECOVERY = 1000
_LAST_RECORDING_ERROR = ""
_TOOL_ALIASES = {"previewFile": "preview_file", "openFile": "open_file"}
_PREVIEW_TOOLS = {"preview_file", "open_file", "preview_svg"}
_FILE_READ_TOOLS = {"read_data_file", "read_design", "read_calculation_result", "developer_files"}
_SCRIPT_TOOLS = {"run_python_script"}
_DATABASE_SCHEMA = """CREATE TABLE IF NOT EXISTS issues (
    fingerprint TEXT PRIMARY KEY, category TEXT NOT NULL, environment INTEGER NOT NULL,
    title TEXT NOT NULL, error_type TEXT NOT NULL, detail TEXT NOT NULL,
    recovery TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
    count INTEGER NOT NULL, first_context TEXT NOT NULL, last_context TEXT NOT NULL,
    last_success_at TEXT, last_success_context TEXT, recovery_observation TEXT);
CREATE TABLE IF NOT EXISTS occurrences (
    identity TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, seen_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS issue_readers (
    consumer TEXT NOT NULL, fingerprint TEXT NOT NULL, observed_at TEXT NOT NULL,
    PRIMARY KEY(consumer, fingerprint));"""


def enabled(config: Mapping[str, Any] | None = None) -> bool:
    """Require the saved local gate even when a request claims development mode."""
    return development_enabled() and (config is None or development_enabled(dict(config)))


def _redact(text: str, redact: Callable[[str], str] | None = None) -> str:
    value = redact(text) if redact else text
    value = re.sub(r"(?i)\b(?:sk|ak)-[A-Za-z0-9_-]{8,}", "[凭据已隐藏]", value)
    value = re.sub(r"(?i)\bBearer\s+[^\s\"'<>]+", "Bearer [凭据已隐藏]", value)
    value = re.sub(r"(?i)([\"']?(?:api[_ -]?key|access[_ -]?token|secret|password|authorization)[\"']?"
                   r"\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)",
                   r"\1[凭据已隐藏]", value)
    def safe_url(match: re.Match[str]) -> str:
        try:
            parsed = urlsplit(match.group(0))
            host = parsed.netloc.rsplit("@", 1)[-1]
            return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        except ValueError:
            return "[服务器地址已隐藏]"
    return re.sub(r"https?://[^\s\"'<>]+", safe_url, value)


def _error_text(error: Any, depth: int = 0) -> str:
    # Never serialize a whole tool return: it may include source or prompts.
    if depth > 3:
        return ""
    if isinstance(error, BaseException):
        nested = getattr(error, "exceptions", ())
        if isinstance(nested, tuple) and nested:
            return "\n".join(type(item).__name__ + ": " + _error_text(item, depth + 1)
                             for item in nested[:4])
        return str(error) or type(error).__name__ + "（异常未提供错误文字）"
    if isinstance(error, str):
        return error
    if isinstance(error, Mapping):
        parts = [_error_text(error[key], depth + 1) for key in
                 ("error", "message", "detail", "reason") if key in error]
        return "\n".join(part for part in parts if part)
    return ""


def _detail(error: Any, redact: Callable[[str], str] | None = None) -> str:
    value = _redact(_error_text(error)[-16000:], redact)
    lines = value.splitlines()
    if (any("Traceback (most recent call last)" in line for line in lines)
            or (any(re.match(r"\s*File \"", line) for line in lines)
                and any(re.match(r"[A-Za-z_][\w.]*(?:Error|Exception):", line) for line in lines))):
        # Keep file/line references and exception summaries, not traceback code.
        selected = [line.strip() for line in lines if re.match(r"\s*File \"", line)
                    or re.match(r"[A-Za-z_][\w.]*(?:Error|Exception):", line)]
        value = "\n".join(selected[-8:])
    value = re.sub(r"(?is)\b(?:input_value|request_body|prompt|code|script)\s*=.*$",
                   "[调用内容省略]", value)
    # Provider errors sometimes attach a full response payload after `body:`.
    value = re.sub(r"(?is)\bbody:\s*\{.*$", "body: [响应内容省略]", value)
    value = re.sub(r"(?is)(\b(?:Error code|status_code|HTTP)\s*[:=]?\s*\d{3})\s*[-:,]?\s*\{.*$",
                   r"\1 [服务器错误正文省略]", value)
    return value.strip()[:MAX_DETAIL]


def classify_issue(error: Any, *, tool: str = "", stage: str = "",
                   error_type: str = "", redact: Callable[[str], str] | None = None) -> dict[str, Any] | None:
    """Conservative project classification, not a diagnosis of the root cause."""
    tool = _TOOL_ALIASES.get(tool, tool)
    detail = _detail(error, redact)
    structured_type = (error.get("error_type") or error.get("kind") or error.get("type")
                       if isinstance(error, Mapping) else "")
    structured_code = str(error.get("code") or "") if isinstance(error, Mapping) else ""
    kind = error_type or (type(error).__name__ if isinstance(error, BaseException) else str(structured_type or ""))
    lower = detail.lower()
    sandbox = tool in _SCRIPT_TOOLS or tool == "python_sandbox_status" or "lpac" in lower
    classification: tuple[str, bool, str, str] | None = None
    # Routine rejected arguments/designs are not environmental problems. In
    # particular a gateway business_error wrapper must not erase this context.
    expected_rejection = (structured_code in {"invalid_arguments", "unavailable_tool", "repeated_error"}
        or re.search(r"(?:^|error executing tool [\w]+:\s*)(?:already_exists|not_found|permission_denied|"
                     r"invalid_input|invalid_design|forbidden_path|invalid_request):", lower))
    if expected_rejection and not sandbox:
        return None
    if tool in _SCRIPT_TOOLS and ((kind == "AttributeError" or "attributeerror:" in lower) and "has no attribute" in lower
                                  or "cannot import name" in lower):
        classification = ("script_api_usage", False, "脚本调用了不可用的接口或属性",
            "核对当前库实际 API；如 pygal 没有 Heatmap，应改用可用图型或 coat_plot。不能据此断言环境未安装。")
    elif sandbox and ("0xc0000022" in lower or "windows 5:" in lower or "隔离 acl 清理失败" in detail):
        classification = ("sandbox_access", True, "沙箱启动或清理访问受限",
            "保留具体 LPAC 阶段与系统错误，检查受信任运行时和临时 ACL 的初始化/清理；禁止普通 Python 降级或扩大脚本权限。")
    elif sandbox and ("lpac_runtime_load" in lower or "dll load failed" in lower
                      or "0xc0000135" in lower or "0xc000007b" in lower):
        classification = ("sandbox_native_runtime", True, "沙箱原生运行时未能加载",
            "按具体 DLL/NTSTATUS 检查可信私有解释器、原生依赖和体系结构；现有消息只证明加载失败，不证明所有依赖缺失。")
    elif sandbox and ("lpac_create_process" in lower or "私有运行时文件检查失败" in detail
                      or "解释器 dll 不存在" in lower):
        classification = ("sandbox_runtime_files", True, "沙箱解释器路径或私有文件检查失败",
            "通过 python_sandbox_status 查看实际缺失文件/阶段；需要时按项目 tools/setup_sandbox.py 准备可信运行时，保持隔离限制。")
    elif sandbox and ("此平台没有 windows lpac" in lower or "isolationunavailable" in lower
                      or "lpac_profile" in lower or "禁止降级执行" in detail):
        classification = ("sandbox_isolation", True, "系统隔离能力不可用",
            "核对当前平台及具体隔离阶段；当前 LPAC 后端不可用时停止脚本，不回退到普通 Python。")
    elif "no module named" in lower or (kind == "ModuleNotFoundError") or (
            "socksio" in lower and ("not installed" in lower or "缺少" in detail)):
        classification = ("script_dependency" if tool in _SCRIPT_TOOLS else "dependency_missing", True,
            "脚本所需模块不可用" if tool in _SCRIPT_TOOLS else "运行依赖未能载入",
            "脚本只能使用已准备的私有运行时库；改用现有库或在可信安装流程中补齐依赖，不绕过沙箱。"
            if tool in _SCRIPT_TOOLS else "根据具体模块安装/修复项目声明的依赖；不要把单个组件缺失解释为整个环境未安装。")
    elif "cannot import name" in lower or (kind == "ImportError" and "no module named" not in lower):
        classification = ("dependency_api", True, "依赖接口或版本兼容性问题",
            "核对实际导入符号与已安装版本；不能只凭 ImportError 重装全部依赖。")
    elif "dll load failed" in lower or "cannot load library" in lower:
        classification = ("native_dependency", True, "原生组件未能加载",
            "按具体模块、文件和系统错误核对 DLL/动态库及体系结构，不推断未验证的缺失组件。")
    elif (tool in _PREVIEW_TOOLS or stage in {"preview_file", "preview_svg", "open_file"}) and (
            kind in {"OSError", "FileNotFoundError", "PermissionError", "UnicodeDecodeError",
                     "UnidentifiedImageError", "ValueError", "ParseError", "RuntimeError"}
            or any(token in lower for token in (
            "系统找不到指定的文件", "no such file", "not found", "cannot decode", "无法解码", "无法打开", "打开失败"))):
        classification = ("file_preview", True, "附件读取、解码或系统打开失败",
            "使用工具返回的真实文件引用，核对文件存在性、可读性、格式和系统打开方式；不猜测或读取未授权路径。")
    elif tool in _FILE_READ_TOOLS and (kind in {"FileNotFoundError", "PermissionError", "OSError",
            "UnicodeDecodeError"} or any(token in lower for token in ("[errno 2]", "[errno 13]",
            "[winerror 2]", "[winerror 5]", "no such file or directory", "系统找不到指定的文件", "permission denied"))):
        classification = ("file_access", True, "授权文件读取失败",
            "保留实际文件/操作系统错误并核对引用、存在性和可读性；不自动读取其他路径，不绕过授权限制。")
    elif stage in {"worker_initialization", "worker_task"} and (kind in {
            "FileNotFoundError", "PermissionError", "OSError", "OperationalError"} and any(
            token in lower for token in ("[errno", "[winerror", "permission", "系统找不到",
                "unable to open database", "database is locked", "database disk image", "disk i/o", "readonly database"))):
        classification = ("runtime_storage", True, "助手运行文件或状态存储失败",
            "核对实际文件引用、可读写状态和存储错误；不要清空会话记录或自动变更路径权限。")
    elif structured_code == "transport_error" or kind in {"ConnectionError", "ConnectError", "ConnectTimeout", "ReadTimeout", "TimeoutError",
                  "APITimeoutError", "APIConnectionError"} or any(token in lower for token in (
            "connection refused", "connection reset", "failed to connect", "certificate verify failed",
            "name resolution", "getaddrinfo failed", "connect timeout", "read timeout", "connection error",
            "connection closed", "broken pipe", "模型连接超时")):
        classification = ("service_connection", True, "模型或项目服务连接失败",
            "核对具体连接、代理、证书和服务可用性；写入效果未知时先核对记录，禁止重复提交。")
    elif stage in {"model_request", "worker_initialization"} and re.search(
            r"(?:status(?:_code)?|http|error code)\s*[:=]?\s*(?:401|403|404)\b", lower):
        classification = ("service_configuration", True, "模型服务地址或授权未被接受",
            "核对当前服务 API 路径、模型名和授权配置；日志不记录密钥，HTTP 状态本身不证明具体根因。")
    elif stage == "model_request" and re.search(
            r"(?:status(?:_code)?|http|error code)\s*[:=]?\s*(?:429|5\d\d)\b", lower):
        classification = ("service_unavailable", True, "模型服务拒绝或未完成本次请求",
            "保留实际 HTTP 状态与时间，核对服务限流/可用性；不把一次服务失败解释为本地环境缺失。")
    elif stage in {"tool_execute", "worker_task", "worker_initialization", "model_request", "cancel_cleanup"}:
        # Keep real, previously unrecognized failures in the ledger instead of
        # silently dropping them. No unverified environment diagnosis is made.
        classification = ("unclassified_runtime_failure", False, "实际运行失败（根因待核对）",
            "此条来自真实异常或失败工具反馈。现有证据不足以判定环境根因；核对具体阶段、工具契约和版本后补充报告。")
    if classification is None or not detail:
        return None
    category, environment, title, recovery = classification
    return {"category": category, "environment": environment, "title": title,
            "error_type": kind, "detail": detail, "recovery": recovery}


def issue_report_status() -> dict[str, Any]:
    """Read the effective gate and whether its ledger/readable reports exist."""
    available: dict[str, bool] = {}
    for suffix in (".sqlite3", ".json", ".md"):
        try:
            available[suffix] = data_path(LOG_BASE + suffix, root_allowed=False).is_file()
        except (OSError, ValueError, PermissionError):
            available[suffix] = False
    return {"enabled": enabled(), "automatic": True, "explicit_tool": "developer_report_issue",
            "report_tool": "developer_issue_reports", "ledger": LOG_BASE + ".sqlite3",
            "references": [LOG_BASE + suffix for suffix in (".md", ".json") if available[suffix]],
            "files_exist": available, "last_recording_error": _LAST_RECORDING_ERROR or None,
            "note": "只记录真实失败；未遇到失败时不编造记录。SESSION_ISSUES.md 不会覆盖。"}


def _context(stage: str, tool: str, request_id: str, session_id: str, source: str) -> dict[str, str]:
    # These are protocol identifiers/labels, never user text or command arguments.
    return {key: re.sub(r"[^\w.:-]", "_", str(value))[:128] for key, value in {
        "stage": stage, "tool": _TOOL_ALIASES.get(tool, tool), "request_id": request_id,
        "session_id": session_id, "source": source}.items()}


def _paths() -> tuple[Path, Path, Path]:
    directory = data_path("data/tmp")
    directory.mkdir(parents=True, exist_ok=True)
    # Recheck every fixed output and SQLite sidecar before opening anything.
    for suffix in (".sqlite3", ".sqlite3-journal", ".sqlite3-wal", ".sqlite3-shm", ".json", ".md"):
        data_path(LOG_BASE + suffix, root_allowed=False)
    return tuple(data_path(LOG_BASE + suffix, root_allowed=False)
                 for suffix in (".sqlite3", ".json", ".md"))  # type: ignore[return-value]


def _atomic_text(path: Path, text: str) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                prefix=".environment-issues-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        data_path(str(path), root_allowed=False)
        replace_with_retry(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _export(database: sqlite3.Connection, json_path: Path, markdown_path: Path) -> None:
    total, observations = database.execute("SELECT count(*), coalesce(sum(count),0) FROM issues").fetchone()
    rows = database.execute("SELECT * FROM issues ORDER BY last_seen DESC LIMIT ?", (RECENT_LIMIT,)).fetchall()
    records = []
    for row in rows:
        record = dict(row)
        record["environment"] = bool(record["environment"])
        for key in ("first_context", "last_context", "last_success_context"):
            record[key] = json.loads(record[key]) if record[key] else None
        records.append(record)
    payload = {"format_version": 1, "updated_at": datetime.now(timezone.utc).isoformat(),
        "total_issues": total, "total_occurrences": observations, "recent_limit": RECENT_LIMIT,
        "ledger": LOG_BASE + ".sqlite3", "issues": records,
        "note": "自动分类与恢复建议是本项目规则，不是已验证根因；后续调用成功不证明历史原因已修复。"}
    lines = ["# 开发环境与能力问题 · 自动记录", "", payload["note"], "",
             f"共 {total} 类问题、{observations} 次实际观察；此页展示最近 {len(records)} 类。",
             "完整去重记录保留在 ENVIRONMENT_ISSUES.sqlite3；不覆盖 SESSION_ISSUES.md。", ""]
    for record in records:
        context = record["last_context"]
        lines.extend(["## " + record["title"], "",
            f"- 类别：`{record['category']}`；环境问题：{'是（按已知错误分类，根因仍需核对）' if record['environment'] else '未确认或属于能力用法问题'}",
            f"- 首次：{record['first_seen']}；最后：{record['last_seen']}；次数：{record['count']}",
            f"- 阶段：`{context['stage']}`；工具：`{context['tool']}`；来源：`{context['source']}`",
            f"- 请求：`{context['request_id']}`；会话：`{context['session_id']}`", "",
            "### 实际错误（已脱敏）", "", *("> " + line.replace("<", "&lt;").replace(">", "&gt;")
                                                    for line in record["detail"].splitlines()), "",
            "### 恢复提示（未自动执行）", "", record["recovery"], ""])
        if record["last_success_at"]:
            lines.extend(["### 后续实际观察", "", record["last_success_at"] + " · " +
                          str(record["recovery_observation"] or ""), ""])
        if context.get("evidence_reference"):
            reference = context["evidence_reference"]
            lines.extend(["### 证据文件", "", "`" + reference.replace("`", "") + "`", ""])
    _atomic_text(json_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    _atomic_text(markdown_path, "\n".join(lines))


def record_issue(error: Any, *, stage: str, tool: str = "", request_id: str = "", session_id: str = "",
                 source: str = "", error_type: str = "", recovery: str = "",
                 config: Mapping[str, Any] | None = None, redact: Callable[[str], str] | None = None,
                 occurrence_id: str = "", reported_title: str = "", evidence_reference: str = "") -> dict[str, Any] | None:
    """Record a classified actual issue. Returns None on gate/no match/log failure."""
    global _LAST_RECORDING_ERROR
    if not enabled(config):
        return None
    try:
        issue = classify_issue(error, tool=tool, stage=stage, error_type=error_type, redact=redact)
        if issue is None and reported_title and _detail(error, redact):
            issue = {"category": "agent_reported_observation", "environment": False,
                "title": _redact(reported_title, redact)[:180], "error_type": error_type or "AgentObservation",
                "detail": _detail(error, redact),
                "recovery": "代理根据已观察错误提交；环境根因和修复结论尚未独立核对。"}
        if issue is None:
            return None
        if reported_title:
            issue["title"] = _redact(reported_title, redact)[:180]
        if recovery:
            issue["recovery"] = (issue["recovery"] + "\n工具给出的恢复要求：" +
                                 _redact(recovery, redact))[:MAX_RECOVERY]
        context = _context(stage, tool, request_id, session_id, source)
        if evidence_reference:
            from coat_sim.core.data_files import file_info
            # An actual authorized file only; the tool cannot use report writing
            # to authorize a path or read arbitrary evidence.
            evidence = data_path(evidence_reference, root_allowed=False)
            if not evidence.is_file():
                raise FileNotFoundError("证据文件不存在。")
            context["evidence_reference"] = file_info(evidence)["reference"]
        # Per-run UUIDs/addresses must not split identical initialization issues.
        normalized = re.sub(r"\b[0-9a-f]{32}\b|\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b",
                            "[运行标识]", issue["detail"], flags=re.I)
        normalized = re.sub(r"0x[0-9a-f]{12,16}\b", "[内存地址]", normalized, flags=re.I)
        normalized = re.sub(r"(\[service\.[a-z0-9_.-]+\s*·\s*)[0-9a-f]{16}(?=\])",
                            r"\1[发生编号]", normalized, flags=re.I)
        fingerprint = hashlib.sha256(json.dumps([issue["category"], issue["error_type"],
            context["tool"], normalized], ensure_ascii=False).encode()).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        reports_saved = False
        db_path, json_path, md_path = _paths()
        with sqlite3.connect(db_path, timeout=2) as database:
            database.row_factory = sqlite3.Row
            database.executescript(_DATABASE_SCHEMA)
            database.execute("BEGIN IMMEDIATE")
            if occurrence_id:
                occurrence = hashlib.sha256((fingerprint + "\0" + context["request_id"] + "\0" +
                                             str(occurrence_id)[:256]).encode()).hexdigest()
                previous = database.execute("SELECT 1 FROM occurrences WHERE identity=?", (occurrence,)).fetchone()
                if previous:
                    return {"fingerprint": fingerprint, "duplicate": True, "recorded": True,
                            "reports_saved": md_path.is_file() and json_path.is_file(),
                            "reference": LOG_BASE + ".md"}
                database.execute("INSERT INTO occurrences VALUES (?,?,?)", (occurrence, fingerprint, now))
            encoded_context = json.dumps(context, ensure_ascii=False)
            database.execute("""INSERT INTO issues (fingerprint,category,environment,title,error_type,detail,recovery,
                    first_seen,last_seen,count,first_context,last_context) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(fingerprint) DO UPDATE SET last_seen=excluded.last_seen,count=issues.count+1,
                    detail=excluded.detail,recovery=excluded.recovery,last_context=excluded.last_context""",
                (fingerprint, issue["category"], int(issue["environment"]), issue["title"], issue["error_type"],
                 issue["detail"], issue["recovery"], now, now, 1, encoded_context, encoded_context))
            # Commit the authority before writing read views. Reacquire the
            # SQLite writer lock and export a fresh database snapshot, so a
            # different process cannot overwrite a newer projection with stale
            # rows, while a crash during export cannot lose this observation.
            database.commit()
            try:
                database.execute("BEGIN IMMEDIATE")
                _export(database, json_path, md_path)
                reports_saved = True
                _LAST_RECORDING_ERROR = ""
            except Exception as exc:
                # The ledger transaction still commits if a read view cannot
                # be written; the next observation regenerates its projection.
                _LAST_RECORDING_ERROR = _redact(type(exc).__name__ + ": " + str(exc), redact)[:500]
        return {"fingerprint": fingerprint, "category": issue["category"],
                "environment": issue["environment"], "recorded": True, "reports_saved": reports_saved,
                "reference": LOG_BASE + ".md" if reports_saved else LOG_BASE + ".sqlite3"}
    except Exception as exc:
        # Do not serialize the logger's own exception or hide a charged response.
        _LAST_RECORDING_ERROR = _redact(type(exc).__name__ + ": " + str(exc), redact)[:500]
        return None


def observe_success(*, stage: str, tool: str = "", request_id: str = "", session_id: str = "",
                    source: str = "", config: Mapping[str, Any] | None = None) -> None:
    """Record a real later success in this request, without claiming root repair."""
    if not enabled(config):
        return
    try:
        if not data_path(LOG_BASE + ".sqlite3", root_allowed=False).is_file():
            return
        db_path, json_path, md_path = _paths()
        context = _context(stage, tool, request_id, session_id, source)
        with sqlite3.connect(db_path, timeout=2) as database:
            database.row_factory = sqlite3.Row
            database.execute("BEGIN IMMEDIATE")
            rows = database.execute("SELECT fingerprint,last_seen,last_success_at,last_context FROM issues").fetchall()
            matched = []
            for row in rows:
                previous = json.loads(row["last_context"])
                if (all(previous.get(key) == context[key] for key in ("stage", "tool", "request_id"))
                        and (not row["last_success_at"] or row["last_seen"] > row["last_success_at"])):
                    matched.append(row["fingerprint"])
            if not matched:
                return
            now = datetime.now(timezone.utc).isoformat()
            for fingerprint in matched:
                database.execute("UPDATE issues SET last_success_at=?,last_success_context=?,recovery_observation=? "
                    "WHERE fingerprint=?", (now, json.dumps(context, ensure_ascii=False),
                    "同一请求后续同工具/阶段调用成功；仅记录实际观察，不证明历史根因已修复。", fingerprint))
            database.commit()
            try:
                database.execute("BEGIN IMMEDIATE")
                _export(database, json_path, md_path)
            except Exception:
                pass
    except Exception:
        return


def tool_issue(result: Any, tool: str) -> tuple[Any, str] | None:
    """Extract errors only; source, arbitrary stdout, args and schemas stay out."""
    if not isinstance(result, Mapping) or result.get("dispatch_blocked"):
        return None
    if result.get("ok") is False and result.get("error"):
        error = result["error"]
        if isinstance(error, str) and (result.get("error_type") or result.get("kind")):
            error = {"message": error, "error_type": result.get("error_type") or result.get("kind")}
        recovery = error.get("recovery", "") if isinstance(error, Mapping) else ""
        return error, str(recovery)
    if tool == "run_python_script" and result.get("exit_code") not in (None, 0):
        return str(result.get("stderr") or f"沙箱脚本返回非零退出码：{result['exit_code']}。"), ""
    if tool == "python_sandbox_status" and result.get("available") is False:
        return str(result.get("reason") or ""), ""
    if result.get("state") == "failed" and result.get("error"):
        return result["error"], ""
    simulation = result.get("simulation")
    if isinstance(simulation, Mapping) and simulation.get("ok") is False and simulation.get("error"):
        return simulation["error"], ""
    if result.get("timed_out") is True:
        return {"error_type": "TimeoutError", "message": "工具反馈执行超时。"}, ""
    if tool == "developer_checks" and result.get("passed") is False:
        if result.get("timed_out"):
            return {"error_type": "TimeoutError", "message": "固定开发检查执行超时。"}, ""
        errors = result.get("errors")
        if isinstance(errors, list) and errors:
            return {"error_type": "DevelopmentCheckError", "message": "\n".join(
                str(item.get("error", "")) for item in errors[:4] if isinstance(item, Mapping))}, ""
    return None


def report_observed_issue(title: str, detail: str, *, stage: str = "agent_observation", tool: str = "",
                          evidence_reference: str = "", recovery: str = "",
                          request_id: str = "", session_id: str = "") -> dict[str, Any]:
    """Explicit development report, labelled as observation rather than diagnosis.

    detail must quote an actually encountered error or describe a directly
    observed mismatch. This does not run a check, read evidence, or grant access.
    """
    from coat_sim.agent.development import require_development
    require_development()
    if not title.strip() or len(title) > 180:
        raise ValueError("报告标题须为 1–180 字符。")
    if not detail.strip() or len(detail) > 6000:
        raise ValueError("须提供实际错误或直接观察，最多 6000 字符；不填写推测的环境根因。")
    if len(recovery) > MAX_RECOVERY:
        raise ValueError(f"补充恢复观察最多 {MAX_RECOVERY} 字符。")
    result = record_issue(detail, stage=stage, tool=tool, request_id=request_id, session_id=session_id,
        source="assistant_report", reported_title=title, evidence_reference=evidence_reference, recovery=recovery)
    if result is None:
        raise RuntimeError("开发问题报告未能写入：" + (_LAST_RECORDING_ERROR or "开发门禁未生效。"))
    result["note"] = "已写入实际观察；不代表已验证环境根因，也未执行自动修复。"
    if result.get("reports_saved"):
        from coat_sim.core.data_files import file_info
        result["files"] = [file_info(data_path(LOG_BASE + suffix, root_allowed=False))
                           for suffix in (".md", ".json")]
    if not result.get("reports_saved"):
        result["warning"] = "记录已写入 SQLite，但可读报告生成失败；调用 developer_issue_reports refresh=true 重试。"
    return result


def issue_reports(*, refresh: bool = False, limit: int = 10, consumer_id: str = "mcp",
                  unread_only: bool = True, acknowledge: bool = True,
                  details: bool = False, offset: int = 0) -> dict[str, Any]:
    """Bounded unread views per reader. Reading never deletes diagnostic evidence.

    Acknowledgement covers the exact returned version, so a later occurrence or
    recovery remains unread. Set unread_only=False to revisit archived records.
    Full error/context is opt-in; automatic model prompts use short summaries.
    """
    global _LAST_RECORDING_ERROR
    from coat_sim.agent.development import require_development
    from coat_sim.core.data_files import file_info
    require_development()
    if not isinstance(limit, int) or not 1 <= limit <= RECENT_LIMIT:
        raise ValueError(f"limit 须为 1–{RECENT_LIMIT}。")
    if not isinstance(consumer_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", consumer_id):
        raise ValueError("consumer_id 须为 1–128 字符的读取者标识。")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("offset 须为非负整数；未读读取会移除已返回项，下一页仍使用 offset=0。")
    db_path = data_path(LOG_BASE + ".sqlite3", root_allowed=False)
    if not db_path.is_file():
        return {"status": issue_report_status(), "issues": [], "files": [],
                "note": "尚无实际记录；没有生成虚构问题报告。"}
    db_path, json_path, md_path = _paths()
    with sqlite3.connect(db_path, timeout=2) as database:
        database.row_factory = sqlite3.Row
        database.executescript(_DATABASE_SCHEMA)
        database.execute("BEGIN IMMEDIATE")
        if refresh:
            _export(database, json_path, md_path)
            _LAST_RECORDING_ERROR = ""
        total, observations = database.execute("SELECT count(*),coalesce(sum(count),0) FROM issues").fetchone()
        version = "max(i.last_seen,coalesce(i.last_success_at,''))"
        joined = " FROM issues i LEFT JOIN issue_readers r ON r.fingerprint=i.fingerprint AND r.consumer=?"
        unread = f" WHERE r.observed_at IS NULL OR r.observed_at < {version}"
        unread_count = database.execute("SELECT count(*)" + joined + unread, (consumer_id,)).fetchone()[0]
        rows = database.execute(f"SELECT i.*,{version} AS observed_version" + joined +
            (unread if unread_only else "") + " ORDER BY observed_version DESC,i.fingerprint LIMIT ? OFFSET ?",
            (consumer_id, limit, offset)).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            record["environment"] = bool(record["environment"])
            for key in ("first_context", "last_context", "last_success_context"):
                record[key] = json.loads(record[key]) if record[key] else None
            if not details:
                context = record["last_context"] or {}
                record = {key: record[key] for key in ("fingerprint", "category", "title", "error_type",
                    "environment", "last_seen", "count", "observed_version", "last_success_at")}
                record.update(detail=str(row["detail"])[:400], stage=context.get("stage", ""),
                              tool=context.get("tool", ""), evidence_reference=context.get("evidence_reference"))
            records.append(record)
        if acknowledge:
            database.executemany("""INSERT INTO issue_readers VALUES (?,?,?)
                ON CONFLICT(consumer,fingerprint) DO UPDATE SET
                    observed_at=max(issue_readers.observed_at,excluded.observed_at)""",
                [(consumer_id, row["fingerprint"], row["observed_version"]) for row in rows])
        remaining = database.execute("SELECT count(*)" + joined + unread, (consumer_id,)).fetchone()[0]
    return {"status": issue_report_status(), "total_issues": total, "total_occurrences": observations,
            "consumer_id": consumer_id, "unread_before": unread_count, "unread_remaining": remaining,
            "acknowledged": acknowledge, "issues": records,
            "files": [file_info(path) for path in (md_path, json_path) if path.is_file()],
            "note": "已读仅影响此读取者的未读视图，不清空记录、不表示问题已解决。读取已读记录用 unread_only=false；完整细节用 details=true。"}


def instructions(config: Mapping[str, Any] | None = None) -> str:
    if not enabled(config):
        return ""
    return ("\n[开发问题记录] 程序会从真实初始化、模型连接、工具与沙箱反馈自动记录可识别环境问题，"
            "以及脚本调用不存在 API 的能力用法问题，无需专门调用模型整理每个错误。"
            "最近问题按需调用 developer_issue_reports，默认仅返回此会话未读的10条短摘要并标记已读；"
            "再次发生或补充恢复观察会重新变为未读。不要将累计 JSON/Markdown 全文重复放入上下文；"
            "旧问题用 unread_only=false，完整错误用 details=true，必要时分页读取；"
            "完整去重记录保存在 data/tmp/ENVIRONMENT_ISSUES.sqlite3。现有 SESSION_ISSUES.md 不会覆盖。"
            "记录含首次/末次、次数、工具阶段、脱敏错误及实际恢复观察；分类/恢复提示不是已验证根因。"
            "例如 pygal.Heatmap 不存在表示 API 用法错误，不表示 pygal 未安装；"
            "对未被自动捕获的真实问题（如界面行为与返回结果不一致），通过 project_action 调用"
            "developer_report_issue(title,detail,tool,stage,evidence_reference,recovery) 写入报告；detail 只写实际观察，"
            "不要编造诊断或保存密钥。用 developer_issue_reports(refresh=true) 再生成 Markdown 与 JSON 报告，"
            "developer_status.issue_reports 可核对开发门禁、文件是否生成及记录失败原因。"
            "沙箱 DLL/启动路径失败应保留具体阶段与系统码。正常缺参或设计校验不是环境故障。"
            "不要重复编造环境记录、清空日志、写入密钥/完整提示词/脚本，也不要借问题日志扩大路径权限。")
