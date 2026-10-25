"""Exact durable editable closure deltas, with no duplicated active n/k arrays."""
from copy import deepcopy
import json

import pytest

from coat_sim.core.film_continuation_editable_snapshot import (
    encode_editable_snapshot_delta, restore_editable_snapshot_delta,
)
from coat_sim.core.film_process_import import _sha256
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
