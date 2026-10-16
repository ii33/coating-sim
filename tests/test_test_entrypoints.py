"""Safety checks for the shared pytest entrypoint and collection guard."""

from __future__ import annotations

import errno
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from _pytest.outcomes import Skipped

from tests import conftest as test_config


class _FakeConfig:
    def __init__(self, **options: bool) -> None:
        self.options = options

    def getoption(self, name: str) -> bool:
        return self.options.get(name, False)


class _FakeItem:
    def __init__(self, markers: set[str], **options: bool) -> None:
        self.config = _FakeConfig(**options)
        self.markers = markers

    def get_closest_marker(self, name: str):
        return object() if name in self.markers else None


def test_spectrum_controller_and_ui_modules_have_explicit_qt_tier() -> None:
    for module in ("test_film_spectrum_plot_bounds.py",
                   "test_film_simulation_quick_ui_refinements.py"):
        assert module in test_config.QT_MODULES
        assert module in test_config.TIER_MANIFEST["qt"]
        assert module not in test_config.TIER_MANIFEST["quick"]


def test_font_size_defaults_module_has_explicit_qt_tier() -> None:
    module = "test_font_size_defaults.py"
    assert module in test_config.QT_MODULES
    assert module in test_config.TIER_MANIFEST["qt"]
    assert module not in test_config.TIER_MANIFEST["quick"]


def test_python_network_guard_blocks_external_dns_tcp_and_udp() -> None:
    test_config._install_default_network_guard()

    with pytest.raises(OSError, match="blocked name lookup"):
        socket.getaddrinfo("example.invalid", 443)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        with pytest.raises(OSError, match="blocked connection"):
            stream.connect(("203.0.113.10", 443))

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as datagram:
        with pytest.raises(OSError, match="blocked datagram"):
            datagram.sendto(b"blocked", ("203.0.113.10", 53))


def test_network_guard_allows_loopback_tcp() -> None:
    test_config._install_default_network_guard()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        with socket.create_connection(listener.getsockname(), timeout=1) as client:
            accepted, _ = listener.accept()
            accepted.close()
            assert client.getpeername()[0] == "127.0.0.1"


@pytest.mark.parametrize("method_name", ("connect", "connect_ex", "create_connection"))
def test_network_guard_without_af_unix_preserves_local_tcp_and_blocks_external(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    test_config._install_default_network_guard()
    monkeypatch.delattr(socket, "AF_UNIX", raising=False)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        address = listener.getsockname()
        if method_name == "create_connection":
            with socket.create_connection(address, timeout=1):
                accepted, _ = listener.accept()
                accepted.close()
        else:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
                if method_name == "connect":
                    client.connect(address)
                else:
                    assert client.connect_ex(address) == 0
                accepted, _ = listener.accept()
                accepted.close()

    if method_name == "create_connection":
        with pytest.raises(OSError, match="blocked connection"):
            socket.create_connection(("203.0.113.10", 443), timeout=1)
    else:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            with pytest.raises(OSError, match="blocked connection"):
                getattr(client, method_name)(("203.0.113.10", 443))


def test_network_guard_allows_unix_socket(tmp_path) -> None:
    test_config._install_default_network_guard()
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("Unix-domain sockets are not available on this platform")
    endpoint = str(tmp_path / "local.sock")
    try:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except PermissionError as exc:
        if exc.errno == errno.EPERM:
            pytest.skip("sandbox denies AF_UNIX socket construction (EPERM)")
        raise
    with listener:
        listener.bind(endpoint)
        listener.listen(1)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(endpoint)
            accepted, _ = listener.accept()
            accepted.close()


def test_provider_and_remote_flags_do_not_disable_guard_during_collection() -> None:
    config = _FakeConfig(**{
        "--run-provider": True,
        "--run-remote": True,
        "--run-gpu": True,
    })
    test_config.pytest_configure(config)

    assert socket._deposition_test_guard is True
    with pytest.raises(OSError, match="blocked name lookup"):
        socket.getaddrinfo("example.invalid", 443)


def test_pytest_configure_uses_private_cache_roots(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path / "inherited-matplotlib"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "inherited-xdg"))
    test_config.pytest_configure(_FakeConfig())

    assert Path(os.environ["MPLCONFIGDIR"]) == test_config._TEST_CACHE_ROOT / "matplotlib"
    assert Path(os.environ["XDG_CACHE_HOME"]) == test_config._TEST_CACHE_ROOT / "xdg"
    assert Path(os.environ["MPLCONFIGDIR"]).is_dir()
    assert Path(os.environ["XDG_CACHE_HOME"]).is_dir()


def test_pytest_configure_retains_the_widget_capable_qt_singleton() -> None:
    from PySide6.QtWidgets import QApplication

    test_config.pytest_configure(_FakeConfig())
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    assert test_config._TEST_QT_APPLICATION is application
    test_config._ensure_test_qt_application()
    assert test_config._TEST_QT_APPLICATION is application


def test_qt_application_setup_rejects_an_existing_weaker_singleton(monkeypatch) -> None:
    from PySide6.QtCore import QObject
    from PySide6.QtWidgets import QApplication

    weaker_application = QObject()
    monkeypatch.setattr(QApplication, "instance", lambda: weaker_application)
    with pytest.raises(pytest.UsageError, match="cannot be safely upgraded"):
        test_config._ensure_test_qt_application()


