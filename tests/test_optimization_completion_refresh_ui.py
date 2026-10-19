"""Final optimization delivery writes one unsaved edit and refreshes its spectrum."""
from copy import deepcopy
from dataclasses import replace
import pytest
from PySide6.QtCore import Qt
from coat_sim.core.film_optimization import FilmOptimizationResult
from tests.test_project_film_desktop import desktop as _desktop_fixture, events
from tests.test_project_film_service import bound_design, material

desktop = _desktop_fixture

pytestmark=[pytest.mark.qt,pytest.mark.contract]

def setup(desktop):
    client,stores=desktop
    first,second=[client.open_project(root) for root in stores]
    design=bound_design()
    design['front_layers']=[{'id':'film-layer','material_id':'film','optical_set_id':'visible','symbol':'A', 'thickness':{'value':100.0,'unit':'nm'}}]
    saved=first.service.create_film(design,materials={'shared':material(n=2.0),'film':material('film',n=1.5)})
    source=client.controller_for(first,'film_design')
    assert source.requestLoadSavedDesign(saved.document_id)
    client.open_project_module(first,'film_optimization')
    optimizer=client.controller_for(first,'film_optimization')
    for _ in range(100):
        events()
        if not optimizer.loading:
            break
    # The optimization worker transport is the seam; all draft application,
    # native Qt windows and optical simulation below are production objects.
    optimizer._project_compute=None
    optimizer._operation_serial=7
    optimizer._active_operation_id=7
    optimizer._operation_edit_epoch=source.pendingEditEpoch
    optimizer._busy=True
    before=deepcopy(source.session.snapshot().design)
    candidate=deepcopy(before)
    candidate['front_layers'][0]['thickness']['value']=225.0
    candidate['name']='Candidate metadata must not replace the source'
    result=FilmOptimizationResult(state='completed',design_revision=source.session.snapshot().design_revision,
        best_design=candidate,best_thickness_nm={'film-layer':225.0},max_normalized_error=0,
        weighted_max_normalized_error=0,weighted_rms_error=0,targets_met=True,
        per_condition_errors=(),iterations=1,evaluations=1,message='fixture final',validation_complete=True)
    return client,first,second,source,optimizer,before,result,saved.document_id

def finish(optimizer,result,error=None):
    optimizer._run_coordinator._on_worker_finished(7,result,[],error)
    events()

def wait_sim(client,workspace):
    for _ in range(150):
        events()
        simulation=client.controller_for(workspace,'film_simulation')
        if simulation and not simulation.busy and simulation._result is not None:
            return simulation
    raise AssertionError('spectrum did not finish')

@pytest.mark.parametrize('state',['unopened','existing','minimized'])
def test_final_result_applies_once_refreshes_curve_and_can_undo(desktop,state):
    client,first,second,source,optimizer,before,result,document_id=setup(desktop)
    other_source=client.controller_for(second,'film_design')
    other_before=deepcopy(other_source.session.snapshot().design)
    identity=None
    if state!='unopened':
        identity=client.open_project_module(first,'film_simulation')
        window=next(v[0] for r,v in client._views.items() if r.binding==first.binding and r.module_key=='film_simulation')
        if state=='minimized':
            window.showMinimized()
            events()
    old_revision=source.session.snapshot().design_revision
    applied=[]
    source.optimizationApplied.connect(lambda:applied.append(True))
    finish(optimizer,result)
    assert source.session.snapshot().design['front_layers'][0]['thickness']['value']==225
    assert source.session.snapshot().design_revision==old_revision+1
    assert source.dirty and source.canUndoOptimizedDesign
    assert source.session.snapshot().design['name']==before['name']
    simulation=wait_sim(client,first)
    assert simulation._design['front_layers'][0]['thickness']['value']==225
    baseline=first.service.simulate(before,simulation._current_settings())
    assert simulation._result['R'] != pytest.approx(baseline['R'])
    routes=[r for r in client.host.routes(first.binding.project_id) if r.module_key=='film_simulation']
    assert len(routes)==1
    if identity:
        assert routes[0].route_id==identity
    assert not client.host.window_for(routes[0]).windowState() & Qt.WindowState.WindowMinimized
    assert first.service.load_saved_design(document_id)['front_layers'][0]['thickness']['value']==100
    assert other_source.session.snapshot().design==other_before
    finish(optimizer,result)
    assert applied==[True] and source.session.snapshot().design_revision==old_revision+1
    assert source.undoOptimizedDesign()
    assert source.session.snapshot().design['front_layers'][0]['thickness']['value']==100
    assert not source.canUndoOptimizedDesign
    assert not source.undoOptimizedDesign()
    simulation=wait_sim(client,first)
    assert simulation._design['front_layers'][0]['thickness']['value']==100

