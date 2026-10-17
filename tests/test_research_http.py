"""Deterministic HTTP security checks: no external network or credentials."""
from __future__ import annotations

import json
import socket
import time
from unittest.mock import Mock, patch

import pytest

from coat_sim.agent.research_http import (
    MAX_DOWNLOAD_BYTES, HttpResponse, ResearchHttpError, SafeHttpClient,
    ValidatedTarget, _PinnedHTTPSConnection,
)


class Clock:
    def __init__(self):
        self.now = 100.0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, value):
        self.waits.append(value)
        self.now += value


def fake_client(responses=None, *, addresses=None, **options):
    calls = []
    clock = Clock()
    pending = list(responses or [HttpResponse(200, {}, b"ok", "")])

    def transport(target, headers, timeout, cap):
        calls.append((target, dict(headers), timeout, cap))
        item = pending.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    client = SafeHttpClient(resolver=lambda host: addresses or ["93.184.216.34"],
                            transport=transport, sleep=clock.sleep, clock=clock,
                            **options)
    return client, calls, clock


@pytest.mark.parametrize("url", [
    "http://example.org/file", "file:///etc/passwd", "https://user:secret@example.org/",
    "https://example.org:8443/", "https://example.org/#fragment", "https://example.org./",
    "https://example.org\\@127.0.0.1/", "https://example.org/a\r\nb",
    "https://127.0.0.1/", "https://10.0.0.1/", "https://169.254.169.254/latest/meta-data/",
    "https://[::1]/", "https://[fc00::1]/", "https://[::ffff:8.8.8.8]/",
    "https://224.0.0.1/", "https://192.88.99.1/", "https://[64:ff9b::a00:1]/",
    "https://[::8.8.8.8]/", "https://[fec0::1]/",
    "https://localhost/", "https://metadata.google.internal/",
])
def test_unsafe_urls_never_reach_transport(url):
    client, calls, _ = fake_client()
    with pytest.raises(ResearchHttpError):
        client.get_bytes(url)
    assert calls == []


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["93.184.216.34", "10.0.0.1"],
                                        ["169.254.1.1"], ["2001:db8::1"]])
def test_all_dns_answers_must_be_public(addresses):
    client, calls, _ = fake_client(addresses=addresses)
    with pytest.raises(ResearchHttpError, match="unsafe_address"):
        client.get_bytes("https://publisher.example/paper.pdf")
    assert not calls


def test_constructor_is_offline_and_host_allowlist_enforced_before_dns():
    resolver = Mock()
    client = SafeHttpClient(allowed_hosts={"api.openalex.org"}, resolver=resolver)
    resolver.assert_not_called()
    with pytest.raises(ResearchHttpError, match="host_not_allowed"):
        client.get_bytes("https://elsewhere.example/")
    resolver.assert_not_called()


def test_pinned_connection_uses_checked_ip_and_original_tls_hostname():
    target = ValidatedTarget("publisher.example", "93.184.216.34", 443, "/p", "")
    connection = _PinnedHTTPSConnection(target, 3.0)
    context = Mock()
    connection._tls_context = context
    sock = Mock()
    with patch("socket.create_connection", return_value=sock) as connect:
        connection.connect()
    connect.assert_called_once_with(("93.184.216.34", 443), 3.0)
    context.wrap_socket.assert_called_once_with(sock, server_hostname="publisher.example")


def test_cross_origin_redirect_drops_authorization_custom_header_and_query():
    client, calls, _ = fake_client([
        HttpResponse(302, {"Location": "https://cdn.example/paper?api_key=secret&token=x"}, b"", ""),
        HttpResponse(200, {}, b"%PDF-1.7", ""),
    ])
    response = client.get_bytes("https://publisher.example/paper?api_key=secret",
                                headers={"Authorization": "Bearer secret", "X-Api-Key": "secret"})
    assert calls[0][1]["authorization"] == "Bearer secret"
    assert "authorization" not in calls[1][1] and "x-api-key" not in calls[1][1]
    assert calls[1][0].path == "/paper"
    assert response.url == "https://cdn.example/paper"


def test_redirect_is_checked_each_time_and_dns_rebinding_stops_before_connect():
    resolutions = iter([["93.184.216.34"], ["127.0.0.1"]])
    transport = Mock(return_value=HttpResponse(302, {"location": "/next"}, b"", ""))
    client = SafeHttpClient(resolver=lambda _: next(resolutions), transport=transport)
    with pytest.raises(ResearchHttpError, match="unsafe_address"):
        client.get_bytes("https://publisher.example/first")
    assert transport.call_count == 1


def test_private_redirect_never_connects_and_errors_do_not_echo_url_or_body():
    client, calls, _ = fake_client([HttpResponse(302, {"location": "https://127.0.0.1/?token=secret"},
                                                b"secret", "")])
    with pytest.raises(ResearchHttpError) as error:
        client.get_bytes("https://example.org/?api_key=secret")
    assert len(calls) == 1 and "secret" not in str(error.value)


