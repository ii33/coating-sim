"""Synthetic tests of normal-incidence narrow-rejection proposal geometry.

The analytic test constants are not measurements or a feasibility guarantee.
"""
from copy import deepcopy
from dataclasses import replace
from itertools import islice
import math
from typing import Any

import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_synthesis_global as global_search
from coat_sim.core import film_synthesis_rejection_priors as priors
from coat_sim.core.film_synthesis_continuation import search_fingerprint
from coat_sim.core.film_synthesis_proposal_objectives import reflective_proposal_request
from coat_sim.core.film_synthesis_surfaces import validate_candidate_environment
from tests.test_film_synthesis_transmission_proposal_hints import synthetic_reference_request
from tools.run_film_synthesis_acceptance import AcceptanceService, synthetic_service

pytestmark = pytest.mark.algorithm


def normal_hint_request():
    frozen = synthetic_reference_request()
    normal = replace(frozen, max_layers=None,
                     objectives=tuple(replace(o, angles_deg=(0.,)) for o in frozen.objectives))
    return reflective_proposal_request(normal)[0]


def test_rejection_geometry_is_positive_finite_target_derived_and_retains_known_k():
    request = normal_hint_request()
    before = deepcopy(request)
    output = list(islice(priors.iter_rejection_proposals(request), 2))
    assert output and request == before
    first = output[0]
    assert first.provenance['family'] == 'narrow_rejection'
    assert first.provenance['acceptance_equivalence'] is False
    assert first.provenance['construction_scope'].startswith('normal-incidence')
    assert first.design['substrate'] == request.environment['substrate']
    assert first.design['back_layers'] == request.environment['back_layers']
    assert first.design['front_layers']
    for row in first.design['front_layers']:
        assert row['thickness']['unit'] == 'nm'
        assert math.isfinite(row['thickness']['value']) and row['thickness']['value'] > 0
    fractions = [row['high_phase_fraction'] for row in first.provenance['windows']]
    assert all(0 < fraction <= .5 for fraction in fractions)
    assert any(fraction < .5 for fraction in fractions)  # Unbracketed stops may remain quarter-wave.
    assert any(row['source_nk_at_center']['high'][1] == .02 for row in first.provenance['windows'])
    assert request.frozen_material_snapshot == before.frozen_material_snapshot
    assert request.objectives == before.objectives
    # Production wrapper must place the new unscored family before generic HR.
    wrapped = next(global_search.iter_reflector_proposals(request))
    assert wrapped.provenance['family'] == 'narrow_rejection'
    assert wrapped.design == first.design


def test_only_normal_incidence_bracketed_mixed_objectives_use_family():
    request = normal_hint_request()
    pure_hr = replace(request, objectives=(request.objectives[1], request.objectives[3]))
    assert not priors.rejection_prior_applicable(pure_hr)
    assert list(priors.iter_rejection_proposals(pure_hr)) == []
    assert next(global_search.iter_reflector_proposals(pure_hr)).provenance['family'] != 'narrow_rejection'
    oblique = replace(request, objectives=tuple(replace(o, angles_deg=(0., 10.)) for o in request.objectives))
    assert not priors.rejection_prior_applicable(oblique)
    assert list(priors.iter_rejection_proposals(oblique)) == []
    one_pass = replace(request, objectives=(request.objectives[0], request.objectives[1]))
    assert not priors.rejection_prior_applicable(one_pass)
    assert list(priors.iter_rejection_proposals(one_pass)) == []


def test_identical_index_materials_are_skipped_without_fabricated_contrast():
    request = normal_hint_request()
    _service, _environment, rows = synthetic_service()
    low = next(row for row in rows if row['id'] == 'low')['optical_sets'][0]
    high = next(row for row in rows if row['id'] == 'high')['optical_sets'][0]
    high['n'] = deepcopy(low['n'])
    high['k'] = deepcopy(low['k'])
    frozen = synthesis.freeze_film_synthesis_request(AcceptanceService(rows),
        replace(request, frozen_material_snapshot=None))
    events: list[dict[str, Any]] = []
    assert list(priors.iter_rejection_proposals(frozen, report=events.append)) == []
    assert any('contrast' in str(event.get('reason')) for event in events)


@pytest.mark.parametrize('changes', [
    {'max_layers': 2}, {'max_total_thickness_nm': 1.},
    {'max_layer_thickness_nm': 1.}, {'minimum_nonzero_thickness_nm': 1000.},
])
def test_explicit_layer_and_thickness_limits_are_never_bypassed(changes):
    request = normal_hint_request()
    events: list[dict[str, Any]] = []
    assert list(priors.iter_rejection_proposals(replace(request, **changes), report=events.append)) == []
    assert any(event.get('family') == 'narrow_rejection' for event in events)


