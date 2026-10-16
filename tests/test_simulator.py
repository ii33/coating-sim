import dataclasses
import json
import math
import tempfile
import threading
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coat_sim.core.simulator import Settings, read_mask, simulate, workload


class SimulationTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(grid_size=31, time_steps=16)

    def test_static_center_source_matches_point_source_formula(self):
        s = dataclasses.replace(self.settings, carrier_rpm=0, planet_offset_mm=0,
                                source_x_mm=0, source_y_mm=0)
        r = simulate(s, backend="CPU")
        center = r["field"][15, 15]
        offset = r["field"][15, 25]  # 100 mm from the center
        z = s.substrate_z_mm
        distance2 = z*z + 100*100
        expected = (z*z / distance2) ** ((s.source_exponent + 3) / 2)
        self.assertAlmostEqual(offset / center, expected, places=12)

    def test_gear_and_source_change_output_on_recalculation(self):
        baseline = simulate(self.settings, backend="CPU")
        self.assertAlmostEqual(baseline["omega_p_rpm"], 65)
        gear = simulate(dataclasses.replace(self.settings, planet_teeth=37), backend="CPU")
        self.assertNotAlmostEqual(baseline["omega_p_rpm"], gear["omega_p_rpm"])
        moved = simulate(dataclasses.replace(self.settings, source_x_mm=450), backend="CPU")
        self.assertGreater(abs(moved["cv_percent"] - baseline["cv_percent"]), 0.05)

    def test_mask_hole_and_full_occlusion(self):
        s = dataclasses.replace(self.settings, carrier_rpm=0, planet_offset_mm=0,
                                source_x_mm=0, source_y_mm=0)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "mask.json"
            full = [[-1000,-1000],[1000,-1000],[1000,1000],[-1000,1000]]
            p.write_text(json.dumps({"z_mm": 500, "outer": full}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "无沉积通量"):
                simulate(s, read_mask(str(p)), "CPU")
            hole = [[-25,-25],[25,-25],[25,25],[-25,25]]
            p.write_text(json.dumps({"z_mm": 500, "outer": full,
                                     "holes": [hole]}), encoding="utf-8")
            r = simulate(s, read_mask(str(p)), "CPU")
            self.assertGreater(r["field"][15, 15], 0)
            self.assertEqual(r["field"][15, 30], 0)
            self.assertGreater(workload(s, read_mask(str(p))), workload(s))

    def test_cancellation_stops_the_computation(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(InterruptedError):
            simulate(self.settings, backend="CPU", cancel_event=cancel)


if __name__ == "__main__":
    unittest.main()
