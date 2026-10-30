"""Real frozen worker artifacts, isolated MCP reads/plans; no daemon or user data."""
from copy import deepcopy
import json
from threading import Event
from types import SimpleNamespace

import pytest

from coat_sim.core.film_synthesis_continuation import validate_search_state
from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest, freeze_film_synthesis_request
from coat_sim.core.film_synthesis_io import film_synthesis_request_from_payload, film_synthesis_request_to_payload
from coat_sim.core.film_synthesis_io import FilmSynthesisPayloadError
from coat_sim.core.film_synthesis_jobs import canonical_parameters, execute_job
from coat_sim.mcp import film_synthesis_tools as tools
from coat_sim.scheduler.io import write_json
from coat_sim.scheduler.jobs import JobSpec, freeze_spec
from tests.test_film_synthesis_session_stop import unmet_problem

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


@pytest.fixture
def job(tmp_path, monkeypatch):
    service, request = unmet_problem(enable_thickness_reduction=False)
    request = freeze_film_synthesis_request(service, request)
    payload = film_synthesis_request_to_payload('assistant-source', request)
    values = canonical_parameters(None, {'payload': payload})
    spec = JobSpec.parse(freeze_spec(None, 'film_synthesis', values))
    directory = tmp_path / 'artifacts/runtime/scheduler/jobs/source'
    directory.mkdir(parents=True)
    write_json(directory / 'spec.json', spec.to_dict())
    response = execute_job(spec.parameters, Event(), lambda *_args, **_kwargs: None,
                           directory / 'best-candidate.json', write_json, str(tmp_path))
    # Match the durable scheduler JSON boundary, including tuple/list encoding.
    response = json.loads(json.dumps(response, allow_nan=False))
    row = dict(job_id='source', kind='film_synthesis', state='succeeded', result=response,
               input_digest=spec.fingerprint, progress={'stage': 'completed'}, error=None)
    monkeypatch.setattr(tools, 'SchedulerClient', lambda _root: SimpleNamespace(status=lambda _job_id: deepcopy(row)))
    return SimpleNamespace(service=SimpleNamespace(root=tmp_path), request=request,
                           spec=spec, directory=directory, row=row, response=response)


@pytest.fixture
def queue_job(tmp_path, monkeypatch):
    directory = tmp_path / 'artifacts/runtime/scheduler/jobs/queued-source'
    directory.mkdir(parents=True)
    row = dict(job_id='queued-source', kind='film_synthesis', state='queued',
               result=None, input_digest='0' * 64, progress={'stage': 'queued'},
               error=None, cancel_requested=False)
    monkeypatch.setattr(tools, 'SchedulerClient', lambda _root: SimpleNamespace(
        status=lambda job_id: deepcopy(row) if job_id == row['job_id'] else _missing_job()))
    return SimpleNamespace(service=SimpleNamespace(root=tmp_path), row=row, directory=directory)


def _missing_job():
    raise ValueError('job_not_found')


def test_schema_defaults_are_the_real_product_request_without_hidden_run_caps():
    template = tools.request_template()
    assert template['request']['needle_pre_local'] is True
    for name in ('max_cycles', 'max_evaluations', 'max_seconds'):
        assert template['request'][name] is None
    assert 'objective_progress_policy' not in template['request']
    assert template['request']['environment']['assume_lossless'] is True


def test_mcp_schema_requires_explicit_environment_assumption_and_preserves_boolean():
    from coat_sim.core.capabilities import CapabilityError
    from coat_sim.core.mcp_contracts import FILM_SYNTHESIS_PLAN_INPUT_SCHEMA, validate_mcp_arguments
    from tests.test_film_synthesis_jobs import _payload

    environment_schema = (FILM_SYNTHESIS_PLAN_INPUT_SCHEMA['properties']['payload']['properties']
                          ['request']['properties']['environment'])
    assert environment_schema['required'] == ['assume_lossless']
    assert environment_schema['properties']['assume_lossless']['default'] is True
    for enabled in (True, False):
        payload = _payload()
        payload['request']['environment']['assume_lossless'] = enabled
        for seed in payload['request']['seed_designs']:
            seed['assume_lossless'] = enabled
        validate_mcp_arguments(FILM_SYNTHESIS_PLAN_INPUT_SCHEMA, {'payload': payload})
        _request_id, request, _structures = film_synthesis_request_from_payload(payload)
        assert request.environment['assume_lossless'] is enabled

    payload = _payload()
    del payload['request']['environment']['assume_lossless']
    with pytest.raises(CapabilityError):
        validate_mcp_arguments(FILM_SYNTHESIS_PLAN_INPUT_SCHEMA, {'payload': payload})
    with pytest.raises(FilmSynthesisPayloadError, match='assume_lossless'):
        film_synthesis_request_from_payload(payload)


