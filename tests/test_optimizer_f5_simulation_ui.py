"""Optimizer F5 uses its current source session, never an unapplied candidate."""
from copy import deepcopy
import pytest
from PySide6.QtCore import Qt
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest
from tests.test_project_film_desktop import desktop as desktop, events
from tests.test_project_film_service import bound_design, material

pytestmark = [pytest.mark.qt, pytest.mark.contract]

@pytest.mark.parametrize('state', ['unopened', 'existing', 'minimized'])
def test_optimizer_f5_current_session_singleton_and_project_isolation(desktop, state):
    client, stores = desktop
    first, second = [client.open_project(root) for root in stores]
    sources = []
    for workspace, n in ((first, 2.0), (second, 3.0)):
        saved = workspace.service.create_film(bound_design(), materials={'shared': material(n=n)})
        source = client.controller_for(workspace, 'film_design')
        assert source.requestLoadSavedDesign(saved.document_id)
        sources.append(source)
    client.open_project_module(second, 'film_simulation')
    other = client.controller_for(second, 'film_simulation')
    other_before = deepcopy(other._design)
    if state != 'unopened':
        identity = client.open_project_module(first, 'film_simulation')
        sim_window = next(v[0] for r,v in client._views.items() if r.binding == first.binding and r.module_key == 'film_simulation')
        if state == 'minimized':
            sim_window.showMinimized()
            events()
            assert sim_window.windowState() & Qt.WindowState.WindowMinimized
    else:
        identity = None
    client.open_project_module(first, 'film_optimization')
    optimizer = client.controller_for(first, 'film_optimization')
    window = next(v[0] for r,v in client._views.items() if r.binding == first.binding and r.module_key == 'film_optimization')
    assert sources[0].setDesignValue('name', 'Unsaved current source')
    before = deepcopy(first.editor.snapshot().design)
    # A pending candidate must never become F5's source.
    from coat_sim.core.film_optimization import FilmOptimizationResult
    candidate = FilmOptimizationResult(
        state='completed', design_revision=first.editor.snapshot().design_revision,
        best_design={**deepcopy(before), 'name': 'Unapplied candidate'},
        best_thickness_nm={}, max_normalized_error=0, weighted_max_normalized_error=0,
        weighted_rms_error=0, targets_met=True, per_condition_errors=(), iterations=1,
        evaluations=1)
    optimizer._result = candidate
    optimizer._candidate_saved = optimizer._candidate_applied = False
    window.show()
    window.requestActivate()
    events()
    fields = [x for x in window.findChildren(QQuickItem) if x.metaObject().indexOfSignal('editingFinished()') >= 0 and x.isVisible() and x.isEnabled()]
    assert fields
    fields[0].forceActiveFocus()
    events()
    assert window.activeFocusItem() is not None
    QTest.keyClick(window, Qt.Key.Key_F5)
    for _ in range(150):
        events()
        simulation = client.controller_for(first, 'film_simulation')
        if simulation is not None and simulation._result is not None and not simulation.busy:
            break
    assert simulation is not None
    assert simulation._result is not None, simulation.error
    assert simulation.designName == 'Unsaved current source'
    assert first.editor.snapshot().design['name'] == before['name']
    assert optimizer._result is candidate
    assert not optimizer._candidate_applied
    assert candidate.best_design['name'] == 'Unapplied candidate'
    routes = [r for r in client.host.routes(first.binding.project_id) if r.module_key == 'film_simulation']
    assert len(routes) == 1
    if identity is not None:
        assert routes[0].route_id == identity
    sim_window = client.host.window_for(routes[0])
    assert sim_window.isVisible() and not sim_window.windowState() & Qt.WindowState.WindowMinimized
    assert sim_window.isActive()
    assert other._design == other_before and other._result is None
    # Repeating F5 retains the same simulation route.
    assert optimizer.openSimulationAndCalculate() == routes[0].route_id
    assert len([r for r in client.host.routes(first.binding.project_id) if r.module_key == 'film_simulation']) == 1


def test_optimizer_f5_closed_source_reports_error_without_simulation(desktop):
    client, stores = desktop
    workspace = client.open_project(next(iter(stores)))
    client.open_project_module(workspace, 'film_optimization')
    optimizer = client.controller_for(workspace, 'film_optimization')
    optimizer.markSourceClosed()
    assert optimizer.openSimulationAndCalculate() == ''
    assert optimizer.error
    assert not [r for r in client.host.routes(workspace.binding.project_id) if r.module_key == 'film_simulation']


@pytest.mark.parametrize('key', [Qt.Key.Key_F5, Qt.Key.Key_F6])
@pytest.mark.parametrize(('field_name','value','option'), [
    ('filmStructureGrowthStepsField','0.5, 1','growth_steps_text'),
    ('filmStructureGrowthPhaseFractionsField','0.25, 0.5','growth_phase_fractions_text'),
])
def test_structure_text_focus_commits_before_f5_f6(desktop, monkeypatch, key, field_name, value, option):
    from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
    from tests.test_compute_server_category_ui import _find
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtGui import QKeyEvent
    calls=[]
    def capture(owner):
        calls.append(owner)
        return True
    monkeypatch.setattr(FilmOptimizationQuickController,'startOptimization',capture)
    client, stores=desktop
    workspace=client.open_project(next(iter(stores)))
    saved=workspace.service.create_film(bound_design(),materials={'shared':material(n=2.0)})
    assert client.controller_for(workspace,'film_design').requestLoadSavedDesign(saved.document_id)
    client.open_project_module(workspace,'film_optimization')
    optimizer=client.controller_for(workspace,'film_optimization')
    for _ in range(100):
        events()
        if not optimizer.loading:
            break
    assert optimizer.setSearchMode('structure')
    window=next(v[0] for r,v in client._views.items() if r.binding==workspace.binding and r.module_key=='film_optimization')
    window.requestActivate()
    events()
    field=_find(window,field_name)
    field.forceActiveFocus()
    events()
    QTest.keyClick(window,Qt.Key.Key_A,Qt.KeyboardModifier.ControlModifier)
    QCoreApplication.sendEvent(window,QKeyEvent(QEvent.Type.KeyPress,0,Qt.KeyboardModifier.NoModifier,value))
    assert field.property('text')==value
    QTest.keyClick(window,key)
    for _ in range(150):
        events()
        if calls or client.controller_for(workspace,'film_simulation') is not None:
            break
    assert optimizer.structureOptions[option].replace(' ','') == value.replace(' ','')
    if key==Qt.Key.Key_F6:
        assert calls==[optimizer]
    else:
        assert client.controller_for(workspace,'film_simulation') is not None
