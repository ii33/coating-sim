from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import os
import subprocess
import sys
from threading import Event

import numpy as np
import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_synthesis_forward as forward
from coat_sim.core.film_performance import FrozenFilmEvaluator, PerformanceOptions
from coat_sim.core.film_optimization_structure import _StructureCancelled, evaluate_frozen_structure_candidate
from coat_sim.core.film_synthesis_parallel import EvaluationTask
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def _trial(request, *, thickness=100.):
    design = deepcopy(dict(request.environment))
    design['front_layers'] = [dict(id='gpu-layer', material_id='low', optical_set_id='nk',
        thickness=dict(value=thickness, unit='nm'), note='real GPU forward oracle')]
    return design


def test_gpu_request_validation_and_freeze_do_not_import_torch():
    code = """
import sys
from dataclasses import replace
from tests.test_film_auto_synthesis import setup_problem
from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
service, request = setup_problem()
frozen = freeze_film_synthesis_request(service, replace(request, forward_backend='cuda'))
assert frozen.forward_device is None
assert 'torch' not in sys.modules
print('cold-freeze-no-torch')
"""
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert 'cold-freeze-no-torch' in result.stdout


@pytest.mark.parametrize('backend', ['auto', 'gpu', '', True])
def test_invalid_backend_is_explicit(backend):
    service, request = setup_problem()
    with pytest.raises(ValueError, match='forward_backend'):
        synthesis.freeze_film_synthesis_request(service, replace(request, forward_backend=backend))


def test_binding_validation_is_pure_and_nullable_uuid_is_valid():
    binding = dict(runtime='cuda', index=0, uuid=None, name='test identity only',
        torch_version='test', dtype='float64/complex128')
    assert forward.validate_forward_device(binding, 'cuda') == binding
    with pytest.raises(ValueError, match='runtime/dtype'):
        forward.validate_forward_device(binding, 'xpu')
    with pytest.raises(ValueError, match='CPU'):
        forward.validate_forward_device(binding, 'cpu')


def test_cpu_batch_spectra_use_the_exact_existing_score_aggregation():
    service, request = setup_problem(samples=31)
    request = synthesis.freeze_film_synthesis_request(service, request)
    design = _trial(request)
    payload = forward.SynthesisForward(request, synthesis._proxy(request))._snapshot(design)
    objective = request.objectives[0]
    settings = dict(wavelength_min_nm=objective.wavelength_min_nm,
        wavelength_max_nm=objective.wavelength_max_nm, samples=objective.wavelength_samples,
        angle_deg=objective.angles_deg[0], polarization=objective.polarization)
    evaluator = FrozenFilmEvaluator(payload, settings, options=PerformanceOptions(backend='cpu'))
    batch = evaluator.evaluate_thickness_batch(('gpu-layer',), np.array([[100.], [110.]]))
    value = evaluate_frozen_structure_candidate(synthesis._proxy(request), design,
        forward_responses=[{'wavelength_nm': batch['wavelength_nm'],
            **{m: batch[m][0] for m in ('R', 'T', 'A')}}])
    reference = evaluate_frozen_structure_candidate(synthesis._proxy(request), design)
    assert value['score']['max_error'] == pytest.approx(reference['score']['max_error'], abs=2e-12)
    assert value['score']['weighted_rms_error'] == pytest.approx(reference['score']['weighted_rms_error'], abs=2e-12)
    assert len(value['score']['errors']) == 31
    print('CPU shared aggregation', value['score']['max_error'], reference['score']['max_error'])
    with pytest.raises(ValueError, match='derivatives'):
        evaluate_frozen_structure_candidate(synthesis._proxy(request), design,
            with_jacobian=True, forward_responses=[])


def test_strict_sources_precede_the_legacy_zero_default_sampler():
    service, request = setup_problem()
    # This intentionally bypasses synthesis freeze to exercise the GPU boundary itself.
    service.materials.records['low']['optical_sets'][0]['k'] = [0.] * 3
    request = synthesis.freeze_film_synthesis_request(service, request)
    assert request.frozen_material_snapshot is not None
    snapshot = deepcopy(request.frozen_material_snapshot)
    next(m for m in snapshot['materials'] if m['id'] == 'low')['optical_sets'][0]['k'] = None
    request = replace(request, frozen_material_snapshot=snapshot)
    engine = forward.SynthesisForward(request, synthesis._proxy(request))
    with pytest.raises(ValueError, match='material=low.*optical_set=nk.*unknown k'):
        engine._strict_sources(_trial(request), np.array([550.]))
    assumed = deepcopy(dict(request.environment))
    assumed['assume_lossless'] = True
    request = replace(request, environment=assumed)
    engine = forward.SynthesisForward(request, synthesis._proxy(request))
    engine._strict_sources(_trial(request), np.array([550.]))
    with pytest.raises(ValueError, match='wavelength=800'):
        engine._strict_sources(_trial(request), np.array([800.]))


