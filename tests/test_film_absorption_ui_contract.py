"""A survives editor, public JSON and display selection without becoming R or T."""
from dataclasses import asdict, replace
import json

import numpy as np
from PySide6.QtCore import QObject, Qt
from PySide6.QtTest import QTest

from coat_sim.core.film_objective_payload import objective_from_payload, objective_to_payload, condition_from_payload
from coat_sim.core.film_optimization import FilmOptimizationRequest
from coat_sim.core.film_optimization_job import serialize_request, deserialize_request, _validate_replay_request
from coat_sim.core.mcp_contracts import FILM_BAND_OBJECTIVE_SCHEMA
from coat_sim.core.film_candidate_display import validate_candidate_display_packet
from coat_sim.core.film_full_domain_preview import full_domain_spectrum
from coat_sim.scheduler.film_display import prepare_display_configuration, validate_live_display_packet
from coat_sim.ui.film_candidate_display_controller import target_segments
from tests.film_performance_fixtures import fixture as physical_fixture
from tests.film_structure_fixtures import fixture
from tests.test_film_candidate_display import harness, objective, record, rows
from tests.test_film_optimization_restore_layout import optimizer, window_runtime, app as app


def test_a_public_payload_and_live_packet_roundtrip():
    target = replace(objective(), metric="A")
    wire = json.loads(json.dumps(objective_to_payload(target)))
    assert objective_from_payload(wire) == target
    assert condition_from_payload(json.loads(json.dumps(asdict(rows(target)[0])))).metric == "A"
    assert FILM_BAND_OBJECTIVE_SCHEMA["properties"]["metric"]["enum"] == ["R", "T", "A"]
    observer, packets, _, _ = harness(target)
    record(observer, target=target, actual=.13, best=True)
    inner = validate_candidate_display_packet(json.loads(json.dumps(packets[-1])))
    assert inner is not None and inner["selection"]["metric"] == "A"
    assert inner["best"]["spectrum"]["actual"] == [.13] * 3
    assert inner["best"]["statistics"]["metric"] == "A"
    config = prepare_display_configuration({"schema_version": 1, "generation": "a" * 32,
        "score_config": {}, "enabled": True, "selection": {"objective_index": 0, "angle_deg": 0., "metric": "A"}})
    outer = {"schema_version": 1, "kind": "coat_sim.film_live_display", "job_id": "absorption",
             "input_digest": "b" * 64, "generation": config["generation"], "control_sequence": 0, "display": inner}
    assert validate_live_display_packet(json.loads(json.dumps(outer)))["display"]["selection"]["metric"] == "A"


def test_a_full_domain_matches_independent_forward_response():
    service, design, frozen = physical_fixture(2)
    target = replace(objective(), metric="A")
    selection = {"objective_index": 0, "angle_deg": 0., "metric": "A", "spectrum_scope": "full_domain"}
    result = full_domain_spectrum(frozen, design, (target,), selection)
    direct = service.simulate(design, dict(wavelength_min_nm=500., wavelength_max_nm=600., samples=1024,
        angle_deg=0., polarization="unpolarized", extrapolation_policy="error"))
    assert np.allclose(result["actual"], direct["A"])
    assert min(result["actual"]) > 0.  # Fixture has a finite absorbing film.
    assert np.allclose(result["actual"], 1. - np.asarray(direct["R"]) - np.asarray(direct["T"]))


def test_a_editor_qml_save_restore_and_both_request_paths(app, tmp_path):
    service, _, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _, session):
        with window_runtime(app, controller) as (window, warnings):
            target = window.findChild(QObject, "filmOptimizationMetricBox")
            live = window.findChild(QObject, "filmCandidateLiveMetricBox")
            assert target is not None and live is not None
            assert target.property("count") == live.property("count") == 3
            target.forceActiveFocus()
            QTest.keyClick(window, Qt.Key.Key_Down)
            QTest.keyClick(window, Qt.Key.Key_Down)
            app.processEvents()
            assert controller.selectedObjective["metric"] == "A"
            assert target.property("currentIndex") == 2
            controller.liveDisplay.setMetric("R")
            app.processEvents()
            live.forceActiveFocus()
            QTest.keyClick(window, Qt.Key.Key_Down)
            QTest.keyClick(window, Qt.Key.Key_Down)
            app.processEvents()
            assert controller.liveDisplay.metric == "A"
            assert live.property("currentIndex") == 2
            saved = json.loads(json.dumps(controller.exportOptimizationConfiguration()))
            assert saved["objectives"][0]["metric"] == "A"
            assert session.workflow_configuration("optimization")["objectives"][0]["metric"] == "A"
            assert controller.setObjectiveField("metric", "T")
            app.processEvents()
            assert target.property("currentIndex") == 1
            assert controller.loadOptimizationConfiguration(saved)
            app.processEvents()
            assert target.property("currentIndex") == 2
            request = controller._build_request(require_variables=True)
            _validate_replay_request(request)
            restored = deserialize_request(json.loads(json.dumps(serialize_request(request))))
            assert isinstance(restored, FilmOptimizationRequest)
            assert restored.objectives[0].metric == "A"
            assert controller.setSearchMode("synthesis")
            assert controller._build_synthesis_request().objectives[0].metric == "A"
            assert not warnings, warnings


def test_a_selected_target_segments_do_not_mix_with_reflection_or_transmission():
    absorption = replace(objective(), metric="A")
    reflection = replace(absorption, metric="R", wavelength_min_nm=700., wavelength_max_nm=800.)
    transmission = replace(absorption, metric="T", wavelength_min_nm=810., wavelength_max_nm=900.)
    selection = {"objective_index": 0, "angle_deg": 0., "metric": "A"}
    segments = target_segments((absorption, reflection, transmission), selection)
    assert len(segments) == 1
    assert (segments[0]["wavelength_min_nm"], segments[0]["wavelength_max_nm"]) == (500., 600.)
