"""Bounded project-tool discovery and recoverable MCP business outcomes.

No model retries, implicit writes, pathname expansion or shell execution happen
here. Unknown mutation outcomes must be reconciled, rather than dispatched twice.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable

# Live GUI observations can change outside the assistant's mutation epoch.
# The runtime still applies its independent shared total-call/token budgets.
LIVE_READ_TOOLS = frozenset({'read_window', 'list_windows', 'project_list',
    'project_inspect', 'project_resolve', 'project_editor_snapshot', 'calculation_queue_snapshot'})
STRONG_IDEMPOTENT_TOOLS = frozenset({'submit_single_source', 'submit_process_recipe',
    'submit_mask_optimization', 'submit_film_optimization', 'submit_appearance_render',
    'create_default_workflow', 'create_design_workflow', 'submit_mask_weights',
    'submit_mask_field', 'submit_point_weights', 'submit_python_script', 'request_compute_confirmation'})

MCP_IMAGE_MAX_COUNT = 8
MCP_IMAGE_TURN_MAX_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True, repr=False)
class MCPMultimodalResult:
    """Metadata plus ordered framework content, kept out of JSON fallbacks."""

    value: Any
    content: tuple[Any, ...]

    def __repr__(self) -> str:
        # Never let a diagnostic repr expose image bytes or their Base64 form.
        return f"MCPMultimodalResult(content_items={len(self.content)})"


def _text_result_value(result: Any) -> Any:
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    texts = [part.text for part in getattr(result, "content", [])
             if getattr(part, "type", None) == "text"]
    if len(texts) == 1:
        try:
            return json.loads(texts[0])
        except ValueError:
            return texts[0]
    return {"content": texts}


def _decode_text_content(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def result_value(result: Any, *, preserve_images: bool = False) -> Any:
    """Read MCP text returns, optionally retaining authorized image blocks.

    Image preservation is an explicit caller opt-in. Other tools retain the
    historical text-only behavior, and arbitrary MCP resources are never
    forwarded as image data.
    """
    if not preserve_images:
        return _text_result_value(result)

    from mcp.types import ImageContent, TextContent
    from coat_sim.agent.media import MCPImageContentError, image_content_to_binary

    blocks = list(getattr(result, "content", []) or [])
    if not any(getattr(part, "type", None) == "image" for part in blocks):
        return _text_result_value(result)

    image_blocks = [part for part in blocks
                    if isinstance(part, ImageContent) and getattr(part, "type", None) == "image"]
    if len(image_blocks) > MCP_IMAGE_MAX_COUNT:
        raise MCPImageContentError("MCP image result exceeds the 8-image limit.")

    ordered: list[Any] = []
    image_bytes = 0
    for part in blocks:
        if isinstance(part, TextContent) and getattr(part, "type", None) == "text":
            ordered.append(_decode_text_content(part.text))
        elif isinstance(part, ImageContent) and getattr(part, "type", None) == "image":
            binary = image_content_to_binary(part)
            image_bytes += len(binary.data)
            if image_bytes > MCP_IMAGE_TURN_MAX_BYTES:
                raise MCPImageContentError("MCP image result exceeds the 32 MiB limit.")
            ordered.append(binary)
        else:
            # Fail closed for malformed image claims and unexpected blocks in
            # the image-bearing read_data_file result.
            raise MCPImageContentError("MCP image result contains an unsupported content block.")

    return MCPMultimodalResult(value=_text_result_value(result), content=tuple(ordered))


def argument_errors(schema: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
    """Check the outer call envelope; the service validates domain constraints.

    This deliberately does not implement JSON Schema. It reports missing named
    arguments before a business call, without guessing aliases or coercing data.
    """
    missing = [key for key in schema.get("required", []) if key not in arguments]
    errors = ["Missing required argument: " + str(key) for key in missing]
    if schema.get("additionalProperties") is False:
        properties = schema.get("properties", {})
        errors.extend("Unknown argument: " + str(key) for key in arguments if key not in properties)
    return errors


def normalize_arguments(arguments: Any) -> tuple[dict[str, Any] | None, str]:
    """Accept an object or JSON object text, including omitted/empty objects.

    Invalid shapes become business observations instead of generic tool retry
    exceptions. No guessed field names, stringification or domain coercion.
    """
    if arguments is None:
        return {}, ""
    if isinstance(arguments, str):
        if len(arguments) > 1000000:
            return None, "arguments JSON exceeds the permitted size."
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return None, "arguments must be a JSON object, not malformed JSON text."
    if not isinstance(arguments, dict):
        return None, "arguments must be an object, for example {} for a tool without arguments."
    return arguments, ""


class ProjectGateway:
    def __init__(self, client: Any, allowed: frozenset[str] | set[str],
                 read_only: frozenset[str] | set[str], redact: Callable[[str], str],
                 shared: dict[str, Any] | None = None, read_only_mode: bool = False) -> None:
        self.read_only_mode = read_only_mode
        self.client, self.allowed, self.read_only, self.redact = client, allowed, read_only, redact
        self._tools: dict[str, dict[str, Any]] | None = None
        self._catalog_lock = asyncio.Lock()
        guard = shared if shared is not None else {}
        self._unknown: dict[str, dict[str, Any]] = guard.setdefault("unknown", {})
        self._failures: dict[str, int] = guard.setdefault("failures", {})
        self._completed: dict[str, Any] = guard.setdefault("completed", {})
        self._reads: dict[str, int] = guard.setdefault("reads", {})
        self._catalogs: dict[str, int] = guard.setdefault("catalogs", {})
        self._guard = guard

    def _reserve_image_budget(self, result: MCPMultimodalResult) -> None:
        """Charge valid image output against the run-shared, per-turn budget.

        Check both limits before updating shared state so a rejected result
        never consumes budget. There are no awaits in this method, making the
        check-and-update atomic across gateways on the same event loop.
        """
        from coat_sim.agent.media import MCPImageContentError

        images = [item for item in result.content
                  if str(getattr(item, "media_type", "")).startswith("image/")]
        image_count = len(images)
        image_bytes = sum(len(item.data) for item in images)
        used_count = int(self._guard.get("mcp_image_count", 0))
        used_bytes = int(self._guard.get("mcp_image_bytes", 0))
        if (used_count + image_count > MCP_IMAGE_MAX_COUNT
                or used_bytes + image_bytes > MCP_IMAGE_TURN_MAX_BYTES):
            raise MCPImageContentError("Assistant turn image budget exceeded (8 images / 32 MiB).")
        self._guard["mcp_image_count"] = used_count + image_count
        self._guard["mcp_image_bytes"] = used_bytes + image_bytes

    async def schemas(self) -> dict[str, dict[str, Any]]:
        async with self._catalog_lock:
            if self._tools is None:
                self._tools = {tool.name: {
                    "name": tool.name, "description": tool.description or "",
                    "input_schema": getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", {})}
                    for tool in await self.client.list_tools() if tool.name in self.allowed}
        return self._tools

    async def catalog(self, names: list[str] | None = None) -> dict[str, Any]:
        tools = await self.schemas()
        key = json.dumps(sorted(set(names or [])))
        self._catalogs[key] = self._catalogs.get(key, 0) + 1
        if self._catalogs[key] >= 3:
            return {"ok": False, "error": {"code": "catalog_already_read",
                    "message": "This unchanged catalog was already returned twice. Reuse its schemas; choose new tool_names only when needed.",
                    "effect": "none"}, "requested_tools": names or [], "execution_entry": "project_action"}
        if names:
            if len(names) > 12:
                return {"ok": False, "error": "At most 12 tool schemas can be requested together."}
            return {"tools": [tools[name] for name in dict.fromkeys(names) if name in tools],
                    "unavailable": [name for name in dict.fromkeys(names) if name not in tools]}
        return {"tools": [{"name": value["name"], "description": value["description"][:72]}
                          for value in tools.values()],
                "execution_entry": "project_action",
                "preferred_workflow": tools.get("create_default_workflow"),
                "read_only": self.allowed.issubset(self.read_only),
                "batch_schema_arguments": {"tool_names": [name for name in
                    ("input_schema", "project_editor_snapshot", "prepare_temporary_design", "submit_process_recipe")
                    if name in tools]}}

    def _error(self, name: str, code: str, detail: str, *, schema: dict[str, Any] | None = None,
               effect: str = "none", identity: str = "") -> dict[str, Any]:
        result: dict[str, Any] = {"ok": False, "tool_name": name,
            "error": {"code": code, "message": self.redact(detail)[:1800], "effect": effect,
                      "recovery": ("Do not repeat this mutation. Read its target or find_calculation_job "
                                   "using client_request_id before deciding whether any action is needed."
                                   if effect == "unknown" else
                                   "Correct the reported arguments, reuse successful results, and retry only the failed action.")}}
        if schema:
            result["input_schema"] = schema
        if identity:
            result["client_request_id"] = identity
        return result

    @staticmethod
    def _identity(name: str, arguments: dict[str, Any]) -> str:
        # A new tool_call_id must not disguise an identical unknown submission.
        # A lookup ID is its actual input, rather than a mutation dedupe token.
        stable = {key: value for key, value in arguments.items()
                  if key != "client_request_id" or name == "find_calculation_job"}
        encoded = json.dumps([name, stable], sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    async def execute(self, name: str, arguments: Any = None) -> Any:
        tools = await self.schemas()
        if self.read_only_mode and name not in self.read_only:
            return self._error(name, "discussion_read_only", "Discussion only permits read-only project tools.")
        if name not in self.allowed or name not in tools:
            return self._error(name, "unavailable_tool", "Use project_tools to select a currently allowed tool.")
        schema = tools[name]["input_schema"]
        arguments, detail = normalize_arguments(arguments)
        if arguments is None:
            return self._error(name, "invalid_arguments", detail, schema=schema)
        if self.read_only_mode:
            unknown = set(arguments) - set(schema.get("properties", {}))
            if unknown:
                return self._error(name, "invalid_arguments", "Unknown read-only argument: " + ", ".join(sorted(unknown)), schema=schema)
            if name == "read_calculation_result" and arguments.get("export_data", False) is not False:
                return self._error(name, "discussion_read_only", "Result export is unavailable in discussion.", schema=schema)
        key = self._identity(name, arguments)
        if self._failures.get(key, 0) >= 3:
            return self._error(name, "repeated_error", "The same unsuccessful arguments were already used three times. "
                               "Change the arguments or report the remaining issue; no further dispatch occurred.", schema=schema)
        errors = argument_errors(schema, arguments)
        if errors:
            self._failures[key] = self._failures.get(key, 0) + 1
            return self._error(name, "invalid_arguments", "; ".join(errors), schema=schema)
        from coat_sim.core.capabilities import CapabilityService, CapabilityError, POLICIES
        if name in POLICIES:
            try:
                # Validation has no service invocation. A rejected shared input
                # can safely report no effect before any MCP dispatch happens.
                arguments = CapabilityService(None).validate_arguments(name, arguments)
            except CapabilityError as exc:
                self._failures[key] = self._failures.get(key, 0) + 1
                return self._error(name, exc.code, exc.message, schema=schema)
            key = self._identity(name, arguments)
            if self._failures.get(key, 0) >= 3:
                return self._error(name, "repeated_error", "The same unsuccessful arguments were already used three times. "
                                   "Change the arguments or report the remaining issue; no further dispatch occurred.", schema=schema)
        if key in self._unknown:
            return {**self._unknown[key], "dispatch_blocked": True}
        completed_key = key if name in STRONG_IDEMPOTENT_TOOLS else f"{self._guard.get('epoch', 0)}:{key}"
        if name not in self.read_only and completed_key in self._completed:
            # Submission identity is strong across epochs. Ordinary writes are
            # reusable only while no intervening mutation changed project state.
            previous = self._completed[completed_key]
            if isinstance(previous, dict):
                return {**previous, "reused_result": True, "dispatch_blocked": True}
            return {"ok": True, "reused_result": True, "dispatch_blocked": True, "result": previous}
        mutation = name not in self.read_only
        poll_tools = {"calculation_job_status", "wait_calculation_job", "find_calculation_job", "design_workflow_status"}
        read_key = f"{self._guard.get('epoch', 0)}:{key}"
        if not mutation and name not in poll_tools | LIVE_READ_TOOLS and self._reads.get(read_key, 0) >= 3:
            return self._error(name, "repeated_read", "This read was already executed three times with unchanged arguments and no intervening write. "
                               "Reuse its result or select a needed page/field; no further dispatch occurred.")
        try:
            # Raw MCP outcomes preserve is_error; business errors are ordinary
            # observations and do not consume one shared generic-tool retry cap.
            raw = await self.client.call_tool_mcp(name, arguments)
        except asyncio.CancelledError:
            if mutation:
                # Cancellation stops waiting for the peer; it does not prove
                # that the peer stopped before applying a write or submission.
                # Retain the same replay barrier used for transport failures.
                self._unknown[key] = self._error(
                    name, "dispatch_cancelled",
                    "The dispatched mutation was cancelled while its effect was unknown. "
                    "Reconcile it before retrying.",
                    schema=schema, effect="unknown",
                    identity=str(arguments.get("client_request_id", "")),
                )
            raise
        except Exception as exc:
            result = self._error(name, "transport_error", str(exc), schema=schema,
                                 effect="unknown" if mutation else "none",
                                 identity=str(arguments.get("client_request_id", "")))
            if mutation:
                self._unknown[key] = result
            self._failures[key] = self._failures.get(key, 0) + 1
            return result
        from coat_sim.agent.media import MCPImageContentError
        try:
            value = result_value(raw, preserve_images=(name == "read_data_file"
                                                       and not getattr(raw, "is_error", False)))
            if isinstance(value, MCPMultimodalResult):
                self._reserve_image_budget(value)
        except MCPImageContentError as exc:
            self._failures[key] = self._failures.get(key, 0) + 1
            return self._error(name, "invalid_image_content", str(exc), schema=schema)
        if getattr(raw, "is_error", False):
            self._failures[key] = self._failures.get(key, 0) + 1
            detail = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
            from coat_sim.core.capabilities import decode_capability_error
            shared_error = decode_capability_error(name, detail)
            if shared_error is not None:
                # Peer text cannot downgrade a dispatched write to "none".
                # Unknown effects must retain the existing replay blockade,
                # even if a peer imitates a local error marker.
                effect = "unknown" if mutation else "none"
                result = self._error(name, shared_error["code"], shared_error["message"],
                                     schema=schema, effect=effect,
                                     identity=str(arguments.get("client_request_id", "")))
                if effect == "unknown":
                    self._unknown[key] = result
                return result
            # Validation and explicit precondition failures confirm no action;
            # other write errors may have happened after a partial side effect.
            # Only known service preconditions have no effect. A script's
            # output may contain those words after having written a file.
            prefix = f"error executing tool {name}: "
            message = detail.lower()
            service_detail = message[len(prefix):] if message.startswith(prefix) else message
            no_effect_tools = {"save_design", "write_data_file", "make_data_directory", "prepare_temporary_design",
                               "save_generated_design", "save_simulation_asset"}
            rejected = name in no_effect_tools and any(service_detail.startswith(code + ":") for code in (
                "already_exists", "not_found", "permission_denied", "invalid_input", "invalid_design", "forbidden_path"))
            effect = "none" if not mutation or rejected else "unknown"
            result = self._error(name, "business_error", detail, schema=schema, effect=effect,
                                 identity=str(arguments.get("client_request_id", "")))
            if effect == "unknown":
                self._unknown[key] = result
            return result
        if isinstance(value, dict) and value.get("ok") is False:
            self._failures[key] = self._failures.get(key, 0) + 1
            error = value.get("error")
            if mutation and isinstance(error, dict) and error.get("effect") == "unknown":
                self._unknown[key] = value
        elif mutation:
            self._failures.pop(key, None)
            # Release requests are idempotent by estimate ID. A pending receipt
            # is not a completed mutation: re-read after the desktop decision.
            # Keep caching prepared submits so a new model call ID cannot create
            # a second estimate/job for the same requested work.
            pending_release = (name == "request_compute_confirmation" and isinstance(value, dict)
                               and value.get("state") in ("prepared", "awaiting_confirmation", "pending", "approved"))
            self._guard["epoch"] = self._guard.get("epoch", 0) + 1
            if not pending_release:
                completed_key = key if name in STRONG_IDEMPOTENT_TOOLS else f"{self._guard['epoch']}:{key}"
                self._completed[completed_key] = value
        else:
            self._failures.pop(key, None)
            self._reads[read_key] = self._reads.get(read_key, 0) + 1
        if name == "find_calculation_job" and isinstance(value, dict):
            identity = str(arguments.get("client_request_id", ""))
            if ("job" in value and value["job"] is None) or value.get("state") == "not_found":
                resolved = [key for key, result in self._unknown.items()
                            if result.get("client_request_id") == identity]
                for key in resolved:
                    self._unknown.pop(key, None)
        return value
