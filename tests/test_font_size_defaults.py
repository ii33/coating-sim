"""Regression checks for the shared default font point size."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
import subprocess
import sys

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QFont, QGuiApplication

import coat_sim.ui.settings as settings
import coat_sim.ui.theme_service as theme_service
from coat_sim.ui.theme_package import parse_theme_package
from coat_sim.ui.theme_tokens import build_theme_snapshot


@pytest.fixture
def unconfigured_theme_service() -> Iterator[theme_service.ThemeService]:
    """Own and explicitly dispose a service using conftest's Qt application."""
    application = QGuiApplication.instance()
    assert isinstance(application, QGuiApplication)
    previous_font = QFont(application.font())
    service = theme_service.ThemeService(
        _construction_key=theme_service._INSTANCE_KEY
    )
    try:
        yield service
    finally:
        service.deleteLater()
        QCoreApplication.sendPostedEvents(service, QEvent.Type.DeferredDelete)
        application.setFont(previous_font)
        application.processEvents()


def test_missing_settings_use_nine_points_and_keep_saved_custom_value(tmp_path, monkeypatch):
    path = tmp_path / "ui_settings.json"
    monkeypatch.setattr(settings, "SETTINGS_PATH", path)

    assert settings.load_ui_settings()["font_point_size"] == 9

    path.write_text(json.dumps({"font_point_size": 17}), encoding="utf-8")
    assert settings.load_ui_settings()["font_point_size"] == 17
    assert settings.normalized_ui_settings({})["font_family"] == ""
    assert settings.normalized_ui_settings({"font_family": "  User Sans  "})["font_family"] == "User Sans"


def test_theme_uses_nine_point_fallback_and_preserves_custom_size(monkeypatch):
    calls: list[dict[str, object]] = []

    class Recorder:
        def configure(self, **kwargs):
            calls.append(kwargs)

    recorder = Recorder()
    monkeypatch.setattr(
        theme_service.ThemeService,
        "instance",
        classmethod(lambda cls: recorder),
    )

    theme_service.configure_theme_from_settings({})
    assert calls[-1]["font_scale"] == pytest.approx(9 / 12)
    assert calls[-1]["density_scale"] == 1

    theme_service.configure_theme_from_settings({"font_point_size": 17})
    assert calls[-1]["font_scale"] == pytest.approx(17 / 12)


def test_unconfigured_theme_service_starts_at_nine_points(
    unconfigured_theme_service: theme_service.ThemeService,
) -> None:
    service = unconfigured_theme_service
    assert service.current_snapshot["fontSizeBody"] == 9
    assert service.current_snapshot["fontScale"] == pytest.approx(9 / 12)


def test_default_theme_uses_bundled_noto_without_changing_nine_point_size():
    from coat_sim.ui.fonts import bundled_font_family

    family = bundled_font_family()
    assert family == "Noto Sans CJK SC"
    snapshot = build_theme_snapshot("light", font_scale=9 / 12)
    assert snapshot["fontFamily"] == family
    assert snapshot["fontSizeBody"] == 9


@pytest.mark.parametrize("entrypoint", ["settings", "launcher", "main"])
def test_cold_process_registers_default_font_and_preserves_overrides(
    entrypoint: str, tmp_path: Path,
) -> None:
    """No QApplication or font registration from pytest may reach this probe."""
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    for name in ("COAT_UI_PID", "COAT_UI_PARENT_PID", "COAT_UI_READY_FD", "COAT_UI_READY_TOKEN"):
        environment.pop(name, None)
    environment.update({
        "PYTHONPATH": str(root),
        "QT_QPA_PLATFORM": "offscreen",
        "QT_QUICK_BACKEND": "software",
        "QSG_RHI_BACKEND": "software",
        "COAT_ASSISTANT_RUNTIME_ROOT": str(tmp_path / "assistant-runtime"),
    })
    completed = subprocess.run(
        [sys.executable, "-c",
         "from tests.test_font_size_defaults import _cold_font_probe; "
         "import sys; _cold_font_probe(sys.argv[1], sys.argv[2])",
         entrypoint, str(tmp_path)],
        cwd=root, env=environment, capture_output=True, text=True,
        errors="replace", timeout=90, check=False,
    )
    assert completed.returncode == 0, (
        f"{entrypoint} cold font probe failed:\n{completed.stdout}\n{completed.stderr}"
    )
    report = json.loads(completed.stdout.splitlines()[-1])
    assert report["default"] == {"family": "Noto Sans CJK SC", "point_size": 9}
    if report["distinct_family_available"]:
        assert report["override"]["family"] != "Noto Sans CJK SC"
    else:
        assert report["family_override_limitation"] == "No installed non-Noto family resolves distinctly"
        assert report["override"]["family"] == "Noto Sans CJK SC"
    assert report["override"]["point_size"] == 17
    assert report["restored"] == report["default"]
    if entrypoint != "settings":
        assert report["visible_windows"] > 0


