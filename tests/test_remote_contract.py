from copy import deepcopy
import hashlib
import json
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest

from coat_sim.scheduler.jobs import JobSpec, canonical, freeze_spec
from coat_sim.scheduler.remote_contract import (
    MAX_SPEC_BYTES, RemoteContractError, prepare_portable_spec,
    validate_submission,
)
from coat_sim.scheduler.remote_contract import (
    _validate_mask_optimization_options, _validate_recipe_options,
)

from tests.test_film_optimization_continuation import ordinary, synthesis_payload  # noqa: F401


def _envelope(spec, *, job_id="job-123"):
    blob = canonical(spec.to_dict()).encode("utf-8")
    digest = hashlib.sha256(blob).hexdigest()
    return ({"api_version": 1, "executor_schema_version": 1, "job_id": job_id,
             "spec_blob": {"sha256": digest, "bytes": len(blob)}}, {digest: blob})


def _error_code(call):
    with pytest.raises(RemoteContractError) as error:
        call()
    return error.value.code


def test_probe_admission_is_opt_in_and_bounded():
    spec = JobSpec("probe", {"delay_s": 0.1, "payload_bytes": 5}, {})
    envelope, blobs = _envelope(spec)
    assert _error_code(lambda: validate_submission(envelope, blobs.__getitem__)) == "executor_not_allowed"
    job_id, parsed, metadata = validate_submission(envelope, blobs.__getitem__, allow_probe=True)
    assert job_id == "job-123"
    assert parsed.executor == "probe"
    assert metadata["spec_bytes"] == envelope["spec_blob"]["bytes"]


@pytest.mark.parametrize("params", [
    {"delay_s": -1}, {"delay_s": 31}, {"payload_bytes": 16 * 1024 * 1024 + 1},
    {"crash": True}, {"fail_save": True},
])
def test_probe_rejects_out_of_range_and_failure_injection(params):
    envelope, blobs = _envelope(JobSpec("probe", params, {}))
    assert _error_code(lambda: validate_submission(envelope, blobs.__getitem__, allow_probe=True)) in {
        "invalid_probe_parameter", "invalid_executor_schema"
    }


def test_manifest_digest_and_length_are_checked_before_parse():
    envelope, blobs = _envelope(JobSpec("probe", {}, {}))
    envelope["spec_blob"]["bytes"] += 1
    assert _error_code(lambda: validate_submission(envelope, blobs.__getitem__, allow_probe=True)) == "spec_blob_size_mismatch"


def test_submission_fields_and_job_id_are_strict():
    envelope, blobs = _envelope(JobSpec("probe", {}, {}))
    extra = dict(envelope, ignored=True)
    assert _error_code(lambda: validate_submission(extra, blobs.__getitem__, allow_probe=True)) == "invalid_submission"
    envelope["job_id"] = "../unsafe"
    assert _error_code(lambda: validate_submission(envelope, blobs.__getitem__, allow_probe=True)) == "invalid_job_id"
    envelope["job_id"] = "task.v1"
    assert _error_code(lambda: validate_submission(envelope, blobs.__getitem__, allow_probe=True)) == "invalid_job_id"


def test_portable_copy_removes_provenance_and_runtime_without_mutating_source():
    snapshot = {"kind": "coat_sim.input_snapshot", "version": 1,
                "process_path": "designs/process.cproc", "files": [],
                "original_path": "C:/work/process.json", "runtime": {"cached": True}}
    source = JobSpec("process", {"process_path": snapshot, "settings": {}},
                     {"snapshot": snapshot})
    portable = prepare_portable_spec(source)
    assert "original_path" in source.parameters["process_path"]
    assert "original_path" not in portable.parameters["process_path"]
    assert "runtime" not in portable.inputs["snapshot"]


def test_frozen_spec_rejects_noncanonical_blob():
    spec = JobSpec("python_script", {"code": "pass"}, {})
    envelope, blobs = _envelope(spec)
    blob = next(iter(blobs.values()))
    # Hash and manifest remain valid, but whitespace makes this noncanonical.
    blobs[envelope["spec_blob"]["sha256"]] = b" " + blob
    envelope["spec_blob"]["bytes"] += 1
    digest = hashlib.sha256(blobs[envelope["spec_blob"]["sha256"]]).hexdigest()
    blobs[digest] = blobs.pop(envelope["spec_blob"]["sha256"])
    envelope["spec_blob"]["sha256"] = digest
    assert _error_code(lambda: validate_submission(envelope, blobs.__getitem__)) == "noncanonical_job_spec"


