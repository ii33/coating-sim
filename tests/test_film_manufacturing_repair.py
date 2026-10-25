"""Bounded selected-row repair: analytic fixtures only, no optical dispatch."""
from copy import deepcopy
from dataclasses import replace

import pytest

from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest
from coat_sim.core.film_design import new_design, validate_design
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint, reduce_objective_responses
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_optimization_scoring import reduction_score
from coat_sim.core.film_optimization_structure import _StructureCancelled
from coat_sim.core import film_synthesis_manufacturing as repair

pytestmark = pytest.mark.algorithm


def fixture(mode='both', *, audited=True, same_optical=True, upper=1000., total=None):
    design = new_design(design_id='manufacturing-test')
    design['substrate'].update(material_id='bulk', optical_set_id='nk')
    def row(identifier, material, d, optical='nk'):
        return dict(id=identifier, material_id=material, optical_set_id=optical,
                    thickness=dict(value=d, unit='nm'), note='synthetic')
    design['front_layers'] = [row('left', 'high', 100.), row('defect', 'low', .5),
        row('right', 'high', 100., 'nk' if same_optical else 'other'), row('retained', 'low', 21.)]
    design = validate_design(design)
    env = deepcopy(design)
    env['front_layers'] = []
    objectives = (FilmOptimizationObjective(500., 600., 3,
        mean=FilmObjectiveConstraint(enabled=mode != 'pointwise', lower_bound=.96, upper_bound=None),
        pointwise=FilmObjectiveConstraint(enabled=mode != 'mean', lower_bound=.96, upper_bound=None),
        angles_deg=(0., 30.), polarization='unpolarized'),)
    materials = {'materials': [dict(id=m, optical_sets=[dict(id='nk'), dict(id='other')])
                              for m in ('bulk', 'high', 'low')]}
    synthesis = FilmSynthesisRequest(env, objectives, (), minimum_nonzero_thickness_nm=.01,
        max_layer_thickness_nm=upper, max_total_thickness_nm=total,
        validation_samples_multiplier=1, frozen_material_snapshot=materials,
        max_working_set_bytes=512 * 1024**2)
    calls = []
    def value(candidate, *, audited=False):
        ds = [r['thickness']['value'] for r in candidate['front_layers']]
        actual = .95 - .00001 * (sum(ds) - 180.)**2
        derivative = -.00002 * (sum(ds) - 180.)
        waves = [500., 525., 550., 575., 600.] if audited else [500., 550., 600.]
        responses = [dict(objective_index=0, angle_deg=angle, wavelength_nm=waves,
            actual_values=[actual] * len(waves),
            jacobian_values_by_layer_id={r['id']: [derivative] * len(waves) for r in candidate['front_layers']})
            for angle in (0., 30.)]
        reduced = reduce_objective_responses(objectives, responses,
            layer_ids=tuple(r['id'] for r in candidate['front_layers']), tolerance=synthesis.acceptable_normalized_error)
        score = reduction_score(reduced, synthesis.acceptable_normalized_error)
        if audited:
            dense = value(candidate)
            mean_rows = {r.angle_deg: r for r in dense['score']['errors'] if r.reduction == 'mean'}
            errors = tuple(mean_rows[r.angle_deg] if r.reduction == 'mean' else r for r in score['errors'])
            score = deepcopy(dense['score'])
            score['errors'] = errors
            score['condition_count'] = len(errors)
            score['audit_coverage'] = reduced.coverage
            score['audit'] = dict(dense_points=6, offset_points=4, unique_condition_points=10,
                physical_evaluations=2, wavelength_points=10, bracket_count=0, completed_extrema=0,
                continuous_certificate=False, rms_measure='fixed_dense_grid_only')
        return dict(design=deepcopy(candidate), score=score, responses=responses)
    def evaluate(candidate, *, jacobian, cancel_check):
        cancel_check()
        assert jacobian
        calls.append(('FJ', deepcopy(candidate)))
        return value(candidate)
    def audit(candidate, *, cancel_check):
        cancel_check()
        calls.append(('audit', deepcopy(candidate)))
        return value(candidate, audited=True)
    initial = value(design)
    source = repair.manufacturing_snapshot(synthesis, initial,
        audit_value=value(design, audited=True) if audited else None)
    request = repair.ManufacturingRepairRequest(synthesis,
        repair.manufacturing_source_fingerprint(synthesis, design), ('defect',), 25., 8, 12, 10.)
    return request, source, evaluate, audit, calls, value


