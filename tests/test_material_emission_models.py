"""Material snapshots and opt-in source-footprint/cover/rate behavior."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch

import numpy as np

from coat_sim.core.materials import (default_material_profile, validate_material_profile,
    read_material_profile, save_material, list_materials, delete_material,
    material_emission_model, material_completeness)
from coat_sim.core.source_profiles import (validate_source_profile, source_samples,
    effective_source_exponent, source_cover, source_visibility, source_rate_cache_key)
from coat_sim.core.simulator import Settings, simulate
from coat_sim.core.process_simulator import _monitor_raw_flux, simulate_process
from tests.design_test_fixtures import copy_typed_examples

class MaterialRecordsTests(unittest.TestCase):
    def test_library_create_read_update_delete_and_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = default_material_profile("自定义材料", id="test_material")
            saved = save_material(folder, profile)
            self.assertEqual(list_materials(folder), [saved])
            self.assertEqual(read_material_profile(Path(folder) / 'test_material.json'), saved)
            source = validate_source_profile({"kind": "disk", "radius_mm": 10,
                "device_type": "electron_beam", "material": saved})
            with self.assertRaises(ValueError):
                save_material(folder, profile)
            profile['name'] = '新名字'
            profile['electron_beam_model']['defaults']['spot_sigma_mm'] = 8
            save_material(folder, profile, overwrite=True)
            self.assertEqual(source['material']['name'], '自定义材料')
            self.assertEqual(source['emission_model']['spot_sigma_mm'], 3)
            delete_material(folder, profile['id'])
            self.assertEqual(list_materials(folder), [])
            # No library filesystem reads are required after source resolution.
            source_samples(Settings(), source)

    def test_finiteness_provenance_and_completeness(self):
        profile = default_material_profile(id="test")
        self.assertFalse(material_completeness(profile)['thermodynamic_rate_available'])
        profile['resistance_model']['provenance']['status'] = 'calibrated'
        with self.assertRaisesRegex(ValueError, '来源'):
            validate_material_profile(profile)
        profile['resistance_model']['provenance']['reference'] = '实验记录 A'
        self.assertTrue(material_completeness(profile)['models']['resistance_model']['calibration_declared'])
        for value in (True, float('inf'), float('nan')):
            profile['resistance_model']['defaults']['active_fraction'] = value
            with self.assertRaises(ValueError):
                validate_material_profile(profile)
        with self.assertRaises(ValueError):
            default_material_profile(id='../escape')

    def test_material_name_is_not_a_hidden_physics_constant(self):
        material = default_material_profile("first", id="sample")
        a = {'kind': 'disk', 'radius_mm': 10, 'device_type': 'electron_beam', 'material': material}
        b = deepcopy(a)
        b['material']['name'] = 'second'
        pa, wa = source_samples(Settings(), a)
        pb, wb = source_samples(Settings(), b)
        np.testing.assert_array_equal(pa, pb)
        np.testing.assert_array_equal(wa, wb)

    def test_material_defaults_are_independent_by_heater(self):
        material = default_material_profile(id="sample")
        material['resistance_model']['defaults']['source_exponent'] = 4
        material['electron_beam_model']['defaults']['source_exponent'] = 2
        for kind, expected in (('resistance_boat', 4), ('electron_beam', 2)):
            p = validate_source_profile({'kind': 'point', 'device_type': kind, 'material': material})
            self.assertEqual(p['source_exponent'], expected)
            self.assertEqual(p['emission_model']['kind'], kind)
        self.assertNotIn('source_exponent', material_emission_model(material, 'resistance_boat'))


class SpatialSourceTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(grid_size=21, time_steps=2, carrier_rpm=0,
            planet_offset_mm=0, source_x_mm=0, substrate_z_mm=100,
            substrate_radius_mm=40)

    def model(self, kind='resistance_boat', **kwargs):
        return validate_source_profile({'kind': 'rectangle', 'length_mm': 40,
            'width_mm': 20, 'emission_model': {'kind': kind,
                **({'apertures': [{'kind': 'rectangle', 'length_mm': 20, 'width_mm': 10}]}
                   if kind == 'resistance_boat' else {}), **kwargs}})

    def test_legacy_geometry_and_zero_response_preserve_rate_independence(self):
        for p, expected in (({'kind': 'point'}, 1), ({'kind': 'line', 'length_mm': 8}, 9),
                            ({'kind': 'disk', 'radius_mm': 8}, 12),
                            ({'kind': 'rectangle', 'length_mm': 8, 'width_mm': 4}, 16)):
            positions, weights = source_samples(self.settings, p)
            self.assertEqual(len(weights), expected)
            self.assertAlmostEqual(weights.sum(), 1)
            self.assertTrue(np.isfinite(positions).all())
        profile = self.model()
        a = source_samples(self.settings, profile, rate_nm_s=1)
        b = source_samples(self.settings, profile, rate_nm_s=10)
        np.testing.assert_array_equal(a[0], b[0]); np.testing.assert_array_equal(a[1], b[1])
        self.assertIsNone(source_rate_cache_key(profile, 10))

    def test_legacy_unused_high_rate_remains_accepted(self):
        settings = replace(self.settings, source_rate_nm_s=1e12)
        settings.validate()
        a = source_samples(self.settings, {'kind': 'point'})
        b = source_samples(settings, {'kind': 'point'})
        np.testing.assert_array_equal(a[0], b[0])
        np.testing.assert_array_equal(a[1], b[1])
        with self.assertRaises(ValueError):
            source_samples(settings, self.model())

    def test_explicit_rate_response_changes_width_and_exponent(self):
        p = self.model(active_fraction=.5, rate_active_fraction_per_decade=.2,
                       rate_exponent_per_decade=1)
        p1, _ = source_samples(self.settings, p, rate_nm_s=1)
        p10, _ = source_samples(self.settings, p, rate_nm_s=10)
        self.assertGreater(np.max(abs(p10[:, 0])), np.max(abs(p1[:, 0])))
        self.assertEqual(effective_source_exponent(self.settings, p, 10), 2)
        self.assertEqual(source_rate_cache_key(p, 10), 10)
        with self.assertRaises(ValueError):
            source_samples(self.settings, p, rate_nm_s=1e9)
        bounded = self.model(valid_rate_min_nm_s=.5, valid_rate_max_nm_s=2)
        with self.assertRaisesRegex(ValueError, '有效速率'):
            source_samples(self.settings, bounded, rate_nm_s=10)

    def test_beam_dwell_spot_scan_and_dimensions_affect_actual_weights(self):
        narrow = self.model('electron_beam', spot_sigma_mm=1)
        broad = self.model('electron_beam', spot_sigma_mm=8)
        a, wa = source_samples(self.settings, narrow)
        b, wb = source_samples(self.settings, broad)
        self.assertGreater(float(np.sum(b[:, 0] ** 2 * wb)), float(np.sum(a[:, 0] ** 2 * wa)))
        custom = self.model('electron_beam', spot_sigma_mm=3, scan_pattern='custom',
            dwell_points=[{'x_mm': -10, 'y_mm': 0, 'weight': 1}, {'x_mm': 10, 'y_mm': 0, 'weight': 4}])
        pos, weights = source_samples(self.settings, custom)
        self.assertGreater(float(np.sum(pos[:, 0] * weights)), 0)
        smaller = deepcopy(custom); smaller['length_mm'] = 10
        sp, sw = source_samples(self.settings, smaller)
        self.assertLess(float(np.sum(sp[:, 0] * sw)), float(np.sum(pos[:, 0] * weights)))
        rotated = self.model('electron_beam', spot_sigma_mm=1, scan_pattern='line',
                             scan_width_mm=12, scan_angle_deg=90)
        rp, rw = source_samples(self.settings, rotated)
        self.assertGreater(np.sum(rp[:, 1]**2 * rw), np.sum(rp[:, 0]**2 * rw))
        with self.assertRaises(ValueError):
            self.model('electron_beam', scan_pattern='custom', dwell_points=[{'x_mm': 0, 'y_mm': 0, 'weight': 0}])

    def test_disk_narrow_centered_beam_has_central_node_and_thin_annulus_survives(self):
        p = {'kind': 'disk', 'radius_mm': 10, 'emission_model': {'kind': 'electron_beam', 'spot_sigma_mm': .001}}
        xyz, weights = source_samples(self.settings, p)
        self.assertLess(np.sum(np.sum(xyz[:, :2] ** 2, axis=1) * weights), 1e-10)
        p.update(kind='annulus', inner_radius_mm=9.999, outer_radius_mm=10)
        p.pop('radius_mm')
        xyz, weights = source_samples(self.settings, p)
        self.assertTrue((np.linalg.norm(xyz[:, :2], axis=1) >= 9.999).all())
        self.assertAlmostEqual(weights.sum(), 1)

    def test_cover_visibility_depends_on_receiver_and_checks_both_faces(self):
        p = self.model(boat_mode='covered', cover_height_mm=10,
                       apertures=[{'kind': 'rectangle', 'length_mm': 2, 'width_mm': 20}])
        cover = source_cover(p, self.settings)
        x, y = np.array([0., 8., 20.]), np.zeros(3)
        visibility = source_visibility(np, x, y, 100., 0., 0., 0., cover)
        np.testing.assert_array_equal(visibility, [True, True, False])
        cover['cover_thickness_mm'] = 10
        np.testing.assert_array_equal(source_visibility(np, x, y, 100., 0., 0., 0., cover), [True, False, False])
        cover['angle_deg'] = 90
        self.assertTrue(source_visibility(np, np.array([20.]), np.array([0.]), 100, 0, 0, 0, cover)[0])

    def test_cover_loss_is_real_and_no_transmitted_weight_renormalization(self):
        open_p: dict[str, Any] = {'kind': 'point', 'emission_model': {'kind': 'resistance_boat',
                  'apertures': [{'kind': 'disk', 'radius_mm': 10}]}}
        closed = deepcopy(open_p)
        closed['emission_model'].update(boat_mode='covered', cover_height_mm=50)
        a = simulate(self.settings, source_profile=open_p, backend='CPU')
        b = simulate(self.settings, source_profile=closed, backend='CPU')
        self.assertLess(b['mean_raw'], a['mean_raw'])
        raw_a, raw_b = a['field'] * a['mean_raw'], b['field'] * b['mean_raw']
        self.assertTrue(np.all(raw_b[np.isfinite(raw_b)] <= raw_a[np.isfinite(raw_a)] + 1e-18))
        self.assertAlmostEqual(raw_a[10, 10], raw_b[10, 10])
        self.assertEqual(_monitor_raw_flux(self.settings, closed,
            {'x_mm': 30, 'y_mm': 0, 'z_mm': 100}, None, None), 0)
        self.assertGreater(_monitor_raw_flux(self.settings, closed,
            {'x_mm': 0, 'y_mm': 0, 'z_mm': 100}, None, None), 0)

    def test_mask_preview_excludes_rays_already_absorbed_by_source_cover(self):
        from coat_sim.core.mask_analysis import correction_board_blocking_weights
        settings = replace(self.settings, planet_offset_mm=15, substrate_radius_mm=10)
        p = {'kind': 'point', 'emission_model': {'kind': 'resistance_boat',
            'boat_mode': 'covered', 'cover_height_mm': 50,
            'apertures': [{'kind': 'disk', 'radius_mm': 10}]}}
        board = {'outer': [[11,-100], [100,-100], [100,100], [11,100]], 'holes': [], 'z_mm': 50}
        closed = correction_board_blocking_weights(settings, board, radii_mm=[0, 5, 10], source_profile=p)
        p['emission_model']['boat_mode'] = 'open'
        opened = correction_board_blocking_weights(settings, board, radii_mm=[0, 5, 10], source_profile=p)
        np.testing.assert_array_equal(closed['weights_percent'], [0, 0, 0])
        self.assertGreater(opened['weights_percent'][-1], 0)

    def test_monitor_uses_rate_dependent_exponent(self):
        p = self.model(rate_exponent_per_decade=1)
        monitor = {'x_mm': 50, 'y_mm': 0, 'z_mm': 100}
        a = _monitor_raw_flux(replace(self.settings, source_rate_nm_s=1), p, monitor, None, None)
        b = _monitor_raw_flux(replace(self.settings, source_rate_nm_s=10), p, monitor, None, None)
        self.assertLess(b, a)


class RateCacheTests(unittest.TestCase):
    def test_same_slot_different_responsive_rates_cannot_reuse_cycle(self):
        from coat_sim.core.api import CoatService
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copy_typed_examples(root)
            runtime = CoatService(root).load('process', 'examples/process_example.cproc').runtime
        runtime = deepcopy(runtime)
        runtime['profile']['steps'] = [dict(source_slot=1, target_nm=120., rate_nm_s=rate,
            target_mode='mean', mask_slots=[]) for rate in (1., 10.)]
        runtime['machine']['sources'][0]['profile'] = validate_source_profile({'kind': 'point',
            'emission_model': {'kind': 'resistance_boat', 'rate_exponent_per_decade': 1,
                               'apertures': [{'kind': 'rectangle'}]}})
        with patch('coat_sim.core.process_simulator.simulate', wraps=simulate) as mocked:
            out = simulate_process(Settings(grid_size=21, time_steps=240), runtime,
                                   backend='CPU', fast_cycle=True)
            self.assertEqual(mocked.call_count, 2)
        self.assertNotEqual(out['step_results'][0]['mean_raw'], out['step_results'][1]['mean_raw'])
        runtime['machine']['sources'][0]['profile']['emission_model']['rate_exponent_per_decade'] = 0
        with patch('coat_sim.core.process_simulator.simulate', wraps=simulate) as mocked:
            simulate_process(Settings(grid_size=21, time_steps=240), runtime,
                             backend='CPU', fast_cycle=True)
            self.assertEqual(mocked.call_count, 1)


if __name__ == '__main__':
    unittest.main()
