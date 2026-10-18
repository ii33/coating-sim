"""Portable boundary checks: fake adapters only, no network/process/install."""
import json
import pytest
from coat_sim.agent.toolkit import ToolContext, ToolRegistry, ToolkitError
from coat_sim.agent.toolkit.execution import register_execution_tools, guard_shell
from coat_sim.agent.toolkit.environment import register_environment_tools, pip_argv
from coat_sim.agent.toolkit.network import register_network_tools, network_route
from coat_sim.agent.toolkit.remote import register_remote_tools


@pytest.fixture
def registry():
    registry = ToolRegistry()
    for register in (register_execution_tools, register_environment_tools, register_network_tools, register_remote_tools):
        register(registry)
    return registry


def context(tmp_path, **providers):
    return ToolContext(frozenset({'run', 'sh', 'pwsh', 'env', 'proxy', 'sftp'}), (tmp_path,), lambda spec, args: True, providers=providers)


def host():
    return {'hostname': 'example.invalid', 'known_hosts': 'external-known-hosts', 'strict_host_key': True,
            'password_env': 'SSH_SECRET', 'key_ref': 'keychain:ssh', 'roots': ['/data']}


def test_missing_adapters_are_unavailable(registry, tmp_path):
    ctx = context(tmp_path)
    for tool, args in [('run', {'argv': ['python', '-V']}), ('net_check', {'urls': ['https://github.com']})]:
        assert registry.invoke(tool, args, ctx)['error']['code'] == 'provider_unavailable'
    assert registry.invoke('env_setup', {}, ctx)['error']['code'] == 'dependency_unavailable'
    assert registry.invoke('sftp_hosts', {}, ctx)['data'] == {'hosts': []}


def test_default_denial_and_cancel_prevent_provider_calls(registry, tmp_path):
    calls = []
    def provider(request, ctx):
        return calls.append(request)
    ctx = ToolContext(frozenset({'run'}), (tmp_path,), providers={'execution': provider})
    assert registry.invoke('run', {'argv': ['python']}, ctx)['error']['code'] == 'authorization_denied'
    ctx = ToolContext(frozenset({'run'}), (tmp_path,), lambda s, a: True, lambda: True, {'execution': provider})
    assert registry.invoke('run', {'argv': ['python']}, ctx)['error']['code'] == 'cancelled'
    assert not calls


def test_execution_argv_cwd_budget_and_timeout(registry, tmp_path):
    calls = []
    def adapter(request, ctx):
        calls.append(request)
        return {'exit_code': 2, 'stdout': 'ééé', 'stderr': 'bad'}
    ctx = context(tmp_path, execution=adapter)
    result = registry.invoke('run', {'argv': ['python', 'a b'], 'max_output_bytes': 5, 'timeout_ms': 10}, ctx)
    assert result['data'] == {'exit_code': 2, 'stdout': 'éé', 'stderr': '', 'truncated': True}
    assert calls[0]['argv'] == ['python', 'a b']
    assert calls[0]['shell'] is False and calls[0]['timeout_ms'] == 10
    assert registry.invoke('run', {'argv': ['python'], 'cwd': '../escape'}, ctx)['error']['code'] == 'path_denied'
    assert len(calls) == 1


def test_shell_hygiene_and_pwsh_version_probe(registry, tmp_path):
    for command in ['cmd /c dir', r'C:\Windows\powershell.exe -c x', 'ssh host powershell -c x']:
        with pytest.raises(ToolkitError):
            guard_shell(command)
    calls = []
    def adapter(request, ctx):
        calls.append(request)
        return {'exit_code': 0, 'stdout': '5\n'}
    ctx = context(tmp_path, execution=adapter, execution_config={'pwsh': '/opt/pwsh', 'bash': '/bin/bash'})
    assert registry.invoke('pwsh', {'command': 'Get-Date'}, ctx)['error']['code'] == 'dependency_unavailable'
    assert len(calls) == 1
    assert registry.invoke('sh', {'command': 'printf test'}, ctx)['ok']
    assert calls[1]['argv'] == ['/bin/bash', '--noprofile', '--norc', '-c', 'printf test']


