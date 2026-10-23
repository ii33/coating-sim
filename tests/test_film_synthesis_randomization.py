"""Bounded random-start contracts; no long optical search or live hardware."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
from threading import Event

import numpy as np
import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_synthesis_randomization as randomization
from coat_sim.core.film_synthesis_archive import physical_design_hash
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def design_with_layers():
    _, request = setup_problem()
    design = deepcopy(request.environment)
    design['front_layers'] = [dict(id=f'layer-{i}', material_id=name, optical_set_id='nk',
        thickness=dict(value=d, unit='nm'), note='fixture')
        for i, (name, d) in enumerate((('low', 55.), ('high', 80.), ('low', 0.)))]
    return design


@pytest.mark.parametrize('seed', [True, False, -1, 2**32, .5, 1., '0'])
def test_seed_domain_rejects_non_uint32(seed):
    _, request = setup_problem()
    with pytest.raises(ValueError, match='structure_search_seed'):
        synthesis._validated(replace(request, randomize_template_search=True, structure_search_seed=seed))


def test_seed_zero_and_auto_are_distinct_and_entropy_is_only_at_freeze(monkeypatch):
    service, request = setup_problem()
    calls = []
    def entropy(bits):
        calls.append(bits)
        return 123456789
    monkeypatch.setattr('secrets.randbits', entropy)
    automatic = replace(request, randomize_template_search=True, structure_search_seed=None)
    assert synthesis._validated(automatic).structure_search_seed is None
    assert calls == []
    frozen = synthesis.freeze_film_synthesis_request(service, automatic)
    assert calls == [32] and frozen.structure_search_seed == 123456789
    replay = synthesis.freeze_film_synthesis_request(None, frozen)
    assert replay.structure_search_seed == frozen.structure_search_seed and calls == [32]
    explicit = synthesis.freeze_film_synthesis_request(service,
        replace(request, randomize_template_search=True, structure_search_seed=0))
    assert explicit.structure_search_seed == 0 and calls == [32]
    with pytest.raises(ValueError, match='structure_search_seed'):
        synthesis._validated(replace(request, structure_search_seed=None))


def test_stationary_ar1_formula_is_exact_and_fixed_zero_materials_are_preserved(monkeypatch):
    class KnownNormals:
        def __init__(self):
            self.calls = 0
        def standard_normal(self, n):
            self.calls += 1
            return np.array([1., -1.]) if self.calls == 1 else np.array([.5, -.5])
    monkeypatch.setattr(randomization, 'template_random_stream', lambda *a, **k: KnownNormals())
    design = design_with_layers()
    original = deepcopy(design)
    perturbed, record = randomization.randomized_template_start(design, root_seed=0, stage='test')
    assert perturbed is not None
    z = np.array([1., .85 - np.sqrt(1 - .85**2)])
    delta = .12 * z + .06 * np.array([.5, -.5])
    assert record['log_deltas'] == pytest.approx(delta)
    assert record['start_thickness_nm'] == pytest.approx([55 * np.exp(delta[0]), 80 * np.exp(delta[1]), 0])
    assert design == original and perturbed['front_layers'][2] == original['front_layers'][2]
    for before, after in zip(design['front_layers'], perturbed['front_layers'], strict=True):
        assert (before['material_id'], before['optical_set_id'], before['id']) == (
            after['material_id'], after['optical_set_id'], after['id'])
    fixed, info = randomization.randomized_template_start(design, root_seed=0, stage='test',
        minimum_nm=55., maximum_nm=55.)
    assert fixed is None and info['attempts'] == 0


def test_mask_legal_bounds_and_zero_never_becomes_positive():
    design = design_with_layers()
    result, record = randomization.randomized_template_start(design, root_seed=7,
        stage='test', optimizable_mask=(False, True, True), minimum_nm=1.,
        maximum_nm=150., total_maximum_nm=210.)
    assert result is not None and record['variable_indices'] == [1]
    assert result['front_layers'][0] == design['front_layers'][0]
    assert result['front_layers'][2] == design['front_layers'][2]
    assert len(result['front_layers']) == len(design['front_layers'])
    assert sum(row['thickness']['value'] for row in result['front_layers']) <= 210.


def test_same_seed_identity_order_and_parallel_reconstruct_and_other_seed_differs():
    first = design_with_layers()
    second = deepcopy(first)
    second['front_layers'][0]['thickness']['value'] = 40.
    def start(pair):
        seed, design = pair
        result, record = randomization.randomized_template_start(design, root_seed=seed, stage='test')
        assert result is not None
        return physical_design_hash(result), record['start_thickness_nm']
    expected = list(map(start, [(0, first), (0, second)]))
    with ThreadPoolExecutor(2) as pool:
        reversed_results = list(pool.map(start, [(0, second), (0, first)]))
    assert reversed_results[::-1] == expected
    renamed = deepcopy(first)
    for index, row in enumerate(renamed['front_layers']):
        row.update(id=f'temporary-worker-id-{index}', note='new label')
    assert start((0, renamed)) == expected[0]
    assert start((1, first)) != expected[0]


@pytest.mark.parametrize('duplicate', [False, True])
def test_three_invalid_or_duplicate_draws_retain_base_without_merit_calls(monkeypatch, duplicate):
    design = design_with_layers()
    calls = []
    if duplicate:
        class ZeroNormals:
            def standard_normal(self, n):
                return np.zeros(n)
        monkeypatch.setattr(randomization, 'template_random_stream', lambda *a, **k: ZeroNormals())
        def feasible(trial):
            calls.append(trial)
            return True
    else:
        def feasible(trial):
            calls.append(trial)
            return False
    result, record = randomization.randomized_template_start(design, root_seed=0,
        stage='test', feasible=feasible)
    assert result is None and len(calls) == 3 and record['attempts'] == 3
    assert record['reason'] == 'three_invalid_or_duplicate_draws_base_retained'
    assert design['front_layers'][0]['thickness']['value'] == 55.


def _bounded_pipeline(monkeypatch, *, enabled, workers=1, budget=16, stop_after=2, seed=0):
    service, request = setup_problem(upper=0., low=550., high=550., samples=1)
    initial = design_with_layers()
    initial['front_layers'].pop()  # Real seed designs require strictly positive layers.
    request = replace(request, search_strategy='hybrid', randomize_template_search=enabled,
        structure_search_seed=seed, parallel_batch_size=1, cpu_workers=workers,
        max_evaluations=budget, max_cycles=1, seed_designs=(initial,),
        max_working_set_bytes=None)
    cancel = Event()
    proposals = []
    physical = []
    original = synthesis.evaluate_frozen_structure_candidate
    def evaluation(*args, **kwargs):
        physical.append(deepcopy(args[1]))
        return original(*args, **kwargs)
    monkeypatch.setattr(synthesis, 'evaluate_frozen_structure_candidate', evaluation)
    def progress(event):
        if event.stage == 'structure_proposal':
            proposals.append(json.loads(event.message))
            if len(proposals) >= stop_after:
                cancel.set()
    result = synthesis.synthesize_film_design(service, request, cancel_event=cancel,
        progress_callback=progress)
    return result, proposals, physical


def test_off_preserves_default_exact_order_and_never_creates_new_stream(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('off must not create a template random stream')
    monkeypatch.setattr(randomization, 'template_random_stream', forbidden)
    with monkeypatch.context() as patch:
        first, first_proposals, first_physical = _bounded_pipeline(patch, enabled=False)
    with monkeypatch.context() as patch:
        second, second_proposals, second_physical = _bounded_pipeline(patch, enabled=False)
    assert first_proposals == second_proposals == [dict(family='user_seed', seed_index=0),
        dict(family='homogeneous', quarter_fraction=.5)]
    assert first_physical == second_physical
    assert 'template_randomization' not in first.performance['search']
    assert first.state == second.state == 'cancelled'


def test_hybrid_batch_one_scores_base_and_extra_and_replays_across_worker_settings(monkeypatch):
    results = []
    for workers in (1, 2):
        with monkeypatch.context() as patch:
            result, proposals, physical = _bounded_pipeline(patch, enabled=True, workers=workers)
        metadata = result.performance['search']['template_randomization']
        assert metadata['effective_seed'] == 0 and metadata['bit_generator'] == 'PCG64'
        assert metadata['algorithm_version'] == randomization.ALGORITHM_VERSION
        assert metadata['numpy_version'] == np.__version__
        assert proposals[0] == dict(family='user_seed', seed_index=0)
        assert 'template_randomization' in proposals[1]
        assert len(physical) == 3  # baseline + actual BASE F + actual extra F.
        assert physical[1]['front_layers'][0]['thickness']['value'] == 55.
        assert physical[2]['front_layers'][0]['thickness']['value'] != 55.
        assert result.state == 'cancelled' and result.evaluations == 3
        assert result.validation_evaluations == 0 and result.performance['cleanup_complete']
        results.append(metadata['templates'][0]['start_thickness_nm'])
    assert results[0] == results[1]


def test_no_extra_quota_draws_zero_and_retains_base(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('no extra quota must not construct a stream')
    monkeypatch.setattr(randomization, 'template_random_stream', forbidden)
    result, proposals, physical = _bounded_pipeline(monkeypatch, enabled=True, budget=6, stop_after=1)
    record = result.performance['search']['template_randomization']['templates'][0]
    assert record['reason'] == 'insufficient_extra_candidate_or_physical_budget_base_retained'
    assert record['attempts'] == record['reserved_candidate_slots'] == 0
    assert len(physical) == 2 and len(proposals) == 1


def test_helper_cancel_propagates_before_rng(monkeypatch):
    calls = []
    monkeypatch.setattr(randomization, 'template_random_stream', lambda *a, **k: calls.append('rng'))
    def cancelled():
        raise synthesis._StructureCancelled()
    with pytest.raises(synthesis._StructureCancelled):
        randomization.randomized_template_start(design_with_layers(), root_seed=0,
            stage='test', cancel_check=cancelled)
    assert calls == []


def test_legacy_classic_template_scores_base_then_actual_extra(monkeypatch):
    from coat_sim.core import film_synthesis_global as global_search
    from coat_sim.core import film_synthesis_hr_priors as hr_priors
    service, request = setup_problem(upper=1., low=550., high=550., samples=1)
    request = replace(request, objectives=(replace(request.objectives[0], lower_bound=.999999),),
        randomize_template_search=True, structure_search_seed=0,
        max_evaluations=8, max_cycles=1, cpu_workers=2, max_working_set_bytes=None)
    design = design_with_layers()
    design['front_layers'].pop()
    monkeypatch.setattr(hr_priors, 'pure_hr_prior_eligible', lambda request: False)
    monkeypatch.setattr(global_search, 'iter_reflector_proposals', lambda *a, **k: iter([
        global_search.StructuralProposal(design, dict(family='periodic', high=('high', 'nk')))]))
    cancel = Event()
    messages = []
    def progress(event):
        if event.stage == 'structure_proposal':
            messages.append(json.loads(event.message))
            if len(messages) == 2:
                cancel.set()
    result = synthesis.synthesize_film_design(service, request, cancel_event=cancel, progress_callback=progress)
    assert result.state == 'cancelled' and result.evaluations == 3
    assert messages[0]['family'] == messages[1]['family'] == 'periodic'
    assert 'template_randomization' not in messages[0]
    assert messages[1]['template_randomization']['start_thickness_nm'][0] != 55.
    assert result.performance['search']['template_randomization']['templates'][0]['stage'] == 'legacy_structure_template'


def test_pipeline_three_invalid_draws_scores_only_base_with_visible_reason(monkeypatch):
    class InvalidNormals:
        def standard_normal(self, n):
            return np.full(n, np.nan)
    monkeypatch.setattr(randomization, 'template_random_stream', lambda *a, **k: InvalidNormals())
    result, proposals, physical = _bounded_pipeline(monkeypatch, enabled=True, stop_after=1)
    record = result.performance['search']['template_randomization']['templates'][0]
    assert record['attempts'] == 3 and record['reason'] == 'three_invalid_or_duplicate_draws_base_retained'
    assert len(proposals) == 1 and len(physical) == 2
    assert physical[1]['front_layers'][0]['thickness']['value'] == 55.


def test_legacy_pending_template_uses_resumed_slice_extra_quota(monkeypatch):
    from coat_sim.core import film_synthesis_global as global_search
    from coat_sim.core import film_synthesis_multispectral_minimax as minimax
    from coat_sim.core import film_synthesis_multispectral_priors as priors
    from coat_sim.core import film_synthesis_refinement as refinement
    from tests.test_film_synthesis_multispectral_scheduling import mixed_request, install_streams
    request = replace(mixed_request(max_working_set_bytes=None),
        randomize_template_search=True, structure_search_seed=0)
    old, _, generated, factories, closed = install_streams(monkeypatch, request, old_count=2)
    monkeypatch.setattr(priors, 'iter_multispectral_hr_proposals', lambda *a, **k: iter(()))
    monkeypatch.setattr(synthesis, 'STRUCTURAL_PREFIX_POINT_LAYER_QUANTUM', 6)
    monkeypatch.setattr(minimax, 'refine_multispectral_minimax',
        lambda frozen, value, **kwargs: (value, {'method': 'schedule isolation'}))
    monkeypatch.setattr(refinement, 'refine_synthesis_candidate',
        lambda frozen, value, **kwargs: (value, {'method': 'schedule isolation'}))
    monkeypatch.setattr(synthesis, '_site_records', lambda *args, **kwargs: ())
    original_feasible = synthesis._feasible
    monkeypatch.setattr(synthesis, '_feasible', lambda frozen, design:
        original_feasible(frozen, design) and not any(
            row['id'].startswith('growth-') for row in design['front_layers']))
    class EmptyPopulation:
        def __init__(self, initial, *args, **kwargs):
            self.dimensions = len(initial)
            self.generations = 0
        def proposals(self):
            return np.empty((0, self.dimensions))
        def accept(self, losses):
            assert len(losses) == 0
            self.generations += 1
    monkeypatch.setattr(global_search, 'DifferentialEvolutionProposals', EmptyPopulation)
    cancel = Event()
    scored, schedules = [], []
    def observe(event):
        if event.stage == 'structure_proposal':
            scored.append(json.loads(event.message))
            if len(scored) == 2:
                cancel.set()
        elif event.stage in ('structure_scan_yield', 'structure_scan_complete'):
            schedules.append(json.loads(event.message))
    result = synthesis.synthesize_film_design(None, request, cancel_event=cancel, progress_callback=observe)
    assert result.state == 'cancelled' and len(scored) == 2
    assert scored[0]['schedule_fixture'] == scored[1]['schedule_fixture'] == 'classic-0'
    assert 'template_randomization' not in scored[0]
    assert scored[1]['template_randomization']['parent_physical_hash'] == physical_design_hash(old[0].design)
    assert schedules[0]['admitted_candidates'] == 1 and schedules[0]['yielded']
    assert generated == [0] and factories == [0] and closed == [0]
