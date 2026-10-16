"""Offscreen regression for the visible spectrum-settings QA workflow."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import unittest
from pathlib import Path

from PySide6.QtWidgets import QApplication

from coat_sim.core import materials, optical_constants
from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.ui.film_design import FilmSimulationWindow
from tools.verify_film_plot_layout import calculate, verify_plot_settings_workflow


class FilmPlotLayoutToolTests(unittest.TestCase):
    def test_unified_plot_settings_workflow_with_synthetic_optical_fixture(self):
        app = QApplication.instance()
        if app is None:
            app = QApplication([])

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            material_folder = root / "data" / "materials"
            optical_sets: dict[str, dict[str, object]] = {}
            for identifier, purpose, refractive_index in (
                ("qa_film", "film", 1.8),
                ("qa_substrate", "substrate", 1.5),
            ):
                profile = materials.default_material_profile(
                    f"Synthetic QA fixture {identifier}", id=identifier)
                profile["category"] = purpose
                profile["notes"] = "Synthetic regression data only."
                source = root / f"{identifier}.csv"
                source.write_text(
                    f"wavelength,n,k\n400,{refractive_index},0\n700,{refractive_index},0\n",
                    encoding="utf-8",
                )
                optical = optical_constants.import_optical_file(
                    source,
                    name=f"Synthetic n={refractive_index}",
                    wavelength_unit="nm",
                    notes="Synthetic QA optical fixture",
                    source="test_film_plot_layout_tool",
                )
                optical["id"] = f"{identifier}_set"
                profile["optical_constants"] = [optical]
                materials.save_material(material_folder, profile)
                optical_sets[identifier] = optical

            service = FilmDesignService(root)
            design = service.new_design()
            design["substrate"].update(
                material_id="qa_substrate",
                optical_set_id=optical_sets["qa_substrate"]["id"],
                mode="semi_infinite",
                sides="single",
            )
            design["front_layers"] = [{
                "id": "qa_layer",
                "material_id": "qa_film",
                "optical_set_id": optical_sets["qa_film"]["id"],
                "thickness": {"value": 100, "unit": "nm"},
                "note": "Synthetic QA layer",
            }]

            window = FilmSimulationWindow(root, service=service)
            try:
                window.set_design(service.validate(design))
                window.samples.setValue(31)
                window.show()
                app.processEvents()
                calculate(app, window)
                self.assertIsNotNone(window.result)

                output = root / "workflow-output"
                verify_plot_settings_workflow(window, output)

                self.assertTrue((output / "绘图参数_全中文.png").is_file())
                self.assertTrue((output / "绘图参数_中文校验.png").is_file())
                self.assertFalse(window._plot_settings["grid"])
                curves = window._plot_settings["curves"]
                if not isinstance(curves, list):
                    self.fail("Committed plot curves should be a list")
                self.assertNotIn("T", curves)
            finally:
                window.close()
                app.processEvents()


if __name__ == "__main__":
    unittest.main()