def test_limits_permit_only_valid_returned_rows_and_epochs_progress():
    request = normal_hint_request()
    first = next(priors.iter_rejection_proposals(request, epoch=0))
    later = next(priors.iter_rejection_proposals(request, epoch=1))
    assert later.provenance['epoch'] == 1
    assert all(later_window['pairs'] == initial_window['pairs'] + 1
               for initial_window, later_window in zip(first.provenance['windows'],
                                                        later.provenance['windows'], strict=True))
    values = [row['thickness']['value'] for row in first.design['front_layers']]
    bounded = replace(request, max_layers=len(values), max_total_thickness_nm=sum(values) + 1e-6,
                      max_layer_thickness_nm=max(values) + 1e-6,
                      minimum_nonzero_thickness_nm=min(values) - 1e-6)
    proposal = next(priors.iter_rejection_proposals(bounded))
    values = [row['thickness']['value'] for row in proposal.design['front_layers']]
    assert len(values) <= bounded.max_layers
    assert sum(values) <= bounded.max_total_thickness_nm
    assert min(values) >= bounded.minimum_nonzero_thickness_nm
    assert max(values) <= bounded.max_layer_thickness_nm


def test_cancellation_propagates_during_proposal_construction():
    request = normal_hint_request()
    calls = 0
    class StopNow(RuntimeError):
        pass
    def cancel_check():
        nonlocal calls
        calls += 1
        if calls == 5:
            raise StopNow('cancel sentinel')
    with pytest.raises(StopNow, match='cancel sentinel'):
        next(priors.iter_rejection_proposals(request, cancel_check=cancel_check))
    assert calls == 5


@pytest.mark.parametrize('selected', [('back',), ('front', 'back')])
def test_surface_lifting_preserves_actual_environment_and_unselected_rows(selected):
    request = normal_hint_request()
    environment = deepcopy(request.environment)
    environment['substrate']['sides'] = 'double'
    environment['first_surface'] = 'back'
    if selected == ('back',):
        environment['front_layers'] = [dict(id='fixed-front', material_id='low', optical_set_id='nk',
                                           thickness=dict(value=40., unit='nm'), note='fixed')]
    _service, _base, rows = synthetic_service()
    high = next(row for row in rows if row['id'] == 'high')['optical_sets'][0]
    high['k'] = [.02] * len(high['n'])
    service = AcceptanceService(rows)
    source = replace(request, environment=environment, selected_surfaces=selected,
                     frozen_material_snapshot=None)
    frozen = synthesis.freeze_film_synthesis_request(service, source)
    before = deepcopy(frozen)
    proposal = next(global_search.iter_reflector_proposals(frozen))
    assert proposal.provenance['family'] == 'narrow_rejection'
    assert proposal.provenance['selected_surfaces'] == selected
    assert frozen == before
    validate_candidate_environment(frozen, proposal.design)
    assert proposal.design['substrate'] == environment['substrate']
    assert proposal.design['first_surface'] == 'back'
    if selected == ('back',):
        assert proposal.design['front_layers'] == environment['front_layers']
    assert all(proposal.design[side + '_layers'] for side in selected)
    active = [row for side in selected for row in proposal.design[side + '_layers']]
    thicknesses = [row['thickness']['value'] for row in active]
    bounded = replace(frozen, max_layers=len(active), max_total_thickness_nm=sum(thicknesses) + 1e-5,
                      minimum_nonzero_thickness_nm=min(thicknesses) - 1e-6,
                      max_layer_thickness_nm=max(thicknesses) + 1e-6)
    lifted = next(global_search.iter_reflector_proposals(bounded))
    actual = [row['thickness']['value'] for side in selected for row in lifted.design[side + '_layers']]
    assert bounded.max_layers is not None
    assert len(actual) <= bounded.max_layers
    assert sum(actual) <= bounded.max_total_thickness_nm
    assert min(actual) >= bounded.minimum_nonzero_thickness_nm
    assert max(actual) <= bounded.max_layer_thickness_nm
    validate_candidate_environment(bounded, lifted.design)


def test_policy_fingerprint_covers_lifted_surface_but_not_unrelated_pure_hr(monkeypatch):
    request = normal_hint_request()
    env = deepcopy(request.environment)
    env['substrate']['sides'] = 'double'
    lifted = replace(request, environment=env, selected_surfaces=('back',))
    pure_hr = replace(request, objectives=(request.objectives[1], request.objectives[3]))
    before = [search_fingerprint(row) for row in (request, lifted, pure_hr)]
    monkeypatch.setattr(priors, 'REJECTION_PROPOSAL_POLICY', 'test_future_rejection_policy_v2')
    after = [search_fingerprint(row) for row in (request, lifted, pure_hr)]
    assert after[0] != before[0] and after[1] != before[1]
    assert after[2] == before[2]
