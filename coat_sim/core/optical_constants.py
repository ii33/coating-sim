"""Independent, user-entered optical datasets. Never a deposition-rate model.

Wavelengths retain explicit nm or um units. Tabulated k may be wholly unknown;
real-index dispersion models do not manufacture an extinction coefficient.
"""
from __future__ import annotations

from bisect import bisect_left
from collections.abc import Mapping
from copy import deepcopy
import math
import numbers
from pathlib import Path
import re
import uuid

import numpy as np

UNITS_TO_UM = {"nm": 0.001, "um": 1.0}
MAX_SAMPLES = 100000
MAX_FILE_BYTES = 16 * 1024 * 1024


def _number(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是有限数值")
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} 必须是有限数值") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} 必须是有限数值")
    return value


def _text(value, name, maximum=20000):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{name} 必须是不超过 {maximum} 字的文字")
    return value


_RII_LENGTHS = {
    1: tuple(range(1, 18, 2)), 2: tuple(range(1, 18, 2)),
    3: tuple(range(1, 18, 2)), 4: (9, 11, 13, 15, 17),
    5: tuple(range(1, 12, 2)), 6: tuple(range(1, 12, 2)),
    7: (1, 2, 3, 4, 5, 6), 8: (4,), 9: (6,),
}
_RII_NAMES = {
    1: "Sellmeier", 2: "Sellmeier-2", 3: "多项式 n²", 4: "RII 广义 Sellmeier",
    5: "广义 Cauchy", 6: "气体色散", 7: "Herzberger", 8: "Retro", 9: "Exotic",
}
_RII_QUANTITY = {1: "n2", 2: "n2", 3: "n2", 4: "n2", 5: "n", 6: "n", 7: "n", 8: "n2", 9: "n2"}


def optical_model_catalog():
    """Return the stable material-editor contract for supported optical models.

    RII coefficients are ordered C1..Cn and are always interpreted using a
    wavelength expressed numerically in micrometres. ``wavelength_unit`` on a
    record describes its validity range; it never rescales these coefficients.
    The optional ``k_data`` object is an independent measured k table.

    ``formula_latex`` is display-only Mathtext without dollar delimiters.
    ``formula_wavelength_unit="wavelength_unit"`` means to substitute the
    current record's wavelength unit; RII formulas always use ``"um"``.
    In variable-length RII sums, N is the number of supplied coefficients;
    absent trailing Herzberger coefficients and empty sums contribute zero.
    """
    rows = [
        {"kind": "tabulated", "label": "离散 n/k 数据", "quantity": "n,k",
         "coefficient_wavelength_unit": None, "coefficient_counts": [],
         "coefficient_names": [], "supports_independent_k": False},
        {"kind": "cauchy", "label": "Cauchy（n）", "quantity": "n",
         "coefficient_wavelength_unit": "wavelength_unit", "coefficient_counts": [],
         "coefficient_names": ["A", "B", "C"], "supports_independent_k": True,
         "formula_latex": r"n=A+\frac{B}{\lambda^{2}}+\frac{C}{\lambda^{4}}",
         "formula_wavelength_unit": "wavelength_unit"},
        {"kind": "sellmeier", "label": "Sellmeier（三项，n²）", "quantity": "n2",
         "coefficient_wavelength_unit": "wavelength_unit", "coefficient_counts": [],
         "coefficient_names": ["B1", "C1", "B2", "C2", "B3", "C3"],
         "supports_independent_k": True,
         "formula_latex": r"n^{2}=1+\sum_{i=1}^{3}\frac{B_i\lambda^{2}}{\lambda^{2}-C_i}",
         "formula_wavelength_unit": "wavelength_unit"},
    ]
    rii_formula_latex = {
        1: (r"n^{2}=1+C_1+\sum_{j=1}^{(N-1)/2}"
            r"\frac{C_{2j}\lambda^{2}}{\lambda^{2}-C_{2j+1}^{2}}"
            r",\quad N=\mathrm{len}(C)"),
        2: (r"n^{2}=1+C_1+\sum_{j=1}^{(N-1)/2}"
            r"\frac{C_{2j}\lambda^{2}}{\lambda^{2}-C_{2j+1}}"
            r",\quad N=\mathrm{len}(C)"),
        3: (r"n^{2}=C_1+\sum_{j=1}^{(N-1)/2}C_{2j}\lambda^{C_{2j+1}}"
            r",\quad N=\mathrm{len}(C)"),
        4: (r"n^{2}=C_1+\frac{C_2\lambda^{C_3}}{\lambda^{2}-C_4^{C_5}}"
            r"+\frac{C_6\lambda^{C_7}}{\lambda^{2}-C_8^{C_9}}"
            r"+\sum_{j=0}^{(N-11)/2}C_{10+2j}\lambda^{C_{11+2j}}"
            r",\quad N=\mathrm{len}(C)"),
        5: (r"n=C_1+\sum_{j=1}^{(N-1)/2}C_{2j}\lambda^{C_{2j+1}}"
            r",\quad N=\mathrm{len}(C)"),
        6: (r"n=1+C_1+\sum_{j=1}^{(N-1)/2}"
            r"\frac{C_{2j}}{C_{2j+1}-\lambda^{-2}}"
            r",\quad N=\mathrm{len}(C)"),
        7: (r"n=C_1+C_2 L+C_3 L^{2}+C_4\lambda^{2}+C_5\lambda^{4}+C_6\lambda^{6}"
            r",\quad L=\frac{1}{\lambda^{2}-0.028}"),
        8: (r"n^{2}=\frac{1+2q}{1-q}"
            r",\quad q=C_1+\frac{C_2\lambda^{2}}{\lambda^{2}-C_3}+C_4\lambda^{2}"),
        9: (r"n^{2}=C_1+\frac{C_2}{\lambda^{2}-C_3}"
            r"+\frac{C_4(\lambda-C_5)}{(\lambda-C_5)^{2}+C_6}"),
    }
    for number in range(1, 10):
        maximum = max(_RII_LENGTHS[number])
        rows.append({
            "kind": "rii_formula", "formula_number": number,
            "id": f"rii_formula_{number}",
            "label": f"RII 公式 {number}（{_RII_NAMES[number]}）",
            "quantity": _RII_QUANTITY[number],
            "coefficient_wavelength_unit": "um",
            "coefficient_counts": list(_RII_LENGTHS[number]),
            "coefficient_names": [f"C{i}" for i in range(1, maximum + 1)],
            "supports_independent_k": True,
            "formula_latex": rii_formula_latex[number],
            "formula_wavelength_unit": "um",
        })
    return rows


