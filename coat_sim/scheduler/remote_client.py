"""Authenticated remote client with explicit TLS/plaintext policy."""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path, PureWindowsPath
import re
import ssl
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import (HTTPRedirectHandler, HTTPSHandler, ProxyHandler,
                            Request, build_opener)

from .io import replace_with_retry

_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_MAX_SPEC_BYTES = 20 * 1024 * 1024
_MAX_BLOB_BYTES = 512 * 1024 * 1024
_MAX_CONTROL_BYTES = 64 * 1024
_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
_ARTIFACT_ID = re.compile(r"[0-9a-f]{64}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class RemoteServiceError(ValueError):
    """A remote service error with its stable code and optional HTTP status."""

    def __init__(self, code: str, message: str | None = None, http_status: int | None = None):
        self.code = str(code or "remote_service_error")
        self.http_status = http_status
        super().__init__(message or self.code)


class RemoteSubmissionUnknown(RemoteServiceError):
    """The HTTP acknowledgement was lost; only this job ID may be retried."""

    def __init__(self, job_id: str, *, expected_spec_sha256: str | None = None,
                 device_id: str | None = None):
        self.job_id = job_id
        self.expected_spec_sha256 = expected_spec_sha256
        self.device_id = device_id
        super().__init__("remote_submission_unknown", f"remote_submission_unknown:{job_id}")


class _NoRedirect(HTTPRedirectHandler):
    """Reject redirects so authorization headers can never cross origins."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RemoteServiceError("redirect_rejected", "remote redirects are not allowed", code)


class _SizedReader:
    """File-like HTTP body with an explicit length for urllib/http.client."""

    def __init__(self, stream, size: int):
        self.stream = stream
        self.size = size
        self.remaining = size

    def __len__(self):
        return self.size

    def read(self, size=-1):
        if self.remaining <= 0:
            return b""
        if size is None or size < 0 or size > self.remaining:
            size = self.remaining
        data = self.stream.read(size)
        self.remaining -= len(data)
        return data


class RemoteTransport:
    """Remote service adapter implementing the SchedulerClient transport seam.

    The base URL is explicit. HTTPS always uses certificate and hostname
    verification. HTTP requires a separate explicit authenticated plaintext
    choice, or the existing literal-IP loopback developer option.
    """

    # In-process scheduler test transports also use SchedulerClient.transport.
    # This identifies actual HTTP work for desktop thread-boundary decisions.
    remote_http = True

    _RPC_ACTIONS = frozenset({
        "status", "snapshot", "events", "page", "cancel", "result", "artifact",
        "run_result", "find_request", "refresh_estimates", "film_display_control", "reduction_live",
    })

    def __init__(self, base_url: str, *, token: str | None = None,
                 ca_file: str | Path | None = None, allow_loopback_http: bool = False,
                 allow_insecure_http: bool = False, device_id: str | None = None,
                 pending_root: str | Path | None = None):
        if type(allow_loopback_http) is not bool or type(allow_insecure_http) is not bool:
            raise ValueError("invalid_insecure_http_option")
        if not isinstance(base_url, str) or not base_url:
            raise ValueError("invalid_remote_base_url")
        parsed = urlsplit(base_url)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("remote_url_userinfo_forbidden")
        if parsed.query or parsed.fragment:
            raise ValueError("remote_url_query_or_fragment_forbidden")
        if parsed.scheme not in ("https", "http") or not parsed.hostname:
            raise ValueError("invalid_remote_base_url")
        if allow_insecure_http:
            if parsed.scheme != "http" or allow_loopback_http or ca_file is not None:
                raise ValueError("insecure_http_option_conflict")
            if (not isinstance(token, str) or len(token) < 16 or not token.isascii()
                    or any(not 33 <= ord(char) <= 126 for char in token)):
                raise ValueError("insecure_http_requires_valid_token")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("invalid_remote_base_url") from exc
        if parsed.scheme == "http":
            if not allow_insecure_http:
                try:
                    address = ipaddress.ip_address(parsed.hostname)
                except ValueError as exc:
                    raise ValueError("http_requires_loopback_ip") from exc
                if not allow_loopback_http or not address.is_loopback:
                    raise ValueError("insecure_remote_http_forbidden")
        elif allow_loopback_http and parsed.scheme != "http":
            # The flag only opts into loopback HTTP; it never changes TLS policy.
            pass
        if token is not None and (not isinstance(token, str) or not token or not token.isascii()
                                  or any(char.isspace() or ord(char) < 32 for char in token)):
            raise ValueError("invalid_remote_token")
        base_path = parsed.path.rstrip("/")
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        netloc = host + (f":{port}" if port is not None else "")
        self.base_url = urlunsplit((parsed.scheme, netloc, base_path, "", ""))
        self.token = token
        if device_id is not None and (not isinstance(device_id, str)
                                      or not 1 <= len(device_id) <= 120):
            raise ValueError("invalid_remote_device_id")
        self.device_id = device_id
        self.pending_root = Path(pending_root) if pending_root is not None else None
        self.ca_file = ca_file
        self.allow_insecure_http = allow_insecure_http
        self.allow_loopback_http = allow_loopback_http
        self._opener = build_opener(
            ProxyHandler({}), _NoRedirect(),
            HTTPSHandler(context=ssl.create_default_context(cafile=str(ca_file) if ca_file else None)),
        )

    def _headers(self, content_type: str | None = None):
        headers = {"Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        if self.token is not None:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _read_json(self, response, http_status: int | None = None):
        payload = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(payload) > _MAX_RESPONSE_BYTES:
            raise RemoteServiceError("response_too_large", http_status=http_status)
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteServiceError("invalid_remote_response", http_status=http_status) from exc
        if not isinstance(value, dict):
            raise RemoteServiceError("invalid_remote_response", http_status=http_status)
        return value

    def _open(self, request: Request, *, timeout: float = 30):
        try:
            return self._opener.open(request, timeout=timeout)
        except HTTPError as exc:
            try:
                value = self._read_json(exc, exc.code)
                self._raise_envelope(value, exc.code)
                raise RemoteServiceError("remote_http_error", http_status=exc.code)
            finally:
                exc.close()
        except (URLError, OSError, http.client.HTTPException) as exc:
            # Operating-system transport text can contain URL/user data.
            raise RemoteServiceError("remote_transport_error") from exc

    @staticmethod
    def _raise_envelope(value: dict, http_status: int | None = None):
        if type(value.get("api_version")) is not int or value.get("api_version") != 1 or type(value.get('ok')) is not bool:
            raise RemoteServiceError('invalid_remote_response', http_status=http_status)
        if value.get("ok") is not False:
            return
        error = value.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message")
        else:
            code, message = "remote_service_error", None
        raise RemoteServiceError(code or "remote_service_error", message, http_status)

    def _json_call(self, method: str, path: str, payload: dict | None = None,
                   *, timeout: float = 30):
        data = None if payload is None else json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        if data is not None and len(data) > _MAX_CONTROL_BYTES:
            raise RemoteServiceError("request_too_large")
        request = Request(self.base_url + path, data=data, headers=self._headers(
            "application/json" if data is not None else None), method=method)
        with self._open(request, timeout=timeout) as response:
            value = self._read_json(response, getattr(response, "status", None))
        self._raise_envelope(value, getattr(response, "status", None))
        if value.get("api_version") == 1 and value.get("ok") is True:
            result = value.get("result")
            if not isinstance(result, dict):
                raise RemoteServiceError("invalid_remote_response")
            return result
        raise RemoteServiceError('invalid_remote_response')

    def health(self) -> dict:
        return self._json_call("GET", "/api/v1/health")

    def capabilities(self) -> dict:
        return self._json_call("GET", "/api/v1/capabilities")

    def request(self, action: str, arguments: dict) -> dict:
        if action == "stop":
            raise RemoteServiceError("remote_stop_disabled", "remote stop is disabled")
        if action == "submit":
            if not isinstance(arguments, dict):
                raise RemoteServiceError("invalid_submission")
            return self.submit(arguments.get("spec"), arguments.get("job_id"),
                               arguments.get("metadata"))
        if action == "health":
            return self.health()
        if action == "capabilities":
            return self.capabilities()
        if action not in self._RPC_ACTIONS or not isinstance(arguments, dict):
            raise RemoteServiceError("unknown_scheduler_action")
        return self._json_call("POST", "/api/v1/rpc", {
            "api_version": 1, "action": action, "arguments": arguments,
        }, timeout=40 if action == "events" else 30)

    def _put_blob(self, body, *, sha256: str, size: int, close_body=None):
        if type(size) is not int or size < 0 or size > _MAX_BLOB_BYTES:
            if close_body is not None:
                close_body.close()
            raise RemoteServiceError("blob_too_large")
        request = Request(self.base_url + f"/api/v1/blobs/{sha256}", data=body,
                          headers=self._headers("application/octet-stream"), method="PUT")
        request.add_header("Content-Length", str(size))
        try:
            with self._open(request, timeout=40) as response:
                result = self._read_json(response, getattr(response, "status", None))
            self._raise_envelope(result, getattr(response, "status", None))
            if result.get("api_version") == 1 and result.get("ok") is True:
                result = result.get("result")
            else:
                raise RemoteServiceError('invalid_remote_response')
            if not isinstance(result, dict):
                raise RemoteServiceError("invalid_remote_response")
            return result
        finally:
            if close_body is not None:
                close_body.close()

    def upload_blob(self, source: bytes | Path) -> dict:
        if isinstance(source, bytes):
            if len(source) > _MAX_BLOB_BYTES:
                raise RemoteServiceError("blob_too_large")
            digest = hashlib.sha256(source).hexdigest()
            result = self._put_blob(source, sha256=digest, size=len(source))
        else:
            path = Path(source)
            if not path.is_file() or path.is_symlink():
                raise RemoteServiceError("invalid_blob_source")
            size = path.stat().st_size
            if size > _MAX_BLOB_BYTES:
                raise RemoteServiceError("blob_too_large")
            digest_obj = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest_obj.update(block)
            digest = digest_obj.hexdigest()
            stream = path.open("rb")
            result = self._put_blob(_SizedReader(stream, size), sha256=digest,
                                    size=size, close_body=stream)
        if (result.get("sha256") != digest or type(result.get('bytes')) is not int
                or result['bytes'] != (len(source) if isinstance(source, bytes) else size)):
            raise RemoteServiceError("blob_descriptor_mismatch")
        return {"sha256": digest, "bytes": len(source) if isinstance(source, bytes) else size}

    def submit(self, spec, job_id: str, metadata: dict | None = None) -> dict:
        from .film_display import CONFIG_KEY, prepare_display_configuration
        if metadata not in (None, {}) and (not isinstance(metadata, dict)
                or (self.device_id is None and set(metadata) - {CONFIG_KEY})):
            raise RemoteServiceError("remote_metadata_unsupported")
        display_config = (prepare_display_configuration(metadata[CONFIG_KEY])
                          if isinstance(metadata, dict) and CONFIG_KEY in metadata else None)
        if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
            raise RemoteServiceError("invalid_job_id")
        from .remote_contract import prepare_portable_spec
        if self.device_id is not None:
            from .jobs import JobSpec
            try:
                raw = spec.to_dict() if hasattr(spec, "to_dict") else dict(spec)
                raw = json.loads(json.dumps(raw, ensure_ascii=False, allow_nan=False))
                inputs = raw["inputs"]
                if not isinstance(inputs, dict):
                    raise ValueError("invalid inputs")
                existing = inputs.get("device_binding")
                binding = {"schema_version": 1, "device_id": self.device_id}
                if existing is not None and existing != binding:
                    raise RemoteServiceError("device_binding_mismatch")
                inputs["device_binding"] = binding
                spec = JobSpec.parse(raw)
            except RemoteServiceError:
                raise
            except (TypeError, ValueError, KeyError) as exc:
                raise RemoteServiceError("invalid_portable_spec") from exc
        prepared = prepare_portable_spec(spec)
        to_dict = getattr(prepared, "to_dict", None)
        if not callable(to_dict):
            raise RemoteServiceError("invalid_portable_spec")
        from .jobs import canonical
        try:
            frozen_bytes = canonical(to_dict()).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise RemoteServiceError("invalid_portable_spec") from exc
        if len(frozen_bytes) > _MAX_SPEC_BYTES:
            raise RemoteServiceError("spec_too_large")
        blob = self.upload_blob(frozen_bytes)
        spec_sha256 = blob["sha256"]
        pending = self._pending_path(spec_sha256) if self.device_id is not None else None
        if pending is not None:
            prior = self._write_pending(pending, job_id, spec_sha256)
            if prior != job_id:
                # The UI has generated a fresh ID for identical inputs, but a
                # prior POST may already be running. Never duplicate it.
                raise RemoteSubmissionUnknown(prior, expected_spec_sha256=spec_sha256,
                                              device_id=self.device_id)
        envelope = {
            "api_version": 1, "executor_schema_version": 1, "job_id": job_id,
            "spec_blob": blob,
        }
        if self.device_id is not None:
            envelope.update(device_binding_version=1, device_id=self.device_id)
        if display_config is not None:
            envelope[CONFIG_KEY] = display_config
        try:
            if getattr(prepared, "executor", None) == "appearance_render":
                receipt = self._json_call("POST", "/api/v1/jobs", envelope, timeout=120)
            else:
                receipt = self._json_call("POST", "/api/v1/jobs", envelope)
        except RemoteServiceError as exc:
            explicit_rejection = (exc.http_status is not None
                                  and 400 <= exc.http_status < 500
                                  and exc.code not in {"invalid_remote_response"})
            if explicit_rejection:
                if pending is not None:
                    pending.unlink(missing_ok=True)
                raise
            # Timeouts, malformed replies and server errors say nothing about
            # whether admission committed. Query the same immutable ID.
            try:
                receipt = self._json_call("POST", "/api/v1/rpc", {
                    "api_version": 1, "action": "status", "arguments": {"job_id": job_id},
                })
            except RemoteServiceError:
                raise RemoteSubmissionUnknown(job_id, expected_spec_sha256=spec_sha256,
                                              device_id=self.device_id) from None
        summary = receipt.get("input_summary") if isinstance(receipt, dict) else None
        if self.device_id is not None and (not isinstance(summary, dict)
                or summary.get("device_id") != self.device_id
                or summary.get("external_spec_sha256") != spec_sha256):
            raise RemoteServiceError("submission_receipt_mismatch")
        if pending is not None:
            pending.unlink(missing_ok=True)
        return receipt

    def _pending_path(self, digest: str) -> Path | None:
        if self.pending_root is None:
            return None
        key = hashlib.sha256((self.base_url + "\n" + str(self.device_id) + "\n" + digest).encode()).hexdigest()
        return self.pending_root / (key + ".json")

    @staticmethod
    def _read_pending(path: Path) -> str | None:
        if not path.exists():
            return None
        if path.is_symlink():
            raise RemoteServiceError("invalid_pending_submission")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise RemoteServiceError("invalid_pending_submission") from exc
        identifier = value.get("job_id") if isinstance(value, dict) else None
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier):
            raise RemoteServiceError("invalid_pending_submission")
        return identifier

    @staticmethod
    def _write_pending(path: Path, job_id: str, digest: str) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return RemoteTransport._read_pending(path) or job_id
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"job_id": job_id, "spec_sha256": digest}, stream)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return job_id

    def register_deposition_asset(self, source: Path) -> dict:
        blob = self.upload_blob(Path(source))
        result = self._json_call("POST", "/api/v1/assets", {
            "api_version": 1, "blob": blob, "kind": "deposition_bundle",
        })
        self._validate_descriptor(result.get('artifact_id'), result)
        if (result.get("name") != "input.coatasset" or result.get("role") != "bundle"
                or result.get('sha256') != blob['sha256'] or result.get('bytes') != blob['bytes']):
            raise RemoteServiceError("invalid_asset_descriptor")
        return result

    def register_appearance_asset(self, source: Path) -> dict:
        blob = self.upload_blob(Path(source))
        result = self._json_call("POST", "/api/v1/assets", {
            "api_version": 1, "blob": blob, "kind": "appearance_bundle",
        })
        self._validate_descriptor(result.get("artifact_id"), result)
        if (result.get("name") != "input.coatasset" or result.get("role") != "appearance_bundle"
                or result.get("sha256") != blob["sha256"] or result.get("bytes") != blob["bytes"]):
            raise RemoteServiceError("invalid_asset_descriptor")
        return result

    @staticmethod
    def _validate_descriptor(artifact_id: str, value: dict):
        if not isinstance(artifact_id, str) or not _ARTIFACT_ID.fullmatch(artifact_id):
            raise RemoteServiceError("invalid_artifact_id")
        if not isinstance(value, dict) or value.get("artifact_id") != artifact_id:
            raise RemoteServiceError("artifact_metadata_mismatch")
        name = value.get("name")
        if (not isinstance(name, str) or not name or name in (".", "..") or "\x00" in name
                or any(ord(char) < 32 for char in name)
                or any(char in name for char in '<>:"/\\|?*')
                or name.endswith((".", " "))
                or name.split('.', 1)[0].upper() in ({'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} |
                    {f'{prefix}{i}' for prefix in ('COM', 'LPT') for i in range(1, 10)})
                or Path(name).name != name or PureWindowsPath(name).name != name):
            raise RemoteServiceError("invalid_artifact_name")
        size, digest = value.get("bytes"), value.get("sha256")
        if type(size) is not int or size < 0 or size > _MAX_ARTIFACT_BYTES or not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise RemoteServiceError("invalid_artifact_metadata")
        return name, size, digest

    def download(self, artifact_id: str, destination) -> Path:
        if not isinstance(artifact_id, str) or not _ARTIFACT_ID.fullmatch(artifact_id):
            raise RemoteServiceError("invalid_artifact_id")
        descriptor = self._json_call("GET", f"/api/v1/artifacts/{artifact_id}/metadata")
        _, expected_size, expected_digest = self._validate_descriptor(artifact_id, descriptor)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        digest = hashlib.sha256()
        count = 0
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=destination.parent,
                                             suffix=".tmp", delete=False) as target:
                temporary = Path(target.name)
                request = Request(self.base_url + f"/api/v1/artifacts/{artifact_id}",
                                  headers=self._headers())
                with self._open(request, timeout=40) as response:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        count += len(block)
                        if count > _MAX_ARTIFACT_BYTES or count > expected_size:
                            raise RemoteServiceError("artifact_size_mismatch")
                        target.write(block)
                        digest.update(block)
                target.flush()
                os.fsync(target.fileno())
            if count != expected_size or digest.hexdigest() != expected_digest:
                raise RemoteServiceError("artifact_integrity_failed")
            replace_with_retry(temporary, destination)
            return destination
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def make_remote_client(base_url: str, cache_root, **transport_options):
    """Construct a SchedulerClient whose cache root is independent of the service."""
    from .client import SchedulerClient
    return SchedulerClient(root=cache_root, transport=RemoteTransport(
        base_url, pending_root=Path(cache_root) / "artifacts/runtime/remote-pending",
        **transport_options))
