#!/usr/bin/env python3
"""Summarize existing benchmark evidence without running an optimizer.

The supported paired CSV contract and conservative observation rules are
specified in docs/film-benchmark-diagnostics.md.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

VERSIONS = ("baseline", "candidate")
LABELS = ("budget_stop", "runtime_error", "constraint_violation", "missing_material", "unsupported_metric", "audit_incomplete")
BUDGET_REASONS = {"wall_budget", "evaluation_budget"}


def _value(value: Any) -> Any:
    return None if value is None or value == "" else value


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    return None


def _number(value: Any) -> int | float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return int(result) if result.is_integer() else result


def _json_field(value: str | None, *, field: str, sample_id: str) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{sample_id}: invalid JSON in {field}: {exc}") from exc


def _raw_deviation(detail: dict[str, Any]) -> float | None:
    actual = _number(detail.get("worst_actual"))
    if actual is None:
        return None
    mode = detail.get("mode")
    if mode == "range":
        lower = _number(detail.get("lower_bound"))
        upper = _number(detail.get("upper_bound"))
        if lower is None and upper is None:
            return None
        return max([0.0] + ([lower - actual] if lower is not None else []) + ([actual - upper] if upper is not None else []))
    if mode in {"value", "target", "equal", "equality"}:
        target = _number(detail.get("target_value"))
        return abs(actual - target) if target is not None else None
    return None


def _objectives(boundaries: Any) -> list[dict[str, Any]]:
    if not isinstance(boundaries, list):
        return []
    result = []
    for objective in boundaries:
        if not isinstance(objective, dict):
            continue
        for reduction in ("pointwise", "mean"):
            detail = objective.get(reduction)
            if not isinstance(detail, dict):
                continue
            result.append({
                "objective_index": objective.get("objective_index"),
                "metric": objective.get("metric"),
                "reduction": reduction,
                "wavelength_min_nm": objective.get("wavelength_min_nm"),
                "wavelength_max_nm": objective.get("wavelength_max_nm"),
                "discrete_line": objective.get("discrete_line"),
                "worst_actual": detail.get("worst_actual"),
                "target_value": detail.get("target_value"),
                "lower_bound": detail.get("lower_bound"),
                "upper_bound": detail.get("upper_bound"),
                "strict_lower": detail.get("strict_lower"),
                "strict_upper": detail.get("strict_upper"),
                "mode": detail.get("mode"),
                "targets_met": _bool(detail.get("targets_met")),
                "worst_angle_deg": detail.get("worst_angle_deg"),
                "worst_wavelength_nm": detail.get("worst_wavelength_nm"),
                "raw_violation_at_reported_worst": _raw_deviation(detail),
                "max_normalized_error_source": detail.get("max_normalized_error"),
                "source_detail": detail,
            })
    return result


def _reasons(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item.get("kind", "")) if isinstance(item, dict) else str(item) for item in value]


def analyze_row(row: dict[str, str], version: str, summary: dict[str, Any]) -> dict[str, Any]:
    """Build one version/case record using only observed report fields."""
    sample_id = row.get("sample_id", "")
    def get(name: str) -> Any:
        return _value(row.get(f"{version}_{name}"))
    boundaries = _json_field(get("numerical_boundaries_and_errors"), field=f"{version}_numerical_boundaries_and_errors", sample_id=sample_id)
    blocked = _json_field(get("blocked_reasons"), field=f"{version}_blocked_reasons", sample_id=sample_id)
    for field, value in (("numerical_boundaries_and_errors", boundaries), ("blocked_reasons", blocked)):
        if value is not None and not isinstance(value, list):
            raise ValueError(f"{sample_id}: {version}_{field} must be a JSON array or null")
    objectives = _objectives(boundaries)
    kinds = _reasons(blocked)
    status = get("status")
    stop = get("stop_reason")
    stop_signal = get("stop_signal_observed")
    audit = _bool(get("audit_complete"))
    labels = []
    if stop in BUDGET_REASONS or stop_signal in BUDGET_REASONS:
        labels.append("budget_stop")
    if (status in {"error", "failed", "exception", "runtime_error", "harness_error", "watchdog_timeout"}
            or _bool(get("watchdog_timeout")) is True or get("error")):
        labels.append("runtime_error")
    if any(o["targets_met"] is False for o in objectives):
        labels.append("constraint_violation")
    if any(k.startswith(("missing_source_k", "missing_source_n", "missing_material"))
           or k == "no_single_group_n_coverage" for k in kinds):
        labels.append("missing_material")
    if any("unsupported_metric" in k for k in kinds):
        labels.append("unsupported_metric")
    if audit is False:
        labels.append("audit_incomplete")
    provenance_key = f"{version}_{row.get('split', '')}"
    return {
        "sample_id": sample_id,
        "version": version,
        "split": _value(row.get("split")),
        "category": _value(row.get("category")),
        "design_family": _value(row.get("design_family")),
        "difficulty": _value(row.get("difficulty")),
        "data_status": _value(row.get("data_status")),
        "data_provenance": summary.get("input_provenance", {}).get(provenance_key),
        "source_version": summary.get("source_links", {}).get(version),
        "status": status,
        "nominal_pass": _bool(get("nominal_pass")),
        "budget_qualified_nominal_pass": _bool(get("budget_qualified_nominal_pass")),
        "stop_reason": stop,
        "stop_signal_observed": stop_signal,
        "elapsed_seconds": _number(get("elapsed_seconds")),
        "evaluations": _number(get("evaluations")),
        "evaluation_budget_overshoot": _number(get("evaluation_budget_overshoot")),
        "best_layers": _number(get("layers")),
        "best_total_thickness_nm": _number(get("total_thickness_nm")),
        "best_minimum_thickness_nm": _number(get("minimum_thickness_nm")),
        "validation_max_error_source": _number(get("validation_max_error")),
        "audit_complete": audit,
        "independent_audit_status": get("independent_audit_status"),
        "independent_nominal_pass": _bool(get("independent_nominal_pass")),
        "blocked_reasons": blocked,
        "error": get("error"),
        "watchdog_timeout": _bool(get("watchdog_timeout")),
        "objectives": objectives if boundaries is not None else None,
        "observed_labels": labels,
        "gradient_stagnation": "unknown",
        "structure_stagnation": "unknown",
        "seed": None,
        "best_design_layers": None,
        "multi_seed_evidence": None,
    }


def analyze(csv_path: Path, summary_path: Path | None = None) -> dict[str, Any]:
    summary = json.loads(summary_path.read_text(encoding="utf-8-sig")) if summary_path else {}
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"sample_id", "split", "category"} | {f"{version}_status" for version in VERSIONS}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"missing required CSV columns: {', '.join(sorted(missing))}")
        rows = list(reader)
    identities = [(row.get("sample_id"), row.get("split")) for row in rows]
    if any(not sample_id or not split for sample_id, split in identities):
        raise ValueError("each row requires a nonempty sample_id and split")
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate sample_id/split in CSV")
    cases = [analyze_row(row, version, summary) for row in rows for version in VERSIONS]
    groups: dict[tuple[Any, Any, Any], list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        groups[(case["version"], case["split"], case["category"])].append(case)
    statistics = []
    for (version, split, category), group in sorted(groups.items(), key=lambda pair: tuple(str(x) for x in pair[0])):
        counts = Counter(label for case in group for label in case["observed_labels"])
        statistics.append({
            "version": version, "split": split, "category": category,
            "case_count": len(group),
            "cases_with_objectives": sum(bool(case["objectives"]) for case in group),
            "label_counts": {label: counts[label] for label in LABELS},
        })
    # One case is counted only once per metric/reduction even with multiple objectives.
    objective_groups: dict[tuple[Any, Any, Any, Any], list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        types = {(objective["metric"], objective["reduction"])
                 for objective in case["objectives"] or [] if objective["metric"] is not None}
        for metric, reduction in types:
            objective_groups[(case["version"], case["split"], metric, reduction)].append(case)
    objective_statistics = []
    for (version, split, metric, reduction), group in sorted(
            objective_groups.items(), key=lambda pair: tuple(str(x) for x in pair[0])):
        counts = Counter(label for case in group for label in case["observed_labels"])
        objective_statistics.append({
            "version": version, "split": split, "metric": metric, "reduction": reduction,
            "case_count": len(group),
            "label_counts": {label: counts[label] for label in LABELS},
        })
    return {"schema_version": 1, "case_records": cases, "statistics": statistics,
            "objective_statistics": objective_statistics,
            "proposed_experiments": [],
            "limitations": ["Observed labels overlap; absence of a label is not proof of its negation.",
                            "Stagnation is unknown: no validated trajectory rule is applied.",
                            "Budget stops do not establish infeasibility, local optimality, or poor initialization.",
                            "Finite sampled optical results do not establish manufacturing qualification."]}


def enrich_result(output: dict[str, Any], version: str, result: dict[str, Any]) -> None:
    """Optionally attach a single observed result, matching by version/id/split."""
    matches = [c for c in output["case_records"] if c["version"] == version
               and c["sample_id"] == result.get("sample_id")
               and c["split"] == result.get("split")]
    if len(matches) != 1:
        raise ValueError("result must match exactly one case by version, sample_id and split")
    case = matches[0]
    if case.get("_result_attached"):
        raise ValueError("duplicate result for the same version/case/split")
    case["_result_attached"] = True
    budget = result.get("research_budget")
    if isinstance(budget, dict):
        case["seed"] = budget.get("seed")
    design = result.get("best_design")
    if isinstance(design, dict):
        # Preserve the source's side and layer order; no structure is inferred.
        case["best_design_layers"] = {
            side: design.get(side) for side in ("front_layers", "back_layers")
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", required=True, type=Path, help="paired case_results.csv")
    parser.add_argument("--summary", type=Path, help="optional report summary.json")
    parser.add_argument("--result", action="append", default=[], metavar="VERSION=PATH",
                        help="optional individual result.json; may be repeated")
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    output = analyze(args.cases, args.summary)
    for item in args.result:
        version, separator, path = item.partition("=")
        if not separator or version not in VERSIONS or not path:
            parser.error("--result must be baseline=PATH or candidate=PATH")
        result = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        enrich_result(output, version, result)
    for case in output["case_records"]:
        case.pop("_result_attached", None)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "case_details.json").write_text(json.dumps({"schema_version": output["schema_version"], "case_records": output["case_records"]}, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (args.output_dir / "summary.json").write_text(json.dumps({k: v for k, v in output.items() if k != "case_records"}, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