def _near_zero(value, scale):
    return abs(value) <= 64.0 * math.ulp(1.0) * max(abs(scale), 1e-300)


def _safe_power(base, exponent, label):
    if base == 0 and exponent <= 0:
        raise ValueError(f"{label} 的零底数与指数无效")
    if base < 0 and not float(exponent).is_integer():
        raise ValueError(f"{label} 的负底数不能使用非整数指数")
    try:
        value = base ** exponent
    except (OverflowError, ZeroDivisionError, ValueError) as exc:
        raise ValueError(f"{label} 幂运算溢出或无定义") from exc
    if isinstance(value, complex) or not math.isfinite(value):
        raise ValueError(f"{label} 幂运算产生非实或非有限值")
    return float(value)


def _rii_refractive_index(formula_number, coefficients, wavelength_um):
    """Evaluate RII formulas 1–9 using their published micrometre convention."""
    c = coefficients
    lam = wavelength_um
    lam2 = lam * lam
    if formula_number in (1, 2):
        value = 1.0 + c[0]
        for i in range(1, len(c), 2):
            amplitude, resonance = c[i], c[i + 1]
            if amplitude == 0:
                continue
            pole = resonance * resonance if formula_number == 1 else resonance
            denominator = lam2 - pole
            if _near_zero(denominator, max(lam2, abs(pole))):
                raise ValueError(f"RII 公式 {formula_number} 在查询波长处接近极点")
            value += amplitude * lam2 / denominator
        n_is_squared = True
    elif formula_number in (3, 5):
        value = c[0]
        for i in range(1, len(c), 2):
            amplitude, exponent = c[i], c[i + 1]
            if amplitude != 0:
                value += amplitude * _safe_power(lam, exponent, f"RII 公式 {formula_number}")
        n_is_squared = formula_number == 3
    elif formula_number == 4:
        value = c[0]
        # Two resonant groups are fixed four-coefficient tuples. Inactive
        # zero-amplitude groups are skipped before any power or pole operation.
        for offset in (1, 5):
            if offset >= len(c):
                continue
            amplitude, exponent, base, base_exponent = c[offset:offset + 4]
            if amplitude == 0:
                continue
            resonance = _safe_power(base, base_exponent, "RII 公式 4 共振项")
            denominator = lam2 - resonance
            if _near_zero(denominator, max(lam2, abs(resonance))):
                raise ValueError("RII 公式 4 在查询波长处接近共振极点")
            value += amplitude * _safe_power(lam, exponent, "RII 公式 4") / denominator
        for i in range(9, len(c), 2):
            amplitude, exponent = c[i:i + 2]
            if amplitude != 0:
                value += amplitude * _safe_power(lam, exponent, "RII 公式 4")
        n_is_squared = True
    elif formula_number == 6:
        value = 1.0 + c[0]
        for i in range(1, len(c), 2):
            amplitude, resonance = c[i], c[i + 1]
            if amplitude == 0:
                continue
            inverse_square = 1.0 / lam2
            denominator = resonance - inverse_square
            if _near_zero(denominator, max(abs(resonance), inverse_square)):
                raise ValueError("RII 公式 6 在查询波长处接近气体共振极点")
            value += amplitude / denominator
        n_is_squared = False
    elif formula_number == 7:
        padded = list(c) + [0.0] * (6 - len(c))
        a, b, cc, d, e, f = padded
        value = a
        if b != 0 or cc != 0:
            denominator = lam2 - 0.028
            if _near_zero(denominator, max(lam2, 0.028)):
                raise ValueError("RII 公式 7 在查询波长处接近 Herzberger 极点")
            l_value = 1.0 / denominator
            value += b * l_value + cc * l_value * l_value
        if d != 0: value += d * lam2
        if e != 0: value += e * lam2 * lam2
        if f != 0: value += f * lam2 * lam2 * lam2
        n_is_squared = False
    elif formula_number == 8:
        a, b, resonance, d = c
        q = a
        if b != 0:
            denominator = lam2 - resonance
            if _near_zero(denominator, max(lam2, abs(resonance))):
                raise ValueError("RII 公式 8 在查询波长处接近共振极点")
            q += b * lam2 / denominator
        if d != 0: q += d * lam2
        denominator = 1.0 - q
        if _near_zero(denominator, max(1.0, abs(q))):
            raise ValueError("RII 公式 8 的 q=1 变换极点")
        value = (1.0 + 2.0 * q) / denominator
        n_is_squared = True
    elif formula_number == 9:
        a, b, resonance, d, center, width = c
        value = a
        if b != 0:
            denominator = lam2 - resonance
            if _near_zero(denominator, max(lam2, abs(resonance))):
                raise ValueError("RII 公式 9 第一分式在查询波长处接近极点")
            value += b / denominator
        if d != 0:
            x = lam - center
            denominator = x * x + width
            if _near_zero(denominator, max(x * x, abs(width))):
                raise ValueError("RII 公式 9 第二分式在查询波长处接近极点")
            value += d * x / denominator
        n_is_squared = True
    else:
        raise ValueError("仅支持 RII 色散公式 1–9")

    if not math.isfinite(value):
        raise ValueError(f"RII 公式 {formula_number} 产生非有限结果")
    if n_is_squared:
        if value <= 0:
            raise ValueError(f"RII 公式 {formula_number} 的 n² 必须为正；不从负 n² 推导吸收")
        value = math.sqrt(value)
    elif value <= 0:
        raise ValueError(f"RII 公式 {formula_number} 的 n 必须为有限正数")
    if not math.isfinite(value):
        raise ValueError(f"RII 公式 {formula_number} 产生非有限折射率")
    return value


