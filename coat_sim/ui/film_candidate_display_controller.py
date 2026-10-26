"""Per-window, display-only state and bounded asynchronous preview controls.

No optical calculation, result application, saving, or optimizer acceptance lives
here. A run freezes score preferences; runtime controls only select its preview.
"""
from __future__ import annotations

from dataclasses import asdict

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import math
import re
from threading import Lock
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QObject, Property, Qt, Signal, Slot

from coat_sim.core.film_candidate_display import score_config_to_payload
from coat_sim.scheduler.film_display import prepare_display_configuration, validate_live_display_packet


class FilmCandidateDisplayController(QObject):
    changed = Signal()
    axisChanged = Signal()
    _wake = Signal()
    _controlStatus = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="film-display-control")
        self._wake.connect(self._drain, Qt.ConnectionType.QueuedConnection)
        self._controlStatus.connect(self._on_control_status, Qt.ConnectionType.QueuedConnection)
        self._enabled = False
        self._score_draft = score_config_to_payload()
        self._frozen_score: dict[str, float] = {}
        self._selection: dict[str, Any] = {"objective_index": 0, "angle_deg": 0.0, "metric": "R"}
        self._configuration: dict[str, Any] = {}
        self._objectives: tuple[Any, ...] = ()
        self._packet: dict[str, Any] = {}
        self._attempt = None
        self._binding = None
        self._pending_packet = None
        self._wake_pending = False
        self._control_pending = None
        self._control_inflight = False
        self._minimum_control_sequence = 0
        self._last_sequence = 0
        self._terminal_received = False
        self._running = False
        self._closed = False
        self._source_current = lambda: True
        self._error = ""
        self._axis_auto = True
        self._axis_minimum, self._axis_maximum = 0., 1.
        self._axis_manual_minimum, self._axis_manual_maximum = 0., 1.
        self._axis_shrink_count = 0
        self._axis_initialized = False
        self._axis_error = ""

    @Property(bool, notify=axisChanged)
    def yAxisAuto(self):
        return self._axis_auto

    @Property(float, notify=axisChanged)
    def yAxisMinimum(self):
        return self._axis_minimum

    @Property(float, notify=axisChanged)
    def yAxisMaximum(self):
        return self._axis_maximum

    @Property(str, notify=axisChanged)
    def yAxisManualMinimumPercent(self):
        return f"{100 * self._axis_manual_minimum:.6g}"

    @Property(str, notify=axisChanged)
    def yAxisManualMaximumPercent(self):
        return f"{100 * self._axis_manual_maximum:.6g}"

    @Property(str, notify=axisChanged)
    def axisError(self):
        return self._axis_error

    @Slot(bool)
    def setYAxisAuto(self, automatic):
        self._axis_auto = bool(automatic)
        self._axis_shrink_count = 0
        self._axis_initialized = False
        if not self._axis_auto:
            self._axis_minimum, self._axis_maximum = self._axis_manual_minimum, self._axis_manual_maximum
        self.axisChanged.emit()

    @Slot(str, str, result=bool)
    def setYAxisBoundsPercent(self, minimum, maximum):
        try:
            lo, hi = float(minimum) / 100., float(maximum) / 100.
            if not math.isfinite(lo + hi) or lo < 0 or hi <= lo:
                raise ValueError('纵轴范围须为有限百分比，且 0 ≤ 下限 < 上限。')
        except (TypeError, ValueError, OverflowError) as exc:
            self._axis_error = str(exc)
            self.axisChanged.emit()
            return False
        self._axis_manual_minimum, self._axis_manual_maximum = lo, hi
        self._axis_error = ""
        if not self._axis_auto:
            self._axis_minimum, self._axis_maximum = lo, hi
        self.axisChanged.emit()
        return True

    @Slot('QVariantMap')
    def updatePlotYAxis(self, data):
        if not self._axis_auto or not isinstance(data, dict):
            return
        values = []
        if data.get('live'):
            for role in ('current', 'best'):
                if isinstance(data.get(role), dict):
                    values.extend(data[role].get('actual', ()))
                    if isinstance(data[role].get('maximum'), (int, float)):
                        values.append(data[role]['maximum'])
        else:
            for role in ('baseline', 'candidate'):
                values.extend(data.get(role) or ())
        values = [float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(v)]
        if not values:
            return  # Preserve the preceding range while a new selection loads.
        values.extend(float(data[k]) for k in ('target_value', 'lower_bound', 'upper_bound')
                      if isinstance(data.get(k), (int, float)) and math.isfinite(data[k]))
        peak = max(max(values), 1e-6)
        exponent = 10. ** math.floor(math.log10(peak))
        ceiling = next((step * exponent for step in (1., 2., 2.5, 5., 10.)
                        if step * exponent >= 1.08 * peak), 10. * exponent)
        if peak <= 1. + 64 * math.ulp(1.) and ceiling > 1.:
            ceiling = 1.
        if self._axis_initialized and ceiling < self._axis_maximum and peak < .45 * self._axis_maximum:
            self._axis_shrink_count += 1
            if self._axis_shrink_count < 2:
                return
        elif self._axis_initialized and ceiling < self._axis_maximum:
            self._axis_shrink_count = 0
            return
        self._axis_initialized = True
        self._axis_shrink_count = 0
        if (self._axis_minimum, self._axis_maximum) != (0., ceiling):
            self._axis_minimum, self._axis_maximum = 0., ceiling
            self.axisChanged.emit()

    @Property(bool, notify=changed)
    def enabled(self):
        return self._enabled

    @Property(bool, notify=changed)
    def hasAttempt(self):
        return bool(self._configuration)

    @Property(bool, notify=changed)
    def running(self):
        return self._running

    def score_configuration(self) -> dict:
        return dict(self._score_draft)

    @Property("QVariantMap", notify=changed)  # type: ignore[arg-type]
    def scoreConfig(self) -> dict:
        return self.score_configuration()

    @Property("QVariantMap", notify=changed)  # type: ignore[arg-type]
    def frozenScoreConfig(self) -> dict:
        return dict(self._frozen_score)

    @Property(bool, notify=changed)
    def scoreChangesPending(self):
        return bool(self._frozen_score) and self._score_draft != self._frozen_score

    @Property(str, notify=changed)
    def error(self):
        return self._error

    @Property(str, notify=changed)
    def metric(self):
        return str(self._selection["metric"])

    @Property("QVariantList", notify=changed)  # type: ignore[arg-type]
    def top10(self) -> list:
        scope = self._packet.get("scope", "training")
        return [dict(row, display=f"{index + 1}. " + self._summary(row, scope))
                for index, row in enumerate(self._packet.get("top10", ()))]

    @Property(str, notify=changed)
    def currentSummary(self):
        return self._summary(self._packet.get("current"), self._packet.get("scope", "training"))

    @Property(str, notify=changed)
    def bestSummary(self):
        return self._summary(self._packet.get("best"), self._packet.get("scope", "training"))

    @Property(str, notify=changed)
    def bestDetailSummary(self):
        row = self._packet.get('best')
        if not row:
            return '当前最佳：等待完整冠军。'
        stats = row.get('statistics')
        optical = ('选中波段均值/Rmax：等待匹配的完整原谱' if not stats else
            f"选中波段均值 {100 * stats['mean']:.5g}% · {stats['metric']}max {100 * stats['maximum']:.5g}% @ {stats['maximum_wavelength_nm']:.6g} nm")
        return (f"当前最佳 · {row['layer_count']} 层 · 总厚 {row['total_thickness_nm']:.6g} nm · {optical}\n"
                f"全目标最大归一违规 {row['max_normalized_error']:.5g} · 优化误差 RMS {row['weighted_rms_error']:.5g}\n"
                '均值仅为统计；逐点是否达标取决于原逐点约束。')

    @Property(bool, notify=changed)
    def rankingDiffers(self):
        rows, best = self._packet.get("top10", ()), self._packet.get("best")
        return bool(rows and best and rows[0]["physical_hash"] != best["physical_hash"])

    @Property(str, notify=changed)
    def status(self):
        if not self._enabled:
            return "实时光谱已关闭；候选评分与 Top 10 小摘要仍记录。"
        if not self._configuration:
            return "下一次优化将启用实时光谱；不额外计算光学响应。"
        if not self.plotData:
            return "等待所选目标 / 角度 / metric 的完整候选光谱；不补算缺失分量。"
        return "当前试探与优化器最佳分开显示；仅为原网格抽样，不是达标或应用凭据。"

    @Property("QVariantMap", notify=changed)  # type: ignore[arg-type]
    def plotData(self) -> dict:
        if (not self._enabled or not self._packet.get("enabled")
                or self._packet.get("selection") != self._selection):
            return {}
        current, best = self._packet.get("current"), self._packet.get("best")
        series = {}
        for role, row in (("current", current), ("best", best)):
            if row and row.get("availability") == "available" and row.get("spectrum"):
                series[role] = deepcopy(row["spectrum"])
                if row.get('statistics'):
                    series[role]['maximum'] = row['statistics']['maximum']
        if not series:
            return {}
        result = dict(series, live=True, metric=self.metric)
        index = self._selection["objective_index"]
        if 0 <= index < len(self._objectives):
            objective = self._objectives[index]
            result.update(target_value=objective.pointwise.target_value if objective.pointwise.enabled else None,
                lower_bound=objective.pointwise.lower_bound if objective.pointwise.enabled else None,
                upper_bound=objective.pointwise.upper_bound if objective.pointwise.enabled else None,
                mean_constraint=asdict(objective.mean))
        return result

    @staticmethod
    def _summary(row, scope):
        if not row:
            return "等待完整候选"
        text = (f"S {row['score']:.5g} · E {row['weighted_rms_error']:.5g} · "
                f"名义{'通过' if row['nominal_pass'] else '未通过'} · 最大违规 {row['max_normalized_error']:.4g} · "
                f"N {row['layer_count']} · D {row['total_thickness_nm']:.6g} nm\n"
                f"ID {row['physical_hash'][:12]} · {row.get('source', 'optimization')} · {scope}")
        if "guard_pass" in row:
            text += f" · guard {'通过' if row['guard_pass'] else '未通过'}"
        if "sensitivity_objective" in row:
            text += f" · F {row['sensitivity_objective']:.6g}"
        return text

    @Slot(bool)
    def setEnabled(self, enabled):
        if self._closed:
            return
        self._enabled = bool(enabled)
        self._queue_control()
        self.changed.emit()

    @Slot(str)
    def setMetric(self, metric):
        self.set_selection(self._selection["objective_index"], self._selection["angle_deg"], metric)

    def set_selection(self, objective_index, angle_deg, metric):
        try:
            chosen = prepare_display_configuration({"schema_version": 1, "generation": uuid4().hex,
                "score_config": self._score_draft, "enabled": self._enabled,
                "selection": {"objective_index": int(objective_index), "angle_deg": float(angle_deg),
                              "metric": str(metric)}})["selection"]
        except (ValueError, TypeError, OverflowError):
            return
        if chosen == self._selection:
            return
        self._selection = chosen
        self._queue_control()
        self.changed.emit()

    @Slot(str, str, result=bool)
    def setScoreField(self, field, value):
        if field not in self._score_draft:
            return False
        try:
            number = float(value)
            if isinstance(value, bool) or not math.isfinite(number) or number <= 0:
                raise ValueError("评分偏好和归一化锚点必须是有限正数。")
            self._score_draft = score_config_to_payload(dict(self._score_draft, **{field: number}))
        except (ValueError, TypeError, OverflowError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return False
        self._error = ""
        self.changed.emit()
        return True

    def clear_analysis(self):
        """An independent analysis has no search candidates or fabricated rank."""
        with self._lock:
            self._attempt = self._binding = self._pending_packet = self._control_pending = None
            self._running = False
            self._configuration = {}
            self._last_sequence = self._minimum_control_sequence = 0
        self._packet, self._frozen_score = {}, {}
        self._objectives = ()
        self.changed.emit()

    def begin(self, attempt, *, source_current=None, objectives=(), score_config=None):
        config = prepare_display_configuration({"schema_version": 1, "generation": uuid4().hex,
            "score_config": self._score_draft if score_config is None else score_config,
            "enabled": self._enabled, "selection": self._selection})
        with self._lock:
            self._attempt, self._configuration = attempt, config
            self._binding = self._pending_packet = self._control_pending = None
            self._minimum_control_sequence = self._last_sequence = 0
            self._terminal_received = False
            self._running = True
        self._objectives = tuple(objectives)
        self._frozen_score = deepcopy(config["score_config"])
        self._packet, self._error = {}, ""
        self._source_current = source_current or (lambda: True)
        self.changed.emit()
        return deepcopy(config)

    def bind_admitted(self, attempt, client, job_id):
        """Resolve the admitted identity on the existing queue worker, not Qt.

        Remote admission may transform the frozen spec (device binding and
        portability cleanup). Only its authoritative status, with this exact
        attempt's frozen display configuration, can identify display packets.
        """
        with self._lock:
            if self._closed or attempt != self._attempt or not self._running:
                return False
            configuration = deepcopy(self._configuration)
        try:
            row = client.status(job_id)
            if (not isinstance(row, dict) or row.get("job_id") != job_id
                    or not isinstance(row.get("input_digest"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", row["input_digest"]) is None
                    or not isinstance(row.get("film_display_config"), dict)
                    or row["film_display_config"] != configuration
                    or prepare_display_configuration(row.get("film_display_config")) != configuration):
                return False
        except Exception:
            return False  # A missing/stale receipt loses only this optional display.
        return self.bind(attempt, client, job_id, row["input_digest"], configuration=configuration)

    def bind(self, attempt, client, job_id, input_digest, *, configuration=None):
        """Worker-safe binding; no GUI property is changed from that thread."""
        with self._lock:
            if (self._closed or attempt != self._attempt or not self._running
                    or (configuration is not None and configuration != self._configuration)):
                return False
            self._binding = (client, str(job_id), str(input_digest), self._configuration["generation"])
            changed = (self._enabled != self._configuration["enabled"]
                or self._selection != self._configuration["selection"])
        if changed:
            self._queue_control()
        return True

    def offer(self, attempt, value):
        """Latest-only mailbox: one queued wake, bounded detached packet."""
        with self._lock:
            binding = self._binding
            if self._closed or attempt != self._attempt or binding is None:
                return
        try:
            packet = validate_live_display_packet(value, job_id=binding[1], input_digest=binding[2], generation=binding[3])
        except Exception:
            return
        if packet is None:
            return
        with self._lock:
            if (self._closed or attempt != self._attempt or binding != self._binding or self._terminal_received
                    or (not self._running and not packet["display"]["terminal"])
                    or packet["control_sequence"] < self._minimum_control_sequence):
                return
            pending = self._pending_packet
            if pending is not None and pending["display"]["terminal"]:
                return
            if packet["display"]["sequence"] <= self._last_sequence:
                return
            if pending is not None and packet["display"]["sequence"] <= pending["display"]["sequence"]:
                return
            self._pending_packet = packet
            self._terminal_received = packet["display"]["terminal"]
            wake = not self._wake_pending
            self._wake_pending = True
        if wake:
            try:
                self._wake.emit()
            except RuntimeError:
                return  # Qt disposal raced this optional observation.

    @Slot()
    def _drain(self):
        with self._lock:
            packet, self._pending_packet = self._pending_packet, None
            self._wake_pending = False
            minimum = self._minimum_control_sequence
        if self._closed or packet is None:
            return
        try:
            source_current = self._source_current()
        except Exception:
            source_current = False
        if (not source_current
                or packet["generation"] != self._configuration.get("generation")
                or packet["control_sequence"] < minimum):
            return
        if packet["display"]["sequence"] <= self._last_sequence:
            return
        self._packet = packet["display"]
        with self._lock:
            self._last_sequence = self._packet["sequence"]
        if self._packet["terminal"]:
            with self._lock:
                self._running = False
                self._control_pending = None
        self.changed.emit()

    def _queue_control(self):
        with self._lock:
            if self._closed or not self._running or self._binding is None:
                return
            self._control_pending = (self._binding, self._enabled, dict(self._selection))
            if self._control_inflight:
                return
            self._control_inflight = True
        try:
            self._executor.submit(self._send_controls)
        except RuntimeError:
            with self._lock:
                self._control_inflight = False

    def _send_controls(self):
        while True:
            with self._lock:
                pending, self._control_pending = self._control_pending, None
                if self._closed or not self._running or pending is None:
                    self._control_inflight = False
                    return
                binding, enabled, selection = pending
            error = ""
            for retry in range(2):
                try:
                    reply = binding[0].film_display_control(binding[1], input_digest=binding[2],
                        generation=binding[3], enabled=enabled, selection=selection)
                    with self._lock:
                        if binding == self._binding and isinstance(reply, dict):
                            self._minimum_control_sequence = max(self._minimum_control_sequence,
                                int(reply.get("sequence", 0)))
                    break
                except Exception as exc:
                    with self._lock:
                        still_latest = (not self._closed and self._running and binding == self._binding
                            and enabled == self._enabled and selection == self._selection)
                        if (retry == 0 and str(exc) == "film_display_control_stale" and still_latest):
                            if self._control_pending == pending:
                                self._control_pending = None
                            continue  # Fresh CAS only for the latest unchanged logical intent.
                    error = f"实时显示控制失败：{type(exc).__name__}: {exc}"
                    break
            with self._lock:
                report = (binding == self._binding and enabled == self._enabled
                    and selection == self._selection)
            try:
                if report:
                    self._controlStatus.emit(binding[3], error)
            except RuntimeError:
                return  # The owning Qt window was already destroyed.

    @Slot(str, str)
    def _on_control_status(self, generation, error):
        if not self._closed and generation == self._configuration.get("generation"):
            self._error = error
            self.changed.emit()

    def end(self, attempt):
        if attempt != self._attempt:
            return
        self._drain()  # A terminal packet already in the mailbox must not be lost.
        with self._lock:
            self._running = False
            self._control_pending = None
        self.changed.emit()

    def shutdown(self):
        with self._lock:
            self._closed = True
            self._running = False
            self._pending_packet = self._control_pending = None
        self._executor.shutdown(wait=False, cancel_futures=True)
