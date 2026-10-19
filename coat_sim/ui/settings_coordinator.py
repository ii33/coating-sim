"""Per-settings-window asynchronous model-probe coordination.

The bridge and its probe coordinator have an explicit composition relationship:
the bridge supplies draft, credential, status, and shared-bus services, while
this QObject owns the active probe run and its async resources.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event, Lock
from typing import Callable

from PySide6.QtCore import QObject, QTimer, QUrl
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from coat_sim.ui.model_probe import (
    INVALID_PROBE_VALUE, MODEL_REQUEST_LIMIT, REASONING_REQUEST_LIMIT,
    PROBE_OUTPUT_TOKENS, PROBE_REQUEST_TIMEOUT_MS, PROBE_TOTAL_TIMEOUT_MS,
    STATUS_TEXT, candidate_models, classify_reply, completion_body,
    metadata_efforts, model_records, normalize_api_url, output_parameter,
    output_parameter_rejected, reasoning_result, reported_usage,
    validates_field,
)
from coat_sim.ui.provider_catalog import models_endpoint_for_url


@dataclass(frozen=True)
class SettingsProbeDependencies:
    """Settings-module functions needed by the coordinator, passed explicitly."""

    validate_assistant_url: Callable[[str], str]
    local_anonymous_endpoint: Callable[[str], bool]
    read_api_key: Callable[..., str | None]
    provider_profile_for_url: Callable[[str], dict | None]
    network_manager_factory: Callable[[QObject], QObject]
    auto_probe_debounce_ms: int
    model_cache_retry_seconds: int
    max_model_reply_bytes: int


class SettingsProbeCoordinator(QObject):
    """Own one SettingsBridge's probe run, timers, network manager and budget."""

    def __init__(self, bridge, dependencies: SettingsProbeDependencies, parent=None,
                 network_manager=None):
        super().__init__(parent)
        self._bridge = bridge
        self._dependencies = dependencies
        self._available_models = []
        self._probe_model_records: list[dict] = []
        self._probe_models_received = False
        self._model_discovery_busy = False
        self._model_discovery_timed_out = False
        self._model_discovery_oversized = False
        self._model_discovery_message = ""
        self._network = network_manager or dependencies.network_manager_factory(self)
        self._model_reply: QNetworkReply | None = None
        self._model_timeout = QTimer(self)
        self._model_timeout.setSingleShot(True)
        self._model_timeout.timeout.connect(self._on_model_timeout)
        self._probe_overall_timeout = QTimer(self)
        self._probe_overall_timeout.setSingleShot(True)
        self._probe_overall_timeout.timeout.connect(self._on_probe_budget_timeout)
        self._probe_kind = ""
        self._probe_queue = []
        self._probe_results = []
        self._probe_usage = {}
        self._probe_secret = ""
        self._probe_base = ""
        self._probe_original_url = ""
        self._probe_model = ""
        self._probe_task = {}
        self._probe_efforts = []
        self._probe_thinking = []
        self._probe_output_parameter = "max_tokens"
        self._probe_request_id = ""
        self._probe_fingerprint = None
        self._probe_credential_generation = 0
        self._probe_automatic = False
        self._probe_failed = False
        self._probe_candidate_fallback = False
        self._probe_closed = False
        self._auto_paused_until = 0.0
        self._auto_probe = QTimer(self)
        self._auto_probe.setSingleShot(True)
        self._auto_probe.timeout.connect(self._auto_discover)
        self._budget_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="model-probe-budget")
        self._budget_store = None
        self._budget_lock = Lock()
        self._budget_reservations = {}
        self._probe_cancel_event = Event()
        self._budget_jobs = []
        self._budget_poll = QTimer(self)
        self._budget_poll.timeout.connect(self._on_probe_budget_ready)

    @property
    def assistantApiUrl(self):
        return self._bridge.assistantApiUrl

    @property
    def assistantModel(self):
        return self._bridge.assistantModel

    @property
    def assistantEnabled(self):
        return self._bridge.assistantEnabled

    @property
    def modelDiscoveryBusy(self):
        return self._model_discovery_busy or bool(self._budget_jobs)

    @property
    def modelDiscoveryKind(self):
        return self._probe_kind if self._model_discovery_busy else ""

    @property
    def _discovery_bus(self):
        return self._bridge._discovery_bus

    def assistant_native_reasoning_options(self) -> list[str]:
        """Return advertised native/thinking options for the current draft."""
        bridge = self._bridge
        bus = self._discovery_bus
        if bridge._assistant_settings_requested:
            result = bus.reasoning_for_fingerprint(
                self.assistantApiUrl, self.assistantModel,
                bridge._assistant_credential_fingerprint)
        else:
            result = bus.reasoning_for(self.assistantApiUrl, self.assistantModel)
        result = result or {}
        return list(result.get("native_options", [])) + [
            "thinking:" + value for value in result.get("thinking_options", [])
        ]

    def _set_model_discovery_message(self, message: str) -> None:
        self._model_discovery_message = message
        self._bridge.modelDiscoveryChanged.emit()

    def _set_key_message(self, message: str) -> None:
        self._bridge._set_key_message(message)

    def reset_model_discovery(self) -> None:
        if self._model_discovery_busy:
            self._finish_probe("取消了探测。", cancelled=True)
        self._model_discovery_message = ""
        self._available_models = []
        self._bridge.modelDiscoveryChanged.emit()

    def cancel_model_probe(self) -> None:
        self._auto_paused_until = time.time() + self._dependencies.model_cache_retry_seconds
        self._auto_probe.stop()
        if self._model_discovery_busy:
            self._finish_probe("已取消；保留本次已确认结果。", cancelled=True)



    def _schedule_auto_probe(self) -> None:
        self._auto_probe.stop()
        if self._probe_closed or not self.assistantEnabled or time.time() < self._auto_paused_until:
            return
        api_url = self.assistantApiUrl
        if not self._bridge._assistant_key_status_ready or self._bridge._assistant_credential_api_url != api_url:
            self._bridge._refresh_bound_key()
            return
        if not self._bridge._has_assistant_key or not self._bridge._assistant_credential_fingerprint:
            return
        profile = self._dependencies.provider_profile_for_url(api_url)
        if profile is not None and not profile.get("default_auto_probe", False):
            return
        self._auto_probe.start(self._dependencies.auto_probe_debounce_ms)

    def _auto_discover(self) -> None:
        if self._probe_closed or not self.assistantEnabled or time.time() < self._auto_paused_until:
            return
        if not self._bridge._assistant_key_status_ready or self._bridge._assistant_credential_api_url != self.assistantApiUrl:
            self._bridge._refresh_bound_key()
            return
        if not self._bridge._has_assistant_key or not self._bridge._assistant_credential_fingerprint:
            return
        profile = self._dependencies.provider_profile_for_url(self.assistantApiUrl)
        if profile is not None and not profile.get("default_auto_probe", False):
            return
        if self.modelDiscoveryBusy:
            self._auto_probe.start(1000)
            return
        try:
            self._dependencies.validate_assistant_url(self.assistantApiUrl)
            fingerprint = self._bridge._assistant_credential_fingerprint
            models = self._discovery_bus.models_for_fingerprint(
                self.assistantApiUrl, fingerprint, reload_cache=False)
        except ValueError:
            return
        self._available_models = models
        self._bridge.modelDiscoveryChanged.emit()
        if not self._discovery_bus.models_fresh_for_fingerprint(
                self.assistantApiUrl, fingerprint, reload_cache=False):
            # A different window or process may have rotated this endpoint's
            # key since the bootstrap snapshot. Refresh it off the GUI thread
            # before sending this automatic request.
            self._bridge._refresh_bound_key(force=True, reason="auto_probe")

    def _start_probe(self, kind: str, automatic: bool = False) -> bool:
        if self.modelDiscoveryBusy:
            return False
        profile = self._dependencies.provider_profile_for_url(self.assistantApiUrl)
        if profile is not None and not profile.get("probe_allowed"):
            self._set_model_discovery_message(
                "此目录端点不支持在本应用中探测；可手动填写模型，但不会发送请求。"
            )
            return False
        try:
            base = normalize_api_url(self._dependencies.validate_assistant_url(self.assistantApiUrl))
        except ValueError:
            self._set_model_discovery_message("请求未发送：请先填写有效的服务商地址。")
            return False
        if automatic and (not self._bridge._assistant_key_status_ready
                          or self._bridge._assistant_credential_api_url != self.assistantApiUrl
                          or not self._bridge._has_assistant_key):
            self._bridge._refresh_bound_key()
            return False
        try:
            secret = (self._bridge._assistant_credential_secret if automatic else
                      self._dependencies.read_api_key(self.assistantApiUrl, strict=True))
        except OSError:
            if not self._dependencies.local_anonymous_endpoint(base):
                self._set_key_message("系统安全凭据库不可用或读取失败。")
                self._set_model_discovery_message("请求未发送：无法读取系统安全凭据库；请检查凭据服务。")
                return False
            secret = None
        if not automatic:
            fingerprint = self._discovery_bus.credential_fingerprint(base, secret or "")
            self._bridge._adopt_probe_credentials(self.assistantApiUrl, secret or "", fingerprint)
        if not secret and not self._dependencies.local_anonymous_endpoint(base):
            self._set_key_message("当前地址未保存密钥；请先保存密钥。")
            self._set_model_discovery_message("请求未发送：当前远程地址没有可用密钥；不自动使用匿名请求。")
            return False
        if kind == "reasoning" and (automatic or not self.assistantModel):
            self._set_model_discovery_message("推理测试需手动发起，并先选择或填写当前模型。")
            return False
        if not self._discovery_bus.claim_probe(base, self):
            self._set_model_discovery_message("此服务器正在另一窗口中探测，完成后共享结果。")
            if automatic:
                self._auto_probe.start(2000)
            return False
        self._probe_closed = False
        self._probe_credential_generation = self._bridge._credential_generation
        self._probe_fingerprint = (self._bridge._assistant_credential_fingerprint if automatic else
                                   self._discovery_bus.credential_fingerprint(base, secret or ""))
        self._probe_automatic = automatic
        self._probe_kind = kind
        self._probe_failed = False
        self._probe_candidate_fallback = False
        self._probe_request_id = uuid.uuid4().hex
        self._probe_cancel_event = Event()
        self._probe_base = base
        self._probe_original_url = self.assistantApiUrl
        self._probe_model = self.assistantModel
        self._probe_secret = secret or ""
        self._probe_queue = []
        self._probe_results = []
        self._probe_usage = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "unreported_requests": 0}
        self._probe_efforts = []
        self._probe_thinking = []
        self._probe_output_parameter = output_parameter(base, self._probe_model)
        if kind == "reasoning":
            prior = self._discovery_bus.reasoning_for(self._probe_original_url, self._probe_model) or {}
            known_parameter = prior.get("output_parameter")
            if known_parameter in ("max_tokens", "max_completion_tokens"):
                self._probe_output_parameter = known_parameter
        self._model_discovery_busy = True
        self._probe_overall_timeout.start(PROBE_TOTAL_TIMEOUT_MS)
        self._bridge.modelDiscoveryChanged.emit()
        return True

    def _begin_models_probe(self, automatic: bool = False) -> None:
        if not self._start_probe("models", automatic):
            return
        # Keep the last usable snapshot visible until a replacement succeeds.
        self._probe_model_records = []
        self._probe_models_received = False
        metadata_url = models_endpoint_for_url(self._probe_original_url, self._probe_base)
        self._probe_queue = [{"kind": "metadata", "url": metadata_url}]
        self._next_probe_request()

    def _queue_model_candidate_fallback(self, detail: str) -> None:
        if self._probe_automatic:
            self._record_probe("models endpoint", "unknown", None,
                               "Automatic discovery uses GET /models only; candidate POSTs are manual-only.")
            self._finish_probe(detail + " Automatic discovery stopped after the model-list response; "
                               "use Refresh models for candidate checks.")
            return
        candidates = candidate_models(self._probe_base, self._probe_model)
        if not candidates:
            self._record_probe("models endpoint", "unknown", None,
                               "No candidates are available for a manual check.")
            self._finish_probe(detail + " No candidate requests were sent; enter a model ID manually.")
            return
        self._probe_candidate_fallback = True
        self._record_probe(
            "models endpoint", "unknown", None,
            f"{detail} Checking {len(candidates)} candidates may incur provider charges.",
        )
        self._probe_queue = [
            {"kind": "model", "model": name,
             "output_parameter": output_parameter(self._probe_base, name)}
            for name in candidates
        ]
        self._next_probe_request()

    def _begin_reasoning_probe(self, automatic: bool = False) -> None:
        if not self._start_probe("reasoning", automatic):
            return
        self._discovery_bus.publish_reasoning(self._probe_original_url, self._probe_model, {
            "api_url": self._probe_base, "model": self._probe_model,
            "verified": False, "reasoning_probed": True, "evidence": "unconfirmed", "options": ["auto"],
            "strength_map": {}, "native_options": [],
            "output_parameter": self._probe_output_parameter,
        }, completed=False, expected_fingerprint=self._probe_fingerprint)
        self._probe_queue = [{"kind": "effort_control"}, {"kind": "thinking_control"}]
        self._next_probe_request()

    def _release_unsent_probe_reservations(self) -> None:
        with self._budget_lock:
            unsent = [(key, row) for key, row in self._budget_reservations.items() if not row["posted"]]
            for key, _ in unsent:
                self._budget_reservations.pop(key, None)
        for key, row in unsent:
            self._submit_probe_budget(self._settle_probe_budget, key, row["session"], row["model"], None, None, False)

    def closeModelProbe(self) -> None:
        self._probe_closed = True
        self._auto_probe.stop()
        if self._model_discovery_busy:
            self._finish_probe("窗口已关闭，探测已取消。", cancelled=True)
        self._probe_cancel_event.set()
        self._release_unsent_probe_reservations()

    def _shutdown_probe_budget(self) -> None:
            # Called while the application still has a live Qt event loop.
        self.closeModelProbe()
        self._budget_executor.shutdown(wait=False, cancel_futures=False)

    def _cleanup_destroyed_probe(self) -> None:
            # QObject destruction cannot safely use Qt properties or emit signals.
        self._probe_cancel_event.set()
        if self._bridge._discovery_bus_value is not None:
            self._bridge._discovery_bus_value.release_probe(self._probe_original_url, self)
        with self._budget_lock:
            rows = list(self._budget_reservations.items())
            self._budget_reservations.clear()
        for key, row in rows:
            try:
                self._budget_executor.submit(self._settle_probe_budget, key, row["session"],
                                             row["model"], None, None, row["posted"])
            except RuntimeError:
                pass

    def _submit_probe_budget(self, operation, *args, callback=None) -> None:
        self._budget_jobs.append((self._budget_executor.submit(operation, *args), callback, self._probe_request_id))
        if not self._budget_poll.isActive():
            self._budget_poll.start(30)
        self._bridge.modelDiscoveryChanged.emit()

    def _on_probe_budget_ready(self) -> None:
        jobs, self._budget_jobs = self._budget_jobs, []
        for future, callback, generation in jobs:
            if not future.done():
                self._budget_jobs.append((future, callback, generation))
                continue
            try:
                result = future.result()
                if callback is not None:
                    callback(result)
            except Exception as exc:
                if generation != self._probe_request_id:
                    continue
                if self._model_discovery_busy:
                    self._finish_probe("探测配额记账失败，已停止请求；请检查本机用量存储。")
                else:
                    self._set_model_discovery_message("探测配额记账失败；请检查本机用量存储。")
        if not self._budget_jobs:
            self._budget_poll.stop()
        self._bridge.modelDiscoveryChanged.emit()

    def _probe_store(self):
            # Only called by this bridge's single budget thread.
        if self._budget_store is None:
            from coat_sim.agent.storage import AssistantStore
            self._budget_store = AssistantStore()
        return self._budget_store

    def _reserve_probe_budget(self, config, model, cancel_event):
        if cancel_event.is_set():
            return None
        config = dict(config)
        session = "probe:" + config["reservation_request"]
        config.update({"reservation_owner": os.getpid(), "reservation_session": session,
                       "reservation_model": model})
        reservation, output_limit = self._probe_store().reserve_request(config, 100, PROBE_OUTPUT_TOKENS, minimum_output=1)
        with self._budget_lock:
            cancelled = cancel_event.is_set()
            if not cancelled:
                self._budget_reservations[reservation] = {"session": session, "model": model, "posted": False}
        if cancelled:
            self._probe_store().abandon_request(reservation, charge_estimate=False)
            return None
        return reservation, output_limit

    def _settle_probe_budget(self, reservation, session, model, usage, status, sent=True):
        store = self._probe_store()
        try:
            if usage is not None and not usage.get("unreported_requests"):
                store.settle_request(reservation, session, model, usage["input_tokens"], usage["output_tokens"])
            elif sent and (status is None or 200 <= status < 300 or status >= 500):
                store.abandon_request(reservation, charge_estimate=True, session=session, model=model)
            else:
                # A definite rejected request with no reported usage is not charged.
                store.abandon_request(reservation, charge_estimate=False, session=session, model=model)
        finally:
            with self._budget_lock:
                self._budget_reservations.pop(reservation, None)

    def _probe_credentials_unchanged(self) -> bool:
        if (self._probe_credential_generation != self._bridge._credential_generation
                or self._probe_original_url != self.assistantApiUrl):
            return False
        if self._probe_automatic:
            return (self._bridge._assistant_credential_api_url == self.assistantApiUrl
                    and self._bridge._assistant_credential_fingerprint == self._probe_fingerprint
                    and self._bridge._assistant_key_status_ready and self._bridge._has_assistant_key)
        return self._discovery_bus.credential_fingerprint(self._probe_base) == self._probe_fingerprint

    def _next_probe_request(self) -> None:
        if self._model_discovery_busy and not self._probe_credentials_unchanged():
            self._finish_probe("此服务器密钥已变更，旧探测已停止。", cancelled=True)
            return
        if not self._model_discovery_busy:
            return
        limit = MODEL_REQUEST_LIMIT if self._probe_kind == "models" else REASONING_REQUEST_LIMIT
        if not self._probe_queue or self._probe_usage["requests"] >= limit:
            self._finish_probe("探测完成。" if not self._probe_queue else "已达到请求数上限；保留已确认结果。")
            return
        if self._probe_efforts:
            self._probe_queue = [task for task in self._probe_queue if task.get("kind") not in ("thinking_control", "thinking")]
        if not self._probe_queue:
            self._finish_probe("探测完成。")
            return
        task = self._probe_queue.pop(0)
        if task["kind"] == "metadata":
            self._send_probe_request(task)
            return
        request_id = self._probe_request_id
        config = dict(self._bridge._settings)
        config["reservation_request"] = request_id
        model = task.get("model", self._probe_model)
        self._set_model_discovery_message("正在检查探测 Token 配额…")
        cancel_event = self._probe_cancel_event
        def reserved(result):
            if result is None:
                return
            reservation, output_limit = result
            if self._model_discovery_busy and request_id == self._probe_request_id and not cancel_event.is_set():
                with self._budget_lock:
                    row = self._budget_reservations.get(reservation)
                    if row is None:
                        return
                    row["posted"] = True
                task["_reservation"] = reservation
                task["_session"] = "probe:" + request_id
                self._send_probe_request(task, output_limit)
            else:
                self._submit_probe_budget(self._settle_probe_budget, reservation, "probe:" + request_id, model, None, None, False)
        self._submit_probe_budget(self._reserve_probe_budget, config, model, cancel_event, callback=reserved)

    def _send_probe_request(self, task: dict, output_limit: int = PROBE_OUTPUT_TOKENS) -> None:
        self._probe_task = task
        if not self._probe_credentials_unchanged():
            reservation = task.pop("_reservation", None)
            if reservation:
                self._submit_probe_budget(self._settle_probe_budget, reservation, task["_session"],
                                          task.get("model", self._probe_model), None, None, False)
            self._finish_probe("此服务器密钥已变更，旧探测已停止。", cancelled=True)
            return
        kind = task["kind"]
        fields = {}
        model = task.get("model", self._probe_model)
        parameter = task.get("output_parameter", self._probe_output_parameter)
        if kind == "effort_control":
            fields = {"reasoning_effort": INVALID_PROBE_VALUE}
        elif kind == "effort":
            fields = {"reasoning_effort": task["value"]}
        elif kind == "thinking_control":
            fields = {"thinking": {"type": INVALID_PROBE_VALUE}}
        elif kind == "thinking":
            fields = {"thinking": {"type": task["value"]}}
        url = (task.get("url") or (self._probe_base + "/models")) if kind == "metadata" else self._probe_base + "/chat/completions"
        request = QNetworkRequest(QUrl(url))
        request.setAttribute(QNetworkRequest.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        request.setRawHeader(b"Accept", b"application/json")
        if self._probe_secret:
            request.setRawHeader(b"Authorization", b"Bearer " + self._probe_secret.encode("utf-8"))
        if kind == "metadata":
            reply = self._network.get(request)
            description = "读取模型列表"
        else:
            request.setRawHeader(b"Content-Type", b"application/json")
            self._probe_usage["requests"] += 1
            task["output_parameter"] = parameter
            body = completion_body(model, parameter, fields)
            body[parameter] = output_limit
            reply = self._network.post(request, json.dumps(body, ensure_ascii=False).encode("utf-8"))
            description = ("验证 " + model if kind == "model" else
                           "验证非法值对照" if kind.endswith("_control") else
                           "验证原生档位 " + task["value"])
        self._model_reply = reply
        self._model_discovery_timed_out = False
        self._model_discovery_oversized = False
        reply.finished.connect(lambda: self._on_models_finished(reply))
        reply.readyRead.connect(lambda: self._on_models_ready_read(reply))
        self._model_timeout.start(PROBE_REQUEST_TIMEOUT_MS)
        self._set_model_discovery_message(description + f"…（总时限 {PROBE_TOTAL_TIMEOUT_MS // 1000} 秒，可取消）")

    def _on_model_timeout(self) -> None:
        if self._model_reply is not None:
            self._model_discovery_timed_out = True
            self._model_reply.abort()

    def _on_probe_budget_timeout(self) -> None:
        if self._model_discovery_busy:
            self._finish_probe("已达到总时限；保留本次已确认结果。", cancelled=True)

    def _on_models_ready_read(self, reply: QNetworkReply) -> None:
        if reply is self._model_reply and reply.bytesAvailable() > self._dependencies.max_model_reply_bytes:
            self._model_discovery_oversized = True
            reply.abort()

    def _record_probe(self, label: str, category: str, status: int | None = None, detail: str = "") -> None:
        self._probe_results.append({"label": label, "status": category,
                                   "status_text": STATUS_TEXT.get(category, "未确认"),
                                   "http_status": status or 0, "detail": detail})
        self._bridge.modelDiscoveryChanged.emit()

    def _on_models_finished(self, reply: QNetworkReply) -> None:
        if reply is not self._model_reply:
            reply.deleteLater()
            return
        self._model_reply = None
        self._model_timeout.stop()
        task = self._probe_task
        kind = task["kind"]
        raw_status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        status = raw_status if isinstance(raw_status, int) else None
        payload = {}
        try:
            data = bytes(reply.readAll())
            if len(data) <= self._dependencies.max_model_reply_bytes and not self._model_discovery_oversized:
                try:
                    payload = json.loads(data.decode("utf-8"))
                except (ValueError, UnicodeError):
                    pass
            if kind != "metadata":
                usage = reported_usage(payload)
                for key, value in usage.items():
                    self._probe_usage[key] += value
                reservation = task.pop("_reservation", None)
                if reservation:
                    self._submit_probe_budget(self._settle_probe_budget, reservation, task["_session"],
                                              task.get("model", self._probe_model), usage, status)
            network_failed = (reply.error() != QNetworkReply.NetworkError.NoError
                              or self._model_discovery_oversized)
            category = classify_reply(status, payload, timeout=self._model_discovery_timed_out,
                                      network_error=network_failed)
            if status in (401, 403, 429) or category in ("auth", "rate_limit"):
                self._probe_failed = True
                self._record_probe(task.get("model", self._probe_model) or "模型列表", category, status)
                message = {
                    401: "远程服务返回 HTTP 401：本次请求鉴权未通过；请核对已保存密钥、服务地址及该接口的鉴权方式。",
                    403: "远程服务返回 HTTP 403：当前请求无权限，请检查套餐与接口授权。",
                    429: "远程服务返回 HTTP 429：限流或额度不足，请检查服务商用量与限制。",
                }.get(status, "远程服务拒绝鉴权或触发用量限制。")
                self._finish_probe(message + " 已停止后续请求；仍可手动填写模型。")
                return
            if self._probe_kind == "reasoning" and (category in ("endpoint", "unavailable_model") or (status is not None and status >= 500)):
                self._probe_failed = True
                self._record_probe(self._probe_model, category, status)
                self._finish_probe("当前模型、聊天接口或服务器不可用，已停止后续档位探测。")
                return
            if network_failed or status is None or self._model_discovery_timed_out or (status is not None and status >= 500):
                self._probe_failed = True
            if kind == "metadata":
                records = (model_records(payload) if status is not None and 200 <= status < 300
                           and not network_failed and not self._model_discovery_timed_out else [])
                if records:
                    if not self._probe_credentials_unchanged():
                        self._probe_failed = True
                        self._finish_probe("端点或凭据已变化；丢弃过期刷新结果。", cancelled=True)
                        return
                    updated = self._bridge._apply_discovered_model_metadata(self._probe_original_url, records)
                    self._probe_model_records = records
                    self._probe_models_received = True
                    self._available_models = records
                    detail = (f"已更新 {updated} 个模型的服务端声明容量；能力仍未验证。" if updated else
                              "未返回已接入的明确容量字段，保留文档预置；能力仍未验证。")
                    self._finish_probe(f"服务器返回 {len(records)} 个模型；点击选择。" + detail)
                    return
                if network_failed or status is None or status >= 500 or self._model_discovery_timed_out:
                    self._record_probe("模型列表接口", "timeout" if self._model_discovery_timed_out else "unknown", status)
                    self._finish_probe("模型列表服务连接失败；未继续发送枚举请求。")
                    return
                self._queue_model_candidate_fallback(
                    "模型列表接口未返回可用列表；候选按当前精确端点目录生成。"
                )
                return
            else:
                parameter = task["output_parameter"]
                if status in (400, 422) and output_parameter_rejected(payload, parameter) and not task.get("retried_output"):
                    alternate = "max_tokens" if parameter == "max_completion_tokens" else "max_completion_tokens"
                    self._probe_queue.insert(0, {**task, "output_parameter": alternate, "retried_output": True})
                    if self._probe_kind == "reasoning":
                        self._probe_output_parameter = alternate
                    self._record_probe("输出上限兼容", "unknown", status, "使用另一种输出上限字段重试一次")
                elif kind == "model":
                    if category == "ok":
                        self._probe_model_records.append({"id": task["model"], "metadata": {},
                                                       "output_parameter": parameter, "evidence": "completion_request"})
                        self._available_models = list(self._probe_model_records)
                        self._discovery_bus.publish(self._probe_original_url, self._probe_model_records, completed=False, expected_fingerprint=self._probe_fingerprint)
                        prior = self._discovery_bus.reasoning_for(self._probe_original_url, task["model"])
                        if not isinstance(prior, dict) or prior.get("verified") is not True:
                            self._discovery_bus.publish_reasoning(self._probe_original_url, task["model"], {
                                "api_url": self._probe_base, "model": task["model"], "verified": False,
                                "reasoning_probed": False, "evidence": "unconfirmed", "options": ["auto"],
                                "strength_map": {}, "native_options": [], "output_parameter": parameter,
                            }, expected_fingerprint=self._probe_fingerprint)
                    if category == "unsupported_parameter":
                        category = "unknown"
                    self._record_probe(task["model"], category, status)
                    if category == "endpoint" or (status is not None and status >= 500):
                        self._finish_probe("聊天接口不可用，已停止后续枚举。")
                        return
                elif kind in ("effort_control", "thinking_control"):
                    field = "reasoning_effort" if kind == "effort_control" else "thinking.type"
                    validated = validates_field(status, payload, field)
                    self._record_probe(field + " 非法值对照", "ok" if validated else "unknown", status,
                                       "服务器明确拒绝该字段的非法值" if validated else "未证明服务器验证此字段，跳过该字段档位枚举")
                    if validated:
                        tasks = ([{"kind": "effort", "value": value} for value in metadata_efforts(self._available_models, self._probe_model)]
                                 if kind == "effort_control" else [{"kind": "thinking", "value": value} for value in ("enabled", "disabled")])
                        self._probe_queue[0:0] = tasks
                else:
                    label = ("reasoning_effort=" if kind == "effort" else "thinking.type=") + task["value"]
                    self._record_probe(label, category, status,
                                       "请求被接受；不证明推理行为发生变化" if category == "ok" else "")
                    if category == "ok":
                        self._probe_output_parameter = parameter
                        (self._probe_efforts if kind == "effort" else self._probe_thinking).append(task["value"])
                    if status is not None and status >= 500:
                        self._finish_probe("服务器异常，已停止后续探测。")
                        return
        finally:
            reply.deleteLater()
        self._next_probe_request()

    def _finish_probe(self, message: str, cancelled: bool = False) -> None:
        reply, self._model_reply = self._model_reply, None
        self._model_timeout.stop()
        self._probe_overall_timeout.stop()
        self._probe_cancel_event.set()
        self._release_unsent_probe_reservations()
        if reply is not None:
            task = self._probe_task
            reservation = task.pop("_reservation", None)
            if reservation:
                self._probe_usage["unreported_requests"] += 1
                self._submit_probe_budget(self._settle_probe_budget, reservation, task["_session"],
                                          task.get("model", self._probe_model), None, None)
            reply.abort()
            reply.deleteLater()
        incomplete = cancelled or self._probe_failed or bool(self._probe_queue)
        if self._probe_kind == "reasoning":
            result = reasoning_result(self._probe_base, self._probe_model, self._probe_efforts,
                                      self._probe_thinking, self._probe_usage, self._probe_results,
                                      self._probe_output_parameter)
            result["incomplete"] = incomplete
            self._discovery_bus.publish_reasoning(self._probe_original_url, self._probe_model, result,
                                                  cancelled=incomplete, expected_fingerprint=self._probe_fingerprint)
            if result["verified"]:
                message += " 已确认原生档位：" + "、".join(result["options"][1:]) + "。"
            else:
                message += " 固定思考档位未确认，保留自动。"
        elif self._probe_kind == "models":
            received = self._probe_models_received or bool(self._probe_model_records)
            self._discovery_bus.publish(self._probe_original_url, self._probe_model_records,
                                        cancelled=incomplete or not received,
                                        expected_fingerprint=self._probe_fingerprint)
            if not received:
                message += " 本次刷新未获得可用结果；保留离线预置与上次列表，手动配置未更改。"
            if self._probe_candidate_fallback:
                confirmed = sum(row.get("evidence") == "completion_request" for row in self._available_models)
                sent = self._probe_usage["requests"]
                if confirmed:
                    message += f" 候选探测确认 {confirmed} 个模型接受了短聊天请求；已发送 {sent} 次请求，可能计费。"
                elif sent:
                    message += f" 已发送 {sent} 次候选短聊天请求，但没有模型确认接受；未知或拒绝的候选未加入列表，可能计费。可保留手填模型并在稍后重试。"
                else:
                    message += " 模型列表接口不可用；候选探测未发送请求，手填模型仍保留。"
            else:
                message += f" 本次列表返回 {len(self._available_models)} 个模型；未验证聊天可用性。"
        self._probe_secret = ""
        self._probe_queue = []
        self._model_discovery_busy = False
        self._discovery_bus.release_probe(self._probe_original_url, self)
        self._set_model_discovery_message(message)
        self._bridge.assistantStateChanged.emit()
        self._schedule_auto_probe()
        self._probe_automatic = False
