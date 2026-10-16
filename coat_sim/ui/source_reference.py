"""Read-only literature presentation and transactional ZnS reference scenarios.

This UI module never supplies material coefficients or runs a thermal model.
All calculations and their validation are delegated to core.material_physics.
"""
from copy import deepcopy
import weakref
from PySide6.QtCore import QObject, Property, Signal, Slot

PROPERTY_LABELS = {
    "refractive_index_n": "折射率 n", "extinction_coefficient_k": "消光系数 k",
    "QCM_reference_density": "QCM 参考密度（仪器设置）", "QCM_z_ratio": "QCM Z-ratio（仪器设置）",
    "theoretical_density": "理论块材密度", "deposition_rate_range": "供应商沉积速率建议（非本机标定）",
    "thermal_conductivity": "薄膜热导率", "vendor_vapor_pressure": "供应商蒸气压锚点",
    "substrate_temperature_guidance": "供应商基片温度建议", "substrate_temperature_categories": "供应商基片温度分类",
    "evaporation_temperature": "蒸发温度（定义冲突）", "gibbs_formation_liquid_zinc": "液态 Zn 标准态关系（排除于气相模型）",
    "temperature_K": "温度 / K", "measurement_temperature_C": "测量温度 / °C",
    "substrate_temperature_C": "基片温度 / °C", "original_temperature_C": "原始温度 / °C",
    "wavelength_um": "波长 / µm", "thickness_nm": "膜厚 / nm", "state": "材料状态",
    "material_state": "材料状态", "pressure_definition": "压力定义", "supplier": "供应商",
    "product": "产品", "method": "方法", "original_pressure_mbar": "原始压力 / mbar",
    "pressure_mbar": "压力 / mbar", "application_wavelength_um": "应用波长 / µm",
}
VALUE_LABELS = {
    "zns_vendor_pressure_v1": "供应商双锚点压力插值",
    "zns_ri8301_gas_v1": "RI8301 Zn(g) + S₂(g) 条件平衡 / HK",
    "unspecified_total_or_species": "来源未定义总压或物种分压",
    "species_partial_pressures": "已明确的 Zn / S₂ 物种分压",
    "pressure_ratio": "显式指定物种分压比",
    "sealed_stoichiometric_gas": "封闭化学计量气体（仅压力）",
    "congruent_outgoing": "条件化学计量逸出（显式物种系数）",
    "ideal_effusion": "显式理想小孔逸出（α=τ=1）",
    "source_snapshot": "当前源内嵌文献快照",
    "evaporated Patinal film": "蒸发 Patinal 薄膜",
    "instrument_setting_not_film_density": "仪器设置值，并非实测薄膜密度",
    "QCM_instrument_setting": "QCM 仪器设置",
    "supplier_theoretical_bulk_material": "供应商理论块材；非粉床或薄膜",
    "see_conditions_and_source_card": "见条件与原始来源，未独立界定",
    "ZnS_film": "ZnS 薄膜",
}
STATUS_LABELS = {"literature_reference": "文献参考（非本机标定）", "unknown": "未知，不能填零",
                 "quarantined": "已隔离，禁止计算采用", "excluded_from_gas_model": "排除于气相模型"}


def display_value(value):
    if value is None:
        return "未知 / 未给出"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (float, int)):
        return format(value, ".8g")
    if isinstance(value, list):
        return "；".join(display_value(item) for item in value)
    if isinstance(value, dict):
        return "；".join(PROPERTY_LABELS.get(key, key) + "：" + display_value(item) for key, item in value.items())
    return VALUE_LABELS.get(str(value), str(value))


