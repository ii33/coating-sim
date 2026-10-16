"""Explicit plaintext HTTP opt-in checks with mocked listeners and networking."""
import json
import logging
from pathlib import Path
import ssl
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

from coat_sim.scheduler import remote_client, remote_server


TOKEN = "temporary-test-token-0123456789"


def make_service(monkeypatch, root):
    fake_scheduler = SimpleNamespace(
        runtime=Path(root), owner="test-owner",
        stopping=SimpleNamespace(wait=lambda _s: True, set=lambda: None),
        guard=SimpleNamespace(close=lambda: None), lock=SimpleNamespace(close=lambda: None),
        active=None,
    )
    monkeypatch.setitem(sys.modules, "numpy", ModuleType("numpy"))
    monkeypatch.setattr(remote_server, "BlobStore", lambda *args: object())
    monkeypatch.setattr(remote_server, "RemoteScheduler", lambda _root: fake_scheduler)
    return fake_scheduler


def test_server_requires_explicit_insecure_mode_for_non_loopback(tmp_path, monkeypatch):
    root = tmp_path / "not-created"
    with pytest.raises(remote_server.ServiceError, match="tls_and_authentication_required"):
        remote_server.RemoteComputeService(root, host="0.0.0.0", token=TOKEN)
    assert not root.exists()

    make_service(monkeypatch, tmp_path / "scheduler-runtime")
    # No actual listener is created by construction; this verifies admission only.
    service = remote_server.RemoteComputeService(
        root, host="0.0.0.0", token=TOKEN, allow_insecure_http=True,
    )
    assert service.host == "0.0.0.0"
    assert service.allow_insecure_http is True
    assert service.token == TOKEN


@pytest.mark.parametrize("kwargs, expected", [
    ({"allow_insecure_http": True}, "insecure_http_requires_authentication"),
    ({"allow_insecure_http": True, "token": TOKEN, "tls_cert": "cert", "tls_key": "key"},
     "insecure_http_mode_conflict"),
    ({"allow_insecure_http": True, "token": TOKEN, "insecure_loopback": True},
     "insecure_http_mode_conflict"),
    ({"allow_insecure_http": "true", "token": TOKEN}, "invalid_insecure_http_option"),
])
def test_server_rejects_missing_auth_and_conflicting_or_invalid_options_before_root(tmp_path, kwargs, expected):
    root = tmp_path / "not-created"
    with pytest.raises(remote_server.ServiceError, match=expected):
        remote_server.RemoteComputeService(root, host="0.0.0.0", **kwargs)
    assert not root.exists()


def test_old_loopback_mode_remains_loopback_only(tmp_path, monkeypatch):
    make_service(monkeypatch, tmp_path / "runtime")
    service = remote_server.RemoteComputeService(
        tmp_path / "root", host="127.0.0.1", insecure_loopback=True,
    )
    assert service.host == "127.0.0.1"
    probe_service = remote_server.RemoteComputeService(
        tmp_path / "probe-root", host="127.0.0.1", insecure_loopback=True, allow_probe=True,
    )
    assert probe_service.allow_probe is True
    with pytest.raises(remote_server.ServiceError, match="probe_requires_insecure_loopback_test_mode"):
        remote_server.RemoteComputeService(
            tmp_path / "probe-http-root", host="0.0.0.0", token=TOKEN,
            allow_insecure_http=True, allow_probe=True,
        )
    with pytest.raises(remote_server.ServiceError, match="insecure_listening_requires_loopback"):
        remote_server.RemoteComputeService(
            tmp_path / "other", host="0.0.0.0", insecure_loopback=True,
        )


@pytest.mark.parametrize("url", ["http://example.test", "http://192.0.2.10:8000"])
def test_client_denies_non_loopback_http_without_explicit_opt_in(url):
    with pytest.raises(ValueError, match="insecure_remote_http_forbidden|http_requires_loopback_ip"):
        remote_client.RemoteTransport(url)


