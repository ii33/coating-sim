"""Schema 5 freezes the hybrid thickness-reduction decision across producers."""
from copy import deepcopy
from dataclasses import replace
import asyncio
import json

import pytest

from coat_sim.core.api import CoatService
from coat_sim.core.capabilities import CapabilityError
from coat_sim.core.film_auto_synthesis import _validated, freeze_film_synthesis_request
from coat_sim.core.film_optimization import FilmOptimizationRequest
from coat_sim.core.film_synthesis_io import (
    SCHEMA_VERSION, FilmSynthesisPayloadError, film_synthesis_request_from_payload,
    film_synthesis_request_to_payload, prepare_film_synthesis_payload,
)
from coat_sim.core.film_synthesis_jobs import (
    build_job_payload, canonical_parameters, deserialize_job_payload, is_synthesis_result,
)
from coat_sim.core.mcp_contracts import (
    FILM_SYNTHESIS_PLAN_INPUT_SCHEMA, FILM_SYNTHESIS_SUBMIT_INPUT_SCHEMA,
    validate_mcp_arguments,
)
from coat_sim.scheduler.jobs import JobSpec
from coat_sim.ui.film_synthesis_quick_adapter import (
    build_synthesis_request, synthesis_options, synthesis_spec,
)
from tests.test_film_auto_synthesis import setup_problem
from tests.test_film_synthesis_cli import _run_cli
from tests.test_film_synthesis_jobs import _request

pytestmark = pytest.mark.contract


def test_new_core_and_gui_requests_default_to_explicit_true():
    _service, core = setup_problem()
    assert core.enable_thickness_reduction is True
    base = FilmOptimizationRequest(core.environment, 7, (), core.objectives)
    gui = build_synthesis_request(base, core.candidate_materials, {})
    assert gui.enable_thickness_reduction is True
    assert synthesis_options({})["enable_thickness_reduction"] is True


@pytest.mark.parametrize("enabled", [True, False])
def test_freeze_json_plan_and_durable_job_preserve_boolean(enabled):
    request = freeze_film_synthesis_request(None, replace(_request(), enable_thickness_reduction=enabled))
    payload = json.loads(json.dumps(film_synthesis_request_to_payload("reduction", request)))
    assert SCHEMA_VERSION == payload["schema_version"] == 5
    assert payload["request"]["enable_thickness_reduction"] is enabled
    request_id, restored, _ = film_synthesis_request_from_payload(payload)
    assert request_id == "reduction" and restored == request
    planned, metadata = prepare_film_synthesis_payload(None, payload)
    assert planned == payload and metadata == ()
    job = build_job_payload(canonical_parameters(None, {"payload": planned}))
    assert job["schema_version"] == job["request"]["schema_version"] == 5
    assert deserialize_job_payload(job)[1] == request
    spec = JobSpec.parse_frozen(synthesis_spec(None, request)).to_spec()
    assert deserialize_job_payload(spec.parameters["payload"])[1].enable_thickness_reduction is enabled


@pytest.mark.parametrize("value", [None, 0, 1, "false", "true", [], {}])
def test_boolean_never_coerces_at_core_gui_wire_or_mcp_boundary(value):
    request = _request()
    with pytest.raises(ValueError, match="enable_thickness_reduction"):
        _validated(replace(request, enable_thickness_reduction=value))
    with pytest.raises(ValueError, match="enable_thickness_reduction"):
        synthesis_options({"enable_thickness_reduction": value})
    payload = film_synthesis_request_to_payload("invalid-reduction", request)
    payload["request"]["enable_thickness_reduction"] = value
    with pytest.raises(FilmSynthesisPayloadError, match="enable_thickness_reduction"):
        film_synthesis_request_from_payload(payload)
    with pytest.raises(CapabilityError):
        validate_mcp_arguments(FILM_SYNTHESIS_PLAN_INPUT_SCHEMA, {"payload": payload})


