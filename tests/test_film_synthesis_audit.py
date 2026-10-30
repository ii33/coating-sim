from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from threading import Event

import pytest

from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_synthesis_audit import audit_frozen_synthesis_candidate
from coat_sim.core.film_optimization_structure import _StructureCancelled
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def test_audit_uses_same_measure_and_keeps_discrete_line_discrete():
    service, request = setup_problem(upper=.05, low=550., high=550., samples=1)
    frozen = freeze_film_synthesis_request(service, request)
    value = audit_frozen_synthesis_candidate(frozen, frozen.environment)
    assert value['audit']['physical_evaluations'] == 1
    assert value['audit']['wavelength_points'] == 1
    assert len(value['score']['errors']) == 1
    assert value['score']['max_error'] == 0
    assert value['audit']['objective_status'][0]['discrete_line']
    assert not value['audit']['continuous_certificate']


def test_actual_high_q_cavity_peak_is_found_between_dense_and_shifted_nodes():
    service, request = setup_problem(low=500., high=600., samples=3, materials=('low', 'high'))
    # Explicit ideal substrate and mirror constants; this is a synthetic physical
    # stress test, not a claimed real-material or literature reproduction.
    for identifier, index in [('glass', 1.), ('low', 1.3), ('high', 2.2)]:
        service.materials.records[identifier]['optical_sets'][0]['n'] = [index] * 3
    request = replace(request, objectives=(FilmOptimizationObjective(549., 551., 3, pointwise=FilmObjectiveConstraint(mode='range', target_value=None, lower_bound=None, upper_bound=.01), metric='T'),))
    frozen = freeze_film_synthesis_request(service, request)
    design = deepcopy(dict(frozen.environment))
    center = 550.123456
    mirror = [('high', center / (4 * 2.2)), ('low', center / (4 * 1.3))] * 14
    recipe = mirror + [('low', center / (2 * 1.3))] + list(reversed(mirror))
    design['front_layers'] = [dict(id=f'cavity-{i}', material_id=m, optical_set_id='nk',
        thickness=dict(value=d, unit='nm'), note='Synthetic symmetric cavity') for i, (m, d) in enumerate(recipe)]
    value = audit_frozen_synthesis_candidate(frozen, design)
    dense_max = max(max(row['actual_values']) for row in value['responses'])
    assert dense_max < .01
    assert value['score']['max_error'] > .9
    assert value['audit']['bracket_count'] >= 1
    assert value['audit']['completed_extrema'] == value['audit']['bracket_count']
    assert value['audit']['physical_evaluations'] > 2
    assert value['audit']['wavelength_points'] >= value['audit']['unique_condition_points']
    # Adaptive points must not change the fixed-grid weighted RMS objective.
    assert value['score']['weighted_rms_error'] == 0
    worst = max(value['score']['errors'], key=lambda e: e.actual)
    assert worst.actual > .99
    assert abs(worst.wavelength_nm - center) < 1e-5


def test_cancelled_audit_cannot_produce_a_partial_pass():
    service, request = setup_problem()
    frozen = freeze_film_synthesis_request(service, request)
    event = Event()
    def progress(value):
        if value['stage'] == 'audit_offset':
            event.set()
    with pytest.raises(_StructureCancelled):
        audit_frozen_synthesis_candidate(frozen, frozen.environment,
            cancel_event=event, progress_callback=progress)


def test_public_audit_rejects_compatibility_zero_outside_source_k_domain():
    service, request = setup_problem(low=520., high=580., samples=3)
    frozen = freeze_film_synthesis_request(service, request)
    assert frozen.frozen_material_snapshot is not None
    snapshot = deepcopy(frozen.frozen_material_snapshot)
    optical = next(row for row in snapshot['materials'] if row['id'] == 'glass')['optical_sets'][0]
    optical.clear()
    optical.update(id='nk', name='restricted explicit k domain', kind='cauchy',
        parameters={'A':1.5,'B':0.,'C':0.}, wavelength_unit='nm',
        wavelength_min=450., wavelength_max=700.,
        k_data={'wavelength_unit':'nm','wavelength_min':540.,'wavelength_max':560.,
                'wavelengths':[540.,560.],'k':[.01,.02]})
    # Deliberately bypass synthesis freeze with an ordinary/modified snapshot.
    # The audit itself must not inherit the legacy compatibility k=0 policy.
    with pytest.raises(ValueError, match=r'material=glass, optical_set=nk, wavelength=520'):
        audit_frozen_synthesis_candidate(replace(frozen, frozen_material_snapshot=snapshot), frozen.environment)