def test_environment_uses_selected_interpreter_and_install_verifies(registry, tmp_path):
    calls = []
    def adapter(request, ctx):
        calls.append(request)
        output = json.dumps({'executable': '/actual/python', 'version': '3.12', 'pip': True}) if '-c' in request['argv'] else 'Name: six'
        return {'exit_code': 0, 'stdout': output}
    ctx = context(tmp_path, execution=adapter, execution_config={'python': '/selected/python'})
    data = registry.invoke('env_setup', {}, ctx)['data']
    assert data['selected_python'] == '/selected/python' and data['python']['executable'] == '/actual/python'
    result = registry.invoke('pip_install', {'packages': ['six==1.17.0'], 'index_url': 'https://pypi.org/simple'}, ctx)
    assert result['data']['verified']
    assert all(call['argv'][0] == '/selected/python' for call in calls)
    assert calls[-2]['argv'][1:4] == ['-m', 'pip', 'install']
    assert '--trusted-host' not in calls[-2]['argv']
    assert calls[-1]['argv'] == ['/selected/python', '-m', 'pip', 'show', 'six']


def test_missing_pip_and_bad_packages_never_install(registry, tmp_path):
    calls = []
    def adapter(request, ctx):
        calls.append(request)
        return {'exit_code': 0, 'stdout': json.dumps({'executable': '/python', 'pip': False})}
    ctx = context(tmp_path, execution=adapter, execution_config={'python': '/python'})
    result = registry.invoke('pip_install', {'packages': ['six']}, ctx)
    assert result['error']['code'] == 'dependency_unavailable' and len(calls) == 1
    for package in ['--trusted-host=x', 'https://site/file', '../local', 'six;echo bad']:
        with pytest.raises(ToolkitError):
            pip_argv('/python', [package])
    with pytest.raises(ToolkitError):
        pip_argv('/python', ['six'], 'http://pypi.org/simple')


def test_network_routes_do_not_mutate_settings(registry, tmp_path):
    calls = []
    def adapter(request, ctx):
        calls.append(request)
        return {'reachable': True, 'status_code': 200, 'latency_ms': 5, 'secret': 'ignored'}
    config = {'github_proxy_url': 'http://127.0.0.1:10808'}
    ctx = context(tmp_path, network=adapter, network_config=config)
    result = registry.invoke('net_check', {'urls': ['https://cnb.cool/a', 'https://github.com/a']}, ctx)
    assert result['ok'] and not result['data']['settings_changed']
    assert calls[0]['route'] == {'mode': 'direct'}
    assert calls[1]['route']['proxy_url'].endswith(':10808')
    assert all(call['verify_tls'] and not call['follow_redirects'] for call in calls)
    assert network_route('https://github.com.evil.invalid', config) == {'mode': 'direct'}
    assert network_route('https://github.com', {}) == {'mode': 'direct'}


def test_remote_hosts_are_verified_and_redacted(registry, tmp_path):
    calls = []
    def adapter(operation, args, ctx):
        calls.append((operation, args))
        return {'matches': [], 'truncated': False}
    ctx = context(tmp_path, remote_fs=adapter, remote_hosts={'lab': host()})
    listing = registry.invoke('sftp_hosts', {}, ctx)['data']
    assert listing == {'hosts': [{'alias': 'lab', 'configured': True, 'transport_ready': True}]}
    assert 'SSH_SECRET' not in str(listing) and 'keychain' not in str(listing)
    assert registry.invoke('search_remote', {'host': 'LAB', 'path': '/data', 'query': 'literal.*'}, ctx)['ok']
    assert calls[0][1]['literal'] is True and calls[0][1]['strict_host_key'] is True
    for path in ['/outside', '/data/../secret']:
        assert registry.invoke('search_remote', {'host': 'lab', 'path': path, 'query': 'a'}, ctx)['error']['code'] == 'path_denied'
    assert registry.invoke('sftp_hosts', {'action': 'ping', 'host': 'missing'}, ctx)['error']['code'] == 'unknown_host'
    assert len(calls) == 1


@pytest.mark.parametrize('patch', [{'password': 'secret'}, {'strict_host_key': False}, {'known_hosts': ''}, {'roots': []}, {'key_ref': '-----BEGIN PRIVATE KEY-----\nsecret'}])
def test_remote_insecure_config_rejected(registry, tmp_path, patch):
    config = {**host(), **patch}
    ctx = context(tmp_path, remote_hosts={'lab': config})
    assert registry.invoke('sftp_hosts', {}, ctx)['error']['code'] == 'invalid_configuration'


def test_remote_result_limit_and_cancel(registry, tmp_path):
    cancelled = [False]
    def adapter(operation, args, ctx):
        cancelled[0] = True
        return {'ok': True}
    ctx = ToolContext(frozenset({'sftp'}), (tmp_path,), lambda s, a: True, lambda: cancelled[0], {'remote_fs': adapter, 'remote_hosts': {'lab': host()}})
    assert registry.invoke('sftp_hosts', {'action': 'ping', 'host': 'lab'}, ctx)['error']['code'] == 'cancelled'
