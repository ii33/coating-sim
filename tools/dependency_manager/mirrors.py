"""HTTPS-only PyPI mirror metadata and bounded mirror benchmarking.

This module intentionally uses only the Python standard library. It does not
install packages or change persistent pip configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
from html.parser import HTMLParser
import math
import re
import ssl
import time
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import (  # noqa: F401 - kept explicit for easy patching in tests
    HTTPSHandler,
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
    getproxies_environment,
)


MIRRORS: dict[str, dict[str, str]] = {
    "pypi": {
        "name": "PyPI",
        "url": "https://pypi.org/simple/",
        "documentation": "https://docs.pypi.org/api/index-api/",
    },
    "tuna": {
        "name": "Tsinghua TUNA",
        "url": "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple/",
        "documentation": "https://mirror.tuna.tsinghua.edu.cn/help/pypi/",
    },
    "ustc": {
        "name": "USTC Mirror",
        "url": "https://mirrors.ustc.edu.cn/pypi/simple/",
        "documentation": "https://ipv4.mirrors.ustc.edu.cn/help/pypi.html",
    },
}

_PROXY_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
_NO_PROXY_KEYS = ("NO_PROXY", "no_proxy")
_MAX_MIRRORS = 4
_MAX_INDEX_BYTES = 1024 * 1024
_MAX_SAMPLE_BYTES = 1024 * 1024
_SAMPLE_WHEEL = "pip-25.2-py3-none-any.whl"
_CHUNK_SIZE = 64 * 1024
_TOKEN_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"api[_-]?key|token|secret|password|passwd|client[_-]?secret)"
    r"(\s*[:=]\s*)([^\s&;,]+)"
)
_AUTH_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization)(\s*[:=]\s*)"
    r"(?:bearer\s+|basic\s+)?[^\s,;]+"
)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _clean_proxy_url(value: str, *, strip_query: bool = True) -> str:
    """Return an HTTP(S) URL without credentials or sensitive query material."""
    parts = urlsplit(value)
    if not parts.scheme or not parts.hostname:
        return "[REDACTED]"
    host = parts.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        return "[REDACTED]"
    netloc = host + (f":{port}" if port is not None else "")
    query = "" if strip_query else parts.query
    return urlunsplit((parts.scheme.lower(), netloc, parts.path, query, ""))


def redact(text: object, proxy: "ProxyConfig | None" = None) -> str:
    """Remove URL credentials, query strings, proxy secrets, and common tokens."""
    result = str(text)

    # Replace URL-shaped values before token matching so encoded credentials and
    # arbitrary query names are covered, not just the common token parameter names.
    def replace_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        suffix = ""
        while raw and raw[-1] in ".,);]}":
            suffix = raw[-1] + suffix
            raw = raw[:-1]
        return _clean_proxy_url(raw, strip_query=True) + suffix

    result = _URL_RE.sub(replace_url, result)
    result = _TOKEN_ASSIGNMENT_RE.sub(
        lambda m: m.group(1) + m.group(2) + "[REDACTED]", result
    )
    result = _AUTH_ASSIGNMENT_RE.sub(
        lambda m: m.group(1) + m.group(2) + "[REDACTED]", result
    )

    if proxy is not None:
        try:
            proxy_value = proxy._effective_url() if proxy.mode == "env" else proxy.url
            proxy_parts = urlsplit(proxy_value)
        except (AttributeError, TypeError, ValueError):
            proxy_parts = None
        if proxy_parts is not None:
            for secret in (proxy_parts.username, proxy_parts.password):
                if secret:
                    candidates = {
                        secret,
                        unquote(secret),
                        quote(unquote(secret), safe=""),
                    }
                    for candidate in sorted(candidates, key=len, reverse=True):
                        if candidate:
                            result = result.replace(candidate, "[REDACTED]")
    return result


def _proxy_values_from_environment() -> list[str]:
    # getproxies_environment applies urllib's CGI HTTP_PROXY safeguard and reads
    # only environment variables (not desktop/system proxy settings).
    proxies = getproxies_environment()
    # This client requests only HTTP(S) URLs. Explicit HTTP/HTTPS proxy settings
    # take precedence over ALL_PROXY; an irrelevant fallback must not block a
    # valid explicit setup (or unexpectedly route these requests elsewhere).
    values = [
        str(proxies[key]).strip()
        for key in ("http", "https")
        if str(proxies.get(key, "")).strip()
    ]
    if not values and str(proxies.get("all", "")).strip():
        values = [str(proxies["all"]).strip()]
    return list(dict.fromkeys(values))


def _validate_proxy_url(value: str) -> None:
    try:
        parts = urlsplit(value)
        port = parts.port
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid proxy URL") from exc
    if parts.scheme.lower() != "http":
        raise ValueError(
            "Only http:// CONNECT proxies are supported; HTTPS-proxy TLS-in-TLS and SOCKS are unsupported"
        )
    if not parts.hostname:
        raise ValueError("Proxy URL must include a host")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise ValueError("Proxy URL cannot include a path, query, or fragment")
    if port is not None and not (1 <= port <= 65535):
        raise ValueError("Invalid proxy port")


@dataclass
class ProxyConfig:
    """Transient proxy selection for the current operation only."""

    mode: str = "env"
    url: str = ""

    def validate(self) -> None:
        if self.mode not in {"none", "env", "http"}:
            raise ValueError("Proxy mode must be none, env, or http")
        if self.mode == "http":
            if not self.url:
                raise ValueError("HTTP proxy mode requires a proxy URL")
            _validate_proxy_url(self.url)
            return
        if self.url:
            raise ValueError("A proxy URL is only valid in HTTP proxy mode")
        if self.mode == "env":
            values = _proxy_values_from_environment()
            for value in values:
                _validate_proxy_url(value)
            if len(values) > 1:
                raise ValueError(
                    "Environment proxy variables must use one consistent proxy"
                )

    def _effective_url(self) -> str:
        if self.mode == "none":
            return ""
        if self.mode == "http":
            return self.url
        values = _proxy_values_from_environment()
        return values[0] if values else ""

    def public(self) -> dict[str, Any]:
        """Return the redacted, non-secret form safe for UI/log output."""
        self.validate()
        endpoint = (
            _clean_proxy_url(self._effective_url()) if self._effective_url() else None
        )
        return {"mode": self.mode, "endpoint": endpoint}

    def environment(self) -> dict[str, str]:
        """Return subprocess proxy overrides without modifying the parent process."""
        self.validate()
        endpoint = self._effective_url()
        env = {key: endpoint for key in _PROXY_KEYS}
        env.update({key: "" for key in _NO_PROXY_KEYS})
        return env


def _effective_proxy_map(proxy: ProxyConfig) -> dict[str, str]:
    proxy.validate()
    endpoint = proxy._effective_url()
    return {"http": endpoint, "https": endpoint} if endpoint else {}


def _safe_https_url(value: str, allowed_hosts: set[str]) -> str:
    try:
        parts = urlsplit(value)
        _ = parts.port
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid download URL") from exc
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise ValueError("Only HTTPS download URLs are permitted")
    if parts.username is not None or parts.password is not None:
        raise ValueError("Download URL credentials are not permitted")
    if parts.hostname.lower() not in allowed_hosts:
        raise ValueError("Download host is not approved for this mirror")
    return value


class _WheelLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            for key, value in attrs:
                if key.lower() == "href" and value:
                    self.hrefs.append(value)


class _CheckedRedirectHandler(HTTPRedirectHandler):
    def __init__(self, allowed_hosts: set[str]) -> None:
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        resolved_url = urljoin(req.full_url, newurl)
        _safe_https_url(resolved_url, self.allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, resolved_url)


class _UniformProxyHandler(ProxyHandler):
    """Like urllib's handler, but deliberately ignores NO_PROXY bypass rules."""

    def proxy_open(self, req: Any, proxy: str, proxy_type: str) -> Any:
        orig_type = req.type
        proxy_parts = urlsplit(proxy)
        scheme = proxy_parts.scheme.lower() or orig_type
        hostport = proxy_parts.netloc.rsplit("@", 1)[-1]
        if not hostport:
            raise ValueError("Proxy URL must include a host")
        if proxy_parts.username is not None:
            user_pass = (
                f"{unquote(proxy_parts.username)}:{unquote(proxy_parts.password or '')}"
            )
            creds = base64.b64encode(user_pass.encode("utf-8")).decode("ascii")
            req.add_header("Proxy-authorization", "Basic " + creds)
        req.set_proxy(unquote(hostport), scheme)
        if orig_type == scheme or orig_type == "https":
            return None
        return self.parent.open(req, timeout=req.timeout)