def test_client_explicit_http_mode_requires_valid_token_and_has_no_conflicting_flags():
    client = remote_client.RemoteTransport(
        "http://service.example:8080", token=TOKEN, allow_insecure_http=True,
    )
    assert client.base_url == "http://service.example:8080"
    assert client._headers()["Authorization"] == f"Bearer {TOKEN}"

    invalid = [
        ("http://service.example", {"allow_insecure_http": True}),
        ("http://service.example", {"token": "short", "allow_insecure_http": True}),
        ("http://service.example", {"token": "bad token with whitespace", "allow_insecure_http": True}),
        ("http://service.example", {"token": "é" * 16, "allow_insecure_http": True}),
        ("http://service.example", {"token": TOKEN, "allow_insecure_http": True,
                                     "allow_loopback_http": True}),
        ("http://service.example", {"token": TOKEN, "allow_insecure_http": True,
                                     "ca_file": "ca.pem"}),
        ("https://service.example", {"token": TOKEN, "allow_insecure_http": True}),
    ]
    for url, options in invalid:
        with pytest.raises(ValueError):
            remote_client.RemoteTransport(url, **options)


def test_old_loopback_client_and_https_with_legacy_flag_keep_compatibility():
    loopback = remote_client.RemoteTransport("http://127.0.0.1:8000", allow_loopback_http=True)
    assert loopback.base_url == "http://127.0.0.1:8000"
    tls = remote_client.RemoteTransport("https://service.example", allow_loopback_http=True)
    assert tls.base_url == "https://service.example"
    short_token_tls = remote_client.RemoteTransport("https://service.example", token="short")
    assert short_token_tls.token == "short"
    assert tls._opener.handlers


def test_https_uses_default_certificate_and_hostname_verification_and_keeps_redirect_block():
    observed = {}
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True

    def capture_context(*args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return context

    with patch.object(remote_client.ssl, "create_default_context", side_effect=capture_context):
        transport = remote_client.RemoteTransport("https://service.example", ca_file="extra-ca.pem")
    assert observed["kwargs"] == {"cafile": "extra-ca.pem"}
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    redirect = next(handler for handler in transport._opener.handlers
                    if isinstance(handler, remote_client._NoRedirect))
    with pytest.raises(remote_client.RemoteServiceError, match="remote redirects are not allowed"):
        redirect.redirect_request(None, None, 302, "Found", {}, "https://other.example/")


def test_server_json_opt_in_is_preserved_and_cli_flag_is_optional_override(tmp_path, monkeypatch):
    config = tmp_path / "service.json"
    config.write_text(json.dumps({
        "data_root": str(tmp_path / "root"), "host": "0.0.0.0",
        "token_env": "REMOTE_TEST_TOKEN", "allow_insecure_http": True,
    }), encoding="utf-8")
    monkeypatch.setenv("REMOTE_TEST_TOKEN", TOKEN)
    calls = []
    fake_service = SimpleNamespace(scheduler=SimpleNamespace(stopping=SimpleNamespace(set=lambda: None)),
                                   run=lambda: calls.append("run"))
    with patch.object(remote_server, "RemoteComputeService", side_effect=lambda **kw: calls.append(kw) or fake_service), \
            patch.object(remote_server.signal, "signal"):
        assert remote_server.main(["--config", str(config)]) == 0
    assert calls[0]["allow_insecure_http"] is True

    config.write_text(json.dumps({
        "data_root": str(tmp_path / "root"), "host": "0.0.0.0",
        "token_env": "REMOTE_TEST_TOKEN", "allow_insecure_http": False,
    }), encoding="utf-8")
    # Each invocation models a new service process. The first invocation now
    # consumes its token variable before any child worker can inherit it.
    monkeypatch.setenv("REMOTE_TEST_TOKEN", TOKEN)
    with patch.object(remote_server, "RemoteComputeService", side_effect=lambda **kw: calls.append(kw) or fake_service), \
            patch.object(remote_server.signal, "signal"):
        assert remote_server.main(["--config", str(config), "--allow-insecure-http"]) == 0
    assert calls[2]["allow_insecure_http"] is True


def test_startup_warning_precedes_mock_listener_and_never_contains_token(tmp_path, monkeypatch, caplog):
    make_service(monkeypatch, tmp_path / "runtime")
    service = remote_server.RemoteComputeService(
        tmp_path / "root", host="0.0.0.0", token=TOKEN, allow_insecure_http=True,
    )
    caplog.set_level(logging.WARNING)

    def listener_after_warning(*args, **kwargs):
        assert any("Bearer credentials and request data are unencrypted" in record.getMessage()
                   for record in caplog.records)
        raise RuntimeError("listener construction observed; no socket was opened")

    monkeypatch.setattr(remote_server, "_BoundedHTTPServer", listener_after_warning)
    with pytest.raises(RuntimeError, match="no socket was opened"):
        service.run()
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "WARNING" in messages
    assert "Bearer credentials" in messages
    assert "encrypted tunnel" in messages
    assert TOKEN not in messages

