"""Unified target bounds/range form preserves exact request and stored meanings."""
from copy import deepcopy
import pytest
from PySide6.QtCore import QObject, Qt, QMetaObject
from PySide6.QtQuick import QQuickItem
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_restore_layout import optimizer, window_runtime, app as app
from tests.test_design_f6_live_ui import type_value
from tests.test_compute_server_category_ui import _find, _settle

pytestmark=[pytest.mark.qt,pytest.mark.contract]


def test_bounds_derive_request_and_roundtrip_without_hidden_mode(app,tmp_path):
    service,library,design=fixture(tmp_path)
    with optimizer(service,design) as (controller,source,session):
        row=controller.selectedObjective
        assert row['wavelength_step_nm']==row['angle_step_deg']==1
        assert controller._build_request(require_variables=False).objectives[0].angles_deg==(0.0,)
        assert controller.setObjectiveField('upper_bound_percent',20)
        assert controller.setObjectiveField('lower_bound_percent',10)
        objective=controller._build_request(require_variables=False).objectives[0]
        assert objective.mode=='range' and objective.target_value is None
        assert (objective.lower_bound,objective.upper_bound)==(.1,.2)
        assert controller.setObjectiveField('lower_bound_percent',20)
        objective=controller._build_request(require_variables=False).objectives[0]
        assert objective.mode=='value' and objective.target_value==.2
        assert objective.lower_bound is objective.upper_bound is None
        saved=controller.exportOptimizationConfiguration()
        assert saved['objectives'][0]['mode']=='range'
        assert controller.loadOptimizationConfiguration(saved)
        assert controller._build_request(require_variables=False).objectives[0]==objective
        assert not controller.setObjectiveField('lower_bound_percent',21)
        assert controller.error
        with pytest.raises(ValueError):
            controller._build_request(require_variables=False)


def test_value_configuration_is_losslessly_expressed_as_equal_bounds(app,tmp_path):
    service,library,design=fixture(tmp_path)
    with optimizer(service,design) as (controller,source,session):
        saved=controller.exportOptimizationConfiguration()
        saved['objectives'][0].update(mode='value',target_value_percent=37.5,lower_bound_percent=0,upper_bound_percent=100)
        assert controller.loadOptimizationConfiguration(saved)
        assert controller.selectedObjective['mode']=='range'
        assert controller.selectedObjective['lower_bound_percent']==controller.selectedObjective['upper_bound_percent']==37.5
        assert controller._build_request(require_variables=False).objectives[0].target_value==.375
        invalid=deepcopy(saved)
        invalid['objectives'][0]['mode']='maximize'
        assert not controller.loadOptimizationConfiguration(invalid)
        assert controller.error


def test_nonuniform_angles_require_explicit_confirmation_and_preserve_saved_original(app,tmp_path):
    service,library,design=fixture(tmp_path)
    with optimizer(service,design) as (controller,source,session):
        saved=controller.exportOptimizationConfiguration()
        saved['objectives'][0].update(angle_sampling_mode='points',angles_text='0, 13.5, 47')
        assert controller.loadOptimizationConfiguration(saved)
        assert controller.exportOptimizationConfiguration()['objectives'][0]['angles_text']=='0, 13.5, 47'
        with pytest.raises(ValueError,match='确认'):
            controller._build_request(require_variables=False)
        assert not controller.canStart
        with window_runtime(app,controller) as (window,warnings):
            assert window.findChild(QObject,'filmOptimizationAngleSamplingModeBox') is None
            assert window.findChild(QObject,'filmOptimizationAnglesField') is None
            warning=_find(window,'filmOptimizationAngleRangeWarning')
            assert warning.property('visible') and '13.5' in warning.property('text')
            for name,text in [('filmOptimizationAngleMinField','2'),('filmOptimizationAngleMaxField','4'),('filmOptimizationAngleStepField','1')]:
                type_value(window,_find(window,name),text)
            button=_find(window,'filmOptimizationConfirmAngleRangeButton')
            assert QMetaObject.invokeMethod(button,'clicked',Qt.ConnectionType.DirectConnection)
            _settle()
            assert not warning.property('visible')
            assert controller.selectedObjective['angle_sampling_mode']=='range'
            assert controller._build_request(require_variables=False).objectives[0].angles_deg==(2.0,3.0,4.0)