def _make_opener(allowed_hosts: set[str], proxies: dict[str, str]) -> Any:
    return build_opener(
        _UniformProxyHandler(proxies),
        _CheckedRedirectHandler(allowed_hosts),
        HTTPSHandler(context=ssl.create_default_context()),
    )


def _set_read_timeout(response: Any, timeout: float) -> None:
    """Tighten urllib's socket timeout between reads to enforce a total deadline."""
    fp = getattr(response, "fp", None)
    raw = getattr(fp, "raw", None)
    sock = getattr(raw, "_sock", None)
    setter = getattr(sock, "settimeout", None)
    if callable(setter):
        setter(timeout)


def _read_response(
    response: Any,
    limit: int,
    cancel_event: Any,
    *,
    require_eof: bool,
    deadline: float | None = None,
    request_timeout: float | None = None,
) -> tuple[bytes, bool, bool]:
    chunks: list[bytes] = []
    total = 0
    while total < limit:
        if cancel_event is not None and cancel_event.is_set():
            return b"".join(chunks), True, False
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Mirror benchmark exceeded its total deadline")
            socket_timeout = (
                min(remaining, request_timeout) if request_timeout else remaining
            )
            _set_read_timeout(response, socket_timeout)
        block = getattr(response, "read1", response.read)(
            min(_CHUNK_SIZE, limit - total)
        )
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("Mirror benchmark exceeded its total deadline")
        if not block:
            return b"".join(chunks), False, False
        chunks.append(block)
        total += len(block)
    # For the index, reaching the cap is treated as an oversized/truncated page.
    # For a wheel sample, the cap is the intended stopping point and no extra byte
    # is read, keeping actual transfer size bounded by the requested sample.
    return b"".join(chunks), False, bool(require_eof)


