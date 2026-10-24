"""Editable GUI ownership and frozen queue persistence of thickness reduction."""
from copy import deepcopy
import json

import pytest
from PySide6.QtGui import QGuiApplication

from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
from coat_sim.core.film_synthesis_jobs import deserialize_job_payload
from coat_sim.scheduler.jobs import JobSpec
from coat_sim.ui.film_synthesis_quick_adapter import synthesis_spec
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_restore_layout import optimizer
from tests.test_film_synthesis_gui_review import _Scene

pytestmark = pytest.mark.qt


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


def configure(controller):
    assert controller.setSearchMode("synthesis")
    assert controller.setStructureMaterialSelected("acceptance-film", "film-set", True)
    assert controller.setObjectiveField("wavelength_min_nm", 550.)
    assert controller.setObjectiveField("wavelength_max_nm", 550.)


def test_qml_toggle_config_reopen_and_queue_keep_explicit_false(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        configure(controller)
        assert controller.synthesisOptions["enable_thickness_reduction"] is True
        scene = _Scene(controller, "FilmOptimizationWindow.qml")
        try:
            scene.click(scene.item("filmOptimizationAlgorithmTab"))
            toggle = scene.item("filmSynthesisThicknessReductionField")
            assert toggle.property("checked") is True
            scene.click(toggle)
            assert toggle.property("checked") is False
            assert controller._build_synthesis_request().enable_thickness_reduction is False
            saved = json.loads(json.dumps(controller.exportOptimizationConfiguration()))
            assert saved["synthesis"]["options"]["enable_thickness_reduction"] is False
            frozen = freeze_film_synthesis_request(service, controller._build_synthesis_request())
            job = JobSpec.parse_frozen(synthesis_spec(service, frozen)).to_spec()
            assert deserialize_job_payload(job.parameters["payload"])[1].enable_thickness_reduction is False
            scene.click(toggle)
            assert controller.synthesisOptions["enable_thickness_reduction"] is True
            assert frozen.enable_thickness_reduction is False
            assert controller.setSynthesisOption("search_strategy", "legacy")
            assert not toggle.isVisible()
            assert controller.setSynthesisOption("search_strategy", "hybrid")
            assert toggle.isVisible() and toggle.property("checked") is True
        finally:
            scene.close()
    with optimizer(service, design, saved) as (restored, _source, _session):
        assert restored.synthesisOptions["enable_thickness_reduction"] is False
        assert restored._build_synthesis_request().enable_thickness_reduction is False
        assert restored.setSynthesisOption("enable_thickness_reduction", True)
        assert restored.loadOptimizationConfiguration(saved)
        assert restored.synthesisOptions["enable_thickness_reduction"] is False


def test_busy_pending_and_invalid_configuration_cannot_mutate_choice(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        configure(controller)
        controller._busy = True
        assert not controller.setSynthesisOption("enable_thickness_reduction", False)
        assert controller.synthesisOptions["enable_thickness_reduction"] is True
        controller._busy = False
        saved = controller.exportOptimizationConfiguration()
        for value in (None, 0, 1, "false"):
            assert not controller.setSynthesisOption("enable_thickness_reduction", value)
            assert controller.synthesisOptions["enable_thickness_reduction"] is True
            assert controller.setSynthesisOption("enable_thickness_reduction", True)
            malformed = deepcopy(saved)
            malformed["synthesis"]["options"]["enable_thickness_reduction"] = value
            assert not controller.loadOptimizationConfiguration(malformed)
            assert controller.synthesisOptions["enable_thickness_reduction"] is True
        assert controller.loadOptimizationConfiguration(saved)
        assert controller.setSynthesisOption("max_seconds", "invalid") is False
        assert not controller.setSynthesisOption("enable_thickness_reduction", False)
        assert controller.synthesisOptions["enable_thickness_reduction"] is True
        assert controller.setSynthesisOption("max_seconds", "")
        assert controller.setSynthesisOption("enable_thickness_reduction", False)