def _cold_font_probe(entrypoint: str, state_directory: str) -> None:
    """Exercise real entry initialization with isolated state and no services."""
    from unittest.mock import patch

    # The real launcher redirects both stdout names to its bounded log.
    # Keep the probe result channel captured before application initialization.
    report_stdout = sys.stdout

    # The guard isolates QSettings and networking, but never registers fonts.
    from tests import qt_test_guard  # noqa: F401
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QFontDatabase, QFontInfo
    from PySide6.QtWidgets import QApplication
    import coat_sim.ui.fonts as fonts

    assert QGuiApplication.instance() is None
    assert fonts._registered_application is None
    assert fonts._family == ""
    state = Path(state_directory)
    settings.SETTINGS_PATH = state / "ui_settings.json"
    theme_service.ThemeService.PACKAGE_ROOT = state / "themes"
    report: dict[str, object] = {}

    def inspect_font(application: QGuiApplication) -> None:
        def actual_font() -> dict[str, object]:
            info = QFontInfo(application.font())
            # QFontInfo's size is pixel-rounded by the platform; the configured
            # point size is retained exactly by the application's QFont.
            return {"family": info.family(), "point_size": application.font().pointSizeF()}

        # Reading an already-requested name is insufficient: Qt may substitute
        # Tahoma/DejaVu if the bundled font was never registered on cold start.
        assert fonts._registered_application is application
        assert fonts._family == "Noto Sans CJK SC"
        report["default"] = actual_font()
        assert report["default"] == {"family": "Noto Sans CJK SC", "point_size": 9}
        # Offscreen Windows can resolve SystemFont.FixedFont to the current
        # application font, including Noto. Select from installed families and
        # verify actual resolution using the same font attributes we will apply.
        available = set(QFontDatabase.families())
        preferred = ("Arial", "DejaVu Sans", "Liberation Sans", "Segoe UI", "Tahoma")
        candidates = [name for name in preferred if name in available]
        candidates.extend(sorted(available.difference(candidates)))
        explicit = resolved_family = "Noto Sans CJK SC"
        for candidate in candidates:
            if candidate.casefold().startswith("noto"):
                continue
            candidate_font = QFont(application.font())
            candidate_font.setFamily(candidate)
            candidate_font.setPointSizeF(17)
            actual_family = QFontInfo(candidate_font).family()
            if actual_family and not actual_family.casefold().startswith("noto"):
                explicit, resolved_family = candidate, actual_family
                break
        report["distinct_family_available"] = resolved_family != "Noto Sans CJK SC"
        report["explicit_family"] = explicit
        if not report["distinct_family_available"]:
            report["family_override_limitation"] = "No installed non-Noto family resolves distinctly"
        configured = settings.normalized_ui_settings({
            "font_family": explicit, "font_point_size": 17,
        })
        theme_service.configure_theme_from_settings(configured)
        settings.apply_font_settings(configured, app=application)
        report["override"] = actual_font()
        assert report["override"] == {"family": resolved_family, "point_size": 17}

        cleared = settings.normalized_ui_settings({"font_family": ""})
        theme_service.configure_theme_from_settings(cleared)
        settings.apply_font_settings(cleared, app=application)
        report["restored"] = actual_font()
        assert report["restored"] == report["default"]

    if entrypoint == "settings":
        application = QApplication([])
        settings.apply_font_settings({}, app=application)
        inspect_font(application)
    else:
        import launcher as one_click_launcher
        import coat_sim.ui.launcher as launcher_module
        import coat_sim.ui.main as main_module
        from coat_sim.core import data_files
        from coat_sim.ui.control import UiControlServer
        from coat_sim.ui.project_application_client import ProjectApplicationClient

        # Preserve the real main/launcher/QML/font chain. Redirect only writable
        # state, and avoid starting the unrelated independent assistant service.
        launcher_module.PROJECT_ROOT = state
        data_files.PROJECT_ROOT = state
        data_files.DATA_ROOT = state / "data"
        original_server_init = UiControlServer.__init__
        original_service = main_module.CoatService
        original_exec = QApplication.exec
        errors: list[BaseException] = []

        def server_init(self, dispatch, **kwargs):
            original_server_init(self, dispatch, root=state, **kwargs)

        def application_exec(application: QApplication) -> int:
            timer = QTimer(application)
            timer.setInterval(25)

            def inspect_visible_window() -> None:
                visible = [window for window in application.allWindows() if window.isVisible()]
                if not visible:
                    return
                timer.stop()
                try:
                    report["visible_windows"] = len(visible)
                    inspect_font(application)
                except BaseException as exc:
                    errors.append(exc)
                application.exit(0)

            timer.timeout.connect(inspect_visible_window)
            timer.start()
            QTimer.singleShot(30000, lambda: application.exit(3))
            return original_exec()

        arguments = ["launcher.py", "--software"]
        if entrypoint == "main":
            arguments.extend(["--module", "main"])
        with (
            patch.object(sys, "argv", arguments),
            patch.object(QApplication, "exec", application_exec),
            patch.object(UiControlServer, "__init__", server_init),
            patch.object(ProjectApplicationClient, "start", lambda self, endpoint: None),
            patch.object(main_module, "CoatService",
                         lambda **kwargs: original_service(root=state, **kwargs)),
        ):
            assert one_click_launcher.main() == 0
        if errors:
            raise errors[0]
        assert "default" in report, "entry point did not present a window"
    print(json.dumps(report), file=report_stdout, flush=True)


