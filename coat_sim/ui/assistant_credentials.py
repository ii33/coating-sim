"""Endpoint-bound API secrets through the cross-platform OS keyring.

Project files contain addresses/cache metadata only, never API keys.  Supported
secure backends are Windows Credential Locker, macOS Keychain and Linux desktop
Secret Service/KWallet.  There is no plaintext-file fallback.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

SERVICE = "coat_sim/assistant/providers"
MCP_SERVICE = "coat_sim/assistant/external-mcp"
COMPUTE_SERVICE = "coat_sim/compute-server"
LEGACY_SERVICE = "coat_sim/assistant/api-key"
LEGACY_USER = "coat_sim"
SETTINGS_PATH = Path(__file__).resolve().parents[2] / "config" / "ui_settings.json"
_SECURE_BACKENDS = ("keyring.backends.Windows", "keyring.backends.macOS", "keyring.backends.OS_X", "keyring.backends.SecretService", "keyring.backends.kwallet")

# Only static labels reach the UI. Never render backend exception text or args.
_STORE_ERRORS = {
    "unavailable": "系统安全凭据后端不可用",
    "locked": "系统钱包仍锁定或未能解锁",
    "cancelled": "系统凭据授权提示已取消",
    "init": "系统凭据库初始化失败",
    "dbus": "桌面凭据服务通信失败（D-Bus）",
    "access_denied": "系统拒绝访问凭据库",
    "write_failed": "系统凭据库写入失败",
    "unknown": "系统凭据操作失败，原因尚未识别",
}
_KNOWN_ERRORS = {
    ("keyring.errors", "NoKeyringError"): "unavailable",
    ("keyring.errors", "KeyringLocked"): "locked",
    ("keyring.errors", "InitError"): "init",
    ("keyring.errors", "PasswordSetError"): "write_failed",
    ("secretstorage.exceptions", "LockedException"): "locked",
    ("secretstorage.exceptions", "PromptDismissedException"): "cancelled",
    ("secretstorage.exceptions", "SecretServiceNotAvailableException"): "dbus",
    ("secretstorage.exceptions", "ItemNotFoundException"): "init",
    ("jeepney.wrappers", "DBusErrorResponse"): "dbus",
    ("dbus.exceptions", "DBusException"): "dbus",
}


# Exact protocol names only: never display arbitrary name/message/body values.
_DBUS_CODES = {
    "org.freedesktop.DBus.Error.ServiceUnknown": "service_unknown",
    "org.freedesktop.DBus.Error.NameHasNoOwner": "no_owner",
    "org.freedesktop.DBus.Error.NoReply": "no_reply",
    "org.freedesktop.DBus.Error.Timeout": "timeout",
    "org.freedesktop.DBus.Error.TimedOut": "timed_out",
    "org.freedesktop.DBus.Error.Disconnected": "disconnected",
    "org.freedesktop.DBus.Error.AccessDenied": "access_denied",
    "org.freedesktop.DBus.Error.AuthFailed": "auth_failed",
    "org.freedesktop.DBus.Error.UnknownObject": "unknown_object",
    "org.freedesktop.DBus.Error.UnknownMethod": "unknown_method",
    "org.freedesktop.DBus.Error.UnknownInterface": "unknown_interface",
    "org.freedesktop.DBus.Error.InvalidArgs": "invalid_args",
    "org.freedesktop.DBus.Error.NotSupported": "not_supported",
    "org.freedesktop.DBus.Error.Failed": "failed",
    "org.freedesktop.Secret.Error.IsLocked": "locked",
    "org.freedesktop.Secret.Error.NoSuchObject": "no_such_object",
    "org.freedesktop.Secret.Error.NoSession": "no_session",
}


def _dbus_code(exc: Exception) -> str | None:
    try:
        kind = (type(exc).__module__, type(exc).__name__)
        if kind == ("jeepney.wrappers", "DBusErrorResponse"):
            name = exc.name
        elif kind == ("dbus.exceptions", "DBusException"):
            name = exc.get_dbus_name()
        else:
            return None
        return _DBUS_CODES.get(name) if type(name) is str else None
    except Exception:
        return None


_STAGES = frozenset(("backend-selection", "collection", "open-session", "write"))
_STAGE_FRAMES = {
    ("keyring/backends/SecretService.py", "get_preferred_collection"): "collection",
    ("secretstorage/collection.py", "__init__"): "collection",
    ("secretstorage/collection.py", "get_default_collection"): "collection",
    ("secretstorage/collection.py", "unlock"): "collection",
    ("secretstorage/collection.py", "create_item"): "write",
    ("secretstorage/util.py", "open_session"): "open-session",
}


def _exception_stage(exc: Exception) -> str | None:
    # Inspect code identifiers only, never frame locals, arguments or traceback text.
    stage = None
    tb = exc.__traceback__
    for _ in range(64):
        if tb is None:
            break
        code = tb.tb_frame.f_code
        for (suffix, function), label in _STAGE_FRAMES.items():
            if code.co_filename.replace("\\", "/").endswith("/" + suffix) and code.co_name == function:
                stage = label
        tb = tb.tb_next
    return stage


class CredentialStoreError(OSError):
    """Sanitized category; the original exception stays in the private chain."""

    def __init__(self, category: str, dbus_code: str | None = None, stage: str | None = None):
        self.stage = stage if stage in _STAGES else None
        self.dbus_code = dbus_code if dbus_code in _DBUS_CODES.values() else None
        self.dbus_error_name = next((name for name, code in _DBUS_CODES.items() if code == self.dbus_code), None)
        self.category = category if category in _STORE_ERRORS else "unknown"
        super().__init__(_STORE_ERRORS[self.category])


def _store_error(exc: Exception, fallback: str = "unknown", stage: str | None = None) -> CredentialStoreError:
    # Prefer the deepest explicit cause, e.g. InitError caused by D-Bus failure.
    # A bounded, cycle-safe walk never inspects exception messages or arguments.
    category = fallback
    dbus_code = None
    seen = set()
    for _ in range(16):
        if id(exc) in seen:
            break
        seen.add(id(exc))
        if isinstance(exc, CredentialStoreError):
            category = exc.category
            dbus_code = exc.dbus_code or dbus_code
            stage = exc.stage or stage
        elif isinstance(exc, PermissionError):
            category = "access_denied"
        else:
            category = _KNOWN_ERRORS.get((type(exc).__module__, type(exc).__name__), category)
        dbus_code = _dbus_code(exc) or dbus_code
        stage = _exception_stage(exc) or stage
        if exc.__cause__ is None:
            break
        exc = exc.__cause__
    return CredentialStoreError(category, dbus_code, stage)


def credential_save_error_message(exc: OSError) -> str:
    error = _store_error(exc)
    code = error.category
    if code == "dbus" and error.dbus_code:
        code += ":" + error.dbus_code
    if error.stage:
        code += ";" + error.stage
    return f"密钥未保存：{_STORE_ERRORS[error.category]}（{code}）。"


def _saved_url() -> str:
    try:
        return str(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")).get("assistant_api_url", ""))
    except (OSError, ValueError, TypeError):
        return ""


def _endpoint(api_url: str | None) -> str:
    parts = urlsplit((_saved_url() if api_url is None else api_url).strip().rstrip("/"))
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("请先填写有效的服务商地址")
    path = parts.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[:-len("/chat/completions")]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path or "/v1", "", ""))


def _saved_endpoint() -> str | None:
    try:
        return _endpoint(_saved_url())
    except ValueError:
        return None


def _account(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def _backend():
    try:
        import keyring
        backend = keyring.get_keyring()
        candidates = getattr(backend, "backends", [backend])
        for item in candidates:
            if item.__class__.__module__.startswith(_SECURE_BACKENDS) and item.priority > 0:
                return item
    except Exception as exc:
        raise _store_error(exc, "unavailable", "backend-selection") from exc
    raise CredentialStoreError("unavailable", stage="backend-selection")


def save_api_key(secret: str, api_url: str | None = None) -> None:
    secret = secret.strip()
    if not secret or len(secret.encode("utf-8")) > 4096:
        raise ValueError("API 密钥长度须为 1–4096 字节")
    endpoint = _endpoint(api_url)
    try:
        _backend().set_password(SERVICE, _account(endpoint), secret)
    except Exception as exc:
        raise _store_error(exc, stage="write") from exc


def read_api_key(api_url: str | None = None, *, strict: bool = False) -> str | None:
    """Read a key; strict callers distinguish an unavailable store from no key."""
    try:
        endpoint = _endpoint(api_url)
    except ValueError:
        return None
    try:
        backend = _backend()
    except OSError:
        if strict:
            raise
        return None
    try:
        value = backend.get_password(SERVICE, _account(endpoint))
        if value:
            return value
        # Migrate only to the saved current endpoint, never a newly typed URL.
        if _saved_endpoint() != endpoint:
            return None
        value = backend.get_password(LEGACY_SERVICE, LEGACY_USER)
        if value:
            # The previous implementation stored UTF-8 bytes in WinCred. keyring
            # first attempts UTF-16; reverse that interpretation for ASCII keys.
            if not value.isascii():
                try:
                    recovered = value.encode("utf-16-le").decode("utf-8")
                    if recovered.isascii():
                        value = recovered
                except (UnicodeError, ValueError):
                    return None
            backend.set_password(SERVICE, _account(endpoint), value)
            backend.delete_password(LEGACY_SERVICE, LEGACY_USER)
        return value or None
    except Exception as exc:
        raise OSError("系统凭据存储读取失败") from exc


def delete_api_key(api_url: str | None = None) -> None:
    endpoint = _endpoint(api_url)
    try:
        backend = _backend()
        if backend.get_password(SERVICE, _account(endpoint)) is not None:
            backend.delete_password(SERVICE, _account(endpoint))
        if _saved_endpoint() == endpoint and backend.get_password(LEGACY_SERVICE, LEGACY_USER) is not None:
            backend.delete_password(LEGACY_SERVICE, LEGACY_USER)
    except OSError:
        raise
    except Exception as exc:
        raise OSError("系统凭据存储删除失败") from exc


def _compute_endpoint(url: str) -> str:
    """Validate and canonicalize an explicit compute-server URL for keyring binding."""
    if not isinstance(url, str) or not url.strip() or len(url) > 2048:
        raise ValueError("请先填写有效的计算服务器地址")
    value = url.strip().rstrip("/")
    parts = urlsplit(value)
    if (parts.scheme not in ("https", "http") or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment):
        raise ValueError("计算服务器地址须为不含账户、查询参数的 HTTP(S) 地址")
    try:
        parts.port
    except ValueError as exc:
        raise ValueError("计算服务器端口无效") from exc
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", "", ""))


def save_compute_token(secret: str, url: str) -> None:
    """Save the explicit compute-server token only in the OS credential store."""
    if not isinstance(secret, str):
        raise ValueError("计算服务器令牌格式无效")
    secret = secret.strip()
    if (not secret or len(secret.encode("utf-8")) > 4096 or not secret.isascii()
            or any(char.isspace() or ord(char) < 33 or ord(char) > 126 for char in secret)):
        raise ValueError("计算服务器令牌须为 1–4096 字节的可打印 ASCII 字符")
    endpoint = _compute_endpoint(url)
    try:
        _backend().set_password(COMPUTE_SERVICE, _account(endpoint), secret)
    except Exception as exc:
        raise _store_error(exc, stage="write") from exc


def read_compute_token(url: str, *, strict: bool = False) -> str | None:
    """Read a token bound to a compute endpoint; never falls back to a file."""
    try:
        endpoint = _compute_endpoint(url)
    except ValueError:
        return None
    try:
        backend = _backend()
    except OSError:
        if strict:
            raise
        return None
    try:
        return backend.get_password(COMPUTE_SERVICE, _account(endpoint)) or None
    except Exception as exc:
        raise OSError("系统凭据存储读取失败") from exc


def delete_compute_token(url: str) -> None:
    """Delete only the compute token associated with this explicit endpoint."""
    endpoint = _compute_endpoint(url)
    try:
        backend = _backend()
        account = _account(endpoint)
        if backend.get_password(COMPUTE_SERVICE, account) is not None:
            backend.delete_password(COMPUTE_SERVICE, account)
    except OSError:
        raise
    except Exception as exc:
        raise OSError("系统凭据存储删除失败") from exc


def _mcp_account(server_id: str) -> str:
    if (not isinstance(server_id, str) or not 1 <= len(server_id) <= 64
            or any(not (char.isascii() and (char.isalnum() or char in "_-")) for char in server_id)):
        raise ValueError("MCP 服务标识无效")
    return _account("external-mcp:" + server_id)


def save_mcp_secret(server_id: str, secret: str) -> None:
    """Keep a service-specific environment/header secret in the same safe backend."""
    if not isinstance(secret, str) or not secret or len(secret.encode("utf-8")) > 4096:
        raise ValueError("MCP 凭据长度须为 1–4096 字节")
    if "\x00" in secret or "\r" in secret or "\n" in secret:
        raise ValueError("MCP 凭据不能包含换行或空字符")
    try:
        _backend().set_password(MCP_SERVICE, _mcp_account(server_id), secret)
    except (OSError, ValueError):
        raise
    except Exception as exc:
        raise OSError("MCP 系统凭据保存失败") from exc


def read_mcp_secret(server_id: str) -> str | None:
    try:
        return _backend().get_password(MCP_SERVICE, _mcp_account(server_id)) or None
    except (OSError, ValueError):
        raise
    except Exception as exc:
        raise OSError("MCP 系统凭据读取失败") from exc


def delete_mcp_secret(server_id: str) -> None:
    try:
        backend, account = _backend(), _mcp_account(server_id)
        if backend.get_password(MCP_SERVICE, account) is not None:
            backend.delete_password(MCP_SERVICE, account)
    except (OSError, ValueError):
        raise
    except Exception as exc:
        raise OSError("MCP 系统凭据删除失败") from exc