@pytest.mark.parametrize('case',['revision','pending_edit','layer_id','invalid','cancelled','failed'])
def test_conflict_cancel_failure_never_overwrites_or_opens_spectrum(desktop,case):
    client,first,second,source,optimizer,before,result,document_id=setup(desktop)
    error: str | dict[str, str] | None = None
    if case=='revision':
        assert source.setDesignValue('name','User edit during run')
    elif case=='pending_edit':
        source.markEditingDirty()
    elif case=='layer_id':
        candidate=deepcopy(result.best_design)
        candidate['front_layers'][0]['id']='wrong-layer'
        result=replace(result,best_design=candidate)
    elif case=='invalid':
        candidate=deepcopy(result.best_design)
        candidate['front_layers'][0]['thickness']['value']=-1
        result=replace(result,best_design=candidate)
    elif case=='cancelled':
        error={'state':'cancelled'}
    elif case=='failed':
        error='fixture failure'
    finish(optimizer,result,error)
    assert source.session.snapshot().design['front_layers'][0]['thickness']['value']==100
    assert client.controller_for(first,'film_simulation') is None
    assert not source.canUndoOptimizedDesign
    if case not in ('cancelled','failed'):
        assert optimizer.error


def test_undo_rejects_uncommitted_edit_after_application(desktop):
    client,first,second,source,optimizer,before,result,document_id=setup(desktop)
    finish(optimizer,result)
    wait_sim(client,first)
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtTest import QTest
    from tests.test_compute_server_category_ui import _find
    from tests.test_design_f6_live_ui import type_value
    window=next(v[0] for r,v in client._views.items() if r.binding==first.binding and r.module_key=='film_design')
    button=window.findChild(QQuickItem,'filmUndoOptimizationButton')
    assert button is not None and button.property('visible')
    window.requestActivate()
    events()
    _find(window,'filmDesignTabFront').forceActiveFocus()
    QTest.keyClick(window,Qt.Key.Key_Space)
    events()
    type_value(window,_find(window,'filmLayerNm_film-layer'),'240')
    assert not button.property('visible')
    assert not source.canUndoOptimizedDesign
    assert not source.undoOptimizedDesign()
    assert source.session.snapshot().design['front_layers'][0]['thickness']['value']==225


def test_saved_application_cannot_resurrect_undo_after_workflow_edit(desktop):
    client,first,second,source,optimizer,before,result,document_id=setup(desktop)
    finish(optimizer,result)
    wait_sim(client,first)
    assert source.canUndoOptimizedDesign
    assert source.session.save_current()
    assert not source.canUndoOptimizedDesign
    configuration=source.session.workflow_configuration('simulation')
    configuration['samples']=29
    assert source.session.set_workflow_configuration('simulation',configuration)
    assert source.dirty and not source.canUndoOptimizedDesign
    assert not source.undoOptimizedDesign()
    assert source.session.snapshot().design['front_layers'][0]['thickness']['value']==225

@pytest.mark.parametrize('boundary',['confirmation','expression'])
def test_atomic_application_rejects_busy_source_boundary(desktop,boundary):
    client,first,second,source,optimizer,before,result,document_id=setup(desktop)
    if boundary=='confirmation':
        source.session._pending_confirmation={'action':'close'}
    else:
        source.session._expression_busy=True
    finish(optimizer,result)
    assert source.session.snapshot().design['front_layers'][0]['thickness']['value']==100
    assert not source.canUndoOptimizedDesign
    source.session._pending_confirmation=None
    source.session._expression_busy=False


def test_file_save_permanently_invalidates_optimization_undo(tmp_path):
    from tests.film_structure_fixtures import fixture
    from coat_sim.core.film_design_session import FilmDesignEditSession
    service,library,design=fixture(tmp_path)
    session=FilmDesignEditSession(service)
    assert session.request_replace_design(design)
    if session.snapshot().pending_confirmation:
        assert session.resolve_pending_action(True)
    candidate=deepcopy(session.snapshot().design)
    candidate['front_layers'][0]['thickness']['value']=225
    assert session.apply_optimized_design(candidate,session.snapshot().design_revision)
    revision=session.snapshot().design_revision
    assert session.can_undo_optimized_design(revision)
    assert session.save_file(str(tmp_path/'optimized.cfilm'))
    assert session.set_workflow_configuration('simulation',{'samples':31})
    assert not session.can_undo_optimized_design(revision)
    assert not session.undo_optimized_design(revision)
