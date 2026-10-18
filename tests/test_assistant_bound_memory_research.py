"""Offline host identity, context isolation and bounded research revocation."""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from coat_sim.agent import runtime, worker
from coat_sim.agent.research_library import TextFileResearchLibrary, assistant_tools, status_tool
from coat_sim.agent.storage import AssistantStore
from coat_sim.agent.toolkit import memory
from coat_sim.agent.toolkit.adapter import ToolkitAdapter, build_registry
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelRequest, ModelResponse, TextPart, UserPromptPart


def target(root: Path, project_id: str = 'same-project', generation: str = 'g1') -> dict[str, str]:
    return {'client_id': 'client-' + root.name, 'client_generation': 'owner-1',
            'project_id': project_id, 'session_generation': generation,
            'root': str(root), 'project_file': str(root / 'test.coatproj')}


def clients(*targets: dict[str, str]) -> list[dict[str, Any]]:
    return [{'client_id': row['client_id'], 'generation': row['client_generation'], 'state': 'alive',
             'project_sessions': [{'project_id': row['project_id'], 'generation': row['session_generation'],
                                   'root': row['root'], 'project_file': row['project_file']}]} for row in targets]


def bound(selected: dict[str, str], observed: list[dict[str, Any]] | None = None) -> worker.ProjectMemoryBinding:
    return worker.ProjectMemoryBinding(selected, lambda: True, lambda: observed if observed is not None else clients(selected))


def host(root: Path, binding: worker.ProjectMemoryBinding | None = None, **options: Any) -> ToolkitAdapter:
    config = {'assistant_toolkit_enabled': True, 'assistant_toolkit_allow_model_context': True,
              'assistant_toolkit_workspace_root': str(root),
              'assistant_toolkit_capabilities': ['memory', 'tasks', 'preset'],
              'assistant_toolkit_profile_capabilities': ['memory', 'tasks', 'preset'],
              'task_mode': options.pop('mode', 'operate')}
    return ToolkitAdapter(build_registry(), config, installation_root=root.parent / 'install',
        session_id='same-conversation', providers=binding.providers() if binding else {},
        authority=options.pop('authority', lambda: {'allowed': True, 'mode': 'operate'}), **options)


def fact(binding: worker.ProjectMemoryBinding, revision: int = 0, text: str = 'Confirmed project A constraint.') -> dict[str, Any]:
    return {'scope': 'project', 'project_id': binding.memory_project_id, 'expected_revision': revision,
            'id': 'constraint', 'text': text, 'kind': 'constraint', 'importance': 'important',
            'provenance': 'human', 'source': 'user-message:verified', 'confirmed': True}