def _assert_no_rii_poles(formula_number, coefficients, low_um, high_um):
    """Reject valid ranges that include real singularities of the selected formula."""
    c = coefficients

    def check(wavelength, label):
        if not math.isfinite(wavelength) or wavelength <= 0:
            return
        margin = 64.0 * max(math.ulp(low_um), math.ulp(high_um), math.ulp(wavelength))
        if low_um - margin <= wavelength <= high_um + margin:
            raise ValueError(f"RII 公式 {formula_number} 的有效波段包含或过于接近{label}，请缩小波段")

    if formula_number in (1, 2):
        for index in range(1, len(c), 2):
            amplitude, resonance = c[index:index + 2]
            if amplitude == 0:
                continue
            pole_square = resonance * resonance if formula_number == 1 else resonance
            if pole_square > 0:
                check(math.sqrt(pole_square), "共振极点")
    elif formula_number == 4:
        for offset in (1, 5):
            if offset >= len(c):
                continue
            amplitude, exponent, base, base_exponent = c[offset:offset + 4]
            if amplitude == 0:
                continue
            resonance = _safe_power(base, base_exponent, "RII 公式 4 共振项")
            if resonance > 0:
                check(math.sqrt(resonance), "共振极点")
    elif formula_number == 6:
        for index in range(1, len(c), 2):
            amplitude, resonance = c[index:index + 2]
            if amplitude != 0 and resonance > 0:
                check(1.0 / math.sqrt(resonance), "气体共振极点")
    elif formula_number == 7:
        padded = list(c) + [0.0] * (6 - len(c))
        if padded[1] != 0 or padded[2] != 0:
            check(math.sqrt(0.028), "Herzberger 极点")
    elif formula_number == 8:
        a, b, resonance, d = c
        if b != 0 and resonance > 0:
            check(math.sqrt(resonance), "Retro 共振极点")

        # q=1 is the transform pole. Solve its numerator in x=lambda^2,
        # then verify each positive root against the original expression.
        p2 = -d
        p1 = (1.0 - a) + d * resonance - b
        p0 = -(1.0 - a) * resonance
        if not all(math.isfinite(value) for value in (p2, p1, p0)):
            raise ValueError("RII 公式 8 的极点分析溢出")
        scale = max(abs(p2), abs(p1), abs(p0))
        if scale == 0:
            raise ValueError("RII 公式 8 的 q=1 变换极点覆盖整个有效波段")
        p2, p1, p0 = p2 / scale, p1 / scale, p0 / scale
        roots = []
        if p2 == 0:
            if p1 != 0:
                roots.append(-p0 / p1)
        else:
            discriminant = p1 * p1 - 4.0 * p2 * p0
            if discriminant >= 0:
                root = math.sqrt(discriminant)
                roots.extend(((-p1 - root) / (2.0 * p2), (-p1 + root) / (2.0 * p2)))
        for square in roots:
            if not math.isfinite(square) or square <= 0:
                continue
            if b != 0 and _near_zero(square - resonance, max(square, abs(resonance))):
                continue  # The underlying resonance was checked above.
            q = a + d * square
            if b != 0:
                denominator = square - resonance
                if denominator == 0:
                    continue
                q += b * square / denominator
            if math.isfinite(q) and _near_zero(1.0 - q, max(1.0, abs(q))):
                check(math.sqrt(square), "q=1 变换极点")
    elif formula_number == 9:
        _a, b, resonance, d, center, width = c
        if b != 0 and resonance > 0:
            check(math.sqrt(resonance), "第一分式极点")
        if d != 0 and width <= 0:
            radius = math.sqrt(-width) if width < 0 else 0.0
            for wavelength in (center - radius, center + radius):
                check(wavelength, "第二分式极点")


