"""Real small physics through typed jobs/CLI/planners, without IPC endpoints."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import io
import json
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest

from coat_sim.core import film_sensitivity_workflow_jobs as jobs
from coat_sim.core import compute_estimation
from coat_sim.core.capabilities import CapabilityError
from coat_sim.scheduler import freeze_spec
from coat_sim.scheduler.jobs import JobSpec
from coat_sim.scheduler.result_capability import job_has_viewable_result
from coat_sim.scheduler.worker import _completed_film_cancel_reply, _film_reply_matches_payload

pytestmark = pytest.mark.algorithm


def problem(kind):
    if kind == 'film_ideal_sensitivity':
        from tests.test_film_ideal_sensitivity import problem as ideal_problem
        return ideal_problem(max_evaluations=100, max_iterations=15)
    from tests.test_film_thickness_error_table import problem as table_problem
    return table_problem(include_finite_bias=True)


@pytest.mark.parametrize('kind', jobs.WORKFLOW_KINDS)
def test_real_typed_job_freeze_binding_checkpoint_cancel_and_replay(kind):
    service, request = problem(kind)
    frozen = jobs.freeze_workflow_request(service, request)
    spec = JobSpec.parse_frozen(freeze_spec(None, kind, {'request': frozen}))
    assert spec.executor == kind and not spec.inputs
    assert spec.parameters['payload']['schema_version'] == 1
    assert spec.parameters['payload']['request']['schema_version'] == (3 if kind == 'film_ideal_sensitivity' else 2)
    assert jobs.workflow_request_to_payload(jobs.deserialize_job_payload(spec.parameters['payload'])) == jobs.workflow_request_to_payload(frozen)
    progress: list[dict[str, Any]] = []
    checkpoints: list[tuple[str, dict[str, Any]]] = []
    payload = jobs.execute_job(spec.parameters, Event(), progress.append,
        'in-memory-only-checkpoint', lambda path, item: checkpoints.append((path, deepcopy(item))))
    assert payload['state'] == 'completed', payload.get('message')
    assert jobs.workflow_for_result(payload) == kind and jobs.is_workflow_result(payload)
    assert jobs.result_matches_job(payload, spec.parameters['payload'])
    assert _film_reply_matches_payload(payload, spec.parameters['payload'])
    assert progress and all(row['stage'].startswith(kind + ':') for row in progress)
    jobs.validate_workflow_result(payload, expected_request=frozen, replay=True)
    cancelled = _completed_film_cancel_reply(payload, spec.parameters['payload'])
    assert cancelled['state'] == 'cancelled' and cancelled['is_final']
    jobs.validate_workflow_result(cancelled, expected_request=frozen, replay=True)
    failed = jobs.terminal_reply(payload, 'failed')
    assert failed['state'] == 'failed' and failed['termination_reason'] == 'scheduler_failed'
    jobs.validate_workflow_result(failed, expected_request=frozen, replay=True)
    invented = deepcopy(cancelled)
    invented['termination_reason'] = 'unrecognized_success_disguised_as_cancel'
    with pytest.raises(ValueError):
        jobs.workflow_result_from_payload(invented)
    assert job_has_viewable_result({'kind': kind, 'state': 'cancelled', 'result': cancelled})
    other_kind = next(value for value in jobs.WORKFLOW_KINDS if value != kind)
    assert not job_has_viewable_result({'kind': other_kind, 'state': 'succeeded', 'result': payload})
    if checkpoints:
        assert checkpoints[-1][1]['schema_version'] == 1
        recovered = jobs.validate_checkpoint(checkpoints[-1][1], expected_job=spec.parameters['payload'])
        assert recovered['state'] == 'cancelled'
        jobs.validate_workflow_result(recovered, expected_request=frozen, replay=True)
    foreign = deepcopy(spec.parameters['payload'])
    foreign['request']['design_revision'] += 1
    assert not jobs.result_matches_job(payload, foreign)
    assert _completed_film_cancel_reply(payload, foreign) is None
    with pytest.raises(ValueError):
        jobs.validate_workflow_result(payload, expected_request=foreign['request'], replay=True)


@pytest.mark.parametrize('kind', jobs.WORKFLOW_KINDS)
def test_new_executor_and_canonical_are_exactly_bound_to_typed_request(kind):
    service, request = problem(kind)
    frozen = jobs.freeze_workflow_request(service, request)
    raw = jobs.workflow_request_to_payload(frozen)
    values = compute_estimation.canonical_parameters(service, kind, {'payload': raw})
    assert values == {'payload': raw}
    other_kind = next(value for value in jobs.WORKFLOW_KINDS if value != kind)
    with pytest.raises(ValueError, match='differs'):
        freeze_spec(None, other_kind, {'request': frozen})
    with pytest.raises(ValueError, match='differs'):
        compute_estimation.canonical_parameters(service, other_kind, {'payload': raw})
    for mutation in ({'extra': True}, {'schema_version': True}, {'kind': 'coat_sim.film_sensitivity_request'}):
        foreign = deepcopy(raw)
        foreign.update(mutation)
        with pytest.raises(ValueError):
            jobs.workflow_request_from_payload(foreign)


@pytest.mark.parametrize('kind', jobs.WORKFLOW_KINDS)
def test_actual_cli_stdout_new_report_replay_and_never_overwrite(kind, tmp_path, monkeypatch, capsys):
    from coat_sim import film_sensitivity_workflow_cli as cli
    service, request = problem(kind)
    raw = jobs.workflow_request_to_payload(jobs.freeze_workflow_request(service, request))
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(raw)))
    assert cli.main([]) == 0
    result = json.loads(capsys.readouterr().out)['result']
    jobs.validate_workflow_result(result, expected_request=raw, replay=True)
    request_path, report_path = tmp_path/'request.json', tmp_path/'report.json'
    request_path.write_text(json.dumps(raw), encoding='utf-8')
    assert cli.main(['--request', str(request_path), '--output', str(report_path)]) == 0
    stored = report_path.read_bytes()
    jobs.validate_workflow_result(json.loads(stored)['result'], expected_request=raw, replay=True)
    assert cli.main(['--request', str(request_path), '--output', str(report_path)]) == 2
    assert report_path.read_bytes() == stored


@pytest.mark.parametrize('kind', jobs.WORKFLOW_KINDS)
def test_cli_rejects_live_material_dependency_and_reports_real_cancellation(kind, monkeypatch, capsys):
    from coat_sim import film_sensitivity_workflow_cli as cli
    service, request = problem(kind)
    raw = jobs.workflow_request_to_payload(request)
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(raw)))
    assert cli.main([]) == 2
    assert 'frozen materials' in capsys.readouterr().out
    frozen = jobs.freeze_workflow_request(service, request)
    cancel = Event()
    cancel.set()
    module = jobs._api(kind)
    result = getattr(module, 'execute_' + kind + '_request')(None, frozen, cancel_event=cancel)
    assert result.state == 'cancelled'


@pytest.mark.parametrize('kind', jobs.WORKFLOW_KINDS)
def test_readonly_mcp_planner_freezes_exact_request_without_job_or_transport(kind, monkeypatch):
    from coat_sim.mcp import server
    service, request = problem(kind)
    monkeypatch.setattr(server, 'service', SimpleNamespace(film_design_service=service))
    payload = jobs.workflow_request_to_payload(request)
    result = getattr(server, 'plan_' + kind)(payload)
    assert result['ok'] and 'resources' in result
    expected = jobs.workflow_request_to_payload(jobs.freeze_workflow_request(service, request))
    assert result['request'] == expected
    other_kind = next(value for value in jobs.WORKFLOW_KINDS if value != kind)
    with pytest.raises(Exception, match='differs'):
        getattr(server, 'plan_' + other_kind)(payload)


def test_table_request_never_changes_current_ideal_payload():
    service, ideal = problem('film_ideal_sensitivity')
    before = jobs.workflow_request_to_payload(jobs.freeze_workflow_request(service, ideal))
    _, table = problem('film_thickness_error_table')
    changed = replace(table, rows=(replace(table.rows[0], bias=7.),))
    assert jobs.workflow_request_to_payload(table) != jobs.workflow_request_to_payload(changed)
    assert jobs.workflow_request_to_payload(jobs.freeze_workflow_request(service, ideal)) == before


def test_scheduler_failed_terminal_never_relabels_initial_nominal_failure():
    from tests.test_film_ideal_sensitivity import problem as ideal_problem
    service, request = ideal_problem(upper=0.)
    frozen = jobs.freeze_workflow_request(service, request)
    result = jobs._api('film_ideal_sensitivity').execute_film_ideal_sensitivity_request(None, frozen)
    payload = jobs.workflow_result_to_payload(result)
    assert payload['state'] == 'failed' and payload['termination_reason'] == 'initial_nominal_infeasible'
    assert jobs.terminal_reply(payload, 'failed') == payload


@pytest.mark.parametrize('kind', jobs.WORKFLOW_KINDS)
def test_mcp_schema_and_effect_boundaries_expose_no_implicit_process_defaults(kind):
    from coat_sim.core import mcp_contracts as contracts
    from coat_sim.agent import runtime, tool_dispatch
    _, request = problem(kind)
    raw = jobs.workflow_request_to_payload(request)
    schema = (contracts.FILM_IDEAL_SENSITIVITY_PLAN_INPUT_SCHEMA if kind == 'film_ideal_sensitivity'
              else contracts.FILM_ERROR_TABLE_PLAN_INPUT_SCHEMA)
    assert contracts.validate_mcp_arguments(schema, {'payload': raw}) == {'payload': raw}
    props = schema['properties']['payload']['properties']
    assert 'error_model' not in props and 'distribution' not in props and 'scenarios' not in props
    assert contracts.tool_annotations('plan_' + kind).read_only_hint is True
    assert contracts.tool_annotations('submit_' + kind).read_only_hint is False
    assert 'plan_' + kind in runtime.MAIN_TOOLS
    assert 'submit_' + kind in runtime.SUBMIT_TOOLS
    assert 'submit_' + kind in tool_dispatch.STRONG_IDEMPOTENT_TOOLS
    assert 'plan_' + kind in runtime.EXECUTION_TOOLS['simulation']
    assert 'submit_' + kind in runtime.EXECUTION_TOOLS['simulation']
    unexpected = deepcopy(raw)
    unexpected['error_model'] = 'independent'
    with pytest.raises(CapabilityError):
        contracts.validate_mcp_arguments(schema, {'payload': unexpected})


def test_independent_ideal_analysis_cannot_substitute_for_formal_optimization_job():
    from coat_sim.core import film_ideal_sensitivity as ideal
    service, request = problem('film_ideal_sensitivity')
    frozen = jobs.freeze_workflow_request(service, request)
    job = jobs.build_job_payload(None, frozen)
    result = ideal.analyze_film_ideal_sensitivity(None, frozen)
    payload = ideal.film_ideal_sensitivity_result_to_payload(result)
    jobs.validate_workflow_result(payload, expected_request=frozen, replay=True)
    assert payload['operation'] == 'analyze'
    assert not jobs.result_matches_job(payload, job)
    from coat_sim.scheduler.result_capability import is_best_candidate_result
    assert not job_has_viewable_result({'kind': 'film_ideal_sensitivity', 'state': 'succeeded', 'result': payload})
    assert not is_best_candidate_result(payload)
    assert not _film_reply_matches_payload(payload, job)
    assert _completed_film_cancel_reply(payload, job) is None
    checkpoint = {'schema_version': 1, 'kind': 'coat_sim.film_ideal_sensitivity_checkpoint',
                  'job': job, 'result': payload}
    with pytest.raises(ValueError, match='bound'):
        jobs.validate_checkpoint(checkpoint, expected_job=job)


def test_gui_observer_rejects_analysis_reply_for_its_ideal_optimization_job(monkeypatch):
    from coat_sim.core import film_ideal_sensitivity as ideal
    from coat_sim.ui import film_sensitivity_quick_controller as gui
    service, request = problem('film_ideal_sensitivity')
    frozen = jobs.freeze_workflow_request(service, request)
    payload = ideal.film_ideal_sensitivity_result_to_payload(
        ideal.analyze_film_ideal_sensitivity(None, frozen))
    finished: list[tuple[Any, ...]] = []
    signals = SimpleNamespace(
        finished=SimpleNamespace(emit=lambda *args: finished.append(args)),
        progress=SimpleNamespace(emit=lambda *_args: None),
        admitted=SimpleNamespace(emit=lambda *_args: None))
    client = SimpleNamespace(capabilities=lambda: {'executors': ['film_ideal_sensitivity']})
    cancel = SimpleNamespace(is_set=lambda: False, mark_admitted=lambda: None)
    monkeypatch.setattr(gui, 'submit_local_with_receipt', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gui, 'observe_job', lambda *_args, **_kwargs: deepcopy(payload))
    task = gui._SensitivityTask(None, frozen, client, 'in-memory-observer', cancel, {}, 4,
                                signals, Event(), lambda: True)
    task.run()
    assert len(finished) == 1
    assert finished[0][0] == 4 and finished[0][1] is None
    assert 'operation' in finished[0][2].lower() or 'bound' in finished[0][2].lower()
