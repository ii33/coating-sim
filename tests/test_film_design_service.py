import csv
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from coat_sim.core.film_design_service import (FilmDesignService, simulation_settings, simulate_frozen,
    standard_air_to_vacuum_nm, vacuum_to_standard_air_nm)
from coat_sim.core.material_business import MaterialLibrary
from coat_sim.core.materials import default_material_profile


class FilmServiceTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.root=Path(self.folder.name)
        library=MaterialLibrary(self.root)
        def make(identifier,category,sets):
            record=default_material_profile('Synthetic test '+identifier,id=identifier)
            record.update(category=category,optical_constants=sets)
            library.write(record)
        def table(identifier,n,k):
            return dict(id=identifier,name=identifier,kind='tabulated',notes='Synthetic test fixture, not production data',
                        source='Unit test analytical oracle',wavelength_unit='nm',wavelength_min=400.,wavelength_max=700.,
                        wavelengths=[400.,700.],n=[n,n],k=None if k is None else [k,k])
        make('fixture_glass','substrate',[table('glass',1.5,0)])
        make('fixture_film','film',[table('low',1.45,0),table('high',2.1,0),table('absorbing',2.1,.08)])
        self.service=FilmDesignService(self.root)
        self.design=self.service.new_design()
        self.design['substrate'].update(material_id='fixture_glass',optical_set_id='glass')
        self.settings=dict(wavelength_min_nm=500.,wavelength_max_nm=600.,samples=11)

    def tearDown(self):
        self.folder.cleanup()

    def test_no_film_and_plate(self):
        result=self.service.simulate(self.design,self.settings)
        np.testing.assert_allclose(result['R'],.04,atol=2e-14)
        self.design['substrate'].update(mode='incoherent',sides='double')
        result=self.service.simulate(self.design,self.settings)
        np.testing.assert_allclose(result['R'],1/13,atol=2e-14)
        np.testing.assert_allclose(np.array(result['R'])+result['T']+np.array(result['A']),1,atol=2e-14)

    def test_set_switch_source_and_provenance(self):
        layer=dict(id='testlayer',material_id='fixture_film',optical_set_id='low',thickness={'value':100.,'unit':'nm'},note='test')
        self.design['front_layers']=[layer]
        low=self.service.simulate(self.design,self.settings)
        layer['optical_set_id']='high'
        high=self.service.simulate(self.design,self.settings)
        self.assertGreater(max(abs(a-b) for a,b in zip(low['R'],high['R'])),.01)
        weighted=self.service.simulate(self.design,{**self.settings,'light_source':{'kind':'blackbody','temperature_k':3000}})
        np.testing.assert_allclose(weighted['R'],high['R'])
        self.assertNotEqual(weighted['reflected_spectrum'],high['reflected_spectrum'])
        self.assertTrue(all(len(row['sha256'])==64 for row in high['provenance']))
        json.dumps(high,allow_nan=False)

    def test_chunked_qw_reference_queries_do_not_inflate_spectral_provenance(self):
        self.design['front_layers'] = [dict(
            id='qw-reference-layer', material_id='fixture_film', optical_set_id='low',
            thickness={'value': 1.0, 'unit': 'qw'}, note='synthetic QW provenance fixture')]
        result = self.service.simulate(
            self.design, {'wavelength_min_nm': 400.0, 'wavelength_max_nm': 700.0,
                          'samples': 10003})
        film = next(row for row in result['provenance']
                    if row['material_id'] == 'fixture_film')
        evaluation = film['evaluation']
        self.assertEqual(len(evaluation['thickness_reference_queries']), 1)
        self.assertEqual(evaluation['n']['interpolated_query_count'], 10001)
        self.assertEqual(evaluation['k']['source_evaluated_query_count'], 10003)
        self.assertEqual(evaluation['k']['interpolated_query_count'], 10001)

    def test_persistence_and_csv(self):
        path=self.root/'data'/'film_design'/'roundtrip.cfilm'
        self.service.save(path,self.design)
        self.assertEqual(self.service.load(path),self.service.validate(self.design))
        with self.assertRaises(ValueError): self.service.save(path,self.design)
        original = path.read_bytes()
        with self.assertRaises(ValueError): self.service.save(path,self.design,overwrite='false')
        self.assertEqual(path.read_bytes(),original)
        changed = {**self.design,'name':'Explicitly replaced design'}
        self.service.save(path,changed,overwrite=True)
        self.assertEqual(self.service.load(path)['name'],changed['name'])
        result=self.service.simulate(self.design,self.settings)
        csv=self.root/'data'/'results'/'spectrum.csv'
        self.service.export_csv(csv,result)
        self.assertEqual(len(csv.read_text(encoding='utf-8-sig').splitlines()),12)
        with self.assertRaises(ValueError): self.service.export_csv(csv,result)
        original = csv.read_bytes()
        with self.assertRaises(ValueError): self.service.export_csv(csv,result,overwrite='false')
        self.assertEqual(csv.read_bytes(),original)
        with self.assertRaises(Exception): self.service.save(self.root.parent/'outside.json',self.design)

    def test_unknown_k_and_domain(self):
        self.design['substrate'].update(material_id='film_substrate_schott_nbk7',optical_set_id='schott_nbk7_sellmeier')
        enabled=self.service.simulate(self.design,self.settings)
        self.design['assume_lossless']=True
        np.testing.assert_array_equal(self.service.simulate(self.design,self.settings)['R'],enabled['R'])
        self.design['use_k']=False
        result=self.service.simulate(self.design,self.settings)
        np.testing.assert_array_equal(result['R'],enabled['R'])
        self.assertFalse(any('使用用户显式无损假设' in w for w in result['warnings']))
        self.assertIsNone(result['provenance'][0]['optical_set'].get('k'))
        self.assertFalse(result['provenance'][0]['evaluation']['k']['known'])
        with self.assertRaisesRegex(ValueError,'有效波段'):
            self.service.simulate(self.design,dict(wavelength_min_nm=300.,wavelength_max_nm=600.,samples=3,extrapolation_policy='error'))

    def test_endpoint_evaluation_report_and_csv_preserve_per_set_provenance(self):
        result=self.service.simulate(self.design,dict(wavelength_min_nm=300.,wavelength_max_nm=600.,samples=4))
        material=next(item for item in result['provenance'] if item['material_id']=='fixture_glass')
        evaluation=material['evaluation']
        n=evaluation['n']; k=evaluation['k']
        self.assertEqual(evaluation['extrapolation_policy'],'endpoint_hold')
        self.assertEqual(n['method'],'piecewise_linear')
        self.assertEqual(n['source_range_nm'],[400.,700.])
        self.assertEqual(n['effective_input_range_nm'],[400.,600.])
        self.assertEqual(n['interpolation_method'],'linear')
        self.assertEqual(n['extrapolation_method'],'endpoint_clamp')
        self.assertEqual(n['extrapolated_ranges_nm'],[[300.,400.]])
        self.assertTrue(k['known'])
        self.assertEqual(k['effective_input_range_nm'],[400.,600.])
        self.assertEqual(k['extrapolated_ranges_nm'],[])
        self.assertEqual(k['default_zero_query_count'],1)
        self.assertTrue(any('300–400 nm' in warning for warning in material['warnings']))
        self.assertTrue(any('300–400 nm' in warning for warning in result['warnings']))

        csv_path=self.root/'data'/'results'/'endpoint-report.csv'
        self.service.export_csv(csv_path,result)
        with csv_path.open(encoding='utf-8-sig',newline='') as stream:
            rows=list(csv.DictReader(stream))
        report=json.loads(rows[0]['evaluation_report_json'])
        exported=next(item for item in report['materials'] if item['material_id']=='fixture_glass')
        self.assertEqual(exported['source'],'Unit test analytical oracle')
        self.assertEqual(exported['evaluation']['n']['source_range_nm'],[400.,700.])
        self.assertEqual(exported['evaluation']['n']['effective_input_range_nm'],[400.,600.])
        self.assertTrue(exported['warnings'])
        self.assertTrue(json.loads(rows[0]['warnings_json']))
        self.assertEqual(rows[1]['evaluation_report_json'],'')

    def test_usage_and_material_reference(self):
        before={r['id'] for r in self.service.material_options('film')}
        self.assertNotIn('fixture_glass',before)
        self.service.materials.set_usage('fixture_glass','film',True)
        self.assertIn('fixture_glass',{r['id'] for r in self.service.material_options('film')})
        self.assertIn('fixture_glass',{r['id'] for r in self.service.material_options('substrate')})
        self.assertAlmostEqual(self.service.materials.evaluate('film_substrate_schott_nbk7','schott_nbk7_sellmeier',587.6,True).real,1.5167984,places=5)

    def test_formula_n_and_independent_k_have_separate_domains_and_csv_sources(self):
        library=MaterialLibrary(self.root)
        record=library.read('fixture_film')['record']
        record['optical_constants'].append({
            'id':'independent_grid','name':'Independent n/k fixture','kind':'rii_formula',
            'formula_number':5,'coefficients':[1.7,.001,-2],
            'wavelength_unit':'nm','wavelength_min':400.,'wavelength_max':700.,
            'source':'n formula fixture','notes':'Synthetic regression only',
            'k_data':{'wavelength_unit':'um','wavelength_min':.5,'wavelength_max':.6,
                      'wavelengths':[.5,.55,.6],'k':[.01,.02,.03],
                      'source':'independent k fixture','notes':'Separate k experiment'},
        })
        library.write(record,update=True)
        self.design['front_layers']=[{'id':'separate_grid_layer','material_id':'fixture_film',
            'optical_set_id':'independent_grid','thickness':{'value':100.,'unit':'nm'}}]
        result=self.service.simulate(self.design,{'wavelength_min_nm':350.,'wavelength_max_nm':750.,'samples':9})
        item=next(row for row in result['provenance'] if row['material_id']=='fixture_film')
        self.assertEqual(item['evaluation']['n']['source_range_nm'],[400.,700.])
        self.assertEqual(item['evaluation']['k']['source_range_nm'],[500.,600.])
        self.assertEqual(item['evaluation']['n']['extrapolated_ranges_nm'],[[350.,400.],[700.,750.]])
        self.assertEqual(item['evaluation']['k']['extrapolated_ranges_nm'],[])
        self.assertEqual(item['evaluation']['k']['default_zero_query_count'],6)
        self.assertEqual(item['evaluation']['k']['source'],'independent k fixture')
        self.assertEqual(item['optical_set']['k_data']['wavelengths'],[.5,.55,.6])
        path=self.root/'data'/'results'/'independent-grid.csv'
        self.service.export_csv(path,result)
        with path.open(encoding='utf-8-sig',newline='') as stream:
            report=json.loads(next(csv.DictReader(stream))['evaluation_report_json'])
        exported=next(row for row in report['materials'] if row['material_id']=='fixture_film')
        self.assertEqual(exported['evaluation']['k']['notes'],'Separate k experiment')
        self.assertEqual(simulation_settings({'extrapolation_policy':'endpoint'})['extrapolation_policy'],'endpoint_hold')

    def test_settings_negative(self):
        for settings in ({'samples':True},{'samples':1},{'angle_deg':90},{'wavelength_min_nm':float('nan')},
                         {'light_source':{'kind':'tabulated','wavelength_nm':[400,700],'intensity':[0,0]}}):
            with self.assertRaises(ValueError): simulation_settings(settings)

    def test_snapshot_and_first_surface(self):
        self.design['front_layers']=[dict(id='absorbing-layer',material_id='fixture_film',optical_set_id='absorbing',
            thickness={'value':100.,'unit':'nm'},note='synthetic fixture')]
        self.design['substrate'].update(mode='incoherent',sides='double')
        front=self.service.simulate(self.design,self.settings)
        self.design['first_surface']='back'
        back=self.service.simulate(self.design,self.settings)
        self.assertEqual(back['settings']['first_surface'],'back')
        self.assertGreater(max(abs(a-b) for a,b in zip(front['R'],back['R'])),1e-3)
        payload=self.service.freeze_design(self.design,self.settings)
        library=MaterialLibrary(self.root)
        record=library.read('fixture_film')['record']
        record['optical_constants'][2]['n']=[1.1,1.1]
        library.write(record,update=True)
        fixed=simulate_frozen(self.root,payload)
        np.testing.assert_allclose(fixed['R'],back['R'])
        payload['design']['name']='changed after freeze'
        with self.assertRaisesRegex(ValueError,'摘要'): simulate_frozen(self.root,payload)


    def test_cie_standard_air_source_is_postprocessing_only(self):
        catalog_path = (Path(__file__).resolve().parents[1] / "data" / "film_design" /
                        "cie-illuminants-20261006" / "data" / "cie_illuminants.json")
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        dataset = catalog["datasets"]["D65"]
        source = {
            "kind": "tabulated",
            "wavelength_nm": [row[0] for row in dataset["samples"]],
            "intensity": [row[1] for row in dataset["samples"]],
            "source": dataset["citation"],
            "wavelength_basis": dataset["wavelengthBasis"],
            "dataset_id": dataset["id"],
            "snapshot_date": catalog["snapshotDate"],
            "data_license": dataset["license"]["spdx"],
            "source_page": dataset["sourcePage"],
            "doi": dataset["doi"],
            "catalog_version": str(catalog["schemaVersion"]),
            "source_warnings": dataset.get("warnings", []),
        }
        vacuum = standard_air_to_vacuum_nm(np.array([300.0, 550.0, 830.0]))
        self.assertTrue(np.all(vacuum > np.array([300.0, 550.0, 830.0])))
        np.testing.assert_allclose(vacuum_to_standard_air_nm(vacuum), [300.0, 550.0, 830.0], atol=1e-10)
        flat = self.service.simulate(self.design, self.settings)
        weighted = self.service.simulate(
            self.design, {**self.settings, "light_source": source})
        np.testing.assert_array_equal(weighted["R"], flat["R"])
        np.testing.assert_array_equal(weighted["T"], flat["T"])
        np.testing.assert_array_equal(weighted["A"], flat["A"])
        self.assertEqual(weighted["wavelength_basis"], "vacuum")
        self.assertEqual(weighted["source_wavelength_basis"], "standard_air")
        self.assertEqual(weighted["settings"]["light_source"]["dataset_id"], "D65")
        self.assertNotEqual(weighted["source_spectrum"], flat["source_spectrum"])
        self.assertAlmostEqual(sum(weighted["weighted_rta"].values()), 1.0, places=12)

if __name__=='__main__': unittest.main()
