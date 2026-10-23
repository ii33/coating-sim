"""Typed sensitivity queue dispatch fails closed without IPC or asset files."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from threading import Event
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest

from coat_sim.core import film_sensitivity_workflow_jobs as jobs
from coat_sim.scheduler import freeze_spec
from coat_sim.scheduler.daemon import Scheduler
from coat_sim.scheduler.jobs import JobSpec, canonical
from coat_sim.scheduler.result_capability import (
    is_best_candidate_result,
    is_film_optimization_result,
    is_sensitivity_result,
    job_has_viewable_result,
)
from coat_sim.ui import compute_queue as queue
from tests.test_film_sensitivity_workflow_jobs import problem

pytestmark = pytest.mark.contract

_EXECUTORS = ('film_sensitivity', *jobs.WORKFLOW_KINDS)
_TERMINAL = ('succeeded', 'failed', 'cancelled')
_RUN_ID = 'a' * 32


def _generic_result() -> dict[str, Any]:
    return {
        'run_id': _RUN_ID,
        'asset': {'run_id': _RUN_ID},
        'partial_result': {
            'schema_version': 1, 'kind': 'best_candidate', 'best_so_far': True,
            'candidate_verified': True, 'quality': {'metric': 'loss', 'value': .01},
        },
        'result_artifacts': [
            {'role': 'bundle', 'artifact_id': 'b' * 64, 'sha256': 'c' * 64,
             'bytes': 1, 'name': 'result.coatasset'},
            {'role': 'result', 'artifact_id': 'd' * 64, 'sha256': 'e' * 64,
             'bytes': 1, 'name': 'result.json'},
        ],
    }


def _malformed_result(executor: str) -> dict[str, Any]:
    # A valid generic candidate/artifact must not repair a broken typed body.
    return {**_generic_result(), 'schema_version': 1,
            'kind': 'coat_sim.' + executor + '_result', 'is_final': True,
            'state': 'completed'}


def _row(kind: str, result: dict[str, Any], state: str = 'succeeded') -> dict[str, Any]:
    return {'job_id': 'typed-queue-test', 'kind': kind, 'state': state, 'result': result}


class _MemoryStore:
    def __init__(self, row: dict[str, Any], root: Path = Path('.')):
        self.root = root
        self.row = deepcopy(row)
        self.generic_calls: list[str] = []

    def read_job(self, _job_id: str) -> dict[str, Any]:
        return deepcopy(self.row)

    def result_asset_path(self, _job_id: str) -> Path:
        self.generic_calls.append('result_asset_path')
        return Path('memory-only.coatasset')


class _MemoryService:
    def __init__(self):
        self.generic_calls: list[Path] = []

    def load_simulation_asset(self, path: Path) -> dict[str, Any]:
        self.generic_calls.append(path)
        return {'run_id': _RUN_ID, 'unchanged_generic_asset': True}


@pytest.fixture
def asset_database_spy(monkeypatch):
    """Observe first-pass generic run collection without creating a database."""
    calls: list[str] = []

    class Connection:
        def execute(self, query, _parameters=()):
            calls.append(query)
            return []

        def close(self):
            calls.append('close')

    monkeypatch.setattr(Path, 'is_file', lambda path: path.name == 'simulation_history.sqlite3')
    monkeypatch.setattr(queue.sqlite3, 'connect', lambda *_args, **_kwargs: Connection())
    return calls


@pytest.mark.parametrize('executor', _EXECUTORS)
def test_malformed_typed_body_cannot_advertise_generic_best_candidate(executor):
    result = _malformed_result(executor)
    assert not is_sensitivity_result(result)
    assert not is_best_candidate_result(result)


@pytest.mark.parametrize('executor', _EXECUTORS)
@pytest.mark.parametrize('state', _TERMINAL)
@pytest.mark.parametrize('bait', ['candidate_and_bundle', 'registered_result_only'])
def test_malformed_typed_body_cannot_advertise_generic_viewability(executor, state, bait):
    result = _malformed_result(executor)
    if bait == 'registered_result_only':
        result.pop('run_id')
        result.pop('asset')
        result.pop('partial_result')
        result['result_artifacts'] = [result['result_artifacts'][1]]
    assert not job_has_viewable_result(_row(executor, result, state))


@pytest.mark.parametrize('executor', _EXECUTORS)
@pytest.mark.parametrize('state', _TERMINAL)
@pytest.mark.parametrize('explicit_asset', [False, True])
def test_malformed_typed_body_never_loads_generic_asset(executor, state, explicit_asset):
    store = _MemoryStore(_row(executor, _malformed_result(executor), state))
    service = _MemoryService()
    with pytest.raises(ValueError, match=r'(?i)sensitivity|typed|workflow|invalid'):
        queue._read_queue_result(service, store, store.row['job_id'],
                                 'memory-only.coatasset' if explicit_asset else None)
    assert store.generic_calls == []
    assert service.generic_calls == []


@pytest.mark.parametrize('executor', _EXECUTORS)
@pytest.mark.parametrize('state', _TERMINAL)
def test_malformed_typed_availability_skips_both_generic_passes(executor, state, asset_database_spy):
    row = _row(executor, _malformed_result(executor), state)
    queue._annotate_result_availability(Path('.'), [row])
    assert row.get('resultAvailable') is False
    assert row.get('resultUnavailableReason')
    assert asset_database_spy == []


@pytest.mark.parametrize('executor', _EXECUTORS)
@pytest.mark.parametrize('other_kind', ['simulation', 'film_optimization', 'other_typed'])
def test_raw_typed_identity_conflict_never_uses_generic_route(executor, other_kind, asset_database_spy):
    if other_kind == 'other_typed':
        other_kind = next(kind for kind in _EXECUTORS if kind != executor)
    row = _row(other_kind, _malformed_result(executor))
    assert not job_has_viewable_result(row)
    service, store = _MemoryService(), _MemoryStore(row)
    with pytest.raises(ValueError, match=r'(?i)sensitivity|typed|workflow|belong|kind'):
        queue._read_queue_result(service, store, row['job_id'])
    queue._annotate_result_availability(Path('.'), [row])
    assert row.get('resultAvailable') is False
    assert row.get('resultUnavailableReason')
    assert asset_database_spy == []
    assert store.generic_calls == [] and service.generic_calls == []


def _optimization_shape() -> dict[str, Any]:
    """Existing cheap optimization capability, without numerical replay mocks."""
    quality = {'metric': 'max_normalized_error', 'value': .1, 'direction': 'minimize',
               'weighted_max_normalized_error': .1, 'weighted_rms_error': .1,
               'targets_met': False}
    return {
        'schema_version': 1, 'kind': 'coat_sim.film_optimization_result',
        'request': {'schema_version': 1, 'kind': 'coat_sim.film_optimization_request',
                    'design_revision': 7, 'frozen_material_snapshot': {'sha256': 'f' * 64}},
        'candidate': {'schema_version': 1, 'kind': 'coat_sim.film_optimization_candidate',
                      'design_revision': 7, 'design': {}, 'thickness_nm': {},
                      'quality': quality, 'per_condition_errors': [],
                      'iterations': 0, 'evaluations': 1},
        'partial_result': {'schema_version': 1, 'kind': 'best_candidate',
                           'best_so_far': True, 'candidate_verified': True,
                           'quality': {'metric': quality['metric'], 'value': quality['value']},
                           'artifact_kind': 'coat_sim.film_optimization_candidate',
                           'artifact_schema_version': 1},
    }


@pytest.mark.parametrize('executor', _EXECUTORS)
@pytest.mark.parametrize('raw_kind', ['generic', 'film_optimization'])
def test_typed_row_does_not_accept_generic_or_other_film_result(executor, raw_kind, monkeypatch,
                                                               asset_database_spy):
    result = _generic_result() if raw_kind == 'generic' else _optimization_shape()
    if raw_kind == 'film_optimization':
        assert is_film_optimization_result(result)
    row = _row(executor, result)
    assert not job_has_viewable_result(row)
    optimization_calls: list[dict[str, Any]] = []

    def load_optimization(_service, payload):
        optimization_calls.append(payload)
        return deepcopy(payload)

    monkeypatch.setattr(queue, 'validate_film_optimization_result', load_optimization)
    service, store = _MemoryService(), _MemoryStore(row)
    with pytest.raises(ValueError, match=r'(?i)sensitivity|typed|workflow|belong|kind'):
        queue._read_queue_result(service, store, row['job_id'])
    queue._annotate_result_availability(Path('.'), [row])
    assert row.get('resultAvailable') is False
    assert row.get('resultUnavailableReason')
    assert asset_database_spy == []
    assert optimization_calls == []
    assert store.generic_calls == [] and service.generic_calls == []


def test_ordinary_simulation_keeps_original_generic_asset_route():
    result = _generic_result()
    row = _row('process', result)
    store, service = _MemoryStore(row), _MemoryService()
    assert job_has_viewable_result(row)
    assert is_best_candidate_result(result)
    loaded, asset = queue._read_queue_result(service, store, row['job_id'])
    assert loaded == {'run_id': _RUN_ID, 'unchanged_generic_asset': True}
    assert asset == 'memory-only.coatasset'
    assert store.generic_calls == ['result_asset_path']
    assert service.generic_calls == [Path('memory-only.coatasset')]


@pytest.fixture(scope='module', params=_EXECUTORS)
def real_typed_job(request):
    executor = request.param
    adapter: ModuleType
    if executor == 'film_sensitivity':
        from coat_sim.core import film_sensitivity_jobs as adapter
        from tests.test_film_sensitivity import problem as legacy_problem
        service, workflow_request = legacy_problem()
    else:
        adapter = jobs
        service, workflow_request = problem(executor)
    frozen = jobs.freeze_workflow_request(service, workflow_request)
    spec = JobSpec.parse_frozen(freeze_spec(None, executor, {'request': frozen}))
    payload = adapter.execute_job(spec.parameters, Event(), lambda _item: None,
                                  'in-memory-only', lambda *_args: None)
    jobs.validate_workflow_result(payload, expected_request=frozen, replay=True)
    assert is_sensitivity_result(payload)
    return executor, spec, payload


def _bind_spec(tmp_path, real_typed_job):
    executor, spec, payload = real_typed_job
    row = _row(executor, deepcopy(payload))
    row['input_digest'] = spec.fingerprint
    directory = tmp_path / 'artifacts/runtime/scheduler/jobs' / row['job_id']
    directory.mkdir(parents=True)
    (directory / 'spec.json').write_text(canonical(spec.to_spec().to_dict()), encoding='utf-8')
    return row


def test_real_typed_reader_preserves_frozen_request_and_replay(tmp_path, real_typed_job):
    row = _bind_spec(tmp_path, real_typed_job)
    service, store = _MemoryService(), _MemoryStore(row, tmp_path)
    assert job_has_viewable_result(row)
    queue._annotate_result_availability(tmp_path, [row])
    assert row.get('resultAvailable') is True
    loaded, asset = queue._read_queue_result(service, store, row['job_id'])
    assert loaded == row['result'] and loaded is not row['result']
    assert asset == ''
    assert loaded['frozen_request'] == row['result']['frozen_request']
    assert store.generic_calls == [] and service.generic_calls == []


def test_real_typed_reader_rejects_another_queue_job_id(tmp_path, real_typed_job):
    row = _bind_spec(tmp_path, real_typed_job)
    requested_id = row['job_id']
    row['job_id'] = 'different-queue-job'
    with pytest.raises(ValueError, match=r'(?i)identity|another|bound|job'):
        queue._read_queue_result(_MemoryService(), _MemoryStore(row, tmp_path), requested_id)


@pytest.mark.parametrize('state', ['queued', 'running', 'cancelling'])
def test_real_typed_reader_rejects_nonterminal_queue_row(tmp_path, real_typed_job, state):
    row = _bind_spec(tmp_path, real_typed_job)
    row['state'] = state
    assert not job_has_viewable_result(row)
    with pytest.raises(ValueError, match=r'(?i)terminal|complete|state'):
        queue._read_queue_result(_MemoryService(), _MemoryStore(row, tmp_path), row['job_id'])
    queue._annotate_result_availability(tmp_path, [row])
    assert row.get('resultAvailable') is False
    assert row.get('resultUnavailableReason')


@pytest.mark.parametrize('mutation', ['digest', 'frozen_request', 'analysis_replay'])
def test_real_typed_reader_rejects_input_binding_or_replay_tampering(tmp_path, real_typed_job,
                                                                     mutation):
    row = _bind_spec(tmp_path, real_typed_job)
    result = row['result']
    if mutation == 'digest':
        row['input_digest'] = '0' * 64
    elif mutation == 'frozen_request':
        result['frozen_request']['design_revision'] += 1
    elif row['kind'] == 'film_thickness_error_table':
        result['analysis']['conditions'][0]['nominal_value'] += .25
    elif row['kind'] == 'film_ideal_sensitivity':
        result['best_analysis']['objective_value'] += .25
    else:
        result['best_analysis']['linear_sensitivity_norm'] += .25
    service, store = _MemoryService(), _MemoryStore(row, tmp_path)
    with pytest.raises(ValueError):
        queue._read_queue_result(service, store, row['job_id'])
    assert store.generic_calls == [] and service.generic_calls == []


def test_real_typed_result_cannot_cross_executor_ownership(tmp_path, real_typed_job):
    row = _bind_spec(tmp_path, real_typed_job)
    row['kind'] = next(kind for kind in _EXECUTORS if kind != row['kind'])
    assert not job_has_viewable_result(row)
    service, store = _MemoryService(), _MemoryStore(row, tmp_path)
    with pytest.raises(ValueError, match=r'(?i)belong|kind|executor|workflow'):
        queue._read_queue_result(service, store, row['job_id'])
    queue._annotate_result_availability(tmp_path, [row])
    assert row.get('resultAvailable') is False
    assert row.get('resultUnavailableReason')
    assert store.generic_calls == [] and service.generic_calls == []


@pytest.fixture(scope='module', params=['cancelled_table', 'failed_initial_nominal'])
def readonly_terminal_report(request):
    if request.param == 'cancelled_table':
        executor = 'film_thickness_error_table'
        service, workflow_request = problem(executor)
    else:
        from tests.test_film_ideal_sensitivity import problem as ideal_problem
        executor = 'film_ideal_sensitivity'
        service, workflow_request = ideal_problem(upper=0.)
    frozen = jobs.freeze_workflow_request(service, workflow_request)
    spec = JobSpec.parse_frozen(freeze_spec(None, executor, {'request': frozen}))
    payload = jobs.execute_job(spec.parameters, Event(), lambda _item: None,
                               'in-memory-only', lambda *_args: None)
    if request.param == 'cancelled_table':
        payload = jobs.terminal_reply(payload, 'cancelled')
    else:
        assert payload['state'] == 'failed'
        assert payload['termination_reason'] == 'initial_nominal_infeasible'
    jobs.validate_workflow_result(payload, expected_request=frozen, replay=True)
    assert is_sensitivity_result(payload)
    assert not is_best_candidate_result(payload)
    return executor, spec, payload


@pytest.mark.parametrize('bound', [True, False])
def test_scheduler_result_returns_bound_readonly_report_without_best_candidate(readonly_terminal_report,
                                                                                bound):
    executor, _spec, payload = readonly_terminal_report
    row = _row(executor, payload, payload['state'])
    binding_checks: list[tuple[str, dict[str, Any]]] = []

    def binding(job_id, result):
        binding_checks.append((job_id, result))
        return bound

    scheduler = cast(Scheduler, SimpleNamespace(
        memory_status=lambda **_arguments: deepcopy(row), _bound_film_reply=binding))
    if bound:
        assert Scheduler._dispatch(scheduler, 'result', {'job_id': row['job_id']}) == payload
    else:
        with pytest.raises(ValueError, match=r'(?i)bound|binding|identity|invalid|unavailable'):
            Scheduler._dispatch(scheduler, 'result', {'job_id': row['job_id']})
    assert binding_checks == [(row['job_id'], payload)]
