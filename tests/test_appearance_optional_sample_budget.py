"""An explicit aggregate budget override preserves all physical validation."""
import ast
import json
from pathlib import Path

import pytest

from coat_sim.appearance.contract import AppearanceInputError, validate_job
from tests.test_appearance_result_contract import _job
from tools.validation.gpu_appearance_preview import render_preview

pytestmark = pytest.mark.contract


def large_job():
    job = _job()
    job["resolution"].update(width=512, height=384, spp=64)
    return job


def test_default_rejects_aggregate_budget_and_none_only_removes_it():
    job = large_job()
    with pytest.raises(AppearanceInputError, match="20 million render budget"):
        validate_job(job)
    normalized = validate_job(job, max_wavelength_samples=None)
    assert normalized["resolution"] == job["resolution"]
    assert normalized["wavelength_nm"].tolist() == job["wavelength_nm"]


@pytest.mark.parametrize("invalid", [True, 0, -1, 20_000_000., "unlimited"])
def test_optional_budget_configuration_has_exact_integer_or_none_type(invalid):
    with pytest.raises(AppearanceInputError, match="positive integer or None"):
        validate_job(_job(), max_wavelength_samples=invalid)


def test_custom_finite_budget_is_respected():
    with pytest.raises(AppearanceInputError, match="7 render budget"):
        validate_job(_job(), max_wavelength_samples=7)
    validate_job(_job(), max_wavelength_samples=8)


@pytest.mark.parametrize("mutation", ["width_float", "spp_bool", "absorbing_substrate", "nan_camera", "pixels", "spp"])
def test_none_budget_does_not_bypass_other_validation(mutation):
    job = large_job()
    if mutation == "width_float":
        job["resolution"]["width"] = 512.
    elif mutation == "spp_bool":
        job["resolution"]["spp"] = True
    elif mutation == "absorbing_substrate":
        job["substrate"]["material"]["k"] = .1
    elif mutation == "nan_camera":
        job["camera"]["position_mm"][0] = float("nan")
    elif mutation == "pixels":
        job["resolution"].update(width=1025, height=1024)
    elif mutation == "spp":
        job["resolution"]["spp"] = 4097
    with pytest.raises(AppearanceInputError):
        validate_job(job, max_wavelength_samples=None)


def test_gpu_explicitly_disables_only_aggregate_budget_and_reports_it(tmp_path, monkeypatch):
    tree = ast.parse(Path("coat_sim/appearance/gpu.py").read_text())
    render = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "render_preview")
    calls = [node for node in ast.walk(render) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "validate_job"]
    assert len(calls) == 1
    keyword = next(k for k in calls[0].keywords if k.arg == "max_wavelength_samples")
    assert isinstance(keyword.value, ast.Constant) and keyword.value.value is None
    def unavailable():
        raise ImportError("deliberately absent GPU runtime after input validation")
    monkeypatch.setattr("tools.validation.gpu_appearance_preview._load_runtime", unavailable)
    output = tmp_path / "large-gpu"
    with pytest.raises(ImportError, match="deliberately"):
        render_preview(large_job(), output)
    manifest = json.loads((output / "rejected-manifest.json").read_text())
    assert manifest["estimated_wavelength_samples"] == 512 * 384 * 64 * 2
    assert manifest["max_wavelength_samples"] is None
    assert manifest["legacy_max_wavelength_samples"] == 20_000_000
    assert len(manifest["execution_budget_warnings"]) == 1
    assert manifest["execution_budget_warnings"][0] in manifest["input_warnings"]