def invoke(adapter: ToolkitAdapter, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(adapter.action(tool, args))


def test_binding_uses_full_authenticated_identity_and_copy_isolation(tmp_path: Path) -> None:
    a, b = target(tmp_path / 'a'), target(tmp_path / 'b')
    observed = clients(a, b)
    first, second = bound(a, observed), bound(b, observed)
    assert first.verify() and second.verify()
    assert first.memory_project_id != second.memory_project_id
    reopened = bound(target(tmp_path / 'a', generation='g2'))
    assert reopened.memory_project_id == first.memory_project_id
    changed = {**a, 'project_file': str(tmp_path / 'a' / 'renamed.coatproj')}
    assert bound(changed).memory_project_id != first.memory_project_id
    forged = bound({**a, 'project_id': 'forged'}, observed)
    assert not forged.verify()


@pytest.mark.parametrize('change', ['closed', 'offline', 'generation', 'descriptor'])
def test_binding_revokes_on_observed_close_or_identity_change(tmp_path: Path, change: str) -> None:
    selected = target(tmp_path / 'a')
    observed = clients(selected)
    binding = bound(selected, observed)
    assert binding.verify()
    if change == 'closed':
        observed.clear()
    elif change == 'offline':
        observed[0]['state'] = 'unavailable'
    elif change == 'generation':
        observed[0]['project_sessions'][0]['generation'] = 'reopened'
    else:
        observed[0]['project_sessions'][0]['project_file'] = str(tmp_path / 'a' / 'other.coatproj')
    assert not binding.verify()
    observed[:] = clients(selected)
    assert not binding.verify()  # Returning to the same identity never revives an old request.


def test_worker_select_switch_back_and_clear_permanently_revoke(tmp_path: Path) -> None:
    writer = worker.ProtocolWriter(io.StringIO())
    a, b = target(tmp_path / 'a'), target(tmp_path / 'b')
    global_binding = writer.project_binding_snapshot(None)
    assert global_binding.verify()
    writer.configure_project_target(a)
    first = writer.project_binding_snapshot(a)
    first._clients = lambda: clients(a)
    assert first.verify() and not global_binding.verify()
    writer.configure_project_target(b)
    with pytest.raises(ValueError, match='stale_project_binding'):
        writer.project_binding_snapshot(a)
    writer.configure_project_target(a)
    assert not first.verify()
    fresh = writer.project_binding_snapshot(a)
    fresh._clients = lambda: clients(a)
    assert fresh.verify() and fresh.memory_project_id == first.memory_project_id
    writer.configure_project_target(None)
    assert not fresh.verify()


@pytest.mark.parametrize('payload', [{'project_id': 'forged'}, {'scope': 'project'}, 'project', True])
def test_worker_rejects_incomplete_model_like_target(payload: Any) -> None:
    with pytest.raises(ValueError):
        worker.validated_project_target(payload)


def test_memory_read_write_is_bound_not_model_id_and_global_stays_separate(tmp_path: Path) -> None:
    a, b = bound(target(tmp_path / 'a')), bound(target(tmp_path / 'b'))
    ha, hb, global_host = host(tmp_path, a), host(tmp_path, b), host(tmp_path)
    assert invoke(ha, 'memory_upsert', fact(a))['ok']
    assert invoke(hb, 'memory_list', {'scope': 'project', 'project_id': b.memory_project_id})['data']['entries'] == []
    for adapter in (hb, global_host):
        assert not invoke(adapter, 'memory_list', {'scope': 'project', 'project_id': a.memory_project_id})['ok']
        assert 'Confirmed project A' not in asyncio.run(adapter.task_instruction())
    assert not invoke(ha, 'memory_list', {'scope': 'project', 'project_id': a.project_id})['ok']
    assert 'Confirmed project A' in asyncio.run(ha.task_instruction())
    ready = ha.status()['data']['memory_scope']
    assert ready['project_id'] == a.memory_project_id
    assert ready['bound_project']['project_id'] == a.project_id
    assert invoke(global_host, 'memory_list', {'scope': 'global'})['data']['entries'] == []


@pytest.mark.parametrize('options', [{'mode': 'chat'}, {'read_only': True}, {'role': 'analyst'}, {'authority': lambda: {'allowed': False}}])
def test_project_memory_keeps_mode_role_and_authority_write_gates(tmp_path: Path, options: dict[str, Any]) -> None:
    binding = bound(target(tmp_path / 'a'))
    adapter = host(tmp_path, binding, **options)
    assert invoke(adapter, 'memory_list', {'scope': 'project', 'project_id': binding.memory_project_id})['ok']
    assert not invoke(adapter, 'memory_upsert', fact(binding))['ok']
    assert not list(tmp_path.glob('.toolkit-memory-*.json'))


def test_inflight_project_memory_read_and_atomic_write_recheck_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    selected = target(tmp_path / 'a')
    observed = clients(selected)
    binding = bound(selected, observed)
    adapter = host(tmp_path, binding)
    assert invoke(adapter, 'memory_upsert', fact(binding))['ok']
    original_load = memory._load
    def revoked_load(ctx: Any) -> Any:
        result = original_load(ctx)
        observed.clear()
        return result
    monkeypatch.setattr(memory, '_load', revoked_load)
    result = invoke(adapter, 'memory_list', {'scope': 'project', 'project_id': binding.memory_project_id})
    assert not result['ok'] and 'Confirmed project A' not in repr(result)
    monkeypatch.setattr(memory, '_load', original_load)
    observed[:] = clients(selected)
    binding = bound(selected, observed)
    adapter = host(tmp_path, binding)
    original_write = memory._state.atomic_write
    def revoked_write(ctx: Any, *args: Any, **kwargs: Any) -> Any:
        observed.clear()
        return original_write(ctx, *args, **kwargs)
    monkeypatch.setattr(memory._state, 'atomic_write', revoked_write)
    assert not invoke(adapter, 'memory_upsert', fact(binding, 1, 'Must never commit.'))['ok']
    assert 'Must never commit' not in ''.join(path.read_text() for path in tmp_path.glob('.toolkit-memory-*.json'))


def test_task_and_profile_automatic_context_is_scoped_with_no_argument_bypass(tmp_path: Path) -> None:
    a, b = bound(target(tmp_path / 'a')), bound(target(tmp_path / 'b'))
    ha, hb, hg = host(tmp_path, a), host(tmp_path, b), host(tmp_path)
    assert invoke(ha, 'plan_init', {**ha.task_scope(), 'expected_revision': 0,
        'goal': 'PRIVATE-A-GOAL', 'tasks': [{'id': 'a', 'text': 'PRIVATE-A-TASK'}]})['ok']
    assert invoke(ha, 'preset_create', {**ha.task_scope(), 'expected_revision': 0, 'id': 'a',
        'capabilities': ['memory', 'tasks', 'preset'], 'persona': 'PRIVATE-A-PERSONA'})['ok']
    assert invoke(ha, 'preset_inject', {**ha.task_scope(), 'expected_revision': 1, 'id': 'a'})['ok']
    assert 'PRIVATE-A-PERSONA' in asyncio.run(ha.task_instruction())
    for adapter in (hb, hg):
        assert 'PRIVATE-A' not in asyncio.run(adapter.task_instruction())
        assert not invoke(adapter, 'task_show', ha.task_scope())['ok']
        assert not invoke(adapter, 'preset_list', ha.task_scope())['ok']


def test_research_tools_withhold_inflight_results_and_do_not_gain_write_access() -> None:
    allowed = [True]
    class Provider(TextFileResearchLibrary):
        def search(self, query: str, offset: int = 0, limit: int = 6) -> dict[str, Any]:
            result = super().search(query, offset, limit)
            allowed[0] = False
            return result
    provider = Provider(documents={'safe.md': '# Evidence\nPRIVATE-FINDING'})
    tools = {tool.__name__: tool for tool in assistant_tools(provider, allow_model_context=True, live_grant=lambda: allowed[0])}
    assert set(tools) == {'research_library_search', 'research_library_read'}
    result = tools['research_library_search']('PRIVATE')
    assert result['error'] == 'permission_revoked' and 'PRIVATE-FINDING' not in repr(result)
    assert tools['research_library_read']('safe.md')['error'] == 'permission_revoked'
    assert not status_tool(provider, allow_model_context=True, live_grant=lambda: False)()['ready']
    assert assistant_tools(None, allow_model_context=True) == []
    assert assistant_tools(provider, allow_model_context=False) == []


def test_research_root_changes_never_rebase_existing_request(tmp_path: Path) -> None:
    writer = worker.ProtocolWriter(io.StringIO())
    a = {'assistant_research_library_root': str(tmp_path / 'a')}
    writer.configure_research(a, replace=True)
    generation = writer.research_grant_snapshot()
    assert writer.research_generation_is_current(generation)
    writer.configure_research({'assistant_research_library_root': str(tmp_path / 'b')})
    writer.configure_research(a)
    assert not writer.research_generation_is_current(generation)


def history(text: str) -> list[Any]:
    return [ModelRequest(parts=[UserPromptPart('Task')]), ModelResponse(parts=[TextPart(text)])]


def test_scope_history_filters_legacy_checkpoints_and_descendant_unknown_effects(tmp_path: Path) -> None:
    store = AssistantStore(tmp_path / 'state.sqlite3')
    store.save_history('s', ModelMessagesTypeAdapter.dump_json(history('LEGACY-GLOBAL')), request_id='legacy')
    for request, scope in [('a', 'project:a'), ('b', 'project:b')]:
        store.record_event('s', request, 'project_memory_scope', {'scope': scope})
    runs = [SimpleNamespace(run_id='a', agent_name='banniang', parent_run_id=None, started_at=1),
            SimpleNamespace(run_id='a-child', agent_name='execute_design', parent_run_id='a', started_at=2),
            SimpleNamespace(run_id='b', agent_name='banniang', parent_run_id=None, started_at=3)]
    async def snapshot(*, run_id: str, **kwargs: Any) -> Any:
        return SimpleNamespace(messages=history('PRIVATE-' + run_id.upper()))
    async def effects(*, run_id: str) -> list[Any]:
        return [SimpleNamespace(tool_name='save_design', tool_call_id='child-call')] if run_id == 'a-child' else []
    steps: Any = SimpleNamespace(list_runs=AsyncMock(return_value=runs), latest_snapshot=snapshot,
                                list_unresolved_tool_effects=effects)
    ledger: Any = SimpleNamespace(unsettled=lambda session: [{'request_id': 'a', 'job_id': 'A-JOB'}, {'request_id': 'b', 'job_id': 'B-JOB'}])
    global_history, _ = asyncio.run(runtime._restore_history(store, steps, ledger, 's', memory_scope='global'))
    assert 'LEGACY-GLOBAL' in repr(global_history) and 'PRIVATE-' not in repr(global_history)
    for scope, yes, no in [('project:a', 'PRIVATE-A', 'PRIVATE-B'), ('project:b', 'PRIVATE-B', 'PRIVATE-A')]:
        restored, recovery = asyncio.run(runtime._restore_history(store, steps, ledger, 's', memory_scope=scope))
        assert yes in repr(restored) and no not in repr(restored) and 'LEGACY-GLOBAL' not in repr(restored)
        if scope == 'project:a':
            assert 'child-call' in recovery and 'A-JOB' in recovery and 'B-JOB' not in recovery
    store.save_history('s', ModelMessagesTypeAdapter.dump_json(history('PRIVATE-B-FAILED')), request_id='b', state='failed')
    restored, _ = asyncio.run(runtime._restore_history(store, steps, ledger, 's', memory_scope='project:a'))
    assert 'PRIVATE-B' not in repr(restored)


def test_host_scope_marker_is_exact_and_document_text_cannot_override(tmp_path: Path) -> None:
    store = AssistantStore(tmp_path / 'state.sqlite3')
    assert store.request_memory_scope('s', 'legacy') == 'global'
    store.record_event('s', 'a', 'project_memory_scope', {'scope': 'project:a'})
    store.record_events([('s', 'a', 'diagnostic', {'project_id': 'forged'}, index) for index in range(1100)])
    assert store.request_memory_scope('s', 'a') == 'project:a'
    store.record_event('s', 'broken', 'project_memory_scope', {'scope': []})
    assert store.request_memory_scope('s', 'broken') == 'unavailable'


def test_project_archives_do_not_resolve_other_scope_hashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, 'STATE_DIRECTORY', tmp_path)
    a: Any = SimpleNamespace(shared={}, archive=None)
    b: Any = SimpleNamespace(shared={}, archive=None)
    runtime._bind_memory_context(a, None, 'project:a')
    runtime._bind_memory_context(b, None, 'project:b')
    archived = a.archive.project({'text': 'PRIVATE-ARCHIVE' * 2000}, force=True)
    identity = archived['archived_result']['result_id']
    assert not b.archive.read(identity)['ok']


