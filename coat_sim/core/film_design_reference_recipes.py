"""Fixed, source-described thin-film reference recipes and strict mappings.

The numeric layer tables below are an independent transcription of public
nominal examples. They are not material-library datasets or a performance
claim. Mapping evaluates the user's selected datasets at the fixed design
wavelength and preserves every recipe thickness in physical nanometres.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import math
import uuid
from typing import Any

from .film_design import validate_design
from .film_design_templates import TemplateGenerationError, _evaluate_selected


_SOURCE_URL = (
    "https://doc.comsol.com/6.4/doc/com.comsol.help.models.roptics."
    "antireflective_coating_multilayer/antireflective_coating_multilayer.html"
)
_SOURCE_TITLE = "Antireflective Coating with Multiple Layers"
_VERSION = "6.4"
_CHECKED_DATE = "2026-10-11"
_LAMBDA0_NM = 550.0
_TF_SOURCE_URL = "https://www.hulinks.co.jp/en/tfcalc/examples/list-of-coating-design/growing-antireflection/"
_NOMINAL_INCIDENT = {"label": "air", "n": 1.0, "k": 0.0,
                     "model_note": "Nominal transparent tutorial model; k=0 is not a measured k dataset."}
_NOMINAL_SUBSTRATE = {"label": "glass", "n": 1.5, "k": 0.0,
                      "model_note": "Nominal transparent tutorial model; k=0 is not a measured k dataset."}


def _layer(sequence: int, role: str, label: str, nominal_n: float,
           q: float) -> dict[str, Any]:
    if q == 2.0:
        thickness_nm = _LAMBDA0_NM / (2.0 * nominal_n)
        thickness_formula = f"550 nm / (2 * {nominal_n:g})"
    else:
        thickness_nm = _LAMBDA0_NM / (4.0 * nominal_n)
        thickness_formula = f"550 nm / (4 * {nominal_n:g})"
    return {
        "sequence": sequence,
        "role": role,
        "label": label,
        "nominal_material": label,
        "nominal_n": nominal_n,
        "nominal_k": 0.0,
        "qw_multiplier": q,
        "thickness_formula": thickness_formula,
        "thickness_nm": thickness_nm,
    }


def _fixed_layer(sequence: int, role: str, label: str, nominal_n: float,
                 thickness_nm: float) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "role": role,
        "label": label,
        "nominal_material": label,
        "nominal_n": nominal_n,
        "nominal_k": 0.0,
        "qw_multiplier": None,
        "thickness_formula": "Source fixed physical thickness; not a quarter-wave layer",
        "thickness_nm": thickness_nm,
    }


_COMMON_SOURCE_LIMIT = (
    "COMSOL 6.4 tutorial design point is 550 nm. Its 400–800 nm interval is an "
    "example scan range, not a validated material-data band. Nominal constants "
    "are tutorial-model values; k=0 is this reference's transparent-model "
    "assumption, not measured material data or a material-library fallback."
)

_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "stable_id": "comsol_ideal_single_ar",
        "family": "anti_reflection",
        "title": "Ideal single-layer AR reference",
        "description": "Project-fixed quarter-wave reference using the public ideal matching formula.",
        "source_url": _SOURCE_URL,
        "source_title": _SOURCE_TITLE,
        "version": _VERSION,
        "publication_date": None,
        "checked_date": _CHECKED_DATE,
        "reference_section": "Project-fixed formula example; the source tutorial does not publish this as a material recipe.",
        "origin": "project_formula_fixed_reference",
        "license_note": "No source text, image, software model, or material dataset is copied. The fixed layer is independently constructed from the public quarter-wave matching formula; no open-license claim is made for the linked source.",
        "original_material_band": _COMMON_SOURCE_LIMIT,
        "verification_status": "Fixed nominal layer thickness is independently calculated from the stated formula; no mapped-material performance is guaranteed.",
        "lambda0_nm": _LAMBDA0_NM,
        "nominal_endpoints": {
            "incident": deepcopy(_NOMINAL_INCIDENT),
            "substrate": deepcopy(_NOMINAL_SUBSTRATE),
        },
        "layers": (_layer(1, "F", "ideal matched layer", math.sqrt(1.5), 1.0),),
        "roles": ({"role": "F", "label": "F", "nominal_material": "ideal matched layer",
                   "nominal_n": math.sqrt(1.5)},),
        "principle": "The ideal single-layer matching index is sqrt(n0*ns); this fixed reference uses n0=1 and ns=1.5.",
        "limitations": "This is a project-fixed ideal formula example rather than a published material recipe. Mapping to a real dataset changes the layer index while the nominal physical thickness remains fixed.",
    },
    {
        "stable_id": "comsol_qq_mgf2_cef3",
        "family": "anti_reflection",
        "title": "MgF2 / CeF3 Q/Q reference",
        "description": "Two nominal quarter-wave layers from the COMSOL tutorial example.",
        "source_url": _SOURCE_URL,
        "source_title": _SOURCE_TITLE,
        "version": _VERSION,
        "publication_date": None,
        "checked_date": _CHECKED_DATE,
        "reference_section": "COMSOL 6.4 tutorial, 550 nm antireflective multilayer example.",
        "origin": "public_example_structure_represented_as_fixed_seed",
        "license_note": "Only source-linked numeric example facts are independently represented as metadata. No tutorial prose, image, software model, or material dataset is copied; no open-license claim is made for the linked source.",
        "original_material_band": _COMMON_SOURCE_LIMIT,
        "verification_status": "Nominal layer table transcribed and independently checked; mapped designs require explicit real dataset evaluation and have no inherited performance guarantee.",
        "lambda0_nm": _LAMBDA0_NM,
        "nominal_endpoints": {
            "incident": deepcopy(_NOMINAL_INCIDENT),
            "substrate": deepcopy(_NOMINAL_SUBSTRATE),
        },
        "layers": (
            _layer(1, "F1", "MgF2", 1.38, 1.0),
            _layer(2, "F2", "CeF3", 1.63, 1.0),
        ),
        "roles": (
            {"role": "F1", "label": "F1", "nominal_material": "MgF2", "nominal_n": 1.38},
            {"role": "F2", "label": "F2", "nominal_material": "CeF3", "nominal_n": 1.63},
        ),
        "principle": "A fixed two-layer quarter-wave comparison seed; the tutorial scan is not a material validity band.",
        "limitations": "The nominal constants are not imported datasets. Selected material indices may differ, so the preserved physical thicknesses are generally no longer quarter-wave after mapping.",
    },
    {
        "stable_id": "comsol_qhq_mgf2_zro2_cef3",
        "family": "anti_reflection",
        "title": "MgF2 / ZrO2 / CeF3 Q/H/Q reference",
        "description": "A quarter-wave / half-wave / quarter-wave comparison seed using the COMSOL tutorial values.",
        "source_url": _SOURCE_URL,
        "source_title": _SOURCE_TITLE,
        "version": _VERSION,
        "publication_date": None,
        "checked_date": _CHECKED_DATE,
        "reference_section": "COMSOL 6.4 tutorial, 550 nm antireflective multilayer example.",
        "origin": "public_example_structure_represented_as_fixed_seed",
        "license_note": "Only source-linked numeric example facts are independently represented as metadata. No tutorial prose, image, software model, or material dataset is copied; no open-license claim is made for the linked source.",
        "original_material_band": _COMMON_SOURCE_LIMIT,
        "verification_status": "Nominal layer table transcribed and independently checked; mapped designs require explicit real dataset evaluation and have no inherited performance guarantee.",
        "lambda0_nm": _LAMBDA0_NM,
        "nominal_endpoints": {
            "incident": deepcopy(_NOMINAL_INCIDENT),
            "substrate": deepcopy(_NOMINAL_SUBSTRATE),
        },
        "layers": (
            _layer(1, "F1", "MgF2", 1.38, 1.0),
            _layer(2, "F2", "ZrO2", 2.2, 2.0),
            _layer(3, "F3", "CeF3", 1.63, 1.0),
        ),
        "roles": (
            {"role": "F1", "label": "F1", "nominal_material": "MgF2", "nominal_n": 1.38},
            {"role": "F2", "label": "F2", "nominal_material": "ZrO2", "nominal_n": 2.2},
            {"role": "F3", "label": "F3", "nominal_material": "CeF3", "nominal_n": 1.63},
        ),
        "principle": "The middle layer uses a half-wave nominal thickness; the tutorial values define a comparison seed, not a mapped performance result.",
        "limitations": "After mapping, the selected datasets can change all layer indices. Physical thicknesses remain those in the nominal source table and are not recomputed from mapped indices.",
    },
    {
        "stable_id": "tfcalc_growing_ar_13",
        "family": "anti_reflection",
        "title": "TFCalc growing antireflection · 13 fixed layers",
        "description": "Fixed-thickness example in incidence-to-substrate order; role bindings are selected explicitly.",
        "source_url": _TF_SOURCE_URL,
        "source_title": "TFCalc: Growing an Antireflection Coating",
        "version": "未标注（官方公开示例）",
        "publication_date": None,
        "checked_date": _CHECKED_DATE,
        "reference_section": "TFCalc official example page; source table is substrate-outward and is reversed here for incidence-to-substrate display.",
        "origin": "public_example_fixed_physical_thicknesses",
        "license_note": "Only independently organized numeric facts from the linked public example are represented. No TFD/demo file, source prose, image, software model, or material dataset is copied; no open-license claim is made for the linked page.",
        "original_material_band": "Reference wavelength 10000 nm; source target interval 7700–12300 nm is a design target, not a validated material-data band. Nominal H=Ge n=4.2, L=ZnS n=2.2, substrate Ge n=4.0; incident air n=1.0. Normal incidence and k=0 are project recomputation assumptions; the source page does not specify them.",
        "verification_status": "Fixed source thickness table checked after reversing the published substrate-outward order. The source's 0.9% figure is a target, not a mapped performance guarantee.",
        "lambda0_nm": 10000.0,
        "nominal_endpoints": {
            "incident": {"label": "air", "n": 1.0, "k": 0.0,
                         "model_note": "Project recomputation assumption; source page does not specify incident-port k."},
            "substrate": {"label": "Ge substrate", "n": 4.0, "k": 0.0,
                          "model_note": "Nominal source value; k=0 is a project recomputation assumption, not measured k data."},
        },
        "roles": (
            {"role": "H", "label": "H", "nominal_material": "Ge", "nominal_n": 4.2},
            {"role": "L", "label": "L", "nominal_material": "ZnS", "nominal_n": 2.2},
        ),
        # Source table is substrate-outward. Store the public catalog in the
        # displayed incidence-to-substrate order; schema v1 reverses again.
        "layers": tuple(
            _fixed_layer(i, role, material, n, d)
            for i, (role, material, n, d) in enumerate((
                ("L", "ZnS", 2.2, 1076.97),
                ("H", "Ge", 4.2, 629.03),
                ("L", "ZnS", 2.2, 457.49),
                ("H", "Ge", 4.2, 131.55),
                ("L", "ZnS", 2.2, 1234.92),
                ("H", "Ge", 4.2, 1142.59),
                ("L", "ZnS", 2.2, 2590.54),
                ("H", "Ge", 4.2, 81.75),
                ("L", "ZnS", 2.2, 1827.50),
                ("H", "Ge", 4.2, 84.60),
                ("L", "ZnS", 2.2, 563.34),
                ("H", "Ge", 4.2, 285.97),
                ("L", "ZnS", 2.2, 157.14),
            ), 1)
        ),
        "principle": "A growing antireflection example with source-prescribed physical layer thicknesses. Mapping changes optical constants only; it does not regenerate optical thicknesses.",
        "limitations": "The source target interval and target reflectance are not guarantees after mapping. Selected library records must cover 10000 nm; all thirteen source thicknesses stay fixed in nm.",
    },
)


def reference_recipe_catalog() -> list[dict[str, Any]]:
    """Return detached, JSON-serializable public recipe metadata."""
    return [_json_compatible(row) for row in _CATALOG]


def _json_compatible(value: Any) -> Any:
    """Copy catalog values into JSON's container types without hiding NaN."""
    if isinstance(value, Mapping):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_compatible(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise TemplateGenerationError("Reference catalog contains a non-finite number.")
    return deepcopy(value)


def _recipe(recipe_id: object) -> dict[str, Any]:
    identity = str(recipe_id or "")
    for row in _CATALOG:
        if row["stable_id"] == identity:
            return row
    raise TemplateGenerationError(f"未知公开参考配方：{identity or '（未选择）'}")


def _binding(inputs: Mapping[str, Any], key: str, label: str) -> Mapping[str, Any]:
    value = inputs.get(key)
    if not isinstance(value, Mapping):
        raise TemplateGenerationError(f"{label} mapping must select a material and dataset.")
    return value


def build_reference_recipe_preview(*, inputs: Mapping[str, Any],
                                   base_design: Mapping[str, Any],
                                   material_options: Mapping[str, Any]) -> dict[str, Any]:
    """Map one fixed reference recipe onto selected real library records.

    This function only evaluates point n/k and constructs a schema-v1 design.
    It does not calculate mapped reflectance or transmission. Every source
    physical thickness is copied unchanged into the candidate.
    """
    if not isinstance(inputs, Mapping):
        raise TemplateGenerationError("Reference recipe inputs must be a map.")
    if not isinstance(base_design, Mapping):
        raise TemplateGenerationError("Current design must be a map.")
    if not isinstance(material_options, Mapping):
        raise TemplateGenerationError("Material library records are unavailable.")
    recipe = _recipe(inputs.get("recipe_id"))
    wavelength_nm = float(recipe["lambda0_nm"])
    if "wavelength_nm" in inputs:
        requested = inputs["wavelength_nm"]
        if isinstance(requested, bool):
            raise TemplateGenerationError(f"该参考配方的设计波长固定为 {wavelength_nm:g} nm。")
        try:
            requested_nm = float(requested)
        except (TypeError, ValueError, OverflowError) as exc:
            raise TemplateGenerationError(f"该参考配方的设计波长固定为 {wavelength_nm:g} nm。") from exc
        if not math.isfinite(requested_nm) or requested_nm != wavelength_nm:
            raise TemplateGenerationError(f"该参考配方的设计波长固定为 {wavelength_nm:g} nm。")
    if inputs.get("mapping_confirmed") is not True:
        raise TemplateGenerationError("Confirm the explicit material and dataset mapping first.")
    assume_lossless = inputs.get("assume_lossless", False)
    if not isinstance(assume_lossless, bool):
        raise TemplateGenerationError("Unknown-k lossless assumption must be a boolean.")

    roles = list(dict.fromkeys(str(row["role"]) for row in recipe["layers"]))
    bindings = inputs.get("role_bindings")
    if not isinstance(bindings, Mapping):
        raise TemplateGenerationError("Select a real material and dataset for every layer role.")
    if set(bindings) != set(roles):
        missing = [role for role in roles if role not in bindings]
        extra = [str(role) for role in bindings if role not in roles]
        detail = ", ".join(missing or extra)
        raise TemplateGenerationError(f"Layer role mapping is incomplete or unexpected: {detail}.")

    evaluated_roles: dict[str, dict[str, Any]] = {}
    for source_layer in recipe["layers"]:
        role = str(source_layer["role"])
        evaluated_roles[role] = _evaluate_selected(
            material_options, "film", bindings[role],
            f"{role} ({source_layer['label']})", wavelength_nm, assume_lossless,
            require_transparent=False)
    incident = _evaluate_selected(
        material_options, "medium", _binding(inputs, "incident_binding", "Incident medium"),
        "Incident medium", wavelength_nm, assume_lossless, require_transparent=False)
    if incident["k"] is not None and float(incident["k"]) != 0.0:
        raise TemplateGenerationError(
            "当前光谱求解器要求入射端口 k=0；已知非零 k 不会被清零，请选择无损入射数据集。")
    substrate = _evaluate_selected(
        material_options, "substrate", _binding(inputs, "substrate_binding", "Substrate"),
        "Substrate", wavelength_nm, assume_lossless, require_transparent=False)

    nominal_layers = deepcopy(list(recipe["layers"]))
    mapped_layers: list[dict[str, Any]] = []
    candidate_layers_incident_order: list[dict[str, Any]] = []
    total_nm = 0.0
    unknown_k: list[str] = []
    for sequence, source_layer in enumerate(recipe["layers"], 1):
        role = str(source_layer["role"])
        evaluated = evaluated_roles[role]
        physical_nm = float(source_layer["thickness_nm"])
        if not math.isfinite(physical_nm) or physical_nm <= 0:
            raise TemplateGenerationError(f"Reference layer {role} has an invalid fixed thickness.")
        total_nm += physical_nm
        if not math.isfinite(total_nm):
            raise TemplateGenerationError("Reference total thickness is outside the finite numeric range.")
        delta_n = float(evaluated["n"]) - float(source_layer["nominal_n"])
        delta_k = (None if evaluated["k"] is None else
                   float(evaluated["k"]) - float(source_layer["nominal_k"]))
        mapped_layers.append({
            "sequence": sequence,
            "role": role,
            "nominal_material": str(source_layer["nominal_material"]),
            "nominal_n": float(source_layer["nominal_n"]),
            "nominal_k": float(source_layer["nominal_k"]),
            "material_id": evaluated["material_id"],
            "material_name": evaluated["material_name"],
            "optical_set_id": evaluated["optical_set_id"],
            "optical_set_name": evaluated["optical_set_name"],
            "n_at_wavelength": float(evaluated["n"]),
            "k_at_wavelength": evaluated["k"],
            "k_unknown_reason": evaluated["k_unknown_reason"],
            "delta_n": delta_n,
            "delta_k": delta_k,
            "qw_multiplier": (None if source_layer["qw_multiplier"] is None
                              else float(source_layer["qw_multiplier"])),
            "fixed_thickness_nm": physical_nm,
            "source_band_nm": evaluated["source_band_nm"],
        })
        if evaluated["k"] is None:
            unknown_k.append(f"{role}: {evaluated['k_unknown_reason']}")
        candidate_layers_incident_order.append({
            "id": uuid.uuid4().hex,
            "material_id": evaluated["material_id"],
            "optical_set_id": evaluated["optical_set_id"],
            "thickness": {"value": physical_nm, "unit": "nm"},
            "note": (
                f"Reference recipe mapping {recipe['stable_id']} role {role}; "
                f"nominal n={float(source_layer['nominal_n']):.12g}; "
                f"mapped n({wavelength_nm:g} nm)={float(evaluated['n']):.12g}; "
                f"fixed d={physical_nm:.12g} nm"
            ),
        })

    assumptions = [
        "正入射是本项目复算假设；来源端口与层折射率是名义值，不是所选材料库中的实测值。",
        f"n/k 来自明确选择的真实数据集，并在固定设计波长 {wavelength_nm:g} nm 求值。",
        "已知非零吸收 k 可保留在膜层与基片；当前求解器要求入射端口 k=0，未知端口 k 只能按明确无损假设处理。",
        "原配方物理厚度以 nm 保留；映射不会按新 n 重算四分之一波或半波厚度。",
        "本预览不计算映射后的反射率或透射率，也不保证映射性能。",
        "候选设计按单面正面涂层建立半无限基片模型，清空背面层并保留基片厚度。",
    ]
    if unknown_k:
        if not assume_lossless:
            # _evaluate_selected already enforces this, but keep the invariant explicit here.
            raise TemplateGenerationError("Unknown k requires the explicit assume-lossless option.")
        assumptions.append("用户已明确对未知 k 假设无损：" + "；".join(unknown_k))
    absorbing_records = []
    for row in mapped_layers:
        if row["k_at_wavelength"] is not None and abs(float(row["k_at_wavelength"])) > 1e-9:
            absorbing_records.append(f"{row['role']} k={float(row['k_at_wavelength']):.12g}")
    for label, evaluated in (("incident", incident), ("substrate", substrate)):
        if evaluated["k"] is None:
            if not assume_lossless:
                raise TemplateGenerationError("Unknown port k requires the explicit assume-lossless option.")
            assumptions.append(f"用户已明确对{label}端口的未知 k 假设无损：{evaluated['k_unknown_reason']}")
        elif abs(float(evaluated["k"])) > 1e-9:
            absorbing_records.append(f"{label} k={float(evaluated['k']):.12g}")
    if absorbing_records:
        assumptions.append(
            "所选数据集在目标波长含真实非零吸收 k；候选保留该 k，吸收不被无损勾选覆盖；"
            "映射性能需基于实际 n/k 重新求解，不能沿用来源名义结果（"
            + "；".join(absorbing_records) + "）。")

    candidate = deepcopy(dict(base_design))
    candidate["id"] = uuid.uuid4().hex
    candidate["name"] = f"参考配方映射 — {recipe['title']}"[:120]
    candidate["reference_wavelength_nm"] = wavelength_nm
    candidate["incident_medium"] = {
        "kind": "material", "material_id": incident["material_id"],
        "optical_set_id": incident["optical_set_id"],
    }
    candidate["substrate"]["material_id"] = substrate["material_id"]
    candidate["substrate"]["optical_set_id"] = substrate["optical_set_id"]
    candidate["substrate"]["sides"] = "single"
    candidate["first_surface"] = "front"
    candidate["front_layers"] = list(reversed(candidate_layers_incident_order))
    candidate["back_layers"] = []
    candidate["assume_lossless"] = assume_lossless
    candidate["use_k"] = True
    candidate["expression"] = {"text": "", "symbols": {}, "in_sync": True}
    note_lines = [
        "参考配方映射；来源名义值不是材料数据集，也不保证映射后的光学性能。",
        f"原配方：{recipe['stable_id']}；来源版本：{recipe['version']}；核验日期：{recipe['checked_date']}",
        f"来源：{recipe['source_url']}",
        f"固定设计波长：{wavelength_nm:g} nm；正入射为本项目复算假设。",
        (f"来源名义端口：{recipe['nominal_endpoints']['incident']['label']} "
         f"n={recipe['nominal_endpoints']['incident']['n']}, "
         f"k={recipe['nominal_endpoints']['incident']['k']}；"
         f"{recipe['nominal_endpoints']['substrate']['label']} "
         f"n={recipe['nominal_endpoints']['substrate']['n']}, "
         f"k={recipe['nominal_endpoints']['substrate']['k']}。"),
        (f"映射入射端：{incident['material_name']} [{incident['material_id']}]；"
         f"数据集 {incident['optical_set_name']} [{incident['optical_set_id']}]；"
         f"n={incident['n']:.12g}, k={incident['k']}（{incident['k_unknown_reason']}）。"),
        (f"映射基片：{substrate['material_name']} [{substrate['material_id']}]；"
         f"数据集 {substrate['optical_set_name']} [{substrate['optical_set_id']}]；"
         f"n={substrate['n']:.12g}, k={substrate['k']}（{substrate['k_unknown_reason']}）。"),
    ]
    for row in mapped_layers:
        note_lines.append(
            f"{row['sequence']}. {row['role']}：来源名义 {row['nominal_material']} "
            f"n={row['nominal_n']:.12g}, k={row['nominal_k']:.12g}；实际映射 "
            f"{row['material_name']} [{row['material_id']}]，数据集 "
            f"{row['optical_set_name']} [{row['optical_set_id']}]，"
            f"n={row['n_at_wavelength']:.12g}, k={row['k_at_wavelength']}；"
            f"原始固定厚度 d={row['fixed_thickness_nm']:.12g} nm。"
        )
    note_lines.append("映射假设：" + " ".join(assumptions))
    candidate["note"] = "\n".join(note_lines)
    try:
        validated = validate_design(candidate)
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise TemplateGenerationError(f"Mapped reference design failed schema validation: {exc}") from exc

    def _port_view(label: str, evaluated: Mapping[str, Any]) -> dict[str, Any]:
        nominal = recipe["nominal_endpoints"][label]
        delta_n = float(evaluated["n"]) - float(nominal["n"])
        delta_k = (None if evaluated["k"] is None else
                   float(evaluated["k"]) - float(nominal["k"]))
        return {
            "role": label,
            "nominal_n": float(nominal["n"]),
            "nominal_k": float(nominal["k"]),
            "material_id": evaluated["material_id"],
            "material_name": evaluated["material_name"],
            "optical_set_id": evaluated["optical_set_id"],
            "optical_set_name": evaluated["optical_set_name"],
            "n_at_wavelength": float(evaluated["n"]),
            "k_at_wavelength": evaluated["k"],
            "delta_n": delta_n,
            "delta_k": delta_k,
            "k_unknown_reason": evaluated["k_unknown_reason"],
            "source_band_nm": evaluated["source_band_nm"],
        }

    return {
        "recipe": _json_compatible(recipe),
        "recipe_id": recipe["stable_id"],
        "lambda0_nm": wavelength_nm,
        "nominal_endpoints": deepcopy(recipe["nominal_endpoints"]),
        "nominal_layers_incident_order": nominal_layers,
        "mapped_endpoints": {
            "incident": _port_view("incident", incident),
            "substrate": _port_view("substrate", substrate),
        },
        "mapped_layers_incident_order": mapped_layers,
        "layer_count": len(mapped_layers),
        "total_physical_thickness_nm": total_nm,
        "assumptions": assumptions,
        "limitations": recipe["limitations"] + "\n半无限吸收基片模型中的 T 表示穿过膜系进入基片界面的功率，不是整块基片的透射率；A 也不代表整片基片总吸收。",
        "principle": recipe["principle"],
        "source_url": recipe["source_url"],
        "source_reference": recipe["reference_section"],
        "candidate_design": validated,
    }
