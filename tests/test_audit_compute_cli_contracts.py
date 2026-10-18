"""Synthetic CPU regressions for frozen-service and headless boundary parity."""
from dataclasses import replace
import hashlib
import io
import json
import signal
import sys
from threading import Event
from unittest.mock import Mock, patch

import numpy as np
import pytest

from coat_sim import algorithm_cli, film_performance_cli
from coat_sim.core.api import CoatService
from coat_sim.core.film_design_service import simulation_settings, simulate_frozen
from coat_sim.core.film_performance import FrozenFilmEvaluator
from coat_sim.core.film_optimization_job import build_job_payload, execute_film_optimization_job
from tests.film_performance_fixtures import fixture
from tests.test_film_optimization_forward_cache import _request


def resign(frozen):
    frozen['sha256'] = hashlib.sha256(json.dumps({k: v for k, v in frozen.items() if k != 'sha256'},
        sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


@pytest.mark.parametrize('thickness', [1000., 100000.])
@pytest.mark.parametrize('surface,port', [('front', 'exit_medium'), ('back', 'incident_medium')])
@pytest.mark.parametrize('use_k', [True, False])
def test_external_port_domain_parity(thickness, surface, port, use_k):
    service, design, frozen = fixture(double=True, substrate_nm=thickness, use_k=use_k)
    design[port] = {'kind': 'material', 'material_id': 'high', 'optical_set_id': 'optical'}
    frozen['design'] = design
    resign(frozen)
    settings = {'samples': 3, 'first_surface': surface}
    if use_k:
        with pytest.raises(ValueError, match='外部端口'):
            service.simulate(design, settings)
        with pytest.raises(ValueError, match='外部端口'):
            FrozenFilmEvaluator(frozen, settings)
    else:
        reference = service.simulate(design, settings)
        response = FrozenFilmEvaluator(frozen, settings).evaluate_thickness_batch([], np.empty((1, 0)))
        np.testing.assert_allclose(response['R'][0], reference['R'], atol=1e-12)


@pytest.mark.parametrize('material,optical', [('absent', 'absent'), ('high', 'absent'), ('high', 'optical')])
def test_inactive_back_qw_not_resolved(material, optical):
    service, design, frozen = fixture()
    design['reference_wavelength_nm'] = 1000  # Outside the synthetic tables.
    design['back_layers'] = [dict(id='inactive', material_id=material, optical_set_id=optical,
                                 thickness={'unit': 'qw', 'value': 1}, note='')]
    frozen['design'] = design
    resign(frozen)
    settings = {'samples': 3, 'extrapolation_policy': 'error'}
    reference = service.simulate(design, settings)
    evaluator = FrozenFilmEvaluator(frozen, settings)
    response = evaluator.evaluate_thickness_batch([], np.empty((1, 0)))
    np.testing.assert_allclose(response['R'][0], reference['R'], atol=1e-12)
    with pytest.raises(ValueError, match='variable IDs'):
        evaluator.evaluate_thickness_batch(['inactive'], [[1.]])


def test_normalization_single_point_roundtrip_and_replay():
    raw = {'wavelength_min_nm': 550, 'wavelength_max_nm': 550, 'samples': 1}
    normalized = simulation_settings(raw)
    assert simulation_settings(normalized) == normalized
    for invalid in ({'step_nm': 0}, {**raw, 'samples': 2, 'step_nm': 0},
                    {'wavelength_min_nm': 550, 'wavelength_max_nm': 550, 'step_nm': 0}):
        with pytest.raises(ValueError):
            simulation_settings(invalid)
    service, design, frozen = fixture()
    service.materials._records = lambda: frozen['materials']
    frozen = service.freeze_design(design, raw)
    response = FrozenFilmEvaluator(frozen).evaluate_thickness_batch([], np.empty((1, 0)))
    with patch('coat_sim.core.film_design_service.FilmDesignService', return_value=service):
        reference = simulate_frozen('/unused', frozen)
    np.testing.assert_allclose(response['R'][0], reference['R'])


@pytest.mark.parametrize('inactive', [False, True])
def test_serial_deferred_job_parity(inactive):
    service, design, frozen = fixture(double=not inactive, substrate_nm=100000.)
    if inactive:
        design['back_layers'] = [dict(id='inactive', material_id='absent', optical_set_id='absent',
                                     thickness={'unit': 'qw', 'value': 1}, note='')]
        frozen['design'] = design
        resign(frozen)
    request = _request(design, frozen, budget=4)
    objective = replace(request.objectives[0], wavelength_min_nm=550, wavelength_max_nm=550, wavelength_samples=1)
    request = replace(request, objectives=(objective,), solver='differential_evolution', batch_size=2,
                      validation_samples_multiplier=1)
    for mode in ('serial', 'deferred_batch'):
        result = execute_film_optimization_job(service, build_job_payload(replace(request, execution_mode=mode)), cancel_event=Event())
        assert result['state'] == 'completed', result


@pytest.mark.parametrize('snapshot', [None, [], 'bad', 3])
@pytest.mark.parametrize('explicit', [False, True])
def test_film_cli_malformed_snapshot(snapshot, explicit, monkeypatch, capsys):
    request = {'frozen_snapshot': snapshot, 'layer_ids': [], 'thickness_nm': [[]]}
    if explicit:
        request['settings'] = {'samples': 3}
    monkeypatch.setattr(sys, 'argv', ['film_performance_cli', 'evaluate'])
    monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(io.BytesIO(json.dumps(request).encode())))
    assert film_performance_cli.main() == 2
    assert json.loads(capsys.readouterr().out)['ok'] is False


def test_film_cli_single_point_defaults(monkeypatch, capsys):
    _, _, frozen = fixture()
    frozen['settings'] = simulation_settings({'wavelength_min_nm': 550, 'wavelength_max_nm': 550, 'samples': 1})
    resign(frozen)
    request = {'frozen_snapshot': frozen, 'layer_ids': [], 'thickness_nm': [[]]}
    monkeypatch.setattr(sys, 'argv', ['film_performance_cli', 'evaluate'])
    monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(io.BytesIO(json.dumps(request).encode())))
    assert film_performance_cli.main() == 0
    assert json.loads(capsys.readouterr().out)['result']['wavelength_nm'] == [550.]


