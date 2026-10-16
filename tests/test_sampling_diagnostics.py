"""Diagnostics never silently change sampling or physical integration."""
import copy
from pathlib import Path
import tempfile
import unittest
import numpy as np
from coat_sim.core.api import CoatService
from coat_sim.core.process_simulator import simulate_process, process_sampling_plan
from coat_sim.core.simulator import Settings

ROOT = Path(__file__).resolve().parents[1]

class SamplingDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        from tests.design_test_fixtures import copy_typed_examples
        self.temp_root = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_root.cleanup)
        copy_typed_examples(Path(self.temp_root.name))
        self.runtime = CoatService(Path(self.temp_root.name)).load('process', 'examples/process_example.cproc').runtime

    def test_warning_preserves_requested_count_and_tracks_each_layer(self):
        for step in self.runtime['profile']['steps']:
            step['target_mode'] = 'mean'
            step.pop('monitor_slot', None)
        settings = Settings(grid_size=21, time_steps=12)
        out = simulate_process(settings, self.runtime, backend='CPU')
        self.assertEqual(out['computed_time_steps'], 12)
        self.assertTrue(out['sampling_warnings'])
        self.assertTrue(any(s['below_fast_angular_heuristic'] for s in out['step_results']))
        elapsed = 0.
        for step in out['step_results']:
            self.assertAlmostEqual(step['effective_settings']['carrier_phase_deg'],
                                   settings.carrier_rpm * 6 * elapsed % 360)
            elapsed += step['duration_s']
        self.assertAlmostEqual(np.nanmean(out['field_nm']), out['total_target_nm'])

    def test_stationary_same_source_splitting_invariant(self):
        runtime = copy.deepcopy(self.runtime)
        runtime['profile']['steps'] = [copy.deepcopy(runtime['profile']['steps'][0])]
        runtime['profile']['steps'][0]['target_mode'] = 'mean'
        runtime['profile']['steps'][0]['target_nm'] = 7000.
        settings = Settings(grid_size=21,time_steps=120,carrier_rpm=0)
        one = simulate_process(settings,runtime,backend='CPU')
        layer = copy.deepcopy(runtime['profile']['steps'][0]); layer['target_nm'] /= 60
        runtime['profile']['steps'] = [copy.deepcopy(layer) for _ in range(60)]
        split = simulate_process(settings,runtime,backend='CPU')
        np.testing.assert_allclose(one['field_nm'],split['field_nm'],rtol=1e-12,atol=1e-9)
        self.assertEqual(split['sampling_warnings'],[])
        self.assertTrue(all(s['samples_per_carrier_revolution'] is None for s in split['step_results']))

    def test_fast_sampling_diagnostic_is_not_an_accuracy_claim(self):
        plan = process_sampling_plan(Settings(grid_size=21,time_steps=12),self.runtime,fast_cycle=True)
        self.assertTrue(all(not p['below_fast_angular_heuristic'] for p in plan['steps']))

if __name__ == '__main__': unittest.main()