def _evaluate_model(record, wavelength):
    p = record.get("parameters", {})
    try:
        if record["kind"] == "cauchy":
            inv = 1.0 / wavelength ** 2
            n = p["A"] + p["B"] * inv + p["C"] * inv ** 2
        elif record["kind"] == "sellmeier":
            square = wavelength ** 2
            terms = []
            for index in (1, 2, 3):
                c = p[f"C{index}"]
                denominator = square - c
                if abs(denominator) <= 1e-12 * max(abs(square), abs(c), 1e-30):
                    raise ValueError("波长接近 Sellmeier 极点，不能计算")
                terms.append(p[f"B{index}"] * square / denominator)
            radicand = 1.0 + math.fsum(terms)
            if not math.isfinite(radicand) or radicand <= 0:
                raise ValueError("Sellmeier 模型在此波长的 n² 非正或非有限")
            n = math.sqrt(radicand)
        elif record["kind"] == "rii_formula":
            wavelength_um = wavelength * UNITS_TO_UM[record["wavelength_unit"]]
            n = _rii_refractive_index(record["formula_number"], record["coefficients"], wavelength_um)
        else:
            raise ValueError("未知光学模型")
    except (OverflowError, ZeroDivisionError) as exc:
        raise ValueError("色散参数导致溢出或奇点") from exc
    if not math.isfinite(n) or n <= 0:
        raise ValueError("模型在有效波段内的折射率必须为有限正数")
    return n


def _coefficient(value, name):
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise ValueError(f"{name} 必须为有限数值系数")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} 必须为有限数值系数")
    return result


def _validate_k_data(data):
    if not isinstance(data, Mapping):
        raise ValueError("独立 k 数据必须为对象")
    allowed = {"wavelength_unit", "wavelength_min", "wavelength_max", "wavelengths", "k", "source", "notes"}
    if set(data) - allowed:
        raise ValueError("独立 k 数据包含未知字段")
    unit = data.get("wavelength_unit")
    if unit not in UNITS_TO_UM:
        raise ValueError("独立 k 数据必须明确选择 nm 或 um")
    low = _number(data.get("wavelength_min"), "k 有效波长下限")
    high = _number(data.get("wavelength_max"), "k 有效波长上限")
    waves, values = data.get("wavelengths"), data.get("k")
    if not isinstance(waves, list) or not isinstance(values, list) or not 1 <= len(waves) <= MAX_SAMPLES or len(values) != len(waves):
        raise ValueError("独立 k 数据须有 1–100000 对波长和 k")
    waves = [_number(x, "k 波长") for x in waves]
    values = [_number(x, "k") for x in values]
    if waves[0] <= 0 or any(b <= a for a, b in zip(waves, waves[1:])):
        raise ValueError("k 波长必须为正且严格递增，不接受重复点")
    if any(x < 0 for x in values):
        raise ValueError("k 必须非负")
    if not 0 < low <= high or low < waves[0] or high > waves[-1]:
        raise ValueError("k 有效域必须为有限正数且位于其数据范围内")
    return {"wavelength_unit": unit, "wavelength_min": low, "wavelength_max": high,
            "wavelengths": waves, "k": values,
            "source": _text(data.get("source", ""), "k 数据来源"),
            "notes": _text(data.get("notes", ""), "k 数据备注")}


