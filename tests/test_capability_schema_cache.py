"""Contract tests for bounded, service-independent capability parameter models."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import contextmanager
import inspect
from pathlib import Path
import tempfile
from threading import Barrier
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from typing import Any, Literal, get_args, get_origin, get_type_hints
from unittest.mock import patch

from pydantic import BaseModel, ConfigDict, create_model

from coat_sim.agent.runtime import EXECUTION_TOOLS, MAIN_TOOLS, READ_TOOLS
from coat_sim.core import capabilities as capabilities_module
from coat_sim.core.capabilities import (
    CapabilityError,
    CapabilityService,
    POLICIES,
    decode_capability_error,
)
from coat_sim.mcp import server
from mcp.server.mcpserver.exceptions import ToolError


def _service(root: str = "C:/fixture/project"):
    return SimpleNamespace(root=root)


@contextmanager
def _isolated_model_cache():
    cache = capabilities_module._PARAMETER_MODEL_CACHE
    lock = capabilities_module._PARAMETER_MODEL_CACHE_LOCK
    with lock:
        saved = OrderedDict(cache)
        cache.clear()
    try:
        yield cache
    finally:
        with lock:
            cache.clear()
            cache.update(saved)


def _legacy_uncached_model(self, operation):
    """Reproduce the former per-instance constructor for catalog parity checks."""
    declaration = getattr(type(self), "_op_" + operation)
    model = self._models.get(operation)
    if model is None or self._model_declarations.get(operation) is not declaration:
        method = getattr(self, "_op_" + operation)
        hints = get_type_hints(method)
        fields = {
            name: (hints.get(name, Any),
                   ... if argument.default is inspect.Parameter.empty else argument.default)
            for name, argument in inspect.signature(method).parameters.items()
        }
        model = create_model("Capability_" + operation,
                             __config__=ConfigDict(extra="forbid", strict=True), **fields)
        self._models[operation] = model
        self._model_declarations[operation] = declaration
    return model


_UNSET = inspect.Parameter.empty


def _tool_schema(tool: Any) -> dict[str, Any]:
    if isinstance(tool, dict):
        value = tool
    elif hasattr(tool, "model_dump"):
        value = tool.model_dump(by_alias=True)
    else:
        value = {key: getattr(tool, key) for key in ("name", "input_schema", "inputSchema")
                 if hasattr(tool, key)}
    return value.get("input_schema") or value.get("inputSchema") or {}


def _sample_value(annotation: Any, default: Any) -> Any:
    if default is not _UNSET:
        return default
    origin = get_origin(annotation)
    if origin is Literal:
        return get_args(annotation)[0]
    if origin is not None:
        args = get_args(annotation)
        if origin is list:
            return []
        if origin is dict:
            return {}
        if any(arg is type(None) for arg in args):
            return None
    if annotation is str:
        return "fixture"
    if annotation is int:
        return 0
    if annotation is float:
        return 1.0
    if annotation is bool:
        return False
    if annotation in (dict, Any):
        return {}
    if annotation is list:
        return []
    return None


def _sample_arguments(function: Any) -> dict[str, Any]:
    hints = get_type_hints(function)
    return {
        parameter.name: _sample_value(hints.get(parameter.name, parameter.annotation),
                                      parameter.default)
        for parameter in inspect.signature(function).parameters.values()
    }


async def _call_registered(name: str, arguments: dict[str, Any]):
    try:
        return await server.mcp.call_tool(name, arguments)
    except ToolError as exc:
        return SimpleNamespace(is_error=True, structured_content=None,
            content=[SimpleNamespace(type="text", text=str(exc))])


def _mcp_error_text(reply: Any) -> str:
    texts = [part.text for part in getattr(reply, "content", ()) or ()
             if getattr(part, "type", None) == "text"]
    return "\n".join(texts) or str(getattr(reply, "structured_content", None))


class CapabilitySchemaCacheTests(unittest.TestCase):
    def test_shared_catalog_signatures_schemas_and_mcp_dispatch_match(self):
        with tempfile.TemporaryDirectory(prefix="capability-schema-mcp-") as temporary_root:
            capabilities = CapabilityService(_service(str(Path(temporary_root).resolve())))
            catalog = capabilities.catalog()
            operations = {item["name"]: item for item in catalog["operations"]}
            names = set(operations)
            implementation_names = {
                name.removeprefix("_op_") for name in dir(type(capabilities))
                if name.startswith("_op_")
            }
            self.assertEqual(names, set(POLICIES))
            self.assertEqual(names, implementation_names)
            self.assertEqual(len(names), 22)

            tools = asyncio.run(server.mcp.list_tools())
            tools_by_name = {tool.name: tool for tool in tools}
            self.assertTrue(names <= set(tools_by_name))
            self.assertTrue(names <= MAIN_TOOLS)
            read_names = {name for name, policy in POLICIES.items()
                          if policy.effect == "read"}
            self.assertTrue(read_names <= READ_TOOLS)
            self.assertTrue({"save_film_design", "simulate_film_design",
                             "cancel_calculation_job"} <= MAIN_TOOLS)
            self.assertFalse({"save_film_design", "simulate_film_design",
                              "cancel_calculation_job"} & READ_TOOLS)
            for group in ("design", "simulation", "correction", "windows"):
                self.assertTrue(set(EXECUTION_TOOLS[group]) <= MAIN_TOOLS)

            for name in sorted(names):
                with self.subTest(operation=name):
                    function = getattr(server, name)
                    core_method = getattr(capabilities, "_op_" + name)
                    core_parameters = list(inspect.signature(core_method).parameters.values())
                    mcp_parameters = list(inspect.signature(function).parameters.values())
                    self.assertEqual([item.name for item in mcp_parameters],
                                     [item.name for item in core_parameters])
                    self.assertEqual([item.default for item in mcp_parameters],
                                     [item.default for item in core_parameters])
                    self.assertEqual(_tool_schema(tools_by_name[name]),
                                     operations[name]["input_schema"])

            for name in sorted(names):
                arguments = _sample_arguments(getattr(capabilities, "_op_" + name))
                with self.subTest(mcp_dispatch=name), patch.object(
                        server, "_capability_call", return_value={"ok": True}) as dispatch:
                    reply = asyncio.run(_call_registered(name, arguments))
                    self.assertFalse(getattr(reply, "is_error", False), _mcp_error_text(reply))
                    dispatch.assert_called_once_with(name, arguments)

    def test_mcp_shared_adapter_rejects_coercion_unknown_fields_and_bad_markers(self):
        with patch.object(server, "_capability_call") as dispatch:
            for arguments in (
                {"path": "data/design.cfilm", "data": {}, "overwrite": "false"},
                {"path": "data/design.cfilm", "data": {}, "overwrite": 1},
                {"path": "data/design.cfilm", "data": {}, "extra": True},
            ):
                with self.subTest(arguments=arguments):
                    reply = asyncio.run(_call_registered("save_film_design", arguments))
                    self.assertTrue(getattr(reply, "is_error", False))
                    decoded = decode_capability_error(
                        "save_film_design", _mcp_error_text(reply))
                    self.assertIsNotNone(decoded)
                    self.assertEqual(decoded["code"], "invalid_arguments")
            dispatch.assert_not_called()

        malformed = (
            "not json\n[coat_capability_error]{}",
            '\n[coat_capability_error]{"code":"invalid_request","message":"x","effect":[]} ',
        )
        for detail in malformed:
            self.assertIsNone(decode_capability_error("save_film_design", detail))

    def test_cache_shares_only_model_classes_across_roots_and_schedulers(self):
        with _isolated_model_cache() as cache:
            first_service, second_service = _service("C:/one"), _service("D:/two")
            first_scheduler, second_scheduler = object(), object()
            first = CapabilityService(first_service, scheduler=first_scheduler)
            second = CapabilityService(second_service, scheduler=second_scheduler)

            first_model = first._model("calculation_job_status")
            second_model = second._model("calculation_job_status")

            self.assertIs(first_model, second_model)
            self.assertIs(first.service, first_service)
            self.assertIs(second.service, second_service)
            self.assertIs(first.scheduler, first_scheduler)
            self.assertIs(second.scheduler, second_scheduler)
            self.assertIsNot(first.scheduler, second.scheduler)
            self.assertTrue(cache)
            self.assertTrue(all(issubclass(model, BaseModel) for model in cache.values()))
            self.assertTrue(all(not hasattr(model, "root") for model in cache.values()))

    def test_strict_boolean_and_unknown_field_rejections_are_unchanged(self):
        bad_arguments = (
            {"path": "fixture/design.cfilm", "data": {}, "overwrite": "false"},
            {"path": "fixture/design.cfilm", "data": {}, "overwrite": 1},
            {"path": "fixture/design.cfilm", "data": {}, "unexpected": True},
        )
        with _isolated_model_cache():
            for arguments in bad_arguments:
                with self.subTest(arguments=arguments):
                    with self.assertRaises(CapabilityError) as caught:
                        CapabilityService(_service()).validate_arguments(
                            "save_film_design", arguments
                        )
                    self.assertEqual(caught.exception.code, "invalid_arguments")

        with tempfile.TemporaryDirectory(prefix="capability-schema-call-") as temporary_root:
            calls = []

            def record_call(*_args, **_kwargs):
                calls.append(True)

            service = SimpleNamespace(
                root=str(Path(temporary_root).resolve()),
                save_film_design=record_call,
                load_film_design=record_call,
                validate_film_design=record_call,
            )
            capabilities = CapabilityService(service)
            invalid_calls: tuple[tuple[str, dict[str, Any], str], ...] = (
                ("save_film_design", {
                    "path": "fixture/design.cfilm", "data": {}, "overwrite": "false",
                }, "invalid_arguments"),
                ("save_film_design", {
                    "path": "fixture/design.cfilm", "data": {}, "overwrite": 1,
                }, "invalid_arguments"),
                ("save_film_design", {
                    "path": "fixture/design.cfilm", "data": {}, "extra": True,
                }, "invalid_arguments"),
                ("load_film_design", {}, "invalid_arguments"),
                ("validate_film_design", {"data": {"bad": float("nan")}},
                 "invalid_arguments"),
                ("not_registered", {}, "unavailable_operation"),
            )
            for operation, call_arguments, code in invalid_calls:
                with self.subTest(operation=operation, arguments=call_arguments):
                    with self.assertRaises(CapabilityError) as caught:
                        capabilities.call(operation, call_arguments)
                    self.assertEqual(caught.exception.code, code)
            self.assertEqual(calls, [])

    def test_cached_and_uncached_full_catalogs_match_and_schema_mutation_is_isolated(self):
        with _isolated_model_cache():
            with patch.object(CapabilityService, "_model", _legacy_uncached_model):
                uncached_catalog = CapabilityService(_service()).catalog()

            service = CapabilityService(_service())
            first_catalog = service.catalog()
            self.assertEqual(first_catalog, uncached_catalog)
            self.assertTrue(any(
                operation["name"] == "calculation_job_status" and "route" in operation
                for operation in first_catalog["operations"]
            ))

            save = next(item for item in first_catalog["operations"]
                        if item["name"] == "save_film_design")
            save["input_schema"]["properties"]["overwrite"]["type"] = "mutated"
            next_catalog = service.catalog()

            self.assertEqual(next_catalog, uncached_catalog)
            self.assertIsNot(first_catalog, next_catalog)
            self.assertIsNot(first_catalog["operations"], next_catalog["operations"])

    def test_subclasses_with_different_signatures_get_distinct_models(self):
        class IdentifierReader(CapabilityService):
            def _op_read_material(self, identifier: str):
                return identifier

        class PathReader(CapabilityService):
            def _op_read_material(self, path: str, include_metadata: bool = False):
                return path, include_metadata

        with _isolated_model_cache():
            identifier_model = IdentifierReader(_service())._model("read_material")
            path_model = PathReader(_service())._model("read_material")

            self.assertIsNot(identifier_model, path_model)
            self.assertEqual(set(identifier_model.model_fields), {"identifier"})
            self.assertEqual(set(path_model.model_fields), {"path", "include_metadata"})

    def test_replacing_a_declaration_invalidates_the_instance_front_cache(self):
        def replacement(self, path: str, detail: str = "summary"):
            return path, detail

        with _isolated_model_cache():
            # Rebuild the original model inside this isolated cache.
            service = CapabilityService(_service())
            original = service._model("read_material")
            with patch.object(CapabilityService, "_op_read_material", replacement):
                replaced = service._model("read_material")
                self.assertIsNot(original, replaced)
                self.assertEqual(set(replaced.model_fields), {"path", "detail"})

            restored = service._model("read_material")
            self.assertIs(original, restored)

    def test_concurrent_services_build_a_model_once(self):
        with _isolated_model_cache():
            worker_count = 16
            barrier = Barrier(worker_count)
            original_create_model = capabilities_module.create_model

            def create_counted(*args, **kwargs):
                return original_create_model(*args, **kwargs)

            with patch.object(capabilities_module, "create_model", side_effect=create_counted) as spy:
                def build(_index):
                    service = CapabilityService(_service())
                    barrier.wait(timeout=10)
                    return service._model("save_film_design")

                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                    models = list(executor.map(build, range(worker_count)))

            self.assertEqual(spy.call_count, 1)
            self.assertTrue(all(model is models[0] for model in models))

    def test_cache_is_lru_bounded_and_declaration_identity_separates_entries(self):
        maxsize = capabilities_module._PARAMETER_MODEL_CACHE_MAXSIZE
        with _isolated_model_cache() as cache:
            original_create_model = capabilities_module.create_model

            with patch.object(capabilities_module, "create_model",
                              wraps=original_create_model) as spy:
                declarations = []
                for version in range(maxsize + 7):
                    def read_material(self, identifier: str, version: int = version):
                        return identifier, version

                    subclass = type(
                        "VersionedReader" + str(version),
                        (CapabilityService,),
                        {"_op_read_material": read_material},
                    )
                    declarations.append(subclass)
                    model = subclass(_service())._model("read_material")
                    self.assertEqual(model.model_fields["version"].default, version)

                self.assertEqual(spy.call_count, maxsize + 7)
                self.assertEqual(len(cache), maxsize)

                # The least-recently-used first declaration was evicted.
                declarations[0](_service())._model("read_material")
                self.assertEqual(spy.call_count, maxsize + 8)
                self.assertEqual(len(cache), maxsize)


if __name__ == "__main__":
    unittest.main()