def test_duplicate_json_keys_are_rejected():
    blob = b'{"executor":"probe","executor":"probe","inputs":{},"parameters":{},"protocol_version":1}'
    digest = hashlib.sha256(blob).hexdigest()
    envelope = {"api_version": 1, "executor_schema_version": 1, "job_id": "job-1",
                "spec_blob": {"sha256": digest, "bytes": len(blob)}}
    assert _error_code(lambda: validate_submission(envelope, lambda _: blob, allow_probe=True)) == "duplicate_json_key"


def test_python_script_is_not_a_remote_executor():
    envelope, blobs = _envelope(JobSpec("python_script", {"code": "pass"}, {}))
    assert _error_code(lambda: validate_submission(envelope, blobs.__getitem__)) == "executor_not_allowed"


def test_spec_size_limit_is_20_mib():
    assert MAX_SPEC_BYTES == 20 * 1024 * 1024


@pytest.mark.parametrize("validator,bad", [
    (_validate_recipe_options, {"unknown": 1}),
    (_validate_recipe_options, {"max_evaluations": True}),
    (_validate_mask_optimization_options, {"unknown": 1}),
    (_validate_mask_optimization_options, {"max_evaluations": True}),
])
def test_nested_option_schemas_reject_unknown_fields_and_wrong_types(validator, bad):
    assert _error_code(lambda: validator(bad)) == "invalid_executor_schema"



@pytest.fixture(params=["film_optimization", "film_synthesis"])
def continuation_spec(request, ordinary, synthesis_payload):  # noqa: F811
    from coat_sim.core.film_optimization_continuation import (
        build_optimization_warm_restart, build_synthesis_warm_restart,
    )
    from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest
    from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload
    from coat_sim.ui.film_optimization_continue_source import SOURCE_INPUT_KEY, build_source_binding
    if request.param == "film_optimization":
        original, result = ordinary
        plan = build_optimization_warm_restart(original, result, design_revision=18,
            max_iterations=2, max_evaluations=4)
        spec = freeze_spec(None, request.param, {"request": plan.request})
        source_design = original.design_snapshot
    else:
        plan = build_synthesis_warm_restart(synthesis_payload, design_revision=18,
            max_cycles=1, max_evaluations=4, max_seconds=10.)
        assert isinstance(plan.request, FilmSynthesisRequest)
        payload = film_synthesis_request_to_payload("remote-warm", plan.request)
        spec = freeze_spec(None, request.param, {"payload": payload, "physical_structure_metadata": []})
        source_design = plan.request.environment
    # The source may still show the original design, not the retained candidate.
    snapshot = SimpleNamespace(path="", design=source_design, design_revision=18)
    spec["inputs"][SOURCE_INPUT_KEY] = build_source_binding(None, snapshot, plan.request)
    plan.metadata.update(source_job_id="prior-job-123", source_spec_fingerprint="a" * 64)
    spec["inputs"]["film_warm_restart"] = plan.metadata
    return JobSpec.parse(spec)


def _validate_film_spec(spec, *, bound=False):
    spec = prepare_portable_spec(spec)
    envelope, blobs = _envelope(spec)
    if bound:
        envelope.update(device_binding_version=1, device_id="cpu:default")
    return validate_submission(envelope, blobs.__getitem__)


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("provenance", ["legacy", "source", "warm_restart"])
def test_film_accepts_only_current_optional_source_and_restart_inputs(
        continuation_spec, bound, provenance):
    from coat_sim.ui.film_optimization_continue_source import SOURCE_INPUT_KEY
    raw = continuation_spec.to_dict()
    if provenance != "warm_restart":
        raw["inputs"].pop("film_warm_restart")
    if provenance == "legacy":
        raw["inputs"].pop(SOURCE_INPUT_KEY)
    if bound:
        raw["inputs"]["device_binding"] = {"schema_version": 1, "device_id": "cpu:default"}
    spec = JobSpec.parse(raw)
    before = deepcopy(spec.to_dict())
    _job_id, parsed, _metadata = _validate_film_spec(spec, bound=bound)
    assert parsed.to_dict() == before
    assert spec.to_dict() == before


