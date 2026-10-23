"""Template randomization owns a uint32 draft and displays the queued frozen seed."""
from copy import deepcopy
import json
from threading import Event

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
from coat_sim.ui.film_synthesis_quick_adapter import synthesis_options, synthesis_structure_search_seed
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_restore_layout import optimizer
from tests.test_film_synthesis_gui_review import _Scene, _optimizer, _wait, gui as gui

pytestmark = pytest.mark.qt


@pytest.mark.parametrize("value", [True, False, 1.0, 1.5, -1, 4294967296,
    "-1", "1.0", "1e2", "4294967296", "１２", "1e", "+1"])
def test_seed_parser_rejects_non_decimal_uint32(value):
    with pytest.raises(ValueError):
        synthesis_structure_search_seed(value)


@pytest.mark.parametrize("value, expected", [(None, None), ("", None), ("  ", None),
    (0, 0), ("0", 0), (4294967295, 4294967295), ("4294967295", 4294967295)])
def test_seed_parser_keeps_exact_boundaries(value, expected):
    assert synthesis_structure_search_seed(value) == expected


def _configure(controller):
    assert controller.setSearchMode("synthesis")
    assert controller.setStructureMaterialSelected("acceptance-film", "film-set", True)
    assert controller.setObjectiveField("wavelength_min_nm", 550.)
    assert controller.setObjectiveField("wavelength_max_nm", 550.)


