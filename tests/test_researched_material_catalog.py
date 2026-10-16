"""Imported evidence is role-bounded, immutable and independent of solver inputs."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from coat_sim.core.materials import (builtin_material_profiles, material_completeness,
    material_emission_model, validate_material_profile, save_material, list_materials)
from coat_sim.core.material_research import research_material_records
from coat_sim.core.source_profiles import validate_source_profile
from coat_sim.core.schemas import design_data
from coat_sim.core.input_snapshot import freeze_process, runtime_from_snapshot, canonical_json_bytes
from coat_sim.core.simulation_assets import save_simulation_input_asset, read_simulation_asset, export_simulation_asset
from coat_sim.core.api import CoatService
from tests.test_input_snapshot import fixture


class ResearchCatalogTests(unittest.TestCase):
    def test_every_catalog_record_is_present_and_preserves_all_evidence(self):
        records = research_material_records()
        builtins = builtin_material_profiles()
        self.assertGreaterEqual(len(records), 17)
        self.assertEqual(len({p['id'] for p in builtins}), len(builtins))
        researched = [p for p in builtins if 'research_data' in p]
        non_research = [p for p in builtins if 'research_data' not in p]
        by_research_id = {p['research_data']['id']: p for p in researched}
        self.assertEqual(len(by_research_id), len(researched))
        self.assertEqual({r['id'] for r in records}, set(by_research_id))
        self.assertEqual({r['id']: r for r in records},
                         {identity: p['research_data'] for identity, p in by_research_id.items()})
        # Air is a documented optical-medium placeholder added with the Film Quick workflow
        # (docs/film-design-quick-migration.md; commit 129de86), not a research-catalog row.
        self.assertEqual(len(non_research), 1)
        air = non_research[0]
        self.assertEqual(air['id'], 'builtin_ideal_air')
        self.assertEqual(air['roles'], ['medium'])
        self.assertTrue(air['optical_only'])
        self.assertIsNone(air['resistance_model'])
        self.assertIsNone(air['electron_beam_model'])
        self.assertFalse(material_completeness(air)['research_available'])

        for row in builtins:
            parsed = validate_material_profile(row)
            self.assertEqual(parsed, row)

        zns = by_research_id['ZnS_film']
        self.assertEqual(zns['reference_data']['material_id'], 'ZnS')
        for row in researched:
            info = material_completeness(row)
            self.assertEqual(info['property_coverage']['total'], len(row['research_data']['properties']))
            self.assertFalse(info['thermodynamic_rate_available'])
            self.assertTrue(info['research_available'])
            if row['id'] != 'builtin_zns_literature_20261004':
                self.assertEqual(info['reference_models'], [])
                self.assertEqual(row['resistance_model']['defaults']['source_exponent'], 1)

    def test_research_cannot_be_tampered_or_reclassified_on_copy(self):
        original = next(row for row in builtin_material_profiles()
                        if row.get('research_data', {}).get('id') == 'Ta2O5_film')
        copy = deepcopy(original)
        copy.update(id='user_copy', name='My custom label')
        self.assertEqual(validate_material_profile(copy)['research_data'], original['research_data'])
        copy['research_data']['description'] += ' altered'
        with self.assertRaisesRegex(ValueError, '快照'):
            validate_material_profile(copy)
        copy = deepcopy(original); copy['roles'] = ['film'] if original['roles'] != ['film'] else ['boat']
        with self.assertRaisesRegex(ValueError, '用途'):
            validate_material_profile(copy)
        unchanged = next(row for row in builtin_material_profiles() if row['id'] == original['id'])
        self.assertEqual(unchanged, original)

    def test_source_role_guard_applies_to_custom_and_uniform_sources(self):
        for material in builtin_material_profiles():
            profile = {'kind':'point', 'material':material}
            if 'film' in material['roles']:
                self.assertEqual(validate_source_profile(profile)['material'], material)
                self.assertEqual(design_data('source', validate_source_profile(profile))['material'], material)
            else:
                self.assertFalse(material_completeness(material)['source_bindable'])
                with self.assertRaisesRegex(ValueError, '用途'):
                    validate_source_profile(profile)
                with self.assertRaisesRegex(ValueError, '蒸发'):
                    material_emission_model(material, 'resistance_boat')

    def test_copy_save_roundtrip_and_source_asset_queue_preserve_research(self):
        material = next(row for row in builtin_material_profiles()[1:] if 'film' in row['roles'])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material.update(id='custom_researched', name='Custom batch')
            save_material(root/'library', material)
            self.assertEqual(list_materials(root/'library')[0], material)
            service, recipe = fixture(root, mask_count=0)
            source_path = root/'parts/source.csrc'
            source = json.loads(source_path.read_text()); source['material'] = material
            service.save('source',source_path,source,overwrite=True)
            snap = freeze_process(service, recipe, root)
            before = canonical_json_bytes(snap)
            source_path.unlink()
            profile = runtime_from_snapshot(snap)['machine']['sources'][0]['profile']
            self.assertEqual(profile['material'],material)
            profile['material']['name'] = 'mutated copy'
            self.assertEqual(canonical_json_bytes(snap),before)
            asset = save_simulation_input_asset(root,name='Research evidence',settings={},input_snapshot=snap,confirmed=True)['asset_path']
            record = next(row for row in read_simulation_asset(asset,detail='full')['designs'] if row['kind']=='source')
            exported = export_simulation_asset(asset,root/'exported',members=[record['path']])
            path = next(row['path'] for row in exported['designs'] if row['kind']=='source')
            self.assertEqual(CoatService(root).load('source',path).data['material'],material)