@pytest.mark.parametrize("incident_kind", ["constant", "material"])
@pytest.mark.parametrize("exit_kind", ["constant", "material"])
@pytest.mark.parametrize("sides,first_surface", [("single", "front"), ("single", "back"), ("double", "front"), ("double", "back")])
def test_active_reference_matrix_matches_forward_ports_and_audit_domain(incident_kind, exit_kind, sides, first_surface):
    import numpy as np
    from coat_sim.core.film_design_service import active_material_references, simulate_frozen
    service, request = setup_problem(upper=1., low=550., high=550., samples=1)
    design = deepcopy(request.environment)
    design["substrate"].update(sides=sides, thickness=dict(value=200., unit="nm"))
    design["first_surface"] = first_surface
    design["front_layers"] = [dict(id="front", material_id="high", optical_set_id="nk", thickness=dict(value=60., unit="nm"), note="")]
    design["back_layers"] = [dict(id="back", material_id="low", optical_set_id="nk", thickness=dict(value=80., unit="nm"), note="")]
    design["incident_medium"] = (dict(kind="constant", n=1.) if incident_kind == "constant" else
                                 dict(kind="material", material_id="low", optical_set_id="nk"))
    design["exit_medium"] = (dict(kind="constant", n=1.) if exit_kind == "constant" else
                             dict(kind="material", material_id="high", optical_set_id="nk"))
    expected = [design["substrate"], *design["front_layers"]]
    if sides == "double":
        expected += design["back_layers"]
    if incident_kind == "material":
        expected.append(design["incident_medium"])
    if sides == "double" and exit_kind == "material":
        expected.append(design["exit_medium"])
    assert active_material_references(design) == expected
    settings = dict(wavelength_min_nm=550., wavelength_max_nm=550., samples=1, first_surface=first_surface)
    payload = service.freeze_design(design, settings)
    public_request = replace(request, environment=design, frozen_material_snapshot=payload)
    if sides == "single" and first_surface == "back":
        with pytest.raises(ValueError):
            simulate_frozen(".", payload)
    else:
        direct = simulate_frozen(".", payload)
        np.testing.assert_allclose(np.array(direct["R"]) + direct["T"] + direct["A"], 1., atol=1e-12)
    if sides == "double" or first_surface == "front":
        audited = audit_frozen_synthesis_candidate(public_request, design)
        assert audited["score"]["errors"][0].actual == pytest.approx(direct["R"][0], abs=1e-13)
        print(dict(case="supported_audit_port_matrix", incident=incident_kind, exit=exit_kind,
                   R=direct["R"][0], physical_evaluations=audited["audit"]["physical_evaluations"]))
    else:
        with pytest.raises(ValueError, match="back-surface incidence"):
            audit_frozen_synthesis_candidate(public_request, design)
        print(dict(case="unsupported_audit_port_matrix", sides=sides, first_surface=first_surface,
                   incident=incident_kind, exit=exit_kind, explicit_domain_rejection=True))


