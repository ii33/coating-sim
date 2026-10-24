"""Optional synthesis wall-time limit routing through the existing UI adapter."""
from copy import deepcopy
import json

import pytest
from PySide6.QtGui import QGuiApplication

from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
from coat_sim.core.film_synthesis_io import (
    film_synthesis_request_from_payload,
    film_synthesis_request_to_payload,
)
from coat_sim.core.film_synthesis_jobs import (
    build_job_payload,
    canonical_parameters,
    deserialize_job_payload,
)
from coat_sim.core.film_optimization import FilmOptimizationRequest
from coat_sim.scheduler.jobs import JobSpec
from coat_sim.ui.film_synthesis_quick_adapter import (
    SYNTHESIS_DEFAULT_OPTIONS,
    build_synthesis_request,
    synthesis_options,
    synthesis_spec,
)
from tests.test_film_optimization_restore_layout import optimizer, window_runtime
from tests.test_film_auto_synthesis import setup_problem


pytestmark = pytest.mark.qt


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


def _quick_item(root, name):
    if root.objectName() == name:
        return root
    for child in root.childItems():
        found = _quick_item(child, name)
        if found is not None:
            return found
    return None


def test_optional_seconds_limit_validation_and_blank_current_option():
    assert "max_seconds" in SYNTHESIS_DEFAULT_OPTIONS
    assert SYNTHESIS_DEFAULT_OPTIONS["max_seconds"] is None
    assert synthesis_options({})["max_seconds"] is None
    assert synthesis_options({"max_seconds": ""})["max_seconds"] is None
    assert synthesis_options({"max_seconds": "12.5"})["max_seconds"] == 12.5

    for value in (True, 0, -1, "nan", "inf", "not-a-number"):
        with pytest.raises((ValueError, TypeError, OverflowError)):
            synthesis_options({"max_seconds": value})


def test_gui_independent_adapter_builds_request_with_seconds_limit():
    _service, base_request = setup_problem(materials=("low",))
    base = FilmOptimizationRequest(base_request.environment, 17, (), base_request.objectives)

    request = build_synthesis_request(
        base, base_request.candidate_materials, {"max_seconds": "2.75"})

    assert request.max_seconds == 2.75
    assert request.max_cycles is None
    assert request.max_evaluations is None


def test_seconds_limit_survives_configuration_request_and_typed_job_roundtrip(app, tmp_path):
    service, fixture_request = setup_problem(materials=("low", "high"))
    service.root = tmp_path
    # The small in-memory synthesis fixture does not expose the GUI catalog
    # enumerator; provide its two candidate rows without writing a catalog.
    service.material_options = lambda _purpose: [
        service.materials.get(name) for name in ("low", "high")]
    for material in service.materials.records.values():
        optical = material["optical_sets"][0]
        if optical["wavelength_min"] > 400.0:
            optical["wavelengths"].insert(0, 400.0)
            optical["n"].insert(0, optical["n"][0])
            optical["k"].insert(0, optical["k"][0])
            optical["wavelength_min"] = 400.0
    design = deepcopy(dict(fixture_request.environment))
    with optimizer(service, design) as (controller, _source, _session):
        assert controller.setSearchMode("synthesis")
        row_id = controller.addSynthesisMaterialRow()
        assert controller.setSynthesisRowMaterial(row_id, "low")
        assert controller.setSynthesisRowOpticalSet(row_id, "nk")
        assert controller.synthesisOptions["max_seconds"] is None
        assert controller.setSynthesisOption("max_seconds", "12.5")

        configuration = json.loads(json.dumps(controller.exportOptimizationConfiguration()))
        assert configuration["synthesis"]["options"]["max_seconds"] == 12.5
        assert controller.setSearchMode("fixed_topology")
        assert controller.loadOptimizationConfiguration(configuration)
        assert controller.synthesisOptions["max_seconds"] == 12.5

        legacy = deepcopy(configuration)
        legacy["synthesis"]["options"].pop("max_seconds")
        assert controller.loadOptimizationConfiguration(legacy)
        assert controller.synthesisOptions["max_seconds"] is None

        assert controller.setSynthesisOption("max_seconds", "0.125")
        frozen = freeze_film_synthesis_request(service, controller._build_synthesis_request())
        payload = json.loads(json.dumps(film_synthesis_request_to_payload("time-limit", frozen)))
        request_id, decoded_json, structures = film_synthesis_request_from_payload(payload)
        assert request_id == "time-limit"
        assert structures == ()
        assert decoded_json.max_seconds == 0.125

        # The durable scheduler's pure boundary must preserve the same frozen
        # request, without reopening GUI or catalog services.
        parameters = canonical_parameters(None, {
            "payload": payload, "physical_structure_metadata": [],
        })
        job_payload = build_job_payload(parameters)
        _, decoded_pure_job, metadata = deserialize_job_payload(job_payload)
        assert metadata == []
        assert decoded_pure_job.max_seconds == 0.125

        # Frozen current requests carry every execution field, including an
        # explicit None for an unbounded wall-time limit; omission is rejected.
        missing_limit = deepcopy(payload)
        missing_limit["request"].pop("max_seconds")
        with pytest.raises(ValueError, match="requires every field"):
            film_synthesis_request_from_payload(missing_limit)
        with pytest.raises(ValueError, match="requires every field"):
            canonical_parameters(None, {
                "payload": missing_limit, "physical_structure_metadata": [],
            })

        job = JobSpec.parse_frozen(synthesis_spec(service, frozen)).to_spec()
        _, decoded_job, job_structures = deserialize_job_payload(job.parameters["payload"])
        assert not job_structures
        assert decoded_job.max_seconds == 0.125

        assert controller.setSearchMode("synthesis")
        with window_runtime(app, controller) as (window, warnings):
            field = _quick_item(window.contentItem(), "filmSynthesis_max_secondsField")
            assert field is not None
            assert field.property("accessibleLabel") == "最长计算时间（秒）"
            assert not any("ReferenceError" in row or "TypeError" in row for row in warnings), warnings