def test_qt_application_setup_allows_missing_pyside_for_core_only_selection() -> None:
    script = '''
import builtins
original_import = builtins.__import__
def without_pyside(name, *args, **kwargs):
    if name.startswith("PySide6"):
        raise ModuleNotFoundError("No module named 'PySide6'", name="PySide6")
    return original_import(name, *args, **kwargs)
builtins.__import__ = without_pyside
from tests import conftest
conftest._ensure_test_qt_application()
assert conftest._TEST_QT_APPLICATION is None
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, check=False, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_full_tests_collection_fails_for_uncollected_module_and_writes_report(tmp_path) -> None:
    root = Path(__file__).resolve().parents[1]
    empty_module = root / "tests" / f"test__empty_collection_gate_{uuid4().hex}.py"
    report_path = tmp_path / "collection.json"
    empty_module.write_text('"""Temporary module with no test cases."""\n', encoding="utf-8")
    env = os.environ.copy()
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    try:
        targeted = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", str(empty_module)],
            cwd=root, env=env, text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False, timeout=60,
        )
        assert targeted.returncode == 5
        assert "test modules contain no collected items" not in targeted.stdout

        complete_without_report = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests"],
            cwd=root, env=env, text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False, timeout=60,
        )
        assert complete_without_report.returncode == 4
        assert "test modules contain no collected items" in complete_without_report.stdout

        complete = subprocess.run(
            [
                sys.executable, "-m", "pytest", "--collect-only", "-q", "tests",
                "--collection-report-json", str(report_path),
            ],
            cwd=root, env=env, text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False, timeout=60,
        )
        assert complete.returncode == 4, complete.stdout[-4000:]
        assert "test modules contain no collected items" in complete.stdout
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["collection_error_count"] == 0
        assert empty_module.name in report["modules_without_collected_items"]
    finally:
        empty_module.unlink(missing_ok=True)


@pytest.mark.parametrize(
    ("markers", "options", "allowed"),
    [
        ({"live_provider"}, {"--run-provider": True}, True),
        ({"live_remote"}, {"--run-remote": True}, True),
        ({"live_provider"}, {"--run-remote": True}, False),
        ({"live_remote"}, {"--run-provider": True}, False),
        ({"live_provider"}, {"--run-gpu": True}, False),
        ({"real_gpu"}, {"--run-gpu": True}, False),
    ],
)
def test_only_matching_live_network_opt_in_opens_socket_for_test_body(
    markers: set[str],
    options: dict[str, bool],
    allowed: bool,
) -> None:
    test_config._install_default_network_guard()
    item = _FakeItem(markers, **options)
    hook = test_config.pytest_runtest_call(item)
    next(hook)
    try:
        assert socket._deposition_test_guard is (not allowed)
    finally:
        hook.close()
    assert socket._deposition_test_guard is True


@pytest.mark.parametrize(
    ("markers", "options"),
    [
        ({"live_provider"}, {}),
        ({"live_provider"}, {"--run-remote": True}),
        ({"live_remote"}, {"--run-provider": True}),
        ({"real_gpu"}, {}),
    ],
)
def test_live_and_gpu_markers_require_their_matching_opt_in(
    markers: set[str],
    options: dict[str, bool],
) -> None:
    item = _FakeItem(markers, **options)
    with pytest.raises(Skipped, match="requires explicit opt-in"):
        test_config.pytest_runtest_setup(item)


def test_matching_gpu_flag_allows_collection_setup_without_opening_network() -> None:
    test_config._install_default_network_guard()
    item = _FakeItem({"real_gpu"}, **{"--run-gpu": True})
    test_config.pytest_runtest_setup(item)
    assert socket._deposition_test_guard is True
    with pytest.raises(OSError, match="blocked name lookup"):
        socket.getaddrinfo("example.invalid", 443)


def test_runner_forwards_pytest_arguments_only_after_separator(monkeypatch) -> None:
    from types import SimpleNamespace
    import tools.run_tests as runner

    monkeypatch.setenv("MPLCONFIGDIR", "/inherited/user/matplotlib")
    monkeypatch.setenv("XDG_CACHE_HOME", "/inherited/user/xdg")
    captured = {}

    def fake_run(command, *, cwd, env, check):
        captured.update(command=command, cwd=cwd, env=env, check=check)
        assert Path(env["MPLCONFIGDIR"]).is_dir()
        assert Path(env["XDG_CACHE_HOME"]).is_dir()
        return SimpleNamespace(returncode=17)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    code = runner.main([
        "quick", "--collection-report", "artifacts/collection.json", "--",
        "-k", "focused_case", "--junitxml=artifacts/junit.xml",
    ])

    command = captured["command"]
    assert code == 17
    assert command[:4] == [sys.executable, "-m", "pytest", "-m"]
    assert command[4] == "quick"
    assert captured["env"]["MPLCONFIGDIR"] != "/inherited/user/matplotlib"
    assert captured["env"]["XDG_CACHE_HOME"] != "/inherited/user/xdg"
    assert "tests" in command
    assert command[-3:] == ["-k", "focused_case", "--junitxml=artifacts/junit.xml"]
    assert Path(command[command.index("--collection-report-json") + 1]) == Path("artifacts/collection.json")


def test_runner_rejects_unknown_arguments_before_separator() -> None:
    import tools.run_tests as runner

    with pytest.raises(SystemExit) as caught:
        runner.main(["quick", "--unknown-runner-option"])
    assert caught.value.code == 2


def test_runner_maps_signal_termination_to_shell_status(monkeypatch, capsys) -> None:
    from types import SimpleNamespace
    import tools.run_tests as runner

    monkeypatch.setattr(
        runner.subprocess, "run",
        lambda command, *, cwd, env, check: SimpleNamespace(returncode=-6),
    )
    assert runner.main(["quick"]) == 134
    assert "signal 6" in capsys.readouterr().err
