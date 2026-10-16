"""Finite-data pan bounds are display constraints, not simulation inputs."""
import copy
import math
import unittest
from unittest.mock import patch

from coat_sim.ui.film_simulation_quick_controller import (
    FilmSimulationQuickController, _bounded_pan_axis, _finite_plot_domain,
)


class FilmSpectrumPanBoundsTests(unittest.TestCase):
    def setUp(self):
        self.controller = FilmSimulationQuickController()
        render_patch = patch.object(self.controller, "_render_plot", return_value=None)
        render_patch.start()
        self.addCleanup(render_patch.stop)

    def tearDown(self):
        self.controller.close()

    def result(self, values, *, other=None):
        self.controller._result = {
            "wavelength_nm": [400.0, 450.0, 500.0, 550.0, 600.0],
            "R": values, "T": other or [0.0, 0.1, 0.5, 0.9, 1.0],
        }
        self.controller._plot_settings["curves"] = ["R"]
        self.controller._refresh_plot_data()

    def test_zoomed_pan_clamps_both_edges_without_jitter(self):
        self.result([math.nan, 0.2, 0.4, 0.6, math.inf])
        original = copy.deepcopy(self.controller._result)
        self.controller._view_bounds = {
            "x_min": 475.0, "x_max": 525.0, "y_min": 0.3, "y_max": 0.5,
        }
        self.controller.panPlot(100.0, -100.0)
        view = self.controller.property("plotView")
        self.assertEqual((view["x_min"], view["x_max"]), (450.0, 500.0))
        self.assertAlmostEqual(view["y_min"], 0.2)
        self.assertAlmostEqual(view["y_max"], 0.4)
        for _ in range(20):
            self.controller.panPlot(100.0, -100.0)
        self.assertEqual(view, self.controller.property("plotView"))
        self.controller.panPlot(-100.0, 100.0)
        view = self.controller.property("plotView")
        self.assertEqual((view["x_min"], view["x_max"]), (500.0, 550.0))
        self.assertAlmostEqual(view["y_min"], 0.4)
        self.assertAlmostEqual(view["y_max"], 0.6)
        for _ in range(20):
            self.controller.panPlot(-100.0, 100.0)
        self.assertEqual(view, self.controller.property("plotView"))
        assert original is not None
        assert self.controller._result is not None
        self.assertEqual(original["wavelength_nm"], self.controller._result["wavelength_nm"])
        self.assertTrue(math.isnan(self.controller._result["R"][0]))

    def test_hidden_curves_do_not_expand_domain_visible_union_does(self):
        self.result([math.nan, 0.2, 0.4, 0.6, math.nan])
        self.assertEqual(self.controller.property("plotData")["pan_bounds"], {
            "x_min": 450.0, "x_max": 550.0, "y_min": 0.2, "y_max": 0.6,
        })
        self.controller._plot_settings["curves"] = ["R", "T"]
        self.controller._refresh_plot_data()
        self.assertEqual(self.controller.property("plotData")["pan_bounds"], {
            "x_min": 400.0, "x_max": 600.0, "y_min": 0.0, "y_max": 1.0,
        })

    def test_empty_nan_and_degenerate_domains_cannot_pan(self):
        for values, curves in (([math.nan] * 5, ["R"]), ([0.5] * 5, [])):
            self.result(values)
            self.controller._plot_settings["curves"] = curves
            self.controller._refresh_plot_data()
            before = self.controller.property("plotView")
            self.controller.panPlot(0.2, 0.2)
            self.assertEqual(before, self.controller.property("plotView"))
            self.assertEqual(self.controller.property("plotData")["pan_bounds"], {})
        self.result([math.nan, math.nan, 0.5, math.nan, math.nan])
        self.controller.zoomPlot(0.2, 0.5, 0.5)
        before = self.controller.property("plotView")
        self.controller.panPlot(0.2, 0.2)
        self.assertEqual(before, self.controller.property("plotView"))

    def test_flat_curve_preserves_manual_percent_axis_and_reset(self):
        self.result([0.95] * 5)
        self.assertTrue(self.controller.setPlotSettings({
            "curves": ["R"], "x": {"auto": True},
            "y": {"unit": "percent", "auto": False, "min": 0, "max": 100},
        }))
        initial = self.controller.property("plotView")
        self.controller.panPlot(2, 2)
        self.assertEqual(initial, self.controller.property("plotView"))
        self.controller.zoomPlot(0.5, 0.5, 0.5)
        self.controller.panPlot(2, 2)
        self.assertEqual((self.controller.property("plotView")["y_min"], self.controller.property("plotView")["y_max"]),
                         (0.25, 0.75))
        self.controller.resetPlotView()
        self.assertEqual(initial, self.controller.property("plotView"))
        self.assertEqual((initial["y_min"], initial["y_max"]), (0.0, 1.0))

    def test_explicit_empty_axis_does_not_snap_to_data(self):
        self.assertEqual(_bounded_pan_axis(700.0, 720.0, -100, 400, 600), (700.0, 720.0))

    def test_long_spectrum_domain_uses_full_precision_original_extrema(self):
        size = 50001
        waves = [1000 + index * 0.000123456789 for index in range(size)]
        values = [0.5] * size
        values[431], values[8432] = 0.123456789012345, 0.987654321098765
        domain = _finite_plot_domain({"wavelength_nm": waves, "R": values},
                                     {"curves": ["R"], "polarizations": []})
        self.assertEqual(domain, {"x_min": waves[0], "x_max": waves[-1],
                                  "y_min": values[431], "y_max": values[8432]})

    def test_selected_polarization_domain_excludes_invalid_coordinate_pairs(self):
        result = {"wavelength_nm": [False, 400.0, 500.0, 600.0, math.inf],
                  "R": [0.0, math.nan, 0.5, math.nan, 1.0],
                  "polarization_curves": {"s": {"R": [0.0, 0.1, 0.3, 0.9, 1.0]},
                                          "p": {"R": [0.0, 0.0, 0.4, 1.0, 1.0]}}}
        self.assertEqual(_finite_plot_domain(result, {"curves": ["R"], "polarizations": ["s"]}),
                         {"x_min": 400.0, "x_max": 600.0, "y_min": 0.1, "y_max": 0.9})


if __name__ == "__main__":
    unittest.main()
