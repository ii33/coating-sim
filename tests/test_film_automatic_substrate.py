"""Approved automatic substrate policy and cache invalidation regression."""
import copy
import json
import os
import re
import unittest
import numpy as np
from tests import test_film_design_service as fixtures
from coat_sim.core.film_design_service import simulate_frozen

class AutomaticSubstrateTests(unittest.TestCase):
    setUp=fixtures.FilmServiceTests.setUp
    tearDown=fixtures.FilmServiceTests.tearDown

    def test_legacy_modes_migrate_and_single_ignores_exit_and_back_drafts(self):
        baseline=self.service.simulate(self.design,self.settings)
        for mode in ('auto','semi_infinite','incoherent','coherent'):
            design=copy.deepcopy(self.design); design['substrate']['mode']=mode
            design['exit_medium']={'kind':'material','material_id':'missing','optical_set_id':'absent'}
            design['back_layers']=[{'id':'draft','material_id':'missing','optical_set_id':'absent','note':'keep',
                                   'thickness':{'value':200,'unit':'nm'}}]
            result=self.service.simulate(design,self.settings)
            np.testing.assert_array_equal(result['R'],baseline['R'])
            self.assertEqual(result['design']['substrate']['mode'],'auto')
            self.assertEqual(len(result['design']['back_layers']),1)
            frozen=self.service.freeze_design(design,self.settings)
            np.testing.assert_array_equal(simulate_frozen(self.root,frozen)['T'],baseline['T'])
            self.assertTrue(result['substrate_handling']['exit_medium_ignored'])

    def test_double_empty_back_still_has_rear_interface_and_thin_fringes(self):
        single=self.service.simulate(self.design,self.settings)
        self.design['substrate'].update(sides='double',thickness={'value':1000.,'unit':'nm'})
        one=self.service.simulate(self.design,self.settings)
        self.assertEqual(one['substrate_handling']['resolved_mode'],'coherent')
        self.assertGreater(max(abs(np.array(one['R'])-single['R'])),.01)
        self.design['substrate']['thickness']['value']=1100
        two=self.service.simulate(self.design,self.settings)
        self.assertGreater(max(abs(np.array(one['R'])-two['R'])),.01)
        self.assertEqual(one['substrate_handling']['phase_sampling']['convergence_verified'],False)

    def test_strict_whole_band_threshold_and_transparent_thick_invariance(self):
        self.design['substrate'].update(sides='double',thickness={'value':6.,'unit':'um'})
        boundary=self.service.simulate(self.design,self.settings)
        self.assertEqual(boundary['substrate_handling']['resolved_mode'],'coherent')
        self.design['substrate']['thickness']['value']=6.001
        thick=self.service.simulate(self.design,self.settings)
        self.assertEqual(thick['substrate_handling']['resolved_mode'],'incoherent')
        self.assertTrue(thick['substrate_handling']['average_fringes_approximation'])
        self.design['substrate']['thickness']={'value':1.,'unit':'mm'}
        thicker=self.service.simulate(self.design,self.settings)
        np.testing.assert_array_equal(thick['R'],thicker['R'])
        np.testing.assert_array_equal(thick['T'],thicker['T'])

    def test_coherent_under_sampling_is_reported_without_altering_grid(self):
        self.design['substrate'].update(sides='double',thickness={'value':6000.,'unit':'nm'})
        settings={**self.settings,'samples':2}
        result=self.service.simulate(self.design,settings)
        self.assertEqual(result['wavelength_nm'],[500.,600.])
        self.assertFalse(result['substrate_handling']['phase_sampling']['nyquist_necessary_condition_met'])
        self.assertTrue(any('采样不足' in warning for warning in result['warnings']))

    def test_cache_reads_detached_data_and_invalidates_same_size_timestamp_change(self):
        first=self.service.materials.get('fixture_glass')
        first['optical_sets'][0]['n'][0]=9
        self.assertEqual(self.service.materials.get('fixture_glass')['optical_sets'][0]['n'][0],1.5)
        path=next(p for p in (self.root/'data/materials').glob('*.json') if json.loads(p.read_text('utf-8'))['id']=='fixture_glass')
        raw=path.read_text('utf-8'); stamp=path.stat()
        changed=re.sub(r'("n"\s*:\s*\[\s*)1\.5',r'\g<1>1.6',raw,count=1)
        self.assertNotEqual(raw,changed); self.assertEqual(len(raw),len(changed))
        path.write_text(changed,encoding='utf-8'); os.utime(path,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
        self.assertEqual(self.service.materials.get('fixture_glass')['optical_sets'][0]['n'][0],1.6)
        path.write_text('{invalid',encoding='utf-8')
        with self.assertRaises((ValueError,OSError)): self.service.materials.get('fixture_glass')
        path.unlink()
        with self.assertRaises(ValueError): self.service.materials.get('fixture_glass')

if __name__=='__main__': unittest.main()
