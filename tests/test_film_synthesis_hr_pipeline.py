"""Default HR prefix keeps parent budgets, cancellation and typed requests."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from threading import Event

import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_synthesis_hr_minimax as minimax
from coat_sim.core.film_synthesis_io import film_synthesis_request_from_payload
from tests.test_film_synthesis_hr_priors import hr_request

pytestmark = pytest.mark.algorithm


def pipeline_request(**changes):
    request = hr_request(low=450., high=750., angles=(0., 20.),
                         max_layers=8, absorbing=False)
    return replace(request, max_evaluations=20, max_cycles=1,
                   validation_samples_multiplier=2, **changes)


@pytest.mark.parametrize("stop_mode", ["cancel", "time_limit"])
def test_default_prefix_stop_keeps_completed_training_and_no_post_stop_audit(monkeypatch, stop_mode):
    request = pipeline_request()
    clock = [0.]
    if stop_mode == "time_limit":
        request = replace(request, max_seconds=1.)
        monkeypatch.setattr(synthesis, "monotonic", lambda: clock[0])
    cancel = Event()
    completed = []
    seen = []

    def stop_after_completed_trial(request, initial, *, evaluate, archive,
                                   cancel_check, progress, **options):
        seen.append(options)
        completed.append(initial['design'])
        trial = deepcopy(initial['design'])
        trial['front_layers'][0]['thickness']['value'] *= 1.001
        value = evaluate(trial, jacobian=True)
        completed.append(value['design'])
        archive(value, audit_eligible=False)
        if stop_mode == "cancel":
            cancel.set()
        else:
            clock[0] = 2.
        cancel_check()
        raise AssertionError('parent cancellation must propagate')

    monkeypatch.setattr(minimax, 'refine_hr_minimax', stop_after_completed_trial)
    result = synthesis.synthesize_film_design(None, request, cancel_event=cancel)
    assert seen == [{'max_iterations': 180, 'soft_method': 'L-BFGS-B',
                     'coordinate_scale_nm': 100.0}]
    assert result.state == ('cancelled' if stop_mode == 'cancel' else 'not_found')
    assert result.termination_reason == ('cancelled' if stop_mode == 'cancel' else 'time_limit')
    assert not result.targets_met
    assert result.evaluations == 3 and result.validation_evaluations == 0
    assert result.candidate_design is not None and result.training_score is not None
    assert len(result.candidate_design['front_layers']) == 8
    assert result.training_score['design'] in completed
    assert result.validation_score is None
    assert result.candidate_provenance['training_complete']
    flag = 'frozen_at_cancel' if stop_mode == 'cancel' else 'frozen_at_time_limit'
    assert result.candidate_provenance[flag]
    assert result.performance['cleanup_complete']
    assert result.performance['training_physical_evaluations'] > 0
    assert any(row.stage == 'hr_minimax_prior' for row in result.trace)


def test_default_prefix_cannot_spend_beyond_parent_evaluation_admission(monkeypatch):
    request = replace(pipeline_request(), max_evaluations=5)
    calls = []

    def exhaust(request, initial, *, evaluate, archive, cancel_check, **options):
        for index in range(20):
            cancel_check()
            trial = deepcopy(initial['design'])
            trial['front_layers'][0]['thickness']['value'] *= 1 + .001 * (index + 1)
            value = evaluate(trial, jacobian=True)
            calls.append(value)
            archive(value, audit_eligible=False)
        raise AssertionError('finite parent budget must stop this stage')

    monkeypatch.setattr(minimax, 'refine_hr_minimax', exhaust)
    result = synthesis.synthesize_film_design(None, request)
    assert len(calls) == 2  # baseline, prior, two trials, reserved final audit
    assert result.evaluations == 5 and result.validation_evaluations == 1
    assert result.state != 'failed' and not result.targets_met
    assert result.performance['cleanup_complete']
    assert any(row.stage == 'hr_minimax_prior' for row in result.trace)


def test_public_case_has_no_hidden_seed_and_uses_normal_typed_request_contract(monkeypatch):
    from coat_sim.core.film_synthesis_jobs import (
        build_job_payload, canonical_parameters, deserialize_job_payload,
    )
    from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
    from coat_sim.core import compute_resources
    from coat_sim.ui.film_synthesis_quick_adapter import (
        build_synthesis_request, synthesis_base_request,
    )
    path = Path(__file__).resolve().parents[1] / (
        'benchmarks/film_synthesis/20261021/hr-minimax-tio2-franta-v1/request.json')
    payload = json.loads(path.read_text(encoding='utf8'))
    _, original, _ = film_synthesis_request_from_payload(payload)
    assert original.max_working_set_bytes == 0 and original.resource_guard_resolution is None
    available_bytes = 8 * 1024**3
    monkeypatch.setattr(compute_resources, "available_memory_bytes", lambda: available_bytes)
    expected_resolution = dict(version=1, mode="auto", requested_bytes=0,
        effective_bytes=available_bytes // 2, available_bytes=available_bytes,
        basis="half_available_memory_at_freeze")
    assert original.seed_designs == () and not payload['physical_structures']
    assert not original.environment['front_layers'] and not original.environment['back_layers']
    assert original.frozen_material_snapshot is not None
    assert not original.frozen_material_snapshot['design']['front_layers']
    options = {key: getattr(original, key) for key in (
        'max_layers', 'max_evaluations', 'max_seconds', 'minimum_nonzero_thickness_nm',
        'max_layer_thickness_nm', 'max_total_thickness_nm')}
    rebuilt = build_synthesis_request(synthesis_base_request(original),
        original.candidate_materials, options, use_current_seed=False,
        assume_unknown_k_zero=True, cpu_workers=1)
    rebuilt = freeze_film_synthesis_request(None, replace(rebuilt,
        frozen_material_snapshot=original.frozen_material_snapshot))
    # Every original request field remains binding. The only addition is the
    # canonical derived AUTO policy, not a seed or a changed physical budget.
    assert rebuilt == replace(original, resource_guard_resolution=expected_resolution)
    assert rebuilt.resource_guard_resolution == expected_resolution
    job = build_job_payload(canonical_parameters(None, {'payload': payload}))
    _, queued, _ = deserialize_job_payload(job)
    assert queued == replace(original, resource_guard_resolution=expected_resolution)
    assert queued.resource_guard_resolution == expected_resolution and not queued.seed_designs
    assert all(o.wavelength_samples == 301 for o in queued.objectives)
    assert {(o.polarization, a) for o in queued.objectives for a in o.angles_deg} == {
        ('s', 0.), ('s', 30.), ('p', 0.), ('p', 30.)}
    materials = queued.frozen_material_snapshot['materials']
    films = {r.material_id for r in queued.candidate_materials}
    assert all(o['k'] is not None for m in materials if m['id'] in films
               for o in m['optical_sets'])
    unknown = [(m['id'], o['id']) for m in materials for o in m['optical_sets']
               if o.get('k') is None and o.get('k_data') is None]
    assert unknown == [(queued.environment['substrate']['material_id'],
                        queued.environment['substrate']['optical_set_id'])]
    assert queued.environment['substrate']['sides'] == 'single'
    assert queued.environment['use_k'] and queued.environment['assume_lossless']
