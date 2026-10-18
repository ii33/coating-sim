"""MCP-02: malformed permission must never admit project cwd or open a peer."""
import asyncio
import pytest
from coat_sim.agent import external_mcp as external

pytestmark = pytest.mark.contract


def config(permission):
    return {'id': 'synthetic', 'name': 'Synthetic', 'transport': 'stdio',
            'command': 'never-start-this-process', 'args': [],
            'cwd': str(external.PROJECT_ROOT), 'enabled': True, 'allow_tools': True,
            'allow_project_data': permission}


@pytest.mark.parametrize('permission', [False, 0, 1, 'false', 'true', None, [], {}])
def test_project_cwd_strict_true_only(monkeypatch, permission):
    calls = []
    def connection(row):
        calls.append(row)
        raise AssertionError('No connection may be attempted')
    monkeypatch.setattr(external, '_connection', connection)
    with pytest.raises(ValueError, match='项目'):
        external.validate_server(config(permission))
    assert external.normalized_servers([config(permission)]) == []
    async def scenario():
        pool = external.ExternalMcpPool()
        await pool.sync([config(permission)])
        assert pool.configs == {}
        assert not (await pool.catalog('synthetic'))['ok']
        assert not (await external.probe_server(config(permission)))['ok']
        await pool.close()
    asyncio.run(scenario())
    assert not calls


def test_true_consent_normalization_is_idempotent():
    row = external.validate_server(config(True))
    assert row['allow_project_data'] is True
    assert external.validate_server(row) == row
    assert external.normalized_servers([row]) == [row]


@pytest.mark.parametrize('permission', [False, 0, 1, 'false', 'true', None])
def test_nonproject_cwd_retains_false_normalization(permission):
    raw = config(permission)
    raw['cwd'] = ''
    row = external.validate_server(raw)
    assert row['allow_project_data'] is False
    assert external.validate_server(row) == row