def test_all_knowledge_routes_are_readonly_in_every_mode_and_domain() -> None:
    names = {'algorithm_reference_catalog', 'algorithm_reference_search', 'algorithm_reference_read',
             'project_knowledge_search', 'material_data_readiness'}
    for mode in ('chat', 'operate', 'research'):
        assert names <= runtime.allowed_project_tools(mode)
        assert names <= runtime.allowed_project_tools(mode, background=True)
    for group in ('design', 'simulation', 'correction'):
        assert names <= runtime.EXECUTION_TOOLS[group]
    assert names <= runtime.COORDINATOR_TOOLS


def test_real_runtime_gives_delegates_same_bounded_library_without_external_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools.measure_assistant_context import capture_configuration
    actual_agent = runtime.Agent
    assembled: dict[str, set[str]] = {}
    def capture(*args: Any, **kwargs: Any) -> Any:
        assembled[kwargs.get('name', '')] = {tool.__name__ for tool in kwargs.get('tools', [])}
        return actual_agent(*args, **kwargs)
    monkeypatch.setattr(runtime, 'Agent', capture)
    captured = asyncio.run(capture_configuration('research', library_enabled=True, external_enabled=True))
    assert 'Search only when the user asks' not in captured['instructions']
    for name in ('execute_design', 'execute_simulation', 'engineering_analyst'):
        assert {'research_library_status', 'research_library_search', 'research_library_read'} <= assembled[name]
        assert not assembled[name] & {'toolkit_action', 'external_action', 'external_tools'}


