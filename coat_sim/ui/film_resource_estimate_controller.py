"""Read-only asynchronous adapter between a design session and resource estimates."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import re
import threading
from typing import Any, cast
from uuid import uuid4

from PySide6.QtCore import QObject, Property, QRunnable, QThreadPool, Signal, Slot, Qt


def _friendly_error(error: Any) -> str:
    message = str(error)
    lower = message.lower()
    if "unknown k" in lower or "unknown_k" in lower or "n/k source" in lower:
        return "材料的消光系数 k 未知或数据无效；请检查光学数据，或在优化设置中明确选择未知 k 按零假设。"
    if "missing" in lower or "not found" in lower:
        return "材料或光学数据缺失；请检查选择和数据集是否仍可用。"
    if (any("\u4e00" <= char <= "\u9fff" for char in message)
            and not re.search(r"[A-Za-z0-9_/-]{8,}", message)):
        return message
    return "输入或材料数据无法完成评估；请检查技术详情及当前设置。"


def _digest(design: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            design, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _prepare_request(design: dict, revision: int, limit: int, cancel: threading.Event, material_rows, substrate_reference=None, probe_max_evaluations=0, probe_seeds=(0, 1), audit_candidates=False):
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
    substrate = substrate_reference if substrate_reference is not None else synthesis.get("substrate_reference")
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
        probe_max_evaluations=probe_max_evaluations,
        audit_candidates=audit_candidates,
        probe_seeds=tuple(probe_seeds),
        layer_search_limit=limit,
        search_budget_reference={
            key: budget[key] for key in ("max_layers", "max_total_thickness_nm")
        },
    )


class _Signals(QObject):
    progress = Signal(int, object)
    finished = Signal(int, object, object)


class _Task(QRunnable):
    def __init__(self, service, design, revision, limit, cancel, token, signals, material_rows, substrate_reference, probe_max_evaluations=0, probe_seeds=(0, 1), audit_candidates=False):
        super().__init__()
        self.service, self.design, self.revision, self.limit = service, design, revision, limit
        self.cancel, self.token, self.signals = cancel, token, signals
        self.material_rows = material_rows
        self.substrate_reference = substrate_reference
        self.probe_max_evaluations = probe_max_evaluations
        self.probe_seeds = tuple(probe_seeds)
        self.audit_candidates = audit_candidates

    def run(self):
        try:
            request = _prepare_request(self.design, self.revision, self.limit, self.cancel, self.material_rows, self.substrate_reference, self.probe_max_evaluations, self.probe_seeds, self.audit_candidates)
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
            payload["_audit_candidates"] = self.audit_candidates
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
        self._substrate_catalog = []
        self._substrate = None
        self._catalog_error = ""
        self.refreshMaterials()
        self._busy = False
        self._stale = False
        self._limit = 128
        self._limit_valid = True
        self._verification_enabled = False
        self._probe_max_evaluations = 256
        self._probe_seeds: tuple[int, ...] = (0, 1)
        self._probe_budget_error = ""
        self._probe_seeds_error = ""
        self._status = "尚未开始。请先检查当前目标与材料设置。"
        self._result = "尚未计算。"
        self._technical = ""
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
            self._substrate = None
            self._digest = ""
            self._stale = False

    def resetDraft(self):
        self.cancel()
        self._material_rows = []
        self._substrate = None
        if self._digest:
            self._stale = True
            self._status = "设计已替换；评估草稿已清空，原结果已过期。"
        self.changed.emit()

    @Slot()
    def refreshMaterials(self):
        self._check_session()
        try:
            records = self._source.service.material_options("film")
            substrate_records = self._source.service.material_options("substrate")
            def catalog_for(records):
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
                return sorted(catalog, key=lambda row: (row["name"].casefold(), row["id"]))
            self._catalog = catalog_for(records)
            self._substrate_catalog = catalog_for(substrate_records)
            self._initialize_substrate()
            self._catalog_error = ""
        except Exception as exc:
            self._catalog = []
            self._substrate_catalog = []
            self._catalog_error = f"材料目录读取失败：{exc}"
        self.changed.emit()

    def _initialize_substrate(self):
        if self._substrate is not None or self._source.session is None:
            return
        design = self._source.session.snapshot().design
        substrate = design.get("substrate", {})
        self._substrate = {key: substrate.get(key, "") for key in ("material_id", "optical_set_id")}

    @Property(dict, notify=changed)
    def substrateSelection(self):
        self._check_session()
        self._initialize_substrate()
        row = self._substrate or {"material_id": "", "optical_set_id": ""}
        material = next((m for m in self._substrate_catalog if m["id"] == row["material_id"]), None)
        optical = material["optical_sets"] if material else []
        valid = any(item["id"] == row["optical_set_id"] for item in optical)
        return {**row, "material_options": [{"id": m["id"], "name": m["name"]} for m in self._substrate_catalog],
                "optical_set_options": deepcopy(optical),
                "error": "" if valid else "基片材料或数据集不可用；请重新选择。"}

    @Slot(str, result=bool)
    def setSubstrateMaterial(self, material_id):
        if self._busy:
            return False
        material = next((m for m in self._substrate_catalog if m["id"] == material_id), None)
        if material is None:
            return False
        self._substrate = {"material_id": material_id, "optical_set_id": material["optical_sets"][0]["id"]}
        self._materials_changed()
        return True

    @Slot(str, result=bool)
    def setSubstrateOpticalSet(self, optical_id):
        if self._busy:
            return False
        selection = cast(dict, self.substrateSelection)
        if not any(item["id"] == optical_id for item in selection["optical_set_options"]):
            return False
        assert self._substrate is not None
        self._substrate["optical_set_id"] = optical_id
        self._materials_changed()
        return True

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
            self._status = "评估材料或基片已变化；原结果已过期，请重新评估。"
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

    @Property(bool, notify=changed)
    def verificationEnabled(self):
        return self._verification_enabled

    @Slot(bool, result=bool)
    def setVerificationEnabled(self, enabled):
        if self._busy:
            return False
        if bool(enabled) != self._verification_enabled:
            self._verification_enabled = bool(enabled)
            if self._digest:
                self._stale = True
                self._status = "评估模式已变化；原结果已过期，请重新评估。"
            self.changed.emit()
        return True

    @Property(str, notify=changed)
    def probeMaxEvaluations(self):
        return str(self._probe_max_evaluations)

    @Property(str, notify=changed)
    def probeSeeds(self):
        return ",".join(str(seed) for seed in self._probe_seeds)

    @Property(bool, notify=changed)
    def probeSettingsValid(self):
        return not self._probe_budget_error and not self._probe_seeds_error

    @Property(str, notify=changed)
    def probeSettingsError(self):
        return self._probe_budget_error or self._probe_seeds_error

    @Slot(str, result=bool)
    def setProbeMaxEvaluations(self, value):
        if self._busy:
            return False
        text = str(value).strip()
        try:
            budget = int(text) if text.isascii() and text.isdecimal() else -1
        except ValueError:
            budget = -1
        if budget < 0 or budget == 1:
            self._probe_budget_error = "每个种子的逻辑评价上限须为 0 或不小于 2 的整数；0 表示不探测。"
            self.invalidateDraft()
            self.changed.emit()
            return False
        self._probe_budget_error = ""
        if budget != self._probe_max_evaluations:
            self._probe_max_evaluations = budget
            self.invalidateDraft()
        self.changed.emit()
        return True

    @Slot(str, result=bool)
    def setProbeSeeds(self, value):
        if self._busy:
            return False
        parts = [part.strip() for part in str(value).split(",")]
        if any(not part.isascii() or not part.isdecimal() for part in parts):
            self._probe_seeds_error = "请输入不同的非负整数种子，以英文逗号分隔。"
            self.invalidateDraft()
            self.changed.emit()
            return False
        try:
            seeds = tuple(int(part) for part in parts)
        except ValueError:
            self._probe_seeds_error = "种子整数过长，无法解析。"
            self.invalidateDraft()
            self.changed.emit()
            return False
        if len(set(seeds)) != len(seeds) or any(seed > 4294967295 for seed in seeds):
            self._probe_seeds_error = "种子不能重复，且须介于 0 和 4294967295 之间。"
            self.invalidateDraft()
            self.changed.emit()
            return False
        self._probe_seeds_error = ""
        if seeds != self._probe_seeds:
            self._probe_seeds = seeds
            self.invalidateDraft()
        self.changed.emit()
        return True

    @Property(str, notify=changed)
    def status(self):
        return self._status

    @Property(str, notify=changed)
    def sourceSummary(self):
        return self._source_summary

    @Property(str, notify=changed)
    def resultSummary(self):
        return self._result

    @Property(str, notify=changed)
    def technicalDetails(self):
        return self._technical

    @Slot(str, result=bool)
    def setLayerLimit(self, value):
        if self._busy:
            return False
        try:
            if not value.isascii() or not value.isdecimal():
                raise ValueError()
            limit = int(value)
            if not 1 <= limit <= 100000:
                raise ValueError()
        except (ValueError, TypeError):
            self._limit_valid = False
            self.invalidateDraft()
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
        return self.startWithConfiguration(None)

    @Slot(object, result=bool)
    def startWithConfiguration(self, configuration):
        if (
            self._busy
            or not self._limit_valid
            or (self._verification_enabled and not self.probeSettingsValid)
            or self._source.session is None
            or self._source._source_closed
        ):
            return False
        self.refreshMaterials()
        substrate_selection = cast(dict, self.substrateSelection)
        if self.materialError or substrate_selection["error"]:
            self._status = cast(str, self.materialError) or substrate_selection["error"]
            self.changed.emit()
            return False
        snapshot = self._source.session.snapshot()
        design = deepcopy(snapshot.design)
        self._digest = _digest(design)
        if configuration is not None:
            design.setdefault("workflow_settings", {})["optimization"] = deepcopy(configuration)
        self._source_revision = snapshot.design_revision
        self._token += 1
        self._cancel = threading.Event()
        self._busy = True
        self._stale = False
        self._status = "准备冻结快照…"
        self._result = "正在计算…"
        self._technical = ""
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
        substrate = {**substrate, **(self._substrate or {})}

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
        def name_pair(row, catalog):
            material = next((m for m in catalog if m["id"] == row.get("material_id")), None)
            if not material:
                return "材料不可用"
            optical = next((o for o in material["optical_sets"] if o["id"] == row.get("optical_set_id")), None)
            return material["name"] + " / " + (optical["name"] if optical else "数据集不可用")

        side_names = {"front": "正面", "back": "背面"}
        self._source_summary = (
            "冻结条件：" + "、".join(side_names.get(side, str(side)) for side in sides)
            + "；未选面保持固定。\n候选膜料："
            + ("；".join(name_pair(row, self._catalog) for row in rows) or "无")
            + "\n基片：" + name_pair(substrate, self._substrate_catalog)
            + "\n目标：" + target_text
            + "\n未知 k：" + ("按零假设" if synthesis.get("assume_unknown_k_zero") is True else "不按零" if synthesis.get("assume_unknown_k_zero") is False else "未确认")
            + f"；单层最小非零厚度 {setting('minimum_nonzero_thickness_nm', ' nm')}；单层最大厚度 {setting('max_layer_thickness_nm', ' nm')}。"
            + (f"\n进一步验证（合成探测）：每种子逻辑评价上限 {self._probe_max_evaluations} 次（含审计）；种子 {self.probeSeeds}。" if self._verification_enabled else "\n快速评估：不运行合成探测或独立候选审计。")
            + "\n评估不受原优化的层数与总厚度预算限制；制造良率未评估。"
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
                deepcopy(self._substrate),
                self._probe_max_evaluations if self._verification_enabled else 0,
                tuple(self._probe_seeds),
                self._verification_enabled,
            )
        )
        return True

    @Slot()
    def invalidateDraft(self):
        if self._digest and not self._stale:
            self._stale = True
            self._status = "优化设置已变化；结果已过期，请重新评估。"
            self.changed.emit()

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
            self._status = "评估中：" + str(getattr(event, "message", "正在计算"))
            physical = getattr(event, "physical_evaluations", None)
            points = getattr(event, "wavelength_points", None)
            if physical is not None:
                self._status += f"；物理光谱计算 {physical} 次"
            if points is not None:
                self._status += f"；波长点 {points} 个"
            self._status += "。"
            self.changed.emit()

    @Slot(int, object, object)
    def _on_finished(self, token, result, error):
        if token != self._token or not self._busy:
            return
        self._source_changed()
        self._busy = False
        if error is not None:
            self._status = (
                "已取消。" if isinstance(error, InterruptedError) else "评估未完成：" + _friendly_error(error)
            )
            self._result = "未取得计算结果。"
            self._technical = "原始错误：" + str(error)
        else:
            state = result.get("state")
            self._status = {"completed": "评估完成。", "cancelled": "评估已取消。", "failed": "评估未完成。"}.get(state, "评估状态未知。")
            if state != "completed" and result.get("message"):
                self._status += " " + _friendly_error(result["message"])

            source_check = result.get("_source_check", "unavailable")
            if source_check == "changed":
                self._stale = True
                self._status += " 材料源在评估期间变化。"
            elif source_check == "unverified":
                self._stale = True
                self._status += " 材料源未能复核；结果不可视作当前来源。请查看技术详情。"
            elif source_check == "current":
                self._status += " 材料源在完成时已复核；此后外部材料变更请重新评估。"
            self._result = _format_summary(result)
            self._technical = ("冻结输入摘要：" + str(result.get("input_digest") or "未知")
                               + "\n材料与物理源摘要：" + str(result.get("source_digest") or "未知")
                               + "\n原始状态信息：" + str(result.get("message") or "无")
                               + "\n来源复核详情：" + str(result.get("_source_check_message") or "无")
                               + "\n\n" + _format_result(result))
        if self._stale:
            self._status = "来源已变化，本次结果已过期；请重新评估。" + self._status
        self.changed.emit()


def _fixed_surface_note(item: dict[str, Any], selected: Any) -> str:
    if not isinstance(selected, (list, tuple)) or not selected:
        return "；选面信息未知，固定面未列出"
    surfaces = item.get("surfaces") or {}
    fixed = []
    for side, label in (("front", "正面"), ("back", "背面")):
        row = surfaces.get(side)
        if side in selected or not isinstance(row, dict) or row.get("active") is not True:
            continue
        count, thickness = row.get("layer_count"), row.get("total_thickness_nm")
        amount = f"{thickness:.4g} nm" if isinstance(thickness, (int, float)) else "厚度未统计"
        fixed.append(f"{label} {count if count is not None else '未知'} 层 / {amount}")
    return "；未选固定面：" + "、".join(fixed) if fixed else ""


def _format_summary(result: dict[str, Any]) -> str:
    state = result.get("state")
    if state not in (None, "completed"):
        work = result.get("work") or {}
        partial = ""
        if "physical_evaluations" in work:
            partial = (f"\n已记录物理光谱计算 {work.get('physical_evaluations', '未知')} 次，"
                       f"波长点 {work.get('wavelength_points', '未知')} 个；中断时工作量可能不完整。")
        if work.get("actual_work_complete") is False:
            partial += "\n工作量统计不完整。"
        for outcome in work.get("probe_outcomes") or []:
            partial += (f"\n种子 {outcome.get('seed', '未知')}：已记录逻辑评价 "
                        f"{outcome.get('evaluations', outcome.get('logical_evaluations_observed', '未知'))} / {outcome.get('requested_max_evaluations', '未知')} 次，"
                        f"物理光谱计算 {outcome.get('physical_evaluations', '未知')} 次；工作量可能不完整。")
        return "评估未完成，尚无完整结果；请检查状态并重试。" + partial
    work = result.get("work") or {}
    mode = work.get("assessment_mode")
    fast = (mode == "fast_screen" or (mode is None and result.get("_audit_candidates") is False
                                        and work.get("probe_runs") == 0))
    lines = ["评估结果（有限采样，不是连续域证书）"]
    if not fast:
        lines.append("审计点按指标违约程度选择，不等同全谱极值；若全部通过、违约量相同，显示的可能只是代表点。")
    for item in result.get("bounds", []):
        kind = item.get("kind")
        label = {"hr_thickness_lp": "厚度必要界", "hr_layer_dp": "层数必要界"}.get(kind, "必要界")
        value = item.get("value_nm") if kind == "hr_thickness_lp" else item.get("value_layers")
        unit = "nm" if kind == "hr_thickness_lp" else "层"
        reason = item.get("reason", "")
        if value is not None:
            text = f"{value:.4g} {unit}（条件必要界，非充分条件）"
        elif item.get("applicability") == "not_applicable":
            text = "不适用" + ("：此界仅支持逐点高反射目标" if reason == "requires_pointwise_hr" else "：当前物理条件不满足此界的适用条件")
        else:
            text = "未算出；不能据此判断不可行"
        excluded = item.get("excluded_through")
        if isinstance(excluded, int):
            text += f"；枚举范围内已排除至 {excluded} 层"
        if item.get("conditional_on_assumption") is True:
            text += "；依赖未知 k 按零假设"
        lines.append(f"• {label}：{text}")
    if not result.get("bounds"):
        lines.append("• 当前条件下没有可用的必要界；不能据此判断不可行。")
    reports = result.get("candidate_reports") or []
    current = next((report for report in reports if report.get("source", "candidate") == "candidate"), None)
    if current:
        item = current
        layers = item.get("layer_count")
        thickness = item.get("total_thickness_nm")
        quantities = (f"{layers} 层 / {thickness:.4g} nm" if layers is not None and isinstance(thickness, (int, float)) else "资源量未知")
        met = item.get("targets_met")
        outcome = "目标通过" if met is True else "目标未通过" if met is False else "目标是否通过未确定"
        if item.get("state") != "audited":
            outcome = "未取得完整审计" + ("：" + _friendly_error(item["message"]) if item.get("message") else "")
        assumption_note = "；结果依赖未知 k 按零假设" if item.get("conditional_on_assumption") is True else ""
        lines.append(f"• 当前结构审计（所选膜面合计）：{quantities}；{outcome}{assumption_note}{_fixed_surface_note(item, result.get("selected_surfaces"))}。")
        audit = (item.get("audit") or {}) if item.get("state") == "audited" else {}
        for objective in audit.get("objective_status", []):
            metric = objective.get("metric", "指标")
            for kind, label in (("pointwise", "逐点"), ("mean", "均值")):
                condition = objective.get(kind) or {}
                if not condition.get("enabled"):
                    continue
                actual = condition.get("worst_actual")
                actual_text = f"{100 * actual:.5g}%" if isinstance(actual, (int, float)) else "未知"
                if condition.get("mode") == "value":
                    target = condition.get("target_value")
                    criterion = f"目标 {100 * target:.5g}%" if isinstance(target, (int, float)) else "目标未知"
                else:
                    low, high = condition.get("lower_bound"), condition.get("upper_bound")
                    pieces = []
                    if isinstance(low, (int, float)):
                        pieces.append((">" if condition.get("strict_lower") else "≥") + f"{100 * low:.5g}%")
                    if isinstance(high, (int, float)):
                        pieces.append(("<" if condition.get("strict_upper") else "≤") + f"{100 * high:.5g}%")
                    criterion = "、".join(pieces) or "边界未知"
                angle, wavelength = condition.get("worst_angle_deg"), condition.get("worst_wavelength_nm")
                location = f"角度 {angle:g}°" if isinstance(angle, (int, float)) else "角度未知"
                if kind == "pointwise":
                    location += f"，波长 {wavelength:g} nm" if isinstance(wavelength, (int, float)) else "，波长未知"
                passed = condition.get("targets_met")
                outcome = "达标" if passed is True else "未达标" if passed is False else "达标状态未知"
                detail = "最不利审计点" if kind == "pointwise" else "固定密网格积分均值（最不利角度）"
                low_nm, high_nm = objective.get("wavelength_min_nm"), objective.get("wavelength_max_nm")
                band = f"{low_nm:g}–{high_nm:g} nm" if isinstance(low_nm, (int, float)) and isinstance(high_nm, (int, float)) else "波段未知"
                lines.append(f"  目标 {objective.get('objective_index', 0) + 1} {metric}（{band}）{label}：{actual_text}；{criterion}；{outcome}。{detail}：{location}。")
        error = item.get("max_normalized_error") if item.get("state") == "audited" else None
        if isinstance(error, (int, float)):
            lines.append(f"  最大归一化违约量：{error:.4g}（不是反射率）。")
    else:
        lines.append("• 当前结构：未进行独立审计。" if fast else "• 当前结构：未取得完整审计。")
    nodes = result.get("recommended_budget") or result.get("pareto_witnesses") or []
    if nodes:
        lines.append("• 已找到以下有限采样审计见证（所选膜面合计层数 / 厚度，不是连续域证书）：")
        for item in nodes:
            conditional = item.get("conditional_on_assumption")
            evidence = item.get("absorption_assumption")
            matching = isinstance(evidence, dict) and evidence.get("conditional_on_assumption") is conditional
            assumption_note = ("；依赖未知 k 按零假设" if conditional is True and matching
                         else "；未使用未知 k 按零假设" if conditional is False and matching
                         else "；吸收假设状态未知")
            origin = (f"探测种子 {item.get('probe_seed')}" if item.get("source") == "probe" else "当前结构")
            lines.append(f"  {item.get('layer_count', '?')} 层 / {item.get('total_thickness_nm', '?')} nm（{origin}）{assumption_note}{_fixed_surface_note(item, result.get("selected_surfaces"))}")
    else:
        lines.append("• 快速评估未搜索或审计可行见证；是否存在可行方案未知。" if fast else "• 尚未取得通过审计的可行见证；这不代表不存在可行方案。")
    near = [row for row in reports if row.get("source") == "probe" and row.get("state") == "audited"
            and row.get("targets_met") is False and row.get("within_constraints") is True
            and row.get("within_material_pool") is True
            and isinstance(row.get("max_normalized_error"), (int, float))]
    if near:
        best = min(near, key=lambda row: row["max_normalized_error"])
        conditional = best.get("conditional_on_assumption")
        evidence = best.get("absorption_assumption")
        matching = isinstance(evidence, dict) and evidence.get("conditional_on_assumption") is conditional
        assumption = ("依赖未知 k 按零假设" if conditional is True and matching
                      else "未使用未知 k 按零假设" if conditional is False and matching
                      else "吸收假设状态未知")
        lines.append(f"• 已审计且符合材料及工艺约束的最接近探测候选（所选膜面合计）：{best.get('layer_count', '未知')} 层 / {best.get('total_thickness_nm', '未知')} nm{_fixed_surface_note(best, result.get("selected_surfaces"))}；最大归一化违约量 {best['max_normalized_error']:.4g}；{assumption}（未达标，不是可行见证）。")
        for objective in (best.get("audit") or {}).get("objective_status", []):
            for kind, label in (("pointwise", "逐点"), ("mean", "均值")):
                condition = objective.get(kind) or {}
                actual = condition.get("worst_actual")
                if not condition.get("enabled") or not isinstance(actual, (int, float)):
                    continue
                angle, wavelength = condition.get("worst_angle_deg"), condition.get("worst_wavelength_nm")
                location = f"，角度 {angle:g}°" if isinstance(angle, (int, float)) else ""
                if kind == "pointwise" and isinstance(wavelength, (int, float)):
                    location += f"，波长 {wavelength:g} nm"
                passed = condition.get("targets_met")
                status = "达标" if passed is True else "未达标" if passed is False else "状态未知"
                lines.append(f"  目标 {objective.get('objective_index', 0) + 1} {objective.get('metric', '指标')} {label}最不利审计值 {100 * actual:.5g}%{location}；{status}。")
    work = result.get("work") or {}
    outcomes = work.get("probe_outcomes") or result.get("probe_outcomes") or []
    if outcomes:
        lines.append("• 有限探测工作量（逻辑评价包含审计；不代表搜索穷尽）：")
        for outcome in outcomes:
            state = {"completed": "完成", "not_found": "未找到", "cancelled": "取消", "failed": "失败", "deadline": "到时停止"}.get(outcome.get("state"), "未知")
            reason = str(outcome.get("termination_reason") or "")
            reason_text = {
                "validated_targets_met": "审计目标通过", "evaluation_limit": "达到评价上限",
                "cycle_limit": "达到轮次上限", "time_limit": "达到时间上限",
                "resource_limit": "达到资源上限", "search_exhausted": "搜索结束",
                "training_targets_met": "训练目标通过", "validation_targets_not_met": "审计目标未通过",
                "continuation_local_session_complete": "本次搜索结束", "cancelled": "已取消",
            }.get(reason, "数值或审计失败" if reason.startswith(("numerical_failure:", "validation_failed:")) else "原因未知")
            lines.append(f"  种子 {outcome.get('seed', '?')}：{state}；逻辑评价 {outcome.get('evaluations', outcome.get('logical_evaluations_observed', '未知'))} / {outcome.get('requested_max_evaluations', '?')} 次；物理光谱计算 {outcome.get('physical_evaluations', '未知')} 次；其中审计评价 {outcome.get('validation_evaluations', '未知')} 次；保留 {outcome.get('candidates_retained', '未知')} 个候选；停止原因：{reason_text}。")
            if "evaluations" not in outcome and "logical_evaluations_observed" in outcome:
                lines.append("  逻辑评价数为运行中已记录值，非最终完整统计。")
            if outcome.get("evaluation_window_exceeded") is True:
                lines.append("  实际逻辑评价超出请求窗口；请以记录的实际工作量为准。")
            if outcome.get("actual_work_complete") is False:
                lines.append("  该种子的实际工作量记录不完整。")
        validation = [outcome.get("validation_evaluations") for outcome in outcomes]
        audit_total = sum(validation) if all(type(value) is int for value in validation) else "未知"
        lines.append(f"  探测合计：逻辑评价 {work.get('probe_evaluations', '未知')} 次（其中审计评价 {audit_total} 次），物理光谱计算 {work.get('probe_physical_evaluations', '未知')} 次，波长点 {work.get('probe_wavelength_points', '未知')} 个。")
        if work.get("actual_work_complete") is False:
            lines.append("  工作量统计不完整，以上仅为已记录部分。")
    elif work.get("probe_runs") == 0:
        lines.append("• 未运行合成探测；未进行独立候选审计。" if fast else "• 未运行探测；当前结构审计状态见上。")
    if "physical_evaluations" in work:
        lines.append(f"• 全部物理光谱计算 {work.get('physical_evaluations', '未知')} 次，波长点 {work.get('wavelength_points', '未知')} 个（含探测）。")
    lines.append("• 通用层数与厚度预测：未知；目前没有经过校准的预测模型。不能将必要界或审计实例当作最小值，审计实例也不是预测。")
    return "\n".join(lines)


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