@pytest.mark.parametrize("change", [
    "extra-input", "missing-source", "extra-source-field", "source-version", "source-version-bool",
    "source-kind", "source-design-hash", "source-revision-bool", "request-digest", "material-digest",
    "managed-shape", "managed-project-path", "managed-document-path", "managed-revision",
    "editable-delta",
])
def test_film_rejects_unbound_or_malformed_source_inputs(continuation_spec, change):
    from coat_sim.ui.film_optimization_continue_source import SOURCE_INPUT_KEY
    raw = continuation_spec.to_dict()
    inputs = raw["inputs"]
    binding = inputs[SOURCE_INPUT_KEY]
    if change == "extra-input":
        inputs["ignored"] = True
    elif change == "missing-source":
        inputs.pop(SOURCE_INPUT_KEY)
    elif change == "extra-source-field":
        binding["ignored"] = True
    elif change == "source-version":
        binding["schema_version"] = 2
    elif change == "source-version-bool":
        binding["schema_version"] = True
    elif change == "source-kind":
        binding["kind"] = "unrecognized"
    elif change == "source-design-hash":
        binding["source_design_sha256"] = "not-a-digest"
    elif change == "source-revision-bool":
        binding["source_design_revision"] = True
    elif change == "request-digest":
        binding["frozen_request_sha256"] = "b" * 64
    elif change == "material-digest":
        binding["frozen_material_sha256"] = "b" * 64
    elif change == "editable-delta":
        binding["synthesis_editable_delta"] = {"ignored": True}
    else:
        binding["managed_film"] = {"project_id": "a" * 32, "document_id": "b" * 32,
                                   "revision": "c" * 64}
        if change == "managed-shape":
            binding["managed_film"]["ignored"] = True
        elif change == "managed-project-path":
            binding["managed_film"]["project_id"] = "/private/client/project"
        elif change == "managed-document-path":
            binding["managed_film"]["document_id"] = "C:\\private\\film"
        else:
            binding["managed_film"]["revision"] = "bad"
    assert _error_code(lambda: _validate_film_spec(JobSpec.parse(raw))) == "invalid_executor_inputs"


@pytest.mark.parametrize("change", [
    "extra-field", "schema-bool", "kind", "mode", "source-kind", "original-revision-bool",
    "original-digest", "candidate-digest", "source-job-path", "source-fingerprint", "budget-extra",
    "budget-mismatch", "budget-bool", "spent-extra", "spent-negative", "spent-bool",
    "solver_state_preserved", "saved_training_complete", "physical_replay_performed",
    "independent_audit_performed", "saved_scores_reused", "seeds-extra", "seeds-capacity",
    "seeds-count", "seeds-archive", "seeds-bool", "seeds-candidate-first", "seeds-scores",
])
def test_film_rejects_malformed_or_mismatched_restart_provenance(continuation_spec, change):
    raw = continuation_spec.to_dict()
    meta = raw["inputs"]["film_warm_restart"]
    if change == "extra-field":
        meta["ignored"] = True
    elif change == "schema-bool":
        meta["schema_version"] = True
    elif change == "kind":
        meta["kind"] = "unrecognized"
    elif change == "mode":
        meta["mode"] = "restore_solver_checkpoint"
    elif change == "source-kind":
        meta["source_kind"] = "film_synthesis" if raw["executor"] == "film_optimization" else "film_optimization"
    elif change == "original-revision-bool":
        meta["original_design_revision"] = True
    elif change == "original-digest":
        meta["original_request_sha256"] = "not-a-digest"
    elif change == "candidate-digest":
        meta["candidate_physical_hash"] = "b" * 64
    elif change == "source-job-path":
        meta["source_job_id"] = "../prior-job"
    elif change == "source-fingerprint":
        meta["source_spec_fingerprint"] = "bad"
    elif change == "budget-extra":
        meta["fresh_budget"]["ignored"] = True
    elif change == "budget-mismatch":
        meta["fresh_budget"]["max_evaluations"] += 1
    elif change == "budget-bool":
        meta["fresh_budget"]["max_evaluations"] = True
    elif change == "spent-extra":
        meta["spent"]["ignored"] = 0
    elif change == "spent-negative":
        meta["spent"]["evaluations"] = -1
    elif change == "spent-bool":
        meta["spent"]["evaluations"] = True
    elif change.startswith("seeds-"):
        seeds = meta.setdefault("seeds", {})
        key, value = {
            "seeds-extra": ("ignored", True), "seeds-capacity": ("archive_geometry_capacity", 999),
            "seeds-count": ("retained_count", 0), "seeds-archive": ("archive_added", 999),
            "seeds-bool": ("original_count", True), "seeds-candidate-first": ("candidate_first", False),
            "seeds-scores": ("saved_scores_reused", True),
        }[change]
        seeds[key] = value
    else:
        meta[change] = not meta[change]
    assert _error_code(lambda: _validate_film_spec(JobSpec.parse(raw))) == "invalid_executor_inputs"


def test_current_source_provenance_is_content_addressed(continuation_spec):
    changed = continuation_spec.to_dict()
    changed["inputs"]["film_warm_restart"]["source_job_id"] = "another-prior-job"
    assert JobSpec.parse(changed).fingerprint != continuation_spec.fingerprint


