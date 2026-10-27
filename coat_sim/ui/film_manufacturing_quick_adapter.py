"""Read-only manufacturing evidence projection; no optical evaluation or scoring."""
from __future__ import annotations

from collections.abc import Mapping
import math
from .film_display_format import format_film_thickness
from typing import Any

from coat_sim.core.film_objective_criteria import wavelength_measure


def _num(value: Any, *, percent: bool = False) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "未知"
    return f"{value * 100 if percent else value:.8g}" + ("%" if percent else "")


def _flag(value: Any) -> str:
    return "通过" if value is True else "未通过" if value is False else "未知/未通过"


def comparison_scope_text(settings: Mapping[str, Any] | None) -> str:
    """Describe the frozen simulation scope without inventing missing evidence."""
    if not isinstance(settings, Mapping):
        return "比较范围未记录"
    polarization = {"unpolarized": "非偏振", "s": "s 偏振", "p": "p 偏振"}.get(
        str(settings.get("polarization")), "偏振未知")
    surface = {"front": "前面入射", "back": "背面入射"}.get(
        str(settings.get("first_surface")), "入射面未知")
    policy = {"endpoint_hold": "材料范围外沿用端点值", "error": "材料范围外报错"}.get(
        str(settings.get("extrapolation_policy")), "材料外推策略未记录")
    return (f"{_num(settings.get('wavelength_min_nm'))}–{_num(settings.get('wavelength_max_nm'))} nm、"
            f"{settings.get('samples', '未知')} 点（{_num(settings.get('step_nm'))} nm 网格）、"
            f"{_num(settings.get('angle_deg'))}°{polarization}、{surface} R/T；{policy}")


