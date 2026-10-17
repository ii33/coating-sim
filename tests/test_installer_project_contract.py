"""Project-side contract retained for the separately published alpha.1 installer."""
import ast
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def test_published_installer_consumption_files_remain_application_owned():
    for name in (
        "requirements.txt", "requirements-test.txt", "requirements-analysis.txt",
        "requirements-analysis-lock.txt", "requirements-sandbox.txt",
        "requirements-remote-service.txt", "environment.yml", "launcher.py",
        "coat_sim/version.txt", "coat_sim/ui/assets/icons/launcher.ico", "tools/run_tests.py",
        "coat_sim/core/compute_device_probe.py", "coat_sim/core/project_dependencies.py",
    ):
        assert (ROOT / name).is_file(), name
    version = (ROOT / "coat_sim/version.txt").read_bytes()
    assert len(version) <= 128
    assert re.fullmatch(r"dev\.\d{8}\.\d+\s*", version.decode("utf-8"))
    assert (ROOT / "coat_sim/ui/assets/icons/launcher.ico").read_bytes().startswith(b"\0\0\1\0")


def test_alpha1_requirement_inputs_keep_the_simple_declaration_contract():
    for name in ("requirements.txt", "requirements-test.txt", "requirements-sandbox.txt"):
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
            value = line.split("#", 1)[0].strip()
            if not value:
                continue
            assert not any(char in value for char in (";", "@", ":", " "))
            assert not value.startswith("-"), (name, value)
    appearance = (ROOT / "requirements-test.txt").read_text(encoding="utf-8").lower()
    assert "mitsuba==" in appearance and "drjit==" in appearance
    tooling = (ROOT / "requirements-analysis.txt").read_text(encoding="utf-8").lower()
    assert "ruff==" in tooling and "mypy==" in tooling


def test_application_has_no_import_dependency_on_installer_implementation():
    forbidden = ("tools.dependency_manager", "dependency_manager", "tools.build_offline_installer")
    for directory in ("coat_sim", "scripts", "tools"):
        for path in (ROOT / directory).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else (
                    [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                assert not any(name == prefix or name.startswith(prefix + ".")
                               for name in names for prefix in forbidden), path


def test_installer_copy_is_absent_and_remote_service_deployment_is_retained():
    for name in ("tools/dependency_manager", "dependency_manager.py", "dependency_manager.cmd",
                 "install.bat", "install.sh", "tools/build_offline_installer.py",
                 "tools/offline_installer_bootstrap.cs", "requirements-dependency-manager-build.txt",
                 ".github/workflows/dependency-manager-build.yml"):
        assert not (ROOT / name).exists(), name
    assert any((ROOT / "scripts").glob("*remote*")) or any((ROOT / "tools").glob("*remote*"))
