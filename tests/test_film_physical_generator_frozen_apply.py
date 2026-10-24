"""Synthesis application owns a task's frozen material closure through save/undo."""
from copy import deepcopy
from typing import Any, cast

import pytest

from coat_sim.core.film_design import validate_design
from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.film_design_session import FilmDesignEditSession
from coat_sim.core.film_process_import import _sha256
from coat_sim.core.film_physical_generator import freeze_synthesis_editable_snapshot
from coat_sim.core.project_film_service import ProjectOpticalSnapshot
from tests.test_project_core import material
from tests.test_project_film_default_draft import frozen_film_project, workspace_for


def synthesis_candidate(frozen):
    snapshot = deepcopy(frozen)
    snapshot['design']['front_layers'] = []
    snapshot['materials'].append(material('new-synthesis-film', 2.4))
    snapshot['sha256'] = _sha256(snapshot)
    candidate = deepcopy(snapshot['design'])
    candidate['front_layers'] = [{'id': 'new-layer', 'material_id': 'new-synthesis-film',
        'optical_set_id': 'set', 'thickness': {'value': 75., 'unit': 'nm'}, 'note': ''}]
    return validate_design(candidate), snapshot


def test_synthesis_apply_project_undo_restores_snapshot_and_later_workflow(tmp_path):
    store, frozen = frozen_film_project(tmp_path / 'project')
    workspace = workspace_for([store], store)
    assert workspace.initialize_frozen_draft(frozen)
    editor = workspace.editor
    before = editor.snapshot()
    provider = workspace.service.materials
    candidate, snapshot = synthesis_candidate(frozen)
    assert editor.apply_optimized_design(candidate, before.design_revision,
                                         frozen_material_snapshot=snapshot)
    assert workspace.service.materials.evaluate('new-synthesis-film', 'set', 550.) == 2.4+0j
    snapshot['materials'][1]['optical_sets'][0]['n'][0] = 99.
    assert workspace.service.materials.evaluate('new-synthesis-film', 'set', 400.) == 2.4+0j
    assert editor.set_workflow_configuration('simulation', {'samples': 19})
    revision = editor.snapshot().design_revision
    assert editor.undo_optimized_design(revision)
    restored = editor.snapshot()
    assert restored.design['front_layers'] == before.design['front_layers']
    assert editor.workflow_configuration('simulation') == {'samples': 19}
    assert workspace.service.materials.get('film') == provider.get('film')
    with pytest.raises(ValueError):
        workspace.service.materials.get('new-synthesis-film')


def test_synthesis_project_first_save_includes_new_material(tmp_path):
    store, frozen = frozen_film_project(tmp_path / 'project')
    workspace = workspace_for([store], store)
    assert workspace.initialize_frozen_draft(frozen)
    editor = workspace.editor
    candidate, snapshot = synthesis_candidate(frozen)
    assert editor.apply_optimized_design(candidate, editor.snapshot().design_revision,
                                         frozen_material_snapshot=snapshot)
    assert editor.save_current(), editor.snapshot().error
    film_id = next(row['id'] for row in store.manifest['documents'] if row['kind'] == 'film')
    graph = store.resolve_document(film_id)
    captured = {row['id']: row for row in graph['materials']}
    assert captured['new-synthesis-film']['optical_sets'][0]['n'] == [2.4, 2.4]
    assert captured['film']['optical_sets'][0]['n'] == [2., 2.]
    assert editor._frozen_draft_materials is None
    assert workspace.service.materials.evaluate('new-synthesis-film', 'set', 550.) == 2.4+0j


def test_standalone_synthesis_apply_and_undo_detach_task_provider(tmp_path):
    _store, frozen = frozen_film_project(tmp_path / 'project')
    service = FilmDesignService(tmp_path / 'standalone', restrict_to_root=False)
    cast(Any, service).materials = ProjectOpticalSnapshot(frozen['materials'])
    editor = FilmDesignEditSession(service)
    provider = service.materials
    before = editor.snapshot()
    candidate, snapshot = synthesis_candidate(frozen)
    assert editor.apply_optimized_design(candidate, before.design_revision,
                                         frozen_material_snapshot=snapshot)
    assert service.materials is not provider
    assert service.materials.evaluate('new-synthesis-film', 'set', 550.) == 2.4+0j
    assert editor.undo_optimized_design(editor.snapshot().design_revision), editor.snapshot().error
    assert service.materials is provider
    assert editor.snapshot().design == before.design