def _layer_map(snapshot: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(snapshot, Mapping):
        return {}
    layers = snapshot.get("layers")
    if not isinstance(layers, list):
        layers = [dict(row, thickness_nm=(row.get("thickness", {}).get("value")
                  if row.get("thickness", {}).get("unit") == "nm" else None))
                  for row in snapshot.get("design", {}).get("front_layers", ())]
    return {str(row["id"]): dict(row, order=index + 1) for index, row in enumerate(layers)}


def layer_total_nm(snapshot: Mapping[str, Any] | None) -> float | None:
    """Sum one displayed candidate's physical layers; unknown QW is not zero."""
    if not isinstance(snapshot, Mapping):
        return None
    values: list[float] = []
    for row in _layer_map(snapshot).values():
        value = row.get("thickness_nm")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            return None
        values.append(float(value))
    return math.fsum(values)


def layer_change_rows(source: Mapping[str, Any] | None, current: Mapping[str, Any] | None,
                      champion: Mapping[str, Any] | None, *, bar_scale_nm: float = 0.0) -> list[dict[str, Any]]:
    """Join only stable IDs; lineage explains topology changes, never row positions."""
    original, working, best = _layer_map(source), _layer_map(current), _layer_map(champion)
    identifiers = dict.fromkeys((*original, *working, *best))

    def thickness(row):
        value = None if row is None else row.get("thickness_nm")
        return value if isinstance(value, (int, float)) and math.isfinite(value) else None

    def show(value):
        return "—" if value is None else format_film_thickness(value) + " nm"

    def origins(row):
        return [] if row is None else list(row.get("origins", ()))

    def relationship(identifier, row, rows):
        if row is not None:
            if thickness(row) == 0:
                return "厚度归零，待核验消层"
            ancestry = origins(row)
            if len(ancestry) > 1:
                names = [f"原第{original[key]['order']}层" if key in original else str(key)
                         for key in ancestry]
                return "合并自" + "、".join(names)
            if identifier not in original:
                return "新增" if not ancestry else "拆分/衍生自" + "、".join(
                    f"原第{original[key]['order']}层" if key in original else str(key) for key in ancestry)
            return "保留"
        descendants = [str(item["order"]) for item in rows.values() if identifier in origins(item)]
        return "合并/拆分至第" + "、".join(descendants) + "层" if descendants else "已删除"

    rows = []
    for identifier in identifiers:
        base, now, optimum = original.get(identifier), working.get(identifier), best.get(identifier)
        original_nm, current_nm, best_nm = thickness(base), thickness(now), thickness(optimum)
        metadata = now or optimum or base or {}
        current_deleted = current is not None and now is None
        best_deleted = champion is not None and optimum is None
        label = (f"原第{base['order']}层" if base is not None else "新增层")
        label += " · " + str(metadata.get("material_id", "材料未知"))

        def delta(candidate, value, source_nm=original_nm, layer_id=identifier):
            if value is None or source_nm is None:
                return "—"
            if origins(candidate) not in ([], [layer_id]):
                return "结构变化，见来源"
            difference = value - source_nm
            return ("+" if difference > 0 else "") + format_film_thickness(difference) + " nm"

        rows.append(dict(row_id=identifier, id=identifier, label=label, original_nm=original_nm,
            current_nm=current_nm, best_nm=best_nm, original=show(original_nm), current=show(current_nm),
            best=show(best_nm), current_delta=delta(now, current_nm), best_delta=delta(optimum, best_nm),
            current_deleted=current_deleted, best_deleted=best_deleted,
            current_ratio=(current_nm / bar_scale_nm if current_nm is not None and bar_scale_nm > 0 else 0.0),
            best_ratio=(best_nm / bar_scale_nm if best_nm is not None and bar_scale_nm > 0 else 0.0),
            current_origins=origins(now), best_origins=origins(optimum), origins=origins(now or optimum),
            status=("当前：" + relationship(identifier, now, working) if current is not None else "当前：尚无计算")
                + "；" + ("最佳：" + relationship(identifier, optimum, best) if champion is not None else "最佳：尚无可应用改进")))
    return rows


def score_text(score: Mapping[str, Any] | None, objectives: list[dict]) -> str:
    """Show every original band/angle. Never infer acceptance from a scalar."""
    if not isinstance(score, Mapping):
        return "未知/未通过：没有完整匹配证据。"
    lines = [f"nominal：{_flag(score.get('nominal_pass'))}；solver 分类：{_flag(score.get('solver_acceptable'))}",
             f"原始约束数：{score.get('condition_count', '未知')}；最大归一违反：{_num(score.get('max_error'))}"]
    errors = score.get("errors", ())
    for index, objective in enumerate(objectives):
        low, high = objective.get("wavelength_min_nm"), objective.get("wavelength_max_nm")
        lines.append(f"目标 {index + 1} · {objective.get('metric', '?')} · {_num(low)}–{_num(high)} nm")
        for angle in objective.get("angles_deg", ()):
            rows = [e for e in errors if isinstance(e, Mapping)
                    and e.get("objective_index") == index and e.get("angle_deg") == angle]
            parts = [f"  {_num(angle)}°"]
            # Diagnostic means use only the canonical fixed-grid coverage.
            # Audit extrema/offset points are used for the sampled range, never
            # mixed into the original mean or its nominal acceptance.
            dense = next((c for c in score.get("coverage", ()) if isinstance(c, Mapping)
                          and c.get("objective_index") == index and c.get("angle_deg") == angle), None)
            raw = next((c for c in score.get("audit_coverage", score.get("coverage", ()))
                        if isinstance(c, Mapping) and c.get("objective_index") == index
                        and c.get("angle_deg") == angle), None)
            if dense is not None:
                try:
                    if (isinstance(low, bool) or isinstance(high, bool)
                            or not isinstance(low, (int, float)) or not isinstance(high, (int, float))):
                        raise ValueError("band endpoints must be numeric")
                    weights = wavelength_measure(dense["wavelength_nm"], float(low), float(high))
                    values = dense["actual_values"]
                    if len(values) != len(weights):
                        raise ValueError("coverage cardinality mismatch")
                    mean = math.fsum(float(v) * float(w) for v, w in zip(values, weights, strict=True))
                    parts.append("诊断固定网格均值：" + _num(mean, percent=True))
                except (ValueError, TypeError, KeyError, OverflowError):
                    parts.append("诊断固定网格均值：未知（覆盖不完整）")
            else:
                parts.append("诊断固定网格均值：未知（覆盖不完整）")
            if raw is not None and raw.get("actual_values"):
                samples = list(zip(raw["wavelength_nm"], raw["actual_values"], strict=True))
                minimum, maximum = min(samples, key=lambda x: x[1]), max(samples, key=lambda x: x[1])
                parts.append(f"采样最小：{_num(minimum[1], percent=True)} @{_num(minimum[0])} nm")
                parts.append(f"采样最大：{_num(maximum[1], percent=True)} @{_num(maximum[0])} nm")
            for reduction, caption in (("mean", "波段均值"), ("pointwise", "最大违反点（并列取首点）")):
                if not objective.get(reduction, {}).get("enabled", False):
                    parts.append(caption + "约束：未启用")
                    continue
                values = [r for r in rows if r.get("reduction", "pointwise") == reduction]
                if not values:
                    parts.append(caption + "：未知/未通过，缺失证据")
                    continue
                worst = max(values, key=lambda r: r.get("normalized_violation", float("inf")))
                where = (f" @{_num(worst.get('wavelength_nm'))} nm" if reduction == "pointwise" else "")
                parts.append(f"{caption}：{_num(worst.get('actual'), percent=True)}{where}，"
                             f"归一违反 {_num(worst.get('normalized_violation'))}")
            lines.append("；".join(parts))
    audit = score.get("audit")
    if isinstance(audit, Mapping):
        lines.append("审计覆盖：" + "; ".join(f"{name}={audit.get(name, '未知')}" for name in
                     ("physical_evaluations", "dense_points", "offset_points", "extra_points", "completed_extrema")))
        lines.append(f"均值口径：{audit.get('mean_measure', '未知')}；连续证书：{audit.get('continuous_certificate', False)}")
    return "\n".join(lines)


def snapshot_text(snapshot: Mapping[str, Any] | None, objectives: list[dict]) -> str:
    if not isinstance(snapshot, Mapping):
        return "没有完整候选；训练及独立审计均为未知/未通过。"
    rows = snapshot.get("design", {}).get("front_layers", ())
    thickness = [row["thickness"]["value"] for row in rows
                 if row.get("thickness", {}).get("unit") == "nm"]
    geometry = (f"层数 {len(rows)}；最薄 {format_film_thickness(min(thickness)) if thickness else '无膜层'} nm；"
                f"D 总厚度 {format_film_thickness(math.fsum(thickness))} nm")
    training, audit = snapshot.get("training_score"), snapshot.get("audit_score")
    accepted = (isinstance(training, Mapping) and isinstance(audit, Mapping)
                and training.get("nominal_pass") is True and audit.get("nominal_pass") is True)
    classification = ("训练与独立审计均通过" if accepted else "原始目标未通过（完整训练与独立审计）"
                      if isinstance(training, Mapping) and isinstance(audit, Mapping) else "未知/未通过")
    return (geometry + "\n原始目标最终分类：" + classification
            + "\n原始约束训练\n" + score_text(training, objectives)
            + "\n独立审计\n" + score_text(audit, objectives))


def comparison_rows(payload: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    if payload.get('schema_version') in (2, 3):
        request, result = payload['request'], payload.get('result') or {}
        objectives = request['synthesis']['request']['objectives']
        present = request['original_targets_present']
        rows = []
        live = payload.get("schema_version") == 3
        for key, label in (('source','修复前原始膜系'),
                           ('best','最佳可应用改进（未必全部完成）' if live else '已通过联合验收的修复结果'),
                           ('reviewed','最近完整候选（请核对真实验收结果）')):
            snapshot = result.get(key)
            if not isinstance(snapshot, Mapping):
                if key in ('source', 'best'):
                    rows.append(dict(route='original' if key == 'source' else 'automatic', label=label, available=False,
                        details=('原曲线尚未完整计算，光学变化未知，不以 0 代替。' if key == 'source' else
                                 ('本轮尚无完整核验的严格改进，原设计未改变。' if live else
                                  '本轮尚无通过厚度、逐点光学变化及适用原目标联合验收的结果；不表示不存在可行修复。'))))
                continue
            guard = snapshot.get('optical_guard')
            if present:
                details = snapshot_text(snapshot, objectives)
            else:
                layers = snapshot['design']['front_layers']
                details = (f"层数 {len(layers)}；总厚 {format_film_thickness(sum(r['thickness']['value'] for r in layers))} nm。\n"
                    '没有启用的原光学目标，原目标验收不适用；本轮冻结比较范围为 '
                    + comparison_scope_text(request.get('comparison_settings')) + '。')
            if isinstance(guard, Mapping):
                if live:
                    assessment = snapshot.get("assessment") or {}
                    verdict = (f"可应用改进：{_flag(assessment.get('applicable_improvement'))}；"
                               f"全部条件满足：{_flag(assessment.get('all_requirements_met'))}。\n"
                               f"光学保护：{_flag(assessment.get('optical_protection_pass'))}；原目标名义通过："
                               + ("不适用" if not present else _flag(assessment.get("original_nominal_pass"))) + "。\n")
                else:
                    from coat_sim.core.film_automatic_repair import candidate_accepted
                    verdict = (f"联合验收：{_flag(candidate_accepted(snapshot, present))}；"
                               + ('须同时通过全部启用原目标的训练与独立审计。\n' if present else '原目标不适用。\n'))
                details = (verdict
                    + f"单层接受下限 {result['lower_limit_nm']:g} nm；保留层厚度下限复验：{_flag(snapshot.get('thickness_pass'))}。\n"
                    f"相对原曲线最大变化 {100*guard['maximum_absolute_change']:.8g} 个百分点 / "
                    f"允许 {request['optical_tolerance_percentage_points']:g} 个百分点：{_flag(guard['passed'])}。\n"
                    '逐点比较同一波长、角度、偏振和 R/T，不用均值抵消；采样及差分极值审计不等于连续谱证书。\n' + details)
            rows.append(dict(route='original' if key=='source' else 'automatic' if key=='best' else 'reviewed',
                label=label,available=True,details=details))
        return rows
    request = payload.get("request", {})
    objectives = request.get("synthesis", {}).get("request", {}).get("objectives", [])
    result = payload.get("result")
    if not isinstance(result, Mapping):
        reason = str(payload.get("termination_reason", "未知"))
        return [{"route": name, "label": label, "available": False,
                 "details": "终止原因：" + reason + "\n尚无完整训练与独立审计；未知/未通过。"}
                for name, label in (("original", "原始来源（不修改）"),
                                    ("raise_selected", "A · 保留增厚"),
                                    ("delete_selected_merge", "B · 删除合并"))]
    rows = [{"route": "original", "label": "原始来源（不修改）", "available": True,
             "details": snapshot_text(result.get("source"), objectives)}]
    route_map = {r["route"]: r for r in result.get("routes", ())}
    for name, label in (("raise_selected", "A · 保留增厚"), ("delete_selected_merge", "B · 删除合并")):
        route = route_map.get(name, {})
        champion = route.get("champion")
        budgets = "; ".join(f"{key}={route.get(key, '未知')}" for key in
                            ("start_calls", "refinement_calls", "audit_calls", "completed_training_calls",
                             "completed_audit_calls", "elapsed_seconds"))
        quotas = (f"每路配额：精修评价 {request.get('refinement_evaluations_per_route', '未知')}；"
                  f"迭代 {request.get('max_iterations_per_route', '未知')}；秒 {request.get('seconds_per_route', '未知')}")
        rows.append({"route": name, "label": label, "available": isinstance(champion, Mapping),
                     "details": f"终止/状态：{route.get('status', '未开始')}\n{quotas}\n预算账目：{budgets}\n"
                                + snapshot_text(champion or {"design": route.get("design", {})}, objectives)})
    return rows
