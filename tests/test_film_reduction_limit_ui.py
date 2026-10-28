"""Actual QML budget editors, strict configuration and reduction request wiring."""
from copy import deepcopy

import pytest
from PySide6.QtCore import QMetaObject, QObject
from PySide6.QtTest import QTest

from tests.test_film_optimization_cancel_apply_review import app as app, review as review
from tests.test_film_optimization_restore_layout import window_runtime

pytestmark = pytest.mark.qt


def test_reduction_defaults_ignore_ordinary_caps_and_old_configuration_does_not_restore_them(review):
    c = review.controller
    assert c.setSearchMode("reduction")
    assert c.reductionLimits == {"max_iterations": None, "max_evaluations": None}
    saved = c.exportOptimizationConfiguration()
    saved.pop("reduction_limits")
    saved["solver"].update(max_iterations=1, max_evaluations=1)
    assert c.loadOptimizationConfiguration(saved), c.error
    assert c.canStart, c.error
    request = c._build_reduction_request()
    assert request.max_iterations is request.max_evaluations is None
    assert request.base_request.max_iterations == request.base_request.max_evaluations == 1
    assert "默认不设全局次数或时长限制" in c.reductionPolicySummary


def test_optional_limits_roundtrip_clear_and_do_not_modify_ordinary_solver(review):
    c = review.controller
    assert c.setSearchMode("reduction")
    ordinary = c.exportOptimizationConfiguration()["solver"]
    assert c.setReductionLimit("max_iterations", "11")
    assert c.setReductionLimit("max_evaluations", "37")
    request = c._build_reduction_request()
    assert (request.max_iterations, request.max_evaluations) == (11, 37)
    saved = c.exportOptimizationConfiguration()
    assert saved["solver"] == ordinary
    assert c.setReductionLimit("max_iterations", "")
    assert c.setReductionLimit("max_evaluations", " ")
    assert c.reductionLimits == {"max_iterations": None, "max_evaluations": None}
    assert c.loadOptimizationConfiguration(saved), c.error
    assert c.exportOptimizationConfiguration() == saved


@pytest.mark.parametrize("name,text", [("max_iterations", "0"), ("max_evaluations", "1"),
                                      ("max_evaluations", "3.5"), ("max_iterations", "-1")])
def test_invalid_limit_blocks_run_until_same_draft_is_corrected(review, name, text):
    c = review.controller
    assert c.setSearchMode("reduction")
    assert not c.setReductionLimit(name, text)
    assert not c.canStart and c.error
    assert c.setReductionLimit(name, "")
    assert c.canStart, c.error


@pytest.mark.parametrize("limits", [
    {"max_iterations": True, "max_evaluations": None},
    {"max_iterations": None},
    {"max_iterations": None, "max_evaluations": None, "max_seconds": 2},
])
def test_invalid_import_is_atomic(review, limits):
    c = review.controller
    saved = c.exportOptimizationConfiguration()
    corrupted = deepcopy(saved)
    corrupted["reduction_limits"] = limits
    assert not c.loadOptimizationConfiguration(corrupted)
    assert c.exportOptimizationConfiguration() == saved


def test_actual_qml_blank_configure_clear_and_mode_switch_preserve_independent_limits(review, app):
    c = review.controller
    assert c.setSearchMode("reduction")
    with window_runtime(app, c) as (window, warnings):
        iterations = window.findChild(QObject, "filmOptimizationMaxIterationsField")
        evaluations = window.findChild(QObject, "filmOptimizationMaxEvaluationsField")
        assert iterations is not None and evaluations is not None
        QTest.qWait(30)
        assert iterations.property("text") == evaluations.property("text") == ""
        assert iterations.property("placeholderText") == evaluations.property("placeholderText") == "不设限制"
        for field, text in ((iterations, "9"), (evaluations, "23")):
            field.setProperty("numericEditText", text)
            field.setProperty("numericValueEdited", True)
            assert QMetaObject.invokeMethod(field, "commitDraft")
        assert c.reductionLimits == {"max_iterations": 9, "max_evaluations": 23}
        request = c._build_reduction_request()
        assert (request.max_iterations, request.max_evaluations) == (9, 23)
        assert c.setSearchMode("fixed_topology")
        QTest.qWait(30)
        assert iterations.property("numericDisplayValue") == 200
        assert evaluations.property("numericDisplayValue") == 1000
        assert c.setSearchMode("reduction")
        QTest.qWait(30)
        assert iterations.property("numericDisplayValue") == 9
        assert evaluations.property("numericDisplayValue") == 23
        for field in (iterations, evaluations):
            field.setProperty("numericEditText", "")
            field.setProperty("numericValueEdited", True)
            assert QMetaObject.invokeMethod(field, "commitDraft")
        assert c.reductionLimits == {"max_iterations": None, "max_evaluations": None}
        QTest.qWait(30)
        assert iterations.property("text") == evaluations.property("text") == ""
        assert not [warning for warning in warnings if "Error" in warning]
