"""Real Qt bridge provenance, with fake worker/settings and no runtime actions."""
from copy import deepcopy

from tests.test_assistant_token_budget_contract import real_bridge as _real_bridge_fixture
from coat_sim.ui import assistant

# Re-export the existing local fixture without changing its scope or cleanup.
real_bridge = _real_bridge_fixture


def authority_rows(bridge):
    with bridge._store._db() as db:
        modes = [dict(row) for row in db.execute('SELECT * FROM assistant_ui_session_modes')]
        requests = [dict(row) for row in db.execute('SELECT * FROM assistant_ui_requests')]
    return modes, requests


def test_ui_binds_actual_override_mode_before_worker_and_received_log(real_bridge):
    bridge, worker = real_bridge
    assert bridge.taskMode == 'chat'
    assert bridge._start_message({'session_id': bridge._session, 'prompt': 'offline explicit debug request',
        'attachments': [], 'config_overrides': {'task_mode': 'research'}})
    command, = worker.stdin.commands()
    modes, requests = authority_rows(bridge)
    assert command['config']['task_mode'] == 'research'
    assert requests[0]['mode'] == 'research'
    assert modes[0]['mode'] == 'chat'  # Overrides cannot expand the current selector.
    assert requests[0]['request_id'] == command['request_id']
    assert requests[0]['generation'] == modes[0]['generation']
    received = [event for event in bridge._store.task_events(bridge._session)
                if event['payload'].get('stage') == 'received']
    assert received[0]['payload']['mode'] == 'research'


def test_ui_global_mode_revokes_hidden_conversation_countdowns(real_bridge, monkeypatch):
    bridge, _worker = real_bridge
    original = bridge._session
    bridge.newConversation()
    hidden = original
    monkeypatch.setattr(assistant, 'save_ui_settings', lambda value: deepcopy(value))
    bridge.setTaskMode('operate')
    modes, _ = authority_rows(bridge)
    assert {row['mode'] for row in modes} == {'operate'}
    by_session = {row['session']: row for row in modes}
    bridge._store.bind_ui_request(hidden, 'old-hidden-operation', 'operate')
    bridge.setTaskMode('chat')
    modes, requests = authority_rows(bridge)
    assert {row['mode'] for row in modes} == {'chat'}
    assert all(row['generation'] == by_session[row['session']]['generation'] + 1 for row in modes)
    assert requests[0]['generation'] < next(row['generation'] for row in modes if row['session'] == hidden)


def test_reload_configuration_updates_trusted_authority_before_configure(real_bridge, monkeypatch):
    bridge, worker = real_bridge
    bridge._config['assistant_task_mode'] = 'operate'
    bridge._store.set_ui_session_mode(bridge._session, 'operate')
    bridge._store.bind_ui_request(bridge._session, 'running-operation', 'operate')
    loaded = {**bridge._config, 'assistant_task_mode': 'chat'}
    monkeypatch.setattr(assistant, 'load_ui_settings', lambda: deepcopy(loaded))
    observed = []
    bridge.configurationChanged.connect(lambda: observed.append(authority_rows(bridge)[0][0]['mode']))
    bridge.reload_configuration()
    assert observed == ['chat']
    modes, requests = authority_rows(bridge)
    assert modes[0]['generation'] > requests[0]['generation']
    command, = worker.stdin.commands()
    assert command['op'] == 'configure'
    assert command['config']['task_mode'] == 'chat'
