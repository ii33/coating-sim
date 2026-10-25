"""Explicit progress-policy drafts, frozen v4 roundtrip and real Qt inputs."""
from copy import deepcopy
import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
from coat_sim.core.film_synthesis_io import film_synthesis_request_from_payload, film_synthesis_request_to_payload
from coat_sim.ui.film_objective_progress_editor import ObjectiveProgressDraft
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_restore_layout import optimizer
from tests.test_film_synthesis_gui_review import _Scene, _optimizer, _wait, gui as gui

pytestmark = pytest.mark.qt
VALID = {"accepted_window_steps": "8", "relative_sse_progress_floor": "0.001",
         "relative_worst_progress_floor": "0", "stagnation_windows": "2"}


@pytest.mark.parametrize("field,value", [
    ("accepted_window_steps", ""), ("accepted_window_steps", "0"),
    ("accepted_window_steps", "1.5"), ("accepted_window_steps", "1e2"),
    ("accepted_window_steps", "9007199254740992"), ("accepted_window_steps", "１２"),
    ("stagnation_windows", "-1"), ("relative_sse_progress_floor", "nan"),
    ("relative_sse_progress_floor", "inf"), ("relative_sse_progress_floor", "-0.1"),
    ("relative_worst_progress_floor", "1e"),
])
def test_progress_policy_editor_strict_values(field, value):
    draft = ObjectiveProgressDraft(True, {**VALID, field: value})
    with pytest.raises(ValueError):
        draft.policy_payload()
    assert draft.texts[field] == value
    draft.enabled = False
    assert draft.policy_payload() is None


def _configure(controller):
    assert controller.setSearchMode("synthesis")
    assert controller.setStructureMaterialSelected("acceptance-film", "film-set", True)
    assert controller.setObjectiveField("wavelength_min_nm", 550.)
    assert controller.setObjectiveField("wavelength_max_nm", 550.)


def _fill(controller, values=VALID):
    for key, value in values.items():
        assert controller.setSynthesisProgressPolicyText(key, value)
    assert controller.finishSynthesisProgressPolicyEdit()


