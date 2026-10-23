"""Independent thickness-error panel using the durable local CPU scheduler.

This is not an ordinary thickness optimizer mode. It captures its own complete
request and never silently changes the source design or nominal acceptance.
"""

from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QObject, Property, QRunnable, QThreadPool, QUrl, Signal, Slot

from coat_sim.core.film_sensitivity import (
    FilmSensitivityScenario,
    FilmThicknessError,
    film_sensitivity_result_from_payload,
    freeze_film_sensitivity_request,
    sensitivity_request_from_optimization,
    validate_film_sensitivity_result_payload,
)
from coat_sim.core.film_result_service import atomic_film_json
from coat_sim.core.film_nominal_acceptance import solver_condition_acceptable
from coat_sim.scheduler import SchedulerClient, freeze_spec
from coat_sim.scheduler.desktop import RemoteCancelEvent, observe_job, submit_local_with_receipt
from coat_sim.ui.film_synthesis_quick_adapter import synthesis_strict_upper_bound_indices


def _raw_violation(actual, target, lower, upper):
    return abs(actual - target) if target is not None else max(
        0., 0. if lower is None else lower - actual, 0. if upper is None else actual - upper)


class _Signals(QObject):
    progress = Signal(int, object)
    finished = Signal(int, object, str)
    admitted = Signal(int)


class _SensitivityTask(QRunnable):
    def __init__(
        self,
        service,
        request,
        client,
        job_id,
        cancel,
        metadata,
        generation,
        signals,
        disconnected,
        admission_check,
    ):
        super().__init__()
        self.service, self.request, self.client = service, request, client
        self.job_id, self.cancel, self.metadata = job_id, cancel, metadata
        self.generation, self.signals = generation, signals
        self.disconnected, self.admission_check = disconnected, admission_check

    def run(self):
        try:
            request = freeze_film_sensitivity_request(self.service, self.request)
            spec = freeze_spec(None, "film_sensitivity", {"request": request})
            if self.cancel.is_set() or self.disconnected.is_set() or not self.admission_check():
                raise InterruptedError(
                    "Cancelled or source/project changed before admission; no job was submitted."
                )
            capabilities = self.client.capabilities()
            if "film_sensitivity" not in capabilities.get("executors", ()):
                raise ValueError(
                    "The running local scheduler does not support film_sensitivity; close it normally and restart the application."
                )
            if self.cancel.is_set() or self.disconnected.is_set() or not self.admission_check():
                raise InterruptedError(
                    "Cancelled or source/project changed during capability discovery; no job was submitted."
                )
            submit_local_with_receipt(self.client, spec, job_id=self.job_id, metadata=self.metadata)
            self.cancel.mark_admitted()
            self.signals.admitted.emit(self.generation)
            try:
                payload = observe_job(
                    self.client,
                    self.job_id,
                    lambda item: self.signals.progress.emit(self.generation, item),
                    disconnected=self.disconnected.is_set,
                )
            except (InterruptedError, RuntimeError):
                row = self.client.status(self.job_id)
                payload = row.get("result")
                if not isinstance(payload, dict) or row.get("state") not in ("failed", "cancelled"):
                    raise
                from coat_sim.core.film_sensitivity_jobs import terminal_reply
                payload = terminal_reply(payload, row["state"])
            validated = validate_film_sensitivity_result_payload(
                payload, expected_request=request, replay=True
            )
            self.signals.finished.emit(self.generation, validated, "")
        except Exception as exc:
            from coat_sim.scheduler.remote_client import RemoteSubmissionUnknown

            prefix = "submission_unknown: " if isinstance(exc, RemoteSubmissionUnknown) else ""
            self.signals.finished.emit(
                self.generation, None, prefix + f"{type(exc).__name__}: {exc}"
            )