def _validate_process_parameters(value):
    """Validate explicit per-set process labels without interpreting their values."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 128:
        raise ValueError("process_parameters 必须是最多 128 行的数组")
    result = []
    for index, row in enumerate(value, 1):
        if not isinstance(row, Mapping) or set(row) - {"name", "value", "unit"}:
            raise ValueError(f"工艺参数第 {index} 行只能包含 name、value、unit")
        clean = {}
        for key, maximum in (("name", 120), ("value", 512), ("unit", 80)):
            item = row.get(key, "")
            if not isinstance(item, str):
                raise ValueError(f"工艺参数第 {index} 行 {key} 必须为文本")
            clean[key] = item.strip()
            if len(clean[key]) > maximum:
                raise ValueError(f"工艺参数第 {index} 行 {key} 超出 {maximum} 字符")
        if not any(clean.values()):
            continue
        if not clean["name"]:
            raise ValueError(f"工艺参数第 {index} 行必须填写 name")
        result.append(clean)
    return result


def validate_optical_set(data):
    if not isinstance(data, Mapping):
        raise ValueError("光学常数集必须为对象")
    allowed = {"id", "name", "notes", "source", "kind", "wavelength_unit", "wavelength_min", "wavelength_max",
               "wavelengths", "n", "k", "parameters", "formula_number", "coefficients", "k_data",
               "process_parameters"}
    if set(data) - allowed:
        raise ValueError("光学常数集含未知字段")
    identity = data.get("id")
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", identity):
        raise ValueError("光学常数集 id 无效")
    name = _text(data.get("name"), "光学常数集名称", 120).strip()
    if not name:
        raise ValueError("光学常数集必须命名")
    kind = data.get("kind")
    if kind not in ("tabulated", "cauchy", "sellmeier", "rii_formula"):
        raise ValueError("光学模型须为 tabulated、cauchy、sellmeier 或 rii_formula")
    unit = data.get("wavelength_unit")
    if unit not in UNITS_TO_UM:
        raise ValueError("必须明确选择波长单位 nm 或 um")
    result = {"id": identity, "name": name, "notes": _text(data.get("notes", ""), "备注"),
              "source": _text(data.get("source", ""), "来源"), "kind": kind, "wavelength_unit": unit}
    low = _number(data.get("wavelength_min"), "有效波长下限")
    high = _number(data.get("wavelength_max"), "有效波长上限")
    if (not 0 < low <= high or (kind != "tabulated" and low == high)
            or not math.isfinite(high ** 2 if high < 1e154 else math.inf)):
        raise ValueError("有效波长须为有限正数；离散表下限不得大于上限，色散公式下限须小于上限")
    result.update(wavelength_min=low, wavelength_max=high)
    if "process_parameters" in data:
        result["process_parameters"] = _validate_process_parameters(data["process_parameters"])

    if data.get("k_data") is not None:
        result["k_data"] = _validate_k_data(data["k_data"])

    if kind == "tabulated":
        if any(key in data for key in ("parameters", "formula_number", "coefficients", "k_data")):
            raise ValueError("离散 n/k 数据不能同时携带色散参数或独立 k_data")
        wave, n, k = data.get("wavelengths"), data.get("n"), data.get("k")
        if not isinstance(wave, list) or not 1 <= len(wave) <= MAX_SAMPLES or not isinstance(n, list) or len(n) != len(wave):
            raise ValueError(f"离散数据须有 1–{MAX_SAMPLES} 对波长和 n")
        if k is not None and (not isinstance(k, list) or len(k) != len(wave)):
            raise ValueError("k 必须与波长等长，或保持未知 null")
        wave = [_number(v, "波长") for v in wave]
        n = [_number(v, "n") for v in n]
        k = [_number(v, "k") for v in k] if k is not None else None
        if wave[0] <= 0 or any(b <= a for a, b in zip(wave, wave[1:])):
            raise ValueError("波长必须为正且严格递增，不接受重复点")
        if any(v <= 0 for v in n) or (k is not None and any(v < 0 for v in k)):
            raise ValueError("n 必须为正，k 必须非负")
        if low < wave[0] or high > wave[-1]:
            raise ValueError("有效波长域不能超出离散数据范围")
        result.update(wavelengths=wave, n=n, k=k)
    elif kind in ("cauchy", "sellmeier"):
        if any(key in data for key in ("wavelengths", "n", "k", "formula_number", "coefficients")):
            raise ValueError("Cauchy/Sellmeier 仅接受 parameters；独立 k 使用 k_data")
        expected = {"A", "B", "C"} if kind == "cauchy" else {f"{key}{i}" for key in ("B", "C") for i in (1, 2, 3)}
        parameters = data.get("parameters")
        if not isinstance(parameters, Mapping) or set(parameters) != expected:
            raise ValueError("请完整填写模型所需参数：" + ", ".join(sorted(expected)))
        result["parameters"] = p = {key: _number(value, key) for key, value in parameters.items()}
        checkpoints = [low, high]
        if kind == "cauchy":
            if p["C"] != 0:
                x = -p["B"] / (2 * p["C"])
                if x > 0 and low < 1 / math.sqrt(x) < high:
                    checkpoints.append(1 / math.sqrt(x))
        else:
            if any(value < 0 for value in p.values()):
                raise ValueError("本实现的三项 Sellmeier 要求 Bᵢ、Cᵢ 非负")
            for i in (1, 2, 3):
                pole = math.sqrt(p[f"C{i}"])
                margin = 1e-10 * max(high, pole, 1e-15)
                if low - margin <= pole <= high + margin:
                    raise ValueError("Sellmeier 有效波长域包含或过于接近极点，请缩小波段")
        for point in checkpoints:
            _evaluate_model(result, point)
    else:
        if any(key in data for key in ("wavelengths", "n", "k", "parameters")):
            raise ValueError("RII 公式仅接受 coefficients 和可选独立 k_data")
        number = data.get("formula_number")
        if type(number) is not int or not 1 <= number <= 9:
            raise ValueError("formula_number 必须是 1–9 整数")
        coefficients = data.get("coefficients")
        if not isinstance(coefficients, list) or len(coefficients) not in _RII_LENGTHS[number]:
            allowed_lengths = ", ".join(str(x) for x in _RII_LENGTHS[number])
            raise ValueError(f"RII 公式 {number} coefficients 长度须为 {allowed_lengths}")
        result["formula_number"] = number
        result["coefficients"] = [_coefficient(value, f"C{index}") for index, value in enumerate(coefficients, 1)]
        _assert_no_rii_poles(number, result["coefficients"], low * UNITS_TO_UM[unit], high * UNITS_TO_UM[unit])
        for point in (low, high):
            _evaluate_model(result, point)

    return result


def validate_optical_sets(records=None):
    if records is None:
        return []
    if not isinstance(records, list) or len(records) > 100:
        raise ValueError("每种材料最多支持 100 个光学常数集")
    result = [validate_optical_set(row) for row in records]
    if len({row["id"] for row in result}) != len(result):
        raise ValueError("同一材料的光学常数集 id 必须唯一")
    if len({row["name"].casefold() for row in result}) != len(result):
        raise ValueError("同一材料的光学常数集名称必须唯一")
    return result


def import_optical_text(text, *, name, wavelength_unit, notes="", source="", identifier=None):
    """Strict 2/3-column CSV, TSV or whitespace: wavelength,n[,k].

    One optional literal header and # comments are accepted. Units are never
    inferred from a filename or silently converted. Missing k stays None.
    """
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError("光学数据文本不能超过 16 MiB")
    rows, columns = [], None
    for line_number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cells = [cell.strip() for cell in line.split(",")] if "," in line else line.split()
        if not rows and columns is None and [cell.casefold() for cell in cells] in (["wavelength", "n"], ["wavelength", "n", "k"]):
            columns = len(cells)
            continue
        if len(cells) not in (2, 3) or (columns is not None and len(cells) != columns):
            raise ValueError(f"第 {line_number} 行必须有一致的 wavelength,n 或 wavelength,n,k 列")
        columns = len(cells)
        rows.append([_number(cell, f"第 {line_number} 行") for cell in cells])
        if len(rows) > MAX_SAMPLES:
            raise ValueError("光学数据点数超限")
    if not rows:
        raise ValueError("至少需要一个光学数据点")
    return validate_optical_set({"id": identifier or uuid.uuid4().hex, "name": name, "notes": notes,
        "source": source, "kind": "tabulated", "wavelength_unit": wavelength_unit,
        "wavelength_min": rows[0][0], "wavelength_max": rows[-1][0],
        "wavelengths": [row[0] for row in rows], "n": [row[1] for row in rows],
        "k": [row[2] for row in rows] if columns == 3 else None})


def import_optical_file(path, **kwargs):
    file = Path(path)
    if not file.is_file() or file.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("光学数据文件必须是小于 16 MiB 的普通文本文件")
    try:
        return import_optical_text(file.read_text(encoding="utf-8-sig"), **kwargs)
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"无法读取光学数据文件：{exc}") from exc


def _nm(value, unit):
    return value * (1000.0 if unit == "um" else 1.0)


def _normalized_endpoint(value, low, high):
    for endpoint in (low, high):
        if math.isfinite(value) and abs(value - endpoint) <= 4 * max(math.ulp(value), math.ulp(endpoint)):
            return endpoint
    return value


def _bounded_query(value, low, high, policy, label):
    value = _normalized_endpoint(value, low, high)
    if low <= value <= high:
        return value, None
    side = "below" if value < low else "above"
    if policy == "error":
        raise ValueError(f"查询波长超出{label}有效波段（有效域），禁止外推")
    if policy != "endpoint_hold":
        raise ValueError("extrapolation_policy 须为 error 或 endpoint_hold")
    return (low if side == "below" else high), side


def normalize_extrapolation_policy(policy):
    """Retain the legacy endpoint spelling while publishing endpoint_hold."""
    if policy == "endpoint":
        return "endpoint_hold"
    if policy not in ("error", "endpoint_hold"):
        raise ValueError("extrapolation_policy 须为 error 或 endpoint_hold")
    return policy


def _linear_table_value(wavelengths, values, x, unit):
    index = bisect_left(wavelengths, x)
    if index < len(wavelengths) and wavelengths[index] == x:
        return values[index], None
    if index <= 0 or index >= len(wavelengths):
        raise ValueError("表格插值查询点不在已记录的相邻样本之间")
    segment_nm = [_nm(wavelengths[index - 1], unit), _nm(wavelengths[index], unit)]
    weight = (x - wavelengths[index - 1]) / (wavelengths[index] - wavelengths[index - 1])
    return (1.0 - weight) * values[index - 1] + weight * values[index], segment_nm


def _snap_table_endpoints_many(values, low, high):
    """Apply the same four-ULP source-unit endpoint rule to a 1D query batch."""
    values = np.asarray(values, dtype=float).copy()
    for endpoint in (low, high):
        tolerance = 4 * np.maximum(np.spacing(np.abs(values)), math.ulp(endpoint))
        values[np.abs(values - endpoint) <= tolerance] = endpoint
    return values


def _bounded_queries_many(values, low, high, policy, label):
    values = _snap_table_endpoints_many(values, low, high)
    below, above = values < low, values > high
    outside = below | above
    if policy == "error" and np.any(outside):
        index = int(np.flatnonzero(outside)[0])
        _bounded_query(float(values[index]), low, high, policy, label)
    if policy == "endpoint_hold":
        values = np.clip(values, low, high)
    elif policy != "error":
        raise ValueError("extrapolation_policy 须为 error 或 endpoint_hold")
    return values, below, above


def _linear_table_values_many(wavelengths, values, queries, unit):
    """Match _linear_table_value's exact interpolation order for 1D arrays."""
    x = np.asarray(wavelengths, dtype=float)
    y = np.asarray(values, dtype=float)
    queries = np.asarray(queries, dtype=float)
    indices = np.searchsorted(x, queries, side="left")
    safe_indices = np.minimum(indices, len(x) - 1)
    exact = (indices < len(x)) & (x[safe_indices] == queries)
    between = ~exact
    if np.any(between & ((indices <= 0) | (indices >= len(x)))):
        raise ValueError("表格插值查询点不在已记录的相邻样本之间")
    result = np.empty(queries.size, dtype=float)
    result[exact] = y[indices[exact]]
    if np.any(between):
        upper = indices[between]
        lower = upper - 1
        weight = (queries[between] - x[lower]) / (x[upper] - x[lower])
        result[between] = (1.0 - weight) * y[lower] + weight * y[upper]
        scale = 1000.0 if unit == "um" else 1.0
        segment = [float(np.min(x[lower]) * scale), float(np.max(x[upper]) * scale)]
    else:
        segment = None
    return result, int(np.count_nonzero(between)), segment


