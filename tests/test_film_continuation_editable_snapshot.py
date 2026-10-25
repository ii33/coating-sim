"""Exact durable editable closure deltas, with no duplicated active n/k arrays."""
from copy import deepcopy
import json

import pytest

from coat_sim.core.film_continuation_editable_snapshot import (
    encode_editable_snapshot_delta, restore_editable_snapshot_delta,
)
from coat_sim.core.film_process_import import (
    _sha256, validate_active_frozen_design_snapshot, validate_frozen_design_snapshot,
)
from tests.test_film_synthesis_jobs import _request

pytestmark = pytest.mark.contract


def _snapshots():
    active = deepcopy(_request().frozen_material_snapshot)
    full = deepcopy(active)
    full["materials"][0]["notes"] = "Original editable metadata"
    extra = deepcopy(full["materials"][0]["optical_sets"][0])
    extra["id"] = "inactive-source-set"
    extra["n"] = [value + .1 for value in extra["n"]]
    full["materials"][0]["optical_sets"].append(extra)
    full["sha256"] = _sha256(full)
    return active, full


def test_delta_roundtrips_json_exactly_and_references_active_arrays():
    active, full = _snapshots()
    before = deepcopy((active, full))
    delta = json.loads(json.dumps(encode_editable_snapshot_delta(active, full)))
    assert restore_editable_snapshot_delta(active, delta) == full
    assert (active, full) == before
    optical = delta["materials"][0]["optical_sets"]
    assert optical[0] == {"active_set_id": "nk"}
    assert "wavelengths" not in json.dumps(optical[0])
    assert optical[1]["value"] == full["materials"][0]["optical_sets"][1]
    assert delta["active_sha256"] == active["sha256"]
    assert delta["full_sha256"] == full["sha256"]


@pytest.mark.parametrize("failure", ["wrong_base", "wrong_full_hash", "unknown_set", "corrupt_record", "old_schema", "duplicate_material"])
def test_delta_rejects_missing_or_changed_identity(failure):
    active, full = _snapshots()
    delta = encode_editable_snapshot_delta(active, full)
    if failure == "wrong_base":
        active["materials"][0]["notes"] = "changed"
        active["sha256"] = _sha256(active)
    elif failure == "wrong_full_hash":
        delta["full_sha256"] = "0" * 64
    elif failure == "unknown_set":
        delta["materials"][0]["optical_sets"][0]["active_set_id"] = "missing"
    elif failure == "corrupt_record":
        delta["materials"][0]["record"]["notes"] = "changed"
    elif failure == "old_schema":
        delta["schema_version"] = 0
    else:
        delta["materials"].append(deepcopy(delta["materials"][0]))
    with pytest.raises(ValueError):
        restore_editable_snapshot_delta(active, delta)


def test_delta_cannot_change_source_design_or_settings():
    active, full = _snapshots()
    full["design"]["name"] = "Different source"
    full["sha256"] = _sha256(full)
    with pytest.raises(ValueError, match="design/settings"):
        encode_editable_snapshot_delta(active, full)


def _inactive_snapshots():
    active, _ = _snapshots()
    active["design"]["exit_medium"] = {
        "kind": "material", "material_id": "inactive-exit", "optical_set_id": "inactive-set",
    }
    active["sha256"] = _sha256(active)
    full = deepcopy(active)
    inactive = deepcopy(full["materials"][0])
    inactive["id"] = "inactive-exit"
    inactive["optical_sets"] = [deepcopy(inactive["optical_sets"][0])]
    inactive["optical_sets"][0].update(id="inactive-set", k=None)
    full["materials"].append(inactive)
    full["sha256"] = _sha256(full)
    return active, full


