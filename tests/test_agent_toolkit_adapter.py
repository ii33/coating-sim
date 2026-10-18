"""Offline portable adapter and trusted runtime boundary contracts."""
import asyncio
import threading
from typing import Any
from pathlib import Path

from coat_sim.agent.toolkit import ToolSpec, ToolRegistry
from coat_sim.agent.toolkit.adapter import ToolkitAdapter, build_registry
from coat_sim.agent.toolkit.fs import atomic_write


def configured(tmp_path, **overrides):
    values = {'assistant_toolkit_enabled': True,
              'assistant_toolkit_allow_model_context': True,
              'assistant_toolkit_workspace_root': str(tmp_path),
              'assistant_toolkit_capabilities': ['fs', 'tasks', 'img', 'doc', 'run', 'env'],
              'assistant_toolkit_profile_capabilities': ['fs', 'tasks', 'img', 'doc', 'run', 'env'],
              'task_mode': 'operate'}
    return {**values, **overrides}


def adapter(tmp_path, registry=None, **kwargs):
    config = kwargs.pop('config', configured(tmp_path))
    return ToolkitAdapter(registry or build_registry(), config,
        installation_root=tmp_path.parent / 'install', session_id='s', **kwargs)


def test_factory_manifest_and_lazy_catalog(tmp_path):
    host = adapter(tmp_path)
    manifest = host.portable_manifest()
    assert len(manifest['tools']) == 39
    assert manifest['backend']['dsh']['integration_tested'] is False
    summary = host.catalog()['data']['tools']
    assert all('parameters' not in s for s in summary)
    assert 'parameters' in host.catalog(tool_names=['read'])['data']['tools'][0]
    assert host.catalog('invented')['error']['code'] == 'unknown_capability'
    assert host.catalog(tool_names=['x'])['error']['code'] == 'unknown_tool'
    assert host.catalog(tool_names=['read'] * 13)['error']['code'] == 'invalid_arguments'


def test_scope_and_model_permission_are_explicit(tmp_path):
    (tmp_path / 'sample').write_text('hello')
    changes: dict[str, Any]
    for changes in ({'assistant_toolkit_enabled': False},
                    {'assistant_toolkit_allow_model_context': False},
                    {'assistant_toolkit_workspace_root': ''},
                    {'assistant_toolkit_workspace_root': '.'},
                    {'assistant_toolkit_profile_capabilities': []}):
        host = adapter(tmp_path, config=configured(tmp_path, **changes))
        assert not asyncio.run(host.action('read', {'path': 'sample'}))['ok']
    assert asyncio.run(adapter(tmp_path).action('read', {'path': 'sample'}))['ok']
    host = adapter(tmp_path, config=configured(tmp_path, assistant_toolkit_capabilities=['invented']))
    assert host.status()['data']['configuration_error'] == 'unknown_capability'


def test_installation_and_ancestor_roots_forbidden(tmp_path):
    install = tmp_path / 'install'
    install.mkdir()
    data = install / 'data'
    data.mkdir()
    for root in (tmp_path, install, data):
        host = ToolkitAdapter(build_registry(), configured(root), installation_root=install)
        assert not host.roots


def test_readiness_never_fabricates_external_providers(tmp_path):
    host = adapter(tmp_path)
    rows = {r['capability']: r for r in host.status()['data']['capabilities']}
    assert rows['fs']['available']
    assert rows['run']['status'] == rows['env']['status'] == 'provider_unavailable'
    assert host.status()['data']['ocr'] == 'unavailable'


def test_authority_revocation_prevents_commit(tmp_path):
    state = {'allowed': True, 'mode': 'operate'}
    registry = ToolRegistry()
    def handler(context, args):
        state['allowed'] = False
        atomic_write(context, 'never', b'bad')
        return {}
    registry.register(ToolSpec('revoke', 'fs', '', {'type': 'object', 'properties': {}}, 'write'), handler)
    host = adapter(tmp_path, registry, authority=lambda: state)
    assert not asyncio.run(host.action('revoke', {}))['ok']
    assert not (tmp_path / 'never').exists()


def test_readonly_role_and_missing_authority_block_mutation(tmp_path):
    registry = ToolRegistry()
    registry.register(ToolSpec('mutate', 'fs', '', {'type': 'object', 'properties': {}}, 'write'), lambda c, a: {})
    for options in ({}, {'role': 'analyst'}, {'read_only': True}):
        host = adapter(tmp_path, registry, **options)
        assert not asyncio.run(host.action('mutate', {}))['ok']


