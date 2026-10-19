"""Durable tool observations with bounded model views and explicit paging.

Projection never edits user messages or destroys a tool's original observation.
The size limits below are project conventions for interaction cost control.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from coat_sim.core.atomic_io import replace_with_retry


class TaskLimitReached(RuntimeError):
    """A recoverable agent boundary; submitted domain jobs keep running."""


def task_limits(config: dict[str, Any], mode: str) -> dict[str, int | None]:
    """Optional user task ceilings; None means no per-task cumulative cap."""
    if mode not in {"chat", "operate", "research"}:
        raise ValueError("Unknown assistant task mode")
    fields = (("requests", "assistant_task_max_requests", 256),
              ("input_tokens", "assistant_task_max_input_tokens", 8000000),
              ("total_tokens", "assistant_task_max_total_tokens", 10000000),
              ("tool_calls", "assistant_task_max_tool_calls", 1024),
              ("delegations", "assistant_task_max_delegations", 256))
    result = {}
    for key, setting, maximum in fields:
        value = config.get(setting)
        result[key] = min(maximum, value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
    return result


def _json(value: Any) -> str:
    def convert(item: Any) -> Any:
        if hasattr(item, "tolist"):
            return item.tolist()
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json")
        return str(item)
    return json.dumps(value, ensure_ascii=False, default=convert, separators=(",", ":"))


def _overview(value: Any, depth: int = 0, key: str = "") -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value if len(value) <= 1800 else value[:1800] + "… [read_archived_tool_result for the remainder]"
    if isinstance(value, dict):
        if depth >= 5:
            return {"type": "object", "keys": list(value)[:24], "key_count": len(value)}
        return {str(k): _overview(v, depth + 1, str(k)) for k, v in list(value.items())[:48]}
    if isinstance(value, (list, tuple)):
        # Preserve small argument/schema lists. Numerical fields and per-layer
        # settings stay local even if a grid or process happens to be small.
        if key in {"grid", "field", "field_nm", "step_results", "steps", "vertices", "points"}:
            return {"type": "array", "count": len(value), "detail": "available in archived result"}
        if len(value) <= 8 and depth < 5:
            return [_overview(v, depth + 1) for v in value]
        return {"type": "array", "count": len(value), "preview": [_overview(v, depth + 1) for v in value[:2]]}
    return _overview(json.loads(_json(value)), depth, key)


_SCHEMA_ANNOTATIONS = frozenset({"title", "description", "examples", "$comment"})
_SCHEMA_MAPS = frozenset({"properties", "patternProperties", "$defs", "definitions", "dependentSchemas",
                          "dependentRequired", "dependencies"})


def _compact_schema(value: Any) -> Any:
    """Remove optional annotations, preserving every validation branch.

    Property and definition names are user data, including names such as
    'description'. Likewise default/enum/const values are kept verbatim.
    """
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in _SCHEMA_ANNOTATIONS:
                continue
            if key in _SCHEMA_MAPS and isinstance(item, dict):
                result[key] = {name: _compact_schema(branch) for name, branch in item.items()}
            elif key in {"default", "enum", "const", "discriminator"}:
                result[key] = item
            else:
                result[key] = _compact_schema(item)
        return result
    if isinstance(value, list):
        return [_compact_schema(item) for item in value]
    return value


def _schema_paths(value: Any, pointer: str = "") -> list[tuple[str, Any]]:
    paths = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = pointer + "/" + str(key).replace("~", "~0").replace("/", "~1")
            if key in {"input_schema", "schema"} and isinstance(item, (dict, bool)):
                paths.append((child, item))
            else:
                paths.extend(_schema_paths(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            paths.extend(_schema_paths(item, pointer + "/" + str(index)))
    return paths


def _contract_transform(value: Any, *, result_id: str = "", pointer: str = "") -> Any:
    """Compact complete schemas, or provide explicit exact-schema references."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            child = pointer + "/" + str(key).replace("~", "~0").replace("/", "~1")
            if key in {"input_schema", "schema"} and isinstance(item, (dict, bool)):
                if result_id:
                    reference = {"result_id": result_id, "pointer": child, "mode": "schema",
                                 "read_tool": "read_archived_tool_result", "complete_schema_in_archive": True}
                    if isinstance(item, dict):
                        reference["required"] = item.get("required", [])
                        reference["argument_names"] = list(item.get("properties", {}))
                        reference["schema_keys"] = list(item)
                    result[str(key) + "_read"] = reference
                else:
                    result[key] = _compact_schema(item)
            else:
                result[key] = _contract_transform(item, result_id=result_id, pointer=child)
        return result
    if isinstance(value, list):
        return [_contract_transform(item, result_id=result_id, pointer=pointer + "/" + str(index))
                for index, item in enumerate(value)]
    return value


