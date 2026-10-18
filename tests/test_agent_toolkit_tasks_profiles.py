"""Offline synthetic state/profile portability and safety contracts."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from coat_sim.agent.toolkit import ToolContext, ToolRegistry
from coat_sim.agent.toolkit.tasks import register_tasks
from coat_sim.agent.toolkit.profiles import register_profiles, effective_capabilities


def setup(root):
    registry = ToolRegistry()
    register_tasks(registry)
    register_profiles(registry)
    ctx = ToolContext(allowed_capabilities=frozenset({'tasks', 'preset', 'fs'}),
                      allowed_roots=(root,), authorize=lambda spec, args: True)
    args = {'state_root': str(root), 'project_id': 'optical', 'scope': 'project'}
    return registry, ctx, args


def invoke(registry, ctx, args, name, **extra):
    return registry.invoke(name, {**args, **extra}, ctx)


def test_restart_cas_and_user_ledger(tmp_path):
    user_file = tmp_path / 'TASKS.md'
    user_file.write_text('User content must survive')
    reg, ctx, args = setup(tmp_path)
    first = invoke(reg, ctx, args, 'plan_init', expected_revision=0, goal='A',
                   tasks=[{'id': '1', 'text': 'Research'}])
    assert first['ok'] and first['data']['revision'] == 1
    reg, ctx, args = setup(tmp_path)
    restored = invoke(reg, ctx, args, 'task_show')
    assert 'Research' in restored['data']['panel']
    assert invoke(reg, ctx, args, 'task_update', expected_revision=1, id='1', status='doing')['ok']
    assert invoke(reg, ctx, args, 'stage_set', expected_revision=1, stage='stale')['error']['code'] == 'conflict'
    assert invoke(reg, ctx, args, 'plan_init', expected_revision=2, goal='replace', tasks=[])['error']['code'] == 'conflict'
    assert user_file.read_text() == 'User content must survive'


def test_scopes_and_bounds(tmp_path):
    reg, ctx, args = setup(tmp_path)
    assert invoke(reg, ctx, args, 'plan_init', expected_revision=0, goal='g'*4000,
                  tasks=[{'id': str(i), 'text': 'x'*4000} for i in range(10)])['ok']
    show = invoke(reg, ctx, args, 'task_show')['data']
    assert len(show['panel']) <= 700 and len(show['detail']) <= 4000 and show['truncated']
    for other in [dict(args, project_id='other'), dict(args, scope='session', session_id='one'),
                  dict(args, scope='session', session_id='two')]:
        assert invoke(reg, ctx, other, 'task_show')['data']['revision'] == 0
    assert invoke(reg, ctx, dict(args, scope='session'), 'task_show')['error']['code'] == 'invalid_arguments'
    assert invoke(reg, ctx, dict(args, session_id='bad'), 'task_show')['error']['code'] == 'invalid_arguments'


def test_profile_intersection_and_declarative_selection(tmp_path):
    reg, ctx, args = setup(tmp_path)
    create = invoke(reg, ctx, args, 'preset_create', expected_revision=0, id='review',
                    capabilities=['fs', 'run'], persona='Do not execute: $(touch injected)')
    assert create['ok']
    assert create['data']['profiles'][0]['effective_capabilities'] == ['fs']
    assert not effective_capabilities([], {'capabilities': ['fs']})
    assert not effective_capabilities(['fs'], {'capabilities': []})
    assert invoke(reg, ctx, args, 'preset_inject', expected_revision=1, id='review')['data']['active'] == 'review'
    assert not (tmp_path / 'injected').exists()
    reg, ctx, args = setup(tmp_path)
    assert invoke(reg, ctx, args, 'preset_list')['data']['active'] == 'review'
    assert invoke(reg, ctx, args, 'preset_eject', expected_revision=2)['data']['active'] == ''
    assert invoke(reg, ctx, args, 'preset_delete', expected_revision=3, id='review')['data']['profiles'] == []


def test_corrupt_state_preserved(tmp_path):
    reg, ctx, args = setup(tmp_path)
    invoke(reg, ctx, args, 'plan_init', expected_revision=0, goal='A', tasks=[])
    path = next(tmp_path.glob('*.json'))
    state = json.loads(path.read_text())
    state['version'] = 77
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    result = invoke(reg, ctx, args, 'stage_set', expected_revision=1, stage='B')
    assert result['error']['code'] == 'invalid_state'
    assert path.read_bytes() == before


def test_concurrent_writers_only_one_commits(tmp_path):
    reg, ctx, args = setup(tmp_path)
    invoke(reg, ctx, args, 'plan_init', expected_revision=0, goal='A', tasks=[])
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: invoke(reg, ctx, args, 'stage_set', expected_revision=1,
                                                stage=str(i)), range(8)))
    assert sum(result['ok'] for result in results) == 1
    assert all(r['ok'] or r['error']['code'] == 'conflict' for r in results)


def test_policy_and_malformed_args(tmp_path):
    reg, ctx, args = setup(tmp_path)
    denied = ToolContext(allowed_capabilities=frozenset({'tasks'}), allowed_roots=(tmp_path,))
    assert invoke(reg, denied, args, 'task_show')['error']['code'] == 'authorization_denied'
    assert invoke(reg, ctx, dict(args, state_root=str(tmp_path.parent)), 'task_show')['error']['code'] == 'path_denied'
    assert invoke(reg, ctx, args, 'preset_create', expected_revision=0, id='p', capabilities=[],
                  command='anything')['error']['code'] == 'invalid_arguments'
    cancelled = ToolContext(allowed_capabilities=frozenset({'tasks'}), allowed_roots=(tmp_path,),
                            authorize=lambda *_: True, cancelled=lambda: True)
    assert invoke(reg, cancelled, args, 'task_show')['error']['code'] == 'cancelled'
    assert not list(tmp_path.iterdir())


def test_lock_release_and_transaction_rollback(tmp_path):
    from coat_sim.agent.toolkit import _state, ToolkitError
    reg, ctx, args = setup(tmp_path)
    invoke(reg, ctx, args, 'plan_init', expected_revision=0, goal='A', tasks=[])
    path, _ = _state.location(ctx, args, 'tasks')
    before = path.read_bytes()
    with _state.locked(ctx, path):
        busy = invoke(reg, ctx, args, 'stage_set', expected_revision=1, stage='busy')
        assert busy['error']['code'] == 'conflict'
    assert path.read_bytes() == before
    def broken(data):
        data['goal'] = 'must not commit'
        raise ToolkitError('cancelled', 'Stopped')
    with pytest.raises(ToolkitError):
        _state.mutate(ctx, dict(args, expected_revision=1), 'tasks', broken)
    assert path.read_bytes() == before
    assert invoke(reg, ctx, args, 'stage_set', expected_revision=1, stage='released')['ok']


def test_symlink_state_and_invalid_data_are_preserved(tmp_path):
    from coat_sim.agent.toolkit import _state
    reg, ctx, args = setup(tmp_path)
    path, _ = _state.location(ctx, args, 'tasks')
    other = tmp_path / 'other.json'
    other.write_text('private')
    try:
        path.symlink_to(other)
    except OSError:
        pytest.skip('Symlinks unavailable')
    assert invoke(reg, ctx, args, 'task_show')['error']['code'] == 'path_denied'
    assert other.read_text() == 'private'
    path.unlink()
    invoke(reg, ctx, args, 'plan_init', expected_revision=0, goal='A', tasks=[])
    state = json.loads(path.read_text())
    state['data']['tasks'] = [{'id': '1', 'text': 'x', 'status': 'executing-shell'}]
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    assert invoke(reg, ctx, args, 'task_show')['error']['code'] == 'invalid_state'
    assert invoke(reg, ctx, args, 'stage_set', expected_revision=1, stage='B')['error']['code'] == 'invalid_state'
    assert path.read_bytes() == before