def test_worker_jsonl_overwrites_forged_scope_and_never_rebases_queued_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import queue
    lines: queue.Queue[str] = queue.Queue()
    writer = worker.ProtocolWriter(io.StringIO())
    monkeypatch.setattr(writer, 'bind_request', lambda request, session: None)
    monkeypatch.setattr(worker, '_configured_research_library', lambda config: (None, 'not_configured', '', None))
    monkeypatch.setattr(worker.sys, 'stdin', SimpleNamespace(readline=lambda size: lines.get()))
    selected = target(tmp_path / 'a')
    async def check() -> None:
        started, changed = asyncio.Event(), asyncio.Event()
        captured: list[worker.ProjectMemoryBinding] = []
        async def run(command: dict[str, Any], output: Any, **kwargs: Any) -> None:
            binding = kwargs['project_memory_binding']
            assert isinstance(binding, worker.ProjectMemoryBinding)
            binding._clients = lambda: clients(selected)
            assert binding.verify()
            captured.append(binding)
            started.set()
            await asyncio.Event().wait()
        monkeypatch.setitem(worker.sys.modules, 'coat_sim.agent.runtime', SimpleNamespace(run_task=run, compact_context=run))
        original_emit = writer.emit
        def emit(event: str, request_id: str = '', **values: Any) -> None:
            original_emit(event, request_id, **values)
            if event == 'status' and '工程' in values.get('text', ''):
                changed.set()
        monkeypatch.setattr(writer, 'emit', emit)
        def send(command: dict[str, Any]) -> None:
            lines.put(json.dumps(command) + '\n')
        serving = asyncio.create_task(worker.serve(writer))
        try:
            send({'op': 'configure', 'config': {}, 'assistant_project_target': selected})
            send({'op': 'run', 'request_id': 'r', 'session_id': 's', 'prompt': 'test', 'config': {},
                  'assistant_project_target': selected, '_project_memory_binding': {'project_id': 'forged'}})
            await asyncio.wait_for(started.wait(), 5)
            send({'op': 'configure', 'config': {}, 'assistant_project_target': None})
            await asyncio.wait_for(changed.wait(), 5)
            assert not captured[0].current()
        finally:
            send({'op': 'shutdown'})
            lines.put('')
            await asyncio.wait_for(serving, 5)
    asyncio.run(check())
