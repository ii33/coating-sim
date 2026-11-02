"""Contract tests use synthetic observations, not private benchmark cases."""
import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools import film_benchmark_diagnostics as diag


def row(**kwargs):
    base = {"sample_id": "synthetic", "split": "train", "category": "example",
            "baseline_status": "cancelled", "candidate_status": "blocked"}
    base.update(kwargs)
    return base


def boundary(actual=0.3, met=False):
    return [{"objective_index": 0, "metric": "R", "wavelength_min_nm": 400,
             "wavelength_max_nm": 600, "pointwise": {"mode": "range", "lower_bound": 0,
             "upper_bound": 0.2, "worst_actual": actual, "targets_met": met,
             "max_normalized_error": 0.1, "worst_angle_deg": 10, "worst_wavelength_nm": 500}}]


def write_csv(path, rows):
    fields = sorted(set().union(*(r.keys() for r in rows)))
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_observed_overlapping_labels_and_raw_deviation():
    data = row(baseline_stop_reason="wall_budget", baseline_audit_complete="False",
               baseline_numerical_boundaries_and_errors=json.dumps(boundary()),
               baseline_evaluations="512", baseline_elapsed_seconds="60.5",
               baseline_layers="2", baseline_total_thickness_nm="123.4")
    c = diag.analyze_row(data, "baseline", {"input_provenance": {"baseline_train": "data v1"}})
    assert c["observed_labels"] == ["budget_stop", "constraint_violation", "audit_incomplete"]
    assert c["objectives"][0]["raw_violation_at_reported_worst"] == pytest.approx(0.1)
    assert c["objectives"][0]["max_normalized_error_source"] == 0.1
    assert c["gradient_stagnation"] == c["structure_stagnation"] == "unknown"
    assert c["evaluations"] == 512 and c["elapsed_seconds"] == 60.5
    assert c["best_layers"] == 2 and c["best_total_thickness_nm"] == 123.4
    assert c["data_provenance"] == "data v1"


def test_missing_fields_unknown_not_failure_or_zero():
    c = diag.analyze_row(row(), "baseline", {})
    assert c["observed_labels"] == []
    for key in ("objectives", "audit_complete", "evaluations", "elapsed_seconds", "seed",
                "multi_seed_evidence", "best_layers", "data_provenance", "best_design_layers"):
        assert c[key] is None
    assert diag.analyze_row(row(baseline_numerical_boundaries_and_errors="[]"), "baseline", {})["objectives"] == []


def test_explicit_blocked_labels_and_runtime_error():
    b = [{"kind": "missing_source_k_in_include_k"}, {"kind": "core_unsupported_metric_A"},
         {"kind": "contradictory_substrate_description"}]
    c = diag.analyze_row(row(baseline_status="error", baseline_blocked_reasons=json.dumps(b)), "baseline", {})
    assert c["observed_labels"] == ["runtime_error", "missing_material", "unsupported_metric"]
    assert len(c["blocked_reasons"]) == 3


def test_max_error_not_reinterpreted_and_boundary_modes():
    assert diag._raw_deviation({"mode": "range", "worst_actual": 0.2, "upper_bound": 0.2}) == 0
    assert diag._raw_deviation({"mode": "target", "worst_actual": 4, "target_value": 3}) == 1
    assert diag._raw_deviation({"mode": "range", "worst_actual": 0.2}) is None
    assert diag._raw_deviation({"mode": "unknown", "worst_actual": 0.2, "upper_bound": 0.1}) is None
    c = diag.analyze_row(row(baseline_validation_max_error="0.12"), "baseline", {})
    assert c["validation_max_error_source"] == 0.12 and not c["observed_labels"]


def test_invalid_json_and_missing_required_columns(tmp_path):
    with pytest.raises(ValueError, match="invalid JSON"):
        diag.analyze_row(row(baseline_blocked_reasons="["), "baseline", {})
    with pytest.raises(ValueError, match="must be a JSON array"):
        diag.analyze_row(row(baseline_blocked_reasons='{"kind": "unexpected"}'), "baseline", {})
    p = tmp_path / "case.csv"
    p.write_text("sample_id\none\n")
    with pytest.raises(ValueError, match="missing required CSV columns"):
        diag.analyze(p)