def _evaluate_tabulated_many(record, wavelengths, wavelength_unit,
                             extrapolation_policy, include_k, k_sampling_policy):
    if wavelength_unit not in UNITS_TO_UM:
        raise ValueError("查询波长必须明确指定 nm 或 um")
    extrapolation_policy = normalize_extrapolation_policy(extrapolation_policy)
    if type(include_k) is not bool:
        raise ValueError("include_k 须为布尔值")
    query = np.asarray(wavelengths, dtype=float)
    if (query.ndim != 1 or query.size == 0 or not np.all(np.isfinite(query))
            or np.any(query <= 0)):
        raise ValueError("查询波长须为正的一维有限值")
    query_nm = query * (1000.0 if wavelength_unit == "um" else 1.0)
    n_unit = record["wavelength_unit"]
    n_low, n_high = record["wavelength_min"], record["wavelength_max"]
    n_query, n_below, n_above = _bounded_queries_many(
        query_nm / (1000.0 if n_unit == "um" else 1.0),
        n_low, n_high, extrapolation_policy, "n")
    n_values, n_interpolated, n_segment = _linear_table_values_many(
        record["wavelengths"], record["n"], n_query, n_unit)
    summary = {
        "n_interpolation_range_nm": n_segment,
        "n_interpolated_queries": n_interpolated,
        "k_interpolation_range_nm": None,
        "k_interpolated_queries": 0,
        "k_evaluated_input_range_nm": None,
        "k_source_queries": 0,
        "k_default_zero_queries": 0,
        "n_extrapolated_sides": set(),
        "k_extrapolated_sides": set(),
    }
    if np.any(n_below):
        summary["n_extrapolated_sides"].add("below")
    if np.any(n_above):
        summary["n_extrapolated_sides"].add("above")

    k_table = record if record["k"] is not None else record.get("k_data")
    zero_defaults = k_sampling_policy == "source_only_zero_defaults"
    k_values = None
    if zero_defaults and (not include_k or k_table is None):
        k_values = np.zeros(query.size, dtype=float)
        summary["k_default_zero_queries"] = int(query.size)
    elif include_k and k_table is not None:
        k_values = np.zeros(query.size, dtype=float)
        k_unit = k_table["wavelength_unit"]
        k_low, k_high = k_table["wavelength_min"], k_table["wavelength_max"]
        raw_k_query = _snap_table_endpoints_many(
            query_nm / (1000.0 if k_unit == "um" else 1.0), k_low, k_high)
        if zero_defaults:
            k_zero = (raw_k_query < k_low) | (raw_k_query > k_high)
            summary["k_default_zero_queries"] = int(np.count_nonzero(k_zero))
            selected = ~k_zero
            k_query = raw_k_query[selected]
            k_below = np.zeros(k_query.size, dtype=bool)
            k_above = np.zeros(k_query.size, dtype=bool)
        else:
            k_query, k_below, k_above = _bounded_queries_many(
                raw_k_query, k_low, k_high, extrapolation_policy, "k")
            selected = np.ones(query.size, dtype=bool)
        if np.any(selected):
            sampled_k, k_interpolated, k_segment = _linear_table_values_many(
                k_table["wavelengths"], k_table["k"], k_query, k_unit)
            k_values[selected] = sampled_k
            summary["k_interpolation_range_nm"] = k_segment
            summary["k_interpolated_queries"] = k_interpolated
            summary["k_source_queries"] = int(np.count_nonzero(selected))
            evaluated_nm = k_query * (1000.0 if k_unit == "um" else 1.0)
            summary["k_evaluated_input_range_nm"] = [
                float(np.min(evaluated_nm)), float(np.max(evaluated_nm))]
            if np.any(k_below):
                summary["k_extrapolated_sides"].add("below")
            if np.any(k_above):
                summary["k_extrapolated_sides"].add("above")
    return n_values, k_values, summary