def test_mcp_request_schema_requires_every_nonextension_decoder_field():
    from dataclasses import fields
    from coat_sim.core.mcp_contracts import FILM_SYNTHESIS_PLAN_INPUT_SCHEMA
    request_schema = FILM_SYNTHESIS_PLAN_INPUT_SCHEMA['properties']['payload']['properties']['request']
    optional_extensions = {'search_checkpoint', 'enable_local_margin_refinement', 'selected_surfaces'}
    decoder_fields = {item.name for item in fields(FilmSynthesisRequest)}
    assert set(request_schema['required']) == decoder_fields - optional_extensions


def test_qml_and_docs_match_current_unknown_k_default_and_schema_version():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    qml = (root / 'coat_sim/ui/qml/FilmOptimizationWindow.qml').read_text(encoding='utf-8')
    unknown_k = (root / 'docs/film-synthesis-unknown-k-contract.md').read_text(encoding='utf-8')
    algorithms = (root / 'docs/algorithms.html').read_text(encoding='utf-8')
    execution = (root / 'docs/film-synthesis-execution-contract.md').read_text(encoding='utf-8')
    continuation = (root / 'docs/film-optimization-continue-contract.md').read_text(encoding='utf-8')
    synthesis_contract = (root / 'docs/film-auto-synthesis-contract.md').read_text(encoding='utf-8')
    assert '新建合成默认勾选未知 k 按 0' in qml and '取消后缺 k 会严格拒绝' in qml
    assert '默认启用该未知 k=0 假设' in unknown_k and '用户可关闭' in unknown_k
    assert '合成请求/结果/任务/检查点当前 schema=6' in algorithms
    assert '当前从零合成默认true且可关闭' in algorithms
    assert 'pre-local默认开启（可显式关闭）' in algorithms
    assert 'needle_pre_local=true（新建默认，可显式关闭）' in synthesis_contract
    assert 'schema_version: 6' in execution and 'current schema 6' in execution
    assert 'schema 6' in continuation


@pytest.mark.parametrize('retired_input', ['schema5', 'policy_field'])
def test_retired_execution_input_is_rejected_without_a_compatibility_entry(retired_input):
    from tests.test_film_synthesis_jobs import _payload
    payload = _payload()
    if retired_input == 'schema5':
        payload['schema_version'] = 5
    else:
        payload['request']['objective_progress_policy'] = None
    with pytest.raises(FilmSynthesisPayloadError):
        film_synthesis_request_from_payload(payload)


def test_terminal_state_target_and_candidate_audit_remain_separate(job):
    result = tools.read_job(job.service, 'source')
    assert result['scheduler_state'] == 'succeeded'
    assert result['core_state'] == 'completed'
    assert result['targets_met'] is False
    assert result['best_candidate']['validation_complete'] is True
    assert result['best_candidate']['recorded_candidate_audit']['targets_met'] is False
    assert result['best_candidate']['replayed_by_this_read'] is False
    assert result['continuation']['available'] is True
    assert result['source_input_digest'] == job.spec.fingerprint


def test_live_read_is_from_the_same_committed_audited_checkpoint_without_optics(job, monkeypatch):
    job.row.update(state='running', result=None, progress={'stage': 'local_margin'})
    monkeypatch.setattr('coat_sim.core.film_synthesis_audit.audit_frozen_synthesis_candidate',
                        lambda *_a, **_k: pytest.fail('read must not perform an optical audit'))
    result = tools.read_job(job.service, 'source')
    assert result['scheduler_state'] == 'running' and result['core_state'] is None
    assert result['source_available'] and result['request_available']
    assert result['targets_met'] is None
    assert result['best_candidate']['source'] == 'worker_committed_audited_checkpoint'
    assert result['best_candidate']['physical_hash']
    assert not result['best_candidate']['replayed_by_this_read']
    assert result['continuation'] == dict(available=False,
        checkpoint_sha256=result['continuation']['checkpoint_sha256'], reason='job_still_active')


@pytest.mark.parametrize(('state', 'error_code', 'reason'), [
    ('queued', None, 'worker_source_not_started'),
    ('failed', 'worker_start_failed', 'worker_start_failed_before_source'),
])
def test_pre_worker_status_is_queue_only_and_has_no_fabricated_evidence(queue_job, state, error_code, reason):
    queue_job.row.update(state=state, error_code=error_code, result=None,
                   progress={'stage': 'queued' if state == 'queued' else 'worker_start_failed'})
    for path in queue_job.directory.iterdir():
        path.unlink()
    queue_job.directory.rmdir()

    result = tools.read_job(queue_job.service, 'queued-source')
    assert result['scheduler_state'] == state
    assert result['best_candidate'] is None
    assert result['source_available'] is False
    assert result['request_available'] is False
    assert result['checkpoint_available'] is False
    assert result['source_input_digest'] is None
    assert result['progress_evidence'].startswith('Scheduler queue status only;')
    assert result['continuation'] == {
        'available': False, 'checkpoint_sha256': None, 'reason': reason}
    with pytest.raises(ValueError, match='frozen worker source is not available'):
        tools.plan_continuation(queue_job.service, 'queued-source', 'next')


