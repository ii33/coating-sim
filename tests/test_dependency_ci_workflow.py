"""The lightweight Windows job must run pytest functions and unittest contracts."""
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/dependency-manager-build.yml"


def test_windows_source_installs_pinned_ci_runner_without_product_dependencies():
    source = WORKFLOW.read_text(encoding="utf-8")
    assert 'python -m pip install --disable-pip-version-check "pytest==9.1.1"' in source
    assert "CI test runner installation failed" in source
    assert "pip install -r requirements.txt" not in source
    assert "pip install -r requirements-test.txt" not in source


def test_windows_source_executes_every_dependency_module_with_pytest():
    source = WORKFLOW.read_text(encoding="utf-8")
    assert "Get-ChildItem -LiteralPath tests -Filter 'test_dependency*.py' -File" in source
    assert "$testFiles.Count -eq 0" in source
    assert "python -m pytest -v -ra --junitxml=artifacts/dependency-manager/windows-source.xml @testFiles" in source
    assert "Dependency manager pytest contracts failed" in source
    assert "python -m unittest discover" not in source
    assert "continue-on-error" not in source
    assert "--continue-on-collection-errors" not in source


def test_windows_source_retains_native_bootstrap_probes_and_failure_evidence():
    source = WORKFLOW.read_text(encoding="utf-8")
    assert "Parser]::ParseFile" in source
    assert "-Cli -CheckOnly -BootstrapPython $python" in source
    assert "dependency_manager.cmd -Cli -BootstrapPython $python doctor --json" in source
    assert "if: always()" in source
    assert "actions/upload-artifact@v4" in source
    assert "artifacts/dependency-manager/" in source