def test_explicit_theme_package_font_family_override_is_preserved(
    unconfigured_theme_service: theme_service.ThemeService,
    tmp_path: Path,
) -> None:
    service = unconfigured_theme_service
    package = parse_theme_package({
        "formatVersion": 1,
        "id": "qa-font-override",
        "name": "QA Font Override",
        "modes": {
            "light": {
                "tokenOverrides": {"fontFamily": "Explicit User Family"},
            },
        },
        "sounds": {},
    }, tmp_path)
    service._packages[package.package_id] = package

    service.configure(
        mode="light", font_scale=9 / 12, density_scale=1,
        theme_package_id=package.package_id,
    )

    assert service.current_snapshot["fontFamily"] == "Explicit User Family"
    assert service.current_snapshot["fontSizeBody"] == 9


def test_explicit_settings_font_family_overrides_theme_and_blank_restores_package(
    unconfigured_theme_service: theme_service.ThemeService,
    tmp_path: Path,
) -> None:
    service = unconfigured_theme_service
    package = parse_theme_package({
        "formatVersion": 1,
        "id": "qa-font-package",
        "name": "QA Font Package",
        "modes": {
            "light": {
                "tokenOverrides": {"fontFamily": "Package Family"},
            },
        },
        "sounds": {},
    }, tmp_path)
    service._packages[package.package_id] = package

    service.configure(
        mode="light", font_scale=9 / 12, density_scale=1,
        theme_package_id=package.package_id, font_family="User Settings Family",
    )
    application = QGuiApplication.instance()
    assert isinstance(application, QGuiApplication)
    assert service.current_snapshot["fontFamily"] == "User Settings Family"
    assert application.font().family() == "User Settings Family"

    service.configure(
        mode="light", font_scale=9 / 12, density_scale=1,
        theme_package_id=package.package_id, font_family="",
    )
    assert service.current_snapshot["fontFamily"] == "Package Family"
    assert application.font().family() == "Package Family"