@pytest.mark.parametrize("schema", [FILM_SYNTHESIS_PLAN_INPUT_SCHEMA, FILM_SYNTHESIS_SUBMIT_INPUT_SCHEMA])
@pytest.mark.parametrize("enabled", [True, False])
def test_mcp_discovery_and_validation_expose_strict_boolean(schema, enabled):
    payload = film_synthesis_request_to_payload("mcp-reduction", replace(_request(), enable_thickness_reduction=enabled))
    field = schema["properties"]["payload"]["properties"]["request"]["properties"]["enable_thickness_reduction"]
    assert field["type"] == "boolean" and field["default"] is True
    arguments = {"payload": payload}
    assert validate_mcp_arguments(schema, arguments) is arguments
    del payload["request"]["enable_thickness_reduction"]
    with pytest.raises(CapabilityError):
        validate_mcp_arguments(schema, arguments)
    with pytest.raises(FilmSynthesisPayloadError, match="every field"):
        film_synthesis_request_from_payload(payload)


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_old_request_rejected_before_planning_estimation_and_queueing(tmp_path, version):
    payload = film_synthesis_request_to_payload("old-reduction", _request())
    payload["schema_version"] = version
    original = deepcopy(payload)
    for action in (
        lambda: film_synthesis_request_from_payload(payload),
        lambda: prepare_film_synthesis_payload(None, payload),
        lambda: canonical_parameters(None, {"payload": payload}),
        lambda: build_job_payload({"payload": payload, "physical_structure_metadata": []}),
        lambda: CoatService(tmp_path).estimate_compute_task("film_synthesis", {"payload": payload}),
    ):
        with pytest.raises(FilmSynthesisPayloadError, match="current schema 5.*no migration"):
            action()
    assert payload == original


@pytest.mark.parametrize("enabled", [True, False])
def test_registered_mcp_plan_keeps_request_choice(monkeypatch, enabled):
    from coat_sim.mcp import server
    monkeypatch.setattr(server, "service", None)
    payload = film_synthesis_request_to_payload("plan-reduction", replace(_request(), enable_thickness_reduction=enabled))

    async def plan():
        reply = await server.mcp.call_tool("plan_film_synthesis", {"payload": payload})
        content = getattr(reply, "structured_content", None)
        return content if content is not None else reply.structuredContent

    response = asyncio.run(plan())
    assert response["ok"]
    assert response["request"] == payload


@pytest.mark.parametrize("enabled", [True, False])
def test_cli_result_and_replay_keep_frozen_boolean_and_reject_v2(tmp_path, enabled):
    source, output = tmp_path / "request.json", tmp_path / "result.json"
    payload = film_synthesis_request_to_payload("cli-reduction", replace(_request(), enable_thickness_reduction=enabled))
    source.write_text(json.dumps(payload), encoding="utf-8")
    completed = _run_cli(source, output)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["schema_version"] == 5
    assert result["result"]["frozen_request"]["enable_thickness_reduction"] is enabled
    assert is_synthesis_result(result)
    old_result = deepcopy(result)
    old_result["schema_version"] = 2
    assert not is_synthesis_result(old_result)
    payload["schema_version"] = 2
    source.write_text(json.dumps(payload), encoding="utf-8")
    rejected_output = tmp_path / "old-result.json"
    rejected = _run_cli(source, rejected_output)
    assert rejected.returncode != 0
    assert "current schema 5" in rejected.stdout + rejected.stderr
    assert not rejected_output.exists()


@pytest.mark.parametrize("value", [None, 0, "false"])
def test_cloud_bundle_preflight_rejects_non_boolean_and_does_not_upgrade(value):
    from tools.run_film_synthesis_cloud import REQUEST_CONTRACT_VERSION, _assert_current_request_shape
    assert REQUEST_CONTRACT_VERSION == SCHEMA_VERSION
    payload = film_synthesis_request_to_payload("cloud-reduction", _request())
    request = deepcopy(payload["request"])
    request["enable_thickness_reduction"] = value
    with pytest.raises(ValueError, match="explicit enable_thickness_reduction boolean"):
        _assert_current_request_shape(request)
    del request["enable_thickness_reduction"]
    with pytest.raises(ValueError, match="explicit enable_thickness_reduction boolean"):
        _assert_current_request_shape(request)


@pytest.mark.parametrize("enabled", [True, False])
def test_current_benchmark_export_preserves_explicit_choice(tmp_path, enabled):
    from tests.test_film_synthesis_benchmark_runner import small_suite
    from tools.export_film_synthesis_cloud import build_bundle
    from tools.run_film_synthesis_benchmarks import ROOT
    bundle = build_bundle(small_suite(tmp_path, enable_thickness_reduction=enabled),
        case="tiny_production_contract", seed=0, workers=1, repo_root=ROOT)
    assert bundle["entries"][0]["request"]["enable_thickness_reduction"] is enabled
