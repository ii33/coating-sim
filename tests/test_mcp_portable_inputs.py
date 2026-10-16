"""MCP process inputs use one typed manifest, not copied user design files."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tests.test_process_optimization_modes import fixture, options
from coat_sim.core.schemas import SimulationSettings, MaskOptimizationOptions

class MCPPortableInputTests(unittest.TestCase):
    def test_embedded_process_manifest_and_pending_optimization(self):
        from coat_sim.mcp import server
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); service,path=fixture(root,mask_count=0,source_count=1)
            with patch.object(server,'service',service):
                frozen,manifest=server._freeze_calculation_inputs('process',{'process_path':str(path),'settings':{'grid_size':21,'time_steps':4},'backend':'CPU'})
                folder=Path(manifest['manifest_file']).parent
                self.assertEqual([p.name for p in folder.iterdir()],['manifest.json'])
                path.write_text(path.read_text().replace('Test','Edited later'))
                self.assertEqual(service.load('process',frozen['process_path']).data['name'],'Test')
                read=json.loads(Path(manifest['manifest_file']).read_text())
                self.assertEqual(service.load('process',read).data['name'],'Test')
                result=service.optimize_masks({'grid_size':21,'time_steps':4,'carrier_rpm':0},frozen['process_path'],options=options())
                self.assertTrue(result['artifacts']['pending_save'])
                self.assertEqual(len(result['pending_masks']),1)
                # Include current typed design documents as well as the old
                # JSON-shaped filenames this isolation contract originally
                # excluded: ``machine*.*`` / ``substrate*.*`` each contain
                # the previous ``*json`` match set.
                self.assertFalse(list((root/'data/tmp').rglob('machine*.*')))
                self.assertFalse(list((root/'data/tmp').rglob('substrate*.*')))
                self.assertEqual(result['process']['name'],'Test')
