"""Two physical film materials suffice; use IDs do not imply distinct materials."""
from copy import deepcopy
from typing import Any, cast

import pytest
from PySide6.QtCore import QObject, QSettings, Qt
from PySide6.QtTest import QTest

from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.film_physical_generator import (
    physical_material_use_label, physical_required_material_uses, physical_section_defaults,
    physical_section_fields,
)
from coat_sim.core.materials import default_material_profile
from coat_sim.core.project_film_service import ProjectOpticalSnapshot
from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from tests.film_structure_fixtures import fixture as optical_fixture
from tests.test_film_optimization_needle import _optical
from tests.test_film_synthesis_gui_review import _Scene, _variant, _wait

pytestmark = [pytest.mark.qt, pytest.mark.contract]


def test_material_use_metadata_has_units_and_two_material_cavity_default():
    cavity = physical_section_defaults('coupled_cavity', 550.)
    assert cavity['cavities'][0]['role'] == 'H'
    assert set(physical_required_material_uses([cavity])) == {'H', 'L'}
    assert '高折射率膜料' in physical_material_use_label('H')
    assert '低折射率膜料' in physical_material_use_label('L')
    fields = physical_section_fields('layer')
    thickness = next(row for row in fields if row['name'] == 'optical_thickness_waves')
    assert '无量纲' in thickness['label'] and '0.25' in thickness['help']
    assert all('角色' not in row['label'] for row in fields)
    cavity['cavities'][0]['role'] = 'C'
    assert set(physical_required_material_uses([cavity])) == {'H', 'L', 'C'}


@pytest.fixture
def two_film_scene(tmp_path):
    _service, library, _design = optical_fixture(tmp_path)
    high = default_material_profile('Synthetic high film', id='usability-high')
    high.update(category='film', optical_constants=[_optical('high-set', 2.1), _optical('high-alt-set', 1.9)])
    library.write(high)
    service = FilmDesignService(tmp_path)
    # A real project snapshot limits the picker to exactly two film materials;
    # unrelated built-in research catalog entries are not part of this fixture.
    records = [{key: value for key, value in service.materials.get(identifier).items()
                if key in {'id', 'name', 'notes', 'optical_sets', 'design_ready'}}
               for identifier in ('acceptance-film', 'usability-high', 'acceptance-glass')]
    cast(Any, service).materials = ProjectOpticalSnapshot(records, usage={
        'acceptance-film': {'film'}, 'usability-high': {'film'}, 'acceptance-glass': {'substrate'}})
    controller = FilmDesignQuickController(service=service,
        geometry_settings=QSettings(str(tmp_path / 'two-material.ini'), QSettings.Format.IniFormat))
    assert controller.initializationReady
    assert controller.setSubstrateValue('material_id', 'acceptance-glass')
    assert controller.setSubstrateValue('optical_set_id', 'glass-set')
    assert controller.setSubstrateValue('sides', 'single')
    assert {row['id'] for row in service.material_options('film')} == {'acceptance-film', 'usability-high'}
    scene = _Scene(controller, 'FilmDesignQuick.qml')
    scene.click(scene.item('filmDesignSidebarGenerator'))
    scene.click(scene.item('filmDesignGeneratorMode'))
    QTest.keyClick(scene.window, Qt.Key.Key_End)
    QTest.keyClick(scene.window, Qt.Key.Key_Return)
    form = scene.item('filmPhysicalGeneratorForm')
    _wait(lambda: form.isVisible())
    try:
        yield controller, scene, form
    finally:
        controller.markSourceClosed()
        scene.close()
        controller.deleteLater()


def test_real_two_material_preview_apply_confirmation_and_undo(two_film_scene):
    controller, scene, form = two_film_scene
    assert {row['role'] for row in _variant(form.property('bindings'))} == {'H', 'L'}
    assert scene.item('filmPhysicalMaterialHelp').property('text').find('两种膜料') >= 0
    scene.evaluate(form, "setBinding('H', 'usability-high', 'high-set')")
    scene.evaluate(form, "setBinding('L', 'acceptance-film', 'film-set')")
    # A previously entered, now-unused purpose must not become a third required material.
    scene.evaluate(form, "setBinding('C', 'unused-missing-material', 'unused-missing-set')")
    scene.edit(scene.item('filmPhysicalSection_0_pairs'), '2')
    before = controller.session.snapshot()
    scene.click(scene.item('filmPhysicalPreview'))
    preview = _variant(form.property('previewData'))
    assert preview.get('ok'), form.property('errorText')
    assert preview['layer_count'] == 4
    assert {row['role'] for row in preview['parameters']['bindings']} == {'H', 'L'}
    assert {row['material_id'] for row in preview['layers_substrate_order']} == {'acceptance-film', 'usability-high'}
    scene.click(scene.item('filmPhysicalApply'))
    dialog = scene.window.findChild(QObject, 'filmPhysicalApplyConfirmation')
    assert dialog is not None and dialog.property('visible')
    assert controller.session.snapshot().design == before.design
    scene.accept_dialog(dialog)
    _wait(lambda: controller.session.snapshot().design_revision == before.design_revision + 1)
    assert len(controller.currentDesign()['front_layers']) == 4
    undo = scene.item('filmDesignGeneratorUndoButton')
    scene.click(undo)
    _wait(lambda: controller.currentDesign()['front_layers'] == before.design['front_layers'])
    assert controller.currentDesign() == before.design
    assert not controller.canUndoGeneratedDesign