def test_missing_task_material_rejects_without_replacement(tmp_path):
    store, frozen = frozen_film_project(tmp_path / 'project')
    workspace = workspace_for([store], store)
    assert workspace.initialize_frozen_draft(frozen)
    editor = workspace.editor
    before = editor.snapshot()
    candidate, snapshot = synthesis_candidate(frozen)
    snapshot['materials'].pop()
    snapshot['sha256'] = _sha256(snapshot)
    assert not editor.apply_optimized_design(candidate, before.design_revision,
                                             frozen_material_snapshot=snapshot)
    assert editor.snapshot().design == before.design


def test_standalone_undo_retains_later_workflow_material_reference(tmp_path):
    _store, frozen = frozen_film_project(tmp_path / 'project')
    service = FilmDesignService(tmp_path / 'standalone', restrict_to_root=False)
    cast(Any, service).materials = ProjectOpticalSnapshot(frozen['materials'])
    editor = FilmDesignEditSession(service)
    before = editor.snapshot()
    candidate, snapshot = synthesis_candidate(frozen)
    assert editor.apply_optimized_design(candidate, before.design_revision,
                                         frozen_material_snapshot=snapshot)
    configuration = {'version': 2, 'structure_materials': [
        {'material_id': 'new-synthesis-film', 'optical_set_id': 'set'}]}
    assert editor.set_workflow_configuration('optimization', configuration)
    assert editor.undo_optimized_design(editor.snapshot().design_revision), editor.snapshot().error
    assert editor.snapshot().design['front_layers'] == before.design['front_layers']
    assert editor.workflow_configuration('optimization') == configuration
    assert service.materials.evaluate('new-synthesis-film', 'set', 550.) == 2.4+0j


def test_standalone_same_id_frozen_values_refresh_derived_index_and_undo(tmp_path):
    _store, frozen = frozen_film_project(tmp_path / 'project')
    service = FilmDesignService(tmp_path / 'standalone', restrict_to_root=False)
    cast(Any, service).materials = ProjectOpticalSnapshot(frozen['materials'])
    editor = FilmDesignEditSession(service)
    editor._replace_design(deepcopy(frozen['design']), path=None, message='', dirty=True)
    before = editor.snapshot()
    old_qw = float(before.derived['front']['layer']['qwText'])
    task = deepcopy(frozen)
    task['materials'][0]['optical_sets'][0]['n'] = [3., 3.]
    task['sha256'] = _sha256(task)
    assert editor.apply_optimized_design(deepcopy(task['design']), before.design_revision,
                                         frozen_material_snapshot=task)
    assert float(editor.snapshot().derived['front']['layer']['qwText']) == pytest.approx(old_qw * 1.5)
    assert editor.undo_optimized_design(editor.snapshot().design_revision), editor.snapshot().error
    assert float(editor.snapshot().derived['front']['layer']['qwText']) == pytest.approx(old_qw)


def test_submission_editable_closure_keeps_inactive_unknown_k_without_evaluation(tmp_path):
    _store, frozen = frozen_film_project(tmp_path / 'project')
    inactive = material('inactive-exit', 1.6)
    inactive['optical_sets'][0]['k'] = None
    full_glass = deepcopy(frozen['materials'][0])
    alternate = deepcopy(full_glass['optical_sets'][0])
    alternate['id'] = 'unused-dataset'
    full_glass['optical_sets'].append(alternate)
    service = FilmDesignService(tmp_path / 'standalone', restrict_to_root=False)
    cast(Any, service).materials = ProjectOpticalSnapshot([full_glass, inactive])
    source = deepcopy(frozen['design'])
    source['exit_medium'] = {'kind': 'material', 'material_id': 'inactive-exit', 'optical_set_id': 'set'}
    active = deepcopy(frozen)
    active['design']['front_layers'] = []
    active['design']['exit_medium'] = deepcopy(source['exit_medium'])
    active['materials'][0]['design_ready'] = None
    active['sha256'] = _sha256(active)
    result = freeze_synthesis_editable_snapshot(service, source, active)
    rows = {row['id']: row for row in result['materials']}
    assert 'design_ready' not in rows['film']
    assert {row['id'] for row in rows['film']['optical_sets']} == {'set', 'unused-dataset'}
    assert rows['inactive-exit']['optical_sets'][0]['k'] is None
    assert 'inactive-exit' not in {row['id'] for row in active['materials']}
    assert result['design'] == active['design']
    assert result['sha256'] != active['sha256']


def test_submission_editable_capture_rejects_changed_active_source(tmp_path):
    _store, frozen = frozen_film_project(tmp_path / 'project')
    service = FilmDesignService(tmp_path / 'standalone', restrict_to_root=False)
    cast(Any, service).materials = ProjectOpticalSnapshot([material('film', 3.)])
    with pytest.raises(ValueError, match='数据已改变'):
        freeze_synthesis_editable_snapshot(service, frozen['design'], frozen)