@pytest.mark.parametrize("bad", [False, True])
def test_synthesis_source_validates_complete_editable_delta(continuation_spec, bad):
    from coat_sim.core.film_continuation_editable_snapshot import encode_editable_snapshot_delta
    from coat_sim.core.film_process_import import _sha256
    from coat_sim.core.film_synthesis_jobs import deserialize_job_payload
    from coat_sim.ui.film_optimization_continue_source import SOURCE_INPUT_KEY
    raw = continuation_spec.to_dict()
    if raw["executor"] == "film_synthesis":
        _request_id, request, _metadata = deserialize_job_payload(raw["parameters"]["payload"])
        active = request.frozen_material_snapshot
        full = deepcopy(active)
        delta = encode_editable_snapshot_delta(active, full)
        if bad:
            full["materials"][0]["invented_material_setting"] = "not-current-schema"
            full["sha256"] = _sha256(full)
            delta["materials"][0]["record"]["invented_material_setting"] = "not-current-schema"
            delta["full_sha256"] = full["sha256"]
        raw["inputs"][SOURCE_INPUT_KEY]["synthesis_editable_delta"] = delta
        if bad:
            assert _error_code(lambda: _validate_film_spec(JobSpec.parse(raw))) == "invalid_executor_inputs"
        else:
            assert _validate_film_spec(JobSpec.parse(raw))[1].to_dict() == raw
    else:
        # Even a well-shaped delta is not part of the thickness source schema.
        raw["inputs"][SOURCE_INPUT_KEY]["synthesis_editable_delta"] = {}
        assert _error_code(lambda: _validate_film_spec(JobSpec.parse(raw))) == "invalid_executor_inputs"


def test_film_accepts_stable_managed_source_identity(continuation_spec):
    from coat_sim.ui.film_optimization_continue_source import SOURCE_INPUT_KEY
    raw = continuation_spec.to_dict()
    raw["inputs"][SOURCE_INPUT_KEY]["managed_film"] = {
        "project_id": "a" * 32, "document_id": "b" * 32, "revision": "c" * 64}
    assert _validate_film_spec(JobSpec.parse(raw))[1].to_dict() == raw


def test_film_source_admission_remains_headless(continuation_spec):
    script = textwrap.dedent("""\
        import builtins, json, sys
        original_import = builtins.__import__
        def headless_import(name, *args, **kwargs):
            if name.startswith(("PySide", "PyQt")):
                raise AssertionError("remote film admission imported Qt")
            return original_import(name, *args, **kwargs)
        builtins.__import__ = headless_import
        from coat_sim.scheduler.jobs import JobSpec
        from coat_sim.scheduler.remote_contract import _validate_spec
        _validate_spec(JobSpec.parse(json.loads(sys.stdin.read())), allow_probe=False)
        print("headless admission passed")
    """)
    completed = subprocess.run([sys.executable, "-c", script],
        input=canonical(continuation_spec.to_dict()), text=True, capture_output=True,
        check=False, timeout=20)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "headless admission passed"


def test_remote_cpu_executes_and_preserves_current_warm_restart_inputs(tmp_path, continuation_spec):
    from coat_sim.scheduler.remote_client import make_remote_client
    from tests.test_remote_server import running
    from coat_sim.ui.film_optimization_continue_source import SOURCE_INPUT_KEY
    server_root = tmp_path / "private-server"
    with running(server_root) as server:
        client = make_remote_client(server.url, tmp_path / "private-client-cache",
            token="ephemeral-test-operator-token", allow_loopback_http=True,
            device_id="cpu:default")
        receipt = client.submit(continuation_spec, job_id="current-warm-restart", metadata={})
        assert receipt["requested_device_id"] == "cpu:default"
        deadline = time.monotonic() + 35
        final = client.wait_for_terminal("current-warm-restart",
            should_stop=lambda: time.monotonic() >= deadline, wait_s=.05)
        assert final["state"] == "succeeded", final.get("error_code")
        persisted = json.loads((server_root / "artifacts/runtime/scheduler/jobs/current-warm-restart/spec.json").read_text())
        assert persisted["inputs"][SOURCE_INPUT_KEY] == continuation_spec.inputs[SOURCE_INPUT_KEY]
        assert persisted["inputs"]["film_warm_restart"] == continuation_spec.inputs["film_warm_restart"]
        assert persisted["inputs"]["device_binding"] == {"schema_version": 1, "device_id": "cpu:default"}
        assert client.result("current-warm-restart")["kind"] in {
            "coat_sim.film_optimization_result", "coat_sim.film_synthesis_result"}
