"""Opt-in external MCP connections; configuration never contains credentials.

The worker owns its clients and opens connections only when a model requests
the external catalog or a tool. No conversation, attachment, filesystem root
or model-provider credential is forwarded automatically.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

from coat_sim.agent.mcp_presets import TOOLS, reserve_call, tool_allowed

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAX_SERVERS = 20
MAX_TOOLS = 200
MAX_UNCERTAIN_CALLS = 1024
SAFE_ENVIRONMENT = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "HOME",
                    "LANG", "LC_ALL", "PATHEXT")
_IDENTIFIER = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_FIELD_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]{0,127}$")


def validate_server(values: Mapping[str, Any], *, new: bool = False) -> dict[str, Any]:
    """Validate only the nonsecret server schema; never keep unrecognized fields."""
    identifier = str(values.get("id", "")).strip() or (uuid4().hex if new else "")
    if not _IDENTIFIER.fullmatch(identifier):
        raise ValueError("MCP 服务标识无效")
    name = str(values.get("name", "")).strip()
    if not name or len(name) > 80:
        raise ValueError("MCP 服务名称须为 1–80 个字符")
    transport = values.get("transport", "stdio")
    if transport not in ("stdio", "http", "sse"):
        raise ValueError("MCP 传输须为 stdio、HTTP 或 SSE")
    command, args, cwd, url = "", [], "", ""
    if transport == "stdio":
        command = str(values.get("command", "")).strip()
        if not command or len(command) > 2048 or any(c in command for c in ("\x00", "\n", "\r")):
            raise ValueError("请填写可执行程序路径或命令，参数另填 JSON 数组")
        args = values.get("args", [])
        if not isinstance(args, list) or len(args) > 100 or any(
                not isinstance(arg, str) or len(arg) > 4096 or "\x00" in arg for arg in args):
            raise ValueError("MCP 参数须为最多 100 项的字符串 JSON 数组")
        cwd = str(values.get("cwd", "")).strip()
        if cwd and (len(cwd) > 2048 or not Path(cwd).is_absolute() or "\x00" in cwd):
            raise ValueError("工作目录须为绝对路径，留空使用临时目录")
        if cwd and not values.get("allow_project_data", False):
            try:
                Path(cwd).resolve().relative_to(PROJECT_ROOT.resolve())
            except ValueError:
                pass
            else:
                raise ValueError("将工作目录设为本项目须勾选允许发送或访问项目内容")
    else:
        url = str(values.get("url", "")).strip()
        parsed = urlsplit(url)
        if (len(url) > 2048 or parsed.scheme not in ("https", "http") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("MCP 地址须为不含账户、查询参数的 HTTP(S) 地址")
        if parsed.scheme == "http" and parsed.hostname.lower() not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("远程 MCP 服务须使用 HTTPS")
    field = str(values.get("credential_field", "")).strip()
    if field and not _FIELD_NAME.fullmatch(field):
        raise ValueError("凭据名称须为有效的环境变量名或 HTTP 头名称")
    if transport != "stdio" and field.lower() in ("host", "content-length", "content-type", "connection"):
        raise ValueError("此 HTTP 头不能作为凭据")
    try:
        timeout = int(values.get("timeout_s", 60))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("工具超时须为 5–600 秒") from exc
    if not 5 <= timeout <= 600:
        raise ValueError("工具超时须为 5–600 秒")
    revision = str(values.get("credential_revision", ""))
    if revision and not _IDENTIFIER.fullmatch(revision):
        revision = ""
    preset = values.get("preset_id", "")
    if preset not in TOOLS:
        preset = ""
    try:
        limit = int(values.get("daily_call_limit", 0))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("每日调用上限须为 0–10000") from exc
    if not 0 <= limit <= 10000:
        raise ValueError("每日调用上限须为 0–10000")
    return {"preset_id": preset, "daily_call_limit": limit,
            "allow_paid_calls": values.get("allow_paid_calls") is True,
            "id": identifier, "name": name, "transport": transport,
            "command": command, "args": list(args), "cwd": cwd, "url": url,
            "enabled": values.get("enabled") is True,
            "allow_tools": values.get("allow_tools") is True,
            "allow_project_data": values.get("allow_project_data") is True,
            "credential_field": field, "credential_revision": revision,
            "timeout_s": timeout}


def normalized_servers(values: object) -> list[dict[str, Any]]:
    rows, identifiers = [], set()
    for value in values[:MAX_SERVERS] if isinstance(values, list) else []:
        if not isinstance(value, Mapping):
            continue
        try:
            row = validate_server(value)
        except (ValueError, TypeError):
            continue
        if row["id"] not in identifiers:
            rows.append(row)
            identifiers.add(row["id"])
    return rows


def _connection(config: dict[str, Any]):
    # Lazy imports keep settings/startup available without the optional runtime.
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport, StreamableHttpTransport, SSETransport
    from coat_sim.ui.assistant_credentials import read_mcp_secret

    secret = read_mcp_secret(config["id"]) if config["credential_revision"] else None
    if config["credential_revision"] and not secret:
        raise ValueError("系统安全凭据中未找到此服务的密钥，请重新保存")
    field = config["credential_field"]
    if secret and not field:
        raise ValueError("此服务缺少凭据名称，请编辑并保存")
    if config["transport"] == "stdio":
        env = {name: os.environ[name] for name in SAFE_ENVIRONMENT if name in os.environ}
        if secret:
            env[field] = secret
        # No shell parsing and no automatic project cwd or project credentials.
        transport = StdioTransport(command=config["command"], args=config["args"], env=env,
            cwd=config["cwd"] or tempfile.gettempdir(), keep_alive=True, log_file=Path(os.devnull))
    else:
        headers = {}
        if secret:
            headers[field] = ("Bearer " + secret if field.lower() == "authorization"
                              and not secret.lower().startswith("bearer ") else secret)
        kind = SSETransport if config["transport"] == "sse" else StreamableHttpTransport
        transport = kind(config["url"], headers=headers)
    client = Client(transport, name="coat_external_" + config["id"],
                    roots=[], init_timeout=20, timeout=config["timeout_s"])
    return client, transport, secret or ""


def _safe_error(exc: BaseException, secret: str = "") -> str:
    text = str(exc) or type(exc).__name__
    if secret:
        text = text.replace(secret, "[凭据已隐藏]")
    text = re.sub(r"(?i)(authorization|api[_-]?key|token|password)\s*[:=]\s*[^\s,;]+",
                  r"\1=[凭据已隐藏]", text)
    return text[:1000]


def _has_project_path(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_has_project_path(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_project_path(item) for item in value)
    if isinstance(value, str):
        normalized = value.replace("\\", "/").lower()
        root = str(PROJECT_ROOT).replace("\\", "/").lower()
        return root in normalized or normalized.startswith(("data/", "artifacts/", "config/"))
    return False


def _redact_values(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return value.replace(secret, "[凭据已隐藏]") if secret else value
    if isinstance(value, dict):
        return {_redact_values(key, secret): _redact_values(item, secret) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_values(item, secret) for item in value]
    return value


def _call_identity(config: Mapping[str, Any], tool_name: str,
                   arguments: dict[str, Any]) -> tuple[str, str, str, str]:
    """Hash exact target and JSON arguments; never retain argument/secret text.

    Permission toggles, display names and credential rotation do not change a
    peer's identity. Replacing its endpoint/command does; old identities remain
    guarded if that target is restored in this worker.
    """
    target = {name: config[name] for name in ("transport", "command", "args", "cwd", "url")}
    def digest(value: Any) -> str:
        data = json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False)
        if json.loads(data) != value:
            raise ValueError("Arguments must use JSON object keys and value types")
        return hashlib.sha256(data.encode("utf-8")).hexdigest()
    return config["id"], digest(target), tool_name, digest(arguments)


class ExternalMcpPool:
    """Connections shared across turns in one worker; failed calls never retry."""

    def __init__(self) -> None:
        self.configs: dict[str, dict[str, Any]] = {}
        self.active: dict[str, tuple[Any, Any, str]] = {}
        # A published configuration may change before sync acquires this
        # server's lock to close its old client. Bind each client to the exact
        # nonsecret configuration that actually created it, not just its ID.
        self._active_configs: dict[str, dict[str, Any]] = {}
        self.tools: dict[str, dict[str, Any]] = {}
        self.errors: dict[str, str] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.uncertain_calls: set[tuple[str, str, str, str]] = set()
        self._inflight_calls: set[tuple[str, str, str, str]] = set()

    def _lock(self, identifier: str) -> asyncio.Lock:
        return self.locks.setdefault(identifier, asyncio.Lock())

    async def sync(self, values: object) -> None:
        configs = {row["id"]: row for row in normalized_servers(values) if row["enabled"]}
        previous = self.configs
        self.configs = configs
        # A failed old target is not a connection failure of its replacement.
        # Filter before awaiting cleanup so concurrent syncs cannot restore a
        # stale snapshot of errors after a new connection attempt completes.
        self.errors = {key: value for key, value in self.errors.items()
                       if key in configs and configs[key] == previous.get(key)}
        for identifier in list(self.active):
            if self._active_configs.get(identifier) == self.configs.get(identifier):
                continue
            async with self._lock(identifier):
                # An operation ahead of this cleanup may already have replaced
                # the client. Do not close its now-current connection.
                if (identifier in self.active and self._active_configs.get(identifier)
                        != self.configs.get(identifier)):
                    await self._close_locked(identifier)

    async def _close(self, identifier: str) -> None:
        async with self._lock(identifier):
            await self._close_locked(identifier)

    async def _close_locked(self, identifier: str) -> None:
        active = self.active.pop(identifier, None)
        self._active_configs.pop(identifier, None)
        self.tools.pop(identifier, None)
        self.errors.pop(identifier, None)
        if active:
            client, transport, _ = active
            try:
                await client.close()
            except Exception:
                # A failed HTTP session can re-raise its original socket error
                # during close. Local revocation must still finish; cleanup
                # does not reconcile or clear unknown remote effects.
                pass
            finally:
                try:
                    await transport.close()
                except Exception:
                    pass

    async def close(self) -> None:
        for identifier in list(self.active):
            try:
                await self._close(identifier)
            except Exception:
                pass
        self.configs.clear()

    async def _ensure(self, identifier: str):
        async with self._lock(identifier):
            return await self._ensure_locked(identifier)

    async def _ensure_locked(self, identifier: str):
        # A catalog may have awaited this lock while sync revoked permission.
        # Recheck both gates before reusing or starting a connection.
        config = self.configs.get(identifier)
        if not config or not config["allow_tools"]:
            raise ValueError("外部 MCP 服务未启用或未获工具调用许可。")
        if identifier in self.active:
            if self._active_configs.get(identifier) == config:
                return self.active[identifier][0]
            # sync published a replacement while waiting behind this operation
            # for the lock. Old tools, credentials and transport cannot serve
            # the current target, even when the server ID stayed the same.
            await self._close_locked(identifier)
            if self.configs.get(identifier) != config:
                raise ValueError("关闭旧连接期间外部 MCP 配置已改变；未建立替代连接。")
        client = transport = None
        secret = ""
        try:
            client, transport, secret = _connection(config)
            await client.__aenter__()
            rows = await client.list_tools(max_pages=20)
            if len(rows) > MAX_TOOLS:
                raise ValueError(f"服务工具数量超过 {MAX_TOOLS} 项限制")
            if self.configs.get(identifier) != config:
                raise ValueError("连接期间外部 MCP 配置已改变；未派发工具调用。")
            self.tools[identifier] = {tool.name: tool for tool in rows
                                      if tool_allowed(config, tool.name)}
            self.active[identifier] = (client, transport, secret)
            self._active_configs[identifier] = deepcopy(config)
            self.errors.pop(identifier, None)
            return client
        except BaseException as exc:
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass
            if transport is not None:
                try:
                    await transport.close()
                except Exception:
                    pass
            if isinstance(exc, asyncio.CancelledError):
                raise
            error = _safe_error(exc, secret)
            if self.configs.get(identifier) == config:
                self.errors[identifier] = error
            raise ValueError(error) from None

    async def catalog(self, server_id: str = "", tool_name: str = "") -> dict[str, Any]:
        if server_id and server_id not in self.configs:
            return {"ok": False, "error": "此 MCP 服务未启用或已删除。"}
        ids = [server_id] if server_id else list(self.configs)
        servers, tools = [], []
        for identifier in ids:
            config = self.configs.get(identifier)
            if config is None:
                # A previous server's catalog await may have let sync remove
                # this entry. It must not turn a read into an uncaught KeyError.
                continue
            entry = {"server_id": identifier, "name": config["name"],
                     "transport": config["transport"], "allow_tools": config["allow_tools"],
                     "allow_project_data": config["allow_project_data"]}
            if not config["allow_tools"]:
                entry["status"] = "工具调用未授权；请在设置中开启"
            elif identifier in self.errors:
                # A failed connection is retried on the next user turn, never an
                # automatic model loop in the current turn.
                entry["error"] = self.errors[identifier]
            elif not server_id:
                entry["status"] = "已连接" if identifier in self.active else "按需连接"
            else:
                try:
                    await self._ensure(identifier)
                    # A lock wait can replace the target before _ensure runs.
                    # Report metadata for the client whose tools we return.
                    current = self._active_configs[identifier]
                    entry.update(name=current["name"], transport=current["transport"],
                                 allow_tools=current["allow_tools"],
                                 allow_project_data=current["allow_project_data"])
                    entry["status"] = "已连接"
                    for name, tool in self.tools[identifier].items():
                        if not tool_name or name == tool_name:
                            detail = {"server_id": identifier, "name": name,
                                      "description": str(tool.description or "")[:6000 if tool_name else 600]}
                            if tool_name:
                                detail.update(input_schema=(getattr(tool, "input_schema", None)
                                                           or getattr(tool, "inputSchema", {})),
                                    annotations=tool.annotations.model_dump(mode="json") if tool.annotations else {})
                            tools.append(detail)
                except Exception as exc:
                    entry["error"] = _safe_error(exc)
            servers.append(entry)
        payload = {"ok": True, "servers": servers, "tools": tools,
                   "guidance": "默认仅列服务；指定 server_id 读取工具列表，同时指定 tool_name 读取该工具完整 schema。",
                   "scope": "外部服务只接收调用参数；无自动历史、附件、项目根目录或凭据转发。"}
        for _, _, secret in self.active.values():
            payload = _redact_values(payload, secret)
        return payload

    async def execute(self, server_id: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if server_id not in self.configs:
            return {"ok": False, "error": "此外部 MCP 服务未启用或已删除。", "effect": "none"}
        async with self._lock(server_id):
            return await self._execute_locked(server_id, tool_name, arguments)

    async def _execute_locked(self, server_id: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        config = self.configs.get(server_id)
        if not config or not config["allow_tools"]:
            return {"ok": False, "error": "外部 MCP 服务未启用工具调用；请在设置中明确开启。", "effect": "none"}
        if not tool_allowed(config, tool_name):
            return {"ok": False, "code": "tool_not_allowed", "effect": "none",
                    "error": "此服务客户端仅允许已选搜索/研究工具。"}
        if not isinstance(arguments, dict):
            return {"ok": False, "error": "工具参数须为对象。", "effect": "none"}
        # Freeze before the first connection await: the guard must describe the
        # same arguments that reach the peer even if the caller mutates its dict.
        try:
            arguments = deepcopy(arguments)
        except Exception:
            return {"ok": False, "code": "invalid_arguments",
                    "error": "工具参数须为可复制的 JSON 对象。", "effect": "none"}
        try:
            identity = _call_identity(config, tool_name, arguments)
        except (TypeError, ValueError, OverflowError, RecursionError):
            return {"ok": False, "code": "invalid_arguments",
                    "error": "工具参数须为有限数值的 JSON 对象。", "effect": "none"}
        if not config["allow_project_data"] and _has_project_path(arguments):
            return {"ok": False, "error": "此服务未获发送或访问项目内容的许可，参数含项目路径。", "effect": "none"}
        if identity in self.uncertain_calls:
            return {"ok": False, "code": "external_call_result_unknown",
                    "error": "此前相同服务、工具和参数的调用结果未知；请先核对服务状态，不会自动重放。",
                    "effect": "none", "prior_effect": "unknown", "dispatch_blocked": True}
        if len(self.uncertain_calls) + len(self._inflight_calls) >= MAX_UNCERTAIN_CALLS:
            # Do not evict a replay guard to make room for another call whose
            # outcome could also become unknown. Shutdown is not reconciliation.
            return {"ok": False, "code": "uncertain_capacity_exceeded",
                    "error": "未知效果记录已达本进程上限；请先人工核对服务状态。",
                    "effect": "none", "dispatch_blocked": True}
        if server_id in self.errors:
            return {"ok": False, "error": self.errors[server_id], "effect": "none"}
        try:
            client = await self._ensure_locked(server_id)
        except Exception as exc:
            return {"ok": False, "error": _safe_error(exc), "effect": "none"}
        if self.configs.get(server_id) != config:
            await self._close_locked(server_id)
            return {"ok": False, "code": "configuration_changed",
                    "error": "外部 MCP 配置已改变；未派发工具调用。", "effect": "none"}
        if tool_name not in self.tools[server_id]:
            return {"ok": False, "error": "工具不在此服务目录中，请先读取 external_tools。", "effect": "none"}
        secret = self.active[server_id][2]
        # Other servers may have dispatched while this connection was opening.
        # Reserve one bounded guard slot atomically on this event loop, without
        # an await between the capacity check and insertion.
        if len(self.uncertain_calls) + len(self._inflight_calls) >= MAX_UNCERTAIN_CALLS:
            return {"ok": False, "code": "uncertain_capacity_exceeded",
                    "error": "未知效果记录已达本进程上限；请先人工核对服务状态。",
                    "effect": "none", "dispatch_blocked": True}
        try:
            reserve_call(config)
        except ValueError as exc:
            return {"ok": False, "code": "call_budget_denied", "effect": "none",
                    "error": str(exc), "dispatch_blocked": True}
        self._inflight_calls.add(identity)
        try:
            result = await client.call_tool_mcp(tool_name, arguments)
            payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
            # External credentials must not be echoed into model/history/UI.
            payload = _redact_values(payload, secret)
            is_error = (result.is_error if hasattr(result, "is_error") else result.isError)
            if is_error:
                self.uncertain_calls.add(identity)
            return {"ok": not bool(is_error), "server_id": server_id,
                    "tool_name": tool_name, "result": payload,
                    "effect": "unknown" if is_error else "completed"}
        except Exception as exc:
            self.uncertain_calls.add(identity)
            return {"ok": False, "server_id": server_id, "tool_name": tool_name,
                    "error": _safe_error(exc, secret), "effect": "unknown",
                    "guidance": "调用可能已发生效果，请先核对服务状态，勿自动重复调用。"}
        except asyncio.CancelledError:
            self.uncertain_calls.add(identity)
            raise
        finally:
            self._inflight_calls.discard(identity)

    def begin_turn(self) -> None:
        # Only pre-dispatch connection errors are retryable next turn. A new
        # conversation turn does not establish an unknown remote call's effect.
        self.errors.clear()


async def probe_server(config: dict[str, Any]) -> dict[str, Any]:
    """An explicit settings probe lists tools only and always closes its client."""
    pool = ExternalMcpPool()
    try:
        row = validate_server(config)
        await pool.sync([{**row, "enabled": True, "allow_tools": True}])
        await pool._ensure(row["id"])
        names = list(pool.tools[row["id"]])
        return {"ok": True, "count": len(names), "tools": names,
                "message": f"连接成功，发现 {len(names)} 个允许的工具；仅初始化和列目录，未执行工具，不验证业务额度。"}
    except Exception as exc:
        return {"ok": False, "message": "连接失败：" + _safe_error(exc)}
    finally:
        await pool.close()


def run_probe(config: dict[str, Any], cancelled: Any) -> dict[str, Any]:
    """Thread entry point with cooperative cancellation of a pending connection."""
    async def run():
        task = asyncio.create_task(probe_server(config))
        try:
            while not task.done():
                if cancelled.is_set():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    return {"ok": False, "message": "连接检查已取消。"}
                await asyncio.sleep(.1)
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    return asyncio.run(run())


_POOL: ExternalMcpPool | None = None


def external_pool() -> ExternalMcpPool:
    global _POOL
    if _POOL is None:
        _POOL = ExternalMcpPool()
    return _POOL


async def shutdown_external_mcp() -> None:
    global _POOL
    if _POOL is not None:
        await _POOL.close()
        _POOL = None


def exported_stdio_config(python: str) -> dict[str, Any]:
    return {"mcpServers": {"deposition-sim": {"command": python,
        "args": [str(PROJECT_ROOT / "coat_sim" / "mcp" / "server.py")], "cwd": str(PROJECT_ROOT)}}}
