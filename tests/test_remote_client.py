"""Isolated contract tests for RemoteTransport; all network calls are mocked."""
import hashlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from coat_sim.scheduler.remote_client import RemoteServiceError, RemoteTransport


class Response:
    def __init__(self, body, status=200):
        self.body = io.BytesIO(body)
        self.status = status
        self.code = status
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def read(self, size=-1):
        return self.body.read(size)

    def close(self):
        self.closed = True


class Opener:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        return self.responder(request, timeout)


def success(result, *, status=200):
    return Response(json.dumps({"api_version": 1, "ok": True, "result": result}).encode(), status)


def test_rejects_unsafe_or_ambiguous_base_urls():
    for url, options in (
        ("https://user:secret@example.test", {}),
        ("https://example.test/path?x=1", {}),
        ("http://example.test", {"allow_loopback_http": True}),
        ("http://127.0.0.1", {}),
        ("http://localhost", {"allow_loopback_http": True}),
    ):
        with pytest.raises(ValueError):
            RemoteTransport(url, **options)
    assert RemoteTransport("http://[::1]:8000", allow_loopback_http=True).base_url == "http://[::1]:8000"
    assert RemoteTransport("https://example.test").base_url == "https://example.test"


def test_health_capabilities_and_rpc_use_versioned_routes_and_bounded_timeouts():
    transport = RemoteTransport("https://service.test", token="secret")
    opener = Opener(lambda request, timeout: success({"ok": True}))
    transport._opener = opener

    assert transport.health() == {"ok": True}
    assert transport.capabilities() == {"ok": True}
    assert transport.request("events", {"wait_s": 9}) == {"ok": True}
    assert [call[0].full_url for call in opener.calls] == [
        "https://service.test/api/v1/health",
        "https://service.test/api/v1/capabilities",
        "https://service.test/api/v1/rpc",
    ]
    assert opener.calls[2][1] == 40
    assert opener.calls[2][0].get_header("Authorization") == "Bearer secret"
    assert json.loads(opener.calls[2][0].data) == {
        "api_version": 1, "action": "events", "arguments": {"wait_s": 9},
    }


def test_rpc_rejects_stop_and_unknown_action_without_network_call():
    transport = RemoteTransport("https://service.test")
    opener = Opener(lambda *_: pytest.fail("unexpected network call"))
    transport._opener = opener
    for action, expected in (("stop", "remote_stop_disabled"), ("execute_arbitrary", "unknown_scheduler_action")):
        with pytest.raises(RemoteServiceError) as caught:
            transport.request(action, {})
        assert caught.value.code == expected
    assert not opener.calls


def test_rpc_parses_structured_service_errors():
    transport = RemoteTransport("https://service.test")
    transport._opener = Opener(lambda *_: Response(json.dumps({
        "api_version": 1, "ok": False,
        "error": {"code": "job_not_found", "message": "No such job"},
    }).encode()))
    with pytest.raises(RemoteServiceError, match="No such job") as caught:
        transport.request("status", {"job_id": "j-1"})
    assert caught.value.code == "job_not_found"


def test_download_streams_to_temp_verifies_then_publishes(tmp_path):
    payload = b"remote artifact bytes"
    artifact_id = "a" * 64
    transport = RemoteTransport("https://service.test")
    transport._json_call = lambda method, path: {
        "artifact_id": artifact_id, "name": "result.bin", "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    opener = Opener(lambda *_: Response(payload))
    transport._opener = opener
    destination = tmp_path / "nested" / "result.bin"

    result = transport.download(artifact_id, destination)

    assert result == destination
    assert destination.read_bytes() == payload
    assert opener.calls[0][0].full_url.endswith("/api/v1/artifacts/" + artifact_id)


def test_download_rejects_bad_descriptor_and_keeps_existing_destination(tmp_path):
    destination = tmp_path / "result.bin"
    destination.write_bytes(b"existing")
    transport = RemoteTransport("https://service.test")
    transport._json_call = lambda *_: {
        "artifact_id": "a" * 64, "name": "../escape", "bytes": 1, "sha256": "0" * 64,
    }
    with pytest.raises(RemoteServiceError) as caught:
        transport.download("a" * 64, destination)
    assert caught.value.code == "invalid_artifact_name"
    assert destination.read_bytes() == b"existing"


def test_download_integrity_failure_does_not_replace_destination(tmp_path):
    destination = tmp_path / "result.bin"
    destination.write_bytes(b"existing")
    payload = b"different"
    transport = RemoteTransport("https://service.test")
    transport._json_call = lambda *_: {
        "artifact_id": "a" * 64, "name": "result.bin", "bytes": 1,
        "sha256": hashlib.sha256(b"x").hexdigest(),
    }
    transport._opener = Opener(lambda *_: Response(payload))
    with pytest.raises(RemoteServiceError) as caught:
        transport.download("a" * 64, destination)
    assert caught.value.code == "artifact_size_mismatch"
    assert destination.read_bytes() == b"existing"


def test_submit_uploads_canonical_portable_spec_and_rejects_metadata(tmp_path):
    calls = []
    transport = RemoteTransport("https://service.test")
    transport.upload_blob = lambda body: calls.append(("blob", body)) or {
        "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
    }
    transport._json_call = lambda method, path, payload: calls.append((method, path, payload)) or {"job_id": "stable"}
    portable = {"inputs": {}, "parameters": {"label": "test"}, "executor": "probe",
                "protocol_version": 1}
    module = SimpleNamespace(prepare_portable_spec=lambda spec: SimpleNamespace(
        to_dict=lambda: portable))
    with patch.dict(sys.modules, {"coat_sim.scheduler.remote_contract": module}):
        assert transport.submit("test", "stable") == {"job_id": "stable"}
        with pytest.raises(RemoteServiceError) as caught:
            transport.submit("test", "stable", {"title": "unsupported"})
    assert caught.value.code == "remote_metadata_unsupported"
    frozen = json.dumps({"executor": "probe", "inputs": {}, "parameters": {"label": "test"},
                         "protocol_version": 1}, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":")).encode("utf-8")
    assert calls == [
        ("blob", frozen),
        ("POST", "/api/v1/jobs", {
            "api_version": 1, "executor_schema_version": 1, "job_id": "stable",
            "spec_blob": {"sha256": hashlib.sha256(frozen).hexdigest(), "bytes": len(frozen)},
        }),
    ]


