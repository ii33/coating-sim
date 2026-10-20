"""Shared desktop UI preferences and the Qt Quick settings page bridge.

This module intentionally does not import any simulation or designer modules.
The launcher reads the preferences before it creates QGuiApplication; every
module launched in a separate process therefore uses the same settings file.
"""

from __future__ import annotations

import ipaddress
import hashlib
import json
import math
import re
import time
import weakref
import os
import sys
import secrets
import uuid
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from pathlib import Path
from typing import Literal, Mapping, cast
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, Property, QTimer, QUrl, Signal, Slot, QLockFile, QProcess, QProcessEnvironment, qVersion
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply

from coat_sim.core.atomic_io import replace_with_retry

from coat_sim.agent.toolkit_config import (
    CAPABILITIES, CAPABILITY_LABELS, normalized_capabilities,
    normalized_toolkit_settings, validate_workspace_root, is_private_workspace_root,
    default_workspace_root,
)
from coat_sim.agent.development import development_available, development_enabled
from coat_sim.agent.tone_policy import ASSISTANT_TONES, DEFAULT_ASSISTANT_TONE, normalized_tone
from coat_sim.ui.token_format import format_tokens
from coat_sim.ui.local_files import local_file_path
from coat_sim.agent.modelclient import (
    DEFAULT_COMPACTION_THRESHOLD_PERCENT, REASONING_STRENGTHS, provider_for_endpoint,
    reasoning_capabilities, model_token_limits, normalized_model_limits,
    context_budget_for_model, migrate_model_context_overrides,
)
from coat_sim.ui.model_probe import (
    MODEL_REQUEST_LIMIT, PROBE_OUTPUT_TOKENS, normalize_api_url,
)
from coat_sim.ui.provider_catalog import (
    model_configuration_for, normalize_catalog_endpoint, model_metadata_capacity,
    model_suggestions as provider_model_suggestions,
    profile_for_url as provider_profile_for_url,
    provider_presets,
)

from coat_sim.ui.assistant_credentials import (
    delete_api_key, read_api_key, save_api_key, save_mcp_secret, delete_mcp_secret,
    credential_save_error_message, delete_compute_token, read_compute_token,
    save_compute_token,
)
from coat_sim.agent.external_mcp import (
    MAX_SERVERS, SAFE_ENVIRONMENT, normalized_servers, validate_server, run_probe, exported_stdio_config,
)
from coat_sim.agent.mcp_presets import SEED_VERSION, seed_servers, service_description
from coat_sim.ui.mcp_endpoint_process import McpEndpointProcess
from coat_sim.ui.settings_coordinator import SettingsProbeCoordinator, SettingsProbeDependencies


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SETTINGS_PATH = PROJECT_ROOT / "config" / "ui_settings.json"
DEFAULT_FONT_POINT_SIZE = 9
DEFAULT_DPI_SCALE_PERCENT = 100
DEFAULT_DENSITY_SCALE_PERCENT = 100
MIN_FONT_POINT_SIZE = 8
MAX_FONT_POINT_SIZE = 32
MIN_DPI_SCALE_PERCENT = 75
MAX_DPI_SCALE_PERCENT = 200
MIN_DENSITY_SCALE_PERCENT = 75
MAX_DENSITY_SCALE_PERCENT = 150
def _provider_info_text(profile: dict[str, object] | None) -> str:
    if profile is None:
        return '\u81ea\u5b9a\u4e49\u670d\u52a1\u5730\u5740\uff0c\u6a21\u578b\u517c\u5bb9\u80fd\u529b\u548c\u8d26\u53f7\u6743\u9650\u4ee5\u670d\u52a1\u7aef\u4e3a\u51c6\u3002'
    protocol = {
        'openai_compatible_chat_completions': 'OpenAI \u517c\u5bb9 Chat Completions',
        'openai_chat_completions': 'OpenAI Chat Completions',
        'anthropic_messages': 'Anthropic Messages (\u672c\u5e94\u7528\u672a\u5b9e\u73b0\u8be5\u534f\u8bae)',
    }.get(str(profile.get('protocol')), str(profile.get('protocol', '\u672a\u77e5\u534f\u8bae')))
    discovery = profile.get('models_discovery', {})
    status = str(discovery.get('status', 'unconfirmed'))
    status_text = {
        'confirmed': '\u53ef\u8bfb\u53d6\u6a21\u578b\u76ee\u5f55\uff0c\u5b9e\u9645\u53ef\u7528\u8303\u56f4\u4ee5\u8d26\u53f7\u8fd4\u56de\u4e3a\u51c6\u3002',
        'unconfirmed': '\u76ee\u5f55\u63a5\u53e3\u5c1a\u672a\u786e\u8ba4\uff0c\u4f7f\u7528\u670d\u52a1\u7aef\u7ed3\u679c\u4e3a\u51c6\u3002',
        'unsupported': '\u6b64\u9879\u4e0d\u652f\u6301\u672c\u5e94\u7528\u7684\u6a21\u578b\u5217\u8868\u63a2\u6d4b\u3002',
        'alternate_api': '\u6b64\u5957\u9910\u7684\u76ee\u5f55\u63a5\u53e3\u672a\u63a5\u5165\uff0c\u4ee5\u670d\u52a1\u7aef\u53ef\u7528\u6a21\u578b\u4e3a\u51c6\u3002',
    }.get(status, '\u6a21\u578b\u76ee\u5f55\u72b6\u6001\u672a\u77e5\u3002')
    hint = str(profile.get('display_hint') or profile.get('usage_restriction') or '').strip()
    return f'{protocol}\u3002{hint} {status_text}'.strip()


THEME_MODES = ("system", "light", "dark", "high-contrast-light", "high-contrast-dark")
DEFAULT_ASSISTANT_MODEL = ""
DEFAULT_ASSISTANT_API_URL = ""
DEFAULT_ASSISTANT_ICON_SIZE = 96
DEFAULT_ASSISTANT_NOTIFICATION_VOLUME_PERCENT = 42
MAX_MODEL_REPLY_BYTES = 2_000_000
ASSISTANT_TASK_MODES = ("chat", "operate", "research")
ASSISTANT_REASONING_STRENGTHS = REASONING_STRENGTHS
MODEL_CACHE_PATH = PROJECT_ROOT / "config" / "assistant_model_cache.json"
MODEL_CACHE_TTL_SECONDS = 7 * 24 * 3600
MODEL_CACHE_RETRY_SECONDS = 30 * 60
AUTO_PROBE_DEBOUNCE_MS = 650
MAX_MODEL_CACHE_BYTES = 8_000_000
MODEL_CACHE_VERSION = 3
MODEL_CACHE_SNAPSHOT_ATTEMPTS = 3
_CREDENTIAL_READ_LOCK = Lock()
ModelCacheSignature = tuple[int, int, int, int, int] | None



def _local_anonymous_endpoint(api_url: str) -> bool:
    """Only explicit loopback addresses may be refreshed without a credential."""
    host = urlsplit(api_url).hostname
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