def run(request, source, evaluate, audit, **kwargs):
    return repair.repair_manufacturing_pair(request, source, evaluate=evaluate, audit=audit,
        audit_working_set_bytes=lambda d: 1024, admit=lambda **kw: None, **kwargs)


@pytest.mark.parametrize('mode', ['mean', 'pointwise', 'both'])
def test_real_slsqp_analytic_pair_complete_evidence_and_bounded_calls(mode):
    request, source, evaluate, audit, calls, _ = fixture(mode)
    original = deepcopy(source)
    events = []
    archived = []
    result = run(request, source, evaluate, audit, progress=lambda *args: events.append(args),
        archive=lambda value, **kw: archived.append((deepcopy(value), kw)))
    assert result.status == 'complete'
    assert source == original
    assert len(result.routes) == 2
    for route in result.routes:
        assert route.status == 'audited'
        assert route.start_calls == 1 and route.refinement_calls <= 8 and route.audit_calls == 1
        assert route.completed_training_calls == route.start_calls + route.refinement_calls
        assert route.completed_audit_calls == 1
        assert route.start is not None and route.champion is not None
        assert route.champion.audit_score is not None
        assert 'responses' not in vars(route.champion)
        assert repair.complete_original_score(request.synthesis, route.champion.training_score)
        assert repair.complete_original_score(request.synthesis, route.champion.audit_score, independent_audit=True)
        for row in route.champion.design['front_layers']:
            assert row['thickness']['value'] >= route.lower_bounds_nm[row['id']]
    assert len(archived) == sum(r.completed_training_calls for r in result.routes)
    assert len(calls) == sum(r.start_calls + r.refinement_calls + r.audit_calls for r in result.routes)
    assert result.best_repaired is not None and not result.best_repaired.audit_score['nominal_pass']
    assert result.spectral_incumbent.audit_score['max_error'] <= source.audit_score['max_error']
    assert len([e for e in events if e[0] == 'manufacturing_repair_audit']) == 2


def test_geometry_explicit_defect_only_and_exact_reference_merge():
    request, source, *_ = fixture()
    a, b = repair.prepare_manufacturing_routes(request, source)
    assert [r['id'] for r in a.design['front_layers']] == ['left', 'defect', 'right', 'retained']
    assert [r['thickness']['value'] for r in a.design['front_layers']] == [100., 25., 100., 21.]
    assert a.lower_bounds_nm == dict(left=25., defect=25., right=25., retained=21.)
    assert [r['id'] for r in b.design['front_layers']] == ['left', 'retained']
    assert [r['thickness']['value'] for r in b.design['front_layers']] == [200., 21.]
    assert b.merged_ids == {'right': 'left'} and b.removed_ids == ('defect',)
    assert b.design['substrate'] == source.design['substrate']
    request, source, *_ = fixture(same_optical=False)
    b = repair.prepare_manufacturing_routes(request, source)[1]
    assert len(b.design['front_layers']) == 3 and not b.merged_ids


@pytest.mark.parametrize('changes', [dict(defect_layer_ids=()), dict(defect_layer_ids=('missing',)),
    dict(defect_layer_ids=('defect', 'defect')), dict(defect_layer_ids=['defect']),
    dict(reference_nm=0.), dict(reference_nm=float('nan')), dict(reference_nm=True),
    dict(reference_nm=.5), dict(source_fingerprint='stale'), dict(seconds_per_route=0),
    dict(refinement_evaluations_per_route=True), dict(max_iterations_per_route=0)])
def test_invalid_request_before_any_dispatch(changes):
    request, source, evaluate, audit, calls, _ = fixture()
    with pytest.raises(ValueError):
        run(replace(request, **changes), source, evaluate, audit)
    assert not calls


def test_fingerprint_binds_ids_material_content_and_geometry():
    request, source, evaluate, audit, calls, _ = fixture()
    for change in ('id', 'geometry', 'material'):
        design = deepcopy(source.design)
        synthesis = deepcopy(request.synthesis)
        if change == 'id':
            design['front_layers'][0]['id'] = 'renamed'
        elif change == 'geometry':
            design['front_layers'][0]['thickness']['value'] += 1.
        else:
            synthesis.frozen_material_snapshot['materials'][0]['note'] = 'different content'
        with pytest.raises(ValueError, match='fingerprint'):
            run(replace(request, synthesis=synthesis), replace(source, design=design), evaluate, audit)
    assert not calls


