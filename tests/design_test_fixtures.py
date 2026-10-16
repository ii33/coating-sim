"""Copy historical example content into private, current-format test inputs.

The checked-in examples remain untouched. Only standalone filenames and their
explicit design-file references change; numerical and descriptive data do not.
This is test input preparation, not a production legacy-format reader.
"""
from __future__ import annotations

import json
from pathlib import Path


EXAMPLE_FILENAMES = {
    "source_example.json": "source_example.csrc",
    "rectangular_source_example.json": "rectangular_source_example.csrc",
    "substrate_example.json": "substrate_example.csub",
    "mask_example.json": "mask_example.cmsk",
    "machine_example.json": "machine_example.cmac",
    "process_example.json": "process_example.cproc",
    "film_design_nbk7_plate.json": "film_design_nbk7_plate.cfilm",
}
REFERENCE_KEYS = frozenset({"file", "machine_file", "substrate_file"})
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _typed_references(value):
    if isinstance(value, list):
        return [_typed_references(item) for item in value]
    if isinstance(value, dict):
        return {
            key: EXAMPLE_FILENAMES.get(item, item)
            if key in REFERENCE_KEYS and isinstance(item, str)
            else _typed_references(item)
            for key, item in value.items()
        }
    return value


def copy_typed_examples(root: str | Path) -> Path:
    """Return ``root/examples`` containing seven canonical design documents."""
    destination = Path(root) / "examples"
    destination.mkdir(parents=True, exist_ok=True)
    for historical, current in EXAMPLE_FILENAMES.items():
        contents = json.loads((EXAMPLES / historical).read_text(encoding="utf-8"))
        (destination / current).write_text(
            json.dumps(_typed_references(contents), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return destination
