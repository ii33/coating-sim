"""Synthetic n/k contract: transmission stopband hints never change scoring."""
from copy import deepcopy
from dataclasses import replace
from itertools import islice

import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_synthesis_proposal_objectives as proposal_objectives
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import FilmOptimizationObjective, FilmOptimizationRequest
from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
from coat_sim.core.film_synthesis_continuation import search_fingerprint
from coat_sim.ui.film_synthesis_quick_adapter import build_synthesis_request
from tools.run_film_synthesis_acceptance import AcceptanceService, synthetic_service

pytestmark = pytest.mark.algorithm


def synthetic_reference_request(*, max_evaluations=40):
    # Analytic constant n/k controls, NOT actual TiO2, SiO2 or measured data.
    _service, environment, rows = synthetic_service()
    for row in rows:
        if row['id'] == 'high':
            optical = row['optical_sets'][0]
            optical['k'] = [.02] * len(optical['n'])
            optical['notes'] = 'Synthetic absorbing test fixture, not measured material data.'
    service = AcceptanceService(rows)
    bands = ((400., 500., '>', .8), (510., 550., '<', .01),
             (560., 700., '>', .8), (1040., 1080., '<', .01))
    objectives = tuple(FilmOptimizationObjective(lo, hi, 3, angles_deg=(0., 20.),
        polarization='s', metric='T', weight=1., normalization_scale=1.,
        pointwise=(FilmObjectiveConstraint(lower_bound=bound, upper_bound=None, strict_lower=True)
                   if operator == '>' else
                   FilmObjectiveConstraint(lower_bound=None, upper_bound=bound, strict_upper=True)))
        for lo, hi, operator, bound in bands)
    base = FilmOptimizationRequest(environment, 1, (), objectives, acceptable_normalized_error=0.,
                                   validation_samples_multiplier=2)
    request = build_synthesis_request(base, (FilmNeedleMaterialReference('low', 'nk'),
        FilmNeedleMaterialReference('high', 'nk')), {'max_evaluations': max_evaluations,
        'max_layers': 48, 'enable_thickness_reduction': False}, cpu_workers=1,
        assume_unknown_k_zero=False)
    return synthesis.freeze_film_synthesis_request(service, request)


def test_private_hints_leave_all_original_objectives_and_known_k_untouched():
    request = synthetic_reference_request()
    original = deepcopy(request)
    hinted, hints = proposal_objectives.reflective_proposal_request(request)
    assert request == original and hinted is not request
    assert [objective.metric for objective in request.objectives] == ['T'] * 4
    assert [objective.metric for objective in hinted.objectives] == ['T', 'R', 'T', 'R']
    assert hinted.objectives[0] == request.objectives[0]
    assert hinted.objectives[2] == request.objectives[2]
    assert [item['objective_index'] for item in hints] == [1, 3]
    assert all(item['acceptance_equivalence'] is False for item in hints)
    for index in (1, 3):
        source, hint = request.objectives[index], hinted.objectives[index]
        assert (source.metric, source.pointwise.lower_bound, source.pointwise.upper_bound,
                source.pointwise.strict_lower, source.pointwise.strict_upper) == ('T', None, .01, False, True)
        assert hint.pointwise.lower_bound == pytest.approx(.99) and hint.pointwise.upper_bound is None
        assert hint.pointwise.strict_lower is True and hint.pointwise.strict_upper is False
        assert (hint.angles_deg, hint.polarization, hint.wavelength_min_nm, hint.wavelength_max_nm,
                hint.wavelength_samples, hint.weight, hint.normalization_scale) == (
                    source.angles_deg, source.polarization, source.wavelength_min_nm,
                    source.wavelength_max_nm, source.wavelength_samples, source.weight,
                    source.normalization_scale)
        assert source.mean.enabled is False and hint.mean.enabled is False
    assert hinted.frozen_material_snapshot == request.frozen_material_snapshot
    assert hinted.frozen_material_snapshot is not None
    high = next(row for row in hinted.frozen_material_snapshot['materials'] if row['id'] == 'high')
    assert all(value == .02 for value in high['optical_sets'][0]['k'])
    with_mean = replace(request, objectives=tuple(
        replace(row, mean=FilmObjectiveConstraint(lower_bound=.1, upper_bound=.9)) if index == 1 else row
        for index, row in enumerate(request.objectives)))
    with_mean_before = deepcopy(with_mean)
    private, _ = proposal_objectives.reflective_proposal_request(with_mean)
    assert with_mean == with_mean_before and with_mean.objectives[1].mean.enabled
    assert not private.objectives[1].mean.enabled  # Private proposal only; original criterion remains binding.