def test_statistics_denominators_and_no_test_leakage(tmp_path):
    p = tmp_path / "case.csv"
    write_csv(p, [row(baseline_stop_reason="wall_budget"),
                  row(sample_id="second", baseline_numerical_boundaries_and_errors=json.dumps(boundary()))])
    output = diag.analyze(p)
    baseline = next(g for g in output["statistics"] if g["version"] == "baseline")
    assert baseline["case_count"] == 2 and baseline["cases_with_objectives"] == 1
    assert baseline["label_counts"]["budget_stop"] == 1
    assert baseline["label_counts"]["constraint_violation"] == 1
    metric = next(g for g in output["objective_statistics"] if g["version"] == "baseline")
    assert metric["metric"] == "R" and metric["reduction"] == "pointwise"
    assert metric["case_count"] == 1 and metric["label_counts"]["constraint_violation"] == 1
    assert output["proposed_experiments"] == []
    write_csv(p, [row(), row()])
    with pytest.raises(ValueError, match="duplicate"):
        diag.analyze(p)


def test_optional_individual_result_exact_match_and_duplicate(tmp_path):
    p = tmp_path / "case.csv"
    write_csv(p, [row()])
    output = diag.analyze(p)
    result = {"sample_id": "synthetic", "split": "train", "research_budget": {"seed": 0},
              "best_design": {"front_layers": [{"thickness": {"value": 5, "unit": "nm"}}], "back_layers": []},
              "core_result": {"trace": [{"gradient": 0}]}}
    diag.enrich_result(output, "baseline", result)
    case = output["case_records"][0]
    assert case["seed"] == 0 and case["best_design_layers"]["front_layers"][0]["thickness"]["value"] == 5
    assert case["gradient_stagnation"] == "unknown" and case["multi_seed_evidence"] is None
    with pytest.raises(ValueError, match="duplicate"):
        diag.enrich_result(output, "baseline", result)
    with pytest.raises(ValueError, match="exactly one"):
        diag.enrich_result(output, "candidate", dict(result, split="test"))


def test_cli_writes_json_and_preserves_missing_values(tmp_path):
    p = tmp_path / "case.csv"
    write_csv(p, [row()])
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"sample_id": "synthetic", "split": "train", "research_budget": {"seed": 0}}))
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(Path(diag.__file__)), "--cases", str(p),
                    "--result", f"baseline={result}", "--output-dir", str(out)], check=True)
    details = json.loads((out / "case_details.json").read_text())
    assert details["case_records"][0]["seed"] == 0
    assert details["case_records"][0]["objectives"] is None
    assert "_result_attached" not in details["case_records"][0]
    assert (out / "summary.json").exists()


@pytest.mark.parametrize("status", ["runtime_error", "harness_error", "watchdog_timeout"])
def test_harness_failure_status_without_error_text_is_observed(status):
    case = diag.analyze_row(row(baseline_status=status), "baseline", {})
    assert "runtime_error" in case["observed_labels"]
    assert case["error"] is None


def test_watchdog_boolean_is_explicit_execution_failure():
    case = diag.analyze_row(row(baseline_watchdog_timeout="True"), "baseline", {})
    assert "runtime_error" in case["observed_labels"]
    assert case["watchdog_timeout"] is True


def test_actual_value_criterion_raw_deviation():
    bounds = boundary()
    bounds[0]["pointwise"].update(mode="value", target_value=0.15, worst_actual=0.3)
    case = diag.analyze_row(row(baseline_numerical_boundaries_and_errors=json.dumps(bounds)), "baseline", {})
    assert case["objectives"][0]["raw_violation_at_reported_worst"] == pytest.approx(0.15)
    bounds[0]["pointwise"]["target_value"] = None
    case = diag.analyze_row(row(baseline_numerical_boundaries_and_errors=json.dumps(bounds)), "baseline", {})
    assert case["objectives"][0]["raw_violation_at_reported_worst"] is None