class ModelDiscoveryBus(QObject):
    """Shared, credential-bound model capabilities with a seven-day disk cache."""

    modelsChanged = Signal(str, object)
    reasoningChanged = Signal(str, str, object)
    probeFinished = Signal(str)
    snapshotChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries = {}
        self._snapshot_fingerprints: dict[str, str | None] = {}
        self._transient_models = {}
        self._transient_reasoning = {}
        self._active = {}
        self._dirty = set()
        self._disk_signature = None
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save_cache)

    @staticmethod
    def api_key(api_url: str) -> str:
        try:
            return normalize_api_url(api_url)
        except ValueError:
            return api_url.strip().rstrip("/")

    @classmethod
    def cache_key(cls, api_url: str) -> str:
        endpoint = cls.api_key(api_url)
        return f"{provider_for_endpoint(endpoint)}::{endpoint}"

    @classmethod
    def credential_fingerprint(cls, api_url: str, secret: str | None = None) -> str | None:
        key = cls.api_key(api_url)
        try:
            value = read_api_key(key, strict=True) if secret is None else secret
        except (OSError, ValueError):
            if not _local_anonymous_endpoint(key):
                return None
            value = ""
        # High-entropy service keys never enter JSON; each address has its own digest.
        return hashlib.sha256(("coat-model-cache-v1\0" + key + "\0" + (value or "")).encode()).hexdigest()

    @staticmethod
    def _read_disk() -> dict:
        try:
            if MODEL_CACHE_PATH.stat().st_size > MAX_MODEL_CACHE_BYTES:
                return {}
            data = json.loads(MODEL_CACHE_PATH.read_text(encoding="utf-8"))
            version = data.get("version") if isinstance(data, dict) else None
            if version not in (2, MODEL_CACHE_VERSION) or not isinstance(data.get("endpoints"), dict):
                return {}
            def timestamp(value):
                return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else 0.0
            clean = {}
            for stored_key, entry in list(data["endpoints"].items())[:30]:
                if not isinstance(stored_key, str) or not isinstance(entry, dict):
                    continue
                endpoint = stored_key if version == 2 else entry.get("api_url")
                if not isinstance(endpoint, str):
                    continue
                try:
                    endpoint = normalize_api_url(validate_assistant_url(endpoint))
                    if version == MODEL_CACHE_VERSION:
                        provider = entry.get("provider")
                        if provider != provider_for_endpoint(endpoint):
                            continue
                    else:
                        provider = provider_for_endpoint(endpoint)
                    key = f"{provider}::{endpoint}"
                    if version == MODEL_CACHE_VERSION and stored_key != key:
                        continue
                except ValueError:
                    continue
                fingerprint = entry.get("credential_fingerprint", "")
                if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
                    continue
                rows = []
                for row in entry.get("models", []) if isinstance(entry.get("models"), list) else []:
                    if isinstance(row, dict) and isinstance(row.get("id"), str) and 0 < len(row["id"]) <= 120:
                        rows.append({"id": row["id"], "metadata": row.get("metadata", {}) if isinstance(row.get("metadata"), dict) else {},
                                     **({"output_parameter": row["output_parameter"]} if row.get("output_parameter") in ("max_tokens", "max_completion_tokens") else {}),
                                     **({"evidence": row["evidence"]} if row.get("evidence") in ("completion_request", "models_endpoint") else {})})
                    if len(rows) >= 500:
                        break
                reasoning = {}
                for model, record in entry.get("reasoning", {}).items() if isinstance(entry.get("reasoning"), dict) else []:
                    if isinstance(model, str) and len(model) <= 120 and isinstance(record, dict) and isinstance(record.get("result"), dict):
                        reasoning[model] = {"result": record["result"], "updated_at": timestamp(record.get("updated_at")),
                                            "retry_after": timestamp(record.get("retry_after"))}
                clean[key] = {"provider": provider, "api_url": endpoint,
                              "credential_fingerprint": fingerprint, "models": rows, "reasoning": reasoning,
                              "models_updated_at": timestamp(entry.get("models_updated_at")),
                              "models_retry_after": timestamp(entry.get("models_retry_after"))}
            return clean
        except (OSError, ValueError, TypeError, OverflowError, UnicodeError):
            return {}

    def _reload_cache(self) -> None:
        signature = self.disk_signature()
        if signature == self._disk_signature:
            return
        disk = self._read_disk()
        for key, entry in disk.items():
            if key not in self._dirty and isinstance(entry, dict):
                self._entries[key] = entry
        for key in list(self._entries):
            if key not in disk and key not in self._dirty:
                self._entries.pop(key, None)
        self._disk_signature = signature

    @staticmethod
    def disk_signature() -> ModelCacheSignature:
        try:
            stat = MODEL_CACHE_PATH.stat()
            return (stat.st_dev, stat.st_ino, stat.st_mtime_ns,
                    stat.st_ctime_ns, stat.st_size)
        except OSError:
            return None

    def install_cache_snapshot(self, entries: dict, signature: ModelCacheSignature) -> None:
        """Install a validated disk snapshot produced off the GUI thread."""
        for key, entry in entries.items():
            if key not in self._dirty and isinstance(entry, dict):
                self._entries[key] = entry
        for key in list(self._entries):
            if key not in entries and key not in self._dirty:
                self._entries.pop(key, None)
        self._disk_signature = signature

    def _entry_for_fingerprint(self, api_url: str, fingerprint: str | None, *,
                               reload_cache: bool = True) -> tuple[str, dict] | None:
        if not fingerprint:
            return None
        endpoint = self.api_key(api_url)
        key = self.cache_key(endpoint)
        if reload_cache:
            self._reload_cache()
        entry = self._entries.get(key)
        if not isinstance(entry, dict) or entry.get("credential_fingerprint") != fingerprint:
            return None
        return key, entry

    def _entry(self, api_url: str, expected_fingerprint: str | None = None, *,
               reload_cache: bool = True) -> tuple[str, dict] | None:
        endpoint = self.api_key(api_url)
        key = self.cache_key(endpoint)
        provider = provider_for_endpoint(endpoint)
        # A bridge may pass a fingerprint from its off-thread keyring snapshot.
        # Trust it here so publishing an automatic GET result never re-reads the
        # credential store on the GUI thread.
        fingerprint = (expected_fingerprint if expected_fingerprint is not None
                       else self.credential_fingerprint(endpoint))
        previous_fingerprint = self._snapshot_fingerprints.get(key)
        self._snapshot_fingerprints[key] = fingerprint
        if fingerprint is None or (expected_fingerprint is not None and fingerprint != expected_fingerprint):
            if fingerprint != previous_fingerprint:
                self.snapshotChanged.emit(endpoint)
            return None
        if reload_cache:
            self._reload_cache()
        entry = self._entries.get(key)
        if not isinstance(entry, dict) or entry.get("credential_fingerprint") != fingerprint:
            entry = {"provider": provider, "api_url": endpoint,
                     "credential_fingerprint": fingerprint, "models": [], "reasoning": {}}
            self._entries[key] = entry
            self._transient_models.pop(key, None)
            for pair in list(self._transient_reasoning):
                if pair[0] == key:
                    self._transient_reasoning.pop(pair, None)
        if fingerprint != previous_fingerprint:
            self.snapshotChanged.emit(endpoint)
        return key, entry

    def _persist(self, key: str) -> None:
        self._dirty.add(key)
        self._save_cache()

    def _save_cache(self) -> None:
        if not self._dirty:
            return
        lock = QLockFile(str(MODEL_CACHE_PATH.with_suffix(".lock")))
        lock.setStaleLockTime(30000)
        try:
            MODEL_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            if not lock.tryLock(0):
                self._save_timer.start(250)
                return
            merged = self._read_disk()
            for key in self._dirty:
                if key in self._entries:
                    merged[key] = deepcopy(self._entries[key])
                else:
                    merged.pop(key, None)
            # Bound history size; model metadata may be large even for a few endpoints.
            while len(merged) > 30:
                oldest = min(merged, key=lambda key: float(merged[key].get("models_updated_at", 0)))
                merged.pop(oldest)
            payload = json.dumps({"version": MODEL_CACHE_VERSION, "endpoints": merged}, ensure_ascii=False, indent=2) + "\n"
            while len(payload.encode("utf-8")) > MAX_MODEL_CACHE_BYTES and merged:
                oldest = min(merged, key=lambda key: float(merged[key].get("models_updated_at", 0)))
                merged.pop(oldest)
                payload = json.dumps({"version": MODEL_CACHE_VERSION, "endpoints": merged}, ensure_ascii=False, indent=2) + "\n"
            temporary = MODEL_CACHE_PATH.with_name("." + MODEL_CACHE_PATH.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                temporary.write_text(payload, encoding="utf-8")
                replace_with_retry(temporary, MODEL_CACHE_PATH)
            finally:
                temporary.unlink(missing_ok=True)
            self._dirty.clear()
            self._disk_signature = None
        except (OSError, TypeError, ValueError):
            # Cache failure must never break chat or turn a result into a network retry loop.
            self._save_timer.start(1000)
        finally:
            if lock.isLocked():
                lock.unlock()

    def models_for(self, api_url: str) -> list[dict[str, object]]:
        found = self._entry(api_url)
        if found is None:
            return []
        key, entry = found
        return deepcopy(self._transient_models.get(key, entry.get("models", [])))

    def models_for_fingerprint(self, api_url: str, fingerprint: str | None, *,
                               reload_cache: bool = True) -> list[dict[str, object]]:
        found = self._entry_for_fingerprint(api_url, fingerprint, reload_cache=reload_cache)
        if found is None:
            return []
        key, entry = found
        return deepcopy(self._transient_models.get(key, entry.get("models", [])))

    def models_fresh(self, api_url: str) -> bool:
        found = self._entry(api_url)
        if found is None:
            return False
        _, entry = found
        now = time.time()
        return now < float(entry.get("models_retry_after", 0)) or (
            bool(entry.get("models")) and now - float(entry.get("models_updated_at", 0)) < MODEL_CACHE_TTL_SECONDS)

    def models_fresh_for_fingerprint(self, api_url: str, fingerprint: str | None, *,
                                     reload_cache: bool = True) -> bool:
        found = self._entry_for_fingerprint(api_url, fingerprint, reload_cache=reload_cache)
        if found is None:
            return False
        _, entry = found
        now = time.time()
        return now < float(entry.get("models_retry_after", 0)) or (
            bool(entry.get("models")) and now - float(entry.get("models_updated_at", 0)) < MODEL_CACHE_TTL_SECONDS)

    def model_retry_pending(self, api_url: str) -> bool:
        found = self._entry(api_url)
        return bool(found and time.time() < float(found[1].get("models_retry_after", 0)))

    def publish(self, api_url: str, models: list[dict[str, object]], *, completed: bool = True,
                cancelled: bool = False, expected_fingerprint: str | None = None) -> None:
        found = self._entry(api_url, expected_fingerprint)
        if found is None:
            return
        key, entry = found
        rows = [deepcopy(row) for row in models if isinstance(row, dict) and isinstance(row.get("id"), str)][:500]
        if completed:
            self._transient_models.pop(key, None)
            if cancelled:
                # A failed refresh is not evidence that earlier models vanished.
                retained = {row["id"]: row for row in entry.get("models", [])}
                for row in rows:
                    retained.setdefault(row["id"], row)
                rows = list(retained.values())[:500]
            entry["models"] = rows
            if not cancelled:
                entry["models_updated_at"] = time.time() if rows else 0
            entry["models_retry_after"] = time.time() + MODEL_CACHE_RETRY_SECONDS if cancelled or not rows else 0
            self._persist(key)
        else:
            self._transient_models[key] = rows
        self.modelsChanged.emit(self.api_key(api_url), deepcopy(rows))

    def invalidate(self, api_url: str) -> None:
        endpoint = self.api_key(api_url)
        key = self.cache_key(endpoint)
        active = self._active.get(key)
        owner = active[0]() if active else None
        if isinstance(owner, SettingsProbeCoordinator):
            # Cancel another window's captured old-key run before removing its
            # cache; a late reply must not republish the invalidated identity.
            owner.cancel_model_probe()
        self._snapshot_fingerprints.pop(key, None)
        self._entries.pop(key, None)
        self._transient_models.pop(key, None)
        for pair in list(self._transient_reasoning):
            if pair[0] == key:
                self._transient_reasoning.pop(pair, None)
        self._persist(key)
        self.modelsChanged.emit(endpoint, [])
        self.reasoningChanged.emit(endpoint, "", None)

    def reasoning_for(self, api_url: str, model: str) -> dict | None:
        found = self._entry(api_url)
        if found is None:
            return None
        key, entry = found
        pair = (key, model.strip())
        record = entry.get("reasoning", {}).get(pair[1], {})
        return self._with_capacity_metadata(api_url, model, entry,
                                            self._transient_reasoning.get(pair, record.get("result")))

    def reasoning_snapshot_for(self, api_url: str, model: str) -> dict | None:
        """Read the active in-memory entry without key-store or cache-file I/O.

        Entry adoption checks credential identity; key save/delete invalidates it.
        Presentation may use this snapshot, while request submission still calls
        reasoning_for to check the current credential before building its packet.
        """
        return self.reasoning_for_fingerprint(
            api_url, model, self._snapshot_fingerprints.get(self.cache_key(api_url)),
            reload_cache=False)

    def reasoning_for_fingerprint(self, api_url: str, model: str,
                                  fingerprint: str | None, *, reload_cache: bool = True) -> dict | None:
        found = self._entry_for_fingerprint(api_url, fingerprint, reload_cache=reload_cache)
        if found is None:
            return None
        key, entry = found
        pair = (key, model.strip())
        record = entry.get("reasoning", {}).get(pair[1], {})
        return self._with_capacity_metadata(api_url, model, entry,
                                            self._transient_reasoning.get(pair, record.get("result")))

    @staticmethod
    def _with_capacity_metadata(api_url: str, model: str, entry: dict, probe: object) -> dict | None:
        """Expose caps only through the same credential-checked discovery entry.

        The original model-list timestamp is retained, so reads never renew TTL.
        The existing worker configuration already carries this probe packet.
        """
        result = deepcopy(probe) if isinstance(probe, dict) else None
        observed_at = entry.get("models_updated_at", 0)
        if not isinstance(observed_at, (int, float)) or isinstance(observed_at, bool):
            return result
        for row in entry.get("models", []):
            if not isinstance(row, dict) or row.get("id") != model or row.get("evidence") != "models_endpoint":
                continue
            metadata = model_metadata_capacity(api_url, model, row.get("metadata"), observed_at=observed_at)
            if metadata:
                return {**(result or {"api_url": api_url, "model": model, "evidence": "server_capacity_only"}),
                        "capacity_metadata": metadata}
        return result

    def reasoning_fresh(self, api_url: str, model: str) -> bool:
        found = self._entry(api_url)
        if found is None:
            return False
        _, entry = found
        record = entry.get("reasoning", {}).get(model.strip(), {})
        now = time.time()
        if now < float(record.get("retry_after", 0)):
            return True
        result = record.get("result", {})
        if result.get("incomplete"):
            return False
        if result.get("native_schema") != 1 or result.get("reasoning_probed") is not True:
            return False
        if result.get("verified") is True:
            capabilities = reasoning_capabilities(api_url, model, result)
            if (not capabilities.get("probe_verified")
                    or capabilities.get("supported_strengths") != result.get("options")):
                return False
        elif result.get("evidence") != "unconfirmed" or result.get("options") != ["auto"]:
            return False
        return now - float(record.get("updated_at", 0)) < MODEL_CACHE_TTL_SECONDS

    def publish_reasoning(self, api_url: str, model: str, result: dict | None, *, completed: bool = True,
                          cancelled: bool = False, expected_fingerprint: str | None = None) -> None:
        found = self._entry(api_url, expected_fingerprint)
        if found is None:
            return
        key, entry = found
        model = model.strip()
        pair = (key, model)
        if completed:
            self._transient_reasoning.pop(pair, None)
            if isinstance(result, dict):
                entry.setdefault("reasoning", {})[model] = {
                    "result": deepcopy(result), "updated_at": time.time() if not cancelled else 0,
                    "retry_after": time.time() + MODEL_CACHE_RETRY_SECONDS if cancelled else 0}
            else:
                entry.setdefault("reasoning", {}).pop(model, None)
            self._persist(key)
        elif isinstance(result, dict):
            self._transient_reasoning[pair] = deepcopy(result)
        self.reasoningChanged.emit(self.api_key(api_url), model, deepcopy(result))

    def claim_probe(self, api_url: str, owner: QObject) -> bool:
        endpoint = self.api_key(api_url)
        key = self.cache_key(endpoint)
        active = self._active.get(key)
        if active and active[0]() is not None:
            return False
        if active:
            active[1].unlock()
            self._active.pop(key, None)
        try:
            MODEL_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            filename = ".model-probe-" + hashlib.sha256(key.encode()).hexdigest()[:24] + ".lock"
            lock = QLockFile(str(MODEL_CACHE_PATH.parent / filename))
            lock.setStaleLockTime(0)  # only a dead process may release a live long probe
            if not lock.tryLock(0):
                return False
        except OSError:
            return False
        self._active[key] = (weakref.ref(owner), lock)
        return True

    def release_probe(self, api_url: str, owner: QObject) -> None:
        endpoint = self.api_key(api_url)
        key = self.cache_key(endpoint)
        active = self._active.get(key)
        if active and (active[0]() is owner or active[0]() is None):
            self._active.pop(key, None)
            active[1].unlock()
            self.probeFinished.emit(endpoint)


_model_discovery_bus: ModelDiscoveryBus | None = None


def model_discovery_bus() -> ModelDiscoveryBus:
    global _model_discovery_bus
    if _model_discovery_bus is None:
        _model_discovery_bus = ModelDiscoveryBus(QGuiApplication.instance())
    return _model_discovery_bus


def _read_stable_model_cache_snapshot() -> tuple[dict, ModelCacheSignature]:
    """Read cache contents only when the file identity is stable across the read."""
    for _ in range(MODEL_CACHE_SNAPSHOT_ATTEMPTS):
        before = ModelDiscoveryBus.disk_signature()
        entries = ModelDiscoveryBus._read_disk()
        after = ModelDiscoveryBus.disk_signature()
        if before == after:
            return entries, after
    # A continuously replaced file is not a trustworthy cache snapshot.
    return {}, None


def _read_settings_bootstrap_snapshot(api_url: str) -> tuple[bool, bool, str | None, dict, ModelCacheSignature, str]:
    """Read credentials/cache off-thread; the secret stays inside the Python bridge."""
    secret = None
    credential_error = False
    try:
        # Separate SettingsBridge instances each own an executor. Serialize only
        # their worker-side keyring reads; the GUI thread never waits on this lock.
        with _CREDENTIAL_READ_LOCK:
            secret = read_api_key(api_url, strict=True)
    except (OSError, ValueError):
        credential_error = True
    fingerprint = None
    try:
        if credential_error and _local_anonymous_endpoint(api_url):
            fingerprint = ModelDiscoveryBus.credential_fingerprint(api_url, secret="")
        elif not credential_error:
            fingerprint = ModelDiscoveryBus.credential_fingerprint(api_url, secret=secret or "")
    except (OSError, ValueError):
        fingerprint = None
    entries, signature = _read_stable_model_cache_snapshot()
    return bool(secret), credential_error, fingerprint, entries, signature, secret or ""


def _choice(value: object, choices: tuple[str, ...], default: str) -> str:
    return value if isinstance(value, str) and value in choices else default


def _bounded_integer(value: object, minimum: int, maximum: int, fallback: int) -> int:
    """Ignore invalid or out-of-range persisted settings without failing startup."""
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return number if minimum <= number <= maximum else fallback


def _short_text(value: object, maximum: int) -> str:
    return value.strip()[:maximum] if isinstance(value, str) else ""


def _research_library_root(value: object) -> str:
    """Keep an optional external override intact; empty retains default routing."""
    if not isinstance(value, str):
        return ""
    root = value.strip()
    return root if root and len(root) <= 4096 and chr(0) not in root else ""


def _compute_server_url(value: object) -> str:
    """Keep one explicit, credential-free compute endpoint; empty means local."""
    if not isinstance(value, str):
        return ""
    url = value.strip().rstrip("/")
    if not url:
        return ""
    if len(url) > 2048 or any(char.isspace() or ord(char) < 32 for char in url):
        return ""
    parts = urlsplit(url)
    if (parts.scheme not in ("https", "http") or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment):
        return ""
    try:
        parts.port
    except ValueError:
        return ""
    return url


def _compute_server_ca_file(value: object) -> str:
    """Preserve a bounded explicit CA path without truncating filesystem paths."""
    if not isinstance(value, str):
        return ""
    path = value.strip()
    return path if path and len(path) <= 4096 and chr(0) not in path else ""


def validate_assistant_url(value: str) -> str:
    """Permit HTTPS services and unencrypted loopback development services."""
    url = value.strip().rstrip("/")
    if not url:
        raise ValueError("请填写 API 服务器链接")
    parts = urlsplit(url)
    if (parts.scheme not in ("https", "http") or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment):
        raise ValueError("API 链接须为不含账户、查询参数的 HTTP(S) 地址")
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("非本机 API 服务器须使用 HTTPS")
    return url


def normalized_ui_settings(values: Mapping[str, object] | None = None) -> dict[str, object]:
    """Return the supported, validated settings schema."""
    source = values or {}
    # Migrate from the raw settings object before any legacy value is clamped or
    # dropped. V2 records keep the exact old row under ``legacy_values`` so a
    # bad legacy value remains repairable and reset-to-auto stays idempotent.
    # Keep the exact pre-v2 map so invalid model keys remain recoverable.
    if "assistant_model_limits_legacy" in source:
        legacy_model_limits = deepcopy(source["assistant_model_limits_legacy"])
    elif source.get("assistant_context_budget_version") != 2:
        legacy_model_limits = deepcopy(source.get("assistant_model_limits", {}))
    else:
        legacy_model_limits = {}
    model_limits = migrate_model_context_overrides(source)
    legacy_global_context = source.get(
        "assistant_context_tokens_legacy", source.get("assistant_context_tokens", 0))
    model = _short_text(source.get("assistant_model"), 120)
    history = []
    for value in source.get("assistant_api_urls", []) if isinstance(source.get("assistant_api_urls"), list) else []:
        if not isinstance(value, str):
            continue
        try:
            url = validate_assistant_url(value[:2048])
        except ValueError:
            continue
        if url not in history:
            history.append(url)
    history = history[-30:]
    return {
        "theme_mode": _choice(source.get("theme_mode"), THEME_MODES, "system"),
        "theme_package_id": _theme_package_id(source.get("theme_package_id")),
        "font_family": _short_text(source.get("font_family"), 128),
        "font_point_size": _bounded_integer(
            source.get("font_point_size"),
            MIN_FONT_POINT_SIZE,
            MAX_FONT_POINT_SIZE,
            DEFAULT_FONT_POINT_SIZE,
        ),
        "dpi_scale_percent": _bounded_integer(
            source.get("dpi_scale_percent"),
            MIN_DPI_SCALE_PERCENT,
            MAX_DPI_SCALE_PERCENT,
            DEFAULT_DPI_SCALE_PERCENT,
        ),
        "density_scale_percent": _bounded_integer(
            source.get("density_scale_percent"),
            MIN_DENSITY_SCALE_PERCENT,
            MAX_DENSITY_SCALE_PERCENT,
            DEFAULT_DENSITY_SCALE_PERCENT,
        ),
        **normalized_toolkit_settings(source),
        "assistant_enabled": source.get("assistant_enabled") is True,
        "assistant_notification_sound": source.get("assistant_notification_sound") is not False,
        "assistant_notification_volume_percent": _bounded_integer(
            source.get("assistant_notification_volume_percent"), 0, 100,
            DEFAULT_ASSISTANT_NOTIFICATION_VOLUME_PERCENT),
        "developer_enabled": source.get("developer_enabled") is True,
        "assistant_api_url": _short_text(source.get("assistant_api_url"), 2048),
        "compute_server_url": _compute_server_url(source.get("compute_server_url")),
        "compute_server_tls": (source.get("compute_server_tls")
                               if type(source.get("compute_server_tls")) is bool else True),
        "compute_server_ca_file": _compute_server_ca_file(source.get("compute_server_ca_file")),
        "assistant_model": model,
        "assistant_api_urls": history,
        "assistant_research_library_root": _research_library_root(
            source.get("assistant_research_library_root")),
        "assistant_user_name": _short_text(source.get("assistant_user_name"), 40) or "用户",
        "assistant_tone": normalized_tone(source.get("assistant_tone")),
        "assistant_context_tokens": _bounded_integer(source.get("assistant_context_tokens"), 0, 2000000, 0),
        "assistant_context_tokens_legacy": legacy_global_context,
        "assistant_context_budget_version": 2,
        "assistant_model_limits": model_limits,
        "assistant_model_limits_legacy": legacy_model_limits,
        "assistant_task_mode": _choice(
            source.get("assistant_task_mode"), ASSISTANT_TASK_MODES, "chat"),
        # A null task ceiling means no per-task request/token/tool/delegation cap.
        # Explicit positive ceilings survive load/save and worker handoff.
        "assistant_task_max_requests": _bounded_integer(
            source.get("assistant_task_max_requests"), 0, 256, 0) or None,
        "assistant_task_max_input_tokens": _bounded_integer(
            source.get("assistant_task_max_input_tokens"), 0, 8_000_000, 0) or None,
        "assistant_task_max_total_tokens": _bounded_integer(
            source.get("assistant_task_max_total_tokens"), 0, 10_000_000, 0) or None,
        "assistant_task_max_tool_calls": _bounded_integer(
            source.get("assistant_task_max_tool_calls"), 0, 1024, 0) or None,
        "assistant_task_max_delegations": _bounded_integer(
            source.get("assistant_task_max_delegations"), 0, 256, 0) or None,
        "assistant_reasoning_strength": _choice(
            source.get("assistant_reasoning_strength"), ASSISTANT_REASONING_STRENGTHS, "auto"),
        "assistant_daily_token_limit": _bounded_integer(
            source.get("assistant_daily_token_limit"), 0, 2_000_000_000, 0),
        "assistant_period_token_limit": _bounded_integer(
            source.get("assistant_period_token_limit"), 0, 2_000_000_000, 0),
        "assistant_period_minutes": _bounded_integer(
            source.get("assistant_period_minutes"), 1, 10080, 60),
        "assistant_icon_size": _bounded_integer(
            source.get("assistant_icon_size"), 48, 256, DEFAULT_ASSISTANT_ICON_SIZE),
        "assistant_mcp_servers": normalized_servers(seed_servers(
            normalized_servers(source.get("assistant_mcp_servers", [])),
            source.get("assistant_mcp_seed_version"))),
        "assistant_mcp_seed_version": SEED_VERSION,
    }


def _theme_package_id(value: object) -> str:
    """Keep only package IDs accepted by the data-only theme manifest."""

    if not isinstance(value, str) or not value:
        return ""
    return value if len(value) <= 64 and re.fullmatch(r"[a-z0-9][a-z0-9._-]*", value) else ""


def load_ui_settings() -> dict[str, object]:
    """Read the single shared project configuration; use defaults if absent."""
    try:
        payload = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return normalized_ui_settings()
    except (OSError, UnicodeError, json.JSONDecodeError):
        payload = None
    if not isinstance(payload, dict):
        # An unreadable existing file is not a fresh installation: it may have
        # contained an explicit permission denial. Keep its bytes untouched and
        # never turn on tool/model access as an error-recovery side effect.
        return normalized_ui_settings({'assistant_toolkit_enabled': False,
            'assistant_toolkit_allow_model_context': False,
            'assistant_toolkit_capabilities': [],
            'assistant_toolkit_profile_capabilities': []})
    return normalized_ui_settings(payload)


def save_ui_settings(values: Mapping[str, object]) -> dict[str, object]:
    """Atomically save preferences so another module cannot read partial JSON."""
    result = normalized_ui_settings(values)
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_PATH.with_name(f".{SETTINGS_PATH.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        replace_with_retry(temporary, SETTINGS_PATH)
    finally:
        temporary.unlink(missing_ok=True)
    return result


def _configure_qt_quick_frame_updates() -> None:
    """Keep Windows Qt 6.12 frame requests on the platform timer path.

    Disable only Qt's DXGI VBlank notification thread; retain the selected
    graphics backend, render loop and swap interval. Explicit values win.
    """
    if sys.platform == "win32" and qVersion().startswith("6.12."):
        os.environ.setdefault("QT_D3D_NO_VBLANK_THREAD", "1")

def configure_qt_dpi_scale(settings: Mapping[str, object] | None = None) -> dict[str, object]:
    """Call before constructing QGuiApplication in every standalone entry point.

    Qt applies QT_SCALE_FACTOR while creating its application/screens, so a DPI
    change made in the settings page takes effect after the process restarts.
    """
    _configure_qt_quick_frame_updates()
    resolved = normalized_ui_settings(settings) if settings is not None else load_ui_settings()
    # Fusion consumes the active Qt palette for controls, including fields,
    # buttons and scroll bars. Apply it before any QML Controls are loaded.
    os.environ["QT_QUICK_CONTROLS_STYLE"] = "Fusion"
    os.environ["QT_SCALE_FACTOR"] = f"{resolved['dpi_scale_percent'] / 100:g}"
    return resolved


def apply_font_settings(
    settings: Mapping[str, object] | None = None,
    app: QGuiApplication | None = None,
) -> dict[str, object]:
    """Apply the configured point size to the running Qt application."""
    resolved = normalized_ui_settings(settings) if settings is not None else load_ui_settings()
    application = app or QGuiApplication.instance()
    if application is not None:
        font = QFont(application.font())
        from coat_sim.ui.fonts import bundled_font_family
        # Theme tokens name Noto even before Qt knows the bundled font. Register
        # it first, including when an explicit settings/package font wins below.
        default_family = bundled_font_family()
        family = str(resolved.get("font_family", "")).strip()
        if not family:
            try:
                from coat_sim.ui.theme_service import ThemeService
                family = str(ThemeService.instance().current_snapshot.get("fontFamily", ""))
            except (RuntimeError, TypeError):
                family = ""
            family = family or default_family
        if family:
            font.setFamily(family)
        font.setPointSizeF(float(resolved["font_point_size"]))
        application.setFont(font)
    return resolved


class SettingsBridge(QObject):
    """No-argument bridge used by ``SettingsWindow.qml`` and the module router."""

    changed = Signal()
    themePackagesChanged = Signal()
    themePackageStatusChanged = Signal()
    assistantStateChanged = Signal()
    statusChanged = Signal()
    restartRequiredChanged = Signal()
    assistantSettingsSaved = Signal()
    assistantSettingsReloaded = Signal()
    modelDiscoveryChanged = Signal()
    mcpChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = load_ui_settings()
        # Keep context-profile migration in the bridge draft until the user
        # saves. The pure migration is idempotent and retains legacy backups.
        self._settings["assistant_model_limits"] = migrate_model_context_overrides(self._settings)
        self._settings["assistant_context_budget_version"] = 2
        self._context_profile_touched: set[tuple[str, str]] = set()
        self._theme_package_status = ""
        from coat_sim.ui.theme_service import ThemeService

        self._theme_service = ThemeService.instance()
        self._theme_service.packagesChanged.connect(self._on_theme_packages_changed)
        if not self._theme_service.has_theme(str(self._settings.get("theme_package_id", ""))):
            self._settings["theme_package_id"] = ""
            self._theme_package_status = "已保存的主题包不存在，当前改用内置主题。"
        self._status = "字号保存后立即生效；界面缩放保存后请重新打开程序。"
        self._restart_required = False
        self._has_assistant_key = False
        self._assistant_key_status_ready = False
        self._assistant_key_message = ""
        self._assistant_settings_requested = False
        self._assistant_credential_fingerprint: str | None = None
        self._assistant_credential_api_url: str | None = None
        self._assistant_credential_secret = ""
        self._compute_server_has_token = False
        self._compute_server_token_status = "令牌状态待检查。"
        self._discovery_bus_value: ModelDiscoveryBus | None = None
        self._credential_executor: ThreadPoolExecutor | None = None
        self._credential_future = None
        self._credential_request: tuple[int, str] | None = None
        self._credential_request_reason = "bootstrap"
        self._credential_generation = 0
        self._credential_pending: tuple[int, str] | None = None
        self._credential_pending_reason = "bootstrap"
        self._credential_refresh_debounce = QTimer(self)
        self._credential_refresh_debounce.setSingleShot(True)
        self._credential_refresh_debounce.setInterval(150)
        self._credential_refresh_debounce.timeout.connect(self._start_bound_key_lookup)
        self._credential_poll = QTimer(self)
        self._credential_poll.setInterval(25)
        self._credential_poll.timeout.connect(self._on_bound_key_lookup_ready)
        self.destroyed.connect(lambda *_: self._shutdown_key_lookup())
        self._probe_coordinator = SettingsProbeCoordinator(
            self,
            SettingsProbeDependencies(
                validate_assistant_url=validate_assistant_url,
                local_anonymous_endpoint=_local_anonymous_endpoint,
                read_api_key=read_api_key,
                provider_profile_for_url=provider_profile_for_url,
                network_manager_factory=QNetworkAccessManager,
                auto_probe_debounce_ms=AUTO_PROBE_DEBOUNCE_MS,
                model_cache_retry_seconds=MODEL_CACHE_RETRY_SECONDS,
                max_model_reply_bytes=MAX_MODEL_REPLY_BYTES,
            ),
            parent=self,
        )
        self.destroyed.connect(self._probe_coordinator._cleanup_destroyed_probe)
        application = QGuiApplication.instance()
        if application is not None:
            application.aboutToQuit.connect(self._probe_coordinator._shutdown_probe_budget)
            application.aboutToQuit.connect(self._shutdown_key_lookup)
        self._mcp_message = "新增服务默认停用；外部连接和工具调用需分别开启。"
        self._mcp_statuses = {}
        self._mcp_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mcp-connection-check")
        self._mcp_probe = None
        self._mcp_probe_id = ""
        self._mcp_probe_cancel = Event()
        self._mcp_poll = QTimer(self)
        self._mcp_poll.setInterval(100)
        self._mcp_poll.timeout.connect(self._on_mcp_probe_ready)
        self._mcp_http = McpEndpointProcess(self)
        self._mcp_http.readyReadStandardOutput.connect(self._on_mcp_http_output)
        self._mcp_http.errorOccurred.connect(self._on_mcp_http_error)
        self._mcp_http.finished.connect(self._on_mcp_http_finished)
        self._mcp_http_port = 8766
        self._mcp_http_status = "未启动 HTTP 服务。"
        self._mcp_http_log = ""
        self._mcp_http_stopping = False
        self._mcp_http_credential = ""
        self._mcp_http_token = ""
        self.destroyed.connect(lambda *_: self._clear_own_mcp_credential())
        self._mcp_http_timeout = QTimer(self)
        self._mcp_http_timeout.setSingleShot(True)
        self._mcp_http_timeout.timeout.connect(self._on_mcp_http_timeout)
        self._mcp_http_kill_timer = QTimer(self)
        self._mcp_http_kill_timer.setSingleShot(True)
        self._mcp_http_kill_timer.timeout.connect(self._mcp_http.kill)
        if application is not None:
            application.aboutToQuit.connect(self._shutdown_mcp)

    @Property(int, notify=changed)
    def fontPointSize(self) -> int:
        return self._settings["font_point_size"]

    @Property(str, notify=changed)
    def fontFamily(self) -> str:
        return str(self._settings.get("font_family", ""))

    @Property(str, notify=changed)
    def themeMode(self) -> str:
        return str(self._settings["theme_mode"])

    @Property(str, notify=changed)
    def themePackageId(self) -> str:
        return str(self._settings.get("theme_package_id", ""))

    @Property("QVariantList", notify=themePackagesChanged)
    def themePackages(self) -> list[dict[str, str]]:
        return self._theme_service.available_themes()

    @Property(str, notify=themePackageStatusChanged)
    def themePackageStatus(self) -> str:
        return self._theme_package_status

    @Property(int, notify=changed)
    def densityScalePercent(self) -> int:
        return int(self._settings["density_scale_percent"])

    @Property(int, notify=changed)
    def dpiScalePercent(self) -> int:
        return self._settings["dpi_scale_percent"]

    @Property(bool, notify=assistantStateChanged)
    def developerAvailable(self) -> bool:
        return development_available()

    @Property(bool, notify=assistantStateChanged)
    def developerEnabled(self) -> bool:
        return development_enabled(self._settings)

    @Property(bool, notify=assistantStateChanged)
    def developerLaunchEnabled(self) -> bool:
        return self.developerAvailable and os.environ.get("COAT_DEVELOPMENT") == "1"

    @Slot(bool)
    def setDeveloperEnabled(self, enabled: bool) -> None:
        if self.developerLaunchEnabled and not enabled:
            self._set_status("本次通过启动参数开启开发模式；关闭后请不带开发参数重新启动。")
            self.assistantStateChanged.emit()
            return
        if enabled and not self.developerAvailable:
            self._set_status("当前运行环境未开放开发能力。")
            return
        self._settings["developer_enabled"] = bool(enabled)
        self.assistantStateChanged.emit()

    @Property(bool, notify=assistantStateChanged)
    def assistantEnabled(self) -> bool:
        return bool(self._settings["assistant_enabled"])

    @Property(int, notify=assistantStateChanged)
    def assistantNotificationVolumePercent(self) -> int:
        return int(self._settings["assistant_notification_volume_percent"])

    @Property(str, notify=assistantStateChanged)
    def assistantApiUrl(self) -> str:
        return str(self._settings["assistant_api_url"])

    @Property(str, notify=assistantStateChanged)
    def assistantResearchLibraryRoot(self) -> str:
        return str(self._settings.get("assistant_research_library_root", ""))

    @Slot(str)
    def setAssistantResearchLibraryRoot(self, root: str) -> None:
        # Keep invalid input visible until Save can explain the error.
        self._settings["assistant_research_library_root"] = root.strip()
        self.assistantStateChanged.emit()

    @Property(bool, notify=assistantStateChanged)
    def assistantToolkitEnabled(self) -> bool:
        return self._settings.get("assistant_toolkit_enabled") is True

    @Property(str, notify=assistantStateChanged)
    def assistantToolkitWorkspaceRoot(self) -> str:
        return str(self._settings.get("assistant_toolkit_workspace_root", ""))

    @Property(bool, notify=assistantStateChanged)
    def assistantToolkitAllowModelContext(self) -> bool:
        return self._settings.get("assistant_toolkit_allow_model_context") is True

    # Qt accepts named meta-types here; its shipped stub only declares type.
    @Property(cast(type, "QStringList"), notify=assistantStateChanged)
    def assistantToolkitCapabilities(self) -> list[str]:
        return list(cast(list[str], self._settings.get("assistant_toolkit_capabilities", [])))

    @Property(cast(type, "QStringList"), notify=assistantStateChanged)
    def assistantToolkitProfileCapabilities(self) -> list[str]:
        return list(cast(list[str], self._settings.get("assistant_toolkit_profile_capabilities", [])))

    @Property(cast(type, "QVariantList"), notify=assistantStateChanged)
    def assistantToolkitCapabilityOptions(self) -> list[dict[str, str]]:
        return [{"id": name, "label": CAPABILITY_LABELS[name]} for name in CAPABILITIES]

    @Property(str, notify=assistantStateChanged)
    def assistantToolkitStatus(self) -> str:
        if not self.assistantToolkitEnabled:
            return "工具集已关闭；通用能力调用不可用。"
        try:
            validate_workspace_root(self.assistantToolkitWorkspaceRoot, PROJECT_ROOT,
                allow_missing=is_private_workspace_root(self.assistantToolkitWorkspaceRoot))
        except ValueError as exc:
            return str(exc)
        effective = set(cast(list[str], self.assistantToolkitCapabilities)) & set(cast(list[str], self.assistantToolkitProfileCapabilities))
        if not effective:
            return "全局与配置的能力交集为空；所有工具关闭。"
        if not self.assistantToolkitAllowModelContext:
            return "尚未允许工具结果发送给当前模型；桌面工具未开放。"
        location = "助手独立工作空间" if is_private_workspace_root(self.assistantToolkitWorkspaceRoot) else "所选工作区"
        return location + "已配置：" + "、".join(sorted(effective)) + "。操作/攻关模式可在其中读写、保存和可恢复删除，无需逐次确认；讨论模式只读。首次写入自动创建默认目录。执行/联网/远程/OCR 仍须独立提供器。"

    @Slot()
    def resetAssistantToolkitWorkspace(self):
        self._settings.update(normalized_toolkit_settings({}))
        self.assistantStateChanged.emit()

    @Property(str, constant=True)
    def assistantToolkitDefaultWorkspaceRoot(self) -> str:
        return str(default_workspace_root())

    @Slot(bool, str, bool)
    def setAssistantToolkitConfig(self, enabled, root, allow_context):
        self._settings["assistant_toolkit_enabled"] = enabled is True
        self._settings["assistant_toolkit_workspace_root"] = root.strip()
        self._settings["assistant_toolkit_allow_model_context"] = allow_context is True
        self.assistantStateChanged.emit()

    @Slot(str, bool, bool)
    def setAssistantToolkitCapability(self, capability, enabled, profile):
        if capability not in CAPABILITIES:
            return
        key = "assistant_toolkit_profile_capabilities" if profile else "assistant_toolkit_capabilities"
        values = set(cast(list[str], self._settings.get(key, [])))
        if enabled:
            values.add(capability)
        else:
            values.discard(capability)
        self._settings[key] = normalized_capabilities(list(values))
        self.assistantStateChanged.emit()

    @Property("QStringList", notify=assistantStateChanged)
    def assistantApiUrls(self) -> list[str]:
        urls = list(self._settings.get("assistant_api_urls", []))
        current = self.assistantApiUrl
        # Older settings files may have the active endpoint but no URL history.
        # Keep it selectable so editable ComboBox.currentIndex does not erase it.
        if current and current not in urls:
            urls.append(current)
        return urls

    @Property("QVariantList", constant=True)
    def providerPresets(self) -> list[dict[str, object]]:
        return provider_presets()

    @Slot(str, result=int)
    def providerPresetIndexForUrl(self, api_url: str) -> int:
        profile = provider_profile_for_url(api_url)
        if profile is None:
            return -1
        return next((index for index, row in enumerate(provider_presets())
                     if row["id"] == profile["id"]), -1)

    @Slot(str, result=bool)
    def applyAssistantProviderPreset(self, preset_id: str) -> bool:
        """Apply an offline preset to this window's draft, preserving overrides."""
        preset = next((row for row in provider_presets() if row["id"] == preset_id), None)
        if preset is None or not preset.get("selectable"):
            return False
        same_endpoint = (normalize_catalog_endpoint(cast(str, self.assistantApiUrl))
                         == normalize_catalog_endpoint(str(preset["base_url"])))
        model = cast(str, self.assistantModel) if same_endpoint and self.assistantModel else str(preset["default_model"])
        strength = self._settings.get("assistant_reasoning_strength", "auto")
        self.setAssistantConfig(cast(bool, self.assistantEnabled), str(preset["base_url"]), model)
        if not same_endpoint and strength == "auto":
            self.setAssistantReasoningStrength(str(preset["default_reasoning_strength"]))
        return True

    @Slot(str, str, result=str)
    def providerModelInfo(self, api_url: str, model: str) -> str:
        info = model_configuration_for(api_url, model)
        resolved = context_budget_for_model(self._model_budget_config(api_url, model), api_url, model)["profile"]
        assert isinstance(resolved, dict)
        declared = resolved.get("evidence") == "server_declared_metadata"
        if declared:
            info["context_window_tokens"] = resolved["context_window_tokens"]
            info["max_input_tokens"] = resolved["max_input_tokens"]
            info["max_output_tokens"] = resolved["max_output_tokens"]
            info["notes"] = str(resolved["notes"])
            info["verified_date"] = str(resolved["date"])
            sources = resolved["source"]
            if isinstance(sources, (list, tuple)):
                info["sources"] = list(dict.fromkeys([str(source) for source in sources] + info["sources"]))
        def tokens(field: Literal["context_window_tokens", "max_input_tokens", "max_output_tokens"]) -> str:
            value = info[field]
            return f"{value:,} Token" if value else "未知"
        capability_names = {"tool_use": "工具", "image_input": "图像输入", "audio_input": "音频输入", "video_input": "视频输入"}
        values = {"supported": "支持", "unsupported": "不支持", "unknown": "未知",
                  "none_only": "仅 reasoning_effort=none", "responses_only": "需 Responses（当前未实现）"}
        capabilities = "；".join(f"{label}：{values.get(info['capabilities'][field], info['capabilities'][field])}"
                                  for field, label in capability_names.items())
        return (f"{'服务端声明容量' if declared else '离线文档预置'} · {info['protocol']} · 日期 {info['verified_date'] or '未记录'}\n"
                f"上下文：{tokens('context_window_tokens')}；输入硬限：{tokens('max_input_tokens')}；输出硬限：{tokens('max_output_tokens')}\n"
                f"{capabilities}。账号可用性与能力请求均未验证。"
                + ("\n" + info["notes"] if info["notes"] else "")
                + ("\n来源：" + " · ".join(info["sources"]) if info["sources"] else ""))

    def _apply_discovered_model_metadata(self, api_url: str, models: list[dict]) -> int:
        """Publish valid Models metadata to the existing credential-bound cache.

        No provider capacity is stored among user budget overrides. Deleting or
        replacing a key already invalidates this cache for every settings window.
        """
        coordinator = self._probe_coordinator
        fingerprint = self._assistant_credential_fingerprint
        if coordinator._model_discovery_busy:
            if (coordinator._probe_original_url != self.assistantApiUrl
                    or coordinator._probe_credential_generation != self._credential_generation):
                return 0
            fingerprint = coordinator._probe_fingerprint
        elif self._assistant_credential_api_url != self.assistantApiUrl:
            return 0
        if not fingerprint or normalize_catalog_endpoint(api_url) != normalize_catalog_endpoint(cast(str, self.assistantApiUrl)):
            return 0
        records = [{**row, "evidence": row.get("evidence", "models_endpoint")} for row in models]
        changed = sum(row.get("evidence") == "models_endpoint"
                      and bool(model_metadata_capacity(api_url, row.get("id", ""), row.get("metadata")))
                      for row in records)
        if changed:
            self._discovery_bus.publish(api_url, records, expected_fingerprint=fingerprint)
            self.assistantStateChanged.emit()
        return changed

    def _adopt_probe_credentials(self, api_url: str, secret: str, fingerprint: str | None) -> None:
        """Adopt an explicit manual key-store read before every probe kind."""
        if normalize_catalog_endpoint(api_url) != normalize_catalog_endpoint(cast(str, self.assistantApiUrl)):
            return
        previous = self._assistant_credential_fingerprint
        self._credential_generation += 1
        self._credential_pending = None
        self._assistant_credential_api_url = api_url
        self._assistant_credential_fingerprint = fingerprint
        self._assistant_credential_secret = secret
        self._has_assistant_key = bool(secret)
        self._assistant_key_status_ready = True
        bus = self._discovery_bus_value
        if bus is not None and previous is not None and previous != fingerprint:
            bus.invalidate(api_url)
        self.assistantStateChanged.emit()

    def _model_budget_config(self, api_url: str, model: str) -> dict:
        """Attach only this window's current credential-bound metadata snapshot."""
        config = dict(self._settings)
        config.pop("assistant_reasoning_probe", None)
        bus = self._discovery_bus_value
        if bus is None:
            return config
        fingerprint = self._assistant_credential_fingerprint
        coordinator = self._probe_coordinator
        if (coordinator._model_discovery_busy
                and coordinator._probe_original_url == self.assistantApiUrl
                and coordinator._probe_credential_generation == self._credential_generation):
            fingerprint = coordinator._probe_fingerprint
        config["assistant_reasoning_probe"] = bus.reasoning_for_fingerprint(api_url, model, fingerprint)
        return config

    @Property(str, notify=assistantStateChanged)
    def assistantModelConfigurationInfo(self) -> str:
        return self.providerModelInfo(cast(str, self.assistantApiUrl), cast(str, self.assistantModel))

    @Slot(str, result="QVariantList")
    def providerModelSuggestions(self, api_url: str) -> list[dict[str, str]]:
        return provider_model_suggestions(api_url)

    @Slot(str, result=str)
    def providerInfoForUrl(self, api_url: str) -> str:
        return _provider_info_text(provider_profile_for_url(api_url))

    @Slot(str, result=bool)
    def providerProbeAllowedForUrl(self, api_url: str) -> bool:
        profile = provider_profile_for_url(api_url)
        return bool(profile.get("probe_allowed")) if profile is not None else True

    @Slot(str)
    def rememberAssistantApiUrl(self, value: str) -> None:
        try:
            url = validate_assistant_url(value)
        except ValueError:
            return
        history = [item for item in self.assistantApiUrls if item != url]
        self._settings["assistant_api_urls"] = (history + [url])[-30:]
        # Only this field is immediately persisted; preserve all other unsaved drafts.
        try:
            saved = load_ui_settings()
            saved["assistant_api_urls"] = self._settings["assistant_api_urls"]
            save_ui_settings(saved)
        except OSError as exc:
            self._set_status(f"地址历史保存失败：{exc}")
        self.assistantStateChanged.emit()

    @Property(str, notify=assistantStateChanged)
    def assistantKeyMessage(self) -> str:
        return self._assistant_key_message

    @Property(bool, notify=assistantStateChanged)
    def assistantKeyStatusReady(self) -> bool:
        return self._assistant_key_status_ready

    @property
    def _discovery_bus(self) -> ModelDiscoveryBus:
        bus = self._discovery_bus_value
        if bus is None:
            bus = model_discovery_bus()
            self._discovery_bus_value = bus
            bus.modelsChanged.connect(self._on_shared_discovery)
            bus.reasoningChanged.connect(self._on_shared_reasoning)
            bus.probeFinished.connect(self._on_shared_probe_finished)
        return bus

    def _set_key_message(self, message: str) -> None:
        self._assistant_key_message = message
        self.assistantStateChanged.emit()

    @Slot()
    def prepareAssistantSettings(self) -> None:
        """Load credential-bound metadata for the settings controls on demand."""
        already_requested = self._assistant_settings_requested
        self._assistant_settings_requested = True
        if not already_requested or not self._assistant_key_status_ready:
            self._refresh_bound_key()

    def _refresh_bound_key(self, *, force: bool = False, reason: str = "bootstrap") -> None:
        api_url = cast(str, self.assistantApiUrl)
        if (not force and self._assistant_key_status_ready
                and self._assistant_credential_api_url == api_url):
            return
        if self._credential_pending is not None and self._credential_pending[1] == api_url:
            if reason == "auto_probe":
                self._credential_pending_reason = reason
            return
        if self._credential_request is not None and self._credential_request[1] == api_url:
            if reason == "auto_probe":
                self._credential_request_reason = reason
            return
        if self._credential_future is not None and self._credential_request is not None:
            self._credential_future.cancel()
        if self._credential_future is not None and self._credential_request is not None \
                and self._credential_request[1] != api_url:
            self._credential_future = None
            self._credential_request = None
            self._credential_poll.stop()
        self._credential_generation += 1
        self._credential_pending = (self._credential_generation, api_url)
        self._credential_pending_reason = reason
        self._assistant_credential_api_url = None
        self._assistant_credential_secret = ""
        self._assistant_credential_fingerprint = None
        self._assistant_key_status_ready = False
        self._has_assistant_key = False
        if not api_url:
            self._credential_pending = None
            self._assistant_credential_api_url = api_url
            self._assistant_key_status_ready = True
            self._set_key_message("\u5f53\u524d\u5730\u5740\u672a\u7ed1\u5b9a\u5bc6\u94a5\u3002")
            self._probe_coordinator._available_models = []
            self.modelDiscoveryChanged.emit()
            return
        if self._assistant_settings_requested:
            self._set_key_message("\u6b63\u5728\u8bfb\u53d6\u7cfb\u7edf\u51ed\u636e\u72b6\u6001\u2026")
        self._credential_refresh_debounce.start(0 if reason == "auto_probe" else 150)

    def _start_bound_key_lookup(self) -> None:
        request = self._credential_pending
        if request is None or request[0] != self._credential_generation:
            return
        if self._credential_executor is None:
            self._credential_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="settings-credential-cache")
        generation, api_url = request
        self._credential_request = request
        self._credential_request_reason = self._credential_pending_reason
        self._credential_pending = None
        self._credential_pending_reason = "bootstrap"
        self._credential_future = self._credential_executor.submit(
            _read_settings_bootstrap_snapshot, api_url)
        if not self._credential_poll.isActive():
            self._credential_poll.start()

    def _on_bound_key_lookup_ready(self) -> None:
        future = self._credential_future
        request = self._credential_request
        if future is None or request is None or not future.done():
            return
        self._credential_future = None
        self._credential_request = None
        self._credential_poll.stop()
        reason = self._credential_request_reason
        self._credential_request_reason = "bootstrap"
        generation, api_url = request
        try:
            has_key, credential_error, fingerprint, entries, signature, secret = future.result()
        except Exception:
            has_key, credential_error, fingerprint, entries, signature, secret = False, True, None, {}, None, ""
        if generation != self._credential_generation or api_url != self.assistantApiUrl:
            return
        bus = self._discovery_bus
        bus.install_cache_snapshot(entries, signature)
        self._assistant_credential_fingerprint = fingerprint
        self._assistant_credential_api_url = api_url
        self._assistant_credential_secret = secret
        self._has_assistant_key = has_key
        self._assistant_key_status_ready = True
        if credential_error:
            message = "\u7cfb\u7edf\u5b89\u5168\u51ed\u636e\u5e93\u8bfb\u53d6\u5931\u8d25\uff0c\u672a\u66b4\u9732\u5bc6\u94a5\u3002"
        elif has_key:
            message = "\u5f53\u524d\u5730\u5740\u5df2\u4fdd\u5b58\u5bc6\u94a5\uff0c\u5c1a\u672a\u9a8c\u8bc1\u8fdc\u7a0b\u670d\u52a1\u3002"
        else:
            message = "\u5f53\u524d\u5730\u5740\u672a\u4fdd\u5b58\u5bc6\u94a5\u3002"
        self._set_key_message(message)
        if fingerprint:
            self._probe_coordinator._available_models = bus.models_for_fingerprint(api_url, fingerprint, reload_cache=False)
        else:
            self._probe_coordinator._available_models = []
        self.modelDiscoveryChanged.emit()
        if reason == "auto_probe":
            if (self.assistantEnabled and not self._probe_coordinator._probe_closed
                    and time.time() >= self._probe_coordinator._auto_paused_until
                    and self._has_assistant_key and fingerprint
                    and generation == self._credential_generation
                    and api_url == self.assistantApiUrl
                    and not self.modelDiscoveryBusy
                    and not self._discovery_bus.models_fresh_for_fingerprint(
                        api_url, fingerprint, reload_cache=False)):
                self._begin_models_probe(automatic=True)
            return
        self._schedule_auto_probe()

    def _schedule_auto_probe(self) -> None:
        self._probe_coordinator._schedule_auto_probe()

    @Slot(str)
    def _on_shared_probe_finished(self, api_url: str) -> None:
        bus = self._discovery_bus_value
        if bus is None or api_url != bus.api_key(self.assistantApiUrl):
            return
        if self._assistant_credential_fingerprint:
            self._probe_coordinator._available_models = bus.models_for_fingerprint(
                cast(str, self.assistantApiUrl), self._assistant_credential_fingerprint, reload_cache=False)
            self.modelDiscoveryChanged.emit()
        elif self._assistant_settings_requested:
            self._refresh_bound_key()

    @Slot()
    def resumeModelProbe(self) -> None:
        self._probe_coordinator._probe_closed = False
        if self.assistantEnabled:
            self._refresh_bound_key()
        self._schedule_auto_probe()
        if self._assistant_settings_requested and not self._assistant_key_status_ready:
            self.prepareAssistantSettings()

    def _auto_discover(self) -> None:
        self._probe_coordinator._auto_discover()

    @Property(str, notify=assistantStateChanged)
    def assistantModel(self) -> str:
        return str(self._settings["assistant_model"])

    @Property("QVariantList", notify=modelDiscoveryChanged)
    def availableModels(self) -> list[dict[str, object]]:
        return [dict(row) for row in self._probe_coordinator._available_models]

    @Property(str, notify=assistantStateChanged)
    def assistantTaskMode(self) -> str:
        return str(self._settings["assistant_task_mode"])

    def _reasoning_capabilities(self) -> dict:
        bus = self._discovery_bus
        if self._assistant_settings_requested:
            probe = bus.reasoning_for_fingerprint(
                self.assistantApiUrl, self.assistantModel,
                self._assistant_credential_fingerprint)
        else:
            probe = bus.reasoning_for(self.assistantApiUrl, self.assistantModel)
        return reasoning_capabilities(self.assistantApiUrl, self.assistantModel, probe)

    @Property(str, notify=assistantStateChanged)
    def assistantReasoningStrength(self) -> str:
        selected = str(self._settings["assistant_reasoning_strength"])
        return selected if selected in self.assistantReasoningOptions else "auto"

    @Property("QStringList", notify=assistantStateChanged)
    def assistantReasoningOptions(self) -> list[str]:
        return list(self._reasoning_capabilities().get("supported_strengths", ["auto"]))

    @Property("QStringList", notify=assistantStateChanged)
    def assistantNativeReasoningOptions(self) -> list[str]:
        return self._probe_coordinator.assistant_native_reasoning_options()

    @Property(bool, notify=assistantStateChanged)
    def assistantReasoningSupported(self) -> bool:
        return bool(self._reasoning_capabilities().get("reasoning_supported"))

    @Property(str, notify=assistantStateChanged)
    def assistantReasoningDescription(self) -> str:
        return str(self._reasoning_capabilities().get("detail", ""))

    @Slot(str, object)
    def _on_shared_discovery(self, api_url: str, models: object) -> None:
        if api_url == self._discovery_bus.api_key(self.assistantApiUrl):
            coordinator = self._probe_coordinator
            fingerprint = self._assistant_credential_fingerprint
            if (coordinator._model_discovery_busy
                    and coordinator._probe_original_url == self.assistantApiUrl
                    and coordinator._probe_credential_generation == self._credential_generation):
                fingerprint = coordinator._probe_fingerprint
            # A shared signal has no credential identity. Re-read only the in-memory
            # snapshot for this window's bound key; never trust its row payload.
            if self._discovery_bus._entry_for_fingerprint(api_url, fingerprint, reload_cache=False) is None:
                if isinstance(models, list) and not models:
                    coordinator._available_models = []
                    self.modelDiscoveryChanged.emit()
                self.assistantStateChanged.emit()
                return
            coordinator._available_models = self._discovery_bus.models_for_fingerprint(
                api_url, fingerprint, reload_cache=False)
            self.modelDiscoveryChanged.emit()
            self.assistantStateChanged.emit()

    @Slot(str, str, object)
    def _on_shared_reasoning(self, api_url: str, model: str, result: object) -> None:
        if api_url == self._discovery_bus.api_key(self.assistantApiUrl) and model == self.assistantModel:
            self.assistantStateChanged.emit()

    @Property(bool, notify=modelDiscoveryChanged)
    def modelDiscoveryBusy(self) -> bool:
        return self._probe_coordinator.modelDiscoveryBusy

    @Property(str, notify=modelDiscoveryChanged)
    def modelDiscoveryMessage(self) -> str:
        return self._probe_coordinator._model_discovery_message

    def _set_model_discovery_message(self, message: str) -> None:
        self._probe_coordinator._set_model_discovery_message(message)

    @Property(int, notify=assistantStateChanged)
    def assistantDailyTokenLimit(self) -> int:
        return int(self._settings["assistant_daily_token_limit"])

    @Property(int, notify=assistantStateChanged)
    def assistantPeriodTokenLimit(self) -> int:
        return int(self._settings["assistant_period_token_limit"])

    @Property(int, notify=assistantStateChanged)
    def assistantPeriodMinutes(self) -> int:
        return int(self._settings["assistant_period_minutes"])

    @Property(int, notify=assistantStateChanged)
    def assistantIconSize(self) -> int:
        return int(self._settings["assistant_icon_size"])

    @Property(str, notify=assistantStateChanged)
    def computeServerUrl(self) -> str:
        return str(self._settings["compute_server_url"])

    @Property(bool, notify=assistantStateChanged)
    def computeServerTls(self) -> bool:
        return bool(self._settings["compute_server_tls"])

    @Property(str, notify=assistantStateChanged)
    def computeServerCaFile(self) -> str:
        return str(self._settings["compute_server_ca_file"])

    @Property(bool, notify=assistantStateChanged)
    def computeServerHasToken(self) -> bool:
        return self._compute_server_has_token

    @Property(str, notify=assistantStateChanged)
    def computeServerTokenStatus(self) -> str:
        return self._compute_server_token_status

    def _refresh_compute_server_token_status(self) -> None:
        url = str(self._settings.get("compute_server_url", ""))
        if not url:
            self._compute_server_has_token = False
            self._compute_server_token_status = "填写并保存服务器地址后，可检查令牌是否已安全保存。"
            return
        try:
            self._compute_server_has_token = read_compute_token(url, strict=True) is not None
            self._compute_server_token_status = (
                "令牌已安全保存；连接状态尚未检查。" if self._compute_server_has_token
                else "尚未保存令牌；连接状态尚未检查。")
        except (OSError, ValueError):
            self._compute_server_has_token = False
            self._compute_server_token_status = "系统安全凭据库不可用，令牌状态未知。"

    @Slot()
    def refreshComputeServerTokenStatus(self) -> None:
        self._refresh_compute_server_token_status()
        self.assistantStateChanged.emit()

    @Slot(str, bool, str)
    def setComputeServerSettings(self, url: str, tls_enabled: bool, ca_file: str) -> None:
        previous_url = self.computeServerUrl
        self._settings["compute_server_url"] = _compute_server_url(url)
        self._settings["compute_server_tls"] = tls_enabled if type(tls_enabled) is bool else True
        self._settings["compute_server_ca_file"] = _compute_server_ca_file(ca_file)
        if previous_url != self.computeServerUrl:
            self._refresh_compute_server_token_status()
        self.assistantStateChanged.emit()

    @Slot(str, result=bool)
    def saveComputeServerToken(self, secret: str) -> bool:
        url = self.computeServerUrl
        if not url:
            self._compute_server_token_status = "先填写并保存有效的计算服务器地址。"
            self.assistantStateChanged.emit()
            return False
        try:
            save_compute_token(secret, url)
            self._compute_server_has_token = True
            self._compute_server_token_status = "令牌已安全保存；连接状态尚未检查。"
        except OSError as exc:
            self._compute_server_token_status = credential_save_error_message(exc)
            self._compute_server_has_token = False
            self.assistantStateChanged.emit()
            return False
        except ValueError:
            self._compute_server_token_status = "令牌格式无效或服务器地址尚未保存。"
            self.assistantStateChanged.emit()
            return False
        self.assistantStateChanged.emit()
        return True

    @Slot(result=bool)
    def deleteComputeServerToken(self) -> bool:
        if not self.computeServerUrl:
            self._compute_server_token_status = "没有已配置的计算服务器地址。"
            self.assistantStateChanged.emit()
            return False
        try:
            delete_compute_token(self.computeServerUrl)
        except (OSError, ValueError):
            self._compute_server_token_status = "无法删除系统安全凭据中的令牌。"
            self.assistantStateChanged.emit()
            return False
        self._compute_server_has_token = False
        self._compute_server_token_status = "令牌已从系统安全凭据库删除；连接状态尚未检查。"
        self.assistantStateChanged.emit()
        return True

    @Property(bool, notify=assistantStateChanged)
    def hasAssistantApiKey(self) -> bool:
        return self._has_assistant_key

    @Property(str, notify=statusChanged)
    def statusMessage(self) -> str:
        return self._status

    @Property(bool, notify=restartRequiredChanged)
    def restartRequired(self) -> bool:
        return self._restart_required

    def _set_status(self, message: str) -> None:
        if self._status != message:
            self._status = message
            self.statusChanged.emit()

    def _set_restart_required(self, required: bool) -> None:
        if self._restart_required != required:
            self._restart_required = required
            self.restartRequiredChanged.emit()

    @Slot(int)
    def setFontPointSize(self, value: int) -> None:
        updated = _bounded_integer(value, MIN_FONT_POINT_SIZE, MAX_FONT_POINT_SIZE, self.fontPointSize)
        if self._settings["font_point_size"] != updated:
            self._settings["font_point_size"] = updated
            self.changed.emit()

    @Slot(str)
    def setFontFamily(self, value: str) -> None:
        family = _short_text(value, 128)
        if self._settings.get("font_family", "") != family:
            self._settings["font_family"] = family
            self.changed.emit()

    @Slot(int)
    def setDensityScalePercent(self, value: int) -> None:
        updated = _bounded_integer(
            value, MIN_DENSITY_SCALE_PERCENT, MAX_DENSITY_SCALE_PERCENT,
            self.densityScalePercent,
        )
        if self._settings["density_scale_percent"] != updated:
            self._settings["density_scale_percent"] = updated
            self.changed.emit()

    @Slot(str)
    def setThemeMode(self, value: str) -> None:
        updated = _choice(value, THEME_MODES, self.themeMode)
        if self._settings["theme_mode"] != updated:
            self._settings["theme_mode"] = updated
            self.changed.emit()

    @Slot(str)
    def setThemePackage(self, value: str) -> None:
        theme_id = _theme_package_id(value)
        if theme_id != value or not self._theme_service.has_theme(theme_id):
            self._set_theme_package_status("主题包未安装或 ID 无效。")
            return
        if self._settings.get("theme_package_id", "") != theme_id:
            self._settings["theme_package_id"] = theme_id
            self.changed.emit()
        self._set_theme_package_status("已选择主题包；保存设置后应用。")

    @Slot(str, result=bool)
    def installThemePackage(self, archive_path: str) -> bool:
        try:
            installed = self._theme_service.install_package(archive_path)
        except (OSError, ValueError) as exc:
            self._set_theme_package_status(f"主题包安装失败：{exc}")
            return False
        self._settings["theme_package_id"] = installed.package.package_id
        self.changed.emit()
        self._set_theme_package_status(
            f"已安装并选中“{installed.package.name}”；保存设置后应用。"
        )
        return True

    @Slot(QUrl, result=str)
    def localFilePath(self, url: QUrl) -> str:
        path = local_file_path(url)
        if not path:
            self._set_theme_package_status("请选择本地文件。")
        return path

    def _set_theme_package_status(self, value: str) -> None:
        if self._theme_package_status != value:
            self._theme_package_status = value
            self.themePackageStatusChanged.emit()

    @Slot()
    def _on_theme_packages_changed(self) -> None:
        self.themePackagesChanged.emit()

    @Slot(int)
    def setDpiScalePercent(self, value: int) -> None:
        updated = _bounded_integer(value, MIN_DPI_SCALE_PERCENT, MAX_DPI_SCALE_PERCENT, self.dpiScalePercent)
        if self._settings["dpi_scale_percent"] != updated:
            self._settings["dpi_scale_percent"] = updated
            self.changed.emit()

    @Property(str, notify=assistantStateChanged)
    def assistantUserName(self):
        return str(self._settings.get("assistant_user_name", "用户"))

    @Property(str, notify=assistantStateChanged)
    def assistantTone(self) -> str:
        return normalized_tone(self._settings.get("assistant_tone", DEFAULT_ASSISTANT_TONE))

    @Property("QStringList", notify=assistantStateChanged)
    def assistantToneOptions(self) -> list[str]:
        return list(ASSISTANT_TONES)

    def _selected_model_limits(self) -> dict[str, object]:
        return model_token_limits(self._model_budget_config(cast(str, self.assistantApiUrl), cast(str, self.assistantModel)),
                                  cast(str, self.assistantApiUrl), cast(str, self.assistantModel))

    def _selected_context_budget(self) -> dict[str, object]:
        return context_budget_for_model(self._model_budget_config(cast(str, self.assistantApiUrl), cast(str, self.assistantModel)),
                                        cast(str, self.assistantApiUrl), cast(str, self.assistantModel))

    def _context_profile_record(self, *, create: bool = False) -> tuple[str, str, dict] | None:
        model = self.assistantModel.strip()
        try:
            endpoint = normalize_api_url(validate_assistant_url(self.assistantApiUrl))
        except ValueError:
            return None
        if not model:
            return None
        rows = self._settings.get("assistant_model_limits")
        if not isinstance(rows, dict):
            if not create:
                return None
            rows = {}
            self._settings["assistant_model_limits"] = rows
        models = rows.get(endpoint)
        if not isinstance(models, dict):
            if not create:
                return None
            models = {}
            rows[endpoint] = models
        record = models.get(model)
        if not isinstance(record, dict):
            if not create:
                return None
            current_budget = self._selected_context_budget()
            hard_input = current_budget.get("limits", {}).get("legacy_max_input_tokens", 0)
            record = {
                "context_budget_version": 2,
                "context_window_tokens": 0,
                "reserved_output_tokens": 0,
                "compression_reserve_tokens": 0,
                "history_keep_tokens": 0,
                "compaction_threshold_percent": 0,
                "max_input_tokens": hard_input,
                "legacy_values": {},
            }
            models[model] = record
        return endpoint, model, record

    @Property("QVariantMap", notify=assistantStateChanged)
    def assistantContextBudget(self) -> dict[str, object]:
        budget = deepcopy(self._selected_context_budget())
        profile = budget.get("profile")
        if isinstance(profile, dict) and isinstance(profile.get("source"), tuple):
            profile["source"] = list(profile["source"])
        return budget

    @Property("QVariantMap", notify=assistantStateChanged)
    def assistantContextProfileOverrides(self) -> dict[str, int]:
        values = self._selected_context_budget().get("overrides", {})
        return dict(values) if isinstance(values, Mapping) else {}

    @Slot(int, int, int, int, int)
    def setAssistantContextProfile(self, context_window: int, reserved_output: int,
                                   compression_reserve: int, history_keep: int,
                                   compaction_threshold_percent: int) -> None:
        selected = self._context_profile_record(create=True)
        if selected is None:
            self._set_status("请先填写有效服务商地址并选择模型，再编辑上下文配置。")
            return
        endpoint, model, record = selected
        values = (
            context_window,
            reserved_output,
            compression_reserve,
            history_keep,
            compaction_threshold_percent,
        )
        self._archive_context_migration_error(record)
        for field, value in zip((
                "context_window_tokens", "reserved_output_tokens",
                "compression_reserve_tokens", "history_keep_tokens",
                "compaction_threshold_percent"), values):
            record[field] = value
        record["context_budget_version"] = 2
        self._settings["assistant_context_budget_version"] = 2
        self._context_profile_touched.add((endpoint, model))
        self.assistantStateChanged.emit()

    @staticmethod
    def _archive_context_migration_error(record: dict) -> None:
        """Keep a repairable migration error in audit data, outside validation."""
        error = record.pop("migration_error", None)
        if not error:
            return
        legacy = record.setdefault("legacy_values", {})
        if not isinstance(legacy, dict):
            legacy = {"legacy_values": legacy}
            record["legacy_values"] = legacy
        errors = legacy.setdefault("migration_errors", [])
        if not isinstance(errors, list):
            errors = [errors]
            legacy["migration_errors"] = errors
        if error not in errors:
            errors.append(error)

    @staticmethod
    def _repair_invalid_legacy_input_cap(record: dict) -> bool:
        """Archive an invalid hidden legacy cap only on explicit reset."""
        value = record.get("max_input_tokens", 0)
        if type(value) is int and 0 <= value <= 2_000_000:
            return False
        legacy = record.setdefault("legacy_values", {})
        if not isinstance(legacy, dict):
            legacy = {"legacy_values": legacy}
            record["legacy_values"] = legacy
        legacy.setdefault("invalid_max_input_tokens", value)
        record["max_input_tokens"] = 0
        return True

    @Slot(result=bool)
    def resetAssistantContextProfile(self) -> bool:
        selected = self._context_profile_record(create=True)
        if selected is None:
            self._set_status("请先填写有效服务商地址并选择模型。")
            return False
        endpoint, model, record = selected
        self._archive_context_migration_error(record)
        repaired_legacy_cap = self._repair_invalid_legacy_input_cap(record)
        for field in (
                "context_window_tokens", "reserved_output_tokens",
                "compression_reserve_tokens", "history_keep_tokens",
                "compaction_threshold_percent"):
            record[field] = 0
        # Preserve the v2 marker, legacy backup, and independent hard input cap.
        record["context_budget_version"] = 2
        self._settings["assistant_context_budget_version"] = 2
        self._context_profile_touched.add((endpoint, model))
        self.assistantStateChanged.emit()
        self._set_status("\u5f53\u524d\u6a21\u578b\u4e94\u9879\u914d\u7f6e\u5df2\u8bbe\u4e3a\u81ea\u52a8\uff1b\u4fdd\u5b58\u540e\u751f\u6548\u3002" + ("\u65e0\u6548\u7684\u65e7\u7248\u8f93\u5165\u4e0a\u9650\u5df2\u5f52\u6863\u3002" if repaired_legacy_cap else ""))
        return True


    def _context_profile_save_error(self) -> str:
        pairs = set(self._context_profile_touched)
        try:
            endpoint = normalize_api_url(validate_assistant_url(self.assistantApiUrl))
        except ValueError:
            endpoint = ""
        model = self.assistantModel.strip()
        if endpoint and model:
            pairs.add((endpoint, model))
        for api_url, model_id in sorted(pairs):
            candidate = self._model_budget_config(api_url, model_id)
            candidate["assistant_api_url"] = api_url
            candidate["assistant_model"] = model_id
            budget = context_budget_for_model(candidate, api_url, model_id)
            errors = budget.get("validation_errors", [])
            if errors:
                detail = "；".join(str(error) for error in errors)
                return f"模型 {model_id} 的上下文配置无效：{detail}"
        return ""

    @Property("QStringList", notify=assistantStateChanged)
    def assistantContextProfileErrors(self) -> list[str]:
        errors = self._selected_context_budget().get("validation_errors", [])
        return [str(error) for error in errors] if isinstance(errors, list) else []

    @Property(int, notify=assistantStateChanged)
    def assistantContextTokens(self):
        return cast(int, self._selected_model_limits()["context_window_tokens"])

    @Property(int, notify=assistantStateChanged)
    def assistantMaxInputTokens(self):
        return self._selected_model_limits()["max_input_tokens"]

    @Property(int, notify=assistantStateChanged)
    def assistantMaxOutputTokens(self):
        return self._selected_model_limits()["max_output_tokens"]

    @Property(int, notify=assistantStateChanged)
    def assistantCompactionThresholdPercent(self):
        return self._selected_model_limits()["compaction_threshold_percent"]

    @Slot(int, int, int)
    @Slot(int, int, int, int)
    def setAssistantModelLimits(self, maximum_input: int, maximum_output: int, context: int,
                                compaction_threshold_percent: int | None = None) -> None:
        """Compatibility setter that updates legacy limits without losing v2 fields."""
        selected = self._context_profile_record(create=True)
        if selected is None:
            self._set_status("Please choose a model and valid endpoint before editing model limits.")
            return
        endpoint, model, record = selected
        current_budget = self._selected_context_budget()
        current = current_budget.get("limits", {})
        fallback_input = current.get("legacy_max_input_tokens", 0)
        record["max_input_tokens"] = _bounded_integer(
            maximum_input, 0, 2_000_000, fallback_input)
        record["reserved_output_tokens"] = _bounded_integer(
            maximum_output, 0, 2_000_000, current.get("reserved_output_tokens", 0))
        record["context_window_tokens"] = _bounded_integer(
            context, 0, 2_000_000, current.get("context_window_tokens", 0))
        if compaction_threshold_percent is not None:
            record["compaction_threshold_percent"] = _bounded_integer(
                compaction_threshold_percent, 0, 100,
                current.get("compaction_threshold_percent", 0))
        self._archive_context_migration_error(record)
        record["context_budget_version"] = 2
        self._settings["assistant_context_budget_version"] = 2
        self._context_profile_touched.add((endpoint, model))
        self.assistantStateChanged.emit()

    @Slot(str)
    def setAssistantUserName(self, name):
        self._settings["assistant_user_name"] = _short_text(name, 40) or "用户"
        self.assistantStateChanged.emit()

    @Slot(str)
    def setAssistantTone(self, value: str) -> None:
        updated = normalized_tone(value)
        if self.assistantTone != updated:
            self._settings["assistant_tone"] = updated
            self.assistantStateChanged.emit()

    @Slot(int)
    def setAssistantContextTokens(self, value):
        selected = self._context_profile_record(create=True)
        if selected is None:
            return
        endpoint, model, record = selected
        self._archive_context_migration_error(record)
        record["context_window_tokens"] = _bounded_integer(
            value, 0, 2_000_000, record.get("context_window_tokens", 0))
        record["context_budget_version"] = 2
        self._settings["assistant_context_budget_version"] = 2
        self._context_profile_touched.add((endpoint, model))
        self.assistantStateChanged.emit()

    @Slot(bool, str, str)
    def setAssistantConfig(self, enabled: bool, api_url: str, model: str) -> None:
        previous_url = self.assistantApiUrl
        previous_model = self.assistantModel
        self._settings["assistant_enabled"] = enabled
        self._settings["assistant_api_url"] = api_url.strip().rstrip("/")
        self._settings["assistant_model"] = _short_text(model, 120)
        if self.assistantApiUrl != previous_url or (self.modelDiscoveryBusy and self.assistantModel != previous_model):
            self._reset_model_discovery()
        if (self._discovery_bus_value is not None
                and (not self._assistant_settings_requested or self._assistant_key_status_ready)
                and self._settings["assistant_reasoning_strength"] not in self.assistantReasoningOptions):
            self._settings["assistant_reasoning_strength"] = "auto"
        if self.assistantApiUrl != previous_url and (self.assistantEnabled or self._assistant_settings_requested):
            self._refresh_bound_key()
        elif self.assistantEnabled and not self._assistant_key_status_ready:
            self._refresh_bound_key()
        self._probe_coordinator._probe_closed = False
        self._probe_coordinator._auto_paused_until = 0.0
        self.assistantStateChanged.emit()
        self._schedule_auto_probe()

    @Slot(str, str)
    def setAssistantTaskOptions(self, mode: str, strength: str) -> None:
        self._settings["assistant_task_mode"] = _choice(mode, ASSISTANT_TASK_MODES, "chat")
        current = _choice(self._settings.get("assistant_reasoning_strength"),
                          ASSISTANT_REASONING_STRENGTHS, "auto")
        selected = _choice(strength, ASSISTANT_REASONING_STRENGTHS, current)
        if not self._assistant_settings_requested or self._assistant_key_status_ready:
            current = selected if selected in self.assistantReasoningOptions else "auto"
        self._settings["assistant_reasoning_strength"] = current
        self.assistantStateChanged.emit()

    @Slot(int, int, int)
    def setAssistantTokenLimits(self, daily: int, period: int, minutes: int) -> None:
        self._settings["assistant_daily_token_limit"] = _bounded_integer(
            daily, 0, 2_000_000_000, self.assistantDailyTokenLimit)
        self._settings["assistant_period_token_limit"] = _bounded_integer(
            period, 0, 2_000_000_000, self.assistantPeriodTokenLimit)
        self._settings["assistant_period_minutes"] = _bounded_integer(
            minutes, 1, 10080, self.assistantPeriodMinutes)
        self.assistantStateChanged.emit()

    @Slot(int)
    def setAssistantIconSize(self, value: int) -> None:
        self._settings["assistant_icon_size"] = _bounded_integer(
            value, 48, 256, self.assistantIconSize)
        self.assistantStateChanged.emit()

    @Slot(int)
    def setAssistantNotificationVolumePercent(self, value: int) -> None:
        updated = _bounded_integer(
            value, 0, 100, DEFAULT_ASSISTANT_NOTIFICATION_VOLUME_PERCENT)
        self._settings["assistant_notification_volume_percent"] = updated
        self._sync_assistant_notification_volume()
        self.assistantStateChanged.emit()

    def _sync_assistant_notification_volume(self) -> None:
        app = QGuiApplication.instance()
        desktop = getattr(app, "_coat_assistant_desktop_bridge", None) if app is not None else None
        if desktop is not None:
            desktop.setNotificationSoundVolumePercent(self.assistantNotificationVolumePercent)

    @Slot()
    def previewAssistantNotificationSound(self) -> None:
        from coat_sim.ui.assistant_desktop import preview_notification_sound

        preview_notification_sound(self.assistantNotificationVolumePercent)

    @Slot(str)
    def setAssistantTaskMode(self, mode: str) -> None:
        self.setAssistantTaskOptions(mode, self.assistantReasoningStrength)

    @Slot(str)
    def setAssistantReasoningStrength(self, strength: str) -> None:
        self.setAssistantTaskOptions(self.assistantTaskMode, strength)

    def _reset_model_discovery(self) -> None:
        self._probe_coordinator.reset_model_discovery()

    @Property(str, notify=modelDiscoveryChanged)
    def modelDiscoveryKind(self) -> str:
        return self._probe_coordinator.modelDiscoveryKind

    @Property("QVariantList", notify=modelDiscoveryChanged)
    def probeResults(self) -> list[dict]:
        return deepcopy(self._probe_coordinator._probe_results)

    @Property(str, notify=modelDiscoveryChanged)
    def probeUsage(self) -> str:
        usage = self._probe_coordinator._probe_usage
        count = usage.get("requests", 0)
        if not count:
            return ""
        total = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        missing = usage.get("unreported_requests", 0)
        unit_value = format_tokens(total)
        text = f"本次 {count} 次小请求；服务器实报 {unit_value} Token"
        return text + (f"；{missing} 次未返回用量，未计入实报" if missing else "")

    @Slot()
    def cancelModelProbe(self) -> None:
        self._probe_coordinator.cancel_model_probe()

    def _start_probe(self, kind: str, automatic: bool = False) -> bool:
        return self._probe_coordinator._start_probe(kind, automatic)

    @Slot()
    def discoverModels(self) -> None:
        self.rememberAssistantApiUrl(self.assistantApiUrl)
        self._probe_coordinator._auto_paused_until = 0.0
        self._probe_coordinator._begin_models_probe()

    def _begin_models_probe(self, automatic: bool = False) -> None:
        self._probe_coordinator._begin_models_probe(automatic)

    def _queue_model_candidate_fallback(self, detail: str) -> None:
        self._probe_coordinator._queue_model_candidate_fallback(detail)

    @Slot()
    def discoverReasoning(self) -> None:
        self.rememberAssistantApiUrl(self.assistantApiUrl)
        self._probe_coordinator._auto_paused_until = 0.0
        self._probe_coordinator._begin_reasoning_probe()

    def _begin_reasoning_probe(self, automatic: bool = False) -> None:
        self._probe_coordinator._begin_reasoning_probe(automatic)

    @Slot()
    def closeModelProbe(self) -> None:
        self._probe_coordinator.closeModelProbe()

    def _shutdown_key_lookup(self) -> None:
        self._credential_refresh_debounce.stop()
        self._credential_poll.stop()
        if self._credential_future is not None:
            self._credential_future.cancel()
            self._credential_future = None
        self._credential_request_reason = "bootstrap"
        self._credential_pending_reason = "bootstrap"
        if self._credential_executor is not None:
            self._credential_executor.shutdown(wait=False, cancel_futures=True)
            self._credential_executor = None

    @Slot()
    def closeAssistantSettingsBootstrap(self) -> None:
        """Cancel pending UI delivery; a running key-store read is discarded by generation."""
        self._credential_generation += 1
        self._credential_pending = None
        self._credential_refresh_debounce.stop()
        self._credential_poll.stop()
        if self._credential_future is not None:
            self._credential_future.cancel()
            self._credential_future = None
        self._credential_request = None
        self._credential_request_reason = "bootstrap"
        self._credential_pending_reason = "bootstrap"
        self._assistant_key_status_ready = False
        self._assistant_credential_api_url = None
        self._assistant_credential_secret = ""
        self._assistant_credential_fingerprint = None
        self._has_assistant_key = False

    def _shutdown_probe_budget(self) -> None:
        self._probe_coordinator._shutdown_probe_budget()

    def _cleanup_destroyed_probe(self) -> None:
        self._probe_coordinator._cleanup_destroyed_probe()

    def _submit_probe_budget(self, operation, *args, callback=None) -> None:
        self._probe_coordinator._submit_probe_budget(operation, *args, callback=callback)

    def _on_probe_budget_ready(self) -> None:
        self._probe_coordinator._on_probe_budget_ready()

    def _probe_store(self):
        return self._probe_coordinator._probe_store()

    def _reserve_probe_budget(self, config, model, cancel_event):
        return self._probe_coordinator._reserve_probe_budget(config, model, cancel_event)

    def _settle_probe_budget(self, reservation, session, model, usage, status, sent=True):
        return self._probe_coordinator._settle_probe_budget(
            reservation, session, model, usage, status, sent)

    def _probe_credentials_unchanged(self) -> bool:
        return self._probe_coordinator._probe_credentials_unchanged()

    def _next_probe_request(self) -> None:
        self._probe_coordinator._next_probe_request()

    def _send_probe_request(self, task: dict, output_limit: int = PROBE_OUTPUT_TOKENS) -> None:
        self._probe_coordinator._send_probe_request(task, output_limit)

    def _on_model_timeout(self) -> None:
        self._probe_coordinator._on_model_timeout()

    def _on_probe_budget_timeout(self) -> None:
        self._probe_coordinator._on_probe_budget_timeout()

    def _on_models_ready_read(self, reply: QNetworkReply) -> None:
        self._probe_coordinator._on_models_ready_read(reply)

    def _record_probe(self, label: str, category: str, status: int | None = None, detail: str = "") -> None:
        self._probe_coordinator._record_probe(label, category, status, detail)

    def _on_models_finished(self, reply: QNetworkReply) -> None:
        self._probe_coordinator._on_models_finished(reply)

    def _finish_probe(self, message: str, cancelled: bool = False) -> None:
        self._probe_coordinator._finish_probe(message, cancelled)

    @Slot(str, result=bool)
    def saveAssistantApiKey(self, secret: str) -> bool:
        try:
            save_api_key(secret, self.assistantApiUrl)
        except ValueError:
            message = "密钥未保存：请检查服务地址及密钥长度（1–4096 字节）。"
            self._set_key_message(message)
            self._set_status(message)
            return False
        except OSError as exc:
            message = credential_save_error_message(exc)
            self._set_key_message(message)
            self._set_status(message)
            return False
        self._credential_generation += 1
        self._credential_pending = None
        self._assistant_credential_api_url = cast(str, self.assistantApiUrl)
        self._assistant_credential_secret = secret
        self._assistant_credential_fingerprint = ModelDiscoveryBus.credential_fingerprint(
            self.assistantApiUrl, secret=secret)
        self._has_assistant_key = True
        self._assistant_key_status_ready = True
        self._reset_model_discovery()
        self._discovery_bus.invalidate(cast(str, self.assistantApiUrl))
        self._probe_coordinator._probe_closed = False
        self._probe_coordinator._auto_paused_until = 0.0
        self._schedule_auto_probe()
        self.assistantStateChanged.emit()
        self.assistantSettingsSaved.emit()
        self._set_key_message("密钥已安全保存到当前地址；尚未验证远程服务。")
        self._set_status("API 密钥已保存到 按服务商地址绑定的系统安全凭据库；设置文件不包含密钥。")
        return True

    @Slot(result=bool)
    def clearAssistantApiKey(self) -> bool:
        try:
            delete_api_key(self.assistantApiUrl)
        except (OSError, ValueError):
            message = "密钥删除失败：请检查服务地址和系统安全凭据库。"
            self._set_key_message(message)
            self._set_status(message)
            return False
        self._credential_generation += 1
        self._credential_pending = None
        self._assistant_credential_api_url = cast(str, self.assistantApiUrl)
        self._assistant_credential_secret = ""
        self._assistant_credential_fingerprint = ModelDiscoveryBus.credential_fingerprint(
            self.assistantApiUrl, secret="")
        self._has_assistant_key = False
        self._assistant_key_status_ready = True
        self._reset_model_discovery()
        self._discovery_bus.invalidate(cast(str, self.assistantApiUrl))
        self._probe_coordinator._probe_closed = False
        self._probe_coordinator._auto_paused_until = 0.0
        self._schedule_auto_probe()
        self.assistantStateChanged.emit()
        self.assistantSettingsSaved.emit()
        self._set_key_message("当前地址密钥已删除；远程刷新需先保存密钥。")
        self._set_status("API 密钥已从 按服务商地址绑定的系统安全凭据库删除。")
        return True

    @Slot(result=bool)
    def save(self) -> bool:
        try:
            if self._settings["assistant_enabled"]:
                self._settings["assistant_api_url"] = validate_assistant_url(
                    str(self._settings["assistant_api_url"]))
                if not str(self._settings["assistant_model"]).strip():
                    raise ValueError("启用看板娘前请填写模型名")
            root = str(self._settings.get("assistant_research_library_root", ""))
            if root:
                from coat_sim.agent.research_library import validate_research_library_root
                validate_research_library_root(root)
            if self.assistantToolkitEnabled:
                validate_workspace_root(self.assistantToolkitWorkspaceRoot, PROJECT_ROOT,
                    allow_missing=is_private_workspace_root(self.assistantToolkitWorkspaceRoot))
            context_error = self._context_profile_save_error()
            if context_error:
                raise ValueError(context_error)
            if self.assistantApiUrl:
                self.rememberAssistantApiUrl(self.assistantApiUrl)
            self._settings = save_ui_settings(self._settings)
            self._context_profile_touched.clear()
            from coat_sim.ui.theme_service import configure_theme_from_settings

            configure_theme_from_settings(self._settings)
            apply_font_settings(self._settings)
            self._sync_assistant_notification_volume()
        except (OSError, ValueError) as exc:
            self._set_status(f"保存失败：{exc}")
            return False
        try:
            active_scale = round(float(os.environ.get("QT_SCALE_FACTOR", "1")) * 100)
        except ValueError:
            active_scale = DEFAULT_DPI_SCALE_PERCENT
        restart = self.dpiScalePercent != active_scale
        self._set_restart_required(restart)
        self._set_status("设置已保存；界面缩放将在重新打开程序后生效。" if restart else "设置已保存，字号已更新。")
        self.changed.emit()
        self.assistantStateChanged.emit()
        self.assistantSettingsSaved.emit()
        self._schedule_auto_probe()
        return True

    @Slot()
    def reloadAssistantSettings(self) -> None:
        """Refresh saved assistant state without overwriting font/DPI drafts."""
        previous_url = self.assistantApiUrl
        previous_compute_url = self.computeServerUrl
        loaded = load_ui_settings()
        for key, value in loaded.items():
            if key.startswith("assistant_") or key.startswith("compute_server_") or key == "developer_enabled":
                self._settings[key] = value
        self._settings["assistant_model_limits"] = migrate_model_context_overrides(self._settings)
        self._settings["assistant_context_budget_version"] = 2
        self._context_profile_touched.clear()
        if self.assistantApiUrl != previous_url:
            self._reset_model_discovery()
        elif self._discovery_bus_value is not None and not self._assistant_settings_requested:
            self._probe_coordinator._available_models = self._discovery_bus_value.models_for(self.assistantApiUrl)
        self._settings.pop("assistant_models", None)
        if self.computeServerUrl != previous_compute_url:
            self._refresh_compute_server_token_status()
        if self._assistant_settings_requested:
            self._refresh_bound_key()
        self._probe_coordinator._probe_closed = False
        self._probe_coordinator._auto_paused_until = 0.0
        self._schedule_auto_probe()
        self.assistantStateChanged.emit()
        self.modelDiscoveryChanged.emit()
        self.assistantSettingsReloaded.emit()
        self.mcpChanged.emit()

    @Slot()
    def restoreDefaults(self) -> None:
        self._settings = normalized_ui_settings()
        self._refresh_compute_server_token_status()
        self._sync_assistant_notification_volume()
        self._reset_model_discovery()
        self.changed.emit()
        self.assistantStateChanged.emit()
        self.modelDiscoveryChanged.emit()
        self.mcpChanged.emit()
        self._set_status("已恢复默认值；点击“保存”后生效。")

    @Property(list, notify=mcpChanged)
    def mcpServers(self) -> list[dict]:
        return [{**deepcopy(row), "description": service_description(row),
                 "status": self._mcp_statuses.get(row["id"], "尚未检查连接")}
                for row in self._settings["assistant_mcp_servers"]]

    @Property(str, notify=mcpChanged)
    def mcpMessage(self) -> str:
        return self._mcp_message

    @Property(bool, notify=mcpChanged)
    def mcpProbeBusy(self) -> bool:
        return self._mcp_probe is not None

    @Property(str, constant=True)
    def ownMcpStdioConfig(self) -> str:
        if getattr(sys, "frozen", False):
            from coat_sim.process_entry import module_command
            command = module_command("coat_sim.mcp.server")
            return json.dumps({"mcpServers": {"deposition-sim": {
                "command": command[0], "args": command[1:], "cwd": str(PROJECT_ROOT)}}},
                ensure_ascii=False, indent=2)
        return json.dumps(exported_stdio_config(self._mcp_python()), ensure_ascii=False, indent=2)

    @Property(str, constant=True)
    def ownMcpCommand(self) -> str:
        if getattr(sys, "frozen", False):
            import subprocess
            from coat_sim.process_entry import module_command
            return subprocess.list2cmdline(module_command("coat_sim.mcp.server"))
        return f'"{self._mcp_python()}" "{PROJECT_ROOT / "coat_sim" / "mcp" / "server.py"}"'

    @Property(bool, notify=mcpChanged)
    def ownMcpHttpRunning(self) -> bool:
        return self._mcp_http.state() != QProcess.NotRunning

    @Property(str, notify=mcpChanged)
    def ownMcpHttpStatus(self) -> str:
        return self._mcp_http_status

    @Slot()
    def copyOwnMcpHttpConfig(self) -> None:
        if not self._mcp_http_token:
            self._set_mcp_message("请先启动本机 HTTP 服务，再复制客户端配置。")
            return
        value = json.dumps({"mcpServers": {"deposition-sim": {"url":
            f"http://127.0.0.1:{self._mcp_http_port}/mcp",
            "headers": {"Authorization": "Bearer " + self._mcp_http_token}}}}, ensure_ascii=False, indent=2)
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(value)
            self._set_mcp_message("已复制 HTTP 客户端配置，包含本次启动的临时 Bearer 令牌。")

    @staticmethod
    def _mcp_python() -> str:
        executable = Path(sys.executable)
        if executable.name.lower() == "pythonw.exe":
            candidate = executable.with_name("python.exe")
            if candidate.is_file():
                return str(candidate)
            raise ValueError("stdio/HTTP MCP 需要同环境的 python.exe，以便保留通信管道")
        return str(executable)

    def _set_mcp_message(self, message: str) -> None:
        self._mcp_message = message
        self.mcpChanged.emit()

    def _persist_mcp_servers(self, servers: list[dict]) -> None:
        # Save only this section; keep unsaved font/model fields in this window.
        persisted = load_ui_settings()
        persisted["assistant_mcp_servers"] = servers
        saved = save_ui_settings(persisted)
        self._settings["assistant_mcp_servers"] = saved["assistant_mcp_servers"]
        self._settings["assistant_mcp_seed_version"] = saved["assistant_mcp_seed_version"]
        if self._mcp_probe_id:
            # Invalidate an obsolete probe; its late success must not describe
            # the replacement target or a permission change.
            self._mcp_probe_cancel.set()
        self.mcpChanged.emit()
        self.assistantSettingsSaved.emit()

    @Slot("QVariantMap", str, result=bool)
    def saveMcpServer(self, values: dict, secret: str = "") -> bool:
        try:
            values = dict(values)
            values["args"] = json.loads(str(values.pop("args_json", "[]")))
            rows = deepcopy(self._settings["assistant_mcp_servers"])
            previous = next((row for row in rows if row["id"] == values.get("id")), None)
            if not previous and len(rows) >= MAX_SERVERS:
                raise ValueError(f"最多配置 {MAX_SERVERS} 个 MCP 服务")
            values["credential_revision"] = previous["credential_revision"] if previous else ""
            if previous:
                values["preset_id"] = previous.get("preset_id", "")
            row = validate_server(values, new=True)
            if secret:
                if not row["credential_field"]:
                    raise ValueError("保存凭据前请填写环境变量名或 HTTP 头名称")
                save_mcp_secret(row["id"], secret)
                row["credential_revision"] = uuid.uuid4().hex
            rows = [row if item["id"] == row["id"] else item for item in rows]
            if previous is None:
                rows.append(row)
            self._persist_mcp_servers(rows)
            self._mcp_statuses.pop(row["id"], None)
            self._set_mcp_message("MCP 服务已保存；下一轮助手请求使用新配置。")
            return True
        except (ValueError, OSError, TypeError) as exc:
            self._set_mcp_message(f"MCP 保存失败：{exc}")
            return False

    @Slot(str, bool, bool, result=bool)
    def setMcpPermissions(self, identifier: str, enabled: bool, allow_tools: bool) -> bool:
        try:
            rows = deepcopy(self._settings["assistant_mcp_servers"])
            found = next((row for row in rows if row["id"] == identifier), None)
            if found is None:
                raise ValueError("未找到此服务")
            found.update(enabled=enabled, allow_tools=allow_tools)
            self._persist_mcp_servers(rows)
            self._set_mcp_message("MCP 授权已保存；下一轮助手请求生效。")
            return True
        except (ValueError, OSError) as exc:
            self._set_mcp_message(f"MCP 保存失败：{exc}")
            return False

    @Slot(str, result=bool)
    def deleteMcpServer(self, identifier: str) -> bool:
        try:
            row = next((item for item in self._settings["assistant_mcp_servers"] if item["id"] == identifier), None)
            if row is None:
                raise ValueError("未找到此服务")
            self._persist_mcp_servers([item for item in self._settings["assistant_mcp_servers"]
                                       if item["id"] != identifier])
            if identifier == self._mcp_probe_id:
                self._mcp_probe_cancel.set()
            self._mcp_statuses.pop(identifier, None)
            if row["credential_revision"]:
                try:
                    delete_mcp_secret(identifier)
                except OSError as exc:
                    self._set_mcp_message(f"服务已删除；系统凭据删除失败：{exc}")
                    return True
            self._set_mcp_message("MCP 服务和对应凭据已删除。")
            return True
        except (ValueError, OSError) as exc:
            self._set_mcp_message(f"MCP 删除失败：{exc}")
            return False

    @Slot(str, result=bool)
    def clearMcpCredential(self, identifier: str) -> bool:
        try:
            rows = deepcopy(self._settings["assistant_mcp_servers"])
            found = next((row for row in rows if row["id"] == identifier), None)
            if found is None:
                raise ValueError("请先保存此服务")
            delete_mcp_secret(identifier)
            found["credential_revision"] = ""
            self._persist_mcp_servers(rows)
            self._set_mcp_message("此 MCP 服务的系统凭据已删除。")
            return True
        except (ValueError, OSError) as exc:
            self._set_mcp_message(f"凭据删除失败：{exc}")
            return False

    @Slot(str)
    def checkMcpServer(self, identifier: str) -> None:
        if self._mcp_probe is not None:
            return
        config = next((deepcopy(row) for row in self._settings["assistant_mcp_servers"]
                       if row["id"] == identifier), None)
        if config is None:
            self._set_mcp_message("请先保存 MCP 服务。")
            return
        self._mcp_probe_cancel = Event()
        self._mcp_probe_id = identifier
        self._mcp_statuses[identifier] = "正在检查连接…"
        self._mcp_probe = self._mcp_executor.submit(run_probe, config, self._mcp_probe_cancel)
        self._mcp_poll.start()
        self._set_mcp_message("正在后台启动所配置程序或连接服务，并读取工具目录。")

    def _on_mcp_probe_ready(self) -> None:
        if self._mcp_probe is None or not self._mcp_probe.done():
            return
        try:
            result = self._mcp_probe.result()
            message = str(result["message"])
        except Exception:
            message = "连接检查失败；请检查服务配置和运行依赖。"
        if self._mcp_probe_cancel.is_set():
            message = "连接检查已取消或配置已改变；未执行工具。"
        self._mcp_statuses[self._mcp_probe_id] = message
        self._mcp_probe = None
        self._mcp_poll.stop()
        self._set_mcp_message(message)

    @Slot()
    def cancelMcpProbe(self) -> None:
        self._mcp_probe_cancel.set()
        self._set_mcp_message("正在取消连接检查并关闭连接…")

    @Slot(str)
    def copyMcpText(self, value: str) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(value)
            self._set_mcp_message("MCP 客户端配置已复制。")

    @Slot(int)
    def startOwnMcpHttp(self, port: int) -> None:
        if self.ownMcpHttpRunning:
            return
        if not 1024 <= port <= 65535:
            self._set_mcp_message("本机 HTTP 端口须为 1024–65535。")
            return
        self._clear_own_mcp_credential()
        credential, token = "local-http-" + uuid.uuid4().hex, secrets.token_urlsafe(32)
        try:
            save_mcp_secret(credential, token)
        except (OSError, ValueError) as exc:
            self._mcp_http_status = f"HTTP 启动失败：无法保存临时系统凭据：{exc}"
            self.mcpChanged.emit()
            return
        self._mcp_http_credential, self._mcp_http_token = credential, token
        self._mcp_http_port = port
        self._mcp_http_log = ""
        self._mcp_http_stopping = False
        self._mcp_http_status = f"正在启动本机 HTTP：127.0.0.1:{port}…"
        env = QProcessEnvironment()
        for name in (*SAFE_ENVIRONMENT, "COAT_UI_PID", "COAT_DEVELOPMENT", "COAT_RELEASE_BUILD",
                     "COAT_QA_START_LIMIT", "COAT_QA_START_COUNTER"):
            if name in os.environ:
                env.insert(name, os.environ[name])
        self._mcp_http.setProcessEnvironment(env)
        self._mcp_http.setWorkingDirectory(str(PROJECT_ROOT))
        try:
            python = self._mcp_python()
        except ValueError as exc:
            self._mcp_http_status = str(exc)
            self._clear_own_mcp_credential()
            self.mcpChanged.emit()
            return
        from coat_sim.process_entry import module_command
        command = module_command("coat_sim.mcp.desktop_endpoint", "--port", str(port),
                                 "--credential-id", credential, interpreter=python)
        started = self._mcp_http.start(command[0], command[1:])
        if not started:
            return
        self._mcp_http_timeout.start(15000)
        self.mcpChanged.emit()

    def _on_mcp_http_output(self) -> None:
        text = bytes(self._mcp_http.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._mcp_http_log = (self._mcp_http_log + text)[-8000:]
        if "Uvicorn running on " in self._mcp_http_log:
            self._mcp_http_timeout.stop()
            self._mcp_http_status = f"已启动：http://127.0.0.1:{self._mcp_http_port}/mcp（仅本机）"
            self.mcpChanged.emit()

    def _on_mcp_http_error(self, error) -> None:
        self._mcp_http_timeout.stop()
        self._mcp_http_status = "本机 HTTP 进程启动失败，请检查 Python 环境和 MCP 依赖。"
        self._clear_own_mcp_credential()
        self.mcpChanged.emit()

    def _on_mcp_http_finished(self, code: int, status) -> None:
        self._mcp_http_timeout.stop()
        self._mcp_http_kill_timer.stop()
        self._clear_own_mcp_credential()
        if self._mcp_http_stopping:
            self._mcp_http_status = "本机 HTTP 服务已停止。"
        elif "address already in use" in self._mcp_http_log.lower() or "10048" in self._mcp_http_log:
            self._mcp_http_status = "HTTP 启动失败：端口已被占用，请选择其他端口。"
        else:
            self._mcp_http_status = f"本机 HTTP 进程已退出（代码 {code}）；请检查服务依赖。"
        self.mcpChanged.emit()

    def _on_mcp_http_timeout(self) -> None:
        self.stopOwnMcpHttp()
        self._mcp_http_status = "HTTP 启动超时，已请求关闭进程。"
        self.mcpChanged.emit()

    @Slot()
    def stopOwnMcpHttp(self) -> None:
        if self.ownMcpHttpRunning:
            self._mcp_http_stopping = True
            self._mcp_http_status = "正在停止本机 HTTP 服务…"
            self._mcp_http.terminate()
            self._mcp_http_kill_timer.start(2000)
            self.mcpChanged.emit()

    @Slot()
    def closeMcpConnections(self) -> None:
        self._mcp_probe_cancel.set()
        if self.ownMcpHttpRunning:
            self._mcp_http_stopping = True
            self._mcp_http.kill()
            self._mcp_http_status = "本机 HTTP 服务已关闭。"
        self._clear_own_mcp_credential()
        self.mcpChanged.emit()

    def _clear_own_mcp_credential(self) -> None:
        identifier = self._mcp_http_credential
        self._mcp_http_credential = self._mcp_http_token = ""
        if identifier:
            try:
                delete_mcp_secret(identifier)
            except (OSError, ValueError):
                self._mcp_message = "临时 HTTP 凭据清理失败；服务已停止，原令牌不会复用。"

    def _shutdown_mcp(self) -> None:
        self.closeMcpConnections()
        self._mcp_executor.shutdown(wait=False, cancel_futures=True)
        if self.ownMcpHttpRunning:
            self._mcp_http.kill()
        self._clear_own_mcp_credential()


def main() -> int:
    """Allow ``python -m coat_sim.ui.settings`` as a standalone launcher."""
    import sys

    from coat_sim.ui.launcher import main as launch

    sys.argv[1:] = ["--module", "settings"]
    return int(launch())


if __name__ == "__main__":
    raise SystemExit(main())