def test_upper_and_total_conflicts_skip_routes_without_clipping():
    request, source, evaluate, audit, calls, _ = fixture(upper=150., total=230.)
    result = run(request, source, evaluate, audit)
    assert [r.status for r in result.routes] == ['infeasible_total_thickness_limit', 'infeasible_upper_bound']
    assert result.best_repaired is None and result.spectral_incumbent == source
    assert not calls


def test_no_source_audit_no_audited_incumbent_promotion():
    request, source, evaluate, audit, _, _ = fixture(audited=False)
    result = run(request, source, evaluate, audit)
    assert result.best_repaired is not None
    assert result.spectral_incumbent == source and source.audit_score is None


def test_audit_failure_to_improve_keeps_original_spectral_incumbent():
    request, source, evaluate, _, _, value = fixture()
    def worse_audit(design, *, cancel_check):
        cancel_check()
        bad = value(source.design, audited=True)
        bad['design'] = deepcopy(design)
        return bad
    result = run(request, source, evaluate, worse_audit)
    assert result.best_repaired is not None
    assert result.spectral_incumbent == source


def test_cancellation_on_complete_trial_keeps_snapshot_no_audit_or_b():
    request, source, evaluate, audit, calls, _ = fixture()
    cancelled = False
    def check():
        if cancelled:
            raise _StructureCancelled()
    def progress(*args):
        nonlocal cancelled
        cancelled = True
    result = run(request, source, evaluate, audit, cancel_check=check, progress=progress)
    assert result.status == 'cancelled'
    assert len(calls) == 1 and result.routes[0].start is not None
    assert result.routes[0].completed_training_calls == 1 and result.routes[0].audit_calls == 0
    assert result.routes[1].start_calls == 0 and result.spectral_incumbent == source


def test_partial_inflight_cancel_charges_admission_but_has_no_false_snapshot():
    request, source, _, audit, calls, _ = fixture()
    def evaluate(design, **kwargs):
        calls.append(('partial_block', design))
        raise _StructureCancelled()
    result = run(request, source, evaluate, audit)
    assert result.status == 'cancelled'
    assert result.routes[0].start_calls == 1 and result.routes[0].completed_training_calls == 0
    assert result.routes[0].start is None and result.best_repaired is None
    assert len(calls) == 1


def test_observer_exception_propagates_not_swallowed():
    request, source, evaluate, audit, calls, _ = fixture()
    def progress(*args):
        raise RuntimeError('observer failed')
    with pytest.raises(RuntimeError, match='observer failed'):
        run(request, source, evaluate, audit, progress=progress)
    assert len(calls) == 1


def test_global_admission_budget_no_new_dispatch_no_audit():
    request, source, evaluate, audit, calls, _ = fixture()
    admitted = []
    def admit(**kwargs):
        admitted.append(kwargs)
        if len(admitted) == 2:
            raise repair.ManufacturingRepairStop('shared_budget')
    result = repair.repair_manufacturing_pair(request, source, evaluate=evaluate, audit=audit,
        audit_working_set_bytes=lambda d: 1024, admit=admit)
    assert result.status == 'shared_budget' and len(calls) == 1
    assert result.routes[0].start is not None and result.routes[0].audit_calls == 0