def test_policy_default_off_explicit_validation_and_configuration_roundtrip(tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        _configure(controller)
        assert not controller.synthesisProgressPolicyEnabled
        assert controller._build_synthesis_request().objective_progress_policy is None
        assert controller.synthesisProgressPolicyDrafts == dict.fromkeys(VALID, "")
        assert not controller.setSynthesisProgressPolicyText("accepted_window_steps", "2")
        assert controller.setSynthesisProgressPolicyEnabled(True)
        assert not controller.canStart
        with pytest.raises(ValueError):
            controller._build_synthesis_request()
        with pytest.raises(ValueError):
            controller.exportOptimizationConfiguration()
        _fill(controller)
        policy = controller._build_synthesis_request().objective_progress_policy
        assert policy is not None and policy.relative_sse_progress_floor == .001
        saved = json.loads(json.dumps(controller.exportOptimizationConfiguration()))
        assert saved["synthesis"]["options"]["objective_progress_policy"]["relative_sse_progress_floor"] == .001
        assert controller.loadOptimizationConfiguration(saved)
        assert controller.synthesisProgressPolicyDrafts == VALID
        broken = deepcopy(saved)
        broken["synthesis"]["objective_progress_editor"]["enabled"] = False
        assert not controller.loadOptimizationConfiguration(broken)
        assert controller.synthesisProgressPolicyEnabled
        assert controller.setSynthesisProgressPolicyEnabled(False)
        assert controller._build_synthesis_request().objective_progress_policy is None
        disabled = controller.exportOptimizationConfiguration()
        assert disabled["synthesis"]["objective_progress_editor"]["fields"] == VALID
    with optimizer(service, design, disabled) as (restored, _source, _session):
        assert not restored.synthesisProgressPolicyEnabled
        assert restored.synthesisProgressPolicyDrafts == VALID
        assert restored.setSynthesisProgressPolicyEnabled(True)
        assert restored._build_synthesis_request().objective_progress_policy == policy


def test_policy_v4_freeze_and_old_editor_policy_import(tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        _configure(controller)
        assert controller.setSynthesisProgressPolicyEnabled(True)
        _fill(controller)
        request = freeze_film_synthesis_request(service, controller._build_synthesis_request())
        envelope = film_synthesis_request_to_payload("progress-gui-freeze", request)
        _, restored, _ = film_synthesis_request_from_payload(envelope)
        assert restored.objective_progress_policy == request.objective_progress_policy
        saved = controller.exportOptimizationConfiguration()
        saved["synthesis"].pop("objective_progress_editor")
        assert controller.loadOptimizationConfiguration(saved)
        assert controller.synthesisProgressPolicyEnabled
        assert controller._build_synthesis_request().objective_progress_policy == request.objective_progress_policy


def test_enabled_policy_is_not_silently_ignored_by_legacy_and_close_keeps_candidate(tmp_path, monkeypatch):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        _configure(controller)
        assert controller.setSynthesisProgressPolicyEnabled(True)
        _fill(controller)
        assert controller.setSynthesisOption("search_strategy", "legacy")
        assert not controller.canStart
        with pytest.raises(ValueError, match="统一混合"):
            controller._build_synthesis_request()
        assert controller.setSynthesisProgressPolicyEnabled(False)
        assert controller._build_synthesis_request().objective_progress_policy is None
        retained = object()
        monkeypatch.setattr(controller, "_result", retained)
        asked = []
        controller.closeConfirmationRequested.connect(lambda: asked.append(True))
        controller.requestClose()
        assert controller._result is retained and asked == [True]


def _type(scene, field, text, *, finish=True):
    scene.click(field)
    QTest.keyClick(scene.window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(scene.window, Qt.Key.Key_Backspace)
    for character in text:
        QTest.keyClick(scene.window, Qt.Key(ord(character.upper())))
    if finish:
        QTest.keyClick(scene.window, Qt.Key.Key_Tab)
    QTest.qWait(20)


def test_qml_policy_real_typing_preserves_invalid_and_busy_guards(tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        _configure(controller)
        scene = _Scene(controller, "FilmOptimizationWindow.qml")
        try:
            scene.click(scene.item("filmOptimizationAlgorithmTab"))
            toggle = scene.item("filmSynthesisProgressPolicyToggle")
            assert not toggle.property("checked")
            first = scene.item("filmSynthesisProgress_accepted_window_steps")
            assert not first.isEnabled()
            scene.click(toggle)
            for key, value in VALID.items():
                _type(scene, scene.item("filmSynthesisProgress_" + key), value)
            assert controller.canStart, controller.error
            _type(scene, first, "1e")
            assert first.property("text") == "1e"
            assert controller.synthesisProgressPolicyDrafts["accepted_window_steps"] == "1e"
            assert controller.synthesisProgressPolicyError and not controller.canStart
            assert not controller.startOptimization()
            scene.window.close()
            QTest.qWait(20)
            assert scene.window.isVisible()
            assert first.property("text") == "1e"
            _type(scene, first, "12")
            assert controller._build_synthesis_request().objective_progress_policy.accepted_window_steps == 12
            controller._busy = True
            assert not controller.setSynthesisProgressPolicyEnabled(False)
            assert not controller.setSynthesisProgressPolicyText("accepted_window_steps", "9")
            controller._busy = False
            scene.click(toggle)
            assert not controller.synthesisProgressPolicyEnabled
            assert first.property("text") == "12"
            assert controller._build_synthesis_request().objective_progress_policy is None
        finally:
            scene.close()


def test_real_scheduler_freezes_same_explicit_progress_policy(gui):
    service, _source, controller, _scene = _optimizer(gui)
    assert controller.setSynthesisProgressPolicyEnabled(True)
    _fill(controller)
    expected = controller.exportOptimizationConfiguration()["synthesis"]["options"]["objective_progress_policy"]
    assert controller.startOptimization(), controller.error
    _wait(lambda: bool(controller.queueJobId))
    _wait(lambda: not controller.busy, seconds=30.)
    payload = controller._synthesis_payload
    assert payload is not None, controller.error
    assert payload["result"]["frozen_request"]["objective_progress_policy"] == expected
    spec = service.root / "artifacts/runtime/scheduler/jobs" / controller.queueJobId / "spec.json"
    frozen = json.loads(spec.read_text())["parameters"]["payload"]["request"]["request"]
    assert frozen["objective_progress_policy"] == expected