@pytest.mark.parametrize("incident_kind", ["constant", "material"])
@pytest.mark.parametrize("inactive_source", ["missing", "unknown_k", "out_of_domain"])
def test_single_front_audit_ignores_inactive_exit_and_back_draft_sources(incident_kind, inactive_source):
    import numpy as np
    from coat_sim.core.film_design_service import simulate_frozen
    service, request = setup_problem(upper=1., low=550., high=550., samples=1)
    environment = deepcopy(request.environment)
    environment["exit_medium"] = dict(kind="material", material_id="inactive-exit", optical_set_id="nk")
    if incident_kind == "material":
        environment["incident_medium"] = dict(kind="material", material_id="low", optical_set_id="nk")
    if inactive_source != "missing":
        record = deepcopy(service.materials.records["high"])
        record["id"] = "inactive-exit"
        if inactive_source == "unknown_k":
            record["optical_sets"][0].pop("k")
        else:
            record["optical_sets"][0].update(wavelength_min=1000., wavelength_max=1200.,
                wavelengths=[1000., 1100., 1200.])
        service.materials.records["inactive-exit"] = record
    frozen = freeze_film_synthesis_request(service, replace(request, environment=environment))
    assert frozen.frozen_material_snapshot is not None
    assert "inactive-exit" not in {r["id"] for r in frozen.frozen_material_snapshot["materials"]}
    design = deepcopy(dict(frozen.environment))
    design["front_layers"] = [dict(id="front", material_id="high", optical_set_id="nk", thickness=dict(value=60., unit="nm"), note="")]
    design["back_layers"] = [dict(id="inactive-back", material_id="not-in-catalog", optical_set_id="missing", thickness=dict(value=80., unit="nm"), note="")]
    audited = audit_frozen_synthesis_candidate(frozen, design)
    direct = simulate_frozen(".", service.freeze_design(design,
        dict(wavelength_min_nm=550., wavelength_max_nm=550., samples=1)))
    assert audited["score"]["errors"][0].actual == pytest.approx(direct["R"][0], abs=1e-13)
    np.testing.assert_allclose(np.array(direct["R"]) + direct["T"] + direct["A"], 1., atol=1e-12)
    print(dict(case="inactive_sources_ignored", incident=incident_kind, inactive_source=inactive_source,
               R=direct["R"][0], T=direct["T"][0], audit_physical_evaluations=audited["audit"]["physical_evaluations"]))


@pytest.mark.parametrize("active_location,material", [("substrate", "glass"), ("front", "high"), ("incident", "low")])
@pytest.mark.parametrize("failure", ["material_missing", "optical_set_missing", "unknown_k"])
def test_active_audit_sources_fail_with_material_set_and_target_context(active_location, material, failure):
    service, request = setup_problem(low=520., high=580., samples=3)
    design = deepcopy(request.environment)
    design["front_layers"] = [dict(id="front", material_id="high", optical_set_id="nk", thickness=dict(value=60., unit="nm"), note="")]
    design["incident_medium"] = dict(kind="material", material_id="low", optical_set_id="nk")
    payload = service.freeze_design(design, dict(wavelength_min_nm=520., wavelength_max_nm=580., samples=3))
    if failure == "material_missing":
        payload["materials"] = [r for r in payload["materials"] if r["id"] != material]
    else:
        row = next(r for r in payload["materials"] if r["id"] == material)
        if failure == "optical_set_missing":
            row["optical_sets"] = []
        else:
            row["optical_sets"][0].pop("k")
    public_request = replace(request, environment=design, frozen_material_snapshot=payload)
    with pytest.raises(ValueError) as error:
        audit_frozen_synthesis_candidate(public_request, design)
    message = str(error.value)
    assert f"material={material}, optical_set=nk" in message and "[520, 580] nm" in message
    if failure == "unknown_k":
        assert "wavelength=520 nm" in message
    print(dict(case="active_source_rejection", location=active_location, failure=failure, error=message,
               physical_evaluations=0))


def test_single_front_inactive_exit_minimal_request_completes_actual_core():
    from coat_sim.core.film_auto_synthesis import synthesize_film_design
    service, request = setup_problem(upper=.05)
    environment = deepcopy(request.environment)
    environment["exit_medium"] = dict(kind="material", material_id="inactive-exit-not-in-catalog", optical_set_id="nk")
    result = synthesize_film_design(service, replace(request, environment=environment, max_evaluations=2))
    assert result.state == "completed" and result.targets_met and result.validation_complete
    assert result.candidate_design is not None and result.validation_score is not None
    assert result.candidate_design["exit_medium"] == environment["exit_medium"]
    print(dict(case="inactive_exit_minimal_core", state=result.state, max_error=result.validation_score["max_error"],
               logical_evaluations=result.evaluations, audit_physical_evaluations=result.audit_physical_evaluations))
