"""Durable structural search through private Qt windows and real queue jobs."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from coat_sim.core.project_store import ProjectStore
from tests.test_film_optimization_autoapply import app, isolated_case
from tests.test_film_optimization_cancel_spectrum import wait_done
from tests.test_film_synthesis_gui_review import project_gui, _optimizer, _wait

pytestmark = [pytest.mark.qt, pytest.mark.contract]


def _complete(controller, scene):
    scene.click(scene.item('filmOptimizationStartButton'))
    _wait(lambda: bool(controller.queueJobId))
    _wait(lambda: not controller.busy, seconds=45.)
    assert controller._synthesis_payload is not None, controller.error
    assert controller.hasSynthesisSearchProgress, controller.synthesisSearchDisabledReason
    assert controller.synthesisPhase == '本轮完成'
    assert controller.canContinueSynthesisSearch, controller.synthesisSearchDisabledReason
    return deepcopy(controller._synthesis_search_reference), deepcopy(controller._synthesis_search_record)


def test_project_search_save_view_continue_and_undo(app, project_gui):
    harness, _host, _workspace, store, _library, saved = project_gui
    _service, source, controller, scene = _optimizer(harness)
    assert controller.setSynthesisOption('max_evaluations', '160')
    original = source.session.snapshot()
    ref, record = _complete(controller, scene)
    assert controller._candidate_applied, controller.statusText
    assert '减薄前局部结果' in controller.synthesisAuditSummary
    assert '最终返回候选' in controller.synthesisAuditSummary
    assert source.canUndoOptimizedDesign
    assert scene.item('filmSynthesisContinueSearchButton').isEnabled()
    assert scene.item('filmSynthesisSearchPhase').property('text') == '本轮完成'
    assert controller.previewSynthesisCandidate()
    assert controller._synthesis_search_reference == ref
    assert source.undoOptimizedDesign()
    assert source.currentDesign()['front_layers'] == original.design['front_layers']
    assert controller._synthesis_search_reference == ref
    assert source.saveCurrent(), source.session.snapshot().error
    assert controller._synthesis_search_reference == ref
    persisted = ProjectStore.open(store.project_file).resolve_document(saved.document_id)
    design = next(row['data'] for row in persisted['documents'] if row['id'] == saved.document_id)
    assert design['workflow_settings']['optimization']['synthesis']['search_reference'] == ref
    assert controller.canContinueSynthesisSearch, controller.synthesisSearchDisabledReason
    scene.click(scene.item('filmSynthesisContinueSearchButton'))
    _wait(lambda: controller.busy or controller.queueJobId != ref['job_id'])
    _wait(lambda: not controller.busy, seconds=45.)
    assert controller.queueJobId != ref['job_id'], controller.error
    assert controller._synthesis_payload is not None, controller.error
    frozen = controller._synthesis_search_record['request']
    assert frozen.search_checkpoint['sha256'] == record['checkpoint']['sha256']
    assert frozen.seed_designs[0]['front_layers'] == []
    new = controller._synthesis_search_record['checkpoint']
    assert new['tried'] != record['checkpoint']['tried'] or new['cursors'] != record['checkpoint']['cursors']
    assert controller._synthesis_payload['result']['evaluations'] > 0
    assert controller.synthesisPhase == '本轮完成'


REOPEN = r'''
import json,sys
from pathlib import Path
from copy import deepcopy
from PySide6.QtGui import QGuiApplication
from PySide6.QtCore import QSettings
from coat_sim.core import material_business
from coat_sim.core.material_business import MaterialLibrary
from coat_sim.core.project_store import ProjectStore,MANIFEST_FILENAME
from coat_sim.core.project_film_session import ProjectFilmWorkspace
from coat_sim.project_session import ProjectSessionHost
from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from tests.test_film_synthesis_gui_review import _wait,_Scene
app=QGuiApplication([])
data=json.loads(sys.stdin.read())
library=MaterialLibrary(directory=data['library'])
material_business.application_material_library=lambda:library
host=ProjectSessionHost(lambda root:ProjectStore.open(root/MANIFEST_FILENAME))
workspace=ProjectFilmWorkspace.for_project(host,host.open_project(data['root']).binding)
assert workspace.editor.request_load_saved_design(data['document'])
source=FilmDesignQuickController(service=workspace.service,edit_session=workspace.editor,
 project_context=workspace,geometry_settings=QSettings(data['settings'],QSettings.Format.IniFormat))
c=FilmOptimizationQuickController(source)
_wait(lambda:not c._initial_loading)
scene=_Scene(c,'FilmOptimizationWindow.qml')
assert c._synthesis_search_record is None
assert c._synthesis_search_reference==data['reference']
assert c.canContinueSynthesisSearch,c.synthesisSearchDisabledReason
layer=source.currentDesign()['front_layers'][0]
assert source.setLayerValue('front',layer['id'],'thickness.value',layer['thickness']['value']*1.001)
edited=deepcopy(source.currentDesign())
assert c.canContinueSynthesisSearch,c.synthesisSearchDisabledReason
_wait(lambda:c.computeTarget.canSubmit)
scene.click(scene.item('filmSynthesisContinueSearchButton'))
_wait(lambda:c.busy or c.queueJobId!=data['reference']['job_id'])
_wait(lambda:not c.busy,seconds=45.)
assert c.queueJobId!=data['reference']['job_id'],c.error
assert c._synthesis_payload is not None,c.error
record=c._synthesis_search_record
assert record['request'].search_checkpoint['sha256']==data['checkpoint']['sha256']
assert record['request'].seed_designs[0]['front_layers']==edited['front_layers']
assert c._synthesis_payload['result']['evaluations']>2
new=record['checkpoint']
assert new['cursors']!=data['checkpoint']['cursors'] or new['tried']!=data['checkpoint']['tried']
assert c.synthesisPhase=='本轮完成'
assert source.saveCurrent(),source.session.snapshot().error
Path(data['output']).write_text(json.dumps({'job':c.queueJobId,'checkpoint':new,
 'phase':c.synthesisPhase,'reference':c._synthesis_search_reference,'applied':c._candidate_applied}))
c.shutdown(5000)
scene.close()
source.markSourceClosed()
assert host.close_project(workspace.binding,prepare_close=lambda *_:True)
'''


def test_project_close_new_process_edit_and_continue(app, project_gui, tmp_path):
    harness, host, workspace, store, library, saved = project_gui
    _service, source, controller, scene = _optimizer(harness)
    assert controller.setSynthesisOption('max_evaluations', '160')
    ref, record = _complete(controller, scene)
    assert source.saveCurrent(), source.session.snapshot().error
    assert controller.shutdown(5000)
    scene.close()
    harness[2].remove(controller)
    harness[3].remove(scene)
    source.markSourceClosed()
    assert host.close_project(workspace.binding, prepare_close=lambda *_: True)
    output = tmp_path/'reopened.json'
    data = dict(root=str(store.root), library=str(library.directory), document=saved.document_id,
        settings=str(tmp_path/'reopened.ini'), output=str(output), reference=ref, checkpoint=record['checkpoint'])
    child = subprocess.run([sys.executable, '-c', REOPEN], input=json.dumps(data),
        encoding='utf8', capture_output=True, timeout=80.)
    assert child.returncode == 0, child.stdout+'\n'+child.stderr
    reopened = json.loads(output.read_text())
    assert reopened['job'] != ref['job_id']
    assert reopened['reference']['job_id'] == reopened['job']
    assert reopened['phase'] == '本轮完成'


def test_changed_settings_explain_rebuild_without_erasing_state(app, project_gui):
    harness, _host, _workspace, _store, _library, _saved = project_gui
    _service, _source, controller, scene = _optimizer(harness)
    ref, record = _complete(controller, scene)
    config = deepcopy(controller.exportOptimizationConfiguration())
    assert controller.setObjectiveField('pointwise.upper_bound_percent', .2)
    assert '目标' in controller.synthesisSearchDisabledReason
    assert not controller.continueSynthesisSearch()
    assert controller._synthesis_search_reference == ref
    assert controller.loadOptimizationConfiguration(config)
    assert controller.setSynthesisOption('max_total_thickness_nm', '900')
    assert '约束' in controller.synthesisSearchDisabledReason
    assert controller._synthesis_search_reference == ref
    assert controller.loadOptimizationConfiguration(config)
    assert controller.setSynthesisOption('randomize_template_search', True)
    assert '参数' in controller.synthesisSearchDisabledReason
    assert controller._synthesis_search_record['checkpoint'] == record['checkpoint']
    assert controller.loadOptimizationConfiguration(config)
    row = controller.synthesisMaterialRows[0]
    assert controller.setSynthesisRowMaterial(row['id'], 'review-later-material')
    assert '候选材料' in controller.synthesisSearchDisabledReason
    assert controller._synthesis_search_reference == ref


def test_queue_cancel_preserves_frontier_and_does_not_overwrite_live_draft(app, project_gui):
    harness, _host, _workspace, _store, _library, _saved = project_gui
    service, source, controller, scene = _optimizer(harness)
    assert controller.setSynthesisOption('max_cycles', '')
    assert controller.setSynthesisOption('max_evaluations', '')
    assert controller.duplicateSelectedObjective()
    assert controller.setObjectiveField('pointwise.upper_bound_percent', 100.)
    assert controller.setObjectiveField('pointwise.lower_bound_percent', 90.)
    from coat_sim.core.film_synthesis_archive import physical_design_hash
    before = physical_design_hash(source.currentDesign())
    scene.click(scene.item('filmOptimizationStartButton'))
    _wait(lambda: bool(controller.queueJobId))
    checkpoint = service.root/'artifacts/runtime/scheduler/jobs'/controller.queueJobId/'best-candidate.search-state.json'
    _wait(checkpoint.is_file, seconds=25.)
    source.markEditingDirty()
    controller.cancelOptimization()
    _wait(lambda: not controller.busy, seconds=30.)
    assert controller._synthesis_payload['result']['state'] == 'cancelled', controller.error
    assert physical_design_hash(source.currentDesign()) == before
    assert not controller._candidate_applied
    assert controller.hasSynthesisSearchProgress
    assert controller._synthesis_search_record['checkpoint']['sha256']
    assert controller.synthesisPhase == '本轮完成'


def test_cold_corrupt_checkpoint_is_reported_and_preserved(app, project_gui):
    harness, _host, _workspace, _store, _library, _saved = project_gui
    service, source, controller, scene = _optimizer(harness)
    ref, _record = _complete(controller, scene)
    checkpoint = service.root/'artifacts/runtime/scheduler/jobs'/ref['job_id']/'best-candidate.search-state.json'
    original = checkpoint.read_bytes()
    controller._synthesis_search_record = None
    checkpoint.write_text('{"version":', encoding='utf8')
    assert not controller.continueSynthesisSearch()
    assert controller._synthesis_search_reference == ref
    assert checkpoint.read_text() == '{"version":'
    assert controller.synthesisSearchDisabledReason
    checkpoint.write_bytes(original)
    controller._synthesis_search_error = ''
    controller._synthesis_search_reference = dict(ref, version=99)
    assert '版本' in controller.synthesisSearchDisabledReason
    assert not controller.continueSynthesisSearch()
    assert checkpoint.read_bytes() == original


def test_real_queue_refreshes_existing_spectrum_and_constraints(app, tmp_path, monkeypatch):
    from coat_sim.scheduler.client import SchedulerClient
    with isolated_case(tmp_path, monkeypatch) as case:
        controllers, scenes = [], []
        try:
            _, source, controller, scene = _optimizer((case.service, case.source, controllers, scenes))
            assert controller.setSynthesisOption('max_evaluations', '160')
            _complete(controller, scene)
            assert controller._candidate_applied, controller.error
            wait_done(case.simulation)
            applied = source.session.snapshot()
            assert case.simulation._design == applied.design
            assert case.simulation._source_design_revision == applied.design_revision
            from coat_sim.core.film_synthesis_archive import physical_design_hash
            assert physical_design_hash(case.calls[-1]['design']) == physical_design_hash(applied.design)
            layer_ids = {r['id'] for r in applied.design['front_layers']}
            assert {r['layer_id'] for r in controller.variables} == layer_ids
            assert all(r['enabled'] and r['available'] for r in controller.variables)
        finally:
            for controller in controllers:
                controller.cancelOptimization()
                controller.shutdown(5000)
            for scene in scenes:
                scene.close()
            client = SchedulerClient(case.service.root)
            if client.endpoint.exists():
                client.stop()
