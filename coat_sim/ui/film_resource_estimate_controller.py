"""Read-only asynchronous adapter between a design session and resource estimates."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import threading
from typing import Any, cast
from uuid import uuid4

from PySide6.QtCore import QObject, Property, QRunnable, QThreadPool, Signal, Slot, Qt


def _digest(design: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            design, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _prepare_request(design: dict, revision: int, limit: int, cancel: threading.Event, material_rows):
    """Read the existing editor configuration using the same target conversion helpers."""
    config = design.get("workflow_settings", {}).get("optimization", {})
    if not isinstance(config, dict) or not config:
        raise ValueError("尚无已保存的目标与候选材料；请先在目标与材料设置中配置。")
    rows = config.get("objectives")
    if not isinstance(rows, list) or not rows:
        raise ValueError("尚无目标；请先在目标与材料设置中添加目标。")
    from coat_sim.core.film_optimization import FilmOptimizationObjective, FilmOptimizationRequest
    from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
    from coat_sim.ui.film_optimization_quick_controller import (
        _angles_for_row_cancellable,
        _constraint_from_editor,
        _number,
        _resolve_objective_sampling,
        _resolve_angle_sampling,
        _validate_editor_criteria,
    )
    from coat_sim.ui.film_synthesis_quick_adapter import build_synthesis_request, synthesis_options

    objectives = []
    for index, original in enumerate(rows):
        if not isinstance(original, dict):
            raise ValueError(f"目标 {index + 1} 配置无效")
        row = _resolve_angle_sampling(_resolve_objective_sampling(deepcopy(original)))
        if row.get("angle_sampling_mode") != "range":
            raise ValueError("目标含显式角度配置；请先在目标设置重新确认角度范围。")
        if any(row.get(group, {}).get("bounds_drafts") for group in ("mean", "pointwise")):
            raise ValueError("目标边界有尚未提交的输入；请先在目标设置中修正。")
        _validate_editor_criteria(row)
        low = _number(row["wavelength_min_nm"], "波长下限")
        high = _number(row["wavelength_max_nm"], "波长上限")
        samples = int(row["wavelength_samples"])
        if low <= 0 or high < low or samples < 1 or (low != high and samples < 2):
            raise ValueError(f"目标 {index + 1} 波长范围或采样数无效")
        objectives.append(
            FilmOptimizationObjective(
                wavelength_min_nm=low,
                wavelength_max_nm=high,
                wavelength_samples=1 if low == high else samples,
                angles_deg=_angles_for_row_cancellable(row, cancel),
                polarization=row["polarization"],
                metric=row["metric"],
                pointwise=_constraint_from_editor(row["pointwise"]),
                mean=_constraint_from_editor(row["mean"]),
                weight=_number(row["weight"], "目标权重"),
                normalization_scale=1.0,
            )
        )
    synthesis = config.get("synthesis")
    if not isinstance(synthesis, dict):
        raise ValueError("尚无候选材料配置；请先在目标与材料设置中配置自动合成。")
    if not isinstance(material_rows, list) or not material_rows:
        raise ValueError("尚无候选材料及数据集；请先在目标与材料设置中配置。")
    refs = []
    seen = set()
    for row in material_rows:
        pair = (
            (row.get("material_id", ""), row.get("optical_set_id", ""))
            if isinstance(row, dict)
            else ("", "")
        )
        if not all(isinstance(v, str) and v for v in pair) or pair in seen:
            raise ValueError("候选材料或数据集缺失、重复；请检查目标与材料设置。")
        seen.add(pair)
        refs.append(FilmNeedleMaterialReference(*pair))
    if type(synthesis.get("assume_unknown_k_zero")) is not bool:
        raise ValueError("未知 k 策略尚未明确；请先在目标与材料设置中确认。")
    sides = synthesis.get("selected_surfaces", ["front"])
    if not isinstance(sides, list):
        raise ValueError("所选物理面无效，请在目标与材料设置中检查。")
    substrate = synthesis.get("substrate_reference")
    source = deepcopy(design)
    if substrate is not None:
        if not isinstance(substrate, dict) or not all(
            substrate.get(k) for k in ("material_id", "optical_set_id")
        ):
            raise ValueError("合成基片材料或数据集无效，请在设置中检查。")
        source["substrate"].update({k: substrate[k] for k in ("material_id", "optical_set_id")})
    solver = config.get("solver", {})
    if not isinstance(solver, dict):
        raise ValueError("目标配置无效，请在设置中检查。")
    base = FilmOptimizationRequest(
        source,
        revision,
        (),
        tuple(objectives),
        acceptable_normalized_error=_number(solver.get("acceptable_error_percent", 0.1), "误差容限")
        / 100,
        validation_samples_multiplier=int(solver.get("validation_samples_multiplier", 2)),
    )
    # Resource totals are the quantities under study, not copied optimization
    # search caps. Per-layer process limits, however, describe the admissible
    # structures and must remain part of the frozen physical problem.
    saved_options = synthesis.get("options", {})
    if not isinstance(saved_options, dict):
        raise ValueError("合成工艺配置无效，请在目标与材料设置中检查。")
    process_limits = {
        key: saved_options[key]
        for key in ("minimum_nonzero_thickness_nm", "max_layer_thickness_nm")
        if key in saved_options
    }
    synthesis_request = build_synthesis_request(
        base,
        tuple(refs),
        process_limits,
        False,
        synthesis["assume_unknown_k_zero"],
        selected_surfaces=tuple(sides),
    )
    # Current structure is a candidate only, never an expansion of the allowed material pool.
    candidate = deepcopy(source)
    candidate["assume_lossless"] = synthesis["assume_unknown_k_zero"]
    from coat_sim.core.film_resource_estimation import FilmResourceEstimateRequest

    budget = synthesis_options(
        {key: saved_options.get(key) for key in ("max_layers", "max_total_thickness_nm")}
    )
    return FilmResourceEstimateRequest(
        synthesis_request,
        candidate_designs=(candidate,),
        layer_search_limit=limit,
        search_budget_reference={
            key: budget[key] for key in ("max_layers", "max_total_thickness_nm")
        },
    )


class _Signals(QObject):
    progress = Signal(int, object)
    finished = Signal(int, object, object)


class _Task(QRunnable):
    def __init__(self, service, design, revision, limit, cancel, token, signals, material_rows):
        super().__init__()
        self.service, self.design, self.revision, self.limit = service, design, revision, limit
        self.cancel, self.token, self.signals = cancel, token, signals
        self.material_rows = material_rows

    def run(self):
        try:
            request = _prepare_request(self.design, self.revision, self.limit, self.cancel, self.material_rows)
            from coat_sim.core.film_resource_estimation import (
                estimate_film_resources,
                freeze_resource_estimate_request,
                resource_estimate_source_digest,
                FilmResourceEstimateProgress,
            )

            last_progress = [None]

            def progress(event):
                last_progress[0] = event
                self.signals.progress.emit(self.token, event)

            result = estimate_film_resources(
                self.service, request, cancel_event=self.cancel, progress_callback=progress
            )
            payload = asdict(result)
            payload["_source_check"] = "unavailable"
            payload["_source_check_extra_freeze_attempts"] = 0
            if result.source_digest:
                if self.cancel.is_set():
                    payload["_source_check"] = "unverified"
                else:
                    payload["_source_check_extra_freeze_attempts"] = 1
                    previous = last_progress[0]
                    self.signals.progress.emit(
                        self.token,
                        FilmResourceEstimateProgress(
                            "source_check",
                            "复核当前材料源快照",
                            getattr(previous, "completed_units", 0),
                            getattr(previous, "physical_evaluations", 0),
                            getattr(previous, "wavelength_points", 0),
                        ),
                    )
                    try:
                        current = freeze_resource_estimate_request(
                            self.service, request, cancel_event=self.cancel
                        )
                        digest = resource_estimate_source_digest(current)
                        payload["_source_check"] = (
                            "current" if digest == result.source_digest else "changed"
                        )
                    except Exception as exc:
                        payload["_source_check"] = "unverified"
                        payload["_source_check_message"] = str(exc)
            self.signals.finished.emit(self.token, payload, None)
        except Exception as exc:
            self.signals.finished.emit(self.token, None, exc)


class FilmResourceEstimateController(QObject):
    changed = Signal()

    def __init__(self, source, parent=None):
        super().__init__(parent)
        self._source = source
        self._draft_session = source.session
        self._material_rows = []
        self._catalog = []
        self._catalog_error = ""
        self.refreshMaterials()
        self._busy = False
        self._stale = False
        self._limit = 128
        self._limit_valid = True
        self._status = "尚未开始。请先检查当前目标与材料设置。"
        self._result = "预测：unknown（无已校准模型）。"
        self._source_summary = "尚无冻结快照。"
        self._digest = ""
        self._source_revision = None
        self._token = 0
        self._cancel = None
        self._signals = _Signals()
        self._signals.progress.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
        self._signals.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
        source.changed.connect(self._source_changed)
        source.workflowConfigurationChanged.connect(self._source_changed)
        source.closeConfirmed.connect(self.close)

    def _check_session(self):
        # Draft belongs to this editor session; never carry it into another design.
        if self._draft_session is not self._source.session:
            self._draft_session = self._source.session
            self._material_rows = []
            self._digest = ""
            self._stale = False

    def resetDraft(self):
        self.cancel()
        self._material_rows = []
        if self._digest:
            self._stale = True
            self._status = "设计已替换；评估草稿已清空，原结果已过期。"
        self.changed.emit()

    @Slot()
    def refreshMaterials(self):
        self._check_session()
        try:
            records = self._source.service.material_options("film")
            catalog = []
            for material in records:
                if not isinstance(material, dict) or material.get("design_ready") is False:
                    continue
                mid = material.get("id")
                if not isinstance(mid, str) or not mid:
                    continue
                sets = [{"id": item["id"], "name": str(item.get("name") or item["id"])}
                        for item in material.get("optical_sets", [])
                        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]]
                if sets:
                    catalog.append({"id": mid, "name": str(material.get("name") or mid), "optical_sets": sets})
            self._catalog = sorted(catalog, key=lambda row: (row["name"].casefold(), row["id"]))
            self._catalog_error = ""
        except Exception as exc:
            self._catalog = []
            self._catalog_error = f"材料目录读取失败：{exc}"
        self.changed.emit()

    @Property(list, notify=changed)
    def materialRows(self):
        self._check_session()
        by_id = {item["id"]: item for item in self._catalog}
        options = [{"id": item["id"], "name": item["name"]} for item in self._catalog]
        result = []
        for row in self._material_rows:
            optical = by_id.get(row["material_id"], {}).get("optical_sets", [])
            valid = any(item["id"] == row["optical_set_id"] for item in optical)
            result.append({**row, "material_options": deepcopy(options), "optical_set_options": deepcopy(optical),
                           "error": "" if valid else "材料或数据集未选择或已不可用；请重新选择或删除"})
        return result

    @Property(str, notify=changed)
    def materialError(self):
        if self._catalog_error:
            return self._catalog_error
        if not self._material_rows:
            return "请添加至少一行候选材料及数据集"
        seen = set()
        for row in cast(list[dict], self.materialRows):
            pair = (row["material_id"], row["optical_set_id"])
            if row["error"]:
                return row["error"]
            if pair in seen:
                return "候选材料及数据集重复"
            seen.add(pair)
        return ""

    def _materials_changed(self):
        if self._digest:
            self._stale = True
            self._status = "候选材料已变化；原结果已过期，请重新评估。"
        self.changed.emit()

    @Slot(result=str)
    def addMaterialRow(self):
        if self._busy:
            return ""
        identity = uuid4().hex
        self._material_rows.append({"id": identity, "material_id": "", "optical_set_id": ""})
        self._materials_changed()
        return identity

    @Slot(str, result=bool)
    def removeMaterialRow(self, identity):
        if self._busy:
            return False
        row = next((r for r in self._material_rows if r["id"] == identity), None)
        if row is None:
            return False
        self._material_rows.remove(row)
        self._materials_changed()
        return True

    @Slot(str, int, result=bool)
    def moveMaterialRow(self, identity, delta):
        if self._busy or delta not in (-1, 1):
            return False
        index = next((i for i, r in enumerate(self._material_rows) if r["id"] == identity), -1)
        if index < 0 or not 0 <= index + delta < len(self._material_rows):
            return False
        self._material_rows.insert(index + delta, self._material_rows.pop(index))
        self._materials_changed()
        return True

    @Slot(str, str, result=bool)
    def setRowMaterial(self, identity, material_id):
        if self._busy:
            return False
        row = next((r for r in self._material_rows if r["id"] == identity), None)
        material = next((r for r in self._catalog if r["id"] == material_id), None)
        if row is None or material is None:
            return False
        row.update(material_id=material_id, optical_set_id=material["optical_sets"][0]["id"])
        self._materials_changed()
        return True

    @Slot(str, str, result=bool)
    def setRowOpticalSet(self, identity, optical_id):
        if self._busy:
            return False
        row = next((r for r in self._material_rows if r["id"] == identity), None)
        material = next((r for r in self._catalog if row and r["id"] == row["material_id"]), None)
        if row is None or material is None or not any(r["id"] == optical_id for r in material["optical_sets"]):
            return False
        row["optical_set_id"] = optical_id
        self._materials_changed()
        return True

    @Property(bool, notify=changed)
    def busy(self):
        return self._busy

    @Property(bool, notify=changed)
    def stale(self):
        return self._stale

    @Property(int, notify=changed)
    def layerLimit(self):
        return self._limit

    @Property(bool, notify=changed)
    def limitValid(self):
        return self._limit_valid

    @Property(str, notify=changed)
    def status(self):
        return self._status

    @Property(str, notify=changed)
    def sourceSummary(self):
        return self._source_summary

    @Property(str, notify=changed)
    def resultSummary(self):
        return self._result

    @Slot(str, result=bool)
    def setLayerLimit(self, value):
        try:
            if not value.isascii() or not value.isdecimal():
                raise ValueError()
            limit = int(value)
            if not 1 <= limit <= 100000:
                raise ValueError()
        except (ValueError, TypeError):
            self._limit_valid = False
            self._status = "点频必要界枚举窗口须为 1 到 100000 的整数。"
            self.changed.emit()
            return False
        if self._digest and limit != self._limit:
            self._stale = True
            self._status = "枚举窗口已变化；原结果已过期，请重新评估。"
        self._limit = limit
        self._limit_valid = True
        self.changed.emit()
        return True

    @Slot(result=bool)
    def start(self):
        if (
            self._busy
            or not self._limit_valid
            or self._source.session is None
            or self._source._source_closed
        ):
            return False
        self.refreshMaterials()
        if self.materialError:
            self._status = cast(str, self.materialError)
            self.changed.emit()
            return False
        snapshot = self._source.session.snapshot()
        design = deepcopy(snapshot.design)
        self._digest = _digest(design)
        self._source_revision = snapshot.design_revision
        self._token += 1
        self._cancel = threading.Event()
        self._busy = True
        self._stale = False
        self._status = "准备冻结快照…"
        self._result = "预测：unknown（无已校准模型）。"
        config = design.get("workflow_settings", {}).get("optimization", {})
        synthesis = config.get("synthesis", {}) if isinstance(config, dict) else {}
        if not isinstance(synthesis, dict):
            synthesis = {}
        rows = deepcopy(self._material_rows)
        sides = synthesis.get("selected_surfaces", ["front"]) if isinstance(synthesis, dict) else []
        objectives = config.get("objectives", []) if isinstance(config, dict) else []
        if not isinstance(rows, list):
            rows = []
        if not isinstance(objectives, list):
            objectives = []
        saved_options = synthesis.get("options", {})
        if not isinstance(saved_options, dict):
            saved_options = {}

        def setting(key, unit):
            value = saved_options.get(key)
            return "未设置" if value is None or value == "" else f"{value}{unit}"

        substrate = design.get("substrate", {})
        if isinstance(synthesis, dict) and synthesis.get("substrate_reference"):
            substrate = {**substrate, **synthesis["substrate_reference"]}

        def criterion(value):
            if not isinstance(value, dict) or not value.get("enabled"):
                return "未启用"
            if value.get("mode") == "value":
                return f"={value.get('target_value_percent', '?')}%"
            return (
                f"{'>' if value.get('strict_lower') else '≥'}{value.get('lower_bound_percent', '?')}%，"
                f"{'<' if value.get('strict_upper') else '≤'}{value.get('upper_bound_percent', '?')}%"
            )

        target_text = (
            "; ".join(
                f"{r.get('metric', '?')} {r.get('wavelength_min_nm', '?')}–{r.get('wavelength_max_nm', '?')} nm "
                f"({r.get('wavelength_samples', '?')} 点)，{r.get('polarization', '?')}，"
                f"角度 {r.get('angle_min_deg', '?')}–{r.get('angle_max_deg', '?')}° "
                f"({r.get('angle_sample_count', '?')} 点)，逐点 {criterion(r.get('pointwise'))}，均值 {criterion(r.get('mean'))}"
                for r in objectives
                if isinstance(r, dict)
            )
            or "无"
        )
        self._source_summary = (
            f"来源设计修订 {snapshot.design_revision} · 输入快照 {self._digest[:12]}\n"
            f"选面：{sides}；未选面作为固定环境。候选材料："
            + (
                ", ".join(
                    f"{r.get('material_id', '?')}/{r.get('optical_set_id', '?')}"
                    for r in rows
                    if isinstance(r, dict)
                )
                or "无"
            )
            + f"\n指标：{target_text}\n基片：{substrate.get('material_id', '?')}/{substrate.get('optical_set_id', '?')}，"
            f"{substrate.get('sides', '?')} 面 / {substrate.get('mode', '?')}；入射面：{design.get('first_surface', 'front')}；"
            f"入射介质：{design.get('incident_medium', {})}；出射介质：{design.get('exit_medium', {})}。\n未知 k 策略："
            + (
                "明确按零"
                if synthesis.get("assume_unknown_k_zero") is True
                else "不按零"
                if synthesis.get("assume_unknown_k_zero") is False
                else "未确认"
            )
            + f"\n工艺单层最小非零厚度：{setting('minimum_nonzero_thickness_nm', ' nm')}；"
            f"单层最大厚度：{setting('max_layer_thickness_nm', ' nm')}。"
            f"\n现有优化搜索总层数帽：{setting('max_layers', ' 层')}；"
            f"总厚度帽：{setting('max_total_thickness_nm', ' nm')}。"
            "评估不受当前搜索总预算限制；制造良率未评估。"
        )
        self.changed.emit()
        QThreadPool.globalInstance().start(
            _Task(
                self._source.service,
                design,
                snapshot.design_revision,
                self._limit,
                self._cancel,
                self._token,
                self._signals,
                deepcopy(self._material_rows),
            )
        )
        return True

    @Slot()
    def cancel(self):
        if self._busy and self._cancel is not None:
            self._cancel.set()
            self._status = "已请求取消；正在等待当前计算安全退出（非硬实时）。"
            self.changed.emit()

    @Slot()
    def close(self):
        self.cancel()

    @Slot()
    def _source_changed(self):
        if not self._digest or self._source.session is None:
            return
        try:
            snapshot = self._source.session.snapshot()
            changed = (
                snapshot.design_revision != self._source_revision
                or _digest(snapshot.design) != self._digest
            )
        except (TypeError, ValueError):
            changed = True
        if changed and not self._stale:
            self._stale = True
            self._status = "来源已变化；本次结果已过期。" + (
                "正在运行；可取消。" if self._busy else "请重新评估。"
            )
            self.changed.emit()

    @Slot(int, object)
    def _on_progress(self, token, event):
        if token != self._token or not self._busy:
            return
        cancel = self._cancel
        if not self._stale and cancel is not None and not cancel.is_set():
            self._status = (
                f"{getattr(event, 'stage', '')}：{getattr(event, 'message', '')} · "
                f"工作单元 {getattr(event, 'completed_units', 0)}，物理评估 {getattr(event, 'physical_evaluations', 0)}，波长点 {getattr(event, 'wavelength_points', 0)}"
            )
            self.changed.emit()

    @Slot(int, object, object)
    def _on_finished(self, token, result, error):
        if token != self._token or not self._busy:
            return
        self._source_changed()
        self._busy = False
        if error is not None:
            self._status = (
                "已取消。" if isinstance(error, InterruptedError) else f"评估未完成：{error}"
            )
        else:
            self._status = (
                f"评估状态：{result.get('state', 'unknown')} · {result.get('message', '')}"
            )
            source_check = result.get("_source_check", "unavailable")
            if source_check == "changed":
                self._stale = True
                self._status += " 材料源在评估期间变化。"
            elif source_check == "unverified":
                self._stale = True
                self._status += " 材料源未能复核；结果不可视作当前来源。" + result.get(
                    "_source_check_message", ""
                )
            elif source_check == "current":
                self._status += " 材料源在完成时已复核；此后外部材料变更请重新评估。"
            self._result = (
                "冻结输入摘要："
                + str(result.get("input_digest") or "unknown")
                + "\n材料与物理源摘要："
                + str(result.get("source_digest") or "unknown")
                + "\n\n"
                + _format_result(result)
            )
        if self._stale:
            self._status = "来源已变化，本次结果已过期；请重新评估。" + self._status
        self.changed.emit()


def _format_result(result: dict[str, Any]) -> str:
    translations = {
        "applicable": "适用",
        "not_applicable": "不适用",
        "partial": "部分适用",
        "conflict": "模型筛查冲突",
        "not_excluded": "未排除",
        "audited": "已独立审计",
        "invalid": "无效",
        "error": "审计错误",
        "requires_pointwise_normal_hr_constant_lossless_n": "要求逐点高反射、正入射和恒定无损折射率",
        "no_applicable_lossless_propagating_condition": "没有适用的无损传播条件",
        "no_lossless_propagating_substrate_conditions": "基片不满足无损传播条件",
        "material_source_is_not_constant_tabulated_n": "材料数据不是恒定折射率表格",
        "mvp_requires_single_front_half_space": "当前界仅适用于单面正面半空间模型",
        "requires_incoherent_substrate": "要求非相干基片模型",
        "requires_pointwise_hr": "要求逐点高反射目标",
        "conditional_pointwise_hr_necessary_screen": "点频高反射条件必要界（非充分条件）",
        "finite_sampled_audit_not_continuous_certificate": "有限采样审计，不是连续域证书",
    }

    def tr(value):
        return translations.get(str(value), str(value or ""))

    lines = ["条件数值必要界（浮点筛查，不是严格证书）："]
    for item in result.get("bounds", []):
        name = {"hr_thickness_lp": "厚度必要界", "hr_layer_dp": "层数必要界"}.get(
            item.get("kind"), str(item.get("kind"))
        )
        applicability = item.get("applicability", "not_applicable")
        value = (
            item.get("value_nm")
            if item.get("kind") == "hr_thickness_lp"
            else item.get("value_layers")
        )
        unit = " nm" if item.get("kind") == "hr_thickness_lp" else " 层"
        suffix = f"{value}{unit}" if value is not None else "unknown"
        if item.get("excluded_through") is not None:
            suffix += (
                f"；已排除至 {item['excluded_through']} 层（枚举窗口 {item.get('search_limit')}）"
            )
        assumed = "；以未知 k 按零的假设为条件" if item.get("conditional_on_assumption") else ""
        lines.append(
            f"• {name}：{suffix}；{tr(applicability)}；{tr(item.get('reason', ''))}{assumed}"
        )
    if not result.get("bounds"):
        lines.append("• unknown（无可用界）")
    lines.append("\n适用性及条件筛查（不等价于生产不可行）：")
    for item in result.get("screenings", []):
        lines.append(
            f"• {item.get('kind', '')}：{tr(item.get('state', 'unknown'))}；{tr(item.get('reason', ''))}"
        )
    if not result.get("screenings"):
        lines.append("• 无")

    def face_summary(surfaces):
        if not isinstance(surfaces, dict):
            return "unknown"
        parts = []
        for side, label in (("front", "正面"), ("back", "背面")):
            item = surfaces.get(side)
            if not isinstance(item, dict):
                continue
            if item.get("active") is False:
                parts.append(f"{label}：未参与光路/未知")
            else:
                thickness = item.get("total_thickness_nm")
                parts.append(
                    f"{label}：{item.get('layer_count', '?')} 层 / "
                    + ("未知" if thickness is None else f"{thickness} nm")
                )
        return "；".join(parts) or "unknown"

    lines.append("\n当前方案资源及审计：")
    for item in result.get("candidate_reports", []):
        report_assumption = item.get("absorption_assumption")
        if item.get("conditional_on_assumption") is True:
            report_condition = "本审计以显式未知 k=0 为条件"
        elif (
            item.get("conditional_on_assumption") is False
            and isinstance(report_assumption, dict)
            and report_assumption.get("conditional_on_assumption") is False
        ):
            report_condition = "本审计未使用未知 k=0 假设"
        else:
            report_condition = "本审计假设状态 unknown"
        lines.append(
            f"• {tr(item.get('state', 'unknown'))}；{item.get('layer_count')} 层 / {item.get('total_thickness_nm')} nm；"
            f"各面 {face_summary(item.get('surfaces', {}))}；指标通过 {item.get('targets_met')}；"
            f"求解器可接受 {item.get('solver_acceptable')}；材料池内 {item.get('within_material_pool', 'unknown')}；"
            f"工艺/材料约束内 {item.get('within_constraints')}；"
            f"原搜索预算内 {item.get('within_search_budgets', 'unknown')}；{tr(item.get('message', ''))}；{report_condition}"
        )
    if not result.get("candidate_reports"):
        lines.append("• unknown（无完整审计）")
    lines.append("\n联合层数–厚度推荐（实际见证，不是预测；有限采样，非连续域证书）：")
    nodes = result.get("recommended_budget") or []
    for item in nodes:
        assumption = item.get("absorption_assumption")
        conditional = item.get("conditional_on_assumption")
        if (
            conditional is True
            and isinstance(assumption, dict)
            and assumption.get("conditional_on_assumption") is True
        ):
            affected = (
                assumption.get("used_affected_materials")
                or assumption.get("affected_materials")
                or []
            )
            materials = (
                ", ".join(
                    f"{row.get('material_id', '?')}/{row.get('optical_set_id', '?')}"
                    for row in affected
                    if isinstance(row, dict)
                )
                or "具体材料未知"
            )
            condition = "条件推荐：依赖用户显式未知 k=0 假设；受影响材料：" + materials
        elif (
            conditional is False
            and isinstance(assumption, dict)
            and assumption.get("conditional_on_assumption") is False
        ):
            condition = "该见证未使用未知 k=0 假设（已知 k 保留）"
        else:
            condition = "假设状态 unknown；不可视作无条件推荐"
        evidence = (
            "有限采样审计见证"
            if item.get("claim_level") == "sampled_witness"
            else "证据等级 unknown"
        )
        source = str(item.get("source_digest") or "unknown")
        lines.append(
            f"• 候选 {item.get('candidate_index', '?')}：{item.get('layer_count')} 层 / "
            f"{item.get('total_thickness_nm')} nm；各面 {face_summary(item.get('surfaces', {}))}；"
            f"原搜索预算内 {item.get('within_search_budgets', 'unknown')}。\n"
            f"  {condition}。{evidence}；非连续域证书，也不是实测吸收或制造良率证书。"
            f"\n  来源摘要：{source}"
        )
    if not nodes:
        lines.append("• unknown（未取得已验证见证）")
    lines.append(
        "预测：unknown（无已校准模型）；可行性：" + str(result.get("feasibility_state", "unknown"))
    )
    work = result.get("work", {})
    if not isinstance(work, dict):
        work = {}
    lines.append(
        f"核心材料冻结：开始 {work.get('source_freeze_attempts', 'unknown')} 次；"
        f"完成 {work.get('source_freeze_passes', 'unknown')} 次。"
    )
    lines.append("核心工作量：" + json.dumps(work, ensure_ascii=False, default=str))
    lines.append(
        "UI 来源复核额外冻结尝试次数（不含在核心工作量中）："
        + str(result.get("_source_check_extra_freeze_attempts", 0))
    )
    return "\n".join(lines)
