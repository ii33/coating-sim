"""Foreground permission follows verified, sequential exit leases."""
import pytest
from coat_sim import assistant_process as module
from coat_sim.assistant_process import ApplicationClientRegistry, _Client
from coat_sim.ui.control import UiControlError
from coat_sim.ui.project_exit_client import ProjectExitClient
pytestmark = pytest.mark.contract

def client(name, pid):
    return _Client(client_id=name, endpoint={'pid': pid, 'root': '/runtime', 'role': 'project_host'}, generation=name, process_start='start-' + str(pid), role='project_host', projects=[])

def test_first_gesture_grants_only_first_exact_process_and_rejects_reuse(monkeypatch):
    registry = ApplicationClientRegistry()
    a, b = (client('a', 501), client('b', 502))
    registry._clients = {'a': a, 'b': b}
    states = {501: ('alive', a.process_start), 502: ('alive', b.process_start)}
    monkeypatch.setattr(module, '_process_identity', lambda pid: states[pid])
    granted = []
    monkeypatch.setattr(module, '_allow_foreground_process', lambda pid: granted.append(pid) or True)
    assert registry.prepare_exit_activation() == 'a'
    assert granted == [501]
    states[501] = ('alive', None)
    with pytest.raises(UiControlError, match='verify exit'):
        registry.prepare_exit_activation()
    assert granted == [501]
    registry._exiting = True
    with pytest.raises(UiControlError, match='exit is in progress'):
        registry.prepare_exit_activation()

def test_exit_relays_after_ready_before_next_prepare_and_cancels_on_decline(monkeypatch):
    registry = ApplicationClientRegistry()
    a, b = (client('a', 501), client('b', 502))
    registry._clients = {'a': a, 'b': b}
    monkeypatch.setattr(module, '_process_identity', lambda pid: ('alive', 'start-' + str(pid)))
    monkeypatch.setattr(registry, '_status', lambda target: {})
    calls = []

    def call(target, operation, **args):
        calls.append((target.client_id, operation))
        if operation == 'lifecycle_handoff':
            assert args == {'exit_id': 'exit', 'client_generation': 'a', 'permit_id': 'permit', 'target_pid': 502, 'target_start': 'start-502'}
            return {'generation': 'a', 'granted': True}
        if operation == 'lifecycle_prepare':
            return {'generation': target.generation, 'state': 'ready' if target is a else 'declined', 'permit_id': 'permit'}
        if operation == 'lifecycle_cancel':
            return {}
        raise AssertionError(operation)
    monkeypatch.setattr(registry, '_call', call)
    result = registry.request_exit_all('exit', foreground_client_id='a')
    assert result['state'] == 'declined' and result['committed_clients'] == []
    assert calls == [('a', 'lifecycle_prepare'), ('a', 'lifecycle_handoff'), ('b', 'lifecycle_prepare'), ('a', 'lifecycle_cancel'), ('b', 'lifecycle_cancel')]

@pytest.mark.parametrize('failure', ['pending', 'permit', 'exit', 'generation', 'expired', 'target_reused', 'pid_any', 'pid_bool'])
def test_handoff_rejects_invalid_exit_or_target_without_os_grant(monkeypatch, failure):
    import coat_sim.core.process_identity as identities
    clock = [0.0]
    gate = ProjectExitClient('generation', prepare=lambda: 'pending' if failure == 'pending' else 'ready', commit=lambda: True, cancel=lambda: None, clock=lambda: clock[0])
    args = {'exit_id': 'exit', 'client_generation': 'generation'}
    reply = gate.dispatch('lifecycle_prepare', args)
    args.update(permit_id=reply['permit_id'], target_pid=502, target_start='start-502')
    if failure == 'permit':
        args['permit_id'] = 'wrong'
    if failure == 'exit':
        args['exit_id'] = 'wrong'
    if failure == 'generation':
        args['client_generation'] = 'wrong'
    if failure == 'expired':
        clock[0] = 121
    if failure == 'pid_any':
        args['target_pid'] = -1
    if failure == 'pid_bool':
        args['target_pid'] = True
    monkeypatch.setattr(identities, '_process_identity', lambda pid: ('alive', 'reused' if failure == 'target_reused' else 'start-502'))
    grants = []
    monkeypatch.setattr(module, '_allow_foreground_process', lambda pid: grants.append(pid) or True)
    with pytest.raises(UiControlError):
        gate.dispatch('lifecycle_handoff', args)
    assert grants == []

def test_valid_ready_handoff_does_not_close_or_change_permit(monkeypatch):
    import coat_sim.core.process_identity as identities
    closed = []
    gate = ProjectExitClient('generation', prepare=lambda: 'ready', commit=lambda: closed.append(True) or True, cancel=lambda: None)
    args = {'exit_id': 'exit', 'client_generation': 'generation'}
    reply = gate.dispatch('lifecycle_prepare', args)
    monkeypatch.setattr(identities, '_process_identity', lambda pid: ('alive', 'start-502'))
    monkeypatch.setattr(module, '_allow_foreground_process', lambda pid: pid == 502)
    assert gate.dispatch('lifecycle_handoff', args | {'permit_id': reply['permit_id'], 'target_pid': 502, 'target_start': 'start-502'}) == {'generation': 'generation', 'granted': True}
    assert closed == []
    assert gate.dispatch('lifecycle_prepare', args)['permit_id'] == reply['permit_id']

def test_changed_first_client_aborts_before_any_prepare(monkeypatch):
    registry = ApplicationClientRegistry()
    b = client('b', 502)
    registry._clients = {'b': b}
    monkeypatch.setattr(module, '_process_identity', lambda pid: ('alive', b.process_start))
    calls = []
    monkeypatch.setattr(registry, '_call', lambda *a, **kw: calls.append(a))
    result = registry.request_exit_all('exit', foreground_client_id='a')
    assert result['state'] == 'failed' and result['code'] == 'stale_client'
    assert result['committed_clients'] == [] and calls == [] and (not registry._exiting)

def test_denied_first_gesture_never_admits_exit(monkeypatch):
    registry = ApplicationClientRegistry()
    a = client('a', 501)
    registry._clients = {'a': a}
    monkeypatch.setattr(module, '_process_identity', lambda pid: ('alive', a.process_start))
    monkeypatch.setattr(module, '_allow_foreground_process', lambda pid: False)
    with pytest.raises(UiControlError) as exc:
        registry.prepare_exit_activation()
    assert exc.value.code == 'foreground_denied' and (not registry._exiting)
