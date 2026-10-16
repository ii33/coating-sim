"""Explicit process environments and zero-collision beam survival (SI units).

The cross section is an effective projectile/background collision cross section,
not an inferred material constant. No substrate retention or source emission law
is inferred from temperature. See docs/algorithms.html#process-environment.
"""
from collections.abc import Mapping
import math

BOLTZMANN_J_K = 1.380649e-23
FIELDS = {"transport_model", "substrate_temperature_k", "source_temperature_k",
          "gas_temperature_k", "operating_pressure_pa", "base_pressure_pa", "effective_cross_section_m2",
          "effective_cross_section_source", "gas_components"}


def validate_environment(value):
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("environment 必须为对象")
    if set(value) - FIELDS:
        raise ValueError("environment 含不支持的参数：" + ", ".join(sorted(set(value) - FIELDS)))
    out = {}
    for key, raw in value.items():
        if raw is None:
            out[key] = None  # Explicitly clear inherited value; never convert unknown to zero.
        elif key == "gas_components":
            if not isinstance(raw, list) or len(raw) > 32:
                raise ValueError("gas_components 须为最多 32 项的列表")
            components = []
            for component in raw:
                allowed = {"species", "partial_pressure_pa", "effective_cross_section_m2", "flow_sccm", "cross_section_source"}
                if not isinstance(component, Mapping) or set(component) - allowed:
                    raise ValueError("气体组分含不支持的参数")
                name = component.get("species")
                if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
                    raise ValueError("气体组分须明确 species")
                entry = {"species": name.strip()}
                for field in allowed - {"species"}:
                    value = component.get(field)
                    if value is None:
                        continue
                    if field == "cross_section_source":
                        if not isinstance(value, str) or len(value) > 2000:
                            raise ValueError("截面依据须为不超过 2000 字的文本")
                        entry[field] = value
                    else:
                        if isinstance(value, bool):
                            raise ValueError(f"{field} 必须为有限数值")
                        try:
                            number = float(value)
                        except (ValueError, TypeError, OverflowError) as exc:
                            raise ValueError(f"{field} 必须为有限数值") from exc
                        if not math.isfinite(number) or (number <= 0 if field == "effective_cross_section_m2" else number < 0):
                            raise ValueError(f"{field} 超出有效范围")
                        entry[field] = number
                components.append(entry)
            out[key] = components
        elif key == "effective_cross_section_source":
            if not isinstance(raw, str) or len(raw) > 2000:
                raise ValueError("截面依据须为不超过 2000 字的文本")
            out[key] = raw
        elif key == "transport_model":
            if raw not in ("disabled", "ballistic_loss"):
                raise ValueError("transport_model 必须为 disabled 或 ballistic_loss")
            out[key] = raw
        else:
            if isinstance(raw, bool):
                raise ValueError(f"{key} 必须为有限数值")
            try:
                number = float(raw)
            except (ValueError, TypeError, OverflowError) as exc:
                raise ValueError(f"{key} 必须为有限数值") from exc
            if not math.isfinite(number) or (number < 0 if key in {"operating_pressure_pa", "base_pressure_pa"} else number <= 0):
                raise ValueError(f"{key} 必须为有限正数（绝对压力可为 0）")
            out[key] = number
    return out


def resolve_environment(defaults=None, overrides=None):
    result = validate_environment(defaults)
    result.update(validate_environment(overrides))
    return {key: value for key, value in result.items() if value is not None}


