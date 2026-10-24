"""Managed process routing contracts using inert local runtime fixtures."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from coat_sim import compute_runtime_manifest as runtime
from coat_sim.compute_runtime_dispatch import installed_role_python
from coat_sim.scheduler import daemon, remote_appearance
from tests.test_compute_runtime_manifest import RuntimeFixture


@pytest.fixture
def installed(tmp_path, monkeypatch):
    fixture = RuntimeFixture(tmp_path)
    monkeypatch.setattr(daemon, "__file__", str(tmp_path / "coat_sim/scheduler/daemon.py"))
    monkeypatch.setattr(remote_appearance, "__file__", str(tmp_path / "coat_sim/scheduler/remote_appearance.py"))
    try:
        yield fixture
    finally:
        fixture.close()


def test_only_genuinely_missing_manifest_allows_legacy_route(tmp_path):
    assert installed_role_python(tmp_path, "appearance") is None
    directory = tmp_path / ".runtime"
    directory.mkdir()
    (directory / "compute-runtimes.json").write_text('{"schema_version":99}')
    with pytest.raises(runtime.RuntimeManifestError):
        installed_role_python(tmp_path, "appearance")


def test_active_or_stale_install_lock_never_becomes_legacy_fallback(tmp_path):
    (tmp_path / ".runtime").mkdir()
    (tmp_path / ".runtime/compute-runtime-install.lock").write_text("stale operation")
    with pytest.raises(runtime.RuntimeManifestError, match="fresh runtime-roles-plan"):
        installed_role_python(tmp_path, "appearance")


@pytest.mark.parametrize("backend,environment", [
    ("GPU", "deposition_cuda"), ("DirectML", "deposition_directml"),
    ("CPU", "app"), ("自动", "app"),
])
@pytest.mark.parametrize("executor", ["process", "process_recipe", "single_source", "mask_optimization"])
def test_deposition_explicit_roles_keep_requested_backend(installed, backend, environment, executor):
    parameters = {"backend": backend}
    if executor == "mask_optimization":
        parameters = {"options": parameters}
    spec = SimpleNamespace(executor=executor, parameters=parameters)
    with patch.object(daemon.subprocess, "run", side_effect=AssertionError("No hardware/readiness probe")):
        interpreter, environment_vars = daemon._worker_runtime_route(spec, installed.root)
    assert interpreter is not None and environment_vars is not None
    assert interpreter == str(installed.root / installed.data["environments"][environment]["python"])
    assert environment_vars["PYTHONPATH"] == str(installed.root)
    assert spec.parameters == parameters


@pytest.mark.parametrize("backend", ["cpu", "gpu_cuda"])
def test_appearance_uses_same_private_role_without_claiming_gpu_readiness(installed, backend):
    spec = SimpleNamespace(executor="appearance_render", parameters={"frozen": {"request": {
        "backend": backend, "precision": "native" if backend == "cpu" else "mixed"}}})
    interpreter, _environment = daemon._worker_runtime_route(spec, installed.root)
    expected = str(installed.root / installed.data["environments"]["appearance"]["python"])
    assert interpreter == expected
    assert remote_appearance.appearance_runtime_interpreter(installed.root, backend) == expected


def test_missing_requested_role_does_not_fall_back_to_external_python(installed):
    installed.data["environments"].pop("deposition_directml")
    installed.data["roles"].pop("deposition_directml")
    installed.write()
    spec = SimpleNamespace(executor="process", parameters={"backend": "DirectML"})
    with pytest.raises(runtime.RuntimeBindingMissing):
        daemon._worker_runtime_route(spec, installed.root)


def test_invalid_optional_env_cannot_be_bypassed_by_cpu_or_other_role(installed):
    (installed.root / ".runtime/envs/appearance/Scripts/python.exe").write_bytes(b"changed")
    spec = SimpleNamespace(executor="process", parameters={"backend": "CPU"})
    with pytest.raises(runtime.RuntimeManifestError):
        daemon._worker_runtime_route(spec, installed.root)


@pytest.mark.parametrize("executor", ["film_optimization", "film_synthesis", "film_ideal_sensitivity",
                                    "film_thickness_error_table", "geometric_raytrace", "probe", "python_script"])
def test_film_and_unrelated_jobs_have_no_new_gpu_dispatch(installed, executor):
    spec = SimpleNamespace(executor=executor, parameters={"backend": "GPU"})
    with patch("coat_sim.compute_runtime_dispatch.installed_role_python", side_effect=AssertionError("No role lookup")):
        assert daemon._worker_runtime_route(spec, installed.root) == (None, None)


def test_source_installation_root_wins_over_editable_project_data(installed, tmp_path):
    data_root = tmp_path / "user-project"
    data_root.mkdir()
    (data_root / ".runtime").mkdir()
    (data_root / runtime.MANIFEST_PATH).write_text("malformed user data")
    expected = str(installed.root / installed.data["environments"]["appearance"]["python"])
    assert remote_appearance.appearance_runtime_interpreter(data_root, "cpu") == expected


def test_managed_worker_environment_excludes_python_and_qt_injection(installed, monkeypatch):
    monkeypatch.setenv("PYTHONHOME", "/external/python")
    monkeypatch.setenv("PYTHONPATH", "/external/site-packages")
    monkeypatch.setenv("VIRTUAL_ENV", "/external/venv")
    monkeypatch.setenv("QT_PLUGIN_PATH", "/external/qt")
    spec = SimpleNamespace(executor="process", parameters={"backend": "DirectML"})
    interpreter, environment = daemon._worker_runtime_route(spec, installed.root)
    assert interpreter is not None and environment is not None
    assert "PYTHONHOME" not in environment and "VIRTUAL_ENV" not in environment
    assert "QT_PLUGIN_PATH" not in environment
    assert environment["PYTHONPATH"] == str(installed.root)
    assert environment["PATH"].split(__import__("os").pathsep)[0] == str(Path(interpreter).parent)
