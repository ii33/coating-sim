"""Read-only integration checks for generated designs and existing companions.

Only the QA JSON and isolated test settings/database directories are written.
No calculation, optimizer queue job, user design save, or application window is
started. The production source, session, simulation and optimization controllers
are used without a fake service or fake session.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
QA = ROOT / "artifacts" / "film_design_generator_validation"


def main():
    QA.mkdir(parents=True, exist_ok=True)
    temp_root = QA / "root-test-temp"
    temp_root.mkdir(exist_ok=True)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    from PySide6.QtCore import QCoreApplication, QEvent, QSettings
    from PySide6.QtGui import QGuiApplication
    from coat_sim.core.film_design_service import FilmDesignService
    from coat_sim.ui.film_quick_controller import FilmDesignQuickController
    from coat_sim.ui.film_simulation_quick_controller import FilmSimulationQuickController
    from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController

    app = QGuiApplication.instance() or QGuiApplication([])
    checks = []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    high = {"material_id": "builtin_zns_literature_20261004",
            "optical_set_id": "ri_zns_amotchkina_n"}
    low = {"material_id": "builtin_research_YbF3_film",
           "optical_set_id": "ri_ybf3_amotchkina_n"}
    inputs = {
        "template_id": "hr_hl_qw", "wavelength_nm": 550.0,
        "pairs": 4.0, "role_bindings": {"H": high, "L": low},
        "incident_binding": {"material_id": "builtin_ideal_air",
                             "optical_set_id": "ideal_air_n1_k0"},
        "substrate_binding": {
            "material_id": "builtin_research_fused_silica_method_demonstrator",
            "optical_set_id": "ri_sio2_malitson"},
        "assume_lossless": True,
    }
    with tempfile.TemporaryDirectory(prefix="generator-bridge-", dir=temp_root) as isolated:
        source_service = FilmDesignService(ROOT, restrict_to_root=False)
        service = FilmDesignService(Path(isolated), restrict_to_root=False)
        service.materials = source_service.materials
        settings = QSettings(str(Path(isolated) / "geometry.ini"), QSettings.Format.IniFormat)
        source = FilmDesignQuickController(
            Path(isolated), service=service, geometry_settings=settings,
            defer_initialization=False)
        simulation = FilmSimulationQuickController(
            root=ROOT, service=service, source_controller=source)
        optimizer = FilmOptimizationQuickController(source)
        try:
            before = deepcopy(source.currentDesign())
            revision = source.session.snapshot().design_revision
            epoch = source.pendingEditEpoch
            preview = source.generateDesignPreview(deepcopy(inputs))
            check("integral QML numeric bridge produces eight layers",
                  preview.get("ok") and preview.get("layer_count") == 8)
            check("preview exposes token and metadata but not candidate document",
                  bool(preview.get("preview_token")) and "candidate_design" not in preview)
            check("preview leaves source design and revision unchanged",
                  source.currentDesign() == before and
                  source.session.snapshot().design_revision == revision)
            check("apply commits generated design exactly once",
                  source.applyGeneratedDesign(preview["preview_token"]))
            app.processEvents()
            generated = deepcopy(source.currentDesign())
            applied_revision = source.session.snapshot().design_revision
            check("generated source is dirty with one revision increment",
                  source.dirty and applied_revision == revision + 1)
            check("existing simulation follows generated unsaved session",
                  json.loads(simulation.designJson) == generated)
            check("existing optimizer marks its old snapshot stale",
                  optimizer.sourceStale)
            check("late optimizer candidate cannot overwrite generated design",
                  not source.applyOptimizedDesign(before, revision, epoch) and
                  source.currentDesign() == generated)
            optimizer.refreshDesign()
            check("optimizer explicit refresh uses generated layer IDs and physical thicknesses",
                  not optimizer.sourceStale and
                  {row["layer_id"] for row in optimizer.variables} ==
                  {row["id"] for row in generated["front_layers"]} and
                  all(row["physical_nm"] > 0 for row in optimizer.variables))
            check("generated application token is single-use",
                  not source.applyGeneratedDesign(preview["preview_token"]))
            check("fresh generated design can be undone", source.canUndoGeneratedDesign)
            check("undo restores original source document", source.undoGeneratedDesign())
            app.processEvents()
            check("undo restores dirty state and notifies simulation",
                  source.currentDesign() == before and not source.dirty and
                  json.loads(simulation.designJson) == before)
            check("undo marks optimizer snapshot stale", optimizer.sourceStale)

            preview = source.generateDesignPreview(deepcopy(inputs))
            source.markEditingDirty()
            check("pending field edit invalidates preview without committing a value",
                  not source.applyGeneratedDesign(preview["preview_token"]) and
                  source.currentDesign() == before)
            check("pending edit epoch rejects optimizer result at same committed revision",
                  not source.applyOptimizedDesign(
                      generated, source.session.snapshot().design_revision, epoch))

            preview = source.generateDesignPreview(deepcopy(inputs))
            source.setDesignValue("name", "changed after preview")
            changed = deepcopy(source.currentDesign())
            check("committed design change invalidates old generator token",
                  not source.applyGeneratedDesign(preview["preview_token"]) and
                  source.currentDesign() == changed)
            invalid = source.generateDesignPreview({**inputs, "pairs": 2.5})
            check("fractional QML pairs rejected without changing document",
                  not invalid.get("ok") and source.currentDesign() == changed)
            glass_incident = source.generateDesignPreview({
                **inputs, "incident_binding": deepcopy(inputs["substrate_binding"])})
            check("real substrate-library glass is accepted as an incident port",
                  glass_incident.get("ok") and source.currentDesign() == changed)
            preview = source.generateDesignPreview(deepcopy(inputs))
            source.markSourceClosed()
            check("closed source rejects pending generated apply",
                  not source.applyGeneratedDesign(preview["preview_token"]))
            check("closed source rejects new previews",
                  not source.generateDesignPreview(deepcopy(inputs)).get("ok"))
            check("closed source has no generated undo", not source.canUndoGeneratedDesign)
        finally:
            optimizer.detach_observers()
            simulation.sourceWindowClosed()
            for obj in (optimizer, simulation, source):
                obj.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
    result = {"status": "passed", "timestamp_utc": datetime.now(timezone.utc).isoformat(),
              "checks": checks, "check_count": len(checks),
              "materials": inputs, "real_controllers": True,
              "calculation_or_optimizer_jobs_started": False,
              "user_design_files_written": False}
    target = QA / "generator-bridge-validation.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": result["status"], "checks": len(checks), "output": str(target)}))


if __name__ == "__main__":
    main()