def test_bounded_groups_finite_admission_and_cancel_never_consume_tail(monkeypatch):
    service, request = setup_problem()
    request = synthesis.freeze_film_synthesis_request(service, replace(request, max_evaluations=None))
    cancel = Event()
    engine = forward.SynthesisForward(request, synthesis._proxy(request), cancel)
    seen = []
    # Unit scheduling stub only: no claim that these empty spectra ran on a GPU.
    def spectra(tasks):
        seen.append([t.design['front_layers'][0]['thickness']['value'] for t in tasks])
        return []
    monkeypatch.setattr(engine, '_spectra', spectra)
    tasks = [EvaluationTask(_trial(request, thickness=d)) for d in (90., 100., 110.)]
    with engine.batch(tasks) as values:
        _, first = next(values)
        first.consumed = True
        cancel.set()
        with pytest.raises(_StructureCancelled):
            next(values)
    assert seen == [[90., 100., 110.]] and engine.discarded == 2
    cancel.clear()
    request = replace(request, max_evaluations=3)
    engine = forward.SynthesisForward(request, synthesis._proxy(request), cancel)
    seen.clear()
    monkeypatch.setattr(engine, '_spectra', spectra)
    with engine.batch(tasks, admit=lambda: False) as values:
        task, outcome = next(values)
        assert task is tasks[0] and outcome is None
    assert seen == []


@pytest.mark.skipif(not os.environ.get('COAT_SIM_TEST_SYNTHESIS_GPU'), reason='explicit supported runtime required')
def test_real_gpu_complete_synthesis_and_cpu_audit():
    backend = os.environ['COAT_SIM_TEST_SYNTHESIS_GPU']
    service, request = setup_problem(upper=.001, samples=9, materials=('low',))
    request = replace(request, forward_backend=backend, max_cycles=1, max_evaluations=None)
    result = synthesis.synthesize_film_design(service, request)
    assert result.targets_met, result.termination_reason
    assert result.candidate_design is not None and result.candidate_design['front_layers']
    assert result.validation_complete and result.validation_score is not None
    work = result.performance['forward']
    assert work['actual_backend'] == backend and work['candidates_completed'] > 1
    assert work['verification_cpu_calls'] > 0 and work['verification_gpu_calls'] > 0
    assert work['closed'] and result.performance['cleanup_complete']
    print('actual GPU complete synthesis', json.dumps(work), result.validation_score['max_error'])


@pytest.mark.parametrize('prebound', [False, True])
def test_unavailable_explicit_gpu_fails_instead_of_searching(monkeypatch, prebound):
    service, request = setup_problem()
    def unavailable(*args, **kwargs):
        raise ValueError('controlled unavailable runtime')
    monkeypatch.setattr(forward, 'resolve_forward_device', unavailable)
    binding = (dict(runtime='cuda', index=0, uuid=None, name='test binding',
        torch_version='test', dtype='float64/complex128') if prebound else None)
    result = synthesis.synthesize_film_design(service, replace(request, forward_backend='cuda', forward_device=binding))
    assert result.state == 'failed' and result.cycles == 0
    assert 'Explicit synthesis cuda forward unavailable' in result.termination_reason
    assert result.evaluations == 1 and result.failed_evaluations == 1
    assert result.performance['forward']['errors']
    assert result.performance['forward']['device_binding'] == ('prebound_unverified' if prebound else 'runtime_default')


@pytest.mark.skipif(not os.environ.get('COAT_SIM_TEST_SYNTHESIS_GPU'), reason='explicit supported runtime required')
@pytest.mark.parametrize('boundary', ['after_gate', 'cached_entry'])
def test_real_gpu_cancel_entry_never_reuses_previous_work(boundary):
    backend = os.environ['COAT_SIM_TEST_SYNTHESIS_GPU']
    service, request = setup_problem(samples=9)
    request = synthesis.freeze_film_synthesis_request(service, replace(request, forward_backend=backend))
    engine = forward.SynthesisForward(request, synthesis._proxy(request))
    task = EvaluationTask(request.environment)
    before = None
    if boundary == 'cached_entry':
        engine.single(task)
        before = engine.diagnostics()

    class BoundaryCancellation:
        def is_set(self):
            if boundary == 'cached_entry':
                caller = sys._getframe(1)
                return (caller.f_code.co_name == '_cancelled' and caller.f_back is not None
                        and caller.f_back.f_code.co_name == '_evaluate')
            for evaluator in engine.cache.values():
                gpu = evaluator._batch._gpu if evaluator._batch is not None else None
                if gpu is not None and gpu.verified:
                    return True
            return False

    engine.cancel = BoundaryCancellation()
    try:
        with pytest.raises(_StructureCancelled):
            engine.single(task)
    finally:
        engine.close()
    work = engine.diagnostics()
    assert work['objective_spectral_calls'] is None and work['wavelength_points'] is None
    if before is None:
        assert work['known_objective_spectral_calls'] == 0
        assert work['known_wavelength_points'] == 0
        assert work['verification_cpu_calls'] == work['verification_gpu_calls'] == 2
    else:
        for key in ('known_objective_spectral_calls', 'known_wavelength_points',
                    'verification_cpu_calls', 'verification_gpu_calls'):
            assert work[key] == before[key]
    print('actual cancellation epoch', boundary, work)