def test_429_and_5xx_have_bounded_backoff_and_usage_headers():
    client, calls, clock = fake_client([
        HttpResponse(429, {"Retry-After": "2", "X-RateLimit-Remaining": "0"}, b"", ""),
        HttpResponse(503, {}, b"", ""), HttpResponse(200, {}, b"ok", ""),
    ])
    assert client.get_bytes("https://example.org/p").body == b"ok"
    assert len(calls) == 3 and clock.waits == [2.0, 2.0]
    assert client.requests_remaining == 21


@pytest.mark.parametrize("status", [403, 404])
def test_forbidden_and_not_found_are_not_retried(status):
    client, calls, _ = fake_client([HttpResponse(status, {"Set-Cookie": "secret"}, b"secret", "")])
    with pytest.raises(ResearchHttpError) as error:
        client.get_bytes("https://example.org/p?token=secret")
    assert error.value.status == status and len(calls) == 1
    assert "secret" not in str(error.value) and not error.value.headers


def test_retry_after_long_or_unknown_is_returned_without_early_retry():
    for value in ("120", "Mon, 19 Oct 2026 10:00:00 GMT", "NaN"):
        client, calls, _ = fake_client([HttpResponse(429, {"Retry-After": value}, b"", "")])
        with pytest.raises(ResearchHttpError, match="retry_later") as error:
            client.get_bytes("https://example.org/p")
        assert error.value.status == 429 and len(calls) == 1


def test_retries_and_request_budget_are_finite():
    client, calls, _ = fake_client([HttpResponse(503, {}, b"", "")] * 4)
    with pytest.raises(ResearchHttpError) as error:
        client.get_bytes("https://example.org/p")
    assert error.value.status == 503 and len(calls) == 3
    client, calls, _ = fake_client(max_requests=1, cache_ttl_s=0)
    client.get_bytes("https://example.org/first")
    with pytest.raises(ResearchHttpError, match="request_budget_exhausted"):
        client.get_bytes("https://example.org/second")
    assert len(calls) == 1


def test_cache_is_memory_only_keyed_by_credentials_and_not_charged_again():
    client, calls, clock = fake_client([HttpResponse(200, {}, b"a", ""),
                                       HttpResponse(200, {}, b"b", "")])
    first = client.get_bytes("https://example.org/p", headers={"Authorization": "one"})
    cached = client.get_bytes("https://example.org/p", headers={"Authorization": "one"})
    assert not first.cached and cached.cached and len(calls) == 1
    second = client.get_bytes("https://example.org/p", headers={"Authorization": "two"})
    assert second.body == b"b" and not second.cached and len(calls) == 2
    assert clock.waits == [0.5]
    assert all("one" not in key and "https" not in key for key in client._cache)


def test_size_and_timeout_budgets_apply_to_injected_transports():
    client, _, _ = fake_client([HttpResponse(200, {}, b"12345", "")])
    with pytest.raises(ResearchHttpError, match="response_too_large"):
        client.get_bytes("https://example.org/p", max_bytes=4)
    with pytest.raises(ResearchHttpError, match="invalid_size_limit"):
        client.get_bytes("https://example.org/p", max_bytes=MAX_DOWNLOAD_BYTES + 1)
    client, _, _ = fake_client([socket.timeout("secret")])
    with pytest.raises(ResearchHttpError, match="timeout"):
        client.get_bytes("https://example.org/p")
    client, calls, _ = fake_client([HttpResponse(429, {}, b"", "")], total_timeout_s=0.5)
    with pytest.raises(ResearchHttpError, match="timeout"):
        client.get_bytes("https://example.org/p")
    assert len(calls) == 1


def test_dns_resolution_has_operation_timeout():
    def resolver(_):
        time.sleep(0.05)
        return ["93.184.216.34"]

    client = SafeHttpClient(resolver=resolver, total_timeout_s=0.005)
    with pytest.raises(ResearchHttpError, match="timeout"):
        client.get_bytes("https://example.org/")


def test_json_schema_decoder_and_encoding_are_bounded():
    body = json.dumps({"results": []}).encode()
    client, _, _ = fake_client([HttpResponse(200, {}, body, "")])
    assert client.get_json("https://example.org/p").data == {"results": []}
    client, _, _ = fake_client([HttpResponse(200, {}, b"not json secret", "")])
    with pytest.raises(ResearchHttpError, match="invalid_json"):
        client.get_json("https://example.org/p")
    client, _, _ = fake_client([HttpResponse(200, {"content-encoding": "gzip"}, body, "")])
    with pytest.raises(ResearchHttpError, match="unsupported_encoding"):
        client.get_json("https://example.org/p")


def test_header_injection_is_rejected_before_network():
    client, calls, _ = fake_client()
    for headers in ({"Authorization": "token\r\nHost: localhost"}, {"Host": "localhost"},
                    {"Cookie": "private"}):
        with pytest.raises(ResearchHttpError, match="unsafe_header"):
            client.get_bytes("https://example.org/", headers=headers)
    assert not calls