class ToolResultArchive:
    def __init__(self, directory: Path, *, max_chars: int = 12000) -> None:
        self.directory, self.max_chars = directory, max_chars

    def _persist(self, value: Any) -> tuple[str, int]:
        encoded = _json(value)
        identity = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / (identity + ".json")
        if not target.exists():
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".tool-", suffix=".json",
                                                  dir=self.directory, delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(encoded)
                replace_with_retry(temporary, target)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return identity, len(encoded)

    def project(self, value: Any, *, tool_name: str = "", force: bool = False) -> Any:
        # Explicit bounded pages are already a deliberate detail request.
        if tool_name == "read_archived_tool_result":
            return value
        if isinstance(value, dict) and isinstance(value.get("archived_result"), dict):
            old_reference = value["archived_result"]
            if old_reference.get("projection_version", 1) < 2 and (
                    tool_name in {"input_schema", "project_tools"} or _schema_paths(value)):
                result_id = old_reference.get("result_id", "")
                if isinstance(result_id, str) and re.fullmatch(r"[0-9a-f]{64}", result_id):
                    try:
                        original = json.loads((self.directory / (result_id + ".json")).read_text(encoding="utf-8"))
                        if _schema_paths(original):
                            return self.project(original, tool_name=tool_name, force=True)
                    except (OSError, ValueError):
                        pass
            return value
        encoded = _json(value)
        bulky = isinstance(value, dict) and any(k in value for k in ("grid", "step_results", "process"))
        if not force and not bulky and len(encoded) <= self.max_chars:
            return value
        identity, chars = self._persist(value)
        contracts = _schema_paths(value)
        if contracts:
            schema_budget = self.max_chars - 1200
            view = _contract_transform(value)
            if len(_json(view)) > schema_budget:
                # A large schema is not replaced by a shape guess. Each exact
                # contract gets its own pointer; the model selects what it needs.
                view = _contract_transform(value, result_id=identity)
                if len(_json(view)) > schema_budget:
                    view = {"contract_pages": [{"pointer": pointer, "mode": "schema",
                             "read_tool": "read_archived_tool_result"} for pointer, _ in contracts[:50]],
                            "total_contracts": len(contracts),
                            "note": "Select a contract pointer; schema branches have not been truncated or guessed."}
                    if len(_json(view)) > schema_budget:
                        view = {"schema_read": {"result_id": identity, "pointer": "", "mode": "schema",
                                "read_tool": "read_archived_tool_result"},
                                "note": "Select a smaller contract page; no schema validation fields have been truncated."}
            if not isinstance(view, dict):
                view = {"value": view}
            view["schema_annotations_omitted"] = sorted(_SCHEMA_ANNOTATIONS)
        else:
            view = _overview(value)
        if not contracts and len(_json(view)) > self.max_chars:
            # The fallback keeps a bounded top-level preview and all original
            # values are reachable by JSON pointer, rather than erased.
            view = {"keys": list(value) if isinstance(value, dict) else [],
                    "preview": _json(view)[:self.max_chars - 1800]}
        reference = {"result_id": identity, "original_chars": chars,
                     "read_tool": "read_archived_tool_result", "pointer": "",
                     "projection_version": 2,
                     "note": "Full observation retained locally; request only needed fields/pages."}
        return {**view, "archived_result": reference} if isinstance(view, dict) else {"value": view, "archived_result": reference}

    def project_history(self, messages: list[Any]) -> list[Any]:
        from pydantic_ai.messages import ToolCallPart, ToolReturnPart
        settled = {part.tool_call_id for message in messages for part in message.parts
                   if isinstance(part, ToolReturnPart)}
        rewritten = []
        for message in messages:
            parts = []
            changed = False
            for part in message.parts:
                if isinstance(part, ToolReturnPart):
                    value = self.project(part.content, tool_name=part.tool_name)
                    if value is not part.content:
                        part = replace(part, content=value)
                        changed = True
                elif isinstance(part, ToolCallPart) and part.tool_call_id in settled:
                    # Old successful actions can contain entire layer stacks or
                    # generated scripts. Keep their exact original arguments in
                    # the archive before showing a concise historic observation.
                    # Unsettled tool effects must retain their original identity.
                    original = part.args
                    try:
                        arguments = json.loads(original) if isinstance(original, str) else original
                    except ValueError:
                        arguments = original
                    value = self.project(arguments, tool_name=part.tool_name)
                    if value is not arguments:
                        part = replace(part, args=value)
                        changed = True
                parts.append(part)
            rewritten.append(replace(message, parts=parts) if changed else message)
        return rewritten

    def read(self, result_id: str, pointer: str = "", offset: int = 0,
             limit: int = 20, mode: str = "detail") -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{64}", result_id):
            return {"ok": False, "error": "Invalid archived result_id."}
        if isinstance(offset, bool) or isinstance(limit, bool) or offset < 0 or not 1 <= limit <= 50:
            return {"ok": False, "error": "offset must be nonnegative; limit must be 1..50."}
        if mode not in {"detail", "schema"}:
            return {"ok": False, "error": "mode must be detail or schema."}
        try:
            value = json.loads((self.directory / (result_id + ".json")).read_text(encoding="utf-8"))
            if pointer and not pointer.startswith("/"):
                raise ValueError("pointer must use JSON Pointer, for example /metadata/step_results")
            for token in pointer.split("/")[1:]:
                token = token.replace("~1", "/").replace("~0", "~")
                value = value[int(token)] if isinstance(value, list) else value[token]
            if isinstance(value, dict):
                keys = list(value)[offset:offset + limit]
                page = {key: value[key] for key in keys}
                total = len(value)
            elif isinstance(value, list):
                page, total = value[offset:offset + limit], len(value)
            elif isinstance(value, str):
                # Text pages use characters; no unbounded content read.
                page, total = value[offset:offset + min(limit * 200, 10000)], len(value)
            else:
                page, total = value, 1
            clipped = len(_json(page)) > self.max_chars
            schema_page = (mode == "schema" or bool(_schema_paths(page)) or
                           any(token in {"input_schema", "schema"} for token in pointer.split("/")))
            if schema_page:
                if mode == "schema":
                    if pointer.split("/")[-1] in _SCHEMA_MAPS and isinstance(page, dict):
                        page = {name: _compact_schema(branch) for name, branch in page.items()}
                    else:
                        page = _compact_schema(page)
                if len(_json(page)) > self.max_chars:
                    branches = []
                    iterable = page.items() if isinstance(page, dict) else enumerate(page) if isinstance(page, list) else []
                    for key, item in iterable:
                        branch_pointer = pointer + "/" + str(key).replace("~", "~0").replace("/", "~1")
                        if isinstance(value, list):
                            branch_pointer = pointer + "/" + str(offset + int(key))
                        branches.append({"pointer": branch_pointer, "type": type(item).__name__,
                                         "keys": list(item)[:8] if isinstance(item, dict) else None,
                                         "key_count": len(item) if isinstance(item, dict) else None})
                    return {"ok": True, "result_id": result_id, "pointer": pointer, "mode": mode,
                            "offset": offset, "total": total, "data": None, "complete_schema": False,
                            "branches": branches, "next_offset": offset + limit if offset + limit < total else None,
                            "note": "The selected contract is too large. Select a deeper branch pointer or fewer entries; no schema validation fields were truncated."}
                return {"ok": True, "result_id": result_id, "pointer": pointer, "mode": mode,
                        "offset": offset, "total": total, "next_offset": offset + limit if offset + limit < total else None,
                        "data": page, "projected": False, "schema_annotations_omitted": sorted(_SCHEMA_ANNOTATIONS) if mode == "schema" else []}
            if clipped:
                page = _overview(page)
            if len(_json(page)) > self.max_chars:
                page = {"preview": _json(page)[:self.max_chars - 1000], "note": "Select a deeper pointer or a smaller page."}
            consumed = len(page) if isinstance(value, str) and isinstance(page, str) else min(limit, max(0, total - offset))
            return {"ok": True, "result_id": result_id, "pointer": pointer, "offset": offset,
                    "total": total, "next_offset": offset + consumed if offset + consumed < total else None,
                    "data": page, "projected": clipped}
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            return {"ok": False, "error": str(exc)[:1000], "result_id": result_id}