def test_combined_resource_guard_no_optical_dispatch():
    request, source, evaluate, audit, calls, _ = fixture()
    numerical = repair.manufacturing_refinement_working_set_bytes(request.synthesis, 4)
    cap = numerical + 10000
    request = replace(request, synthesis=replace(request.synthesis, max_working_set_bytes=cap))
    assert numerical < cap and numerical // 2 < cap
    result = run(request, source, evaluate, audit, external_retained_bytes=lambda: numerical // 2)
    assert result.status == 'resource_guard' and not calls
    assert result.estimated_peak_bytes > cap and result.spectral_incumbent == source


def test_resource_reservation_also_precedes_full_audit():
    request, source, evaluate, audit, calls, _ = fixture()
    result = repair.repair_manufacturing_pair(request, source, evaluate=evaluate, audit=audit,
        audit_working_set_bytes=lambda d: 1024**3, admit=lambda **kwargs: None)
    assert result.status == 'resource_guard'
    assert result.routes[0].champion is not None and result.routes[0].audit_calls == 0
    assert all(kind != 'audit' for kind, _ in calls)


def test_route_time_stop_does_not_audit_after_deadline_and_b_has_own_window():
    request, source, evaluate, audit, calls, _ = fixture()
    now = [0.]
    def delayed(design, **kwargs):
        value = evaluate(design, **kwargs)
        now[0] += request.seconds_per_route
        return value
    result = run(request, source, delayed, audit, clock=lambda: now[0])
    assert len(calls) == 2 and all(kind == 'FJ' for kind, _ in calls)
    assert result.routes[0].status == 'route_time_limit'
    assert result.status == 'time_limit'
    assert result.routes[1].start is not None and result.routes[1].audit_calls == 0


def test_evaluator_cannot_rebind_or_mutate_snapshot_design():
    request, source, evaluate, audit, calls, _ = fixture()
    def wrong(design, **kwargs):
        design['front_layers'][0]['id'] = 'wrong'
        return evaluate(design, **kwargs)
    with pytest.raises(ValueError, match='another design'):
        run(request, source, wrong, audit)
    assert source.design['front_layers'][0]['id'] == 'left' and len(calls) == 1


def test_snapshot_is_owned_and_strips_jacobians():
    request, source, _, _, _, value = fixture()
    incoming = value(source.design)
    snap = repair.manufacturing_snapshot(request.synthesis, incoming)
    incoming['score']['max_error'] = 999
    incoming['design']['front_layers'][0]['thickness']['value'] = 999
    assert snap.training_score['max_error'] < 1.
    assert snap.design['front_layers'][0]['thickness']['value'] == 100.
    assert not hasattr(snap, 'responses')


def test_unbound_input_evidence_is_rejected_without_optics():
    request, source, evaluate, audit, calls, _ = fixture()
    bad_score = deepcopy(source.training_score)
    bad_score['coverage'] = ()
    with pytest.raises(ValueError, match='complete original'):
        run(request, replace(source, training_score=bad_score), evaluate, audit)
    assert not calls


@pytest.mark.parametrize('observer', ['archive', 'progress'])
@pytest.mark.parametrize('error_class', [repair.SynthesisStageBudget, repair.SynthesisResourceGuard])
def test_observer_stop_named_exception_still_propagates(observer, error_class):
    request, source, evaluate, audit, calls, _ = fixture()
    def fail(*args, **kwargs):
        raise error_class('observer, not a budget')
    with pytest.raises(error_class, match='observer, not a budget'):
        run(request, source, evaluate, audit, **{observer: fail})
    assert len(calls) == 1


def test_callback_mutations_cannot_corrupt_operator_owned_evidence():
    request, source, evaluate, audit, _, _ = fixture()
    def mutate_progress(stage, snapshot, metadata):
        snapshot.design['front_layers'][0]['thickness']['value'] = 0.
        snapshot.training_score['max_error'] = 999.
    def mutate_archive(value, **kwargs):
        value['design']['front_layers'][0]['id'] = 'forged'
        value['score']['max_error'] = 999.
        assert all('jacobian_values_by_layer_id' not in r for r in value['responses'])
    def mutate_admission(**kwargs):
        kwargs['design']['front_layers'][0]['thickness']['value'] = 0.
    def mutate_estimate(design):
        design['front_layers'][0]['thickness']['value'] = 0.
        return 1024
    result = repair.repair_manufacturing_pair(request, source, evaluate=evaluate, audit=audit,
        admit=mutate_admission, audit_working_set_bytes=mutate_estimate,
        archive=mutate_archive, progress=mutate_progress)
    assert result.status == 'complete'
    for route in result.routes:
        assert route.champion is not None
        assert route.champion.training_score['max_error'] < 1.
        assert all(r['thickness']['value'] >= route.lower_bounds_nm[r['id']]
                   for r in route.champion.design['front_layers'])
    assert result.best_repaired is not None
    assert result.best_repaired.design['front_layers'][0]['thickness']['value'] > 0.


def test_snapshot_projection_drops_unrecognized_numerical_products():
    request, source, _, _, _, value = fixture()
    incoming = value(source.design)
    incoming['score']['debug_jacobian'] = [[1., 2.]]
    incoming['score']['responses'] = incoming['responses']
    snap = repair.manufacturing_snapshot(request.synthesis, incoming)
    assert 'debug_jacobian' not in snap.training_score and 'responses' not in snap.training_score


def test_grid_evidence_and_strict_flag_cannot_be_replaced_by_zero_error():
    request, source, _, _, _, value = fixture()
    incoming = value(source.design)
    incoming['score'].pop('nominal_pass')
    with pytest.raises(ValueError, match='complete original'):
        repair.manufacturing_snapshot(request.synthesis, incoming)
    incoming = value(source.design)
    incoming['responses'][0]['actual_values'][0] += .001
    with pytest.raises(ValueError, match='coverage differ'):
        repair.manufacturing_snapshot(request.synthesis, incoming)
    incoming = value(source.design)
    incoming['responses'].reverse()
    assert repair.manufacturing_snapshot(request.synthesis, incoming).training_score == source.training_score


def test_preallocation_guard_precedes_copy_or_canonical_reduction(monkeypatch):
    request, source, evaluate, audit, calls, _ = fixture()
    request = replace(request, synthesis=replace(request.synthesis, max_working_set_bytes=1))
    def forbidden(*args, **kwargs):
        raise AssertionError('allocation happened before guard')
    monkeypatch.setattr(repair, 'deepcopy', forbidden)
    monkeypatch.setattr(repair, 'complete_original_score', forbidden)
    result = run(request, source, evaluate, audit)
    assert result.status == 'resource_guard' and not result.source_validated
    assert result.source is source and not calls


def test_preallocation_guard_covers_untrusted_audit_grid_multiplier(monkeypatch):
    request, source, evaluate, audit, calls, _ = fixture()
    request = replace(request, synthesis=replace(request.synthesis, validation_samples_multiplier=10**10))
    monkeypatch.setattr(repair, 'complete_original_score', lambda *a, **k: pytest.fail('unsafe reduction'))
    result = run(request, source, evaluate, audit)
    assert result.status == 'resource_guard' and not result.source_validated and not calls


def test_dataclass_storage_includes_instance_dictionary():
    import sys
    from dataclasses import dataclass
    @dataclass(frozen=True)
    class Item:
        value: int
    item = Item(123)
    assert repair._owned_bytes(item) >= sys.getsizeof(item) + sys.getsizeof(vars(item)) + sys.getsizeof(123)
    assert repair._owned_bytes(item, item) == repair._owned_bytes(item)


@pytest.mark.parametrize('mode', ['mean', 'pointwise', 'both'])
def test_grid_dependent_proxy_reserves_multiple_full_public_scores(mode):
    request, source, _, _, _, _ = fixture()
    objective = replace(request.synthesis.objectives[0], wavelength_samples=1000,
        angles_deg=(0.,), mean=FilmObjectiveConstraint(enabled=mode != 'pointwise', mode='value',
            target_value=.96, lower_bound=None, upper_bound=None),
        pointwise=FilmObjectiveConstraint(enabled=mode != 'mean', mode='value',
            target_value=.96, lower_bound=None, upper_bound=None))
    synthesis = replace(request.synthesis, objectives=(objective,))
    waves = [500. + 100. * i / 999 for i in range(1000)]
    owners = []
    for owner in range(6):
        responses = [dict(objective_index=0, angle_deg=0., wavelength_nm=list(waves),
            actual_values=[.94 + 1e-8 * (i + owner) for i in range(1000)],
            jacobian_values_by_layer_id={'one': [.1 + 1e-8 * i for i in range(1000)]})]
        reduced = reduce_objective_responses((objective,), responses, layer_ids=('one',))
        owners.append(dict(design=source.design, responses=responses, score=reduction_score(reduced, .001)))
    variable_reservation = repair.manufacturing_refinement_working_set_bytes(synthesis, 1) - 128 * 1024**2
    assert variable_reservation > repair._owned_bytes(*owners)


def test_joint_original_grid_and_audit_nominal_gate_precedes_repaired_ranking():
    passed = dict(nominal_pass=True, max_error=0., weighted_rms_error=0.)
    failed = dict(nominal_pass=False, max_error=0., weighted_rms_error=0.)
    both = repair.ManufacturingSnapshot({}, passed, passed)
    audit_only = repair.ManufacturingSnapshot({}, failed, passed)
    train_only = repair.ManufacturingSnapshot({}, passed, failed)
    unknown = repair.ManufacturingSnapshot({}, passed)
    assert both.targets_met and not audit_only.targets_met and not train_only.targets_met and not unknown.targets_met
    assert repair._audited_key(both) < repair._audited_key(audit_only)
    assert repair._audited_key(both) < repair._audited_key(train_only)


def test_preexisting_cancellation_precedes_large_evidence_walk(monkeypatch):
    request, source, evaluate, audit, calls, _ = fixture()
    def check():
        raise _StructureCancelled()
    monkeypatch.setattr(repair, '_owned_bytes', lambda *args: pytest.fail('walk after cancellation'))
    result = run(request, source, evaluate, audit, cancel_check=check)
    assert result.status == 'cancelled' and not result.source_validated
    assert not calls and result.estimated_peak_bytes == 0
