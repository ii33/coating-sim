"""Read-only local/remote transport for bounded film-reduction feedback."""
from __future__ import annotations

import json

import pytest

from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import (
    FilmOptimizationObjective, FilmOptimizationRequest, FilmThicknessVariable,
)
from coat_sim.core.film_reduction import FilmReductionRequest, freeze_film_reduction_request
from coat_sim.scheduler.film_reduction_live import (
    LIVE_FILE, MAX_BYTES, build_live_frame,
)
from coat_sim.scheduler.jobs import canonical, freeze_spec
from coat_sim.scheduler.remote_server import RemoteComputeService, ServiceError
from tests.film_structure_fixtures import fixture

pytestmark = pytest.mark.contract


@pytest.fixture
def remote_service(tmp_path):
    service = RemoteComputeService(tmp_path / "remote", insecure_loopback=True)
    try:
        yield service
    finally:
        service.scheduler.guard.close()
        service.scheduler.lock.close()


def _frozen_spec(tmp_path, *, reduction=True):
    service, _library, design = fixture(tmp_path / "project")
    request = FilmOptimizationRequest(
        design_snapshot=design,
        design_revision=1,
        variables=(FilmThicknessVariable("front", design["front_layers"][0]["id"], 0., 200.),),
        objectives=(FilmOptimizationObjective(
            550., 552., 3,
            pointwise=FilmObjectiveConstraint(mode="range", lower_bound=0., upper_bound=1.),
            angles_deg=(0.,)),),
        max_iterations=2,
        max_evaluations=8,
    )
    typed = (freeze_film_reduction_request(service, FilmReductionRequest(request))
             if reduction else request)
    return service, design, typed, freeze_spec(service, "film_optimization", {"request": typed})


def _metrics(total=100.0):
    return {
        "layer_count": 1,
        "total_thickness_nm": total,
        "min_thickness_nm": total,
        "max_normalized_error": 0.1,
        "weighted_max_normalized_error": 0.1,
        "weighted_rms_error": 0.05,
        "targets_met": True,
        "validation_targets_met": True,
        "validation_max_normalized_error": 0.1,
    }


def _frame(job_id, digest, request, design, sequence=0):
    metrics = _metrics()
    return build_live_frame(
        job_id=job_id,
        input_digest=digest,
        sequence=sequence,
        request=request,
        source_design=design,
        best_design=design,
        baseline_metrics=metrics,
        candidate_metrics=metrics,
        stage="test stage",
        state="running",
        iteration=1,
        evaluations=2,
    )


def _admit(service, spec, job_id):
    service.scheduler.store.submit(spec, job_id=job_id)
    path = service.scheduler.runtime / "jobs" / job_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "spec.json").write_text(canonical(spec), encoding="utf-8")
    return path


def _rpc(service, arguments):
    return service._rpc({
        "api_version": 1,
        "action": "reduction_live",
        "arguments": arguments,
    })


def test_local_reduction_live_transport_is_read_only_and_cursor_aware(
        tmp_path, remote_service, monkeypatch):
    service = remote_service
    fixture_service, design, request, spec = _frozen_spec(tmp_path)
    assert isinstance(request, FilmReductionRequest)
    job_id = "reduction-live"
    directory = _admit(service, spec, job_id)
    digest = service.scheduler.memory_status(job_id)["input_digest"]
    live = _frame(job_id, digest, request, design)
    live_path = directory / LIVE_FILE
    live_path.write_text(json.dumps(live), encoding="utf-8")
    monkeypatch.setattr(service.scheduler, "start_job",
                        lambda *_args, **_kwargs: pytest.fail("read-only request started a worker"))

    initial = _rpc(service, {"job_id": job_id, "input_digest": live["input_digest"]})
    assert initial == live
    assert _rpc(service, {"job_id": job_id, "input_digest": live["input_digest"],
                          "after_sequence": 0}) is None

    newer = dict(live, sequence=1, stage="newer")
    live_path.write_text(json.dumps(newer), encoding="utf-8")
    assert _rpc(service, {"job_id": job_id, "input_digest": live["input_digest"],
                          "after_sequence": 0}) == newer
    assert _rpc(service, {"job_id": job_id, "input_digest": live["input_digest"],
                          "after_sequence": -1}) == newer


@pytest.mark.parametrize("arguments,code", [
    ({"input_digest": "0" * 64}, "missing_argument"),
    ({"job_id": "x", "input_digest": "0" * 64, "extra": 1}, "unsupported_action"),
    ({"job_id": "x", "input_digest": "0" * 64, "after_sequence": True}, "invalid_cursor"),
    ({"job_id": "x", "input_digest": "0" * 64, "after_sequence": -2}, "invalid_cursor"),
    ({"job_id": "x", "input_digest": "bad"}, "invalid_digest"),
])
def test_remote_rpc_rejects_invalid_reduction_live_arguments(
        tmp_path, remote_service, arguments, code):
    with pytest.raises(ServiceError) as caught:
        _rpc(remote_service, arguments)
    assert caught.value.code == code


def test_reduction_live_enforces_job_digest_mode_and_missing_frame(
        tmp_path, remote_service):
    service = remote_service
    fixture_service, design, request, reduction_spec = _frozen_spec(tmp_path)
    job_id = "reduction-only"
    directory = _admit(service, reduction_spec, job_id)
    digest = service.scheduler.memory_status(job_id)["input_digest"]
    assert _rpc(service, {"job_id": job_id, "input_digest": digest}) is None

    frame = _frame(job_id, digest, request, design)
    (directory / LIVE_FILE).write_text(json.dumps(frame), encoding="utf-8")
    with pytest.raises(ValueError, match="identity_mismatch"):
        service.scheduler.reduction_live(job_id, input_digest="0" * 64)

    _service, _design, _typed, ordinary_spec = _frozen_spec(tmp_path / "ordinary", reduction=False)
    ordinary_id = "ordinary-optimization"
    ordinary_dir = _admit(service, ordinary_spec, ordinary_id)
    ordinary_digest = service.scheduler.memory_status(ordinary_id)["input_digest"]
    (ordinary_dir / LIVE_FILE).write_text(json.dumps(dict(frame, job_id=ordinary_id,
        input_digest=ordinary_digest)), encoding="utf-8")
    with pytest.raises(ValueError, match="requires_reduction_job"):
        service.scheduler.reduction_live(ordinary_id, input_digest=ordinary_digest)


def test_reduction_sidecar_is_bounded_before_json_parse_and_rejects_symlink(
        tmp_path, remote_service, monkeypatch):
    from coat_sim.scheduler import daemon

    _fixture_service, _design, _request, spec = _frozen_spec(tmp_path)
    job_id = "bounded-reduction"
    directory = _admit(remote_service, spec, job_id)
    sidecar = directory / LIVE_FILE
    sidecar.write_bytes(b"{" * (MAX_BYTES + 1))

    def must_not_parse(_raw):
        pytest.fail("oversized sidecar was parsed before the size check")

    monkeypatch.setattr(daemon.json, "loads", must_not_parse)
    with pytest.raises(ValueError, match="packet_too_large"):
        daemon._reduction_live_json(sidecar)

    monkeypatch.undo()
    target = tmp_path / "foreign.json"
    target.write_text("{}", encoding="utf-8")
    sidecar.unlink()
    try:
        sidecar.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")
    with pytest.raises(ValueError, match="symlink_not_owned"):
        daemon._reduction_live_json(sidecar)
