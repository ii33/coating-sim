"""Visible Windows QA with targeted Qt events; never emits global desktop input."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import time
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt, QTimer, QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QDialogButtonBox, QPushButton
from coat_sim.ui.settings import configure_qt_dpi_scale, apply_font_settings
from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.ui.film_design import FilmSimulationWindow, FilmDesignWindow
from tools.benchmark_film_design import real_zns_design


def click(widget):
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton)


def check(widget):
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(8, widget.height() // 2))


def modal_action(window, button, expected_type, action):
    errors = []
    def respond():
        dialog = QApplication.activeModalWidget()
        try:
            if not isinstance(dialog, expected_type) or dialog.parentWidget() is not window:
                raise AssertionError("The active modal is not owned by this QA window")
            action(dialog)
        except BaseException as error:
            errors.append(error)
            if dialog is not None and dialog.parentWidget() is window:
                dialog.reject()
    watchdog = QTimer(window)
    watchdog.setSingleShot(True)
    def timed_out():
        errors.append(TimeoutError("Owned modal exceeded 10 seconds"))
        dialog = QApplication.activeModalWidget()
        if dialog is not None and dialog.parentWidget() is window:
            dialog.reject()
    watchdog.timeout.connect(timed_out)
    watchdog.start(10000)
    QTimer.singleShot(150, respond)
    click(button)
    watchdog.stop()
    if errors:
        raise errors[0]


def calculate(app, window):
    started = time.perf_counter()
    worker = {}
    original = window.service.simulate
    def measured(*args, **kwargs):
        worker["started"] = time.perf_counter()
        result = original(*args, **kwargs)
        worker["finished"] = time.perf_counter()
        return result
    window.service.simulate = measured
    try:
        click(window.calculate_button)
        deadline = time.monotonic() + 60
        while window.busy and time.monotonic() < deadline:
            app.processEvents(); QTest.qWait(10)
        if window.busy or window.result is None:
            raise AssertionError("Independent calculation failed or exceeded the QA deadline")
        finish = time.perf_counter()
        drawn = time.perf_counter(); window.canvas.draw(); drawn = time.perf_counter() - drawn
        app.processEvents()
        return {"worker_seconds":worker["finished"] - worker["started"],
                "ui_wait_and_result_plot_seconds":finish - worker["finished"],
                "targeted_button_to_result_seconds":finish - started,
                "explicit_canvas_draw_seconds":drawn}
    finally:
        window.service.simulate = original


def snapshot(app, window, path):
    app.processEvents(); QTest.qWait(60)
    if not window.grab().save(str(path), "PNG"):
        raise AssertionError("Screenshot failed")
    return {"window_logical_px":[window.width(),window.height()],
            "canvas_logical_px":[window.canvas.width(),window.canvas.height()],
            "summary_height_logical_px":window.result_summary_area.height(),
            "details_expanded":window.result_details_toggle.isChecked(),
            "device_pixel_ratio":window.devicePixelRatioF(),"screenshot":str(path)}


def verify_plot_settings_workflow(window: FilmSimulationWindow, output: Path) -> None:
    """Exercise the unified settings entry and its transactional plot tab."""
    from coat_sim.ui.film_simulation_plot_settings import FilmSimulationAndPlotSettingsDialog

    output.mkdir(parents=True, exist_ok=True)

    def select_plot_tab(dialog: FilmSimulationAndPlotSettingsDialog) -> None:
        if dialog.windowTitle() != "仿真参数":
            raise AssertionError("Unified settings dialog title changed")
        dialog.tabs.setCurrentIndex(1)
        if dialog.tabs.currentIndex() != 1 or dialog.tabs.tabText(1) != "绘图参数":
            raise AssertionError("The unified dialog did not open its drawing-parameters tab")
        if not dialog.simulation_form.button_box.isHidden() or not dialog.plot_form.button_box.isHidden():
            raise AssertionError("Embedded forms exposed a second Apply/Cancel button box")

    def required_button(dialog: FilmSimulationAndPlotSettingsDialog,
                        standard_button: QDialogButtonBox.StandardButton) -> QPushButton:
        button = dialog.button_box.button(standard_button)
        if button is None:
            raise AssertionError(f"Unified settings button is missing: {standard_button}")
        return button

    def apply(dialog: FilmSimulationAndPlotSettingsDialog) -> None:
        select_plot_tab(dialog)
        dialog.grab().save(str(output / "绘图参数_全中文.png"))
        x = dialog.plot_form.axis_controls["x"]
        check(x["range_auto"])
        x["minimum"].setValue(450)
        x["maximum"].setValue(650)
        check(dialog.plot_form.grid_check)
        check(dialog.plot_form.curve_checks["T"])
        click(required_button(dialog, QDialogButtonBox.StandardButton.Apply))

    modal_action(window, window.simulation_settings_button,
                 FilmSimulationAndPlotSettingsDialog, apply)
    accepted = deepcopy(window._plot_settings)
    if window.figure.axes[0].get_xlim() != (450, 650):
        raise AssertionError("Applying the plot tab did not update the wavelength range")
    if len(window.figure.axes[0].lines) != 2:
        raise AssertionError("Applying the plot tab did not update visible curves")
    if any(line.get_visible() for line in window.figure.axes[0].get_xgridlines()):
        raise AssertionError("Applying the plot tab did not hide the grid")

    def cancel(dialog: FilmSimulationAndPlotSettingsDialog) -> None:
        select_plot_tab(dialog)
        dialog.plot_form.axis_controls["x"]["maximum"].setValue(680)
        click(required_button(dialog, QDialogButtonBox.StandardButton.Cancel))

    modal_action(window, window.simulation_settings_button,
                 FilmSimulationAndPlotSettingsDialog, cancel)
    if window._plot_settings != accepted:
        raise AssertionError("Cancel committed a draft from the unified dialog")

    def reopen(dialog: FilmSimulationAndPlotSettingsDialog) -> None:
        select_plot_tab(dialog)
        _simulation, plot = dialog.values()
        if plot != accepted:
            raise AssertionError("Reopening did not restore the committed plot settings")
        x = dialog.plot_form.axis_controls["x"]
        x["minimum"].setValue(700)
        click(required_button(dialog, QDialogButtonBox.StandardButton.Apply))
        if "最小值必须小于最大值" not in dialog.error_label.text() or not dialog.isVisible():
            raise AssertionError("Invalid plot range was not kept open with Chinese validation")
        dialog.grab().save(str(output / "绘图参数_中文校验.png"))
        click(required_button(dialog, QDialogButtonBox.StandardButton.Cancel))

    modal_action(window, window.simulation_settings_button,
                 FilmSimulationAndPlotSettingsDialog, reopen)
    if window._plot_settings != accepted:
        raise AssertionError("Invalid or cancelled settings changed the committed plot options")


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(); output = args.output.resolve(); output.mkdir(parents=True,exist_ok=True)
    print(json.dumps({"owned_pid":os.getpid(),"sys_executable":sys.executable,"output":str(output)}),flush=True)
    # Only this explicitly started QA process is subject to the deadline.
    process_deadline = threading.Timer(240, lambda: os._exit(124))
    process_deadline.daemon = True; process_deadline.start()
    os.environ.pop("QT_QPA_PLATFORM", None)
    ui = configure_qt_dpi_scale()
    QApplication.setAttribute(Qt.AA_DontUseNativeDialogs, True)
    app = QApplication([]); app.setQuitOnLastWindowClosed(False); apply_font_settings(ui, app=app)
    if app.platformName().lower() != "windows":
        raise RuntimeError("This visible acceptance requires the Windows Qt platform")
    service = FilmDesignService(ROOT); design = real_zns_design(service)
    design_path = output/'owned_qa_real_zns.json'; service.save(design_path,design,overwrite=True)
    window = FilmSimulationWindow(ROOT,service=service); window.resize(980,700); window.show(); app.processEvents()
    report = {"sys_executable":sys.executable,"platform":app.platformName(),"font_point_size":app.font().pointSizeF(),
              "global_input_used":False,"cases":[],"passed":False}
    try:
        load = next(button for button in window.findChildren(QPushButton) if button.text()=="加载膜系")
        with patch('coat_sim.ui.film_design.QFileDialog.getOpenFileName',return_value=(str(design_path),'JSON')):
            click(load)
        report['load_workflow'] = 'QTest load button, controlled picker path, actual JSON read; native picker not tested'
        assert window.current_design()["id"] == design["id"]
        assert window.design_label.text()=="未命名膜系"
        assert "非偏振" in window.settings_summary.text() and "unpolarized" not in window.settings_summary.text()
        report["cold_gui_calculation"] = calculate(app,window)
        assert not window.result_details_toggle.isChecked()
        report["cases"].append(snapshot(app,window,output/'真实ZnS结果_默认折叠.png'))
        result = window.result; generation = window._generation
        click(window.result_details_toggle); app.processEvents()
        assert window.result is result and window._generation==generation and not window.result_stale
        assert window.canvas.height()>=180
        assert "https://" not in window.notes.text() and "采用无损假设" not in window.notes.text()
        report["cases"].append(snapshot(app,window,output/'真实ZnS结果_展开说明.png'))
        verify_plot_settings_workflow(window, output)
        report["plot_dialog_apply_cancel_reopen"] = True
        window.resize(720,520); app.processEvents()
        for repeat in range(2):
            report[f"warm_gui_calculation_{repeat}"]=calculate(app,window)
            assert window.canvas.height()>=180 and window.canvas.height()>window.result_summary_area.height()*2
            report["cases"].append(snapshot(app,window,output/f'真实ZnS小窗口_重算{repeat+1}.png'))
            click(window.result_details_toggle); app.processEvents()
            assert not window.result_stale
            click(window.result_details_toggle); app.processEvents()
        window.service.export_csv(output/'真实ZnS结果.csv',window.result)
        (output/'真实ZnS结果.json').write_text(json.dumps(window.result,ensure_ascii=False,indent=2),encoding='utf-8')
        window.figure.savefig(output/'真实ZnS清晰曲线.png',dpi=160)
        report["notes_without_material_descriptions"]=window.notes.text()
        report["raw_unknown_k_retained"]=any(not row['evaluation']['k']['known'] for row in window.result['provenance'])
        assert report["raw_unknown_k_retained"]
        double = deepcopy(design); double['substrate']['sides']='double'
        rear = deepcopy(double['front_layers'][0]); rear['id']='owned_qa_rear'
        rear['thickness']={'value':1.0,'unit':'qw'}; double['back_layers']=[rear]
        editor = FilmDesignWindow(ROOT,service=service)
        try:
            editor.set_design(service.validate(double)); editor.resize(1150,780); editor.show(); app.processEvents()
            report['real_zns_design_qw_nm']={}
            for side,index in (('front',0),('back',1)):
                bar=editor.layer_tabs.tabBar(); QTest.mouseClick(bar,Qt.LeftButton,pos=bar.tabRect(index).center())
                app.processEvents(); table=editor.tables[side]
                assert table.item(0,5).text()!='不可用' and table.item(0,6).text()!='不可用'
                path=output/f'真实ZnS设计_{side}_QW与nm.png'; editor.grab().save(str(path),'PNG')
                report['real_zns_design_qw_nm'][side]={'qw':table.item(0,5).text(),'nm':table.item(0,6).text(),'screenshot':str(path)}
            editor.reference.selectAll(); QTest.keyClicks(editor.reference,'600'); QTest.keyClick(editor.reference,Qt.Key_Return)
            app.processEvents(); assert editor.reference.value()==600
            assert editor.current_design()['back_layers'][0]['thickness']==rear['thickness']
            click(editor.schematic_button); app.processEvents()
            assert editor._schematic_dialog is not None
            editor._schematic_dialog.grab().save(str(output/'真实ZnS双面示意_QW.png'),'PNG')
            editor._schematic_dialog.close()
            bar=editor.property_tabs.tabBar(); QTest.mouseClick(bar,Qt.LeftButton,pos=bar.tabRect(1).center())
            app.processEvents(); assert not editor.mode.isVisible()
            editor.grab().save(str(output/'自动基底参数_无手选模式.png'),'PNG')
            report['automatic_substrate_editor_verified']=True
        finally:
            editor._force_close=True; editor.close(); app.processEvents()
        report["passed"]=True
    finally:
        (output/'manifest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        window.close(); app.processEvents()
        process_deadline.cancel()
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
