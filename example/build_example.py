"""Rebuild the public synthetic example with the application's current APIs.

Run from the repository root: python example/build_example.py NEW_DIRECTORY
The destination must not already exist. No catalog or user data is read.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from coat_sim.core.film_design import new_design
from coat_sim.core.film_process_import import build_film_process_import, freeze_complete_design
from coat_sim.core.project_default_loading import load_project_default
from coat_sim.core.project_film_service import ProjectFilmService
from coat_sim.core.project_store import ProjectStore


class EmptyCatalog:
    def catalog_records(self):
        return []


def synthetic_material(identity, index):
    return {"id": identity, "name": f"Ideal {identity} (synthetic)",
            "notes": "Original mathematical fixture, MIT licensed; not measured or calibrated.",
            "optical_sets": [{"id": "visible", "name": "Constant index, 400–700 nm",
                              "kind": "tabulated", "wavelength_unit": "nm",
                              "wavelength_min": 400., "wavelength_max": 700.,
                              "wavelengths": [400., 700.], "n": [index, index], "k": [0., 0.],
                              "source": "Original synthetic constant-index example; repository MIT license",
                              "notes": "Not a real material database or production specification."}]}


def build(destination):
    destination = Path(destination)
    if destination.exists():
        raise ValueError("Destination already exists; choose a new directory")
    store = ProjectStore.create(destination, "Synthetic CPU coating demo",
        summary="Self-contained ideal single-layer film and masked deposition demo",
        notes="Original synthetic inputs, MIT license. Not measured, calibrated or production-qualified.")
    materials = {"demo-film": synthetic_material("demo-film", 1.38),
                 "demo-glass": synthetic_material("demo-glass", 1.52)}
    service = ProjectFilmService(store, global_material_library=EmptyCatalog())
    design = new_design("Synthetic single-layer coating", "synthetic-single-layer")
    design["substrate"].update(material_id="demo-glass", optical_set_id="visible")
    design["front_layers"] = [{"id": "demo-layer", "material_id": "demo-film",
        "optical_set_id": "visible", "thickness": {"value": 70., "unit": "nm"},
        "note": "Synthetic starting thickness; use optimization to explore changes."}]
    design["note"] = "Ideal lossless constants; no measured material or machine calibration."
    saved = service.create_film(design, materials=materials)
    # Export actual page configurations, rather than guessing their JSON schema.
    from PySide6.QtGui import QGuiApplication
    from coat_sim.ui.film_quick_controller import FilmDesignQuickController
    from coat_sim.ui.film_simulation_quick_controller import FilmSimulationQuickController
    from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
    app = QGuiApplication.instance() or QGuiApplication([])
    source = FilmDesignQuickController(service=service)
    assert source.session is not None
    assert source.session.request_replace_design(design)
    simulation = FilmSimulationQuickController(service=service, source_controller=source)
    simulation.setParameter("samples", 31)
    optimization = FilmOptimizationQuickController(source)
    config = optimization.exportOptimizationConfiguration()
    config["solver"].update(max_iterations=12, max_evaluations=40, seed=0)
    config["objectives"][0].update(name="Explore low visible reflectance",
        wavelength_min_nm=500., wavelength_max_nm=600., wavelength_samples=11,
        sampling_basis="samples")
    config["objectives"][0]["pointwise"]["upper_bound_percent"] = 1.3
    assert optimization.loadOptimizationConfiguration(config)
    design["workflow_settings"] = {
        "simulation": simulation.exportSimulationConfiguration(),
        "optimization": optimization.exportOptimizationConfiguration()}
    service.save_document(saved.document_id, design, expected_revision=saved.revision)
    optimization.markSourceClosed()
    simulation.close()
    source.markSourceClosed()
    app.processEvents()
    # External inputs are freshly authored here, then imported with their closure.
    with tempfile.TemporaryDirectory(prefix="coating-example-inputs-") as temporary_folder:
        folder = Path(temporary_folder)
        inputs = {
            "source.csrc": {"schema_version": 2, "kind": "point", "device_type": "custom",
                "name": "Synthetic point source",
                "description": "Ideal cosine point emitter at the machine origin; exponent 1.",
                "notes": "Original MIT-licensed demonstration geometry; no measured emission or rate calibration.",
                            "source_exponent": 1.},
            "substrate.csub": {"schema_version": 1, "name": "Synthetic flat disk, diameter 20 mm",
                "outline": {"kind": "circle", "radius_mm": 10.}, "surface": {"kind": "flat"}},
            "mask.cmsk": {"name": "Synthetic rectangular shadow mask",
                "description": "Opaque rectangle at z=50 mm, x=-5 to -2 mm and y=-5 to 5 mm.",
                "notes": "Intentional static shadow yields zero thickness in blocked cells; not a manufactured or calibrated correction plate.",
                "z_mm": 50., "outer": [[-5., -5.], [-2., -5.], [-2., 5.], [-5., 5.]],
                          "holes": []},
            "machine.cmac": {"schema_version": 5,
                "name": "Synthetic static coating machine",
                "description": "One point source, one mask, 100 mm source-to-substrate distance and zero planet offset.",
                "notes": "Uncalibrated synthetic geometry. The demo configuration sets carrier speed to zero; no real equipment performance is implied.", "carrier_x_mm": 0., "carrier_y_mm": 0.,
                "planet_offset_mm": 0., "planet_tilt_deg": 0., "substrate_z_mm": 100.,
                "sun_teeth": 120, "planet_teeth": 36, "gear_module_mm": 0.,
                "sources": [{"file": "source.csrc", "x_mm": 0., "y_mm": 0., "z_mm": 0.}],
                "masks": [{"file": "mask.cmsk"}], "monitors": []},
            "process.cproc": {"schema_version": 6, "name": "Synthetic masked single-layer process",
                "machine_file": "machine.cmac", "substrate_file": "substrate.csub",
                "steps": [{"source_slot": 1, "target_nm": 70., "rate_nm_s": 1., "target_mode": "mean"}]}}
        for name, data in inputs.items():
            (folder / name).write_text(json.dumps(data), encoding="utf-8")
        process_id = store.import_document(folder / "process.cproc")
    graph = store.resolve_document(process_id)
    process = next(row["data"] for row in graph["documents"] if row["id"] == process_id)
    imported = build_film_process_import(freeze_complete_design(service, design), "front",
                                        {"demo-film": 1}, graph["runtime"]["machine"])
    process.update({key: value for key, value in imported.items() if key != "preview"})
    process["schema_version"] = 7
    store.save_document(process_id, process)
    store.add_deposition_configuration("Small CPU demo (21 × 21, 12 time samples)", process_id,
        {"settings": {"grid_size": 21, "time_steps": 12, "carrier_rpm": 0.}, "backend": "CPU"},
        description="Static mask shadow, mean-normalized 70 nm layer; coarse demonstration only.",
        notes="No physical calibration. Refine grid/time sampling before interpreting numerical accuracy.")
    assert load_project_default(ProjectStore.open(store.project_file)).configured
    # Only this newly created destination is touched. Retain the published
    # generation, not superseded transaction generations or empty runtime dirs.
    keep = {store.project_file} | {store.root / row["path"] for row in store.manifest["documents"]}
    for path in store.root.rglob("*"):
        if path.is_file() and path not in keep:
            path.unlink()
    for path in sorted(store.root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    assert load_project_default(ProjectStore.open(store.project_file)).configured
    return store.project_file


if __name__ == "__main__":
    print(build(sys.argv[1]))
