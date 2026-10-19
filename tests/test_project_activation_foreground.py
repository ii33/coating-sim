"""Foreground grants stay bound to authenticated project/process identities."""
from copy import deepcopy
from unittest.mock import Mock
from typing import Any
import os
import pytest
from coat_sim import assistant_process as module
from coat_sim.assistant_process import AssistantProcessClient
from coat_sim.project_activation import activate_shared_project, activation_target
from coat_sim.ui.control import UiControlError

@pytest.fixture
def grant_client(tmp_path, monkeypatch):
    client = AssistantProcessClient(tmp_path)
    row: dict[str, Any] = {'client_id':'owner', 'pid':12345, 'generation':'client-gen', 'state':'alive',
           'process_start':'created-once', 'project_sessions':[{'project_id':'project', 'generation':'session-gen',
           'root':str(tmp_path), 'project_file':str(tmp_path/'project.coatproj')}]}
    target = activation_target(row, row['project_sessions'][0])
    monkeypatch.setattr(client, 'list_clients', lambda: [deepcopy(row)])
    monkeypatch.setattr(module, '_process_identity', lambda pid: ('alive', 'created-once'))
    grant = Mock(return_value=True)
    monkeypatch.setattr(module, '_allow_foreground_process', grant)
    return client, target, grant

@pytest.mark.skipif(os.name != 'nt', reason='Windows foreground handoff')
def test_exact_owner_granted_before_activation_ipc(grant_client, monkeypatch):
    client, target, grant = grant_client
    def request(operation, **arguments):
        grant.assert_called_once_with(12345)
        assert operation == 'activate_project' and arguments == target
        return {'activated':True,'instance_id':'one-window', **{k:target[k] for k in ('client_generation','project_id','session_generation')}}
    monkeypatch.setattr(client, 'request', request)
    assert activate_shared_project(client, target)['activated']

@pytest.mark.skipif(os.name != 'nt', reason='Windows foreground handoff')
@pytest.mark.parametrize('field', ['client_id','client_generation','project_id','session_generation','root','project_file'])
def test_changed_target_never_grants(grant_client, field):
    client, target, grant = grant_client
    target[field] += '-changed'
    with pytest.raises(UiControlError, match='owner has changed'):
        client.prepare_project_activation(target)
    grant.assert_not_called()

@pytest.mark.skipif(os.name != 'nt', reason='Windows foreground handoff')
@pytest.mark.parametrize('identity', [('alive','reused'),('dead',None),('unknown',None)])
def test_reused_or_unknown_pid_never_grants(grant_client, monkeypatch, identity):
    client, target, grant = grant_client
    monkeypatch.setattr(module, '_process_identity', lambda pid: identity)
    with pytest.raises(UiControlError):
        client.prepare_project_activation(target)
    grant.assert_not_called()

@pytest.mark.skipif(os.name != 'nt', reason='Windows foreground handoff')
def test_denied_grant_never_sends_activation(grant_client, monkeypatch):
    client, target, grant = grant_client
    grant.return_value = False
    request = Mock()
    monkeypatch.setattr(client, 'request', request)
    with pytest.raises(UiControlError) as error:
        activate_shared_project(client,target)
    assert error.value.code == 'foreground_denied'
    request.assert_not_called()