def evaluate_optical_set_with_details(record, wavelength, wavelength_unit,
                                      extrapolation_policy="error", include_k=True):
    """Return (n, k, details); endpoint policy clamps each known channel separately.

    Direct source inspection retains strict/endpoint behavior and unknown k=None.
    Film simulation instead requests a prepared source-only k sampler, whose
    missing/out-of-domain calculation default is zero. Source data is unchanged.
    """
    return _evaluate_normalized_optical_set(
        validate_optical_set(record), wavelength, wavelength_unit,
        extrapolation_policy=extrapolation_policy, include_k=include_k)


def prepare_optical_set(record, *, k_sampling_policy="source"):
    """Validate once; optionally sample k only in its own domain, defaulting to 0.

    The default preserves direct source inspection, including unknown k=None.
    source_only_zero_defaults is a calculation convention used by film simulation.
    """
    if k_sampling_policy not in ("source", "source_only_zero_defaults"):
        raise ValueError("k_sampling_policy 须为 source 或 source_only_zero_defaults")
    normalized = validate_optical_set(record)
    def evaluate(wavelength, wavelength_unit, extrapolation_policy="error", include_k=True):
        return _evaluate_normalized_optical_set(
            normalized, wavelength, wavelength_unit,
            extrapolation_policy=extrapolation_policy, include_k=include_k,
            k_sampling_policy=k_sampling_policy)
    def evaluate_many(wavelengths, wavelength_unit, extrapolation_policy="error", include_k=True):
        """Return a table-sampled complex series and aggregate trace details."""
        if normalized["kind"] != "tabulated":
            return None
        n_values, k_values, summary = _evaluate_tabulated_many(
            normalized, wavelengths, wavelength_unit, extrapolation_policy,
            include_k, k_sampling_policy)
        return n_values + 1j * k_values if k_values is not None else None, summary
    evaluate.evaluate_many = evaluate_many
    return evaluate