def test_current_configuration_strictness_and_off_seed_compatibility(tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        _configure(controller)
        assert controller.synthesisOptions["randomize_template_search"] is False
        assert controller._build_synthesis_request().structure_search_seed == 0
        assert controller.synthesisActualStructureSearchSeedText == ""
        assert not controller.setSynthesisStructureSearchSeedText("8")
        assert controller.setSynthesisOption("structure_search_seed", "4294967295")
        assert controller._build_synthesis_request().structure_search_seed == 4294967295
        saved = controller.exportOptimizationConfiguration()
        for name, value in (("random_seed", 1), ("randomize_template_search", 1),
                            ("structure_search_seed", 1.0)):
            bad = deepcopy(saved)
            bad["synthesis"]["options"][name] = value
            assert not controller.loadOptimizationConfiguration(bad)
        assert controller.loadOptimizationConfiguration(saved)
        assert controller.synthesisStructureSearchSeedText == "4294967295"
        with pytest.raises(ValueError):
            synthesis_options({"random_seed": 0})


def _type(scene, field, text, *, tab=True):
    scene.click(field)
    QTest.keyClick(scene.window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(scene.window, Qt.Key.Key_Backspace)
    for character in text:
        QTest.keyClick(scene.window, Qt.Key(ord(character.upper())))
    if tab:
        QTest.keyClick(scene.window, Qt.Key.Key_Tab)
    QTest.qWait(20)


def test_qml_seed_typing_tab_invalid_toggle_and_reopen(tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        _configure(controller)
        scene = _Scene(controller, "FilmOptimizationWindow.qml")
        try:
            scene.click(scene.item("filmOptimizationAlgorithmTab"))
            switch = scene.item("filmSynthesisRandomizeTemplateSearchField")
            field = scene.item("filmSynthesisStructureSearchSeedField")
            actual = scene.item("filmSynthesisActualStructureSearchSeedField")
            assert switch.property("checked") is False
            assert not field.isEnabled()
            assert field.property("placeholderText") == "留空自动"
            assert actual.property("readOnly") is True
            scene.click(switch)
            for value in ("0", "4294967295"):
                _type(scene, field, value)
                assert field.property("text") == value
                assert controller._build_synthesis_request().structure_search_seed == int(value)
                assert controller.canStart, controller.error
            for value in ("1e", "1.5", "-1", "4294967296"):
                _type(scene, field, value)
                assert field.property("text") == value
                assert controller.synthesisStructureSearchSeedError
                assert not controller.canStart
                assert not controller.startOptimization()
                with pytest.raises(ValueError):
                    controller._build_synthesis_request()
                with pytest.raises(ValueError):
                    controller.exportOptimizationConfiguration()
            scene.window.close()
            QTest.qWait(20)
            assert scene.window.isVisible()
            assert field.property("text") == "4294967296"
            _type(scene, field, "4294967295", tab=False)
            scene.click(switch)
            assert not field.isEnabled()
            assert field.property("text") == "4294967295"
            saved = json.loads(json.dumps(controller.exportOptimizationConfiguration()))
            assert saved["synthesis"]["options"]["structure_search_seed"] == 4294967295
            scene.click(switch)
            assert field.isEnabled() and field.property("text") == "4294967295"
        finally:
            scene.close()
    with optimizer(service, design, saved) as (restored, _source, _session):
        assert restored.synthesisOptions["randomize_template_search"] is False
        assert restored.synthesisStructureSearchSeedText == "4294967295"
        assert restored.setSynthesisOption("randomize_template_search", True)
        assert restored._build_synthesis_request().structure_search_seed == 4294967295


def test_blank_auto_raw_build_properties_and_formal_freeze(tmp_path, monkeypatch):
    from coat_sim.ui.film_optimization_quick_controller import _AngleGridPreparationTask, _PreparedAngleGrid
    service, _library, design = fixture(tmp_path)
    draws = []

    def draw(bits: int) -> int:
        draws.append(bits)
        return 3141592653

    with optimizer(service, design) as (controller, _source, _session):
        _configure(controller)
        monkeypatch.setattr("secrets.randbits", draw)
        assert controller.setSynthesisOption("randomize_template_search", True)
        assert controller.setSynthesisStructureSearchSeedText("")
        raw = controller._build_synthesis_request()
        assert raw.structure_search_seed is None
        assert controller._build_synthesis_request().structure_search_seed is None
        assert controller.synthesisActualStructureSearchSeedText == ""
        assert controller.synthesisOptions["structure_search_seed"] is None
        assert draws == []
        frozen = freeze_film_synthesis_request(service, raw)
        assert type(frozen.structure_search_seed) is int
        assert 0 <= frozen.structure_search_seed <= 4294967295
        assert freeze_film_synthesis_request(service, frozen).structure_search_seed == frozen.structure_search_seed
        assert draws == [32]
        worker = _AngleGridPreparationTask(tuple(deepcopy(controller._objectives)), raw,
                                          "synthesis", service, Event(), 3)
        results = []
        worker.signals.finished.connect(lambda _serial, result, _baseline, error: results.append((result, error)))
        worker.run()
        assert len(results) == 1 and results[0][1] is None, results
        prepared = results[0][0]
        assert isinstance(prepared, _PreparedAngleGrid)
        assert type(prepared.request.structure_search_seed) is int
        assert prepared.spec["parameters"]["payload"]["request"]["request"]["structure_search_seed"] == prepared.request.structure_search_seed
        assert draws == [32, 32]  # A separate raw request is a separate formal freeze.


def test_qml_real_local_ipc_submission_displays_same_frozen_actual_seed(gui):
    # This uses the real private local scheduler and CPU worker, no MockTransport.
    service, _source, controller, scene = _optimizer(gui)
    scene.click(scene.item("filmOptimizationAlgorithmTab"))
    scene.click(scene.item("filmSynthesisRandomizeTemplateSearchField"))
    assert controller._build_synthesis_request().structure_search_seed is None
    actual = scene.item("filmSynthesisActualStructureSearchSeedField")
    assert actual.property("text") == ""
    scene.click(scene.item("filmOptimizationStartButton"))
    assert controller.synthesisActualStructureSearchSeedText
    expected = int(controller.synthesisActualStructureSearchSeedText)
    assert actual.property("text") == str(expected)
    _wait(lambda: bool(controller.queueJobId))
    job_id = controller.queueJobId
    _wait(lambda: not controller.busy, seconds=30.)
    payload = controller._synthesis_payload
    assert payload is not None, controller.error
    assert payload["result"]["frozen_request"]["structure_search_seed"] == expected
    spec_path = service.root / "artifacts/runtime/scheduler/jobs" / job_id / "spec.json"
    frozen = json.loads(spec_path.read_text(encoding="utf-8"))["parameters"]["payload"]["request"]["request"]
    assert frozen["structure_search_seed"] == expected
    assert frozen["randomize_template_search"] is True
    assert controller.synthesisActualStructureSearchSeedText == str(expected)
    old_serial = controller._operation_serial
    assert controller.setSynthesisOption("randomize_template_search", False)
    assert controller.synthesisActualStructureSearchSeedText == ""
    controller._on_finished(old_serial - 1, None, [], {"message": "late old seed"})
    assert controller.synthesisActualStructureSearchSeedText == ""