def reference_sections(reference):
    if not reference:
        return []
    sources = {row["id"]: row for row in reference.get("sources", [])}
    sections = []
    for row in reference.get("property_records", []):
        source = sources.get(row.get("source_id"), {})
        unit = row.get("unit", "")
        unit = "（无量纲）" if unit == "1" else unit.replace("/m3", "/m³").replace("/cm3", "/cm³")
        body = ["数值：" + display_value(row.get("value")) + " " + unit,
                "状态：" + STATUS_LABELS.get(row.get("status"), str(row.get("status", "未知"))),
                "条件：" + display_value(row.get("conditions", {}))]
        if row.get("state"):
            body.append("材料状态：" + display_value(row["state"]))
        if row.get("uncertainty"):
            body.append("不确定度：" + display_value(row["uncertainty"]))
        if row.get("notes"):
            body.append("限制：" + row["notes"])
        sections.append({"title": PROPERTY_LABELS.get(row.get("property"), row.get("property", "记录")) + " · " + row.get("record_id", ""),
                         "body": "\n".join(body), "source": row.get("source_id") or "无可用来源", "url": source.get("url", ""),
                         "warning": row.get("status") in ("unknown", "quarantined")})
    for key, model in reference.get("models", {}).items():
        sections.append({"title": "限域模型 · " + key,
                         "body": "有效温度 / K：" + "–".join(display_value(value) for value in model.get("temperature_range_K", [])) + "（含端点；禁止外推）\n" + display_value(model.get("limitations", [])),
                         "source": "、".join(model.get("source_ids", [])), "url": "", "warning": False})
    for row in reference.get("quarantined", []):
        sections.append({"title": "隔离 / 排除 · " + PROPERTY_LABELS.get(row.get("property"), row.get("property", "记录")),
                         "body": display_value(row), "source": row.get("source_id", ""),
                         "url": sources.get(row.get("source_id"), {}).get("url", ""), "warning": True})
    sections.append({"title": "未取得的物性与动力学数据（未知不是零）", "body": display_value(reference.get("unfilled_not_zero", [])), "source": "", "url": "", "warning": True})
    for source in reference.get("sources", []):
        sections.append({"title": "来源 " + source["id"] + " · " + source["title"],
                         "body": "定位：" + source.get("locator", "") + "\n" + source.get("limitations", source.get("support", "")),
                         "source": source["id"] + " · 打开原始来源", "url": source.get("url", ""), "warning": False})
    return sections


