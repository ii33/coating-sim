"""Real urllib opener/handler chain; only the HTTPS transport is replaced."""

import hashlib
import http.client
import io
import os
import threading
from email.message import Message

import pytest

from tools.dependency_manager import git_runtime, runtime
from tools.dependency_manager.mirrors import ProxyConfig

PAYLOAD = b"verified vendor download"


class Response(io.BytesIO):
    def __init__(self, status=200, location=None):
        super().__init__(PAYLOAD if status == 200 else b"")
        self.url = ""
        self.status = status
        self.code = status
        self.reason = "OK" if status == 200 else "Found"
        self.headers = Message()
        if location:
            self.headers["Location"] = location

    def info(self):
        return self.headers

    def geturl(self):
        return self.url


@pytest.fixture
def transport(monkeypatch):
    for key in tuple(os.environ):
        if key.lower().endswith("_proxy"):
            monkeypatch.delenv(key)
    calls: list[dict] = []
    responses: list[Response] = []

    class Connection(http.client.HTTPSConnection):
        debuglevel = 0

        def __init__(self, host, **kwargs):
            self.record = {"host": host}
            self.sock = None
            calls.append(self.record)

        def set_debuglevel(self, value):
            pass

        def set_tunnel(self, host, port=None, headers=None):
            self.record["tunnel"] = host
            self.record["tunnel_options"] = {"port": port, "headers": headers}

        def request(self, method, url, body=None, headers=None, *, encode_chunked=False):
            self.record.update(method=method, path=url, headers=headers)

        def getresponse(self):
            return responses.pop(0) if responses else Response()

        def close(self):
            pass

    monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
    return calls, responses


@pytest.fixture(params=["git", "python"])
def download(request, tmp_path):
    if request.param == "git":
        function = git_runtime.download_git
        url = "https://github.com/git-for-windows/git/releases/download/test/MinGit.zip"
        redirect = "https://release-assets.githubusercontent.com/vendor/MinGit.zip"
    else:
        function = runtime.download_runtime
        url = "https://www.python.org/ftp/python/test/python.exe"
        redirect = "https://www.python.org/ftp/python/test/redirected.exe"
    artifact = {"url": url, "sha256": hashlib.sha256(PAYLOAD).hexdigest()}
    destination = tmp_path / "download.bin"

    def run(proxy):
        function(artifact, destination, proxy, threading.Event(), lambda event: None)
        assert destination.read_bytes() == PAYLOAD

    return run, url, redirect, destination


@pytest.mark.parametrize("mode", ["none", "env"])
def test_direct_download_reaches_https_without_proxy_handler_failure(download, transport, mode):
    run, url, redirect, destination = download
    calls, responses = transport
    run(ProxyConfig(mode))
    assert len(calls) == 1
    assert calls[0]["host"] == url.split("/")[2]
    assert "tunnel" not in calls[0]


@pytest.mark.parametrize("mode", ["http", "env"])
def test_proxy_download_uses_connect_and_keeps_credentials_out_of_origin_headers(
    download, transport, monkeypatch, mode
):
    run, url, redirect, destination = download
    calls, responses = transport
    endpoint = "http://alice:secret@proxy.example:8080"
    if mode == "env":
        monkeypatch.setenv("HTTPS_PROXY", endpoint)
    run(ProxyConfig(mode, endpoint if mode == "http" else ""))
    assert calls[0]["host"] == "proxy.example:8080"
    assert calls[0]["tunnel"] == url.split("/")[2]
    assert "Proxy-Authorization" in calls[0]["tunnel_options"]["headers"]
    assert "Proxy-Authorization" not in calls[0]["headers"]


@pytest.mark.parametrize(
    "endpoint", ["", "http://", "https://proxy.example", "socks5://proxy.example:1080"]
)
def test_invalid_explicit_proxy_is_rejected_before_https(download, transport, endpoint):
    run, url, redirect, destination = download
    calls, responses = transport
    with pytest.raises(ValueError):
        run(ProxyConfig("http", endpoint))
    assert calls == []
    assert not destination.exists()


@pytest.mark.parametrize(
    "proxy", [ProxyConfig("none"), ProxyConfig("http", "http://proxy.example:8080")]
)
def test_official_redirect_traverses_real_handler_chain(download, transport, proxy):
    run, url, redirect, destination = download
    calls, responses = transport
    responses.append(Response(302, redirect))
    run(proxy)
    assert len(calls) == 2
    key = "tunnel" if proxy.mode == "http" else "host"
    assert calls[-1][key] == redirect.split("/")[2]


def test_external_redirect_is_blocked_before_second_connection(download, transport):
    run, url, redirect, destination = download
    calls, responses = transport
    responses.append(Response(302, "https://untrusted.example/payload"))
    with pytest.raises(ValueError, match="official"):
        run(ProxyConfig("none"))
    assert len(calls) == 1
    assert not destination.exists()


def test_invalid_environment_proxy_is_rejected_before_https(download, transport, monkeypatch):
    run, url, redirect, destination = download
    calls, responses = transport
    monkeypatch.setenv("HTTPS_PROXY", "http://")
    with pytest.raises(ValueError):
        run(ProxyConfig("env"))
    assert calls == []
    assert not destination.exists()


def test_explicit_direct_mode_does_not_fall_back_to_system_proxy(download, transport, monkeypatch):
    run, url, redirect, destination = download
    calls, responses = transport
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient-proxy.example:8080")
    run(ProxyConfig("none"))
    assert calls[0]["host"] == url.split("/")[2]
    assert "tunnel" not in calls[0]
    assert os.environ["HTTPS_PROXY"] == "http://ambient-proxy.example:8080"
