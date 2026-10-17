"""Explicit, bounded HTTPS reads for research sources (never startup discovery).

The production transport pins a validated public IP while TLS verifies the
original hostname. It ignores environment proxies/cookies and never logs URLs,
headers or response text. Injectable resolver/transport are trusted test seams.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import math
import queue
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit


MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
_USAGE_HEADERS = frozenset({
    "content-type", "content-length", "content-encoding", "retry-after",
    "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
    "x-ratelimit-credits-used", "x-rate-limit-limit", "x-rate-limit-interval",
    "x-concurrency-limit", "ratelimit-limit", "ratelimit-remaining", "ratelimit-reset",
})


class ResearchHttpError(ValueError):
    """Safe machine-readable failure; never include provider bodies or secrets."""

    def __init__(self, code: str, *, status: int | None = None,
                 headers: Mapping[str, str] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.headers = usage_headers(headers or {})


def usage_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Keep bounded usage/content fields, never Location, cookies or credentials."""
    return {str(key).lower(): str(value)[:256] for key, value in headers.items()
            if str(key).lower() in _USAGE_HEADERS}


@dataclass(frozen=True)
class ValidatedTarget:
    host: str
    ip: str
    port: int
    path: str = field(repr=False)
    url: str = field(repr=False)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes = field(repr=False)
    url: str = field(repr=False)
    cached: bool = False


@dataclass(frozen=True)
class JsonResponse:
    data: Any = field(repr=False)
    headers: dict[str, str]
    status: int
    url: str = field(repr=False)
    cached: bool = False


def _public_address(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise ResearchHttpError("unsafe_address") from None
    if not address.is_global or address.is_reserved or address.is_multicast:
        raise ResearchHttpError("unsafe_address")
    if isinstance(address, ipaddress.IPv4Address) and (
            address in ipaddress.ip_network("192.0.0.0/24")
            or address in ipaddress.ip_network("192.88.99.0/24")):
        # Conservative denial of protocol-assignment and deprecated relay
        # ranges, independent of Python/IANA classification version changes.
        raise ResearchHttpError("unsafe_address")
    if isinstance(address, ipaddress.IPv6Address) and (
            address.is_site_local or address.ipv4_mapped or address.sixtofour or address.teredo):
        raise ResearchHttpError("unsafe_address")
    return str(address)


def _resolve(host: str) -> list[str]:
    return list(dict.fromkeys(str(item[4][0]) for item in socket.getaddrinfo(
        host, 443, type=socket.SOCK_STREAM)))


def _bounded_resolve(resolver: Callable[[str], Sequence[str]], host: str,
                     timeout: float) -> Sequence[str]:
    # getaddrinfo has no portable timeout. A daemon worker cannot hold the
    # caller past its operation deadline; the request budget bounds workers.
    result: queue.Queue[Sequence[str] | Exception] = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            result.put(resolver(host))
        except Exception as exc:
            result.put(exc)

    threading.Thread(target=run, daemon=True, name="research-dns").start()
    try:
        resolved = result.get(timeout=max(0.001, timeout))
    except queue.Empty:
        raise ResearchHttpError("timeout") from None
    if isinstance(resolved, Exception):
        raise ResearchHttpError("dns_failed") from None
    return resolved


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, target: ValidatedTarget, timeout: float) -> None:
        self._tls_context = ssl.create_default_context()
        super().__init__(target.host, target.port, timeout=timeout,
                         context=self._tls_context)
        self._target = target

    def connect(self) -> None:
        sock = socket.create_connection((self._target.ip, self.port), self.timeout)
        try:
            self.sock = self._tls_context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def _transport(target: ValidatedTarget, headers: Mapping[str, str],
               timeout: float, max_bytes: int) -> HttpResponse:
    connection = _PinnedHTTPSConnection(target, timeout)
    deadline = time.monotonic() + timeout
    try:
        connection.request("GET", target.path, headers=dict(headers))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ResearchHttpError("timeout")
        if connection.sock is not None:
            connection.sock.settimeout(remaining)
        response = connection.getresponse()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        length = response_headers.get("content-length", "")
        if length and (not length.isdigit() or int(length) > max_bytes):
            raise ResearchHttpError("response_too_large")
        chunks: list[bytes] = []
        count = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ResearchHttpError("timeout")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(65536, max_bytes + 1 - count))
            count += len(chunk)
            if count > max_bytes:
                raise ResearchHttpError("response_too_large")
            if not chunk:
                break
            chunks.append(chunk)
        return HttpResponse(response.status, response_headers, b"".join(chunks), target.url)
    finally:
        connection.close()


