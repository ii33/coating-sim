"""Small CPU scientific and provenance regressions for process gas conditions."""
from copy import deepcopy
from dataclasses import replace
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np

from coat_sim.core.process_environment import validate_environment, resolve_environment, environment_report
from coat_sim.core.process_profiles import validate_process_profile
from coat_sim.core.process_simulator import simulate_process, _monitor_raw_flux
from coat_sim.core.simulator import Settings, simulate
from coat_sim.core.schemas import ProcessDesign, design_data
from coat_sim.core.input_snapshot import freeze_process, runtime_from_snapshot, canonical_json_bytes
from coat_sim.core.simulation_assets import save_simulation_input_asset, read_simulation_asset, export_simulation_asset
from coat_sim.core.film_stack import build_film_stack
from tests.test_input_snapshot import fixture


def env(**updates):
    result = dict(transport_model='ballistic_loss', operating_pressure_pa=.01,
                  gas_temperature_k=300., effective_cross_section_m2=1e-19)
    result.update(updates)
    return result


class EnvironmentContractTests(unittest.TestCase):
    def test_oracle_temperature_pressure_and_mixture(self):
        alpha = environment_report(env())['attenuation_coefficient_m_inv']
        self.assertAlmostEqual(alpha, .24143235053466402)
        self.assertAlmostEqual(environment_report(env(gas_temperature_k=600))['attenuation_coefficient_m_inv'], alpha / 2)
        self.assertAlmostEqual(environment_report(env(operating_pressure_pa=.02))['attenuation_coefficient_m_inv'], alpha * 2)
        mixture = env(effective_cross_section_m2=None, gas_components=[
            {'species': 'Ar', 'partial_pressure_pa': .004, 'effective_cross_section_m2': 1e-19, 'flow_sccm': 5},
            {'species': 'O2', 'partial_pressure_pa': .006, 'effective_cross_section_m2': 2e-19}])
        report = environment_report(mixture)
        self.assertAlmostEqual(report['attenuation_coefficient_m_inv'], alpha * 1.6)
        self.assertEqual(report['component_status'][0]['flow_sccm'], 'not_modeled')
        self.assertEqual(report['component_status'][1]['reactive_chemistry'], 'not_modeled')

    def test_base_pressure_never_adds_and_unmodeled_is_explicit(self):
        base = environment_report(env())
        changed = environment_report(env(base_pressure_pa=1., source_temperature_k=1800., substrate_temperature_k=600.))
        self.assertEqual(base['attenuation_coefficient_m_inv'], changed['attenuation_coefficient_m_inv'])
        for key in ('base_pressure_pa', 'source_temperature_k', 'substrate_temperature_k'):
            self.assertEqual(changed['parameter_status'][key], 'not_modeled')
        self.assertIsNone(environment_report({'gas_temperature_k': 300, 'operating_pressure_pa': 1})['attenuation_coefficient_m_inv'])

    def test_unknowns_invalid_and_zero_pressure(self):
        for bad in ({'gas_temperature_k': 0}, {'operating_pressure_pa': -1}, {'source_temperature_k': float('nan')},
                    {'effective_cross_section_m2': True}, {'invented': 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_environment(bad)
        for bad in (env(effective_cross_section_m2=None), env(gas_temperature_k=None),
                    env(effective_cross_section_m2=None, gas_components=[{'species':'O2','partial_pressure_pa':.01}]),
                    env(effective_cross_section_m2=None, gas_components=[{'species':'Ar','partial_pressure_pa':.005,'effective_cross_section_m2':1e-19}])):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                environment_report(bad)
        self.assertEqual(environment_report(env(operating_pressure_pa=0, gas_temperature_k=None, effective_cross_section_m2=None))['attenuation_coefficient_m_inv'], 0)

    def test_extreme_finite_parameters_rejected_without_nonfinite_outputs(self):
        for bad in (env(gas_temperature_k=1e-310), env(gas_temperature_k=1e300, effective_cross_section_m2=1e-40),
                    env(operating_pressure_pa=0, effective_cross_section_m2=None,
                        gas_components=[{'species':'Ar','partial_pressure_pa':1}, {'species':'O2'}]),
                    env(operating_pressure_pa=0, effective_cross_section_m2=None,
                        gas_components=[{'species':'Ar','partial_pressure_pa':1e-30}])):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                environment_report(bad)

    def test_override_clear_and_array_replacement(self):
        defaults = env(substrate_temperature_k=400)
        self.assertNotIn('substrate_temperature_k', resolve_environment(defaults, {'substrate_temperature_k': None}))
        self.assertEqual(resolve_environment(defaults, {})['substrate_temperature_k'], 400)
        self.assertEqual(resolve_environment({'gas_components':[{'species':'Ar'}]}, {'gas_components':[]})['gas_components'], [])


class EnvironmentIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service, self.recipe = fixture(self.root, mask_count=0)
        self.settings = Settings(grid_size=21, time_steps=8, carrier_rpm=0)

    def run_recipe(self, recipe, **options):
        return simulate_process(self.settings, recipe, process_base_dir=self.root, backend='CPU', **options)

    def test_exact_legacy_disabled_zero_identity_and_shape_sensitivity(self):
        legacy = self.run_recipe(self.recipe)
        zero = deepcopy(self.recipe); zero['environment'] = env(operating_pressure_pa=0)
        np.testing.assert_array_equal(legacy['field_nm'], self.run_recipe(zero)['field_nm'])
        disabled = deepcopy(self.recipe); disabled['environment'] = env(transport_model='disabled', substrate_temperature_k=900)
        np.testing.assert_array_equal(legacy['field_nm'], self.run_recipe(disabled)['field_nm'])
        active = deepcopy(self.recipe); active['environment'] = env(operating_pressure_pa=.3)
        result = self.run_recipe(active)
        self.assertFalse(np.allclose(result['field_nm'], legacy['field_nm'], equal_nan=True))
        e = result['step_results'][0]['environment']
        self.assertLess(e['direct_beam_survival'], 1)
        self.assertGreater(e['direct_beam_survival'], 0)
        self.assertAlmostEqual(result['step_results'][0]['mean_nm'], 1)
        self.assertIn('absolute_net_growth_not_predicted', e['thickness_semantics'])
        self.assertNotEqual(result['step_results'][1]['mean_nm'], legacy['step_results'][1]['mean_nm'])

    def test_ray_oracle_and_monitor_parity(self):
        settings = Settings(grid_size=21, time_steps=1, carrier_rpm=0, planet_offset_mm=0,
                            substrate_radius_mm=1, substrate_z_mm=1000, source_x_mm=0, beam_attenuation_m_inv=.24143235053466402)
        result = simulate(settings, backend='CPU')
        self.assertAlmostEqual(result['direct_beam_survival'], math.exp(-settings.beam_attenuation_m_inv), places=6)
        source = {'schema_version':2,'kind':'point','device_type':'custom','source_exponent':1.}
        monitor = {'x_mm':0.,'y_mm':0.,'z_mm':1000.}
        vacuum = _monitor_raw_flux(replace(settings, beam_attenuation_m_inv=None), source, monitor, [], None)
        attenuated = _monitor_raw_flux(settings, source, monitor, [], None)
        self.assertAlmostEqual(attenuated/vacuum, math.exp(-settings.beam_attenuation_m_inv))

    def test_step_override_fast_cache_and_finite_periodic_diagnostics(self):
        recipe = deepcopy(self.recipe); recipe['environment'] = env()
        recipe['steps'] = [dict(source_slot=1, target_nm=20., rate_nm_s=1., target_mode='mean', mask_slots=[]),
                           dict(source_slot=1, target_nm=20., rate_nm_s=1., target_mode='mean', mask_slots=[], environment={'operating_pressure_pa':.1})]
        self.settings = replace(self.settings, carrier_rpm=60, time_steps=80)
        for kwargs in ({'fast_cycle':True,'fast_cycle_residual_percent':100}, {'finite_periodic':True}):
            result = self.run_recipe(recipe, **kwargs)
            a,b = [s['environment'] for s in result['step_results']]
            self.assertGreater(a['direct_beam_survival'], b['direct_beam_survival'])
            for item in (a,b):
                self.assertAlmostEqual(item['direct_beam_survival'], item['attenuated_mean_raw']/item['vacuum_mean_raw'])

    def test_schema_serialization_snapshots_assets_and_no_mutation(self):
        recipe = deepcopy(self.recipe); recipe['environment'] = env(substrate_temperature_k=600)
        recipe['steps'][0]['environment'] = {'substrate_temperature_k':None}
        typed = ProcessDesign.model_validate(recipe)
        self.assertIsNone(design_data('process', typed)['steps'][0]['environment']['substrate_temperature_k'])
        normalized = validate_process_profile(recipe)
        snapshot = freeze_process(self.service, normalized, self.root)
        before = canonical_json_bytes(snapshot)
        recipe['environment']['gas_temperature_k'] = 999
        runtime = runtime_from_snapshot(snapshot)
        self.assertEqual(runtime['profile']['environment']['gas_temperature_k'], 300)
        runtime['profile']['environment']['gas_temperature_k'] = 800
        self.assertEqual(canonical_json_bytes(snapshot), before)
        saved = save_simulation_input_asset(self.root, name='gas-environment', settings={}, input_snapshot=snapshot, confirmed=True)
        exported = export_simulation_asset(saved['asset_path'], self.root/'exported')
        process_path = next(Path(i['path']) for i in exported['designs'] if i['kind']=='process')
        loaded = self.service.load('process', process_path).data
        self.assertEqual(loaded['environment'], normalized['environment'])
        self.assertIsNone(loaded['steps'][0]['environment']['substrate_temperature_k'])

    def test_workflow_roundtrip_preserves_cleared_environment(self):
        from coat_sim.core.design_workflow import DesignWorkflowSpec, prepare_design_bundle
        from coat_sim.core.process_environment import model_dump_with_environment
        spec = {'name':'Environment workflow', 'sources':[{'design':{'schema_version':2,'kind':'point','device_type':'custom','source_exponent':1.}}],
                'substrate':{'schema_version':1,'name':'Test','outline':{'kind':'circle','radius_mm':10.},'surface':{'kind':'flat'}},
                'environment':env(substrate_temperature_k=400),
                'steps':[{'source_slot':1,'target_nm':1.,'rate_nm_s':1.,'target_mode':'mean',
                          'environment':{'substrate_temperature_k':None}}], 'settings':{'grid_size':21,'time_steps':2},'backend':'CPU'}
        typed = DesignWorkflowSpec.model_validate(spec)
        serialized = model_dump_with_environment(typed)
        self.assertIsNone(serialized['steps'][0]['environment']['substrate_temperature_k'])
        with patch('coat_sim.core.data_files.PROJECT_ROOT',self.root), patch('coat_sim.core.data_files.DATA_ROOT',self.root/'data'):
            manifest = prepare_design_bundle(self.service,'environment-workflow',serialized)
        saved = self.service.load('process',manifest['paths']['process']).data
        self.assertEqual(saved['environment'], spec['environment'])
        self.assertIsNone(saved['steps'][0]['environment']['substrate_temperature_k'])

    def test_generator_defaults_and_binding_overrides(self):
        payload = dict(mode='equal', name='Gas stack', machine_file='machines/machine.json', substrate_file='parts/substrate.json',
            environment=env(substrate_temperature_k=400), materials=[dict(material='A',source_slot=1,rate_nm_s=1.,target_mode='mean',
            environment={'substrate_temperature_k':None,'gas_temperature_k':600})], sequence=['A'], layer_count=3,total_thickness_nm=30.)
        generated = build_film_stack(payload)['profile']
        self.assertEqual(generated['environment']['gas_temperature_k'], 300)
        self.assertTrue(all(s['environment']['gas_temperature_k']==600 for s in generated['steps']))
        self.assertTrue(all(s['environment']['substrate_temperature_k'] is None for s in generated['steps']))

if __name__ == '__main__': unittest.main()