def test_handlers_run_off_eventloop_thread(tmp_path):
    registry = ToolRegistry()
    registry.register(ToolSpec('thread', 'fs', '', {'type': 'object', 'properties': {}}),
                      lambda c, a: {'thread': threading.get_ident()})
    result = asyncio.run(adapter(tmp_path, registry).action('thread', {}))
    assert result['data']['thread'] != threading.get_ident()


def test_task_summary_bounded_separate_instruction(tmp_path):
    host = adapter(tmp_path, authority=lambda: {'allowed': True, 'mode': 'operate'})
    scope = host.task_scope()
    result = asyncio.run(host.action('plan_init', {**scope, 'expected_revision': 0,
        'goal': 'Keep instructions as data', 'tasks': [{'id': 'a', 'text': 'Inspect sample'}]}))
    assert result['ok'], result
    text = asyncio.run(host.task_instruction())
    assert 'untrusted' in text and 'Inspect sample' in text and len(text) < 850


def test_runtime_uses_three_tools_and_request_instructions():
    source = Path('coat_sim/agent/runtime.py').read_text()
    assert 'toolkit_status, toolkit_catalog, toolkit_action' in source
    assert 'task_context = await toolkit.task_instruction()' in source
    assert 'InstructionPart(content=task_context)' in source
    assert 'portable_toolkit' in source


def test_actual_offline_runtime_exposes_only_compact_toolkit_entrypoints():
    from tests.test_assistant_discussion_mode import _actual_run
    rpc, returned, observed = asyncio.run(_actual_run([
        ('toolkit_status', {}, 'toolkit_status'),
        ('toolkit_catalog', {}, 'toolkit_catalog'),
        ('toolkit_action', {'tool_name': 'read', 'arguments': {'path': 'anything'}}, 'toolkit_action'),
    ]))
    assert rpc == []
    assert {'toolkit_status', 'toolkit_catalog', 'toolkit_action'} <= observed[0]
    assert 'read' not in observed[0] and 'pip_install' not in observed[0]
    assert returned[0].content['data']['enabled'] is True
    assert returned[0].content['data']['workspace']['kind'] == 'private'
    names = {tool['name'] for tool in returned[1].content['data']['tools']}
    assert {'read', 'write', 'delete', 'restore', 'mkdir', 'memory_upsert'} <= names
    assert not names.intersection({'run', 'sh', 'pwsh', 'pip_install', 'sftp_hosts'})
    assert returned[2].content['error']['code'] == 'io_error'


def test_active_profile_narrows_scope_and_persona_is_untrusted(tmp_path):
    config = configured(tmp_path,
        assistant_toolkit_capabilities=['preset', 'tasks', 'fs'],
        assistant_toolkit_profile_capabilities=['preset', 'tasks', 'fs'])
    host = adapter(tmp_path, config=config, authority=lambda: {'allowed': True, 'mode': 'operate'})
    scope = host.task_scope()
    async def check():
        assert (await host.action('preset_create', {**scope, 'expected_revision': 0, 'id': 'narrow',
                'capabilities': ['preset', 'tasks'], 'persona': 'Custom preference'}))['ok']
        assert (await host.action('preset_inject', {**scope, 'expected_revision': 1, 'id': 'narrow'}))['ok']
        instruction = await host.task_instruction()
        assert 'untrusted' in instruction and 'Custom preference' in instruction
        assert host.status()['data']['profile_status'] == 'active'
        assert 'fs' not in host.context.allowed_capabilities
        assert (await host.action('read', {'path': 'x'}))['error']['code'] == 'capability_disabled'
        assert (await host.action('preset_eject', {**scope, 'expected_revision': 2}))['ok']
        assert 'Custom preference' not in await host.task_instruction()
        assert 'fs' not in host.context.allowed_capabilities  # no mid-request scope escalation
        next_turn = adapter(tmp_path, config=config)
        await next_turn.task_instruction()
        assert 'fs' in next_turn.context.allowed_capabilities
    asyncio.run(check())


def test_image_artifact_maps_to_bounded_native_media(tmp_path):
    from PIL import Image
    from coat_sim.agent.toolkit.pydantic_adapter import model_result
    from coat_sim.agent.tool_dispatch import MCPMultimodalResult
    Image.new('RGB', (2, 2)).save(tmp_path / 'small.png')
    host = adapter(tmp_path)
    result = asyncio.run(host.action('read_image', {'path': 'small.png'}))
    mapped = asyncio.run(model_result(result, host.context))
    assert isinstance(mapped, MCPMultimodalResult)
    assert mapped.value == result
    assert mapped.content[0].media_type == 'image/png'
    result['data']['sha256'] = 'changed'
    assert asyncio.run(model_result(result, host.context))['error']['code'] == 'artifact_unavailable'


