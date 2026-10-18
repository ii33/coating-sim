"""Bounded diagnostics must identify failures without logging IPC/provider secrets."""
import io
import json
import os
from unittest.mock import patch

def test_lifecycle_trace_has_clock_pid_and_redacts_credentials():
    from coat_sim.assistant_diagnostics import trace
    from coat_sim.ui.control import UiControlError
    output=io.StringIO()
    error=UiControlError('ui_unavailable','Authorization: Bearer highly-private-token https://user:password@example.org?q=private token=private-value')
    with patch('sys.stderr',output):
        trace('registration_failed',error=error,attempt='attempt-1',elapsed_s=1.25)
    row=json.loads(output.getvalue())
    assert row['pid']==os.getpid() and row['timestamp'].endswith('Z')
    assert row['error_type']=='UiControlError' and row['code']=='ui_unavailable'
    assert row['attempt']=='attempt-1' and row['elapsed_s']==1.25
    for secret in ('highly-private-token','password','q=private','private-value'):
        assert secret not in output.getvalue()

def test_diagnostic_writer_failure_never_changes_lifecycle():
    from coat_sim.assistant_diagnostics import trace
    class Gone:
        def write(self,_value):
            raise OSError('closed output')
    with patch('sys.stderr',Gone()):
        trace('assistant_exit',exit_code=23)

def test_endpoint_summary_never_copies_authentication_fields():
    from coat_sim.assistant_diagnostics import endpoint_summary
    result=endpoint_summary({'protocol':1,'host':'127.0.0.1','pid':42,'role':'assistant-debug',
        'root':'D:/私有工程/app','token':'private-token','key':'private-key','authkey':'private-key',
        'arguments':{'secret':'private-content'}})
    assert result['pid']==42 and result['role']=='assistant-debug'
    assert 'private' not in json.dumps(result)
    assert 'root' not in result and result['root_id']


def test_json_secrets_and_causal_transport_failure_are_redacted():
    from coat_sim.assistant_diagnostics import trace
    from coat_sim.ui.control import UiControlError
    output=io.StringIO()
    error=UiControlError("ui_unavailable", '{"token":"private-json-value"}')
    error.__cause__=OSError("Windows connection refused token=private-cause-value")
    with patch("sys.stderr",output):
        trace("assistant_last_probe_failed",error=error)
    row=json.loads(output.getvalue())
    assert row["causes"] and "OSError" in row["causes"][0]
    assert "private-json-value" not in output.getvalue()
    assert "private-cause-value" not in output.getvalue()



def test_missing_stderr_never_falls_back_to_protocol_stdout():
    from coat_sim.assistant_diagnostics import trace
    protocol=io.StringIO()
    with patch('sys.stderr',None),patch('sys.stdout',protocol):
        trace('diagnostic_without_console')
    assert protocol.getvalue()==''


def test_quoted_secret_values_are_redacted_in_full_in_trace_and_causes():
    from coat_sim.assistant_diagnostics import trace
    examples = [
        'password="SYNTHETIC one two"',
        "secret='SYNTHETIC one, two; three'",
        r'api_key="SYNTHETIC escaped \"quote\" tail"',
        r"token='SYNTHETIC escaped \'quote\' tail'",
        'authkey="SYNTHETIC first\nsecond\nlast"',
        '{"password": "SYNTHETIC spaced JSON value"}',
        'password="SYNTHETIC https://user:pass@example.test/ and tail"',
        'Authorization: Bearer "SYNTHETIC bearer with spaces"',
        'password="SYNTHETIC unterminated tail',
        'password="SYNTHETIC trailing escape' + chr(92),
        r'password="Bearer \"SYNTHETIC escaped quote\" tail"',
        'secret="SYNTHETIC ' + 'long tail ' * 400,
    ]
    for message in examples:
        output = io.StringIO()
        error = RuntimeError(message)
        error.__cause__ = ValueError(message)
        trace("failure", error=error, stream=output)
        row = json.loads(output.getvalue())
        assert row["message"].endswith("<redacted>") or row["message"].endswith("<redacted>}")
        for secret_part in ("SYNTHETIC", "tail", "second", "three", "with spaces", "spaced JSON"):
            assert secret_part not in output.getvalue()


def test_bearer_and_url_redaction_remain_effective():
    from coat_sim.assistant_diagnostics import _clean
    result = _clean('Authorization: Bearer SYNTHETIC_TOKEN https://user:secret@example.test/?key=private')
    assert "SYNTHETIC_TOKEN" not in result
    assert "example.test" not in result
    assert "private" not in result
