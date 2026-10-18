"""ITO discovery, strict transport, and shared-core invocation contracts."""
import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from coat_sim.agent.runtime import (COORDINATOR_TOOLS, EXECUTION_TOOLS,
                                    allowed_project_tools, discussion_operation_allowed)
from coat_sim.core.ito_electrical import calculate_ito_electrical
from coat_sim.mcp import server

pytestmark = pytest.mark.contract
NAME = "ito_electrical_calculate"


def quantity(value, unit, provenance="measured"):
    return {"value": value, "unit": unit, "provenance": provenance, "source": "test specimen"}


def call(request):
    return asyncio.run(server.mcp.call_tool(NAME, {"request": request}))


def test_discovery_and_mode_permissions():
    tool = next(t for t in asyncio.run(server.mcp.list_tools()) if t.name == NAME)
    assert tool.input_schema["additionalProperties"] is False
    request = tool.input_schema["properties"]["request"]
    assert request["additionalProperties"] is False
    assert request["properties"]["thickness"]["additionalProperties"] is False
    assert request["properties"]["rectangle"]["additionalProperties"] is False
    assert tool.annotations.read_only_hint
    assert tool.annotations.idempotent_hint
    assert not tool.annotations.destructive_hint
    for mode in ("chat", "operate", "research"):
        assert NAME in allowed_project_tools(mode)
        assert NAME in allowed_project_tools(mode, background=True)
    assert discussion_operation_allowed(NAME, {"request": {}})
    assert NAME in COORDINATOR_TOOLS
    for group in ("design", "simulation", "correction"):
        assert NAME in EXECUTION_TOOLS[group]


@pytest.mark.parametrize("payload", [
    {}, {"sheet_resistance": quantity(10, "ohm/sq")},
    {"sheet_resistance": quantity(10, "ohm/sq"), "thickness": quantity(100, "nm")},
    {"sheet_resistance": quantity(10, "ohm/sq"), "resistivity": quantity(1e-4, "ohm*cm", "theoretical")},
    {"sheet_resistance": quantity(10, "ohm/sq"), "thickness": quantity(100, "nm"),
     "resistivity": quantity(2e-4, "ohm*cm")},
    {"rectangle": {"resistance": quantity(20, "ohm"), "length": quantity(2, "cm"),
                   "width": quantity(1, "cm"), "electrode_boundary": "full_width_opposite_edges",
                   "contact_assumption": "negligible_or_corrected"}, "thickness": quantity(100, "nm")},
])
def test_real_mcp_preserves_complete_core_result(payload):
    request = payload
    before = deepcopy(request)
    result = call(request)
    assert not result.is_error
    assert result.structured_content == calculate_ito_electrical(request)
    assert request == before


@pytest.mark.parametrize("payload", [
    {"unknown": 1}, {"operation": "fit"}, {"consistency_relative_tolerance": 2},
    {"thickness": quantity(True, "nm")}, {"thickness": quantity("100", "nm")},
    {"thickness": quantity(float("nan"), "nm")}, {"thickness": quantity(0, "nm")},
    {"thickness": quantity(1, "inch")}, {"thickness": quantity(1, "nm", "derived")},
    {"thickness": {**quantity(1, "nm"), "source": "   "}},
    {"thickness": {**quantity(1, "nm"), "unexpected": 1}},
    {"thickness": {**quantity(1, "nm"), "standard_uncertainty": -1}},
    {"rectangle": {}}, {"uncertainty_assumption": "correlated"},
])
def test_invalid_fields_fail_before_sdk(payload):
    request = payload
    with patch.object(MCPServer, "call_tool", new_callable=AsyncMock) as sdk:
        with pytest.raises(ToolError, match="invalid_arguments"):
            call(request)
        sdk.assert_not_called()


def test_domain_error_is_typed_without_crashing_server():
    from coat_sim.core import ito_electrical
    with patch.object(ito_electrical, "calculate_ito_electrical", side_effect=ValueError("invalid combination")):
        with pytest.raises(ToolError, match="invalid_arguments"):
            server.ito_electrical_calculate({})
        with pytest.raises(ToolError, match="invalid_arguments"):
            call({})
    assert not call({}).is_error
