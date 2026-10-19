import hashlib

import pytest

from coat_sim.scheduler.jobs import JobSpec, canonical
from coat_sim.scheduler.remote_contract import (
    MAX_SPEC_BYTES, RemoteContractError, prepare_portable_spec,
    validate_submission,
)
from coat_sim.scheduler.remote_contract import (
    _validate_mask_optimization_options, _validate_recipe_options,
)


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