@pytest.mark.parametrize('failure', ['exists', 'mkdir', 'status', 'service'])
def test_algorithm_setup_errors_restore_handler(tmp_path, capsys, failure):
    output = tmp_path / 'output'
    if failure == 'exists':
        output.mkdir()
    target = {'mkdir': 'coat_sim.algorithm_cli.Path.mkdir', 'status': 'coat_sim.algorithm_cli.replace_with_retry',
              'service': 'coat_sim.algorithm_cli.CoatService'}.get(failure)
    context = patch(target, side_effect=OSError('original setup failure')) if target else patch('coat_sim.algorithm_cli.time.monotonic', return_value=1)
    previous = signal.getsignal(signal.SIGINT)
    with context:
        assert algorithm_cli.main(['--root', str(tmp_path), '--process', 'recipe.cproc', '--output', str(output), '--detach']) == 2
    assert signal.getsignal(signal.SIGINT) == previous
    assert 'error' in json.loads(capsys.readouterr().out)


@pytest.mark.parametrize('from_settings', [False, True])
def test_algorithm_conflicting_time_controls_no_submit(tmp_path, capsys, from_settings):
    args = ['--root', str(tmp_path), '--process', 'recipe.cproc', '--time-step-s', '.3', '--detach']
    if from_settings:
        path = tmp_path / 'settings.json'
        path.write_text('{"time_steps":123}')
        args += ['--settings', str(path)]
    else:
        args += ['--time-steps', '123']
    with patch('coat_sim.scheduler.SchedulerClient') as client:
        assert algorithm_cli.main(args) == 2
        client.assert_not_called()
    assert 'mutually exclusive' in json.loads(capsys.readouterr().out)['error']


def test_algorithm_explicit_root_and_effective_timing(tmp_path, capsys):
    timing = {'time_steps': 4, 'time_step_s': .25, 'total_duration_s': 1.}
    for name in ('first', 'second'):
        root = tmp_path / name
        root.mkdir()
        service = CoatService(root)
        client = Mock()
        client.submit.return_value = {'job_id': name, 'state': 'queued'}
        with patch.object(algorithm_cli, 'CoatService', return_value=service) as factory, \
             patch.object(service, 'time_fields', return_value=timing), \
             patch('coat_sim.scheduler.freeze_spec', side_effect=lambda s, k, p: p) as freeze, \
             patch('coat_sim.scheduler.SchedulerClient', return_value=client) as scheduler:
            assert algorithm_cli.main(['--root', str(root), '--process', 'recipe.cproc', '--time-step-s', '.3', '--detach']) == 0
            factory.assert_called_once_with(str(root))
            scheduler.assert_called_once_with(root)
            assert freeze.call_args.args[2]['settings']['time_steps'] == 4
            assert json.loads(capsys.readouterr().out)['timing'] == timing


def test_algorithm_cross_root_rejected_before_scheduler(tmp_path, capsys):
    root = tmp_path / 'root'
    root.mkdir()
    outside = tmp_path / 'outside.cproc'
    outside.write_text('{}')
    with patch('coat_sim.scheduler.SchedulerClient') as client:
        assert algorithm_cli.main(['--root', str(root), '--process', str(outside), '--detach']) == 2
        client.assert_not_called()
    assert '文件路径超出允许目录' in json.loads(capsys.readouterr().out)['error']


def test_film_cli_missing_snapshot(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['film_performance_cli', 'evaluate'])
    monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(io.BytesIO(b'{}')))
    assert film_performance_cli.main() == 2
    assert json.loads(capsys.readouterr().out)['ok'] is False


@pytest.mark.parametrize('count', [None, 123])
def test_algorithm_count_control(tmp_path, capsys, count):
    service = CoatService(tmp_path)
    client = Mock()
    client.submit.return_value = {'job_id': 'count', 'state': 'queued'}
    args = ['--root', str(tmp_path), '--process', 'recipe.cproc', '--detach']
    if count is not None:
        args += ['--time-steps', str(count)]
    with patch.object(algorithm_cli, 'CoatService', return_value=service), \
         patch('coat_sim.scheduler.freeze_spec', side_effect=lambda s, k, p: p) as freeze, \
         patch('coat_sim.scheduler.SchedulerClient', return_value=client):
        assert algorithm_cli.main(args) == 0
        assert freeze.call_args.args[2]['settings']['time_steps'] == (120 if count is None else count)
    assert json.loads(capsys.readouterr().out)['timing'] is None


def test_nonintegral_requested_timestep_uses_shared_conversion():
    from coat_sim.core.process_simulator import process_timing
    recipe = {'schema_version': 7, 'name': 'Synthetic one-second recipe',
              'machine_file': 'machine.cmachine', 'substrate_file': 'substrate.csubstrate',
              'steps': [{'source_slot': 1, 'target_nm': 1., 'rate_nm_s': 1., 'target_mode': 'mean'}]}
    timing = process_timing(recipe, time_step_s=.3)
    assert timing['time_steps'] == 4
    assert timing['time_step_s'] == .25