class SafeHttpClient:
    """Synchronous HTTPS GETs with per-client request budget and memory cache.

    No network activity occurs at construction. A client is intended for one
    explicitly enabled session. Calls are serialized and paced conservatively.
    Redirects and retries count against the same budget as original requests.
    Cross-origin redirects lose ALL query fields and all but Accept/User-Agent
    headers; signed cross-origin links must therefore be explicitly retried by
    the user as a separate authorized URL. No automatic credential fallback.
    """

    def __init__(self, *, allowed_hosts: set[str] | frozenset[str] | None = None,
                 resolver: Callable[[str], Sequence[str]] | None = None,
                 transport: Callable[[ValidatedTarget, Mapping[str, str], float, int],
                                     HttpResponse] | None = None,
                 timeout_s: float = 15, total_timeout_s: float = 45,
                 max_bytes: int = 2 * 1024 * 1024, max_requests: int = 24,
                 max_retries: int = 2, max_redirects: int = 3,
                 cache_ttl_s: float = 300, min_interval_s: float = 0.5,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if (not 0 < timeout_s <= 30 or not 0 < total_timeout_s <= 60
                or not 1 <= max_bytes <= MAX_DOWNLOAD_BYTES or not 1 <= max_requests <= 1000
                or not 0 <= max_retries <= 2 or not 0 <= max_redirects <= 5
                or not 0 <= cache_ttl_s <= 3600 or not 0 <= min_interval_s <= 10):
            raise ValueError("invalid_http_budget")
        self.allowed_hosts = (frozenset(host.lower() for host in allowed_hosts)
                              if allowed_hosts is not None else None)
        self._resolver = resolver or _resolve
        self._transport = transport or _transport
        self._timeout = float(timeout_s)
        self._total_timeout = float(total_timeout_s)
        self._max_bytes = int(max_bytes)
        self._budget = int(max_requests)
        self._retries = int(max_retries)
        self._redirects = int(max_redirects)
        self._ttl = float(cache_ttl_s)
        self._interval = float(min_interval_s)
        self._sleep, self._clock = sleep, clock
        self._last_request: float | None = None
        self._cache: dict[str, tuple[float, HttpResponse]] = {}
        self._lock = threading.Lock()

    @property
    def requests_remaining(self) -> int:
        return self._budget

    def _target(self, url: str, timeout: float) -> ValidatedTarget:
        if (not isinstance(url, str) or len(url) > 8192
                or any(ord(char) < 33 or ord(char) == 127 for char in url)
                or "\\" in url):
            raise ResearchHttpError("unsafe_url")
        try:
            parsed = urlsplit(url)
            host = parsed.hostname or ""
            port = parsed.port or 443
            if (parsed.scheme != "https" or not host or parsed.username is not None
                    or parsed.password is not None or parsed.fragment or port != 443
                    or host.endswith(".") or "%" in host):
                raise ResearchHttpError("unsafe_url")
            host = host.encode("idna").decode("ascii").lower()
        except (ValueError, UnicodeError):
            raise ResearchHttpError("unsafe_url") from None
        if self.allowed_hosts is not None and host not in self.allowed_hosts:
            raise ResearchHttpError("host_not_allowed")
        if (host in {"localhost", "localhost.localdomain", "metadata.google.internal"}
                or host.endswith((".localhost", ".local", ".internal"))):
            raise ResearchHttpError("unsafe_address")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            addresses = _bounded_resolve(self._resolver, host, timeout)
        else:
            addresses = [host]
        if not addresses or len(addresses) > 32:
            raise ResearchHttpError("dns_failed")
        # Reject mixed public/private answers rather than selecting a safe one.
        checked = [_public_address(address) for address in addresses]
        path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        return ValidatedTarget(host, checked[0], port, path, url)

    def _pause(self, seconds: float, deadline: float) -> None:
        if self._clock() + seconds >= deadline:
            raise ResearchHttpError("timeout")
        if seconds > 0:
            self._sleep(seconds)

    def get_bytes(self, url: str, *, headers: Mapping[str, str] | None = None,
                  max_bytes: int | None = None) -> HttpResponse:
        cap = self._max_bytes if max_bytes is None else max_bytes
        if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= MAX_DOWNLOAD_BYTES:
            raise ResearchHttpError("invalid_size_limit")
        outgoing = {"accept": "*/*", "accept-encoding": "identity",
                    "user-agent": "coating-sim-research/1"}
        for key, value in (headers or {}).items():
            if (not isinstance(key, str) or not isinstance(value, str)
                    or not key.replace("-", "").isalnum() or len(value) > 4096
                    or any(ord(c) < 32 or ord(c) == 127 for c in value)
                    or key.lower() in {"host", "cookie", "connection", "content-length",
                                       "transfer-encoding", "proxy-authorization"}):
                raise ResearchHttpError("unsafe_header")
            outgoing[key.lower()] = value
        # Hash cache keys so neither provider query nor credentials are retained
        # as human-readable cache keys. The cache itself is never persisted.
        cache_key = hashlib.sha256(json.dumps([url, sorted(outgoing.items())],
                                             sort_keys=True).encode()).hexdigest()
        with self._lock:
            now = self._clock()
            cached = self._cache.get(cache_key)
            if cached and cached[0] > now and len(cached[1].body) <= cap:
                return replace(cached[1], cached=True)
            deadline = now + self._total_timeout
            current = url
            redirects, retries = 0, 0
            while True:
                if self._budget <= 0:
                    raise ResearchHttpError("request_budget_exhausted")
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise ResearchHttpError("timeout")
                # Failed DNS/validation attempts also spend the session budget,
                # bounding outstanding daemon resolver workers after timeouts.
                self._budget -= 1
                target = self._target(current, min(self._timeout, remaining))
                if self._last_request is not None:
                    self._pause(max(0, self._interval - (self._clock() - self._last_request)),
                                deadline)
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise ResearchHttpError("timeout")
                self._last_request = self._clock()
                try:
                    response = self._transport(target, outgoing,
                                               min(self._timeout, remaining), cap)
                except ResearchHttpError:
                    raise
                except (TimeoutError, socket.timeout):
                    raise ResearchHttpError("timeout") from None
                except Exception:
                    raise ResearchHttpError("transport_failed") from None
                if self._clock() > deadline:
                    raise ResearchHttpError("timeout")
                if len(response.body) > cap:
                    raise ResearchHttpError("response_too_large")
                fields = {key.lower(): value for key, value in response.headers.items()}
                status = response.status
                if status in (301, 302, 303, 307, 308):
                    if redirects >= self._redirects or not fields.get("location"):
                        raise ResearchHttpError("redirect_limit", status=status)
                    following = urljoin(current, fields["location"])
                    try:
                        next_parts = urlsplit(following)
                        next_host = next_parts.hostname
                    except ValueError:
                        raise ResearchHttpError("unsafe_url") from None
                    if next_host != target.host:
                        outgoing = {key: value for key, value in outgoing.items()
                                    if key.lower() in ("accept", "user-agent")}
                        following = urlunsplit(next_parts._replace(query=""))
                    current = following
                    redirects += 1
                    continue
                if status == 429 or 500 <= status <= 599:
                    if retries < self._retries:
                        wait = float(2 ** retries)
                        retry_after = fields.get("retry-after", "")
                        if retry_after:
                            try:
                                requested_wait = float(retry_after)
                                if not math.isfinite(requested_wait) or requested_wait < 0:
                                    raise ValueError("invalid_retry_after")
                                wait = max(wait, requested_wait)
                            except ValueError:
                                # Date-form/invalid Retry-After is not guessed.
                                raise ResearchHttpError("retry_later", status=status,
                                                        headers=fields) from None
                        if not 0 <= wait <= 4:
                            raise ResearchHttpError("retry_later", status=status, headers=fields)
                        self._pause(wait, deadline)
                        retries += 1
                        continue
                if not 200 <= status < 300:
                    raise ResearchHttpError("http_error", status=status, headers=fields)
                # Decode/compressed expansion is deliberately left to bounded
                # format parsers; JSON requests ask the server for identity.
                safe_url = urlunsplit(urlsplit(current)._replace(query="", fragment=""))
                clean = HttpResponse(status, usage_headers(fields), response.body, safe_url)
                if self._ttl and len(clean.body) <= 1024 * 1024:
                    if len(self._cache) >= 16:
                        self._cache.pop(next(iter(self._cache)))
                    self._cache[cache_key] = (self._clock() + self._ttl, clean)
                return clean

    def get_json(self, url: str, *, headers: Mapping[str, str] | None = None,
                 max_bytes: int | None = None) -> JsonResponse:
        response = self.get_bytes(url, headers={"Accept": "application/json", **(headers or {})},
                                  max_bytes=max_bytes)
        if response.headers.get("content-encoding", "identity").lower() not in ("identity", ""):
            raise ResearchHttpError("unsupported_encoding")
        try:
            data = json.loads(response.body)
        except (ValueError, UnicodeError, RecursionError):
            raise ResearchHttpError("invalid_json") from None
        return JsonResponse(data, response.headers, response.status, response.url, response.cached)
