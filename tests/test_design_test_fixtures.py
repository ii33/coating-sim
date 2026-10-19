"""Protect the synthetic fixture graph: current schema, no repository reads."""
import json
from pathlib import Path
from typing import Literal

import pytest

from coat_sim.core.api import CoatService, ServiceError
from coat_sim.core.film_design import load_design
from coat_sim.core.schemas import design_data
from tests import design_test_fixtures
from tests.design_test_fixtures import EXAMPLE_FILENAMES, copy_typed_examples

_KIND_BY_NAME: dict[str, Literal['source', 'substrate', 'mask', 'machine', 'process']] = {
    "source_example.csrc": "source",
    "rectangular_source_example.csrc": "source",
    "substrate_example.csub": "substrate",
    "mask_example.cmsk": "mask",
    "machine_example.cmac": "machine",
    "process_example.cproc": "process",
}


def test_copied_graph_is_self_contained_current_schema(tmp_path):
    destination = copy_typed_examples(tmp_path)
    assert {path.name for path in destination.iterdir()} == set(EXAMPLE_FILENAMES)
    assert not any(path.suffix == ".json" for path in destination.iterdir())
    documents = {name: json.loads((destination / name).read_text(encoding="utf-8"))
                 for name in EXAMPLE_FILENAMES}
    assert documents["machine_example.cmac"]["schema_version"] == 5
    assert documents["process_example.cproc"]["schema_version"] == 7
    # Every reference stays inside the requested temp root.
    machine = documents["machine_example.cmac"]
    for entry in [*machine["sources"], *machine["masks"]]:
        assert (destination / entry["file"]).is_file()
    process = documents["process_example.cproc"]
    assert (destination / process["machine_file"]).is_file()
    assert (destination / process["substrate_file"]).is_file()
    # Every document validates under the current core schema.
    for name, kind in _KIND_BY_NAME.items():
        design_data(kind, documents[name])
    load_design(destination / "film_design_nbk7_plate.cfilm")


def test_rectangular_source_uses_canonical_apertures_only(tmp_path):
    destination = copy_typed_examples(tmp_path)
    source = json.loads((destination / "rectangular_source_example.csrc").read_text(
        encoding="utf-8"))
    model = source["emission_model"]
    assert model["kind"] == "resistance_boat"
    assert model["apertures"], model
    assert "aperture_kind" not in model
    assert "aperture_radius_mm" not in model
    assert {"kind", "radius_mm"} <= set(model["apertures"][0])


def test_helper_writes_without_reading_repository_examples(tmp_path, monkeypatch):
    reads = []
    original = Path.read_text

    def tracked_read(self, *args, **kwargs):
        reads.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracked_read)
    copy_typed_examples(tmp_path)
    assert reads == []
    assert not hasattr(design_test_fixtures, "EXAMPLES")


def test_typed_example_graph_loads_without_restoring_legacy_format(tmp_path):
    destination = copy_typed_examples(tmp_path)
    service = CoatService(tmp_path)
    document = service.load("process", "examples/process_example.cproc")
    assert document.runtime is not None
    assert document.issues == ()
    # Identical current-schema contents with a legacy standalone suffix still fail.
    legacy = destination / "process_example.json"
    legacy.write_bytes((destination / "process_example.cproc").read_bytes())
    with pytest.raises(ServiceError):
        service.load("process", "examples/process_example.json")
