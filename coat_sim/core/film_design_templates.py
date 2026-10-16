"""Built-in thin-film design seeds and their runtime, source-backed formulas.

Catalog entries contain public citations and self-written formulas only. The
generator evaluates the material-library optical set selected by the user at
the requested vacuum wavelength and stores the resulting physical thicknesses.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import math
import uuid
from typing import Any

from .film_design import validate_design
from .optical_constants import prepare_optical_set

MAX_PAIRS = 64
K_TRANSPARENCY_THRESHOLD = 1e-9
_AUTOMATIC_NAME_SEPARATOR = " · "
_DEFAULT_DESIGN_NAMES = frozenset({"", "未命名膜系", "Untitled film design"})

_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "stable_id": "ar_single_qw",
        "family": "anti_reflection",
        "title": "单层四分之一波 AR",
        "description": "一层四分之一波膜；中心匹配需 nF²=n0·ns。",
        "source_url": "https://ocw.mit.edu/courses/6-974-fundamentals-of-photonics-quantum-electronics-spring-2006/98fcc94d2216c26db424e294c28d459a_mirror_inter_thn.pdf",
        "reference_section": "MIT OpenCourseWare 6.974, Fundamentals of Photonics, thin-film interference notes",
        "original_material_band": "MIT 示例取空气 n0=1、理想膜 nF=√3.5、基片 ns=3.5；未指定真实膜材料或有效数据波段。运行时只使用用户所选数据集的原始 n 波段。",
        "formula": "dF=lambda0/(4*nF); R0=((n0*ns-nF^2)/(n0*ns+nF^2))^2",
        "license_note": "仅链接 MIT OCW 资料并独立编写公式与实现；来源资料采用 CC BY-NC-SA，未复制讲义文字、图像或配方。",
        "verification_status": "公式独立实现并以解析值复核；未用文献性能替代所选材料数据。",
        "roles": ["F"],
        "role_labels": {"F": "四分之一波膜 F"},
        "default_pairs": None,
        "principle": "四分之一波膜的中心光学相位为 π/2；当 nF²=n0·ns 时，中心反射振幅相消。",
        "applicability": "均匀、各向同性、透明薄膜，正常入射，单一设计波长附近的解析起点。",
        "parameter_tips": "先按实际 nF 与 √(n0·ns) 的接近程度筛选材料，再用仿真检查带宽和角度响应。",
        "limitations": "单层匹配只约束设计点；色散、偏振、斜入射、吸收、粗糙度及有限基片背面效应均未纳入该中心公式。",
    },
    {
        "stable_id": "ar_vcoat_qq",
        "family": "anti_reflection",
        "title": "双层 Q/Q V-coat AR",
        "description": "两层 Q/Q；中心导纳匹配条件 n2/n1=√(ns/n0)。",
        "source_url": "https://atomoptics.uoregon.edu/~dsteck/teaching/optics/",
        "reference_section": "D. A. Steck, Classical and Modern Optics, §10.3.2",
        "original_material_band": "来源结构不指定材料数据带；使用各自选中数据集的原始 n 波段。",
        "formula": "d1=lambda0/(4*n1); d2=lambda0/(4*n2); Y=ns*(n1/n2)^2; n2/n1=sqrt(ns/n0) gives R0=0",
        "license_note": "仅保留 Steck 教材公开页面链接；模板公式为独立实现，不复制教材文字或图示。",
        "verification_status": "公式独立实现并以解析值复核；无材料性能或带宽保证。",
        "roles": ["F1", "F2"],
        "role_labels": {"F1": "入射侧四分之一波膜 F1", "F2": "基片侧四分之一波膜 F2"},
        "default_pairs": None,
        "principle": "两层四分之一波膜在中心波长的等效导纳为 Y=ns(n1/n2)^2；满足 n2/n1=√(ns/n0) 时中心反射为零。",
        "applicability": "均匀、各向同性、透明薄膜，正常入射；匹配条件依赖实际 n0、ns、n1、n2。",
        "parameter_tips": "改变材料或入射/基片介质会改变中心导纳；不满足匹配式时仍可作为待优化双层起点。",
        "limitations": "精确零反射只在匹配关系成立的理想中心点；不代表有限带宽、斜入射或有吸收时为零。",
    },
    {
        "stable_id": "ar_qhq_seed",
        "family": "anti_reflection",
        "title": "Q/H/Q 文献示例比较种子",
        "description": "Q/H/Q 可替换材料起点；中间半波层的中心矩阵为 −I。",
        "source_url": "https://doc.comsol.com/6.4/doc/com.comsol.help.models.roptics.antireflective_coating_multilayer/antireflective_coating_multilayer.html",
        "reference_section": "COMSOL 6.4, Antireflective Coating Multilayer example",
        "original_material_band": "COMSOL 示例指定 550 nm 设计点；该点不是材料数据带，也不会成为生成器波长限制。",
        "formula": "q=(1,2,1); d_i=q_i*lambda0/(4*n_i); Y=ns*(n1/n3)^2; middle half-wave matrix=-I",
        "license_note": "仅链接 COMSOL 官方说明；不复制其模型文件、文字、图片、材料常数或可执行配方。条目中的参考实例仅为出处元数据。",
        "verification_status": "Q/H/Q 结构与中心公式独立实现；参考示例常数仅供来源比对，不作为真实材料输入或性能验证。",
        "roles": ["F1", "F2", "F3"],
        "role_labels": {"F1": "入射侧四分之一波膜 F1", "F2": "中间半波膜 F2", "F3": "基片侧四分之一波膜 F3"},
        "default_pairs": None,
        "reference_example": {
            "wavelength_nm": 550.0,
            "incident": {"label": "air", "n": 1.0},
            "substrate": {"label": "glass", "n": 1.5},
            "roles": [
                {"role": "F1", "label": "MgF2", "n": 1.38, "thickness_nm": 99.638},
                {"role": "F2", "label": "ZrO2", "n": 2.2, "thickness_nm": 125.0},
                {"role": "F3", "label": "CeF3", "n": 1.63, "thickness_nm": 84.356},
            ],
            "usage": "文献实例元数据；不得作为材料库选择或求值回退。",
        },
        "principle": "中间半波层的中心特征矩阵为 −I；其中心功率反射率与外侧 F1/F3 两个四分之一波层相同，Y=ns(n1/n3)^2。",
        "applicability": "可替换材料的三层中心波长比较种子；正常入射、透明层。",
        "parameter_tips": "可与双层 Q/Q 条目比较相同 F1、F3 在中心波长的解析结果；可单独改变中间层材料检查非理想色散影响。",
        "limitations": "不宣称中心反射率优于双层设计或具有宽带保证；COMSOL 文献示例常数不会参与运行时计算。",
    },
    {
        "stable_id": "hr_hl_qw",
        "family": "high_reflector",
        "title": "(HL)^N 四分之一波 HR",
        "description": "入射侧 H 开始的 N 对四分之一波堆栈；默认 N=8。",
        "source_url": "https://ocw.mit.edu/courses/6-974-fundamentals-of-photonics-quantum-electronics-spring-2006/98fcc94d2216c26db424e294c28d459a_mirror_inter_thn.pdf",
        "reference_section": "MIT OpenCourseWare 6.974 §2.3.9 ideal-index example; Oregon State University provides a related design with an extra terminal H layer",
        "original_material_band": "MIT 图 2.54–2.55 示例：800 nm、8 对理想高/低折射率 n=2.4/1.45 的交替层，嵌入低折射率介质；未给真实材料名或有效数据波段。此生成模板明确采用 H 先行，中心公式由独立矩阵实现核验。运行时使用所选数据集原始 n。",
        "formula": "dH=lambda0/(4*nH); dL=lambda0/(4*nL); Y=ns*(nH/nL)^(2N); R0=((Y-n0)/(Y+n0))^2; this template starts with H",
        "license_note": "仅保留 MIT OCW 与 Oregon State 公开来源链接并独立实现公式；不复制来源文字、图像或配方。未对 Oregon State 网页许可作推断。",
        "verification_status": "中心导纳公式独立实现并以解析值复核；未验证特定真实材料的谱性能。",
        "roles": ["H", "L"],
        "role_labels": {"H": "高折射率 H", "L": "低折射率 L"},
        "default_pairs": 8,
        "principle": "每个 HL 四分之一波对把中心导纳按 (nH/nL)^2 缩放；N 对后 Y=ns(nH/nL)^(2N)。",
        "applicability": "均匀、各向同性、透明层，正常入射，有限整数对数；实际材料须满足 nH>nL。",
        "parameter_tips": "在目标波段按实际端口与 H/L 折射率比较 pairs；增加 pairs 通常增强目标停带内的镜强度，但任意端口不保证中心反射单调提高。更多对数会增加总厚度和制造敏感性，带宽需扫谱评估。",
        "limitations": "公式针对设计中心的理想实 n；未含吸收、色散、应力、粗糙度、角度、偏振或基片背面反射。",
    },
    {
        "stable_id": "fp_halfwave_defect",
        "family": "resonant_cavity",
        "title": "(HL)^N C (LH)^N 半波缺陷 FP",
        "description": "对称四分之一波反射镜夹半波缺陷层；默认 N=8、C=H。",
        "source_url": "https://sites.science.oregonstate.edu/~mcintyre/mirrors.html",
        "reference_section": "Oregon State University, Dielectric Mirrors (g(HL)^2 HH (LH)^2 a defect example); MIT OpenCourseWare 6.974 supplies the half-wave matrix method",
        "original_material_band": "Oregon State 示例约 λ=470 nm，结构为玻璃入射→(HL)^2 HH (LH)^2→空气，H/L 标为 TiO2/AlPO；未指定真实数据库数据带。示例性能不作为验证或输入；运行时使用所选数据集原始 n。",
        "formula": "qC=2; front=(HL)^N; back=(LH)^N; C half-wave matrix=-I; T0=4*n0*ns/(n0+ns)^2; n0=ns implies T0=1",
        "license_note": "以 Oregon State 公开页面链接记录主结构，仅链接 MIT OCW 作为半波矩阵辅助来源；独立编写公式，未复制来源文字、图像或模型。MIT 讲义采用 CC BY-NC-SA，但未将其许可混入本项目代码许可；未对 Oregon State 网页许可作推断。",
        "verification_status": "对称中心传输条件独立实现并以解析值复核；未验证特定真实材料的共振带宽或峰值。",
        "roles": ["H", "L", "C"],
        "role_labels": {"H": "高折射率 H", "L": "低折射率 L", "C": "半波缺陷层 C（默认沿用 H）"},
        "default_role_bindings": {"C": "H"},
        "default_pairs": 8,
        "principle": "两侧对称 Bragg 堆栈夹半波缺陷层；缺陷层中心矩阵为 −I。理想无损半无限端口中心透射 T0=4*n0*ns/(n0+ns)^2；只有入射介质和基片折射率相等时 T0=1。",
        "applicability": "正常入射、透明实折射率层，整数 pairs，nH>nL；对称入射/基片边界时中心可达理想透射。",
        "parameter_tips": "C 默认复制 H 的材料数据集，可在生成前单独修改；改变 pairs 会改变反射镜强度和总厚度。",
        "limitations": "不对称边界不保证 T=1；未含吸收、色散、角度、偏振、界面误差和有限基片背面效应，仍需扫谱优化。",
    },
)


class TemplateGenerationError(ValueError):
    """A selected template input cannot produce a valid physical seed."""


def template_catalog() -> list[dict[str, Any]]:
    """Return an independent copy of built-in public template metadata."""
    return deepcopy(list(_CATALOG))


def template_roles(template_id: str) -> list[dict[str, str]]:
    template = _template(template_id)
    labels = template["role_labels"]
    return [{"role": role, "label": labels[role]} for role in template["roles"]]


def _template(template_id: str) -> dict[str, Any]:
    identity = str(template_id or "")
    for row in _CATALOG:
        if row["stable_id"] == identity:
            return row
    raise TemplateGenerationError(f"未找到内置模板：{identity or '（空）'}")


def _generated_template_title(template: Mapping[str, Any], pairs: int) -> str:
    """Return a concise, recognizable title with the actual pair count."""
    identity = template["stable_id"]
    if identity == "ar_single_qw":
        return "AR single-QW"
    if identity == "ar_vcoat_qq":
        return "AR Q/Q V-coat"
    if identity == "ar_qhq_seed":
        return "AR Q/H/Q"
    if identity == "hr_hl_qw":
        return f"HR (HL)^{pairs}"
    if identity == "fp_halfwave_defect":
        return f"FP (HL)^{pairs} C (LH)^{pairs}"
    raise TemplateGenerationError("模板缺少简洁名称。")


def _automatic_template_name(template: Mapping[str, Any], pairs: int,
                             wavelength_nm: float) -> str:
    return (f"{_generated_template_title(template, pairs)}"
            f"{_AUTOMATIC_NAME_SEPARATOR}{wavelength_nm:g}nm")


def _is_automatic_template_name(name: str) -> bool:
    """Recognize only names emitted by this generator or its known old format.

    Old generated names used a known default-name prefix and catalog title(s).
    Do not use the free-form design note as an identity marker: users can edit
    it, and it is not authoritative about who chose the design name.
    """
    if name in _DEFAULT_DESIGN_NAMES:
        return True

    for template in _CATALOG:
        counts = range(1, MAX_PAIRS + 1) if template.get("default_pairs") is not None else (0,)
        for pairs in counts:
            title_prefix = _generated_template_title(template, pairs) + _AUTOMATIC_NAME_SEPARATOR
            if not name.startswith(title_prefix) or not name.endswith("nm"):
                continue
            wavelength_text = name[len(title_prefix):-2]
            try:
                wavelength = float(wavelength_text)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(wavelength) and wavelength > 0:
                return True

    # Previous versions appended " - {template title}" every time. Only
    # collapse the exact legacy grammar anchored at a known default name;
    # arbitrary user names and partially recognizable/truncated suffixes stay
    # untouched.
    known_titles = {str(template["title"]) for template in _CATALOG}
    for default_name in (*_DEFAULT_DESIGN_NAMES, "膜系"):
        legacy_prefix = f"{default_name} - "
        if not name.startswith(legacy_prefix):
            continue
        suffixes = name[len(legacy_prefix):].split(" - ")
        return bool(suffixes) and all(suffix in known_titles for suffix in suffixes)
    return False


def _finite_positive(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise TemplateGenerationError(f"{label} 必须是有限正数。")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TemplateGenerationError(f"{label} 必须是有限正数。") from exc
    if not math.isfinite(number) or number <= 0:
        raise TemplateGenerationError(f"{label} 必须是有限正数。")
    return number


def _selected_record(options: Mapping[str, Any], purpose: str,
                     binding: Mapping[str, Any], role_label: str
                     ) -> tuple[dict[str, Any], dict[str, Any]]:
    material_id = str(binding.get("material_id") or "")
    dataset_id = str(binding.get("optical_set_id") or "")
    if not material_id or not dataset_id:
        raise TemplateGenerationError(f"{role_label} 尚未选择材料和光学数据集。")
    if purpose == "medium":
        # The Quick entrance-medium picker intentionally combines the medium,
        # film, and substrate library catalogs. Honor that exact selected
        # record here; never infer a material from its label or guess its n/k.
        materials = [row for key in ("medium", "film", "substrate")
                     for row in options.get(key, [])]
    else:
        materials = options.get(purpose, [])
    material = next((row for row in materials
                     if str(row.get("id", "")) == material_id), None)
    if material is None:
        raise TemplateGenerationError(f"{role_label} 的材料不在当前材料库中。")
    optical = next((row for row in material.get("optical_sets", [])
                    if str(row.get("id", "")) == dataset_id), None)
    if optical is None:
        raise TemplateGenerationError(f"{role_label} 的光学数据集不属于所选材料。")
    return material, optical


def _evaluate_selected(options: Mapping[str, Any], purpose: str,
                       binding: Mapping[str, Any], role_label: str,
                       wavelength_nm: float, assume_lossless: bool, *,
                       require_transparent: bool = True
                       ) -> dict[str, Any]:
    material, optical = _selected_record(options, purpose, binding, role_label)
    dataset_id = str(optical.get("id", ""))
    try:
        evaluator = prepare_optical_set(optical, k_sampling_policy="source")
        n_value, _ignored, n_details = evaluator(
            wavelength_nm, "nm", extrapolation_policy="error", include_k=False)
    except (ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
        raise TemplateGenerationError(
            f"{role_label} 数据集 {dataset_id} 无法在 {wavelength_nm:g} nm 求值 n：{exc}") from exc
    try:
        n = float(n_value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TemplateGenerationError(f"{role_label} 的 n({wavelength_nm:g} nm) 无效。") from exc
    if not math.isfinite(n) or n <= 0:
        raise TemplateGenerationError(f"{role_label} 的 n({wavelength_nm:g} nm) 必须有限且大于零。")

    k: float | None = None
    k_unknown_reason = ""
    if not bool(n_details.get("k_known", False)):
        k_unknown_reason = "数据集未提供 k"
    else:
        try:
            _n_check, k_value, k_details = evaluator(
                wavelength_nm, "nm", extrapolation_policy="error", include_k=True)
            if k_value is None:
                k_unknown_reason = "数据集没有可用的目标波长 k 值"
            else:
                k = float(k_value)
                if not math.isfinite(k):
                    raise TemplateGenerationError(f"{role_label} 的 k({wavelength_nm:g} nm) 无效。")
                if k < 0:
                    raise TemplateGenerationError(
                        f"{role_label} 的 k({wavelength_nm:g} nm)={k:g} 表示增益或无效吸收数据；拒绝使用。")
                if require_transparent and abs(k) > K_TRANSPARENCY_THRESHOLD:
                    raise TemplateGenerationError(
                        f"{role_label} 的 k({wavelength_nm:g} nm)={k:g} 超过无损模板阈值 "
                        f"{K_TRANSPARENCY_THRESHOLD:g}；请选择透明数据集。")
        except TemplateGenerationError:
            raise
        except (ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
            source_range = n_details.get("k_source_range_nm")
            outside_known_k = (
                isinstance(source_range, (list, tuple)) and len(source_range) == 2
                and not float(source_range[0]) <= wavelength_nm <= float(source_range[1])
            )
            if not outside_known_k:
                raise TemplateGenerationError(
                    f"{role_label} 数据集 {dataset_id} 无法在目标波长检查 k：{exc}") from exc
            k_unknown_reason = (
                f"k 来源波段 {float(source_range[0]):g}–{float(source_range[1]):g} nm "
                "未覆盖目标波长")

    if k is None and not assume_lossless:
        raise TemplateGenerationError(
            f"{role_label} 的 k 在 {wavelength_nm:g} nm 未知（{k_unknown_reason}）；"
            "请显式勾选“对未知 k 假设无损”或更换数据集。")

    unit = str(optical.get("wavelength_unit", "nm"))
    scale_to_nm = 1000.0 if unit == "um" else 1.0
    source_low = optical.get("wavelength_min")
    source_high = optical.get("wavelength_max")
    band_nm = None
    if source_low is not None and source_high is not None:
        try:
            band_nm = [float(source_low) * scale_to_nm,
                       float(source_high) * scale_to_nm]
        except (TypeError, ValueError, OverflowError):
            band_nm = None
    return {
        "material_id": str(material.get("id", "")),
        "material_name": str(material.get("name") or material.get("id") or ""),
        "optical_set_id": dataset_id,
        "optical_set_name": str(optical.get("name") or dataset_id),
        "n": n,
        "k": k,
        "k_unknown_reason": k_unknown_reason,
        "source_band_nm": band_nm,
    }


def _reflectance_from_log_admittance(n0: float, log_y: float) -> float:
    return math.tanh(0.5 * (log_y - math.log(n0))) ** 2


def _reference_results(template: Mapping[str, Any], indices: Mapping[str, float],
                       wavelength_nm: float, pairs: int) -> dict[str, float | int | bool]:
    n0 = indices["n0"]
    ns = indices["ns"]
    identity = template["stable_id"]
    if identity == "ar_single_qw":
        log_y = 2.0 * math.log(indices["F"]) - math.log(ns)
        reflectance = _reflectance_from_log_admittance(n0, log_y)
        return {"center_reflectance": reflectance}
    if identity == "ar_vcoat_qq":
        log_y = math.log(ns) + 2.0 * (math.log(indices["F1"]) - math.log(indices["F2"]))
        try:
            ideal_ratio = math.exp(0.5 * (math.log(ns) - math.log(n0)))
        except OverflowError as exc:
            raise TemplateGenerationError("V-coat 理想折射率比超出可表示范围。") from exc
        if not math.isfinite(ideal_ratio):
            raise TemplateGenerationError("V-coat 理想折射率比不是有限值。")
        return {"center_reflectance": _reflectance_from_log_admittance(n0, log_y),
                "ideal_match_ratio_n2_over_n1": ideal_ratio}
    if identity == "ar_qhq_seed":
        log_y = math.log(ns) + 2.0 * (math.log(indices["F1"]) - math.log(indices["F3"]))
        return {"center_reflectance": _reflectance_from_log_admittance(n0, log_y),
                "middle_half_wave_matrix_is_minus_identity": True}
    if identity == "hr_hl_qw":
        log_y = math.log(ns) + 2.0 * pairs * (
            math.log(indices["H"]) - math.log(indices["L"]))
        return {"center_reflectance": _reflectance_from_log_admittance(n0, log_y),
                "effective_admittance_log": log_y,
                "pairs": pairs}
    if identity == "fp_halfwave_defect":
        reflectance = _reflectance_from_log_admittance(n0, math.log(ns))
        transmittance = max(0.0, 1.0 - reflectance)
        return {"center_reflectance": reflectance,
                "center_transmittance": transmittance,
                "ideal_unit_transmission": math.isclose(n0, ns, rel_tol=1e-12, abs_tol=1e-12),
                "pairs": pairs}
    raise TemplateGenerationError("模板缺少中心波长参考公式。")


def build_preview(*, inputs: Mapping[str, Any], base_design: Mapping[str, Any],
                  material_options: Mapping[str, Any]) -> dict[str, Any]:
    """Build a pure, schema-valid preview from selected catalog records.

    The returned candidate is not applied to a session. Every QW thickness is
    converted once using the selected data set's real n at lambda0 and stored
    as physical nanometres so later spectrum calculations keep that thickness.
    """
    if not isinstance(inputs, Mapping):
        raise TemplateGenerationError("生成参数必须是对象。")
    template = _template(str(inputs.get("template_id", "")))
    wavelength_nm = _finite_positive(inputs.get("wavelength_nm"), "目标真空波长 λ0（nm）")
    raw_pairs = inputs.get("pairs", template.get("default_pairs") or 0)
    if template.get("default_pairs") is None:
        pairs = 0
    else:
        if (isinstance(raw_pairs, bool) or not isinstance(raw_pairs, (int, float))
                or not math.isfinite(float(raw_pairs)) or int(raw_pairs) != float(raw_pairs)):
            raise TemplateGenerationError("pairs 必须是整数。")
        pairs = int(raw_pairs)
        if not 1 <= pairs <= MAX_PAIRS:
            raise TemplateGenerationError(f"pairs 必须为 1–{MAX_PAIRS} 的整数。")
    assume_lossless = inputs.get("assume_lossless", False)
    if not isinstance(assume_lossless, bool):
        raise TemplateGenerationError("未知 k 的无损假设必须是显式布尔值。")
    bindings = inputs.get("role_bindings", {})
    if not isinstance(bindings, Mapping):
        raise TemplateGenerationError("膜层角色绑定必须是对象。")
    for role in template["roles"]:
        if role == "C" and role not in bindings and template.get("default_role_bindings", {}).get(role) == "H":
            if "H" in bindings:
                bindings = dict(bindings)
                bindings[role] = dict(bindings["H"])
        if role not in bindings:
            raise TemplateGenerationError(f"角色 {role} 尚未选择材料和光学数据集。")

    n_by_role: dict[str, float] = {}
    evaluated_by_role: dict[str, dict[str, Any]] = {}
    role_labels = template["role_labels"]
    for role in template["roles"]:
        evaluated = _evaluate_selected(material_options, "film", bindings[role],
                                       role_labels[role], wavelength_nm, assume_lossless)
        evaluated_by_role[role] = evaluated
        n_by_role[role] = evaluated["n"]

    if template["stable_id"] in ("hr_hl_qw", "fp_halfwave_defect"):
        if not n_by_role["H"] > n_by_role["L"]:
            raise TemplateGenerationError(
                f"所选数据集在 {wavelength_nm:g} nm 不满足 nH > nL。")
    substrate = _evaluate_selected(material_options, "substrate",
                                   inputs.get("substrate_binding", {}), "基片",
                                   wavelength_nm, assume_lossless)
    incident = _evaluate_selected(material_options, "medium",
                                  inputs.get("incident_binding", {}), "入射介质",
                                  wavelength_nm, assume_lossless)
    if incident["k"] is not None and incident["k"] != 0.0:
        raise TemplateGenerationError(
            f"入射介质在 {wavelength_nm:g} nm 的已知 k={incident['k']:g} 非零；"
            "现有光谱求解器仅支持 k=0 的入射端口，请选择透明入射数据集。")
    ns, n0 = substrate["n"], incident["n"]
    indices = {**n_by_role, "n0": n0, "ns": ns}

    incidence_order: list[tuple[str, int]] = []
    identity = template["stable_id"]
    if identity == "ar_single_qw":
        incidence_order = [("F", 1)]
    elif identity == "ar_vcoat_qq":
        incidence_order = [("F1", 1), ("F2", 1)]
    elif identity == "ar_qhq_seed":
        incidence_order = [("F1", 1), ("F2", 2), ("F3", 1)]
    elif identity == "hr_hl_qw":
        incidence_order = [(role, 1) for _ in range(pairs) for role in ("H", "L")]
    elif identity == "fp_halfwave_defect":
        incidence_order = (
            [(role, 1) for _ in range(pairs) for role in ("H", "L")]
            + [("C", 2)]
            + [(role, 1) for _ in range(pairs) for role in ("L", "H")]
        )
    else:
        raise TemplateGenerationError("模板缺少膜层展开规则。")

    incident_layers: list[dict[str, Any]] = []
    preview_layers: list[dict[str, Any]] = []
    total_nm = 0.0
    for position, (role, q_value) in enumerate(incidence_order, 1):
        evaluated = evaluated_by_role[role]
        physical_nm = q_value * wavelength_nm / (4.0 * evaluated["n"])
        if not math.isfinite(physical_nm) or physical_nm <= 0:
            raise TemplateGenerationError(f"角色 {role} 的物理厚度不是有限正数。")
        total_nm += physical_nm
        if not math.isfinite(total_nm):
            raise TemplateGenerationError("总物理厚度超出有限数值范围。")
        layer = {
            "id": uuid.uuid4().hex,
            "material_id": evaluated["material_id"],
            "optical_set_id": evaluated["optical_set_id"],
            "thickness": {"value": physical_nm, "unit": "nm"},
            "note": (f"参数化模板 {template['stable_id']}；角色 {role}；"
                     f"λ0={wavelength_nm:g} nm，q={q_value:g}，n={evaluated['n']:.12g}"),
        }
        incident_layers.append(layer)
        preview_layers.append({
            "sequence": position,
            "role": role,
            "material_name": evaluated["material_name"],
            "dataset_name": evaluated["optical_set_name"],
            "dataset_id": evaluated["optical_set_id"],
            "n_at_wavelength": evaluated["n"],
            "k_at_wavelength": evaluated["k"],
            "qw_multiplier": q_value,
            "physical_thickness_nm": physical_nm,
            "source_band_nm": evaluated["source_band_nm"],
        })

    reference = _reference_results(template, indices, wavelength_nm, pairs)
    assumptions = [
        "法向入射；均匀、各向同性的透明薄膜；使用实折射率中心公式。",
        "按每个所选数据集在 λ0 的 n 求一次物理厚度；扫谱时厚度固定。",
        "front_layers 已按 schema v1 要求反转为 substrate_outward；back_layers 为空。",
        "生成单面正面涂层；清空背面层并将基片设为 single，保留原基片厚度值。",
    ]
    unknown = [
        f"{label}：{record['k_unknown_reason']}"
        for label, record in [
            *((role_labels[role], evaluated_by_role[role]) for role in template["roles"]),
            ("基片", substrate), ("入射介质", incident),
        ] if record["k"] is None
    ]
    if unknown:
        assumptions.append("用户显式允许未知 k 按无损处理：" + "；".join(unknown))

    design = deepcopy(dict(base_design))
    design["id"] = uuid.uuid4().hex
    base_name = str(base_design.get("name") or "")
    if _is_automatic_template_name(base_name):
        design["name"] = _automatic_template_name(template, pairs, wavelength_nm)
    else:
        design["name"] = base_name
    design["reference_wavelength_nm"] = wavelength_nm
    design["incident_medium"] = {
        "kind": "material", "material_id": incident["material_id"],
        "optical_set_id": incident["optical_set_id"],
    }
    design["substrate"]["material_id"] = substrate["material_id"]
    design["substrate"]["optical_set_id"] = substrate["optical_set_id"]
    design["substrate"]["sides"] = "single"
    design["front_layers"] = list(reversed(incident_layers))
    design["back_layers"] = []
    design["first_surface"] = "front"
    design["assume_lossless"] = assume_lossless
    design["use_k"] = True
    design["expression"] = {"text": "", "symbols": {}, "in_sync": True}
    note_lines = [
        f"由内置经典膜系模板 {template['stable_id']} 生成。",
        f"λ0={wavelength_nm:g} nm；pairs={pairs if template.get('default_pairs') is not None else '不适用'}。",
        f"入射介质={incident['material_name']} / {incident['optical_set_name']}，n={n0:.12g}。",
        f"基片={substrate['material_name']} / {substrate['optical_set_name']}，n={ns:.12g}。",
        "膜层绑定与目标波长实际 n/k 见各层备注；" + "；".join(assumptions),
        "来源：" + template["source_url"],
    ]
    design["note"] = "\n".join(note_lines)
    try:
        validated_design = validate_design(design)
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise TemplateGenerationError(f"生成候选未通过设计格式校验：{exc}") from exc

    material_summary = [
        {"role": role, **evaluated_by_role[role]}
        for role in template["roles"]
    ] + [{"role": "基片", **substrate}, {"role": "入射介质", **incident}]
    return {
        "template": deepcopy(template),
        "layers_incident_order": preview_layers,
        "layer_count": len(preview_layers),
        "total_physical_thickness_nm": total_nm,
        "wavelength_nm": wavelength_nm,
        "pairs": pairs,
        "materials": material_summary,
        "assumptions": assumptions,
        "principle": template["principle"],
        "applicability": template["applicability"],
        "parameter_tips": template["parameter_tips"],
        "limitations": template["limitations"],
        "formula": template["formula"],
        "reference_results": reference,
        "candidate_design": validated_design,
    }

