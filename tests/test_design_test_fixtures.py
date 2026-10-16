"""Protect example data while migrating only current-format test paths."""
import json

import pytest

from coat_sim.core.api import CoatService, ServiceError
from tests.design_test_fixtures import EXAMPLES, EXAMPLE_FILENAMES, copy_typed_examples


def test_typed_example_copy_preserves_data_and_original_files(tmp_path):
    before = {name: (EXAMPLES / name).read_bytes() for name in EXAMPLE_FILENAMES}
    destination = copy_typed_examples(tmp_path)
    assert {path.name for path in destination.iterdir()} == set(EXAMPLE_FILENAMES.values())
    for original, current in EXAMPLE_FILENAMES.items():
        expected = json.loads(before[original])
        if original == "machine_example.json":
            for source in expected["sources"]:
                source["file"] = "source_example.csrc"
            for mask in expected["masks"]:
                mask["file"] = "mask_example.cmsk"
        elif original == "process_example.json":
            expected["machine_file"] = "machine_example.cmac"
            expected["substrate_file"] = "substrate_example.csub"
        assert json.loads((destination / current).read_text(encoding="utf-8")) == expected
        assert (EXAMPLES / original).read_bytes() == before[original]


def test_typed_example_graph_loads_without_restoring_legacy_format(tmp_path):
    destination = copy_typed_examples(tmp_path)
    service = CoatService(tmp_path)
    document = service.load("process", "examples/process_example.cproc")
    assert document.runtime is not None
    # Identical current-schema contents with a legacy standalone suffix still fail.
    legacy = destination / "process_example.json"
    legacy.write_bytes((destination / "process_example.cproc").read_bytes())
    with pytest.raises(ServiceError):
        service.load("process", "examples/process_example.json")