def _evaluate_normalized_optical_set(record, wavelength, wavelength_unit,
                                     extrapolation_policy="error", include_k=True,
                                     k_sampling_policy="source"):
    if k_sampling_policy not in ("source", "source_only_zero_defaults"):
        raise ValueError("k_sampling_policy 须为 source 或 source_only_zero_defaults")
    if wavelength_unit not in UNITS_TO_UM:
        raise ValueError("查询波长必须明确指定 nm 或 um")
    extrapolation_policy = normalize_extrapolation_policy(extrapolation_policy)
    if type(include_k) is not bool:
        raise ValueError("include_k 须为布尔值")
    query = _number(wavelength, "查询波长")
    query_nm = _nm(query, wavelength_unit)
    if not math.isfinite(query_nm) or query_nm <= 0:
        raise ValueError("查询波长须为有限正数")

    n_unit = record["wavelength_unit"]
    x_n = query_nm / (1000.0 if n_unit == "um" else 1.0)
    n_low, n_high = record["wavelength_min"], record["wavelength_max"]
    x_n, n_side = _bounded_query(x_n, n_low, n_high, extrapolation_policy, "n")
    if record["kind"] == "tabulated":
        n_value, n_segment = _linear_table_value(record["wavelengths"], record["n"], x_n, n_unit)
        n_method = "linear"
    else:
        n_value, n_segment = _evaluate_model(record, x_n), None
        n_method = (f"rii_formula_{record['formula_number']}" if record["kind"] == "rii_formula"
                    else record["kind"])

    k_known = False
    k_value = None
    k_side = None
    k_segment = None
    k_range_nm = None
    k_method = "unknown"
    k_record = None
    k_unit = None
    if record["kind"] == "tabulated" and record["k"] is not None:
        k_known = True
        k_method = "linear"
        k_range_nm = [_nm(n_low, n_unit), _nm(n_high, n_unit)]
    elif record.get("k_data") is not None:
        k_known = True
        k_method = "linear"
        k_record = record["k_data"]
        k_unit = k_record["wavelength_unit"]
        k_low, k_high = k_record["wavelength_min"], k_record["wavelength_max"]
        k_range_nm = [_nm(k_low, k_unit), _nm(k_high, k_unit)]
    k_evaluation_nm = None
    k_default_reason = None
    zero_defaults = k_sampling_policy == "source_only_zero_defaults"
    if zero_defaults and not include_k:
        k_value, k_default_reason = 0.0, "disabled"
    elif zero_defaults and not k_known:
        k_value, k_default_reason = 0.0, "missing"
    elif include_k and k_known:
        table = record if record["kind"] == "tabulated" else k_record
        k_unit = table["wavelength_unit"]
        k_low, k_high = table["wavelength_min"], table["wavelength_max"]
        # Use the original wavelength, never the n endpoint-clamped wavelength.
        x_k = query_nm / (1000.0 if k_unit == "um" else 1.0)
        x_k = _normalized_endpoint(x_k, k_low, k_high)
        if zero_defaults and not k_low <= x_k <= k_high:
            k_value, k_default_reason = 0.0, "outside_source_range"
        else:
            x_k, k_side = _bounded_query(x_k, k_low, k_high, extrapolation_policy, "k")
            k_value, k_segment = _linear_table_value(table["wavelengths"], table["k"], x_k, k_unit)
            k_evaluation_nm = _nm(x_k, k_unit)

    n_range_nm = [_nm(n_low, n_unit), _nm(n_high, n_unit)]
    details = {
        "query_wavelength_nm": query_nm,
        "n_evaluation_wavelength_nm": _nm(x_n, n_unit),
        "n_source_range_nm": n_range_nm,
        "n_method": n_method,
        "n_extrapolation_side": n_side,
        "n_interpolation_segment_nm": n_segment,
        "k_known": k_known,
        "k_evaluation_wavelength_nm": k_evaluation_nm,
        "k_sampling_policy": k_sampling_policy,
        "k_default_reason": k_default_reason,
        "k_source_range_nm": k_range_nm,
        "k_method": k_method,
        "k_extrapolation_side": k_side,
        "k_interpolation_segment_nm": k_segment,
        "extrapolation_policy": extrapolation_policy,
        "k_disabled": not include_k,
    }
    return n_value, k_value, details


def evaluate_optical_set(record, wavelength, wavelength_unit, extrapolation_policy="error"):
    """Return (n,k); tables use linear interpolation and k stays None if unknown."""
    n, k, _details = evaluate_optical_set_with_details(
        record, wavelength, wavelength_unit, extrapolation_policy=extrapolation_policy)
    return n, k