def test_unmatched_objectives_are_not_mapped():
    request = synthetic_reference_request()
    floor = request.objectives[0]
    cap = request.objectives[1]
    cases = (
        replace(cap, metric='R'),
        replace(cap, pointwise=replace(cap.pointwise, enabled=False),
                mean=FilmObjectiveConstraint(lower_bound=None, upper_bound=.01)),
        replace(cap, pointwise=FilmObjectiveConstraint(mode='value', target_value=.01,
                                                       lower_bound=None, upper_bound=None)),
        replace(cap, weight=0.),
        replace(cap, pointwise=replace(cap.pointwise, upper_bound=.51)),
        replace(cap, pointwise=replace(cap.pointwise, lower_bound=.001)),
        floor,
    )
    original = replace(request, objectives=cases)
    hinted, hints = proposal_objectives.reflective_proposal_request(original)
    assert hinted is original and hints == () and original.objectives == cases


def test_three_original_proposal_families_transition_from_empty_to_nonempty():
    from coat_sim.core import film_synthesis_global as global_search
    from coat_sim.core import film_synthesis_multispectral_priors as multispectral
    request = synthetic_reference_request()
    hinted, hints = proposal_objectives.reflective_proposal_request(request)
    assert hints and request.frozen_material_snapshot == hinted.frozen_material_snapshot
    for factory in (global_search.iter_reflector_proposals, global_search.iter_bandpass_proposals,
                    multispectral.iter_multispectral_hr_proposals):
        assert list(islice(factory(request), 1)) == []
        assert len(list(islice(factory(hinted), 1))) == 1, factory.__name__


def test_search_checkpoint_fingerprint_includes_policy_only_for_applicable_hybrid(monkeypatch):
    request = synthetic_reference_request()
    r_only = replace(request, objectives=tuple(replace(row, metric='R') for row in request.objectives))
    legacy = replace(request, search_strategy='legacy')
    original = (search_fingerprint(request), search_fingerprint(r_only), search_fingerprint(legacy))
    monkeypatch.setattr(proposal_objectives, 'PROPOSAL_OBJECTIVE_POLICY', 'test_future_policy_v2')
    changed = (search_fingerprint(request), search_fingerprint(r_only), search_fingerprint(legacy))
    assert changed[0] != original[0]
    assert changed[1:] == original[1:]


def test_real_hybrid_proposals_use_hints_but_physics_and_local_refinement_use_original(monkeypatch):
    from coat_sim.core import film_synthesis_global as global_search
    from coat_sim.core import film_synthesis_multispectral_priors as multispectral
    from coat_sim.core import film_synthesis_refinement as refinement

    request = synthetic_reference_request(max_evaluations=40)
    original = deepcopy(request)
    observed_proposals, observed_scoring, observed_local = [], [], []
    original_generator = multispectral.iter_multispectral_hr_proposals
    def proposal_probe(source, **kwargs):
        observed_proposals.append(source)
        yield from original_generator(source, **kwargs)
    monkeypatch.setattr(multispectral, 'iter_multispectral_hr_proposals', proposal_probe)
    original_score = synthesis.evaluate_frozen_structure_candidate
    def score_probe(source, *args, **kwargs):
        observed_scoring.append((source.base_request.objectives, kwargs.get('with_jacobian', False)))
        return original_score(source, *args, **kwargs)
    monkeypatch.setattr(synthesis, 'evaluate_frozen_structure_candidate', score_probe)
    original_refine = refinement.refine_synthesis_explorer
    def local_probe(source, *args, **kwargs):
        observed_local.append(source.objectives)
        return original_refine(source, *args, **kwargs)
    monkeypatch.setattr(refinement, 'refine_synthesis_explorer', local_probe)
    # Suppress one unrelated proposal family only to keep the integration probe
    # inexpensive; the actual hybrid scorer and local optimizer remain active.
    monkeypatch.setattr(global_search, 'iter_reflector_proposals', lambda *_args, **_kwargs: iter(()))
    result = synthesis.synthesize_film_design(None, request)
    assert request == original and result.frozen_request.objectives == request.objectives
    assert observed_proposals and [o.metric for o in observed_proposals[0].objectives] == ['T', 'R', 'T', 'R']
    assert observed_scoring and all(rows == request.objectives for rows, _ in observed_scoring)
    assert any(with_jacobian for _, with_jacobian in observed_scoring)
    assert observed_local and all(rows == request.objectives for rows in observed_local)
    details = result.performance['search']['hybrid']['proposal_objective_hints']
    assert len(details) == 2 and all(item['acceptance_equivalence'] is False for item in details)
    archive = result.performance['search']['design_archive']['entries']
    tagged = [entry['provenance']['proposal_objective_hints'] for entry in archive
              if 'proposal_objective_hints' in entry['provenance']]
    assert tagged and all(item['acceptance_equivalence'] is False for group in tagged for item in group)
