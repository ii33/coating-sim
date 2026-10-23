"""Removed process CLI cannot remain an alternate sensitivity executor."""
from importlib.util import find_spec
import io
import json

import pytest

from coat_sim import film_sensitivity_workflow_cli as cli


def test_removed_process_cli_is_not_importable():
    assert find_spec('coat_sim.film_sensitivity_cli') is None


@pytest.mark.parametrize('payload', [
    {'schema_version': 1, 'kind': 'coat_sim.film_sensitivity_request'},
    {'schema_version': 1, 'kind': 'coat_sim.film_ideal_sensitivity_request'},
])
def test_current_cli_rejects_removed_formats_without_report_or_execution(payload, tmp_path, monkeypatch, capsys):
    def forbidden(*_args, **_kwargs):
        raise AssertionError('removed input must not reach execution')
    monkeypatch.setattr(cli, '_api', forbidden)
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(payload)))
    output = tmp_path / 'result.json'
    assert cli.main(['--output', str(output)]) == 2
    response = json.loads(capsys.readouterr().out)
    assert response['ok'] is False and 'unsupported' in response['error'].lower()
    assert not output.exists()
