"""Real Qt design F6 commits current buffers and refreshes same-project optimizer."""
from copy import deepcopy
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from tests.test_project_film_desktop import desktop as desktop, events
from tests.test_compute_server_category_ui import _find
from tests.test_project_film_service import bound_design, material
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController

pytestmark = [pytest.mark.qt, pytest.mark.contract]

def prepare(desktop):
    client, stores = desktop
    workspace = client.open_project(next(iter(stores)))
    saved = workspace.service.create_film(bound_design(), materials={'shared': material(n=2.0)})
    source = client.controller_for(workspace, 'film_design')
    assert source.requestLoadSavedDesign(saved.document_id)
    assert source.addLayer('front')
    identity = source.front_layers.ids()[0]
    assert source.setLayerValue('front',identity,'material_id','shared')
    assert source.setLayerValue('front',identity,'optical_set_id','visible')
    assert source.setLayerValue('front',identity,'thickness.nm',100)
    window = next(v[0] for r,v in client._views.items() if r.binding == workspace.binding and r.module_key == 'film_design')
    window.requestActivate()
    events()
    tab = _find(window,'filmDesignTabFront')
    tab.forceActiveFocus()
    QTest.keyClick(window,Qt.Key.Key_Space)
    events()
    return client, workspace, source, window, identity

def type_value(window, field, value):
    field.forceActiveFocus()
    events()
    QTest.keyClick(window,Qt.Key.Key_A,Qt.KeyboardModifier.ControlModifier)
    QCoreApplication.sendEvent(window,QKeyEvent(QEvent.Type.KeyPress,0,Qt.KeyboardModifier.NoModifier,value))
    events()
    assert field.property('text') == value

@pytest.mark.parametrize('existing', [False,True])
def test_f6_live_input_refresh_and_f5_roundtrip(desktop, monkeypatch, existing):
    client, workspace, source, window, layer = prepare(desktop)
    # A second project's optimizer is never refreshed by this source's F6.
    other_root = next(root for root in desktop[1] if root != workspace.binding.root)
    other_workspace = client.open_project(other_root)
    client.open_project_module(other_workspace, 'film_optimization')
    other_optimizer = client.controller_for(other_workspace, 'film_optimization')
    other_before = deepcopy(other_optimizer._design)
    calls=[]
    def capture(controller):
        calls.append(deepcopy(controller._design))
        return True
    monkeypatch.setattr(FilmOptimizationQuickController,'startOptimization',capture)
    if existing:
        identity=client.open_project_module(workspace,'film_optimization')
        optimizer=client.controller_for(workspace,'film_optimization')
        for _ in range(100):
            events()
            if not optimizer.loading:
                break
        assert optimizer.setVariableBound('front',layer,'lower_nm',23)
        assert optimizer.setObjectiveField('name','Retained target')
    field=_find(window,'filmLayerNm_'+layer)
    window.requestActivate()
    type_value(window,field,'225')
    assert workspace.editor.snapshot().design['front_layers'][0]['thickness']['value'] == 100
    QTest.keyClick(window,Qt.Key.Key_F6)
    for _ in range(150):
        events()
        if calls:
            break
    assert calls and calls[-1]['front_layers'][0]['thickness']['value'] == 225
    optimizer=client.controller_for(workspace,'film_optimization')
    routes=[r for r in client.host.routes(workspace.binding.project_id) if r.module_key=='film_optimization']
    assert len(routes)==1
    if existing:
        assert routes[0].route_id == identity
        assert optimizer._variables[0]['lower_nm'] == 23
        assert optimizer._objectives[0]['name'] == 'Retained target'
    # Add then delete a layer without saving; existing optimizer must track IDs.
    assert source.addLayer('front')
    added=source.front_layers.ids()[-1]
    assert source.setLayerValue('front',added,'material_id','shared')
    assert source.setLayerValue('front',added,'optical_set_id','visible')
    assert source.openOptimizationAndStart() == routes[0].route_id
    assert {r['layer_id'] for r in optimizer._variables} == {layer,added}
    assert source.deleteLayer('front',added)
    assert source.openOptimizationAndStart() == routes[0].route_id
    assert [r['layer_id'] for r in optimizer._variables] == [layer]
    assert optimizer.openSimulationAndCalculate()
    for _ in range(150):
        events()
        sim=client.controller_for(workspace,'film_simulation')
        if sim and sim._result is not None and not sim.busy:
            break
    assert sim._design['front_layers'][0]['thickness']['value'] == 225
    assert sim._result is not None
    assert other_optimizer._design == other_before

@pytest.mark.parametrize('value', ['-', ''])
def test_f6_invalid_buffer_reports_error_without_old_value(desktop, monkeypatch, value):
    client, workspace, source, window, layer=prepare(desktop)
    if value:
        type_value(window,_find(window,'filmLayerNm_'+layer),value)
    else:
        field=_find(window,'filmLayerNm_'+layer)
        field.forceActiveFocus()
        events()
        QTest.keyClick(window,Qt.Key.Key_A,Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(window,Qt.Key.Key_Backspace)
        events()
    QTest.keyClick(window,Qt.Key.Key_F6)
    events()
    assert window.property('shortcutInputError')
    assert window.findChild(__import__('PySide6.QtCore',fromlist=['QObject']).QObject,'filmDesignErrorDialog').property('visible')
    assert client.controller_for(workspace,'film_optimization') is None
    assert workspace.editor.snapshot().design['front_layers'][0]['thickness']['value'] == 100