def test_cavity_can_reuse_high_film_under_additional_use_id(two_film_scene):
    controller, scene, form = two_film_scene
    scene.evaluate(form, "setBinding('H', 'usability-high', 'high-set')")
    scene.evaluate(form, "setBinding('L', 'acceptance-film', 'film-set')")
    scene.evaluate(form, "selectMechanism('coupled_cavity')")
    sections = _variant(form.property('sections'))
    assert sections[0]['cavities'][0]['role'] == 'H'
    scene.click(scene.item('filmPhysicalPreview'))
    first = _variant(form.property('previewData'))
    assert first.get('ok'), form.property('errorText')
    assert {row['role'] for row in first['parameters']['bindings']} == {'H', 'L'}
    scene.evaluate(form, "setBinding('C', 'usability-high', 'high-set')")
    section = deepcopy(sections[0])
    section['cavities'][0]['role'] = 'C'
    form.setProperty('sections', [section])
    scene.click(scene.item('filmPhysicalPreview'))
    reused = _variant(form.property('previewData'))
    assert reused.get('ok'), form.property('errorText')
    uses = {row['role']: (row['material_id'], row['optical_set_id'])
            for row in reused['parameters']['bindings']}
    assert uses['C'] == uses['H']
    assert len(set(uses.values())) == 2
    assert reused['layer_count'] == first['layer_count']
    assert [row['physical_thickness_nm'] for row in reused['layers_substrate_order']] == pytest.approx(
        [row['physical_thickness_nm'] for row in first['layers_substrate_order']])
    assert controller.currentDesign()['front_layers'] == []


def test_equal_high_low_material_still_fails_physical_index_order(two_film_scene):
    _controller, scene, form = two_film_scene
    scene.evaluate(form, "setBinding('H', 'usability-high', 'high-set')")
    scene.evaluate(form, "setBinding('L', 'usability-high', 'high-set')")
    scene.click(scene.item('filmPhysicalPreview'))
    assert not form.property('previewToken')
    assert 'nH > nL' in str(form.property('errorText'))


def test_explicit_cavity_use_requires_its_selected_binding(two_film_scene):
    controller, scene, form = two_film_scene
    scene.evaluate(form, "setBinding('H', 'usability-high', 'high-set')")
    scene.evaluate(form, "setBinding('L', 'acceptance-film', 'film-set')")
    scene.evaluate(form, "selectMechanism('coupled_cavity')")
    section = deepcopy(_variant(form.property('sections'))[0])
    section['cavities'][0]['role'] = 'C'
    form.setProperty('sections', [section])
    before = controller.session.snapshot()
    scene.click(scene.item('filmPhysicalPreview'))
    assert not form.property('previewToken')
    assert '独立腔层用料（C）' in str(form.property('errorText'))
    assert controller.session.snapshot().design == before.design


def test_same_material_id_with_different_optical_sets_uses_real_indices(two_film_scene):
    _controller, scene, form = two_film_scene
    scene.evaluate(form, "setBinding('H', 'usability-high', 'high-set')")
    scene.evaluate(form, "setBinding('L', 'usability-high', 'high-alt-set')")
    scene.edit(scene.item('filmPhysicalSection_0_pairs'), '1')
    scene.click(scene.item('filmPhysicalPreview'))
    preview = _variant(form.property('previewData'))
    assert preview.get('ok'), form.property('errorText')
    assert {row['material_id'] for row in preview['layers_substrate_order']} == {'usability-high'}
    assert {row['optical_set_id'] for row in preview['layers_substrate_order']} == {'high-set', 'high-alt-set'}
    assert {row['n_at_wavelength'] for row in preview['layers_substrate_order']} == {2.1, 1.9}