class FilmSensitivityQuickController(QObject):
    changed = Signal()
    overwriteRequested = Signal(str)

    def __init__(self, owner: Any, *, client_factory=SchedulerClient):
        super().__init__(owner)
        self.owner = owner
        self.client_factory = client_factory
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self._signals = _Signals()
        self._signals.progress.connect(self._on_progress)
        self._signals.finished.connect(self._on_finished)
        self._signals.admitted.connect(self._on_admitted)
        self._generation = 0
        self._busy = False
        self._closed = False
        self._unknown_submission = False
        self._rows: list[dict[str, Any]] = []
        self._unit = "absolute_nm"
        self._error_model = "independent"
        self._robustness = "linear_sensitivity_norm"
        self._scenarios: tuple[FilmSensitivityScenario, ...] = ()
        self._options = {"max_evaluations": "200", "max_iterations": "20"}
        self._status = "独立误差模型；不会给普通名义目标自动加余量。"
        self._error = ""
        self._payload: dict[str, Any] | None = None
        self._context: dict[str, Any] | None = None
        self._job_id = ""
        self._cancel: RemoteCancelEvent | None = None
        self._disconnected = threading.Event()
        self._applied = False

    @Property(bool, notify=changed)
    def busy(self):
        return self._busy

    @Property(list, notify=changed)
    def layerErrors(self):
        return deepcopy(self._rows)

    @Property(str, notify=changed)
    def errorUnit(self):
        return self._unit

    @Property(str, notify=changed)
    def errorModel(self):
        return self._error_model

    @Property(str, notify=changed)
    def robustnessObjective(self):
        return self._robustness

    @Property(dict, notify=changed)
    def options(self):
        return dict(self._options)

    @Property(int, notify=changed)
    def scenarioCount(self):
        return len(self._scenarios)

    @Property(str, notify=changed)
    def status(self):
        return self._status

    @Property(str, notify=changed)
    def error(self):
        return self._error

    @Property(str, notify=changed)
    def jobId(self):
        return self._job_id

    @Property(bool, notify=changed)
    def hasResult(self):
        return self._payload is not None

    @Property(bool, notify=changed)
    def canApply(self):
        return (
            not self._busy
            and not self._applied
            and self._payload is not None
            and self._payload.get("is_final") is True
            and self._payload["state"] in {"completed", "partial"}
            and self._payload["operation"] == "optimize"
            and self._payload.get("best_design") is not None
            and self._context is not None
            and self.owner.session is not None
        )

    @Property(str, notify=changed)
    def resultSummary(self):
        if self._payload is None:
            return ""
        result = film_sensitivity_result_from_payload(self._payload)
        best, baseline = result.best_analysis, result.baseline_analysis
        text = (
            f"{result.state} / {result.termination_reason} · "
            f"候选评价 {result.evaluations}，前向 {result.forward_evaluations}，Jacobian {result.jacobian_evaluations}"
        )
        if best is not None:
            raw_worst = max((_raw_violation(row.actual, row.target_value, row.lower_bound, row.upper_bound)
                for row in best.nominal_conditions), default=0.)
            text += (
                f"\n所选误差目标 {result.robustness_objective}: {best.robustness_value:.8g}"
                + (f"（原 {baseline.robustness_value:.8g}）" if baseline else "")
                + f"\n原始最坏违规 {raw_worst * 100:.7g} 个百分点；"
                + f"最大归一化违规 {best.nominal_max_normalized_error:.8g}；"
                + ("名义采样通过" if best.nominal_targets_met else "名义采样未通过")
                + ("；求解容差内可接受" if result.solver_acceptable is True else
                   "；求解容差内不可接受" if result.solver_acceptable is False else "；求解容差状态未知")
                + f"\n一阶敏感度范数 {best.linear_sensitivity_norm:.8g}；完整有限扰动场景 {len(best.finite_scenarios)} 个。"
            )
        return text + "\n线性估计不是有限误差上界，有限场景不是制造良率或连续谱证明。"

    def load_verified_history(self, payload, job_id):
        """Queue loader already replayed physics off the GUI thread.

        The history view has no captured source context and cannot apply.
        """
        film_sensitivity_result_from_payload(payload)
        self._payload = deepcopy(payload)
        self._job_id = job_id
        self._context = None
        self._status = "只读历史敏感性报告；没有绑定当前设计，不可应用。"
        self._error = ""
        self.changed.emit()

    @Property(list, notify=changed)
    def scenarioResults(self):
        if self._payload is None or self._payload.get("best_analysis") is None:
            return []
        result = film_sensitivity_result_from_payload(self._payload)
        assert result.best_analysis is not None
        request = result.frozen_request
        return [
            {
                "scenario_id": row.scenario_id,
                "max_error_percent": 100 * max((_raw_violation(actual, nominal.target_value,
                    nominal.lower_bound, nominal.upper_bound) for nominal, actual in zip(
                        result.best_analysis.nominal_conditions, row.finite_perturbation_values,
                        strict=True)), default=0.),
                "max_normalized_error": row.finite_max_normalized_error,
                "targets_met": row.finite_targets_met,
                "solver_acceptable": all(solver_condition_acceptable(request.objectives[nominal.objective_index],
                    actual, request.acceptable_normalized_error,
                    strict_upper=nominal.objective_index in request.strict_upper_bound_objective_indices)
                    for nominal, actual in zip(result.best_analysis.nominal_conditions,
                        row.finite_perturbation_values, strict=True)),
            }
            for row in result.best_analysis.finite_scenarios
        ]

    @Slot(result=bool)
    def prepare(self):
        if self._busy or self._closed:
            return False
        try:
            base = self.owner._build_request(require_variables=False, fixed_topology=False)
            previous = {row["layer_id"]: row for row in self._rows}
            self._rows = [
                {
                    "layer_id": row["id"],
                    "label": f"{index + 1}. {row['material_id']} / {row['optical_set_id']}",
                    "nominal": f"{row['thickness']['value']:g} {row['thickness']['unit']}",
                    "magnitude": previous.get(row["id"], {}).get("magnitude", "1"),
                    "enabled": previous.get(row["id"], {}).get("enabled", True),
                }
                for index, row in enumerate(base.design_snapshot["front_layers"])
            ]
            if not self._rows:
                raise ValueError("敏感性分析需要已有膜层；先完成自动合成或添加真实膜系。")
            self._error = ""
            self.changed.emit()
            return True
        except (ValueError, TypeError, KeyError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return False

    def _edit(self):
        self._error = ""
        self._scenarios = ()
        self.changed.emit()

    @Slot(str, str, result=bool)
    def setChoice(self, name, value):
        if self._busy:
            return False
        choices = {
            "unit": ("absolute_nm", "relative_fraction"),
            "model": ("independent", "common_mode"),
            "objective": (
                "linear_sensitivity_norm",
                "finite_scenario_worst",
                "finite_scenario_expected",
            ),
        }
        if name not in choices or value not in choices[name]:
            return False
        if name == "unit":
            self._unit = value
        elif name == "model":
            self._error_model = value
        else:
            self._robustness = value
        self._edit()
        return True

    @Slot(str, str, result=bool)
    def setMagnitude(self, layer_id, value):
        if self._busy:
            return False
        row = next((item for item in self._rows if item["layer_id"] == layer_id), None)
        if row is None:
            return False
        row["magnitude"] = value
        self._edit()
        return True

    @Slot(str, bool, result=bool)
    def setLayerEnabled(self, layer_id, enabled):
        if self._busy:
            return False
        row = next((item for item in self._rows if item["layer_id"] == layer_id), None)
        if row is None:
            return False
        row["enabled"] = bool(enabled)
        self._edit()
        return True

    @Slot(str, str, result=bool)
    def setOption(self, name, value):
        if self._busy or name not in self._options:
            return False
        self._options[name] = value
        self._error = ""
        self.changed.emit()
        return True

    @Slot(result=bool)
    def generateScenarios(self):
        if self._busy:
            return False
        count = sum(row["enabled"] for row in self._rows)
        if not count:
            self._error = "至少选择一个误差膜层。"
            self.changed.emit()
            return False
        if self._error_model == "common_mode":
            self._scenarios = tuple(
                FilmSensitivityScenario(f"common_{sign:+d}", (float(sign),) * count)
                for sign in (-1, 1)
            )
        else:
            self._scenarios = tuple(
                FilmSensitivityScenario(
                    f"layer_{axis + 1}_{sign:+d}",
                    tuple(float(sign) if i == axis else 0.0 for i in range(count)),
                )
                for axis in range(count)
                for sign in (-1, 1)
            )
        self._status = (
            f"已显式生成 {len(self._scenarios)} 个±幅度场景；逐层场景不覆盖全部联合误差角点。"
        )
        self._error = ""
        self.changed.emit()
        return True

    @staticmethod
    def _limit(value, name):
        if not value.strip():
            return None
        result = int(value)
        if str(result) != value.strip() or result < 1:
            raise ValueError(f"{name} 必须是正整数或留空。")
        return result

    @Slot(str, result=bool)
    def start(self, operation):
        if self._busy or self._closed or self.owner.busy:
            return False
        if self._unknown_submission:
            self._error = f"作业 {self._job_id} 的提交状态未知；请先在队列核对，不会自动重复提交。"
            self.changed.emit()
            return False
        try:
            base = self.owner._build_request(require_variables=False, fixed_topology=False)
            selected = [row for row in self._rows if row["enabled"]]
            errors = []
            for row in selected:
                magnitude = float(row["magnitude"])
                if not math.isfinite(magnitude) or magnitude < 0:
                    raise ValueError("误差幅度必须为有限非负数。")
                errors.append(
                    FilmThicknessError(
                        row["layer_id"],
                        magnitude / 100.0 if self._unit == "relative_fraction" else magnitude,
                        self._unit,
                    )
                )
            request = sensitivity_request_from_optimization(
                base,
                errors=tuple(errors),
                error_model=self._error_model,
                robustness_objective=self._robustness,
                scenarios=self._scenarios,
                operation=operation,
                max_evaluations=self._limit(self._options["max_evaluations"], "最大候选评价"),
                max_iterations=self._limit(self._options["max_iterations"], "最大迭代"),
                strict_upper_bound_objective_indices=synthesis_strict_upper_bound_indices(
                    getattr(self.owner, "_objectives", ())),
            )
            job_id = uuid4().hex
            compute = self.owner._project_compute
            project_operation = compute.capture(job_id) if compute is not None else None
            metadata = {"summary": "膜厚敏感性 " + operation, "origin": {"surface": "desktop"}}
            if compute is not None:
                metadata = compute.metadata(project_operation, metadata)
            context = {
                "revision": base.design_revision,
                "epoch": self.owner._source_edit_epoch(),
                "project_operation": project_operation,
                "project_compute": compute,
            }
            client = self.client_factory(getattr(self.owner.service, "root", Path.cwd()))
            cancel = RemoteCancelEvent(client, job_id, pending_admission=True)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return False
        self._generation += 1
        self._context, self._job_id, self._cancel = context, job_id, cancel
        self._payload, self._applied = None, False
        self._busy, self._error = True, ""
        self._status = "正在冻结原网格/材料并提交独立本地 CPU 作业。"
        self.changed.emit()
        revision, epoch = context["revision"], context["epoch"]
        session, source = self.owner.session, self.owner._source_controller

        def admission_check():
            project_ok = compute is None or (
                not compute.editing_frozen and compute.accepts(project_operation)
            )
            source_ok = session is None or (
                session.snapshot().design_revision == revision
                and int(getattr(source, "pendingEditEpoch", 0)) == epoch
            )
            return project_ok and source_ok

        self.pool.start(
            _SensitivityTask(
                self.owner.service,
                request,
                client,
                job_id,
                cancel,
                metadata,
                self._generation,
                self._signals,
                self._disconnected,
                admission_check,
            )
        )
        return True

    @Slot(int)
    def _on_admitted(self, generation):
        if self._closed or generation != self._generation:
            return
        if self._context is None:
            return
        compute = self._context["project_compute"]
        if compute is not None:
            compute.register_job(
                self._job_id,
                module_key="film_optimization",
                state="queued",
                metadata={"search_mode": "sensitivity"},
            )
        self._status = "独立敏感性作业已进入共享队列；原设计不变。"
        self.changed.emit()

    @Slot(int, object)
    def _on_progress(self, generation, value):
        if self._closed or generation != self._generation:
            return
        self._status = f"{value.get('stage', 'running')} · 评价 {value.get('evaluations', 0)} · {value.get('message', '')}"
        self.changed.emit()

    @Slot(int, object, str)
    def _on_finished(self, generation, payload, error):
        if self._closed or generation != self._generation:
            return
        self._busy = False
        self._unknown_submission = error.startswith("submission_unknown:")
        self._error, self._payload = error, payload
        if payload is not None:
            self._status = payload["state"] + " / " + payload["termination_reason"]
        else:
            self._status = "结果尚不可用；原设计未改变。"
        compute = self._context["project_compute"] if self._context else None
        if compute is not None:
            state = (
                "submission_unknown"
                if self._unknown_submission
                else "failed"
                if error or payload["state"] == "failed"
                else "cancelled"
                if payload["state"] == "cancelled"
                else "succeeded"
            )
            compute.update_job(self._job_id, state)
        self.changed.emit()

    @Slot()
    def cancel(self):
        if self._busy and self._cancel is not None:
            self._cancel.set()
            self._status = "已请求取消；等待当前完整光学评价结束，不把取消意图当成已取消。"
            self.changed.emit()

    @Slot(result=bool)
    def applyBest(self):
        if not self.canApply or self._payload is None or self._context is None:
            return False
        try:
            result = film_sensitivity_result_from_payload(self._payload)
            accepted = self.owner._apply_sensitivity_candidate(result, self._context)
            if not accepted:
                raise ValueError("来源设计、未提交编辑或项目已变化；整个候选未应用。")
            self._applied = True
            self._status = "敏感性最优候选已显式应用；可在设计窗口撤销。"
            self._error = ""
        except (ValueError, TypeError, RuntimeError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return False
        self.changed.emit()
        return True

    @Slot(str, bool, result=bool)
    def saveReport(self, path, overwrite=False):
        if self._busy or self._payload is None:
            return False
        try:
            target = Path(path).expanduser()
            if target.suffix.lower() != ".json":
                target = target.with_suffix(".json")
            if target.exists() and not overwrite:
                self.overwriteRequested.emit(str(target))
                return False
            atomic_film_json(target, self._payload, overwrite=overwrite)
            self._status, self._error = "完整敏感性报告已保存：" + str(target), ""
        except (ValueError, OSError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return False
        self.changed.emit()
        return True

    @Slot(QUrl, bool, result=bool)
    def saveReportUrl(self, url, overwrite=False):
        if not url.isLocalFile():
            self._error = "请选择本地 JSON 文件位置。"
            self.changed.emit()
            return False
        return self.saveReport(url.toLocalFile(), overwrite)

    @Slot()
    def detach(self):
        self._closed = True
        self._disconnected.set()
        # Project-owned durable jobs outlive this observer; standalone owner
        # closure requests cancellation, but never blocks the GUI thread.
        if self.owner.project_context is None and self._busy:
            self.cancel()

    def shutdown(self, timeout_ms=1000):
        self.detach()
        return self.pool.waitForDone(timeout_ms)