class ReferenceCalculatorBridge(QObject):
    changed = Signal()

    def __init__(self, source):
        super().__init__(source)
        # The QObject parent owns this calculator. A strong Python backreference
        # would keep an editor cycle alive until unrelated worker-thread GC.
        self.source = weakref.proxy(source)
        self._draft = {}
        self._result = None
        self._error = ""
        self._status = "尚未计算"
        self._active = False
        self.begin()

    @Property(bool, notify=changed)
    def available(self):
        reference = (self.source._material or {}).get("reference_data", {})
        return reference.get("material_id") == "ZnS" and "zns_ri8301_gas_v1" in reference.get("models", {})

    @Property("QVariantMap", notify=changed)
    def fields(self):
        return dict(self._draft)

    @Property(str, notify=changed)
    def error(self):
        return self._error

    @Property(str, notify=changed)
    def status(self):
        return self._status

    @Property(bool, notify=changed)
    def hasResult(self):
        return self._result is not None

    @Property("QVariantMap", notify=changed)
    def result(self):
        return deepcopy(self._result or {})

    @Property("QVariantList", notify=changed)
    def resultSections(self):
        return result_sections(self._result)

    @Property("QVariantList", notify=changed)
    def referenceSections(self):
        return reference_sections((self.source._material or {}).get("reference_data"))

    @Property(bool, notify=changed)
    def configured(self):
        return self.source._thermochemistry is not None

    @Slot()
    def begin(self):
        config = self.source._thermochemistry or {}
        from .source_materials import _format
        self._draft = {
            "model_id": config.get("model_id", "zns_vendor_pressure_v1"),
            "temperature_K": _format(config.get("temperature_K")),
            "closure": config.get("closure") or "",
            "pressure_ratio": _format(config.get("pressure_ratio")),
            "alpha_Zn": _format(config.get("alpha", {}).get("Zn")),
            "alpha_S2": _format(config.get("alpha", {}).get("S2")),
            "transmission_Zn": _format(config.get("transmission", {}).get("Zn")),
            "transmission_S2": _format(config.get("transmission", {}).get("S2")),
            "emitting_area_mm2": _format(config["emitting_area_m2"] * 1e6 if config.get("emitting_area_m2") is not None else None),
            "area_basis": config.get("area_basis", "explicit"),
        }
        self._active = True
        self._result = None
        self._error = ""
        self._status = "未计算。留空表示未知；只有明确选择理想情景才采用 α=τ=1"
        self.changed.emit()

    @Slot()
    def cancel(self):
        self.begin()
        self._active = False
        self._status = "已取消；当前源保存的参考场景未改变"
        self.changed.emit()

    @Slot(str, str)
    def setField(self, key, value):
        if key not in self._draft or self._draft[key] == value:
            return
        previous = self._draft[key]
        self._draft[key] = value
        if key == "closure" and value == "ideal_effusion":
            for coefficient in ("alpha_Zn", "alpha_S2", "transmission_Zn", "transmission_S2"):
                self._draft[coefficient] = "1"
        elif key == "closure" and previous == "ideal_effusion":
            for coefficient in ("alpha_Zn", "alpha_S2", "transmission_Zn", "transmission_S2"):
                self._draft[coefficient] = ""
        # Exit/aperture area cannot be inferred from source charge geometry.
        if key == "closure" and value in ("ideal_effusion", "congruent_outgoing"):
            self._draft["area_basis"] = "explicit"
        self.invalidate("输入已修改；旧结果已清除，请重新计算")

    @Slot()
    def sourceChanged(self):
        self.invalidate("源、材料或几何已变更；旧结果已清除，请重新计算")

    def invalidate(self, message):
        self._result = None
        self._error = ""
        self._status = message
        self.changed.emit()

    def draftData(self):
        from .source_materials import _float
        from coat_sim.core.material_physics import validate_thermochemistry
        fields = self._draft
        def number(key):
            return _float(fields[key], key) if fields[key].strip() else None
        gas = fields["model_id"] == "zns_ri8301_gas_v1"
        closure = (fields["closure"] or None) if gas else None
        outgoing = gas and closure != "sealed_stoichiometric_gas"
        basis = fields["area_basis"] if outgoing else "explicit"
        area = number("emitting_area_mm2") if outgoing and basis == "explicit" else None
        config = {"model_id": fields["model_id"], "temperature_K": number("temperature_K"),
                  "closure": closure, "pressure_ratio": number("pressure_ratio") if closure == "pressure_ratio" else None,
                  "alpha": {key: number("alpha_" + key) if outgoing else None for key in ("Zn", "S2")},
                  "transmission": {key: number("transmission_" + key) if outgoing else None for key in ("Zn", "S2")},
                  "area_basis": basis, "emitting_area_m2": area * 1e-6 if area is not None else None}
        return validate_thermochemistry(config)

    @Slot(result=bool)
    def calculate(self):
        self._result = None
        self._error = ""
        if not self.available:
            self._error = "当前源没有完整 ZnS 文献参考快照；自定义材料名称不能启用此计算"
            self.changed.emit()
            return False
        try:
            from coat_sim.core.material_physics import evaluate_source_thermochemistry
            source = self.source._compose_document()
            source["thermochemistry"] = self.draftData()
            self._result = evaluate_source_thermochemistry(source)
            labels = {"missing_inputs": "输入不完整；仅显示有依据的可计算项目", "out_of_domain": "超出文献有效温区，禁止外推", "reference_prediction": "文献条件参考结果（非机器标定 / 非沉积速率）", "disabled": "参考计算未启用"}
            self._status = labels.get(self._result.get("status"), "参考结果")
        except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
            self._error = str(exc)
            self._status = "计算失败；无有效结果"
            self.changed.emit()
            return False
        self.changed.emit()
        return True

    @Slot(result=bool)
    def apply(self):
        if not self.available:
            self._error = "当前材料不支持 ZnS 文献参考计算"
            self.changed.emit()
            return False
        try:
            config = self.draftData()
        except (ValueError, TypeError, KeyError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return False
        self.source._thermochemistry = deepcopy(config)
        self.source._edited()
        self.source._set_status("已将 ZnS 参考场景应用到当前源设计；保存源文件后才会写入磁盘。工艺沉积速率未改变")
        self._active = False
        self.changed.emit()
        return True

    @Slot()
    def clear(self):
        if self.source._thermochemistry is not None:
            self.source._thermochemistry = None
            self.source._edited()
        self.begin()
        self._status = "已从当前源设计移除参考场景；文献材料快照保留"
        self.changed.emit()


def result_sections(report):
    """Labels retain units and unknown values; no numerical interpretation here."""
    if not report:
        return []
    rows = []
    def add(title, body, warning=False, url="", source=""):
        rows.append({"title": title, "body": body, "warning": warning, "url": url, "source": source})
    add("计算身份与适用域", "模型：" + display_value(report.get("model_id"))
        + "\n温度 / K：" + display_value(report.get("temperature_K"))
        + "\n有效温区 / K：" + display_value(report.get("valid_temperature_range_K"))
        + "\n闭合情景：" + display_value(report.get("closure"))
        + "\n数据来源：" + display_value(report.get("input_origin")))
    if report.get("missing_inputs"):
        add("待补充 / 未知参数", display_value(report["missing_inputs"]), True)
    pressure = report.get("pressure")
    if pressure:
        labels = {"model_id": "参考模型", "temperature_K": "温度 / K",
                  "allowed_for_species_HK": "允许作为物种 HK 输入",
                  "pressure_standard_Pa": "标准态压力 / Pa",
                  "reported_gibbs_error_J_mol": "原文 Gibbs 误差幅度 / J·mol⁻¹",
                  "uncertainty_note": "误差解释与限制",
                  "vendor_pressure_Pa": "供应商参考压力 / Pa（总压/分压未定义）", "pressure_definition": "压力定义", "total_pressure_Pa": "参考总压力 / Pa",
                  "partial_pressures_Pa": "物种分压 / Pa", "pressure_ratio": "pZn / pS₂",
                  "K_dissociation": "无量纲解离平衡常数 K", "gibbs_formation_J_mol": "生成 Gibbs 能 / J·mol⁻¹",
                  "error_sensitivity_factor": "ΔG 误差敏感度倍率（非统计置信区间）",
                  "total_pressure_sensitivity_Pa": "总压条件敏感度范围 / Pa（非统计置信区间）",
                  "partial_pressure_sensitivity_Pa": "物种分压条件敏感度范围 / Pa（非统计置信区间）"}
        add("平衡 / 供应商参考压力", "\n".join(labels.get(key, key) + "：" + display_value(value) for key, value in pressure.items()))
    flux_labels = {
        "particle_flux_m2_s": "物种粒子通量 / m⁻²·s⁻¹", "element_flux_atoms_m2_s": "元素原子通量 / atoms·m⁻²·s⁻¹",
        "species_mass_flux_kg_m2_s": "物种质量通量 / kg·m⁻²·s⁻¹", "mass_flux_kg_m2_s": "总质量通量 / kg·m⁻²·s⁻¹",
        "s_to_zn_atomic_ratio": "S/Zn 原子比", "particle_rate_s": "物种总粒子率 / s⁻¹",
        "element_rate_atoms_s": "元素总原子率 / atoms·s⁻¹", "species_mass_rate_kg_s": "物种总质量率 / kg·s⁻¹",
        "mass_rate_kg_s": "总质量率 / kg·s⁻¹",
        "mass_flux_sensitivity_kg_m2_s": "总质量通量条件敏感度范围 / kg·m⁻²·s⁻¹",
        "mass_rate_sensitivity_kg_s": "总质量率条件敏感度范围 / kg·s⁻¹",
        "species_mass_flux_sensitivity_kg_m2_s": "物种质量通量条件敏感度范围 / kg·m⁻²·s⁻¹",
        "species_mass_rate_sensitivity_kg_s": "物种质量率条件敏感度范围 / kg·s⁻¹",
        "sensitivity_note": "敏感度界限（非统计置信区间）"}
    for key, title in (("pre_transmission", "条件源通量（透过前）"), ("post_transmission", "条件源通量（透过后）")):
        flux = report.get(key)
        if flux:
            add(title, "\n".join(flux_labels.get(name, name) + "：" + display_value(value) for name, value in flux.items()))
    if report.get("resolved_alpha") is not None or report.get("resolved_transmission") is not None:
        add("实际采用的显式系数", "有效逸出 α：" + display_value(report.get("resolved_alpha")) + "\n透过 τ：" + display_value(report.get("resolved_transmission")))
    if report.get("area"):
        area = report["area"]
        add("采用的物理面积与定义", "面积 / m²：" + display_value(area.get("value_m2")) + "\n面积依据：" + str(area.get("basis")) + "\n" + str(area.get("meaning", "")))
    if report.get("assumptions"):
        add("显式假设与适用条件", "\n".join(report["assumptions"]))
    if report.get("warnings"):
        add("限制 / 不确定度", "\n".join(report["warnings"]), True)
    provenance = report.get("provenance", {})
    if provenance:
        add("证据与算法来源", display_value({key: value for key, value in provenance.items() if key != "sources"}))
        for source in provenance.get("sources", []):
            add(source.get("id", "来源") + " · " + source.get("title", ""), source.get("locator", ""), url=source.get("url", ""), source="打开原始来源")
    return rows
