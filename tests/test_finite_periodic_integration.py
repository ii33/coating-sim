"""Finite layer integrals, bounded cache and the automatic reference default."""
import copy
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
import numpy as np
from coat_sim.core.api import CoatService
from coat_sim.core.algorithm_paths import run_recipe
from coat_sim.core.periodic_integration import PeriodicCache
from coat_sim.core.process_simulator import simulate_process
from coat_sim.core.simulator import Settings

ROOT=Path(__file__).resolve().parents[1]
class FinitePeriodicTests(unittest.TestCase):
    def setUp(self):
        from tests.design_test_fixtures import copy_typed_examples
        self.temp_root = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_root.cleanup)
        copy_typed_examples(Path(self.temp_root.name))
        self.runtime=CoatService(Path(self.temp_root.name)).load('process','examples/process_example.cproc').runtime
        self.settings=Settings(grid_size=21,time_steps=2400)

    def test_fractional_layers_against_dense_full_time(self):
        r=copy.deepcopy(self.runtime)
        # No masks: smooth quadrature comparison exercises source/monitor/center.
        for step in r['profile']['steps']: step['mask_slots']=[]
        for rpm in (15.,-15.):
            s=replace(self.settings,carrier_rpm=rpm,carrier_phase_deg=13.,planet_phase_deg=27.)
            direct=simulate_process(replace(s,time_steps=19200),r,backend='CPU')
            reduced=simulate_process(s,r,backend='CPU',finite_periodic=True)
            delta=np.nanmax(abs(direct['field_nm']-reduced['field_nm']))/np.nanmean(direct['field_nm'])*100
            self.assertLess(delta,.01)
            self.assertLess(reduced['computed_time_steps'],s.time_steps)
            self.assertTrue(any(x['finite_periodic_integration'] for x in reduced['step_results']))

    def test_exact_period_and_stationary(self):
        r=copy.deepcopy(self.runtime)
        r['profile']['steps']=[dict(source_slot=1,target_nm=120.,rate_nm_s=1.,target_mode='mean',mask_slots=[])]
        for rpm in (0.,15.):
            s=replace(self.settings,carrier_rpm=rpm)
            a=simulate_process(s,r,backend='CPU')
            b=simulate_process(s,r,backend='CPU',finite_periodic=True)
            np.testing.assert_allclose(a['field_nm'],b['field_nm'],rtol=1e-10,atol=1e-9)

    def test_cancellation_and_cache_bound(self):
        event=threading.Event();event.set()
        with self.assertRaises(InterruptedError):
            simulate_process(self.settings,self.runtime,backend='CPU',finite_periodic=True,cancel_event=event)
        c=PeriodicCache(max_bytes=80,max_entries=2)
        for i in range(5):c.put(i,{'field':np.ones(5)})
        self.assertEqual(len(c.entries),2);self.assertLessEqual(c.bytes,80)
        self.assertIsNone(c.get(0));self.assertIsNotNone(c.get(4))
        c.put(4,{'field':np.ones(5)})
        self.assertEqual(c.bytes,80)

    def test_reference_default_starts_from_motor_resolution(self):
        r=copy.deepcopy(self.runtime)
        for step in r['profile']['steps']:step['mask_slots']=[]
        s=replace(self.settings,time_steps=600)
        out=run_recipe(s,r,algorithm='reference',backend='CPU')
        report=out['algorithm_report']
        self.assertGreater(report['initial_time_steps'],600)
        self.assertTrue(report['finite_periodic_integration'])
        self.assertEqual(out['requested_settings']['time_steps'],600)
        self.assertTrue(report['converged'])
        self.assertIn('空间网格',report['unverified_dimensions'])
        self.assertIsNone(report['budget_s'])

    def test_legacy_fast_is_unchanged(self):
        a=simulate_process(self.settings,self.runtime,backend='CPU')
        b=run_recipe(self.settings,self.runtime,algorithm='fast',backend='CPU')
        np.testing.assert_array_equal(a['field_nm'],b['field_nm'])

if __name__=='__main__':unittest.main()
