from __future__ import annotations

from dataclasses import replace
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from coat_sim import film_sensitivity_cli as cli
from coat_sim.core.film_sensitivity import (
    FilmThicknessError, film_sensitivity_request_to_payload, film_sensitivity_result_from_payload,
    freeze_film_sensitivity_request,
)
from tests.test_film_sensitivity import problem

pytestmark = pytest.mark.algorithm


def write_request(tmp_path, request):
    path = tmp_path/'request.json'
    path.write_text(json.dumps(film_sensitivity_request_to_payload(request)), encoding='utf-8')
    return path


@pytest.mark.parametrize('operation', ['analyze', 'optimize'])
def test_cli_frozen_request_result_schema_and_overwrite_refusal(tmp_path, capsys, operation):
    service, request = problem(operation=operation)
    request = freeze_film_sensitivity_request(service, request)
    source = write_request(tmp_path,request)
    output = tmp_path/'result.json'
    assert cli.main(['--request',str(source),'--output',str(output)]) == 0
    capture = capsys.readouterr()
    events = [json.loads(line) for line in capture.err.splitlines()]
    assert events[0]['stage'] == 'freezing' and events[-1]['state'] == 'completed'
    payload = json.loads(output.read_text())
    assert payload['ok']
    result = film_sensitivity_result_from_payload(payload['result'])
    assert result.operation == operation and result.state == 'completed'
    assert result.best_analysis is not None and result.frozen_request == request
    before = output.read_bytes()
    assert cli.main(['--request',str(source),'--output',str(output)]) == 2
    assert output.read_bytes() == before
    assert 'already exists' in capsys.readouterr().out


def test_cli_stdin_and_stdout_are_machine_readable_and_live_catalog_is_never_read(tmp_path, monkeypatch,capsys):
    service, request = problem()
    monkeypatch.setattr(sys,'stdin',io.StringIO(json.dumps(film_sensitivity_request_to_payload(request))))
    assert cli.main([]) == 2
    assert 'complete frozen snapshot' in capsys.readouterr().out
    frozen = freeze_film_sensitivity_request(service,request)
    monkeypatch.setattr(sys,'stdin',io.StringIO(json.dumps(film_sensitivity_request_to_payload(frozen))))
    assert cli.main([]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['ok'] and payload['result']['kind'] == 'coat_sim.film_sensitivity_result'


def test_cli_partial_and_physical_failure_have_distinct_exit_codes_and_json_results(tmp_path,capsys):
    service, request = problem(operation='optimize')
    frozen = freeze_film_sensitivity_request(service,replace(request,max_evaluations=1))
    source = write_request(tmp_path,frozen)
    assert cli.main(['--request',str(source)]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload['result']['state'] == 'partial'
    assert payload['result']['termination_reason'] == 'evaluation_limit'
    broken = replace(frozen, operation='analyze',errors=(FilmThicknessError('low-film',100.),))
    source = write_request(tmp_path,broken)
    assert cli.main(['--request',str(source)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert not payload['ok'] and payload['result']['state'] == 'failed'
    assert 'never clipped' in payload['result']['message']


def test_cli_sigint_sets_cancel_event_and_returns_true_cancelled_result(tmp_path,monkeypatch,capsys):
    service, request = problem()
    source = write_request(tmp_path,freeze_film_sensitivity_request(service,request))
    handlers = []
    def register(_signal, handler):
        handlers.append(handler)
        if len(handlers) == 1:
            handler(None,None)
        return 'previous-handler'
    monkeypatch.setattr(signal,'signal',register)
    assert cli.main(['--request',str(source)]) == 130
    payload = json.loads(capsys.readouterr().out)
    assert not payload['ok'] and payload['result']['state'] == 'cancelled'
    assert payload['result']['best_analysis'] is None
    assert handlers[-1] == 'previous-handler'


def test_cli_rejects_unknown_nested_field_bad_json_missing_input(tmp_path,capsys):
    service, request = problem()
    source = write_request(tmp_path,freeze_film_sensitivity_request(service,request))
    payload = json.loads(source.read_text())
    payload['errors'][0]['ignored'] = True
    source.write_text(json.dumps(payload))
    assert cli.main(['--request',str(source)]) == 2
    assert 'unknown fields' in capsys.readouterr().out
    source.write_text('{broken')
    assert cli.main(['--request',str(source)]) == 2
    assert not json.loads(capsys.readouterr().out)['ok']
    assert cli.main(['--request',str(tmp_path/'missing.json')]) == 2
    assert not json.loads(capsys.readouterr().out)['ok']


def test_module_invocation_runs_without_gui_and_writes_native_json(tmp_path):
    service,request = problem()
    source = write_request(tmp_path,freeze_film_sensitivity_request(service,request))
    environment = {**os.environ,'OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'1',
        'MKL_NUM_THREADS':'1','NUMEXPR_NUM_THREADS':'1'}
    completed = subprocess.run([sys.executable,'-m','coat_sim.film_sensitivity_cli','--request',str(source)],
        cwd=Path(__file__).resolve().parents[1],env=environment,capture_output=True,text=True,timeout=30)
    assert completed.returncode == 0,completed.stderr
    result = json.loads(completed.stdout)['result']
    assert result['state'] == 'completed'
    assert result['frozen_request']['design_revision'] == 7
