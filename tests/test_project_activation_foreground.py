"""Mocked Windows handoff: focus permission never substitutes for a window reply."""
from copy import deepcopy
import ctypes
from types import SimpleNamespace
from unittest.mock import Mock
from typing import Any

import pytest

from coat_sim import assistant_process as module
from coat_sim.assistant_process import AssistantProcessClient
from coat_sim.project_activation import activate_shared_project, activation_target
from coat_sim.ui.control import UiControlError


@pytest.fixture
def grant_client(tmp_path, monkeypatch):
    client = AssistantProcessClient(tmp_path)
    row: dict[str, Any] = {'client_id': 'owner', 'pid': 12345, 'generation': 'client-gen', 'state': 'alive',
           'process_start': 'created-once', 'project_sessions': [{'project_id': 'project', 'generation': 'session-gen',
           'root': str(tmp_path), 'project_file': str(tmp_path / 'project.coatproj')}]}
    target = activation_target(row, row['project_sessions'][0])
    monkeypatch.setattr(client, 'list_clients', lambda: [deepcopy(row)])
    monkeypatch.setattr(module, '_process_identity', lambda pid: ('alive', 'created-once'))
    # Replace only this module's platform view, not the process-wide os.name;
    # the real ctypes wrapper is exercised on Linux without loading user32.
    monkeypatch.setattr(module, 'os', SimpleNamespace(name='nt'))
    grant = Mock(return_value=1)
    monkeypatch.setitem(vars(ctypes), 'WinDLL', Mock(return_value=SimpleNamespace(AllowSetForegroundWindow=grant)))
    monkeypatch.setattr(client, 'trace', Mock())
    return client, target, grant


def _reply(target):
    return {'activated': True, 'instance_id': 'one-window',
            **{k: target[k] for k in ('client_generation', 'project_id', 'session_generation')}}


def test_exact_owner_granted_before_activation_ipc(grant_client, monkeypatch):
    client, target, grant = grant_client
    def request(operation, **arguments):
        grant.assert_called_once_with(12345)
        assert operation == 'activate_project' and arguments == target
        return _reply(target)
    monkeypatch.setattr(client, 'request', request)
    assert activate_shared_project(client, target)['activated']
    from ctypes import wintypes
    assert grant.argtypes == [wintypes.DWORD]
    assert grant.restype is wintypes.BOOL
    client.trace.assert_not_called()


@pytest.mark.parametrize('field', ['client_id', 'client_generation', 'project_id', 'session_generation', 'root', 'project_file'])
def test_changed_target_never_grants_or_activates(grant_client, monkeypatch, field):
    client, target, grant = grant_client
    target[field] += '-changed'
    request = Mock()
    monkeypatch.setattr(client, 'request', request)
    with pytest.raises(UiControlError, match='owner has changed'):
        activate_shared_project(client, target)
    grant.assert_not_called()
    request.assert_not_called()


@pytest.mark.parametrize('identity', [('alive', 'reused'), ('dead', None), ('unknown', None)])
def test_reused_or_unknown_pid_never_grants_or_activates(grant_client, monkeypatch, identity):
    client, target, grant = grant_client
    monkeypatch.setattr(module, '_process_identity', lambda pid: identity)
    request = Mock()
    monkeypatch.setattr(client, 'request', request)
    with pytest.raises(UiControlError) as error:
        activate_shared_project(client, target)
    assert error.value.code == 'process_identity_unknown'
    grant.assert_not_called()
    request.assert_not_called()


def test_denied_grant_still_requests_exact_owner_and_records_diagnostic(grant_client, monkeypatch):
    client, target, grant = grant_client
    grant.return_value = 0
    request = Mock(return_value=_reply(target))
    monkeypatch.setattr(client, 'request', request)
    assert activate_shared_project(client, target) == _reply(target)
    grant.assert_called_once_with(12345)
    request.assert_called_once_with('activate_project', **target)
    client.trace.assert_called_once_with('project_activation_foreground_not_granted', client_pid=12345,
        operation='activate_project', ok=False, reason='foreground_grant_denied')


@pytest.mark.parametrize('code', ['ui_unavailable', 'stale_project', 'activation_unconfirmed'])
def test_denied_grant_never_hides_ipc_or_window_confirmation_errors(grant_client, monkeypatch, code):
    client, target, grant = grant_client
    grant.return_value = 0
    request = Mock(side_effect=UiControlError(code, 'owner did not confirm its visible window'))
    monkeypatch.setattr(client, 'request', request)
    with pytest.raises(UiControlError) as error:
        activate_shared_project(client, target)
    assert error.value.code == code
    assert 'owner did not confirm' in str(error.value)
    request.assert_called_once_with('activate_project', **target)


@pytest.mark.parametrize('field,value', [('activated', False), ('instance_id', ''),
    ('project_id', 'other-project'), ('session_generation', 'other-session'),
    ('client_generation', 'other-client')])
def test_denied_grant_never_weakens_reply_identity_checks(grant_client, monkeypatch, field, value):
    client, target, grant = grant_client
    grant.return_value = 0
    reply = _reply(target) | {field: value}
    monkeypatch.setattr(client, 'request', Mock(return_value=reply))
    with pytest.raises(UiControlError) as error:
        activate_shared_project(client, target)
    assert error.value.code == 'activation_unconfirmed'


def test_non_windows_requires_no_native_grant(grant_client, monkeypatch):
    client, target, grant = grant_client
    monkeypatch.setattr(module, 'os', SimpleNamespace(name='posix'))
    monkeypatch.setattr(client, 'list_clients', Mock(side_effect=AssertionError('no Windows preflight')))
    monkeypatch.setattr(client, 'request', Mock(return_value=_reply(target)))
    assert activate_shared_project(client, target)['activated']
    grant.assert_not_called()
