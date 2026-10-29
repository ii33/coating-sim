"""Package imports must work without another test's sys.path side effects."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.qt, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[1]


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in ("PYTHONPATH", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "COATING_PYTEST_MODULE_WORKER"):
        environment.pop(name, None)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["QT_QUICK_BACKEND"] = "software"
    environment["QSG_RHI_BACKEND"] = "software"
    return environment


@pytest.mark.parametrize("module", [
    "test_film_quick_controller.py", "test_mask_viewport.py",
    "test_film_structure_request_review.py", "test_film_structure_payload_review.py",
])
@pytest.mark.parametrize("import_mode", ["prepend", "importlib"])
def test_modules_collect_alone_without_pythonpath(module, import_mode):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         f"--import-mode={import_mode}", str(ROOT / "tests" / module)],
        cwd=ROOT, env=_environment(), capture_output=True, text=True, check=False, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tests collected" in result.stdout


def test_official_runner_from_external_cwd_executes_worker_without_pythonpath(tmp_path):
    report = tmp_path / "junit.xml"
    external = tmp_path / "unrelated-project"
    external.mkdir()
    external_ini = external / "pytest.ini"
    external_ini.write_text("[pytest]\npython_files = never_test_*.py\n", encoding="utf-8")
    collection = external / "collection.json"
    collection.write_text("{}\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/run_tests.py"), "qt",
         "--collection-report", str(collection), "--", "-k",
         "test_new_design_has_default_h_l_symbols_without_layers_or_expression",
         f"--junitxml={report}"],
        cwd=tmp_path, env=_environment(), capture_output=True, text=True, check=False, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    audit = json.loads((tmp_path / "module-processes.json").read_text(encoding="utf-8"))
    expected = ["tests/test_film_quick_controller.py::FilmDesignQuickControllerTests::"
                "test_new_design_has_default_h_l_symbols_without_layers_or_expression"]
    assert audit["selected_nodeids"] == expected
    assert len(audit["batches"]) == 1
    assert audit["batches"][0]["started_nodeids"] == expected
    assert audit["batches"][0]["exit_code"] == 0
    assert json.loads(collection.read_text(encoding="utf-8"))["collection_error_count"] == 0
    assert external_ini.read_text(encoding="utf-8") == "[pytest]\npython_files = never_test_*.py\n"


def test_test_modules_use_canonical_package_names_for_other_test_helpers():
    tests = ROOT / "tests"
    names = {path.stem for path in tests.glob("*.py")}
    violations = []
    for path in sorted(tests.glob("test_*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            modules = []
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            for module in modules:
                # qt_test_guard has an explicit, independently tested same-
                # module alias installed by conftest before Qt imports. Other
                # helpers have no top-level import contract.
                if module.split(".")[0] in names and module != "qt_test_guard":
                    violations.append(f"{path.name}:{node.lineno}: {module}")
    assert violations == []