def test_register_deposition_asset_uses_fixed_kind_and_filename(tmp_path):
    source = tmp_path / "arbitrary-name.coatasset"
    source.write_bytes(b"bundle")
    transport = RemoteTransport("https://service.test")
    calls = []
    transport.upload_blob = lambda path: {"sha256": "a" * 64, "bytes": path.stat().st_size}
    transport._json_call = lambda method, path, payload: calls.append((method, path, payload)) or {
        "artifact_id": "b" * 64, "name": "input.coatasset", "role": "bundle", "sha256": "a" * 64, "bytes": 6,
    }
    result = transport.register_deposition_asset(source)
    assert result["artifact_id"] == "b" * 64
    assert calls[0][2] == {
        "api_version": 1, "blob": {"sha256": "a" * 64, "bytes": 6},
        "kind": "deposition_bundle",
    }


@pytest.mark.parametrize('envelope', [
    {'result': {}}, {'api_version': True, 'ok': True, 'result': {}},
    {'api_version': 2, 'ok': True, 'result': {}}, {'api_version': 1, 'ok': 1, 'result': {}},
])
def test_rejects_unversioned_or_malformed_envelope(envelope):
    transport = RemoteTransport('https://service.test')
    transport._opener = Opener(lambda *_: Response(json.dumps(envelope).encode()))
    with pytest.raises(RemoteServiceError, match='invalid_remote_response'):
        transport.health()


def test_tls_default_context_and_redirect_rejection():
    import ssl
    from urllib.request import Request
    from coat_sim.scheduler.remote_client import _NoRedirect
    with patch('coat_sim.scheduler.remote_client.ssl.create_default_context', wraps=ssl.create_default_context) as create:
        RemoteTransport('https://service.test')
    context = next(handler._context for handler in RemoteTransport('https://service.test')._opener.handlers
                   if hasattr(handler, '_context'))
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert create.call_count == 1
    with pytest.raises(RemoteServiceError, match='redirects'):
        _NoRedirect().redirect_request(Request('https://service.test', headers={'Authorization': 'Bearer test'}),
                                     None, 302, 'redirect', {}, 'https://other.test')


def test_download_equal_size_bad_hash_preserves_file_and_cleans_temporary(tmp_path):
    destination = tmp_path / 'result.bin'
    destination.write_bytes(b'existing')
    transport = RemoteTransport('https://service.test')
    transport._json_call = lambda *_: {'artifact_id': 'a'*64, 'name': 'result.bin',
        'bytes': 3, 'sha256': hashlib.sha256(b'abc').hexdigest()}
    transport._opener = Opener(lambda *_: Response(b'xyz'))
    with pytest.raises(RemoteServiceError, match='artifact_integrity_failed'):
        transport.download('a'*64, destination)
    assert destination.read_bytes() == b'existing'
    assert list(tmp_path.iterdir()) == [destination]


def test_artifact_descriptor_can_exceed_control_response_limit():
    assert RemoteTransport._validate_descriptor('a'*64, {'artifact_id': 'a'*64,
        'name': 'result.coatasset', 'bytes': 128*1024*1024, 'sha256': 'b'*64})[1] == 128*1024*1024

