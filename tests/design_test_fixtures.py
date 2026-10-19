"""Write a private, self-contained current-schema typed design graph.

This helper prepares test input only. It never reads the repository's
historical ``examples`` directory; every document is an explicit synthetic
dictionary written under the caller's temporary root with canonical typed
suffixes and current schema versions.
"""
from __future__ import annotations

import json
from pathlib import Path

EXAMPLE_FILENAMES = (
    "source_example.csrc",
    "rectangular_source_example.csrc",
    "substrate_example.csub",
    "mask_example.cmsk",
    "machine_example.cmac",
    "process_example.cproc",
    "film_design_nbk7_plate.cfilm",
)

# A simple, ideal point source referenced by both machine source slots. No
# material record and no aperture assumptions: the legacy uniform sampling path
# stays authoritative for this graph.
_SOURCE = {
    "schema_version": 2,
    "kind": "point",
    "device_type": "custom",
    "source_exponent": 1.0,
}

# A rectangular resistance-boat source kept separate from the machine graph.
# Current schema requires an explicit apertures list and rejects the historical
# aperture_kind / aperture_radius_mm aliases.
_RECTANGULAR_SOURCE = {
    "schema_version": 2,
    "kind": "rectangle",
    "device_type": "resistance_boat",
    "source_exponent": 1.0,
    "length_mm": 40.0,
    "width_mm": 20.0,
    "angle_deg": 0.0,
    "emission_model": {
        "kind": "resistance_boat",
        "boat_mode": "covered",
        "cover_height_mm": 10.0,
        "cover_thickness_mm": 2.0,
        "apertures": [
            {"kind": "disk", "radius_mm": 5.0, "offset_x_mm": 0.0, "offset_y_mm": 0.0},
            {"kind": "rectangle", "length_mm": 20.0, "width_mm": 10.0,
             "offset_x_mm": 0.0, "offset_y_mm": 0.0, "angle_deg": 0.0},
        ],
    },
}

_SUBSTRATE = {
    "schema_version": 1,
    "name": "圆形平面基片 Ø300 mm",
    "outline": {"kind": "circle", "radius_mm": 150.0},
    "surface": {"kind": "flat"},
}

_MASK = {
    "name": "示例掩膜",
    "z_mm": 500.0,
    "outer": [[-70.0, -100.0], [-15.0, -100.0], [10.0, 0.0],
              [-15.0, 100.0], [-70.0, 100.0]],
    "holes": [],
}

_MACHINE = {
    "schema_version": 5,
    "name": "双源示例机器",
    "carrier_x_mm": 0.0,
    "carrier_y_mm": 0.0,
    "planet_offset_mm": 312.0,
    "planet_tilt_deg": 0.0,
    "substrate_z_mm": 1000.0,
    "sun_teeth": 120,
    "planet_teeth": 36,
    "gear_module_mm": 4.0,
    "sources": [
        {"file": "source_example.csrc", "x_mm": 100.0, "y_mm": 0.0, "z_mm": 0.0},
        {"file": "source_example.csrc", "x_mm": -120.0, "y_mm": 25.0, "z_mm": 0.0},
    ],
    "masks": [{"file": "mask_example.cmsk"}],
    "monitors": [
        {"name": "晶控 A", "kind": "crystal", "x_mm": 500.0, "y_mm": 0.0, "z_mm": 900.0},
        {"name": "光控 B", "kind": "optical", "x_mm": -450.0, "y_mm": 120.0, "z_mm": 700.0},
    ],
}

_PROCESS = {
    "schema_version": 7,
    "name": "双源示例工艺",
    "machine_file": "machine_example.cmac",
    "substrate_file": "substrate_example.csub",
    "steps": [
        {"source_slot": 1, "target_nm": 100.0, "rate_nm_s": 1.0, "target_mode": "mean"},
        {"source_slot": 2, "target_nm": 30.0, "rate_nm_s": 0.5, "target_mode": "monitor",
         "monitor_slot": 1},
        {"source_slot": 1, "target_nm": 10.0, "rate_nm_s": 0.2, "target_mode": "center"},
    ],
}

_FILM_DESIGN = {
    "schema_version": 1,
    "id": "nbk7-transparent-plate-example",
    "name": "N-BK7 裸双面厚基片（显式无损假设示例）",
    "layer_order": "substrate_outward",
    "front_layers": [],
    "back_layers": [],
    "reference_wavelength_nm": 550.0,
    "substrate": {
        "material_id": "film_substrate_schott_nbk7",
        "optical_set_id": "schott_nbk7_sellmeier",
        "thickness": {"value": 1.0, "unit": "mm"},
        "sides": "double",
        "mode": "auto",
    },
    "incident_medium": {"kind": "constant", "n": 1.0},
    "exit_medium": {"kind": "constant", "n": 1.0},
    "assume_lossless": True,
    "first_surface": "front",
    "expression": {"text": "", "symbols": {}, "in_sync": True},
    "use_k": False,
}

_DOCUMENTS = {
    "source_example.csrc": _SOURCE,
    "rectangular_source_example.csrc": _RECTANGULAR_SOURCE,
    "substrate_example.csub": _SUBSTRATE,
    "mask_example.cmsk": _MASK,
    "machine_example.cmac": _MACHINE,
    "process_example.cproc": _PROCESS,
    "film_design_nbk7_plate.cfilm": _FILM_DESIGN,
}


def copy_typed_examples(root: str | Path) -> Path:
    """Return ``root/examples`` containing seven canonical design documents."""
    destination = Path(root) / "examples"
    destination.mkdir(parents=True, exist_ok=True)
    for name in EXAMPLE_FILENAMES:
        (destination / name).write_text(
            json.dumps(_DOCUMENTS[name], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return destination
