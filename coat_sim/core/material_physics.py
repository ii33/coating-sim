"""Bounded, sourced ZnS pressure and conditional outgoing-flux references.

This independent module never changes spatial source weights, process nm/s
normalization, machine settings, or calibrated material models. Mathematical
kernels adapt T05 source_reference.py (inverse-T pressure and HK) and the R2
qcm_flux.py species_elements/gas_impingement accounting. Full attribution and
source conditions live in resources/zns_reference.json. Synthetic heat-solvers
and material-response examples from the research library are not imported.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from functools import lru_cache
import json
import math
from pathlib import Path

REFERENCE_MODELS = ("zns_vendor_pressure_v1", "zns_ri8301_gas_v1")
CLOSURES = ("pressure_ratio", "sealed_stoichiometric_gas", "congruent_outgoing", "ideal_effusion")
SPECIES = ("Zn", "S2")
_RESOURCE = Path(__file__).with_name("resources") / "zns_reference.json"
N_A = 6.02214076e23
K_B = 1.380649e-23
R = N_A * K_B
MOLAR_MASS = {"Zn": 0.06538, "S2": 0.06412}
ELEMENT_MASS = {"Zn": 0.06538, "S": 0.03206}


@lru_cache(maxsize=1)
def _packaged_json():
    """Cache immutable strings, never a caller-mutable dataset object."""
    payload = _RESOURCE.read_text(encoding="utf-8")
    canonical = json.dumps(json.loads(payload), ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False)
    return payload, canonical


def reference_material_data(material_id="ZnS"):
    """Return an independent, provenance-bearing supported reference snapshot."""
    if not isinstance(material_id, str) or material_id.casefold() != "zns":
        raise ValueError("仅提供 ZnS 文献参考数据")
    return json.loads(_packaged_json()[0])


def validate_reference_data(data):
    """Accept only the complete supported immutable literature snapshot.

    Canonical JSON comparison deliberately distinguishes booleans from numeric
    constants and rejects unknown edits. User-owned material names and spatial
    coefficients are editable outside this snapshot. New dataset versions need
    an explicit supported implementation, not silently substituted coefficients.
    """
    if not isinstance(data, Mapping):
        raise ValueError("reference_data 须为完整的受支持 ZnS 文献参考对象")
    try:
        canonical = json.dumps(dict(data), ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False)
        expected = _packaged_json()[1]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("文献参考数据须为有限 JSON 值") from exc
    if canonical != expected:
        raise ValueError("reference_data 与受支持版本不一致；文献快照不可修改或冒充已校准数据")
    return deepcopy(dict(data))


def _number(value, label, *, positive=False):
    try:
        if isinstance(value, bool):
            raise TypeError()
        value = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} 必须是有限数值") from exc
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(f"{label} 必须是有限{'正' if positive else ''}数值")
    return value


def _finite_outputs(value):
    """Guard computed quantities as well as inputs against overflow."""
    if isinstance(value, Mapping):
        for item in value.values():
            _finite_outputs(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            _finite_outputs(item)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("计算通量/面积/误差范围超出有限数值范围；请检查输入尺度")
    return value


def _coefficients(value, label):
    value = {} if value is None else value
    if not isinstance(value, Mapping) or set(value) - set(SPECIES):
        raise ValueError(f"{label} 仅允许 Zn 和 S2 两个物种")
    result = {}
    for species in SPECIES:
        v = value.get(species)
        if v is not None:
            v = _number(v, f"{label}.{species}")
            if not 0 <= v <= 1:
                raise ValueError(f"{label}.{species} 必须在 0–1 之间，未知请留空")
        result[species] = v
    return result


def validate_thermochemistry(config):
    """Validate an optional JSON config, retaining every unknown coefficient.

    Choosing ideal_effusion is the explicit ideal-alpha/ideal-transmission
    scenario. Null coefficients are retained in the saved config; resolved ones
    are disclosed separately in the report. It is never a default closure.
    """
    if config is None:
        return None
    fields = {"model_id", "temperature_K", "closure", "pressure_ratio", "alpha",
              "transmission", "emitting_area_m2", "area_basis"}
    if not isinstance(config, Mapping) or set(config) - fields:
        raise ValueError("thermochemistry 包含未知字段或不是 JSON 对象")
    model = config.get("model_id")
    if model not in REFERENCE_MODELS:
        raise ValueError("未知文献热化学 model_id")
    closure = config.get("closure")
    if closure is not None and closure not in CLOSURES:
        raise ValueError("未知 ZnS 分压闭合条件")
    result = {"model_id": model, "temperature_K": None, "closure": closure,
              "pressure_ratio": None, "alpha": _coefficients(config.get("alpha"), "alpha"),
              "transmission": _coefficients(config.get("transmission"), "transmission"),
              "emitting_area_m2": None, "area_basis": config.get("area_basis", "explicit")}
    for field in ("temperature_K", "pressure_ratio", "emitting_area_m2"):
        if config.get(field) is not None:
            result[field] = _number(config[field], field, positive=True)
    if result["area_basis"] not in ("explicit", "source_geometry"):
        raise ValueError("area_basis 须为 explicit 或 source_geometry")
    if result["area_basis"] == "source_geometry" and result["emitting_area_m2"] is not None:
        raise ValueError("显式面积与源几何面积不能同时指定")
    if result["pressure_ratio"] is not None and closure != "pressure_ratio":
        raise ValueError("pressure_ratio 数值仅用于 pressure_ratio 闭合")
    if closure == "ideal_effusion":
        for field in ("alpha", "transmission"):
            if any(v not in (None, 1.0) for v in result[field].values()):
                raise ValueError("理想小孔逸出情景要求显式 α=τ=1；非理想系数请选择其他闭合")
    if closure == "congruent_outgoing":
        for field in ("alpha", "transmission"):
            if any(v == 0 for v in result[field].values() if v is not None):
                raise ValueError("化学计量逸出闭合要求正的 alpha/transmission；零系数使分压比未定或奇异")
    if closure in ("ideal_effusion", "congruent_outgoing") and result["area_basis"] == "source_geometry":
        raise ValueError("逸出模型须填写实际出口/小孔面积，不能将源料几何面积当出口面积")
    return result


def _model(data, model_id):
    return data["models"][model_id]


def _in_domain(temperature_K, model):
    temperature_K = _number(temperature_K, "temperature_K", positive=True)
    low, high = model["temperature_range_K"]
    if not low <= temperature_K <= high:
        raise ValueError(f"温度超出文献有效范围 {low:g}–{high:g} K；禁止外推")
    return temperature_K


def vendor_pressure_pa(temperature_K, *, reference_data=None):
    """Two original IR02 anchors; logarithmic interpolation in inverse T only.

    This scalar has unspecified total/species meaning and must never be supplied
    as a species pressure to Hertz–Knudsen. No derived enthalpy is asserted.
    """
    data = reference_material_data() if reference_data is None else validate_reference_data(reference_data)
    model = _model(data, "zns_vendor_pressure_v1")
    temperature_K = _in_domain(temperature_K, model)
    a, b = model["coefficients"]["A"], model["coefficients"]["B_K"]
    return 10.0 ** (a - b / temperature_K)


def _resolved_coefficients(config):
    if config["closure"] == "ideal_effusion":
        return {s: 1.0 for s in SPECIES}, {s: 1.0 for s in SPECIES}
    return deepcopy(config["alpha"]), deepcopy(config["transmission"])


def _pressure_ratio(config, alpha, transmission):
    closure = config["closure"]
    if closure == "pressure_ratio":
        return config["pressure_ratio"]
    if closure == "sealed_stoichiometric_gas":
        return 2.0
    if closure in ("congruent_outgoing", "ideal_effusion"):
        if any(alpha[s] is None or transmission[s] is None for s in SPECIES):
            return None
        # Conditional outgoing stoichiometry: tau_Zn alpha_Zn p_Zn/sqrt(M_Zn)
        # = 2 tau_S2 alpha_S2 p_S2/sqrt(M_S2), NOT a surface reaction law.
        log_ratio = (math.log(2.0) + math.log(alpha["S2"]) + math.log(transmission["S2"])
                     - math.log(alpha["Zn"]) - math.log(transmission["Zn"])
                     + 0.5 * math.log(MOLAR_MASS["Zn"] / MOLAR_MASS["S2"]))
        try:
            return _number(math.exp(log_ratio), "derived pressure_ratio", positive=True)
        except OverflowError as exc:
            raise ValueError("逸出系数对应的分压比超出可计算范围") from exc
    return None


def zns_equilibrium_pressures(temperature_K, pressure_ratio, *, reference_data=None):
    """RI8301 gas branch with an explicitly supplied p_Zn/p_S2 closure.

    G_f is formation from gas. Dissociation G_d=-G_f and therefore
    K_d=exp(G_f/RT)=(p_Zn/p0)*sqrt(p_S2/p0). The 1 atm reference is 101325 Pa.
    The liquid-Zn equation is deliberately not used at any temperature.
    """
    data = reference_material_data() if reference_data is None else validate_reference_data(reference_data)
    model = _model(data, "zns_ri8301_gas_v1")
    t = _in_domain(temperature_K, model)
    ratio = _number(pressure_ratio, "pressure_ratio", positive=True)
    coefficients = model["gibbs_formation_kcal_mol"]
    gf = (coefficients["a"] + coefficients["b_per_K"] * t) * 4184.0
    log_k = gf / (R * t)
    # Log form stays well-behaved for very unequal explicitly given ratios.
    log_s2 = math.log(model["pressure_standard_Pa"]) + 2.0 / 3.0 * (log_k - math.log(ratio))
    try:
        partial = {"S2": math.exp(log_s2), "Zn": math.exp(log_s2 + math.log(ratio))}
        for species, value in partial.items():
            _number(value, f"partial pressure {species}", positive=True)
    except OverflowError as exc:
        raise ValueError("给定分压比导致分压超出可计算范围") from exc
    error_j = coefficients["reported_uncertainty"] * 4184.0
    factor = math.exp(2.0 * error_j / (3.0 * R * t))
    total = sum(partial.values())
    return _finite_outputs({"model_id": model["model_id"], "temperature_K": t,
            "pressure_definition": "species_partial_pressures", "allowed_for_species_HK": True,
            "pressure_standard_Pa": model["pressure_standard_Pa"], "partial_pressures_Pa": partial,
            "total_pressure_Pa": total, "pressure_ratio": ratio,
            "K_dissociation": math.exp(log_k), "gibbs_formation_J_mol": gf,
            "reported_gibbs_error_J_mol": error_j, "error_sensitivity_factor": factor,
            "total_pressure_sensitivity_Pa": [total / factor, total * factor],
            "partial_pressure_sensitivity_Pa": {s: [v / factor, v * factor] for s, v in partial.items()},
            "uncertainty_note": "原文 ±0.650 kcal/mol 的条件误差敏感性，不是统计置信区间；未含模型、组分、α或τ误差"})


def species_element_fluxes(particle_flux_m2_s):
    """Explicit Zn atoms and S2 molecules -> Zn/S nuclei and total mass (SI)."""
    if not isinstance(particle_flux_m2_s, Mapping) or set(particle_flux_m2_s) != set(SPECIES):
        raise ValueError("需要 Zn 和 S2 两物种粒子通量，不能代入 ZnS 分子")
    fluxes = {s: _number(particle_flux_m2_s[s], f"particle_flux.{s}") for s in SPECIES}
    if any(v < 0 for v in fluxes.values()):
        raise ValueError("单向粒子通量不能为负")
    elements = {"Zn": fluxes["Zn"], "S": 2.0 * fluxes["S2"]}
    mass = {s: fluxes[s] * MOLAR_MASS[s] / N_A for s in SPECIES}
    return _finite_outputs({"particle_flux_m2_s": fluxes, "element_flux_atoms_m2_s": elements,
            "species_mass_flux_kg_m2_s": mass, "mass_flux_kg_m2_s": sum(mass.values()),
            "s_to_zn_atomic_ratio": elements["S"] / elements["Zn"] if elements["Zn"] else None})


def _stage_report(particles, area, sensitivity_factor):
    result = species_element_fluxes(particles)
    result["particle_rate_s"] = None if area is None else {s: v * area for s, v in result["particle_flux_m2_s"].items()}
    result["element_rate_atoms_s"] = None if area is None else {s: v * area for s, v in result["element_flux_atoms_m2_s"].items()}
    result["species_mass_rate_kg_s"] = None if area is None else {s: v * area for s, v in result["species_mass_flux_kg_m2_s"].items()}
    result["mass_rate_kg_s"] = None if area is None else result["mass_flux_kg_m2_s"] * area
    result["mass_flux_sensitivity_kg_m2_s"] = [result["mass_flux_kg_m2_s"] / sensitivity_factor,
                                               result["mass_flux_kg_m2_s"] * sensitivity_factor]
    result["mass_rate_sensitivity_kg_s"] = (None if area is None else
        [result["mass_rate_kg_s"] / sensitivity_factor, result["mass_rate_kg_s"] * sensitivity_factor])
    result["species_mass_flux_sensitivity_kg_m2_s"] = {
        s: [v / sensitivity_factor, v * sensitivity_factor] for s, v in result["species_mass_flux_kg_m2_s"].items()}
    result["species_mass_rate_sensitivity_kg_s"] = (None if area is None else {
        s: [v / sensitivity_factor, v * sensitivity_factor] for s, v in result["species_mass_rate_kg_s"].items()})
    result["sensitivity_note"] = "仅传播 RI8301 原文 Gibbs 误差；温度、闭合比、α、τ及面积固定，不是置信区间"
    return _finite_outputs(result)


def species_outgoing_fluxes(pressure_state, *, alpha, transmission, emitting_area_m2=None):
    """One-way HK outgoing reference, followed by explicit species transmission.

    Reject ambiguous vendor pressure and sealed-gas reports. This is neither a
    net sealed-vessel loss nor substrate growth. The alpha factors are effective
    outgoing coefficients; they have no hidden default value or measured claim.
    """
    if (not isinstance(pressure_state, Mapping)
            or pressure_state.get("model_id") != "zns_ri8301_gas_v1"
            or pressure_state.get("pressure_definition") != "species_partial_pressures"
            or pressure_state.get("allowed_for_species_HK") is not True):
        raise ValueError("HK 仅接受明确 Zn/S2 分压的开放边界情景；原厂未定义蒸气压或封闭气体不可用于逸出")
    t = _in_domain(pressure_state.get("temperature_K"), reference_material_data()["models"]["zns_ri8301_gas_v1"])
    partial = pressure_state.get("partial_pressures_Pa")
    if not isinstance(partial, Mapping) or set(partial) != set(SPECIES):
        raise ValueError("HK 需要 Zn 与 S2 的分压，不能使用总压或 ZnS 分子质量")
    alpha = _coefficients(alpha, "alpha")
    transmission = _coefficients(transmission, "transmission")
    area = None if emitting_area_m2 is None else _number(emitting_area_m2, "emitting_area_m2", positive=True)
    missing = [f"alpha.{s}" for s in SPECIES if alpha[s] is None]
    missing += [f"transmission.{s}" for s in SPECIES if transmission[s] is None]
    particles = None
    if all(alpha[s] is not None for s in SPECIES):
        particles = {}
        for species in SPECIES:
            p = _number(partial[species], f"partial_pressure.{species}")
            if p < 0:
                raise ValueError("物种分压不得为负")
            # HK in molecule/atom number form. Multiplication by M/N_A gives
            # alpha*p*sqrt(M/(2*pi*R*T)) kg m^-2 s^-1, as in T05.
            particles[species] = alpha[species] * p / math.sqrt(2.0 * math.pi * (MOLAR_MASS[species] / N_A) * K_B * t)
    factor = _number(pressure_state.get("error_sensitivity_factor"), "error_sensitivity_factor", positive=True)
    if factor < 1:
        raise ValueError("误差敏感性因子必须至少为 1")
    pre = None if particles is None else _stage_report(particles, area, factor)
    post = None
    if particles is not None and all(transmission[s] is not None for s in SPECIES):
        post = _stage_report({s: particles[s] * transmission[s] for s in SPECIES}, area, factor)
    return {"pre_transmission": pre, "post_transmission": post, "missing_inputs": missing}


def _source_area(profile, config):
    meaning = ("实际小孔/出口面积；不是装料面积" if config["closure"] in ("ideal_effusion", "congruent_outgoing")
               else "均匀指定温度下的实际发射面面积")
    area = config["emitting_area_m2"]
    if config["area_basis"] == "source_geometry" and profile is not None:
        kind = profile.get("kind")
        def size(key, zero=False):
            v = _number(profile.get(key), key, positive=not zero)
            if v < 0 or v > 10000:
                raise ValueError(f"{key} 须在源几何支持的 0–10000 mm 范围")
            return v
        if kind == "rectangle":
            area = size("length_mm") * size("width_mm") * 1e-6
        elif kind == "disk":
            area = math.pi * size("radius_mm") ** 2 * 1e-6
        elif kind == "annulus":
            inner, outer = size("inner_radius_mm", zero=True), size("outer_radius_mm")
            if inner >= outer:
                raise ValueError("环形面积要求内半径小于外半径")
            area = math.pi * (outer ** 2 - inner ** 2) * 1e-6
    if area is not None:
        area = _number(area, "derived emitting_area_m2", positive=True)
    return {"value_m2": area, "basis": config["area_basis"], "meaning": meaning}


def evaluate_source_thermochemistry(source_or_config, *, reference_data=None):
    """Evaluate an opt-in source snapshot or a standalone reference config.

    A source must embed the supported immutable material.reference_data; its
    material name cannot activate a model. Standalone configs deliberately use
    an explicit reference_data argument or the marked built-in reference. No
    process nm/s, geometry weights, material density or input object is changed.
    """
    if source_or_config is not None and not isinstance(source_or_config, Mapping):
        raise ValueError("需要蒸发源对象或 thermochemistry 配置")
    is_source = bool(source_or_config is not None and ("kind" in source_or_config or "thermochemistry" in source_or_config))
    profile = source_or_config if is_source else None
    config = validate_thermochemistry(profile.get("thermochemistry") if is_source else source_or_config)
    report = {"status": "disabled", "model_id": None, "reference_status": "literature_reference",
              "material_id": "ZnS", "temperature_K": None, "valid_temperature_range_K": None,
              "closure": None, "input_origin": "source_snapshot" if is_source else "standalone_builtin_reference",
              "missing_inputs": [], "warnings": [], "assumptions": [], "pressure": None,
              "pre_transmission": None, "post_transmission": None, "area": None, "provenance": None,
              "resolved_alpha": None, "resolved_transmission": None}
    if config is None:
        return report
    if is_source:
        material = profile.get("material")
        if not isinstance(material, Mapping) or material.get("reference_data") is None:
            raise ValueError("热化学源须嵌入 material.reference_data 的 ZnS 文献快照")
        from .materials import validate_material_profile, source_bindable
        if material.get("research_data") is not None or material.get("roles") is not None:
            material = validate_material_profile(material)
        if not source_bindable(material):
            raise ValueError("热化学参考只适用于匹配的蒸发薄膜材料，不适用于基片或舟")
        data = validate_reference_data(material["reference_data"])
        if reference_data is not None and validate_reference_data(reference_data) != data:
            raise ValueError("不能用外部参考数据覆盖蒸发源的不可变文献快照")
    else:
        data = reference_material_data() if reference_data is None else validate_reference_data(reference_data)
        if reference_data is not None:
            report["input_origin"] = "standalone_explicit_reference"
    model = _model(data, config["model_id"])
    report.update(model_id=config["model_id"], temperature_K=config["temperature_K"],
                  valid_temperature_range_K=deepcopy(model["temperature_range_K"]), closure=config["closure"],
                  status="missing_inputs")
    report["provenance"] = {"status": "literature_reference", "dataset_id": data["dataset_id"],
                            "source_ids": deepcopy(model["source_ids"]),
                            "sources": [deepcopy(s) for s in data["sources"] if s["id"] in model["source_ids"]],
                            "algorithm_provenance": deepcopy(data["algorithm_provenance"])}
    report["warnings"] = list(model["limitations"])
    report["assumptions"] = list(model.get("assumptions", []))
    if config["temperature_K"] is None:
        report["missing_inputs"].append("temperature_K")
        return report
    low, high = model["temperature_range_K"]
    if not low <= config["temperature_K"] <= high:
        report["status"] = "out_of_domain"
        report["warnings"].append(f"温度须在 {low:g}–{high:g} K 内；未外推或拼接其他关系")
        return report
    if config["model_id"] == "zns_vendor_pressure_v1":
        pressure = vendor_pressure_pa(config["temperature_K"], reference_data=data)
        report["pressure"] = {"model_id": config["model_id"], "temperature_K": config["temperature_K"],
                              "pressure_definition": "unspecified_total_or_species", "allowed_for_species_HK": False,
                              "total_pressure_Pa": None, "vendor_pressure_Pa": pressure,
                              "partial_pressures_Pa": None, "pressure_ratio": None,
                              "K_dissociation": None, "gibbs_formation_J_mol": None,
                              "error_sensitivity_factor": None, "total_pressure_sensitivity_Pa": None,
                              "partial_pressure_sensitivity_Pa": None}
        report["warnings"].append("此值为原厂未定义蒸气压，既不标为总压也不标为物种分压；不计算 HK 通量")
        report["status"] = "reference_prediction"
        return report
    alpha, transmission = _resolved_coefficients(config)
    report["resolved_alpha"], report["resolved_transmission"] = alpha, transmission
    if config["closure"] is None:
        report["missing_inputs"].append("closure")
        return report
    ratio = _pressure_ratio(config, alpha, transmission)
    if ratio is None:
        if config["closure"] == "pressure_ratio":
            report["missing_inputs"].append("pressure_ratio")
        else:
            report["missing_inputs"].extend(f"{field}.{s}" for field, values in (("alpha", alpha), ("transmission", transmission)) for s in SPECIES if values[s] is None)
        return report
    report["pressure"] = zns_equilibrium_pressures(config["temperature_K"], ratio, reference_data=data)
    if config["closure"] == "sealed_stoichiometric_gas":
        report["pressure"]["allowed_for_species_HK"] = False
        report["assumptions"].append("封闭、仅由化学计量 ZnS 产生的理想 Zn/S₂ 气体：pZn/pS₂=2；只报平衡压力，无净逸出；保存的 α、τ、面积不用于本模式计算")
        report["status"] = "reference_prediction"
        return report
    if config["closure"] == "ideal_effusion":
        report["assumptions"].append("用户显式选择理想小孔逸出情景：两物种 α=τ=1；非已知 ZnS 表面升华系数")
    elif config["closure"] == "congruent_outgoing":
        report["assumptions"].append("给定有效逸出系数的条件闭合：r=2(αS₂τS₂)/(αZnτZn)√(MZn/MS₂)，不是 RI8301 实测动力学")
    else:
        report["assumptions"].append("用户给定 pZn/pS₂ 的开放边界单向参考通量；不保证化学计量净失重或实际源面反应平衡")
    report["area"] = _source_area(profile, config)
    area = report["area"]["value_m2"]
    flux = species_outgoing_fluxes(report["pressure"], alpha=alpha, transmission=transmission, emitting_area_m2=area)
    report.update(flux)
    if area is None:
        report["missing_inputs"].append("emitting_area_m2" if config["area_basis"] == "explicit" else "source_geometry_area")
    if config["area_basis"] == "source_geometry":
        report["warnings"].append("源几何面积由用户显式选择，假设整个几何面均匀同温；未使用 active_fraction、束斑或扫描足迹")
    report["warnings"].append("理论源边界单向通量与传输后流量，仅为文献条件情景；未计算基片留存/复合、实际膜密度或 nm/s")
    report["status"] = "missing_inputs" if report["missing_inputs"] else "reference_prediction"
    return report