def test_active_delta_preserves_inactive_unknown_k_without_numeric_evaluation(monkeypatch):
    active, full = _inactive_snapshots()
    from coat_sim.core import film_process_import
    monkeypatch.setattr(film_process_import, "evaluate_optical_set",
                        lambda *args, **kwargs: pytest.fail("inactive optical evaluation"))
    before = deepcopy((active, full))
    assert validate_active_frozen_design_snapshot(active) == active
    # Process/editable restoration still demands the complete document closure.
    with pytest.raises(ValueError, match="inactive-exit/inactive-set"):
        validate_frozen_design_snapshot(active)
    delta = json.loads(json.dumps(encode_editable_snapshot_delta(active, full)))
    restored = restore_editable_snapshot_delta(active, delta)
    assert restored == full
    assert restored["materials"][-1]["optical_sets"][0]["k"] is None
    assert (active, full) == before


@pytest.mark.parametrize("failure", [
    "missing_active", "bad_digest", "duplicate_optical", "malformed_optical", "bad_settings",
])
def test_active_scope_keeps_digest_material_and_active_reference_validation(failure):
    active, full = _inactive_snapshots()
    if failure == "missing_active":
        active["materials"] = [row for row in active["materials"] if row["id"] != "glass"]
    elif failure == "bad_digest":
        active["sha256"] = "0" * 64
    elif failure == "duplicate_optical":
        active["materials"][0]["optical_sets"].append(
            deepcopy(active["materials"][0]["optical_sets"][0]))
    elif failure == "malformed_optical":
        active["materials"][0]["optical_sets"][0]["n"] = []
    else:
        active["settings"]["samples"] = True
    if failure != "bad_digest":
        active["sha256"] = _sha256(active)
    with pytest.raises(ValueError):
        validate_active_frozen_design_snapshot(active)
    with pytest.raises(ValueError):
        encode_editable_snapshot_delta(active, full)


def test_active_scope_does_not_allow_incomplete_editable_delta():
    active, full = _inactive_snapshots()
    full["materials"].pop()
    full["sha256"] = _sha256(full)
    with pytest.raises(ValueError, match="inactive-exit/inactive-set"):
        encode_editable_snapshot_delta(active, full)


@pytest.mark.parametrize("failure", ["drop_candidate", "change_physics", "change_readiness", "extra_metadata"])
def test_delta_cannot_rehash_a_changed_active_closure(failure):
    active, full = _snapshots()
    delta = encode_editable_snapshot_delta(active, full)
    candidate = next(row for row in full["materials"] if row["id"] == "low")
    delta_candidate = next(row for row in delta["materials"] if row["record"]["id"] == "low")
    if failure == "drop_candidate":
        full["materials"].remove(candidate)
        delta["materials"].remove(delta_candidate)
    elif failure == "change_physics":
        candidate["optical_sets"][0]["n"] = [value + .1 for value in candidate["optical_sets"][0]["n"]]
        delta_candidate["optical_sets"][0] = {"value": deepcopy(candidate["optical_sets"][0])}
    elif failure == "change_readiness":
        candidate["design_ready"] = not candidate["design_ready"]
        delta_candidate["record"]["design_ready"] = candidate["design_ready"]
    else:
        candidate["invented"] = "not part of the current material schema"
        delta_candidate["record"]["invented"] = candidate["invented"]
    full["sha256"] = _sha256(full)
    delta["full_sha256"] = full["sha256"]
    with pytest.raises(ValueError):
        encode_editable_snapshot_delta(active, full)
    with pytest.raises(ValueError):
        restore_editable_snapshot_delta(active, delta)


def test_delta_requires_inactive_workflow_material_closure():
    active, full = _snapshots()
    delta = encode_editable_snapshot_delta(active, full)
    workflow = {"optimization": {"version": 2, "structure_materials": [
        {"material_id": "missing-workflow", "optical_set_id": "missing-set"},
    ]}}
    for snapshot in (active, full):
        snapshot["design"]["workflow_settings"] = deepcopy(workflow)
        snapshot["sha256"] = _sha256(snapshot)
    delta["active_sha256"] = active["sha256"]
    delta["full_sha256"] = full["sha256"]
    with pytest.raises(ValueError, match="document material reference"):
        encode_editable_snapshot_delta(active, full)
    with pytest.raises(ValueError, match="document material reference"):
        restore_editable_snapshot_delta(active, delta)