def test_queue_only_status_rejects_wrong_kind_and_unknown_job_id(queue_job):
    queue_job.row.update(state='queued', result=None)
    queue_job.row['kind'] = 'film_optimization'
    with pytest.raises(ValueError, match='not the requested synthesis task'):
        tools.read_job(queue_job.service, 'queued-source')
    with pytest.raises(ValueError, match='job_not_found'):
        tools.read_job(queue_job.service, 'missing')


def test_queue_source_rejects_symlinked_job_directory(queue_job):
    import shutil
    other = queue_job.directory.parent / 'other-film-synthesis-job'
    other.mkdir()
    (other / 'spec.json').write_text('{}', encoding='utf-8')
    shutil.rmtree(queue_job.directory)
    queue_job.directory.symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError, match='escaped its workspace'):
        tools.read_job(queue_job.service, 'queued-source')


def test_plan_continuation_preserves_real_progress_and_does_not_mutate_source(job):
    before = {p.name: p.read_bytes() for p in job.directory.iterdir()}
    original_state = job.response['result']['search_checkpoint']
    planned = tools.plan_continuation(job.service, 'source', 'assistant-next',
                                     limits={'max_evaluations': None, 'max_seconds': None})
    identity, continued, _ = film_synthesis_request_from_payload(planned['request'])
    assert identity == 'assistant-next'
    assert continued.search_checkpoint == original_state
    assert continued.max_evaluations is None and continued.max_seconds is None
    assert continued.max_cycles == job.request.max_cycles
    assert continued.search_checkpoint is not None
    assert validate_search_state(continued, dict(continued.search_checkpoint)) == original_state
    assert {p.name: p.read_bytes() for p in job.directory.iterdir()} == before
    assert job.row['result'] == job.response


def test_compatible_edited_seed_is_fresh_input_with_existing_frontier(job):
    edited = deepcopy(job.response['result']['candidate_design'])
    edited['front_layers'][0]['thickness']['value'] += .01
    planned = tools.plan_continuation(job.service, 'source', 'edited-next', current_design=edited)
    _, continued, _ = film_synthesis_request_from_payload(planned['request'])
    assert continued.seed_designs[0] == edited and continued.warm_start_seed_index == 0
    assert continued.search_checkpoint == job.response['result']['search_checkpoint']


@pytest.mark.parametrize('mutation', ['job_kind', 'digest', 'result_request', 'checkpoint_digest'])
def test_wrong_source_or_damaged_state_fails_explicitly_without_fallback(job, mutation):
    if mutation == 'job_kind':
        job.row['kind'] = 'film_optimization'
    elif mutation == 'digest':
        job.row['input_digest'] = '0' * 64
    elif mutation == 'result_request':
        job.row['result']['request_id'] = 'different-source'
    else:
        job.row['result']['result']['search_checkpoint']['sha256'] = '0' * 64
    with pytest.raises(ValueError):
        tools.plan_continuation(job.service, 'source', 'new-request')


def test_active_job_cannot_be_forked_and_missing_checkpoint_is_not_a_warm_restart(job):
    job.row['state'] = 'running'
    with pytest.raises(ValueError, match='still active'):
        tools.plan_continuation(job.service, 'source', 'next')
    job.row['state'] = 'succeeded'
    job.row['result']['result']['search_checkpoint'] = None
    (job.directory / 'best-candidate.search-state.json').unlink()
    with pytest.raises(ValueError, match='no committed search checkpoint'):
        tools.plan_continuation(job.service, 'source', 'next')


@pytest.mark.parametrize('limits', [{'max_evaluations': True}, {'max_seconds': -1}, {'worker': 4}])
def test_continuation_has_no_coerced_or_unrelated_resource_fields(job, limits):
    with pytest.raises((TypeError, ValueError)):
        tools.plan_continuation(job.service, 'source', 'next', limits=limits)


def test_registered_read_tools_are_closed_and_do_not_grant_submit_or_apply(job, monkeypatch):
    import asyncio
    from coat_sim.mcp import server
    from coat_sim.agent.runtime import READ_TOOLS, SUBMIT_TOOLS
    monkeypatch.setattr(server, 'service', job.service)

    async def exercise():
        registry = {item.name: item for item in await server.mcp.list_tools()}
        for name in ('film_synthesis_schema', 'film_synthesis_job', 'plan_film_synthesis_continuation'):
            assert name in READ_TOOLS and name not in SUBMIT_TOOLS
            assert registry[name].input_schema['additionalProperties'] is False
        response = await server.mcp.call_tool('film_synthesis_job', {'job_id': 'source'})
        value = getattr(response, 'structured_content', None) or response.structuredContent
        assert value['best_candidate']['validation_complete']
        with pytest.raises(server.ToolError, match='invalid_arguments'):
            await server.mcp.call_tool('plan_film_synthesis_continuation',
                                      {'job_id': 'source', 'request_id': 'next', 'approve': True})
    asyncio.run(exercise())