def environment_report(environment):
    env = validate_environment(environment)
    model = env.get("transport_model") or "disabled"
    active = model == "ballistic_loss"
    pressure = env.get("operating_pressure_pa")
    alpha = None
    components = env.get("gas_components") or []
    if components and env.get("effective_cross_section_m2") is not None:
        raise ValueError("组分截面与混合气体有效截面不可同时指定，避免重复计算")
    if components and pressure is not None:
        try:
            known_sum = math.fsum(c.get("partial_pressure_pa", 0) for c in components)
        except OverflowError as exc:
            raise ValueError("气体分压总和超出有限数范围") from exc
        tolerance = abs(pressure) * 1e-6
        if known_sum > pressure + tolerance:
            raise ValueError("已知组分分压之和不能超过工作总压力")
        if active and any("partial_pressure_pa" not in c for c in components):
            raise ValueError("启用直达束损失时每种气体须明确分压，未知不能当作零")
        if all("partial_pressure_pa" in c for c in components) and not math.isclose(known_sum, pressure, rel_tol=1e-6, abs_tol=0.0):
            raise ValueError("组分分压之和须等于工作总压力；本底真空不自动加入")
    if active:
        if pressure is None:
            raise ValueError("ballistic_loss 需要明确的绝对工作压力 operating_pressure_pa")
        if pressure == 0:
            alpha = 0.0
        else:
            temperature = env.get("gas_temperature_k")
            if temperature is None:
                raise ValueError("正压力 ballistic_loss 需要气体温度；未知不能当作零")
            if components:
                if any("partial_pressure_pa" not in c or (c["partial_pressure_pa"] > 0 and "effective_cross_section_m2" not in c) for c in components):
                    raise ValueError("每种工作气体需要分压；正分压组分需要有效碰撞截面，未知不能当作零")
                try:
                    numerator = math.fsum(c["partial_pressure_pa"] * c.get("effective_cross_section_m2", 0) for c in components)
                except OverflowError as exc:
                    raise ValueError("气体碰撞系数超出有限数范围") from exc
            else:
                if env.get("effective_cross_section_m2") is None:
                    raise ValueError("正压力 ballistic_loss 需要混合气体有效截面或完整组分；未知不能当作零")
                numerator = pressure * env["effective_cross_section_m2"]
            denominator = BOLTZMANN_J_K * temperature
            if not math.isfinite(denominator) or denominator <= 0:
                raise ValueError("气体热能 kBT 超出有限正数范围")
            alpha = numerator / denominator
            if not math.isfinite(alpha) or alpha <= 0 or not math.isfinite(1 / alpha):
                raise ValueError("碰撞衰减系数超出有限正数范围")
    status = {key: ("coupled_ballistic_loss" if active and key in
              {"gas_temperature_k", "operating_pressure_pa", "effective_cross_section_m2", "gas_components"}
              else "not_modeled") for key, value in env.items() if key != "transport_model" and value is not None}
    return {"effective_environment": {k: v for k, v in env.items() if v is not None},
            "transport_model": model, "parameter_status": status,
            "component_status": [{"species": c["species"], "partial_pressure_pa": "coupled_ballistic_loss" if active else "not_modeled", "flow_sccm": "not_modeled", "reactive_chemistry": "not_modeled", "cross_section_source": c.get("cross_section_source") or "user_assumed_unsourced"} for c in components],
            "cross_section_status": env.get("effective_cross_section_source") or ("per_component" if components else "user_assumed_unsourced"),
            "attenuation_coefficient_m_inv": alpha,
            "beam_interaction_length_m": 1 / alpha if alpha else None,
            "beam_interaction_length_status": "infinite_zero_pressure" if alpha == 0 else ("modeled" if active else "not_modeled"),
            "thickness_semantics": "target_normalized_shape; rate_and_duration_are_user_inputs; absolute_net_growth_not_predicted",
            "limitations": ["Zero-collision direct beam only; no scattered redeposition, gas chemistry or dense source self-collisions",
                            "Effective cross section is user supplied, not a material-library prediction; lumped depositing-projectile population",
                            "Sigma_eff=<sigma(v_rel)*v_rel>/v_beam absorbs relative speed; held fixed in gas-temperature sweeps",
                            "Mixture components share the supplied gas temperature; source species are not separately transported",
                            "Substrate/source temperature response, sticking and re-evaporation are not modeled",
                            "Base vacuum is predeposition metadata, never automatically added to operating pressure",
                            "Gas inlet flow does not determine pressure without a pumping model; reactive gas chemistry is not modeled"]}


def model_dump_with_environment(model):
    """Keep ordinary default serialization, preserving explicit environment nulls.

    Pydantic exclude_none normally erases per-field clearing overrides. Only the
    environment subtree has null-as-clear semantics; unrelated old defaults stay
    byte-for-byte structurally compatible.
    """
    result = model.model_dump(mode="json", exclude_none=True)
    original = model.model_dump(mode="json", exclude_unset=True)
    def restore(target, source):
        if isinstance(source, dict) and isinstance(target, dict):
            for key, value in source.items():
                if key == "environment" and isinstance(value, dict):
                    target[key] = value
                elif key in target:
                    restore(target[key], value)
        elif isinstance(source, list) and isinstance(target, list):
            for left, right in zip(target, source):
                restore(left, right)
    restore(result, original)
    return result
