"""Business child preserves host-selected data paths without inheriting secrets."""
import json
import subprocess
import sys

import pytest
from mcp.client.stdio import get_default_environment

from coat_sim.core.refractiveindex_local import build_from_directory

pytestmark = pytest.mark.contract

PATH_KEYS = ('COAT_SIM_REFRACTIVEINDEX_STORE', 'XDG_DATA_HOME', 'LOCALAPPDATA')
SECRET_KEYS = ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'COAT_SIM_ASSISTANT_API_KEY',
               'AWS_SECRET_ACCESS_KEY', 'SSH_AUTH_SOCK', 'PYTHONPATH', 'HTTP_PROXY')


@pytest.mark.parametrize('path_key', PATH_KEYS)
def test_business_child_reads_existing_host_reference_store(tmp_path, monkeypatch, path_key):
    from coat_sim.agent import runtime

    for key in PATH_KEYS:
        monkeypatch.delenv(key, raising=False)
    data = tmp_path / 'relocated-data'
    root = (data if path_key == 'COAT_SIM_REFRACTIVEINDEX_STORE' else
            data / 'CoatSim/reference-databases/refractiveindex')
    source = tmp_path / 'synthetic-source'
    document = source / 'database/data/main/fixture/nk/sample.yml'
    document.parent.mkdir(parents=True)
    document.write_text(json.dumps({'REFERENCES': 'Synthetic offline fixture',
        'DATA': [{'type': 'tabulated nk', 'data': '0.4 1.5 0.01\n0.7 1.6 0.02'}]}))
    (source / 'LICENSE').write_text('CC0 1.0 Universal\n')
    (source / 'database/catalog-nk.yml').write_text(json.dumps([
        {'SHELF': 'main', 'name': 'Main', 'content': [
            {'BOOK': 'fixture', 'name': 'Fixture', 'content': [
                {'PAGE': 'sample', 'name': 'Sample', 'data': 'main/fixture/nk/sample.yml'}]}]}]))
    built = build_from_directory(source, root, upstream_commit='a' * 40)
    monkeypatch.setenv(path_key, str(data))
    # Isolate the fallback too: no developer or test-runner home is inspected.
    monkeypatch.setenv('HOME', str(tmp_path / 'empty-home'))
    monkeypatch.setenv('USERPROFILE', str(tmp_path / 'empty-home'))
    for key in SECRET_KEYS:
        monkeypatch.setenv(key, 'synthetic-never-forward')
    monkeypatch.setattr(runtime, '_BUSINESS_SERVER', None)
    runtime._business_server()
    assert runtime._BUSINESS_SERVER is not None
    transport = runtime._BUSINESS_SERVER[2]
    child_environment = transport.env
    assert child_environment is not None
    assert not set(SECRET_KEYS).intersection(child_environment)
    # Match MCP stdio's default-env plus explicit-env merge. Windows' path
    # selection is simulated within the module, without changing OS behavior.
    script = '''
import json, os, sys
from types import SimpleNamespace
from coat_sim.core import refractiveindex_local as rii
if sys.argv[1] in {'LOCALAPPDATA', 'XDG_DATA_HOME'}:
    rii.os = SimpleNamespace(name='nt' if sys.argv[1] == 'LOCALAPPDATA' else 'posix', environ=os.environ)
root = rii.default_store_root()
print(json.dumps({'root': str(root), 'status': rii.RefractiveIndexLocalStore(root).status(),
                  'leaked': any(key in os.environ for key in json.loads(sys.argv[2]))}))
'''
    result = subprocess.run([sys.executable, '-c', script, path_key, json.dumps(SECRET_KEYS)],
        env={**get_default_environment(), **child_environment}, cwd=runtime.PROJECT_ROOT,
        capture_output=True, text=True, check=True, timeout=30)
    observed = json.loads(result.stdout)
    assert not observed['leaked']
    assert observed['status']['status'] == 'ready'
    assert observed['root'] == str(root)
    assert observed['status']['snapshot_id'] == built['snapshot_id']
    assert observed['status']['counts']['indexed_entries'] == 1
    assert not (tmp_path / 'empty-home').exists()
