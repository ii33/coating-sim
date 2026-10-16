"""Isolated Qt Quick layout and interaction probe for MainWindow."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]


class MainCompactSidebarUiTests(unittest.TestCase):
    def test_main_window_layout_interactions_and_two_dpi_profiles(self):
        qa_root = ROOT / "build" / f"qa_ui_main_{uuid4().hex[:12]}"
        qa_root.mkdir(parents=True, exist_ok=False)

        for scale in ("1", "1.5"):
            environment = os.environ.copy()
            environment.update({
                "COAT_UI_MAIN_DPR": scale,
                "COAT_UI_MAIN_QA_ROOT": str(qa_root),
                "QT_QPA_PLATFORM": "offscreen",
                "QT_QUICK_BACKEND": "software",
                "QT_QUICK_CONTROLS_STYLE": "Fusion",
                "QT_SCALE_FACTOR": scale,
            })
            completed = subprocess.run(
                [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--render-probe"],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=120,
                check=False,
            )
            self.assertEqual(
                completed.returncode,
                0,
                f"DPR {scale} probe failed.\nstdout:\n{completed.stdout}"
                f"\nstderr:\n{completed.stderr}",
            )


def _render_probe() -> None:
    scale = os.environ["COAT_UI_MAIN_DPR"]
    qa_root = Path(os.environ["COAT_UI_MAIN_QA_ROOT"]).resolve()
    probe_root = qa_root / f"dpr_{scale.replace('.', '_')}"
    probe_root.mkdir(parents=True, exist_ok=False)
    temp_root = probe_root / "temp"
    temp_root.mkdir()
    for key in ("TEMP", "TMP", "TMPDIR"):
        os.environ[key] = str(temp_root)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_QUICK_BACKEND"] = "software"
    os.environ["QT_QUICK_CONTROLS_STYLE"] = "Fusion"
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))

    # Import the Qt test guard before application modules so QSettings writes
    # stay inside this probe's evidence directory and network requests fail.
    from qt_test_guard import NoNetworkAccessManagerFactory

    from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QObject, QPoint, QUrl, Qt
    from PySide6.QtGui import QAccessible, QFont
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    import numpy as np

    from coat_sim.core.api import CoatService
    from coat_sim.core.process_simulator import _field_metrics
    from coat_sim.ui import main as main_module
    from coat_sim.ui.fonts import bundled_font_family
    from coat_sim.ui.theme_service import ThemeService

    class LauncherStub(QObject):
        from PySide6.QtCore import Slot

        @Slot(str, str)
        def openWindow(self, _module: str, _path: str) -> None:
            pass

    app = QApplication.instance() or QApplication([])
    QQuickStyle.setStyle("Fusion")
    family = bundled_font_family()
    if family:
        app.setFont(QFont(family))

    app_data_root = probe_root / "app_data"
    designs_root = probe_root / "designs"
    main_module.data_path = lambda name="": app_data_root / str(name)
    main_module.design_directory = lambda name="process": designs_root / str(name)

    theme = ThemeService.instance()
    theme.configure(mode="light", theme_package_id="", font_scale=1.0,
                    density_scale=1.0)
    service_root = probe_root / "service_data"
    service = CoatService(root=service_root, restrict_to_root=False)
    bridge = main_module.MainBridge(service)
    launcher = LauncherStub()

    engine = QQmlApplicationEngine()
    network_factory = NoNetworkAccessManagerFactory()
    engine.setNetworkAccessManagerFactory(network_factory)
    theme.attach_engine(engine)
    warnings: list[str] = []
    engine.warnings.connect(
        lambda messages: warnings.extend(str(message.toString()) for message in messages)
    )
    engine.rootContext().setContextProperty("bridge", bridge)
    engine.rootContext().setContextProperty("launcher", launcher)
    engine.addImageProvider("thickness", bridge.image_provider)
    engine.load(QUrl.fromLocalFile(str(ROOT / "coat_sim" / "ui" / "qml" / "MainWindow.qml")))
    if not engine.rootObjects():
        raise AssertionError("MainWindow QML did not load:\n" + "\n".join(warnings))
    window = engine.rootObjects()[0]

    def flush(rounds: int = 14) -> None:
        for _ in range(rounds):
            QCoreApplication.processEvents()
            QTest.qWait(5)

    def find(object_name: str):
        result = window.findChild(QObject, object_name)
        if result is None:
            raise AssertionError(f"MainWindow has no object named {object_name!r}")
        return result

    def find_visual(root, object_name: str):
        pending = [root]
        while pending:
            item = pending.pop()
            if item.objectName() == object_name:
                return item
            pending.extend(item.childItems())
        raise AssertionError(f"visual item {object_name!r} is missing below {root.objectName()!r}")

    def capture(name: str) -> str:
        QTest.mouseMove(window, QPoint(0, max(0, int(window.height()) - 1)))
        flush()
        image = window.grabWindow()
        if image.isNull():
            raise AssertionError(f"Qt Quick returned a null image for {name}")
        path = probe_root / f"{name}.png"
        if not image.save(str(path)):
            raise AssertionError(f"could not save screenshot {path}")
        return str(path)

    def assert_chart_sidebar_geometry(label: str) -> None:
        panel_origin = panel.mapToItem(window.contentItem(), QPoint(0, 0))
        plot_origin = plot.mapToItem(window.contentItem(), QPoint(0, 0))
        if float(panel_origin.x()) + float(panel.property("width")) > float(window.width()) + 1:
            raise AssertionError(f"{label}: uniformity sidebar extends beyond the window")
        if float(plot.property("width")) <= 0 or float(plot.property("height")) <= 0:
            raise AssertionError(f"{label}: chart lost its drawing area")
        if float(plot_origin.x()) + float(plot.property("width")) > float(panel_origin.x()) + 1:
            raise AssertionError(f"{label}: chart and uniformity sidebar overlap")

    evidence: dict[str, object] = {
        "requested_dpr": float(scale),
        "screens": [],
        "screenshots": [],
        "checks": [],
    }
    try:
        window.resize(1280, 760)
        window.show()
        flush(24)
        actual_dpr = float(window.devicePixelRatio())
        if abs(actual_dpr - float(scale)) > 0.05:
            raise AssertionError(f"Qt window DPR {actual_dpr} did not match {scale}")
        evidence["actual_dpr"] = actual_dpr

        toolbar = find("mainToolbar")
        input_pane = find("simulationInputPane")
        panel = find("resultMetricsSidebar")
        state_label = find("uniformityState")
        rows_grid = find("uniformityRowsGrid")
        rail_button = find("uniformitySidebarButton")
        rail_toggle = find("uniformitySidebarToggle")
        status_control = find("windowChromeStatus")
        status_text = find("windowChromeStatusText")
        status_tooltip = window.findChild(QObject, "windowChromeStatusToolTip")
        bottom_panel = find("simulationBottomPanel")
        calculate_button = find("calculateButton")
        cancel_button = find("cancelCalculationButton")
        plot = find("depositionChartPlot")

        toolbar_origin = toolbar.mapToItem(window.contentItem(), QPoint(0, 0))
        input_pane_origin = input_pane.mapToItem(window.contentItem(), QPoint(0, 0))
        toolbar_y = float(toolbar_origin.y())
        toolbar_height = float(toolbar.property("height"))
        toolbar_implicit_height = float(toolbar.property("implicitHeight"))
        if abs(toolbar_y - float(window.property("contentTopMargin"))) > 0.5:
            raise AssertionError("top actions do not start at AppWindow.contentTopMargin")
        if toolbar_height >= 44:
            raise AssertionError("top actions kept the old oversized vertical band")
        if toolbar_height + 0.5 < toolbar_implicit_height:
            raise AssertionError("top action row is shorter than its visible controls")
        if abs(float(input_pane_origin.y()) - (toolbar_y + toolbar_height)) > 0.5:
            raise AssertionError("main content does not start at the top action row's bottom edge")
        if float(toolbar.property("x")) + float(toolbar.property("width")) > float(window.width()) + 1:
            raise AssertionError("top actions extend beyond the window")
        if not bool(window.property("uniformitySidebarExpanded")):
            raise AssertionError("a wide MainWindow did not initially expand its uniformity sidebar")
        if not 320 <= float(panel.property("width")) <= 360:
            raise AssertionError("expanded uniformity sidebar is outside its compact width range")
        if not bool(state_label.property("visible")) or str(state_label.property("text")) != bridge.uniformityStateText:
            raise AssertionError("empty uniformity prompt is not visible before results")
        if bool(rows_grid.property("visible")):
            raise AssertionError("uniformity grid appeared before any result was available")
        if abs(float(rail_toggle.property("width")) - 24) > 0.5:
            raise AssertionError("the sidebar toggle is not fixed at 24 logical px wide")
        if abs(float(rail_toggle.property("height")) - 44) > 0.5:
            raise AssertionError("the sidebar toggle does not use the large theme control height")
        if float(bottom_panel.property("height")) >= 156:
            raise AssertionError("bottom panel still reserves the removed status label")
        if bool(calculate_button.property("enabled")):
            raise AssertionError("calculate action ignores the empty process selection")
        if bool(cancel_button.property("enabled")):
            raise AssertionError("cancel action is enabled while no work is running")
        if plot is None:
            raise AssertionError("thickness plot is missing")
        if "选择工艺后计算膜厚。" not in str(window.property("statusText")):
            raise AssertionError("bridge status was not bound into the title bar")
        if str(status_text.property("text")) != "状态：" + bridge.status:
            raise AssertionError("title-bar status label differs from the bridge status")
        evidence["checks"].append("wide initial empty state, top spacing, status, chart, and action gating")
        evidence["top_layout"] = {
            "content_top_margin": float(window.property("contentTopMargin")),
            "toolbar_y": toolbar_y,
            "toolbar_height": toolbar_height,
            "toolbar_implicit_height": toolbar_implicit_height,
            "input_pane_y": float(input_pane_origin.y()),
            "initial_window_size": [int(window.width()), int(window.height())],
        }
        evidence["screenshots"].append(capture("initial_light"))

        # Resizing below the wide default collapses the panel without destroying
        # its scroll content. At 1120 px, the user can still expand it explicitly.
        window.resize(1120, 760)
        flush()
        if bool(window.property("uniformitySidebarExpanded")):
            raise AssertionError("the uniformity sidebar did not default to collapsed below 1200 px")
        if abs(float(panel.property("width")) - 64) > 1:
            raise AssertionError("collapsed uniformity rail is not fixed at 64 logical px")
        evidence["screenshots"].append(capture("empty_collapsed_light"))
        QMetaObject.invokeMethod(rail_button, "click")
        flush()
        if not bool(window.property("uniformitySidebarExpanded")):
            raise AssertionError("the 1120 px window could not expand its uniformity sidebar")
        if not 320 <= float(panel.property("width")) <= 360:
            raise AssertionError("expanded sidebar did not retain its compact width at 1120 px")
        if not bool(rail_button.property("selected")):
            raise AssertionError("the active uniformity rail button lacks its selected state")
        QMetaObject.invokeMethod(rail_button, "click")
        flush()
        if (not bool(window.property("uniformitySidebarExpanded"))
                or not bool(rail_button.property("selected"))):
            raise AssertionError("clicking the selected uniformity section unexpectedly toggled it off")
        evidence["screenshots"].append(capture("empty_expanded_light"))

        # The new rows carry seven bilingual label/value pairs. The compatible
        # legacy string getter remains available to other consumers.
        bridge._metrics = [
            "平均膜厚(Average thickness)：103.2 nm",
            "均匀性(Uniformity)：0.52%",
        ]
        x_axis = np.linspace(-10.0, 10.0, 21)
        field = np.tile(np.linspace(98.0, 109.0, 21), (21, 1))
        bridge._result = {
            **_field_metrics(field),
            "x": x_axis,
            "y": x_axis,
            "field_nm": field,
        }
        bridge._display_field = field
        bridge._image_min, bridge._image_max = bridge.image_provider.set_field(field)
        bridge._image_unit = "nm"
        bridge._image_revision += 1
        bridge._axis_bounds = {
            "x_min": float(x_axis[0]), "x_max": float(x_axis[-1]),
            "y_min": float(x_axis[0]), "y_max": float(x_axis[-1]),
        }
        bridge.changed.emit()
        flush()
        field_image = find("depositionFieldImage")
        if (float(bridge.imageMaximum) <= float(bridge.imageMinimum)
                or float(field_image.property("paintedWidth")) <= 0
                or float(field_image.property("paintedHeight")) <= 0):
            raise AssertionError("the UI probe did not render a real nonzero-thickness image")
        expected_rows = list(bridge.uniformityRows)
        if len(expected_rows) != 7 or not bool(rows_grid.property("visible")):
            raise AssertionError("the result uniformity grid did not expose all seven rows")
        if str(state_label.property("text")):
            raise AssertionError("successful results retained an empty/loading/failure prompt")
        for item in expected_rows:
            key = item["key"]
            label = find_visual(rows_grid, "uniformityLabel_" + key)
            value = find_visual(rows_grid, "uniformityValue_" + key)
            if str(label.property("text")) != item["label"]:
                raise AssertionError(f"uniformity label {key!r} was truncated or changed")
            if str(value.property("text")) != item["value"]:
                raise AssertionError(f"uniformity value {key!r} was truncated or changed")
            if float(label.property("width")) > float(panel.property("width")) + 1:
                raise AssertionError("a uniformity label exceeds the sidebar width")
            if float(value.property("width")) > float(panel.property("width")) + 1:
                raise AssertionError("a uniformity value exceeds the sidebar width")
        if window.findChild(QObject, "surfaceFigureSummaryHeading") is not None:
            raise AssertionError("the unrelated substrate-geometry summary still appears in the sidebar")
        if list(bridge.uniformityMetrics) != ["均匀性(Uniformity)：0.52%"]:
            raise AssertionError("the compatibility uniformityMetrics getter changed")
        if not bool(plot.property("visible")):
            raise AssertionError("result chart disappeared when uniformity rows loaded")
        assert_chart_sidebar_geometry("1120px expanded")
        evidence["checks"].append("seven full bilingual label/value rows, compatibility getter, and chart")
        evidence["screenshots"].append(capture("result_light"))

        # Existing values stay visible while a new calculation is in progress,
        # and a failed refresh exposes its full error without clearing them.
        bridge._busy = True
        bridge.changed.emit()
        flush()
        if str(state_label.property("text")) != bridge.uniformityStateText:
            raise AssertionError("uniformity loading state does not follow bridge state")
        if bridge.uniformityStateSeverity != "busy" or len(bridge.uniformityRows) != 7:
            raise AssertionError("uniformity rows were lost while loading or severity was wrong")
        if not bool(cancel_button.property("enabled")):
            raise AssertionError("cancel action was disabled while a calculation was running")
        evidence["screenshots"].append(capture("result_loading_light"))

        bridge._busy = False
        bridge._uniformity_result_notice = (
            "计算失败：新计算遇到可读错误；当前有效图像、七项均匀性数据和可导出结果仍为上次结果。"
            + "详细诊断保留在本状态提示中。"
        )
        bridge._uniformity_result_notice_severity = "error"
        bridge.changed.emit()
        flush()
        if str(state_label.property("text")) != bridge.uniformityStateText:
            raise AssertionError("uniformity failure state does not expose the full bridge message")
        if bridge.uniformityStateSeverity != "error" or len(bridge.uniformityRows) != 7:
            raise AssertionError("uniformity failure cleared valid rows or lost error severity")
        if not bool(plot.property("visible")):
            raise AssertionError("uniformity failure cleared the previous chart")
        evidence["checks"].append("loading and failed refresh preserve previous valid rows and chart")
        evidence["screenshots"].append(capture("result_failed_light"))

        # A long detailed status forces real ScrollView overflow. Repeated
        # collapse/expand must keep its scroll position and restore only a
        # still-visible focus target after the deferred turn.
        bridge._uniformity_result_notice += "\n" + "详细错误诊断与恢复建议。" * 40
        bridge.changed.emit()
        flush()
        sidebar_scroll = find("themeSidebarScroll")
        flickable_candidates = [sidebar_scroll, *sidebar_scroll.findChildren(QObject)]
        flickable = next((item for item in flickable_candidates
                          if item.metaObject().indexOfProperty("contentY") >= 0
                          and item.metaObject().indexOfProperty("contentHeight") >= 0), None)
        if flickable is None:
            raise AssertionError("could not locate the sidebar's scrollable Flickable")
        content_height = float(flickable.property("contentHeight"))
        view_height = float(flickable.property("height"))
        if content_height <= view_height + 1:
            raise AssertionError("long failure details did not create sidebar scroll overflow")
        desired_scroll = content_height - view_height
        flickable.setProperty("contentY", desired_scroll)
        flush()
        scroll_before = float(flickable.property("contentY"))
        if scroll_before <= 0:
            raise AssertionError("sidebar did not scroll to its long failure details")

        focus_target = find("uniformityDefinitions")
        focus_target.forceActiveFocus(Qt.FocusReason.OtherFocusReason)
        flush()
        if not bool(focus_target.property("activeFocus")):
            raise AssertionError("could not place focus inside the persistent sidebar content")
        for _cycle in range(3):
            QMetaObject.invokeMethod(rail_toggle, "click")
            flush()
            if bool(window.property("uniformitySidebarExpanded")):
                raise AssertionError("sidebar did not collapse on repeated toggle")
            if abs(float(panel.property("width")) - 64) > 1:
                raise AssertionError("sidebar rail changed width while collapsed")
            QMetaObject.invokeMethod(rail_toggle, "click")
            flush()
            if not bool(window.property("uniformitySidebarExpanded")):
                raise AssertionError("sidebar did not re-expand on repeated toggle")
            if not bool(focus_target.property("activeFocus")):
                raise AssertionError("sidebar did not restore a still-valid focus intent")
            if abs(float(flickable.property("contentY")) - scroll_before) > 1:
                raise AssertionError("sidebar scroll position changed during collapse/expand")
            if len(bridge.uniformityRows) != 7:
                raise AssertionError("sidebar toggle cleared the existing uniformity rows")
        evidence["checks"].append("long error scroll and focus survive three collapse/expand cycles")
        evidence["sidebar_scroll"] = {
            "content_height": content_height,
            "viewport_height": view_height,
            "content_y_before": scroll_before,
            "content_y_after": float(flickable.property("contentY")),
        }
        evidence["screenshots"].append(capture("result_failure_scrolled_light"))

        bridge._uniformity_result_notice = ""
        bridge._uniformity_result_notice_severity = "info"
        bridge.changed.emit()
        flush()

        # F4 remains native for all shared ComboBoxes; the sidebar shortcut is
        # available in other focus regions and cannot close a visible popup.
        combo_popup_verified = False
        for selector_name in ("algorithmSelector", "fastCycleSelector"):
            selector = find(selector_name)
            if not bool(selector.property("visible")):
                raise AssertionError(f"expected native ComboBox {selector_name!r} is hidden")
            selector.forceActiveFocus(Qt.FocusReason.OtherFocusReason)
            flush()
            focused_name = window.activeFocusItem().objectName() if window.activeFocusItem() else ""
            if not bool(selector.property("activeFocus")):
                raise AssertionError(
                    f"could not focus {selector_name}; active item={focused_name!r}")
            expanded_before_combo_f4 = bool(window.property("uniformitySidebarExpanded"))
            QTest.keyClick(window, Qt.Key.Key_F4)
            flush()
            if bool(window.property("uniformitySidebarExpanded")) != expanded_before_combo_f4:
                raise AssertionError(f"sidebar F4 shortcut stole {selector_name} native F4")
            popups = window.findChildren(QObject, "themeComboBoxPopup")
            active_popups = [popup for popup in popups if bool(popup.property("visible"))]
            if active_popups:
                combo_popup_verified = True
                QTest.keyClick(window, Qt.Key.Key_Escape)
                flush()
                if any(bool(popup.property("visible")) for popup in popups):
                    raise AssertionError(f"Escape did not close {selector_name} popup")
        target_button = find("computeTargetButton")
        target_popup = find("computeTargetPopup")
        if bool(target_popup.property("visible")):
            raise AssertionError("compute target popup opened without an explicit click")
        QMetaObject.invokeMethod(target_button, "click")
        flush()
        if not bool(target_popup.property("visible")):
            raise AssertionError("simulation-device button did not open host/device popup")
        selector = find("computeHostSelector")
        if not bool(selector.property("visible")):
            raise AssertionError("host selector is hidden after opening the device popup")
        selector.forceActiveFocus(Qt.FocusReason.OtherFocusReason)
        flush()
        expanded_before_combo_f4 = bool(window.property("uniformitySidebarExpanded"))
        QTest.keyClick(window, Qt.Key.Key_F4)
        flush()
        if bool(window.property("uniformitySidebarExpanded")) != expanded_before_combo_f4:
            raise AssertionError("sidebar F4 shortcut stole compute-host ComboBox native F4")
        QTest.keyClick(window, Qt.Key.Key_Escape)
        flush()
        if bool(target_popup.property("visible")):
            QTest.keyClick(window, Qt.Key.Key_Escape)
            flush()
        if bool(target_popup.property("visible")):
            raise AssertionError("Escape did not close simulation-device popup")
        evidence["checks"].append("simulation-device button opens host/device popup and Escape closes it")
        if combo_popup_verified:
            evidence["checks"].append("native F4 popup and Escape close for focused ComboBox")
        else:
            evidence.setdefault("limits", []).append(
                "F4 was sent to the focused algorithm, cycle, and device ComboBoxes in "
                "offscreen Qt; the sidebar stayed open, but no native popup appeared. "
                "This platform cannot verify the popup path."
            )
        bridge.setAlgorithm("reference")
        flush()
        if bridge.algorithm != "reference":
            raise AssertionError("algorithm switching did not reach the bridge")
        evidence["checks"].append("algorithm switching")
        budget_input = find("algorithmBudgetInput")
        if (bridge.algorithmBudgetSeconds != 0
                or int(budget_input.property("from")) != 0
                or int(budget_input.property("value")) != 0
                or bridge._algorithm_options() != {}):
            raise AssertionError("offline recipe default did not remain unlimited")
        bridge.setAlgorithmBudgetSeconds(30)
        flush()
        if (int(budget_input.property("value")) != 30
                or bridge._algorithm_options() != {"budget_s": 30}):
            raise AssertionError("explicit bounded recipe budget was not preserved")
        bridge.setAlgorithmBudgetSeconds(0)
        flush()
        if int(budget_input.property("value")) != 0 or bridge._algorithm_options() != {}:
            raise AssertionError("UI unlimited sentinel leaked a zero-second deadline")
        bridge.setAlgorithmBudgetSeconds(-1)
        bridge.setAlgorithmBudgetSeconds(301)
        if bridge.algorithmBudgetSeconds != 0:
            raise AssertionError("invalid UI recipe budget changed the unlimited default")
        evidence["checks"].append("unlimited default, explicit budget, and zero-sentinel reset")

        focus_target = find("uniformityDefinitions")
        focus_target.forceActiveFocus(Qt.FocusReason.OtherFocusReason)
        flush()
        if not bool(focus_target.property("activeFocus")):
            raise AssertionError("could not focus the uniformity sidebar before testing F4")
        QTest.keyClick(window, Qt.Key.Key_F4)
        flush()
        if bool(window.property("uniformitySidebarExpanded")):
            raise AssertionError("F4 outside a ComboBox did not collapse the sidebar")
        QTest.keyClick(window, Qt.Key.Key_F4)
        flush()
        if not bool(window.property("uniformitySidebarExpanded")):
            raise AssertionError("F4 outside a ComboBox did not reopen the sidebar")
        evidence["checks"].append("F4 toggles the rail from non-ComboBox focus")

        # The window's 1080 px minimum is the narrow geometry supported by this
        # page; verify both columns stay in bounds at that geometry.
        window.resize(1080, 680)
        flush()
        if int(window.width()) != 1080 or int(window.height()) != 680:
            raise AssertionError("MainWindow did not honor its supported narrow geometry")
        narrow_toolbar_origin = toolbar.mapToItem(window.contentItem(), QPoint(0, 0))
        narrow_pane_origin = input_pane.mapToItem(window.contentItem(), QPoint(0, 0))
        if abs(float(narrow_pane_origin.y()) -
               (float(narrow_toolbar_origin.y()) + float(toolbar.property("height")))) > 0.5:
            raise AssertionError("narrow layout introduced an empty band below the top actions")
        # At the supported 1080 px minimum both collapsed and expanded modes
        # keep a positive plot and remain disjoint from the rail/panel.
        if bool(window.property("uniformitySidebarExpanded")):
            QMetaObject.invokeMethod(rail_toggle, "click")
            flush()
        assert_chart_sidebar_geometry("1080px collapsed")
        if abs(float(panel.property("width")) - 64) > 1:
            raise AssertionError("narrow window does not keep the 64 px collapsed rail")
        evidence["screenshots"].append(capture("result_narrow_collapsed_light"))
        QMetaObject.invokeMethod(rail_button, "click")
        flush()
        if not bool(window.property("uniformitySidebarExpanded")):
            raise AssertionError("narrow window cannot explicitly expand the sidebar")
        assert_chart_sidebar_geometry("1080px expanded")
        evidence["checks"].append("1080 px supported geometry, collapsed and expanded without overlap")
        evidence["screenshots"].append(capture("result_narrow_expanded_light"))

        long_status = (
            "状态详情：用户可读的完整信息与输出位置仍由状态提示保留；"
            "C:\\实验项目\\长路径\\"
            + "膜厚结果和设备摘要\\" * 12
            + "result_bundle.coatasset"
        )
        bridge._status = long_status
        bridge.changed.emit()
        flush()
        if str(window.property("statusText")) != long_status:
            raise AssertionError("long status text was truncated before reaching the title bar")
        if status_tooltip is not None:
            raise AssertionError("WindowChrome status must not create an interactive tooltip")
        if bool(status_control.property("activeFocusOnTab")) or bool(status_control.property("focus")):
            raise AssertionError("WindowChrome status unexpectedly accepts keyboard focus")
        if any("MouseArea" in str(child.metaObject().className())
               for child in status_control.childItems()):
            raise AssertionError("WindowChrome status unexpectedly contains a mouse handler")
        accessible = QAccessible.queryAccessibleInterface(status_control)
        accessible_name = str(accessible.text(QAccessible.Text.Name)) if accessible else ""
        if long_status not in accessible_name:
            raise AssertionError("title-bar status lost its complete accessible name")
        bridge._busy = True
        bridge.changed.emit()
        flush()
        if str(window.property("statusSeverity")) != "busy":
            raise AssertionError("busy severity does not follow the bridge's real busy state")
        if not bool(cancel_button.property("enabled")):
            raise AssertionError("cancel action was disabled while the bridge is busy")
        bridge._busy = False
        bridge.changed.emit()
        evidence["checks"].append("long accessible status and busy/cancel state")

        # Existing dialogs remain openable and dismissible while status lives in
        # the chrome. No dialog acceptance runs a save or export in this probe.
        from PySide6.QtCore import QMetaObject

        history_button = find("openHistoryButton")
        history_dialog = find("historyDialog")
        QMetaObject.invokeMethod(history_button, "click")
        flush()
        if not bool(history_dialog.property("visible")):
            raise AssertionError("history toolbar action did not open its dialog")
        QMetaObject.invokeMethod(history_dialog, "close")
        flush()
        if bool(history_dialog.property("visible")):
            raise AssertionError("history dialog did not close")

        if window.findChild(QObject, "saveAssetDialog") is not None:
            raise AssertionError("retired asset save dialog remains in the project UI")
        for dialog_name in ("pdfOptionsDialog",):
            dialog = find(dialog_name)
            QMetaObject.invokeMethod(dialog, "open")
            flush()
            if not bool(dialog.property("visible")):
                raise AssertionError(f"{dialog_name} did not open")
            QMetaObject.invokeMethod(dialog, "close")
            flush()
            if bool(dialog.property("visible")):
                raise AssertionError(f"{dialog_name} did not close")
        checks = evidence["checks"]
        assert isinstance(checks, list)
        checks.append("history and PDF options open and close; legacy asset save UI removed")

        theme.configure(mode="dark", theme_package_id="", font_scale=1.0,
                        density_scale=1.0)
        flush()
        if str(window.property("themePaletteMode")) != "dark":
            raise AssertionError("dark theme switch did not reach MainWindow")
        evidence["screenshots"].append(capture("result_narrow_dark_long_status"))
        theme.configure(mode="light", theme_package_id="", font_scale=1.0,
                        density_scale=1.0)
        flush()
        if str(window.property("themePaletteMode")) != "light":
            raise AssertionError("light theme switch did not reach MainWindow")
        evidence["checks"].append("light/dark palette switching")

        evidence["screens"] = [
            {"name": screen.name(), "dpr": float(screen.devicePixelRatio()),
             "geometry": [screen.geometry().width(), screen.geometry().height()]}
            for screen in app.screens()
        ]
        evidence["qml_warnings"] = list(warnings)
        evidence_path = probe_root / "evidence.json"
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        print(f"UI probe passed at DPR {scale}: {evidence_path}")
    finally:
        window.close()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


if __name__ == "__main__":
    if "--render-probe" in sys.argv:
        _render_probe()
    else:
        unittest.main()
