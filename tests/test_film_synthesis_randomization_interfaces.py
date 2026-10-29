"""One seed resolution across portable plans, estimates, queueing and replay."""
from dataclasses import replace
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from coat_sim.core.api import CoatService
from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
from coat_sim.core.film_synthesis_io import (
    FilmSynthesisPayloadError, film_synthesis_request_from_payload,
    film_synthesis_request_to_payload, film_synthesis_result_to_payload,
    prepare_film_synthesis_payload,
)
from coat_sim.core.film_synthesis_jobs import (
    build_job_payload, canonical_parameters, deserialize_job_payload,
)
from tests.test_film_synthesis_cli import _run_cli
from tests.test_film_synthesis_jobs import _request

pytestmark = pytest.mark.algorithm


def _unresolved():
    # A complete synthetic material snapshot does not resolve an omitted seed.
    payload = film_synthesis_request_to_payload("randomized-interface", _request())
    payload["request"].update(randomize_template_search=True, structure_search_seed=None)
    return payload


def test_plan_freezes_auto_seed_once_and_queue_replay_never_redraws(monkeypatch):
    draws = []

    def draw(bits):
        draws.append(bits)
        return 0

    monkeypatch.setattr("secrets.randbits", draw)
    planned, metadata = prepare_film_synthesis_payload(None, _unresolved())
    assert draws == [32]
    assert planned["request"]["structure_search_seed"] == 0
    assert planned["request"]["randomize_template_search"] is True
    assert metadata == ()
    canonical = canonical_parameters(None, {"payload": json.loads(json.dumps(planned))})
    queued = build_job_payload(canonical)
    request_id, request, _ = deserialize_job_payload(queued)
    assert request_id == "randomized-interface"
    assert request.structure_search_seed == 0
    assert freeze_film_synthesis_request(None, request).structure_search_seed == 0
    assert prepare_film_synthesis_payload(None, planned)[0] == planned
    # Use the registered assistant tool boundary, not a second direct prepare.
    from coat_sim.mcp import server
    monkeypatch.setattr(server, "service", None)  # Fully frozen synthetic inputs.

    async def replay_plan():
        reply = await server.mcp.call_tool("plan_film_synthesis", {"payload": planned})
        content = getattr(reply, "structured_content", None)
        return content if content is not None else reply.structuredContent

    assistant_plan = asyncio.run(replay_plan())
    assert assistant_plan["request"] == json.loads(json.dumps(planned))
    assert assistant_plan["request"]["request"]["structure_search_seed"] == 0
    assert assistant_plan["execution_project_root"] is None
    assert draws == [32]


def test_frozen_wire_and_queue_reject_unresolved_seed_without_drawing(monkeypatch):
    def forbidden(_bits):
        pytest.fail("execution and replay must not choose entropy")

    monkeypatch.setattr("secrets.randbits", forbidden)
    raw = _unresolved()
    with pytest.raises(FilmSynthesisPayloadError, match="resolved structure_search_seed"):
        film_synthesis_request_from_payload(raw)
    with pytest.raises(FilmSynthesisPayloadError, match="resolved structure_search_seed"):
        film_synthesis_request_to_payload("invalid", replace(
            _request(), randomize_template_search=True, structure_search_seed=None))
    with pytest.raises(FilmSynthesisPayloadError, match="resolved structure_search_seed"):
        build_job_payload({"payload": raw, "physical_structure_metadata": []})


def test_estimate_rejects_auto_draft_without_entropy_or_receipt(tmp_path, monkeypatch):
    def forbidden(_bits):
        pytest.fail("estimation must not resolve an automatic seed")

    monkeypatch.setattr("secrets.randbits", forbidden)
    service = CoatService(tmp_path / "estimate")
    with pytest.raises(ValueError, match="freeze randomized template search before estimation"):
        service.estimate_compute_task("film_synthesis", {"payload": _unresolved()})


@pytest.mark.parametrize("seed", [0, 4294967295])
def test_real_cli_preserves_explicit_randomization_seed_in_result(tmp_path, seed):
    request = replace(_request(), randomize_template_search=True, structure_search_seed=seed)
    payload = film_synthesis_request_to_payload("randomized-cli", request)
    source, output = tmp_path / "request.json", tmp_path / "result.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    completed = _run_cli(source, output)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(output.read_text(encoding="utf-8"))
    frozen = result["result"]["frozen_request"]
    assert frozen["randomize_template_search"] is True
    assert frozen["structure_search_seed"] == seed
    assert result["result"]["state"] == "completed"
    assert result["best_candidate_audit"]["targets_met"] is True


def test_real_cli_rejects_unresolved_execution_without_result_file(tmp_path):
    source, output = tmp_path / "unresolved.json", tmp_path / "result.json"
    source.write_text(json.dumps(_unresolved()), encoding="utf-8")
    completed = _run_cli(source, output)
    assert completed.returncode != 0
    assert "resolved structure_search_seed" in completed.stdout + completed.stderr
    assert not output.exists()


def test_real_cli_plan_resolves_seed_and_execution_keeps_it(tmp_path):
    source, planned = tmp_path / "draft.json", tmp_path / "planned.json"
    source.write_text(json.dumps(_unresolved()), encoding="utf-8")
    process = subprocess.run([sys.executable, "-m", "coat_sim.film_synthesis_cli",
        "--plan", "--request", str(source)], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=30)
    assert process.returncode == 0, process.stdout + process.stderr
    frozen_payload = json.loads(process.stdout)["request"]
    seed = frozen_payload["request"]["structure_search_seed"]
    assert type(seed) is int and 0 <= seed <= 4294967295
    planned.write_text(json.dumps(frozen_payload), encoding="utf-8")
    output = tmp_path / "result.json"
    executed = _run_cli(planned, output)
    assert executed.returncode == 0, executed.stdout + executed.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["result"]["frozen_request"]["structure_search_seed"] == seed


def test_result_and_cancel_payload_keep_actual_seed(monkeypatch):
    from coat_sim.core.film_auto_synthesis import synthesize_film_design

    monkeypatch.setattr("secrets.randbits", lambda _bits: 17)
    _, request, _ = film_synthesis_request_from_payload(
        prepare_film_synthesis_payload(None, _unresolved())[0])
    cancelled = threading.Event()
    cancelled.set()
    result = synthesize_film_design(None, request, cancel_event=cancelled)
    encoded = film_synthesis_result_to_payload("cancelled-randomized", result)
    assert encoded["result"]["state"] == "cancelled"
    assert encoded["result"]["frozen_request"]["structure_search_seed"] == 17
    assert encoded["result"]["frozen_request"]["randomize_template_search"] is True