@pytest.mark.parametrize('width',[720,1280])
def test_qml_inline_bounds_and_precision_captions(app,tmp_path,width):
    service,library,design=fixture(tmp_path)
    with optimizer(service,design) as (controller,source,session):
        with window_runtime(app,controller) as (window,warnings):
            window.resize(width,800)
            _settle()
            assert window.findChild(QObject,'filmOptimizationModeBox') is None
            assert window.findChild(QObject,'filmOptimizationTargetValueField') is None
            assert window.findChild(QObject,'filmOptimizationMetricBox') is not None
            assert window.findChild(QObject,'filmOptimizationPolarizationBox') is not None
            lower=_find(window,'filmOptimizationLowerBoundField')
            upper=_find(window,'filmOptimizationUpperBoundField')
            assert lower.parentItem() is upper.parentItem()
            assert lower.parentItem().objectName()=='filmOptimizationTargetBoundsRow'
            assert lower.y()==pytest.approx(upper.y())
            assert lower.width()>=64 and upper.width()>=64
            assert upper.x()+upper.width()<=upper.parentItem().width()+1
            assert controller.setObjectiveField('upper_bound_percent',25)
            type_value(window,lower,'25')
            assert QMetaObject.invokeMethod(lower,'editingFinished',Qt.ConnectionType.DirectConnection)
            _settle()
            assert controller._build_request(require_variables=False).objectives[0].target_value==.25
            labels=[x.property('text') for x in window.findChildren(QQuickItem)]
            assert '采样精度 (nm)' in labels and '精度 (°)' in labels
            assert '采样步长 (nm)' not in labels and '步长 (°)' not in labels


@pytest.mark.parametrize(('field','value'),[('lower_bound_percent',21),('wavelength_step_nm',0)])
def test_angle_confirmation_cannot_clear_unrelated_input_error(app,tmp_path,field,value):
    service,library,design=fixture(tmp_path)
    with optimizer(service,design) as (controller,source,session):
        saved=controller.exportOptimizationConfiguration()
        saved['objectives'][0].update(angle_sampling_mode='points',angles_text='0, 13.5, 47')
        assert controller.loadOptimizationConfiguration(saved)
        assert not controller.setObjectiveField(field,value)
        if field == 'lower_bound_percent':
            range_state = (
                controller.objectiveBoundsError,
                deepcopy(controller._objective_range_errors),
                deepcopy(controller._objective_range_drafts),
                deepcopy(controller._objectives),
            )
            assert range_state[0]
        else:
            key=controller._pending_edit_error_key
        assert not controller.confirmObjectiveAngleRange('0','10','1')
        if field == 'lower_bound_percent':
            assert controller.objectiveBoundsError == range_state[0]
            assert controller._objective_range_errors == range_state[1]
            assert controller._objective_range_drafts == range_state[2]
            assert controller._objectives == range_state[3]
        else:
            assert controller._pending_edit_error_key==key and controller._pending_edit_error
        assert controller.selectedObjective['angle_sampling_mode']=='points'
        with pytest.raises(ValueError):
            controller._build_request(require_variables=False)


def test_angle_confirmation_can_repair_only_angle_input_error(app,tmp_path):
    service,library,design=fixture(tmp_path)
    with optimizer(service,design) as (controller,source,session):
        saved=controller.exportOptimizationConfiguration()
        saved['objectives'][0].update(angle_sampling_mode='points',angles_text='0, 13.5, 47')
        assert controller.loadOptimizationConfiguration(saved)
        assert not controller.setObjectiveField('angle_min_deg','invalid')
        assert controller.confirmObjectiveAngleRange('5','5','1')
        assert not controller._pending_edit_error
        assert controller._build_request(require_variables=False).objectives[0].angles_deg==(5.0,)
