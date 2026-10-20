"""Physical GUI adapters preserve session ownership and real optical data."""
from copy import deepcopy
import unittest

from coat_sim.core.film_design import new_design, validate_design
from coat_sim.core.film_design_session import FilmDesignEditSession
from coat_sim.core.film_physical_generator import (
    MECHANISMS, normalize_physical_form, physical_section_defaults,
)


def material(identifier, n):
    return {'id': identifier, 'name': identifier, 'design_ready': True,
        'optical_sets': [{'id': identifier + '_set', 'name': identifier + ' data',
            'kind': 'tabulated', 'source': 'explicit test data', 'wavelength_unit': 'nm',
            'wavelength_min': 400., 'wavelength_max': 700.,
            'wavelengths': [400., 550., 700.], 'n': [n, n, n], 'k': [0., 0., 0.]}]}


class MemoryService:
    def __init__(self):
        self.materials = object()
        self.options = {'film': [material('H', 2.1), material('L', 1.45), material('C', 1.8)],
                        'substrate': [material('glass', 1.5)], 'medium': []}

    def new_design(self):
        return new_design()

    def material_options_many(self, _purposes):
        return deepcopy(self.options)

    def validate(self, design):
        return validate_design(design)

    def list_saved_designs(self):
        return []

    def list_expression_templates(self):
        return []


def structure(mechanism='multistack'):
    kind = next(row['section_type'] for row in MECHANISMS if row['id'] == mechanism)
    section = physical_section_defaults(kind, 550.)
    if kind == 'bragg':
        section['pairs'] = '2'
    return {'mechanism': mechanism, 'name': 'physical form',
        'bindings': [{'role': role, 'material_id': role, 'optical_set_id': role + '_set'}
                     for role in ('H', 'L', 'C')], 'sections': [section]}


class PhysicalGeneratorSessionTests(unittest.TestCase):
    def setUp(self):
        self.service = MemoryService()
        self.session = FilmDesignEditSession(self.service)
        self.session.set_substrate_value('material_id', 'glass')
        self.session.set_design_value('use_k', True)
        self.session.set_design_value('assume_lossless', False)
        self.session.set_medium_value('incident_medium.kind', 'constant')

    def test_metadata_only_preview_and_atomic_undo(self):
        before = self.session.snapshot()
        preview = self.session.preview_physical_structure(structure())
        self.assertNotIn('candidate_design', preview)
        self.assertNotIn('_physical_material_snapshot', preview)
        self.assertEqual(preview['layer_count'], 4)
        self.assertEqual(preview['layers_substrate_order'][0]['role'], 'L')
        self.assertEqual(self.session.snapshot().design, before.design)
        self.assertTrue(self.session.apply_pending_generated_design(before.design_revision))
        applied = self.session.snapshot()
        self.assertEqual(len(applied.design['front_layers']), 4)
        self.assertEqual(applied.design['reference_wavelength_nm'], before.design['reference_wavelength_nm'])
        self.assertEqual(applied.design['substrate'], before.design['substrate'])
        self.assertTrue(self.session.undo_generated_design(applied.design_revision))
        self.assertEqual(self.session.snapshot().design, before.design)
        self.assertFalse(self.session.undo_generated_design(self.session.snapshot().design_revision))

    def test_all_mechanisms_keep_editable_parameters(self):
        for mechanism in [row['id'] for row in MECHANISMS]:
            with self.subTest(mechanism=mechanism):
                data = structure(mechanism)
                if mechanism == 'coupled_cavity':
                    section = data['sections'][0]
                    section['substrate_mirror']['pairs'] = '2'
                    section['incident_mirror']['pairs'] = '3'
                    section['cavities'].append({'type':'layer', 'role':'C',
                        'wavelength_nm':'555', 'thickness_nm':'110', 'optical_thickness_waves':'.5'})
                    coupling = physical_section_defaults('bragg', 550.)
                    coupling['pairs'] = '1'
                    section['coupling_mirrors'].append(coupling)
                preview = self.session.preview_physical_structure(data)
                self.assertGreater(preview['layer_count'], 0)
                self.assertEqual(preview['parameters'], normalize_physical_form(data))
                self.assertTrue(all(row['physical_thickness_nm'] > 0
                                    for row in preview['layers_substrate_order']))

    def test_same_provider_nk_mutation_rejected_but_labels_allowed(self):
        preview = self.session.preview_physical_structure(structure())
        for row in self.service.options['film']:
            row['name'] += ' renamed'
            row['optical_sets'][0]['notes'] = 'new editorial note'
        self.assertTrue(self.session.apply_pending_generated_design(preview['base_design_revision']))
        fresh = self.session.preview_physical_structure(structure())
        before = self.session.snapshot()
        self.service.options['film'][0]['optical_sets'][0]['n'][1] = 2.3
        self.assertFalse(self.session.apply_pending_generated_design(fresh['base_design_revision']))
        self.assertEqual(self.session.snapshot().design, before.design)

    def test_failed_preview_and_explicit_clear_revoke_old_candidate(self):
        preview = self.session.preview_physical_structure(structure())
        self.session.clear_generated_preview()
        self.assertFalse(self.session.apply_pending_generated_design(preview['base_design_revision']))
        preview = self.session.preview_physical_structure(structure())
        bad = structure()
        bad['sections'][0]['wavelength_nm'] = '900'
        with self.assertRaises(ValueError):
            self.session.preview_physical_structure(bad)
        self.assertFalse(self.session.apply_pending_generated_design(preview['base_design_revision']))

    def test_unknown_k_and_environment_are_explicit_errors(self):
        self.service.options['film'][0]['optical_sets'][0]['k'] = None
        with self.assertRaisesRegex(ValueError, 'unknown k'):
            self.session.preview_physical_structure(structure())
        self.service.options['film'][0]['optical_sets'][0]['k'] = [0.,0.,0.]
        self.session.set_substrate_value('sides', 'double')
        before = self.session.snapshot()
        with self.assertRaisesRegex(ValueError, '单面'):
            self.session.preview_physical_structure(structure())
        self.assertEqual(before.design, self.session.snapshot().design)

    def test_string_numbers_optional_blank_and_invalid_integer(self):
        data = structure('composite')
        data['sections'][0]['thickness_nm'] = ''
        data['sections'][0]['optical_thickness_waves'] = '.25'
        normalized = normalize_physical_form(data)
        self.assertIsNone(normalized['sections'][0]['thickness_nm'])
        self.assertEqual(normalized['sections'][0]['optical_thickness_waves'], .25)
        data = structure()
        data['sections'][0]['pairs'] = '2.2'
        with self.assertRaises(ValueError):
            normalize_physical_form(data)