def _open_response(
    opener: Any, url: str, timeout: float, *, accept: str, allowed_hosts: set[str]
) -> Any:
    request = Request(
        url, headers={"Accept": accept, "User-Agent": "coating-dependency-manager/1"}
    )
    response = opener.open(request, timeout=timeout)
    final_url = response.geturl() if hasattr(response, "geturl") else url
    # The redirect handler validates every hop; validate the final response too.
    try:
        _safe_https_url(final_url, allowed_hosts)
    except Exception:
        close = getattr(response, "close", None)
        if callable(close):
            close()
        raise
    return response


def _empty_result(mirror_id: str, mirror: dict[str, str] | None) -> dict[str, Any]:
    return {
        "mirror": mirror_id,
        "name": mirror["name"] if mirror else mirror_id,
        "reachable": False,
        "latency_ms": None,
        "download_bytes": 0,
        "throughput_mbps": None,
        "error": None,
        "sample": _SAMPLE_WHEEL,
        "download_url": None,
        "download_host": None,
        "sample_origin": None,
        "ranking_eligible": False,
    }


def _failure_result(result: dict[str, Any], error: object, proxy: ProxyConfig) -> None:
    result["error"] = redact(error, proxy)


def benchmark_mirrors(
    mirror_ids: list[str] | tuple[str, ...],
    proxy: ProxyConfig,
    cancel_event: Any,
    timeout: float = 5,
    sample_bytes: int = 262144,
) -> dict[str, Any]:
    """Benchmark at most four approved HTTPS mirrors using one bounded wheel sample."""
    requested_raw = list(mirror_ids) if isinstance(mirror_ids, (list, tuple)) else []
    requested = [mid if isinstance(mid, str) else str(mid) for mid in requested_raw]
    results = [_empty_result(mid, MIRRORS.get(mid)) for mid in requested]
    output: dict[str, Any] = {
        "schema_version": 1,
        "operation": "benchmark",
        "status": "failed",
        "results": results,
        "recommended_mirror": None,
        "recommendation_reason": None,
        "warnings": [],
    }

    try:
        if not isinstance(proxy, ProxyConfig):
            raise ValueError("proxy must be a ProxyConfig")
        proxy.validate()
        if any(not isinstance(mid, str) for mid in requested_raw):
            raise ValueError("Mirror IDs must be strings")
        if not requested:
            raise ValueError("At least one mirror must be selected")
        if len(requested) > _MAX_MIRRORS:
            raise ValueError("At most four mirrors may be benchmarked at once")
        if len(set(requested)) != len(requested):
            raise ValueError("Mirror IDs must be unique")
        if any(mid not in MIRRORS for mid in requested):
            raise ValueError("Unknown mirror ID")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a positive number")
        if (
            isinstance(sample_bytes, bool)
            or not isinstance(sample_bytes, int)
            or not (1 <= sample_bytes <= _MAX_SAMPLE_BYTES)
        ):
            raise ValueError("sample_bytes must be between 1 and 1048576")
    except (TypeError, ValueError) as exc:
        output["warnings"].append(
            redact(exc, proxy if isinstance(proxy, ProxyConfig) else None)
        )
        return output

    total_deadline_seconds = min(max(float(timeout) * 3, float(timeout)), 30.0)
    output["warnings"].append(
        f"Sample: {_SAMPLE_WHEEL}; reads at most {sample_bytes} wheel bytes per mirror. "
        f"Index reads are capped at {_MAX_INDEX_BYTES} bytes. latency_ms measures the complete index request; "
        "throughput_mbps includes connection overhead and is only an estimate. "
        f"Each mirror has a {total_deadline_seconds:g}-second total deadline."
    )
    proxies = _effective_proxy_map(proxy)
    cancelled = False
    for result in results:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            result["error"] = "Cancelled before benchmark"
            continue
        mirror = MIRRORS[result["mirror"]]
        index_url = mirror["url"].rstrip("/") + "/pip/"
        mirror_host = urlsplit(mirror["url"]).hostname
        allowed_hosts = {str(mirror_host).lower(), "files.pythonhosted.org"}
        opener = None
        index_started = time.perf_counter()
        mirror_deadline = time.monotonic() + min(
            max(float(timeout) * 3, float(timeout)), 30.0
        )
        try:
            _safe_https_url(index_url, allowed_hosts)
            opener = _make_opener(allowed_hosts, proxies)
            index_timeout = min(float(timeout), mirror_deadline - time.monotonic())
            if index_timeout <= 0:
                raise TimeoutError("Mirror benchmark exceeded its total deadline")
            with _open_response(
                opener,
                index_url,
                index_timeout,
                accept="text/html",
                allowed_hosts=allowed_hosts,
            ) as response:
                index_response_url = (
                    response.geturl() if hasattr(response, "geturl") else index_url
                )
                index_data, was_cancelled, truncated = _read_response(
                    response,
                    _MAX_INDEX_BYTES,
                    cancel_event,
                    require_eof=True,
                    deadline=mirror_deadline,
                    request_timeout=index_timeout,
                )
            if was_cancelled:
                cancelled = True
                result["error"] = "Cancelled while reading mirror index"
                continue
            if truncated:
                raise ValueError("Mirror index exceeds the 1 MiB read limit")
            result["latency_ms"] = round(
                (time.perf_counter() - index_started) * 1000, 3
            )
            parser = _WheelLinks()
            parser.feed(index_data.decode("utf-8", errors="replace"))
            wheel_url = None
            for href in parser.hrefs:
                candidate = urljoin(index_response_url, href)
                path_name = unquote(urlsplit(candidate).path.rsplit("/", 1)[-1])
                if path_name == _SAMPLE_WHEEL:
                    wheel_url = _safe_https_url(candidate, allowed_hosts)
                    break
            if not wheel_url:
                raise ValueError(f"Mirror did not list {_SAMPLE_WHEEL}")

            download_started = time.perf_counter()
            wheel_timeout = min(float(timeout), mirror_deadline - time.monotonic())
            if wheel_timeout <= 0:
                raise TimeoutError("Mirror benchmark exceeded its total deadline")
            with _open_response(
                opener,
                wheel_url,
                wheel_timeout,
                accept="application/octet-stream",
                allowed_hosts=allowed_hosts,
            ) as response:
                final_download_url = (
                    response.geturl() if hasattr(response, "geturl") else wheel_url
                )
                final_download_host = (
                    urlsplit(final_download_url).hostname or ""
                ).lower()
                result["download_url"] = redact(final_download_url, proxy)
                result["download_host"] = final_download_host
                result["sample_origin"] = (
                    "mirror"
                    if final_download_host == str(mirror_host).lower()
                    else "pypi-cdn"
                    if final_download_host == "files.pythonhosted.org"
                    else None
                )
                result["ranking_eligible"] = result["sample_origin"] == "mirror" or (
                    result["mirror"] == "pypi" and result["sample_origin"] == "pypi-cdn"
                )
                wheel_data, was_cancelled, _ = _read_response(
                    response,
                    sample_bytes,
                    cancel_event,
                    require_eof=False,
                    deadline=mirror_deadline,
                    request_timeout=wheel_timeout,
                )
            result["download_bytes"] = len(wheel_data)
            if was_cancelled:
                cancelled = True
                result["error"] = "Cancelled while reading wheel sample"
                continue
            if not wheel_data:
                raise ValueError("Wheel sample was empty")
            elapsed = max(time.perf_counter() - download_started, 1e-9)
            result["throughput_mbps"] = round(
                len(wheel_data) * 8 / elapsed / 1_000_000, 3
            )
            result["reachable"] = True
        except Exception as exc:  # network/parser failures are per-mirror results
            _failure_result(result, exc, proxy)
        finally:
            if opener is not None:
                close = getattr(opener, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass

    if cancel_event is not None and cancel_event.is_set():
        cancelled = True
    rankable_downloads = [
        item for item in results if item["reachable"] and item["ranking_eligible"]
    ]
    cdn_downloads = [
        item
        for item in results
        if item["reachable"]
        and item["sample_origin"] == "pypi-cdn"
        and not item["ranking_eligible"]
    ]
    for item in cdn_downloads:
        output["warnings"].append(
            f"{item['mirror']} sample came from the PyPI CDN at {item['download_host']}; "
            "its throughput is not used to rank mirror download speed."
        )
    if cancelled:
        output["status"] = "cancelled"
        output["recommended_mirror"] = None
        output["recommendation_reason"] = (
            "No recommendation because the benchmark was cancelled."
        )
    else:
        reachable = [item for item in results if item["reachable"]]
        output["status"] = "success" if reachable else "failed"
        if rankable_downloads:
            best = min(
                rankable_downloads,
                key=lambda item: (
                    -(item["throughput_mbps"] or 0),
                    item["latency_ms"]
                    if item["latency_ms"] is not None
                    else float("inf"),
                    item["mirror"],
                ),
            )
            output["recommended_mirror"] = best["mirror"]
            if best["mirror"] == "pypi" and best["sample_origin"] == "pypi-cdn":
                output["recommendation_reason"] = (
                    "Selected PyPI using its official files.pythonhosted.org package CDN sample; "
                    "CDN samples from TUNA/USTC are excluded from mirror-speed ranking."
                )
            else:
                official_pypi_sample = any(
                    item["mirror"] == "pypi" and item["sample_origin"] == "pypi-cdn"
                    for item in rankable_downloads
                )
                source_note = (
                    "The official PyPI CDN sample is also ranking-eligible. "
                    if official_pypi_sample
                    else ""
                )
                output["recommendation_reason"] = (
                    "Selected from a sample downloaded from the index host. "
                    + source_note
                    + "CDN samples are excluded from TUNA/USTC mirror-speed ranking."
                )
        elif cdn_downloads:
            output["recommendation_reason"] = (
                "No mirror recommendation: downloads came from files.pythonhosted.org (PyPI CDN), "
                "so their throughput does not measure mirror download speed."
            )
        else:
            output["recommendation_reason"] = (
                "No recommendation because no downloadable sample was reachable."
            )
    return output


__all__ = ["MIRRORS", "ProxyConfig", "benchmark_mirrors", "redact"]