def test_workspace_mutations_serialize_across_adapter_instances(tmp_path):
    import time
    registry = ToolRegistry()
    active = 0
    maximum = 0
    def handler(context, args):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        time.sleep(0.03)
        active -= 1
        return {}
    registry.register(ToolSpec('mutate', 'fs', '', {'type': 'object', 'properties': {}}, 'write'), handler)
    hosts = [adapter(tmp_path, registry, authority=lambda: {'allowed': True, 'mode': 'operate'}) for _ in range(2)]
    async def check():
        results = await asyncio.gather(*(host.action('mutate', {}) for host in hosts))
        assert all(result['ok'] for result in results)
    asyncio.run(check())
    assert maximum == 1


def test_guard_status_is_mandatory_even_when_toolkit_disabled(tmp_path):
    host = adapter(tmp_path, config={})
    guard = next(row for row in host.status()['data']['capabilities'] if row['capability'] == 'guard')
    assert guard == {'capability': 'guard', 'available': True, 'status': 'mandatory_policy',
                     'kind': 'policy', 'configured': False, 'disablable': False, 'callable_tools': 0}


def test_large_png_reaches_offline_model_as_native_media(tmp_path):
    from PIL import Image
    from pydantic_ai import Agent
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.messages import BinaryContent, ModelResponse, ToolCallPart, TextPart
    from coat_sim.agent.runtime import BudgetAndTools
    from coat_sim.agent.tool_results import ToolResultArchive
    from coat_sim.agent.tool_dispatch import ProjectGateway
    from coat_sim.agent.toolkit.pydantic_adapter import model_result
    Image.new('RGB', (800, 600)).save(tmp_path / 'large.png', compress_level=0)
    size = (tmp_path / 'large.png').stat().st_size
    assert 1024 * 1024 < size < 8 * 1024 * 1024
    host = adapter(tmp_path)
    meter = BudgetAndTools.__new__(BudgetAndTools)
    meter.archive = ToolResultArchive(tmp_path / 'archive')
    meter.shared = {}
    gateway = ProjectGateway(None, set(), set(), str, shared=meter.shared.setdefault('gateway_guard', {}))
    observed: list[BinaryContent] = []
    def respond(messages, info):
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart('image_tool', {}, tool_call_id='image-call')])
        for message in messages:
            for part in message.parts:
                content = getattr(part, 'content', None)
                if isinstance(content, list):
                    observed.extend(item for item in content if isinstance(item, BinaryContent))
        return ModelResponse(parts=[TextPart('image received')])
    async def image_tool() -> object:
        """Read the synthetic PNG through the portable adapter."""
        result = await host.action('read_image', {'path': 'large.png'})
        mapped = await model_result(result, host.context)
        gateway._reserve_image_budget(mapped)
        return meter._bounded_result('toolkit_action', {'tool_name': 'read_image'}, mapped)
    async def check():
        result = await Agent(FunctionModel(respond), tools=[image_tool]).run('Inspect the image')
        assert result.output == 'image received'
    asyncio.run(check())
    assert len(observed) == 1 and len(observed[0].data) == size
    assert meter.shared['gateway_guard']['mcp_image_bytes'] == size


def test_image_mapping_rejects_over_limit_and_stale_hash(tmp_path):
    import hashlib
    from coat_sim.agent.toolkit.images import MAX_IMAGE_BYTES
    from coat_sim.agent.toolkit.pydantic_adapter import model_result
    path = tmp_path / 'overlimit.png'
    path.write_bytes(b'x' * (MAX_IMAGE_BYTES + 1))
    host = adapter(tmp_path)
    data = {'type': 'image_artifact', 'path': str(path), 'media_type': 'image/png',
            'bytes': MAX_IMAGE_BYTES + 1, 'sha256': 'untrusted'}
    result = {'ok': True, 'data': data, 'error': None}
    assert asyncio.run(model_result(result, host.context))['error']['code'] == 'artifact_unavailable'
    path.write_bytes(b'changed')
    data.update(bytes=7, sha256=hashlib.sha256(b'original').hexdigest())
    assert asyncio.run(model_result(result, host.context))['error']['code'] == 'artifact_unavailable'
