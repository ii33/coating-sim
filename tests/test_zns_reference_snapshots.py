"""The reference calculator is portable metadata, not a hidden process-rate change."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from coat_sim.core.api import CoatService
from coat_sim.core.materials import builtin_material_profiles, default_material_profile, validate_material_profile
from coat_sim.core.material_physics import evaluate_source_thermochemistry
from coat_sim.core.source_profiles import validate_source_profile
from coat_sim.core.schemas import design_data
from coat_sim.core.simulator import Settings
from coat_sim.core.input_snapshot import freeze_process, runtime_from_snapshot, canonical_json_bytes
from coat_sim.core.simulation_assets import save_simulation_input_asset, read_simulation_asset, export_simulation_asset
from tests.test_input_snapshot import fixture


class ZnSReferenceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service, self.recipe = fixture(self.root, mask_count=0)
        self.source_path = self.root / 'parts/source.csrc'
        self.source = json.loads(self.source_path.read_text())
        self.source['material'] = builtin_material_profiles()[0]
        self.source['thermochemistry'] = {'model_id': 'zns_ri8301_gas_v1',
            'temperature_K': 1200, 'closure': 'ideal_effusion', 'emitting_area_m2': 1e-6}
        self.service.save('source', self.source_path, self.source, overwrite=True)
        self.source = self.service.load('source', self.source_path).data

    def test_builtin_copy_and_typed_roundtrip_keep_reference_data(self):
        self.assertEqual(design_data('source', self.source), self.source)
        original = builtin_material_profiles()[0]
        changed = deepcopy(original)
        changed['name'] = 'My ZnS batch'
        self.assertEqual(validate_material_profile(changed)['reference_data'], original['reference_data'])
        changed['reference_data']['models']['zns_vendor_pressure_v1']['coefficients']['A'] = 50
        with self.assertRaisesRegex(ValueError, '快照|参考'):
            validate_material_profile(changed)
        self.assertEqual(builtin_material_profiles()[0], original)

    def test_source_calculator_uses_frozen_data_without_user_library(self):
        snap = freeze_process(self.service, self.recipe, self.root)
        expected = self.service.source_material_reference(self.source_path)
        before = canonical_json_bytes(snap)
        self.source_path.unlink()
        profile = runtime_from_snapshot(snap)['machine']['sources'][0]['profile']
        self.assertEqual(evaluate_source_thermochemistry(profile), expected)
        self.assertEqual(canonical_json_bytes(snap), before)
        self.assertFalse((self.root / 'data/materials').exists())

    def test_asset_export_carries_full_data_and_calculator_inputs(self):
        snap = freeze_process(self.service, self.recipe, self.root)
        asset = save_simulation_input_asset(self.root, name='ZnS reference', settings={},
                    input_snapshot=snap, confirmed=True)['asset_path']
        content = read_simulation_asset(asset, detail='full')
        source = next(row for row in content['designs'] if row['kind']=='source')
        exported = export_simulation_asset(asset, self.root/'exported', members=[source['path']])
        path = next(row['path'] for row in exported['designs'] if row['kind']=='source')
        data = CoatService(self.root).load('source', path).data
        self.assertEqual(data['material']['reference_data'], self.source['material']['reference_data'])
        self.assertEqual(data['thermochemistry'], self.source['thermochemistry'])
        self.assertEqual(evaluate_source_thermochemistry(data), evaluate_source_thermochemistry(self.source))

    def test_reference_inputs_never_change_process_rate_or_thickness(self):
        before = deepcopy(self.recipe)
        settings = Settings(grid_size=21,time_steps=4,carrier_rpm=0)
        with_reference = freeze_process(self.service, self.recipe, self.root)
        self.service.source_material_reference(self.source)
        plain = deepcopy(self.source); plain.pop('thermochemistry')
        self.service.save('source', self.source_path, plain, overwrite=True)
        without_reference = freeze_process(self.service, self.recipe, self.root)
        actual = self.service.simulate_recipe(settings, process_path=with_reference, backend='CPU')
        expected = self.service.simulate_recipe(settings, process_path=without_reference, backend='CPU')
        # Reference report is deliberately outside the existing thickness solver.
        np.testing.assert_array_equal(actual['field_nm'],expected['field_nm'])
        self.assertEqual(self.recipe, before)
        self.assertEqual(with_reference['runtime']['profile']['steps'], without_reference['runtime']['profile']['steps'])

    def test_name_alone_does_not_enable_zns_physics_and_unknowns_remain_unknown(self):
        source = deepcopy(self.source)
        source['material'] = default_material_profile('ZnS',id='just_name')
        with self.assertRaisesRegex(ValueError, 'ZnS'):
            validate_source_profile(source)
        source = deepcopy(self.source)
        source['thermochemistry'] = {'model_id':'zns_ri8301_gas_v1',
                                     'temperature_K':1200,'closure':'pressure_ratio','pressure_ratio':2}
        report = self.service.source_material_reference(source)
        self.assertEqual(report['status'],'missing_inputs')
        self.assertIsNotNone(report['pressure'])
        self.assertIsNone(report['post_transmission'])
        self.assertIsNone(report['resolved_alpha']['Zn'])
