"""Material dependencies travel inside immutable designed-source snapshots."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from coat_sim.core.api import CoatService
from coat_sim.core.input_snapshot import freeze_process, runtime_from_snapshot, canonical_json_bytes
from coat_sim.core.schemas import design_data
from coat_sim.core.simulation_assets import save_simulation_input_asset, read_simulation_asset, export_simulation_asset
from tests.test_input_snapshot import fixture


class MaterialAssetSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service, self.recipe = fixture(self.root)

    def source_with_material(self):
        from coat_sim.core.materials import validate_material_profile
        path = self.root / 'parts/source.csrc'
        data = json.loads(path.read_text())
        data['material'] = validate_material_profile({'schema_version': 1,
            'id': 'custom-zns', 'name': 'Custom ZnS',
            'description': 'User material; no validated angular distribution', 'notes': 'Batch A'})
        self.service.save('source', path, data, overwrite=True)
        return path, self.service.load('source', path).data

    def test_source_public_contract_and_old_file_compatibility(self):
        old = self.service.load('source', self.root / 'parts/source.csrc').data
        self.assertNotIn('material', old)
        self.assertEqual(design_data('source', old), old)
        _, data = self.source_with_material()
        self.assertEqual(design_data('source', data)['material'], data['material'])

    def test_library_and_source_changes_do_not_mutate_queued_snapshot(self):
        path, data = self.source_with_material()
        snapshot = freeze_process(self.service, self.recipe, self.root)
        before = canonical_json_bytes(snapshot)
        data['material']['name'] = 'Later library revision'
        self.service.save('source', path, data, overwrite=True)
        runtime = runtime_from_snapshot(snapshot)
        self.assertEqual(runtime['machine']['sources'][0]['profile']['material']['name'], 'Custom ZnS')
        runtime['machine']['sources'][0]['profile']['material']['name'] = 'Mutated display copy'
        self.assertEqual(canonical_json_bytes(snapshot), before)

    def test_source_only_and_process_bundle_export_carry_material_without_library(self):
        _, source = self.source_with_material()
        snapshot = freeze_process(self.service, self.recipe, self.root)
        saved = save_simulation_input_asset(self.root, name='material-source-test', settings={},
            input_snapshot=snapshot, confirmed=True)
        content = read_simulation_asset(saved['asset_path'], detail='full')
        row = next(row for row in content['designs'] if row['kind'] == 'source')
        for label, members in [('leaf', [row['path']]), ('whole', None)]:
            destination = self.root / label
            exported = export_simulation_asset(saved['asset_path'], destination, members=members)
            source_path = next(Path(item['path']) for item in exported['designs'] if item['kind'] == 'source')
            imported = CoatService(destination).load('source', source_path).data
            self.assertEqual(imported['material'], source['material'])
            self.assertFalse((destination / 'data/materials').exists())
        self.assertTrue(all(row['kind'] != 'material' for row in snapshot['files']))

    def test_material_mutation_breaks_source_hash(self):
        self.source_with_material()
        snapshot = freeze_process(self.service, self.recipe, self.root)
        source = next(row for row in snapshot['files'] if row['kind'] == 'source')
        source['data']['material']['name'] = 'tampered'
        with self.assertRaisesRegex(ValueError, '校验和'):
            runtime_from_snapshot(snapshot)
