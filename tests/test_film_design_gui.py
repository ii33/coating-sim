
"""Focused offscreen editing / real service / plotting regression."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
from pathlib import Path
import unittest
import copy
import time
import threading
import json
import io
import weakref
from unittest import mock
from PySide6.QtCore import QCoreApplication, QEvent, QThread, QTimer, Qt, QPoint, QPointF, QObject, QUrl
from PySide6.QtGui import QColor
from types import SimpleNamespace
from PySide6.QtTest import QTest
import numpy as np
from PySide6.QtWidgets import QApplication, QWidget, QTableWidgetItem, QMessageBox, QDialogButtonBox
from coat_sim.core import materials, optical_constants
from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.ui.film_design import FilmDesignWindow, FilmSimulationWindow, FilmWindowAdapter
from coat_sim.ui import film_design as film_ui
import shiboken6
from shiboken6 import isValid
from tools.pytest_qt_diagnostics import record_qt_disposal as _qt_disposal_trace


_APP: QApplication
_application = QApplication.instance()
if _application is None:
    _APP = QApplication([])
elif isinstance(_application, QApplication):
    _APP = _application
else:
    raise RuntimeError("Film GUI tests require a widget-capable QApplication")


class FilmDesignGuiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        folder = self.root / "data" / "materials"
        self.sets = {}
        for identifier, purpose, indices in [
                ("test_film", "film", [1.8, 2.1]), ("test_substrate", "substrate", [1.5])]:
            record = materials.default_material_profile("Synthetic GUI fixture " + identifier, id=identifier)
            record["category"] = purpose
            record["notes"] = "Synthetic regression data only; not a production optical material."
            optical = []
            for number, index in enumerate(indices):
                path = self.root / (identifier + str(number) + ".csv")
                path.write_text(f"wavelength,n,k\n400,{index},0\n700,{index},0\n", encoding="utf-8")
                data = optical_constants.import_optical_file(path, name=f"Fixture n={index}",
                    wavelength_unit="nm", notes="Synthetic unit-test fixture", source="GUI test")
                data["id"] = identifier + "_set_" + str(number)
                optical.append(data)
            record["optical_constants"] = optical
            materials.save_material(folder, record)
            self.sets[identifier] = optical
        self.service = FilmDesignService(self.root)
        self.design = self.service.new_design()
        self.design["substrate"].update(material_id="test_substrate",
            optical_set_id=self.sets["test_substrate"][0]["id"], mode="semi_infinite", sides="single")
        self.design["front_layers"] = [{"id": "layer_one", "material_id": "test_film",
            "optical_set_id": self.sets["test_film"][0]["id"],
            "thickness": {"value": 100, "unit": "nm"}, "note": "first"}]
        self.window = FilmDesignWindow(self.root, service=self.service)
        self.window.set_design(self.service.validate(self.design))
        self.simulation = FilmSimulationWindow(self.root, service=self.service)

    def tearDown(self):
        # close() deliberately retains these reusable production windows. Tests
        # must dispose their own Qt graphs on the GUI thread instead of leaving
        # them for later Python GC or application shutdown.
        self.assertEqual(QThread.currentThread(), _APP.thread())
        objects: dict[str, QObject] = {
            "design": self.window,
            "design_host": self.window._titlebar_host,
            "design_chrome": self.window._titlebar_widget,
            "design_engine": self.window._titlebar_widget.engine(),
            "simulation": self.simulation,
            "simulation_host": self.simulation._titlebar_host,
            "simulation_chrome": self.simulation._titlebar_widget,
            "simulation_engine": self.simulation._titlebar_widget.engine(),
            "calculation_signals": self.simulation._signals,
        }
        try:
            self.window._force_close = True
            self.window.close()
            self.simulation.close()
            executor = self.simulation._executor
            if executor is not None:
                # close requests cancellation; joining also settles each Future's
                # completion callback before its signal object can be deleted.
                executor.shutdown(wait=True, cancel_futures=True)
            _APP.processEvents()
            # Unload each scene while its Python host and QWidget are still
            # fully alive. Parent QObject child deletion order must not leave
            # WindowChrome bindings evaluating against a host in destruction.
            # The default engine stays owned by its QQuickWidget throughout.
            for label, chrome in (("simulation", self.simulation._titlebar_widget),
                                  ("design", self.window._titlebar_widget)):
                _qt_disposal_trace(self.id(), "unload_begin", label)
                chrome.setSource(QUrl())
                self.assertIsNone(chrome.rootObject(), "test-owned QML scene was not unloaded")
                self.assertTrue(isValid(chrome.engine()), "unloading must preserve the widget-owned engine")
                _qt_disposal_trace(self.id(), "unload_end", label)
            # _CalculationSignals has no QObject parent, so window deletion alone
            # does not destroy it. No calculation can emit through it after join.
            self._delete_qt_graph(
                (self.simulation._signals, self.simulation, self.window), objects)
        finally:
            self.temporary.cleanup()

    def _delete_qt_graph(
        self, roots: tuple[QObject, ...], objects: dict[str, QObject]
    ) -> None:
        """Delete only test-owned roots and verify their exact QObject graph."""
        self.assertEqual(QThread.currentThread(), _APP.thread())
        expected = set(objects)
        references: dict[str, weakref.ReferenceType[QObject]] = {
            name: weakref.ref(value) for name, value in objects.items()
        }
        destroyed: set[str] = set()
        test_id = self.id()

        def receipt(label: str) -> None:
            destroyed.add(label)
            _qt_disposal_trace(test_id, "destroyed", label)

        for name, value in objects.items():
            # The receipt captures only a label, never the QObject being watched.
            value.destroyed.connect(lambda *_args, label=name: receipt(label))
        # Destroy only the test-owned roots synchronously. Qt deletes each
        # parent-owned child (chrome host, Quick widget, and its QML engine)
        # exactly once; deleting those wrappers again would double-delete.
        # A global DeferredDelete dispatch can crash while a QQuickWidget
        # engine is being torn down, so do not rely on deleteLater here.
        for root in roots:
            label = next(name for name, value in objects.items() if value is root)
            _qt_disposal_trace(test_id, "delete_begin", label)
            if isValid(root):
                shiboken6.delete(root)
            _qt_disposal_trace(test_id, "delete_end", label)
        _APP.processEvents()
        self.assertEqual(destroyed, expected, "test-owned Qt graph was not destroyed")
        still_alive: list[str] = []
        for name, reference in references.items():
            remaining = reference()
            if remaining is not None and isValid(remaining):
                still_alive.append(name)
        self.assertEqual(still_alive, [], "test left live C++ Qt objects")

    def test_both_faces_show_qw_and_nm_without_mutating_thickness(self):
        design = copy.deepcopy(self.design)
        design['substrate']['sides']='double'
        back = copy.deepcopy(design['front_layers'][0]); back['id']='back_one'
        back['thickness']={'value':1.0,'unit':'qw'}; design['back_layers']=[back]
        self.window.set_design(self.service.validate(design))
        front = self.window.tables['front']; rear = self.window.tables['back']
        self.assertAlmostEqual(float(front.item(0,5).text()),4*1.8*100/550,places=5)
        self.assertEqual(float(front.item(0,6).text()),100)
        self.assertEqual(float(rear.item(0,5).text()),1)
        self.assertAlmostEqual(float(rear.item(0,6).text()),550/(4*1.8),places=4)
        self.window.reference.setValue(600)
        self.assertAlmostEqual(float(front.item(0,5).text()),1.2)
        self.assertAlmostEqual(float(rear.item(0,6).text()),600/(4*1.8),places=4)
        front.cellWidget(0,1).setCurrentIndex(front.cellWidget(0,1).findData(self.sets['test_film'][1]['id']))
        self.assertAlmostEqual(float(front.item(0,5).text()),1.4)
        front.setItem(0,2,QTableWidgetItem('200'))
        self.assertEqual(float(front.item(0,6).text()),200)
        self.assertEqual(self.window.current_design()['back_layers'][0]['thickness'],back['thickness'])
        self.assertFalse(front.item(0,5).flags() & Qt.ItemIsEditable)
        front.cellWidget(0,1).setCurrentIndex(0)
        self.assertEqual(front.item(0,5).text(),'不可用')
        self.assertEqual(front.item(0,6).text(),'200')
        self.assertEqual(self.window.current_design()['front_layers'][0]['thickness'],{'value':200.,'unit':'nm'})
        self.window.sides.setCurrentIndex(self.window.sides.findData('single'))
        self.assertFalse(self.window.exit_medium.isEnabled())
        self.assertEqual(len(self.window.current_design()['back_layers']),1)
        self.assertEqual(self.window.current_design()['substrate']['mode'],'auto')

    def test_window_chrome_palette_forwarding_ends_with_host_lifetime(self):
        probe_window = QWidget()
        probe_window.winId()
        host = film_ui._WindowChromeHost(probe_window)
        reference = weakref.ref(host)
        forwarded: list[int] = []
        host.paletteChanged.connect(lambda: forwarded.append(threading.get_ident()))
        try:
            _APP.paletteChanged.emit(_APP.palette())
            self.assertEqual(forwarded, [threading.get_ident()])
            del host
            probe_window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            _APP.processEvents()
            self.assertFalse(isValid(probe_window))
            self.assertIsNone(reference(), "application palette signal retained a deleted host")
            errors = io.StringIO()
            with mock.patch("sys.stderr", errors):
                _APP.paletteChanged.emit(_APP.palette())
                _APP.processEvents()
            self.assertEqual(forwarded, [threading.get_ident()])
            self.assertEqual(errors.getvalue(), "", "palette notification reached a deleted host")
        finally:
            if isValid(probe_window):
                probe_window.close()
                probe_window.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                _APP.processEvents()

    def test_legacy_simulation_large_and_invalid_count_inputs_are_safe(self):
        self.simulation.set_design(self.design)
        cancel_event = threading.Event()
        self.simulation._calculation_cancel_event = cancel_event
        self.simulation.samples.setValue(3_000_000_001)
        self.assertTrue(cancel_event.is_set())
        self.assertEqual(self.simulation.samples.value(), 3_000_000_001)
        self.assertEqual(self.simulation.settings()['samples'], 3_000_000_001)
        old_design = self.simulation.current_design()
        old_settings = self.simulation.settings()
        old_token = (self.simulation._generation,
                     self.simulation._signature(old_design, old_settings), old_design)

        self.simulation.samples.setValue(1)
        self.assertIn("无效", self.simulation.settings_summary.text())
        self.simulation._finish_calculation((old_token, {"R": [], "T": [], "A": []}, ""))
        self.assertIsNone(self.simulation.result)

        self.simulation.samples.setValue(301)
        self.simulation.samples.setText("")
        self.assertIn("无效", self.simulation.settings_summary.text())
        self.simulation.samples.setValue(3_000_000_001)
        self.assertEqual(self.simulation.samples.value(), 3_000_000_001)

    def test_plot_percent_formatter_and_raw_export_contract(self):
        import csv
        self.simulation.set_design(self.design); result=self.simulation.calculate()
        axes=self.simulation.figure.axes[0]
        self.assertEqual(axes.get_ylim(),(0,1))
        self.assertEqual(axes.yaxis.get_major_formatter()(0.5),'50%')
        self.simulation._plot_settings['y']={'unit':'percent','auto':False,'min':10,'max':80,'major_tick_auto':False,'major_tick':20}
        self.simulation.plot_result(); axes=self.simulation.figure.axes[0]
        self.assertEqual(axes.get_ylim(),(.1,.8))
        np.testing.assert_array_equal(axes.lines[0].get_ydata(),result['R'])
        self.simulation.plot_result()
        self.assertEqual(self.simulation.figure.axes[0].get_ylim(),(.1,.8))
        path=self.root/'percentage_display_raw.csv'
        self.service.export_csv(path,result)
        with path.open(encoding='utf-8-sig') as stream: rows=list(csv.DictReader(stream))
        self.assertEqual(float(rows[0]['R']),result['R'][0])
        report=json.loads(rows[0]['evaluation_report_json'])
        self.assertEqual(report['units']['R'],'fraction (0–1)')
        output=self.root/'percent.png'; self.simulation.figure.savefig(output)
        self.assertGreater(output.stat().st_size,5000)

    def test_real_mouse_calculation_accepts_unknown_k_when_enabled(self):
        # The window installs each QHeaderView with an explicit table parent;
        # keep checking the C++ round-trip returns the real owned widget type.
        for table in (*self.window.tables.values(), self.window.symbols_table):
            header = table.horizontalHeader()
            self.assertIsInstance(header, film_ui.QHeaderView)
            self.assertIs(header.parentWidget(), table)
        from coat_sim.core.material_business import MaterialLibrary
        library=MaterialLibrary(self.root);record=library.read('test_film')['record']
        record['optical_constants'][0]['k']=None;library.write(record,update=True)
        design=copy.deepcopy(self.design);design['use_k']=True;design['assume_lossless']=False
        self.simulation.set_design(design);self.simulation.show();_APP.processEvents()
        with mock.patch.object(QMessageBox,'warning',side_effect=AssertionError('Unexpected calculation warning dialog')):
            QTest.mouseClick(self.simulation.calculate_button,Qt.LeftButton)
            deadline=time.monotonic()+15
            while self.simulation.busy and time.monotonic()<deadline: QTest.qWait(10)
        self.assertFalse(self.simulation.busy);self.assertIsNotNone(self.simulation.result)
        self.assertEqual(self.simulation.error,'');self.assertFalse(self.simulation.result_stale)
        self.assertNotIn('无损假设',self.simulation.notes.text())
        self.assertIn('n 超域',self.simulation.settings_summary.text())
        self.assertNotIn('n/k 超域',self.simulation.settings_summary.text())
        row=next(p for p in self.simulation.result['provenance'] if p['material_id']=='test_film')
        self.assertIsNone(row['optical_set']['k'])
        self.assertTrue(row['evaluation']['k']['enabled'])
        self.assertEqual(row['evaluation']['k']['default_zero_intervals'][0]['reason'],'missing')

    def test_edit_group_move_save_load_compute_plot_and_export(self):
        table = self.window.tables["front"]
        sets = table.cellWidget(0, 1)
        sets.setCurrentIndex(sets.findData(self.sets["test_film"][1]["id"]))
        self.assertEqual(self.window.current_design()["front_layers"][0]["optical_set_id"], self.sets["test_film"][1]["id"])
        self.window.add_layer()
        material = table.cellWidget(1, 0); material.setCurrentIndex(material.findData("test_film"))
        sets = table.cellWidget(1, 1); sets.setCurrentIndex(sets.findData(self.sets["test_film"][0]["id"]))
        table.setItem(1, 2, QTableWidgetItem("85"))
        table.selectRow(1); self.window.move_layer(-1)
        self.assertEqual(self.window.current_design()["front_layers"][1]["id"], "layer_one")
        path = self.root / "data" / "film_designs" / "gui.cfilm"
        self.window.save_path(path)
        saved = self.window.current_design()
        self.window.load_path(path)
        self.assertEqual(self.window.current_design(), saved)
        self.simulation.set_design(saved)
        result = self.simulation.calculate()
        self.assertEqual(len(result["wavelength_nm"]), 301)
        self.assertIn("来源波段", self.simulation.notes.text())
        self.assertIn("线性插值", self.simulation.notes.text())
        self.assertFalse(self.simulation.figure.axes[0].texts)
        self.assertEqual(self.window.windowTitle(), "设计")
        self.assertEqual(self.simulation.windowTitle(), "仿真计算")
        self.assertIn("k：来源波段", self.simulation.notes.text())
        self.assertIn("分段线性插值", self.simulation.notes.text())
        np.testing.assert_allclose(np.asarray(result["R"]) + result["T"] + result["A"], 1., atol=1e-10)
        self.assertEqual(len(self.simulation.figure.axes[0].lines), 3)
        self.simulation.plot_kind.setCurrentIndex(1)
        self.assertEqual(len(self.simulation.figure.axes[0].lines), 4)
        self.service.export_csv(self.root / "data" / "spectrum.csv", result)
        self.simulation.figure.savefig(self.root / "spectrum.png")
        self.assertGreater((self.root / "spectrum.png").stat().st_size, 1000)
        self.assertGreater((self.root / "data" / "spectrum.csv").stat().st_size, 1000)

    def test_expression_expansion_marks_manual_edits_and_keeps_group(self):
        design = self.window.current_design()
        design["expression"] = {"text": "G1 (AB)^2", "in_sync": False,
            "symbols": {symbol: {"material_id": "test_film",
                "optical_set_id": self.sets["test_film"][number]["id"],
                "thickness": {"value": 1, "unit": "qw"}}
                for number, symbol in enumerate("AB")}}
        self.window.set_design(self.service.validate(design))
        self.window.expand_expression()
        expanded = self.window.current_design()
        self.assertEqual(len(expanded["front_layers"]), 4)
        self.assertTrue(expanded["expression"]["in_sync"])
        group = expanded["front_layers"][0].get("group")
        self.window.tables["front"].setItem(0, 2, QTableWidgetItem("0.9"))
        edited = self.window.current_design()
        self.assertFalse(edited["expression"]["in_sync"])
        self.assertEqual(edited["front_layers"][0].get("group"), group)

    def test_long_multiset_reports_keep_the_chart_visible_after_recalculation(self):
        source = "长来源 https://example.test/paper DOI:10.example/test 许可 CC-BY；" * 30
        note = "长材料条件与数据集备注，不属于结果页的计算语义。" * 80
        for path in (self.root / "data" / "materials").glob("*.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            for optical in record["optical_constants"]:
                optical.update(source=source, notes=note)
            materials.save_material(path.parent, record, overwrite=True)
        design = copy.deepcopy(self.design)
        design["front_layers"].append({
            "id": "second_set", "material_id": "test_film",
            "optical_set_id": self.sets["test_film"][1]["id"],
            "thickness": {"value": 75, "unit": "nm"}, "note": "",
        })
        self.simulation.set_design(self.service.validate(design))
        self.simulation.resize(720, 520)
        self.simulation.show()
        self.assertFalse(self.simulation.result_details_toggle.isChecked())
        for _ in range(2):
            QTest.mouseClick(self.simulation.calculate_button, Qt.LeftButton)
            self._pump_until(lambda: not self.simulation.busy)
            _APP.processEvents()
            self.assertIsNotNone(self.simulation.result)
            before = self.simulation.result
            generation = self.simulation._generation
            QTest.mouseClick(self.simulation.result_details_toggle, Qt.LeftButton)
            _APP.processEvents()
            self.assertTrue(self.simulation.result_summary_area.isVisible())
            self.assertIs(self.simulation.result, before)
            self.assertEqual(self.simulation._generation, generation)
            self.assertGreaterEqual(self.simulation.canvas.height(), 180)
            self.assertGreater(self.simulation.canvas.height(), self.simulation.result_summary_area.height() * 2)
            self.assertEqual(len(self.simulation.figure.axes[0].lines), 3)
            self.assertFalse(self.simulation.figure.axes[0].texts)
            self.assertNotIn(source, self.simulation.notes.text())
            self.assertNotIn(note, self.simulation.notes.text())
            self.assertNotIn("https://", self.simulation.notes.text())
            self.assertIn("来源波段", self.simulation.notes.text())
            self.assertIn("线性插值", self.simulation.notes.text())
            for item in self.simulation.result["provenance"]:
                self.assertEqual(item["optical_set"]["source"], source)
                self.assertEqual(item["optical_set"]["notes"], note)
            QTest.mouseClick(self.simulation.result_details_toggle, Qt.LeftButton)
            _APP.processEvents()
            self.assertFalse(self.simulation.result_summary_area.isVisible())

    def test_plot_settings_apply_cancel_reopen_and_chinese_validation(self):
        from coat_sim.ui.film_simulation_plot_settings import FilmSimulationAndPlotSettingsDialog
        self.simulation.set_design(self.design)
        self.simulation.show()
        self.simulation.calculate()
        _APP.processEvents()
        self.assertIn("非偏振", self.simulation.settings_summary.text())
        self.assertNotIn("unpolarized", self.simulation.settings_summary.text())

        def edit(action):
            errors = []
            def interact():
                dialog = QApplication.activeModalWidget()
                try:
                    assert isinstance(dialog, FilmSimulationAndPlotSettingsDialog)
                    self.assertEqual(dialog.windowTitle(), "仿真参数")
                    dialog.tabs.setCurrentIndex(1)
                    action(dialog)
                except BaseException as exc:
                    errors.append(exc)
                    if dialog is not None:
                        dialog.reject()
            QTimer.singleShot(0, interact)
            QTest.mouseClick(self.simulation.simulation_settings_button, Qt.MouseButton.LeftButton)
            if errors:
                raise errors[0]

        def apply(dialog):
            plot = dialog.plot_form
            x = plot.axis_controls["x"]
            QTest.mouseClick(x["range_auto"], Qt.MouseButton.LeftButton, pos=QPoint(8, x["range_auto"].height() // 2))
            x["minimum"].setValue(450); x["maximum"].setValue(650)
            QTest.mouseClick(plot.curve_checks["T"], Qt.MouseButton.LeftButton, pos=QPoint(8, plot.curve_checks["T"].height() // 2))
            QTest.mouseClick(plot.grid_check, Qt.MouseButton.LeftButton, pos=QPoint(8, plot.grid_check.height() // 2))
            QTest.mouseClick(dialog.button_box.button(QDialogButtonBox.StandardButton.Apply), Qt.MouseButton.LeftButton)
        edit(apply)
        saved = copy.deepcopy(self.simulation._plot_settings)
        self.assertEqual(self.simulation.figure.axes[0].get_xlim(), (450, 650))
        self.assertEqual(len(self.simulation.figure.axes[0].lines), 2)
        self.assertFalse(saved["grid"])
        self.assertFalse(any(line.get_visible() for line in self.simulation.figure.axes[0].get_xgridlines()))

        def cancel(dialog):
            dialog.plot_form.axis_controls["x"]["maximum"].setValue(680)
            QTest.mouseClick(dialog.button_box.button(QDialogButtonBox.StandardButton.Cancel), Qt.MouseButton.LeftButton)
        edit(cancel)
        self.assertEqual(self.simulation._plot_settings, saved)

        def reopen(dialog):
            self.assertEqual(dialog.values()[1], saved)
            dialog.plot_form.axis_controls["x"]["minimum"].setValue(700)
            QTest.mouseClick(dialog.button_box.button(QDialogButtonBox.StandardButton.Apply), Qt.MouseButton.LeftButton)
            self.assertTrue(dialog.isVisible())
            self.assertIn("最小值必须小于最大值", dialog.error_label.text())
            QTest.mouseClick(dialog.button_box.button(QDialogButtonBox.StandardButton.Cancel), Qt.MouseButton.LeftButton)
        edit(reopen)
        self.assertEqual(self.simulation._plot_settings, saved)

    def test_control_adapter_guards_unsaved_and_calculates_real_service(self):
        from coat_sim.ui.control import UiControlError
        adapter = FilmWindowAdapter("film_design", self.window, self.window, self.root)
        adapter.update(fields={"name": "AI draft"})
        with self.assertRaises(UiControlError):
            adapter.update(data=self.design)
        adapter.update(data=self.design, force=True)
        self.assertTrue(adapter.snapshot()["document"]["front_layers"])
        simulator = FilmWindowAdapter("film_simulation", self.simulation, self.simulation, self.root)
        simulator.update(data=self.window.current_design(), fields={"samples": 31,
            "light_source": {"kind": "blackbody", "temperature_k": 5778}, "plot_kind": "source"})
        state = simulator.action("calculate")
        self.assertTrue(state["busy"])
        self._pump_until(lambda: not self.simulation.busy)
        self.assertTrue(simulator.snapshot()["has_result"])
        self.assertFalse(simulator.snapshot()["result_stale"])
        self.assertEqual(len(self.simulation.figure.axes[0].lines), 4)


    def test_real_zns_schematic_uses_the_bundled_font_for_every_text_artist(self):
        real_root = Path(__file__).resolve().parents[1]
        service = FilmDesignService(real_root)
        zns = next(row for row in service.material_options(purpose="film")
                   if row.get("id") == "builtin_zns_literature_20261004")
        optical = next(row for row in zns["optical_sets"]
                       if row.get("id") == "ri_zns_amotchkina_n")
        substrates = service.material_options(purpose="substrate")
        substrate = next((row for row in substrates
                          if row.get("id") == "film_substrate_schott_nbk7"), substrates[0])
        design = service.new_design()
        design["substrate"].update(material_id=substrate["id"],
            optical_set_id=substrate["optical_sets"][0]["id"],
            mode="semi_infinite", sides="single")
        design["front_layers"] = [{
            "id": "real_zns_layer", "material_id": zns["id"], "optical_set_id": optical["id"],
            "thickness": {"value": 1.008, "unit": "um"},
            "note": "Source-reported e-beam film at 120 °C; k remains unknown.",
        }]
        window = FilmDesignWindow(real_root, service=service)
        try:
            window.set_design(service.validate(design))
            window._show_schematic()
            _APP.processEvents()
            figure = window._schematic_figure
            axes = figure.axes[0]
            rendered = "\n".join(text.get_text() for text in axes.texts)
            self.assertIn("L1 ZnS / Amotchkina", rendered)
            self.assertIn("Amotchkina/EB120°C/2020/仅n", rendered)
            self.assertIn("µm", rendered)
            self.assertEqual(window._schematic_dialog.windowTitle(), "膜堆结构示意")

            font_file = film_ui._film_cjk_font.resolve()
            font_properties = film_ui._FILM_FONT_PROPERTIES
            self.assertIsNotNone(font_properties)
            self.assertEqual(Path(font_properties.get_file()).resolve(), font_file)
            from matplotlib import font_manager
            from matplotlib.ft2font import FT2Font
            glyph_map = FT2Font(str(font_file)).get_charmap()
            artists = [*axes.texts, axes.title]
            for artist in artists:
                for character in artist.get_text():
                    if not character.isspace():
                        self.assertTrue(ord(character) in glyph_map,
                                        f"bundled font lacks rendered character {character!r}")
                properties = artist.get_fontproperties()
                self.assertEqual(Path(properties.get_file()).resolve(), font_file)
                self.assertEqual(Path(font_manager.findfont(properties)).resolve(), font_file)
        finally:
            window._force_close = True
            window.close()
            objects: dict[str, QObject] = {
                "zns_design": window,
                "zns_host": window._titlebar_host,
                "zns_chrome": window._titlebar_widget,
                "zns_engine": window._titlebar_widget.engine(),
            }
            if window._schematic_dialog is not None:
                objects["zns_schematic"] = window._schematic_dialog
            self._delete_qt_graph((window,), objects)

    def _pump_until(self, condition, timeout=10):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            _APP.processEvents()
            time.sleep(.001)
        self.assertTrue(condition(), "Qt callback exceeded bounded timeout")

    def test_background_button_keeps_ui_responsive_and_plots_on_main_thread(self):
        self.simulation.set_design(self.design)
        self.simulation.samples.setValue(31)
        entered, release = threading.Event(), threading.Event()
        original = self.service.simulate
        worker_threads, plot_threads, ticks = [], [], []
        main_thread = threading.get_ident()
        def delayed(design, settings, *, cancel_event=None):
            worker_threads.append(threading.get_ident())
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test worker gate timed out")
            return original(design, settings, cancel_event=cancel_event)
        original_plot = self.simulation.plot_result
        def plot(*args):
            plot_threads.append(threading.get_ident())
            return original_plot(*args)
        with mock.patch.object(self.service, "simulate", side_effect=delayed), mock.patch.object(self.simulation, "plot_result", side_effect=plot):
            try:
                self.simulation.calculate_button.click()
                self.assertTrue(self.simulation.busy)
                self.assertFalse(self.simulation.calculate_button.isEnabled())
                self.assertTrue(entered.wait(1))
                QTimer.singleShot(0, lambda: ticks.append(True))
                self._pump_until(lambda: bool(ticks), timeout=1)
                self.assertTrue(self.simulation.busy)
                with self.assertRaises(ValueError):
                    self.simulation.start_calculation()
            finally:
                release.set()
            self._pump_until(lambda: not self.simulation.busy)
        self.assertNotEqual(worker_threads, [main_thread])
        self.assertEqual(plot_threads, [main_thread])
        self.assertFalse(self.simulation.result_stale)
        self.assertTrue(self.simulation.calculate_button.isEnabled())
        self.assertTrue(all(button.isEnabled() for button in self.simulation.export_buttons))
        self.assertEqual(len(self.simulation.figure.axes[0].lines), 3)

    def test_background_error_restores_buttons_and_reports_on_main_thread(self):
        self.simulation.set_design(self.design)
        warning_threads = []
        with mock.patch.object(self.service, "simulate", side_effect=ValueError("synthetic worker failure")), mock.patch("coat_sim.ui.film_design.QMessageBox.warning", side_effect=lambda *args: warning_threads.append(threading.get_ident())):
            self.simulation.start_calculation()
            self._pump_until(lambda: not self.simulation.busy)
        self.assertEqual(warning_threads, [threading.get_ident()])
        self.assertIn("synthetic worker failure", self.simulation.error)
        self.assertIsNone(self.simulation.result)
        self.assertTrue(self.simulation.result_stale)
        self.assertTrue(self.simulation.calculate_button.isEnabled())
        self.assertTrue(all(not button.isEnabled() for button in self.simulation.export_buttons))

    def test_stale_exports_and_old_worker_completion_are_rejected(self):
        from coat_sim.ui.control import UiControlError
        self.simulation.set_design(self.design)
        self.simulation.samples.setValue(31)
        self.simulation.calculate()
        adapter = FilmWindowAdapter("film_simulation", self.simulation, self.simulation, self.root)
        self.simulation.plot_kind.setCurrentIndex(1)
        self.assertFalse(adapter.snapshot()["result_stale"])
        self.simulation.angle.setValue(4)
        self.assertTrue(adapter.snapshot()["result_stale"])
        for action, filename in (("export_csv", "stale.csv"), ("export_png", "stale.png")):
            with self.assertRaises(UiControlError) as caught:
                adapter.action(action, {"path": filename, "overwrite": False})
            self.assertEqual(caught.exception.code, "stale_result")
            self.assertFalse((self.root / filename).exists())
        original = self.service.simulate
        # Parameter changes and replacing a loaded design each invalidate a running job.
        for replace_design in (False, True):
            entered, release = threading.Event(), threading.Event()
            captured = []
            def delayed(design, settings, *, cancel_event=None):
                captured.append((copy.deepcopy(design), copy.deepcopy(settings)))
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("Test worker gate timed out")
                return original(design, settings, cancel_event=cancel_event)
            with mock.patch.object(self.service, "simulate", side_effect=delayed):
                try:
                    state = adapter.action("calculate")
                    self.assertTrue(state["busy"])
                    with self.assertRaises(UiControlError) as caught:
                        adapter.action("calculate")
                    self.assertEqual(caught.exception.code, "busy")
                    self.assertTrue(entered.wait(1))
                    if replace_design:
                        updated = copy.deepcopy(self.design)
                        updated["front_layers"][0]["thickness"]["value"] = 150
                        adapter.update(data=updated)
                    else:
                        adapter.update(fields={"angle_deg": 9})
                finally:
                    release.set()
                self._pump_until(lambda: not self.simulation.busy)
            self.assertIsNone(self.simulation.result)
            self.assertTrue(self.simulation.result_stale)
            self.assertEqual(captured[0][0]["front_layers"][0]["thickness"]["value"], 100)
        adapter.action("calculate")
        self._pump_until(lambda: not self.simulation.busy)
        self.assertFalse(adapter.snapshot()["result_stale"])
        self.assertEqual(self.simulation.current_design()["front_layers"][0]["thickness"]["value"], 150)

    def test_close_while_worker_runs_does_not_touch_closed_widgets(self):
        self.simulation.set_design(self.design)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = self.service.simulate
        def delayed(design, settings, *, cancel_event=None):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test worker gate timed out")
            try:
                return original(design, settings, cancel_event=cancel_event)
            finally:
                finished.set()
        with mock.patch.object(self.service, "simulate", side_effect=delayed), mock.patch("coat_sim.ui.film_design.QMessageBox.warning") as warning:
            try:
                self.simulation.start_calculation()
                self.assertTrue(entered.wait(1))
                self.simulation.close()
                self.assertTrue(self.simulation._closed)
            finally:
                release.set()
            self._pump_until(finished.is_set)
            _APP.processEvents()
            self.assertIsNone(self.simulation.result)
            warning.assert_not_called()



    def test_loading_back_first_surface_preserves_incidence_setting(self):
        design = copy.deepcopy(self.design)
        design["first_surface"] = "back"
        self.simulation.set_design(self.service.validate(design))
        self.assertEqual(self.simulation.first_surface.currentData(), "back")
        self.assertEqual(self.simulation.settings()["first_surface"], "back")

    def test_png_no_clobber_handles_target_created_during_render(self):
        from coat_sim.ui.control import UiControlError
        self.simulation.set_design(self.design)
        self.simulation.samples.setValue(31)
        self.simulation.calculate()
        adapter = FilmWindowAdapter("film_simulation", self.simulation, self.simulation, self.root)
        target = self.root / "concurrent.png"
        original = self.simulation.figure.savefig
        def render_and_create_target(*args, **kwargs):
            original(*args, **kwargs)
            target.write_bytes(b"concurrent writer owns this file")
        with mock.patch.object(self.simulation.figure, "savefig", side_effect=render_and_create_target):
            with self.assertRaises(UiControlError) as caught:
                adapter.action("export_png", {"path": str(target), "overwrite": False})
        self.assertEqual(caught.exception.code, "file_exists")
        self.assertEqual(target.read_bytes(), b"concurrent writer owns this file")
        self.assertEqual(list(self.root.glob("tmp*.png")), [])
        adapter.action("export_png", {"path": str(target), "overwrite": True})
        self.assertTrue(target.read_bytes().startswith(b"\x89PNG"))



    def test_shared_project_chrome_drag_controls_and_close_cancel(self):
        window = self.window
        window.setGeometry(80, 80, 1000, 650)
        window.show()
        _APP.processEvents()
        titlebar = window._titlebar_widget
        chrome = titlebar.rootObject()
        self.assertIsNotNone(chrome)
        self.assertEqual(chrome.property("caption"), window.windowTitle())
        self.assertFalse(chrome.property("lightBackdrop"))
        self.assertEqual(window.windowTitle(), "设计")
        palette = window._titlebar_host.palette
        state = "active" if window._titlebar_host.active else "inactive"
        ink = QColor(chrome.property("ink")).name(QColor.NameFormat.HexArgb)
        theme = titlebar.engine().rootContext().contextProperty("Theme")
        token_ink = QColor(theme.snapshot["colorText"]).name(QColor.NameFormat.HexArgb)
        self.assertEqual(ink.casefold(), token_ink.casefold())
        from PySide6.QtGui import QPalette
        customized = window.palette()
        customized.setColor(QPalette.Active, QPalette.WindowText, QColor("#183b66"))
        customized.setColor(QPalette.Inactive, QPalette.WindowText, QColor("#668899"))
        window.setPalette(customized)
        _APP.processEvents()
        state = "active" if window._titlebar_host.active else "inactive"
        self.assertEqual(window._titlebar_host.palette[state]["windowText"].casefold(),
                         ("#ff183b66" if state == "active" else "#ff668899"))
        self.assertEqual(QColor(chrome.property("ink")).name(QColor.HexArgb).casefold(),
                         token_ink.casefold())
        self.assertTrue(chrome.findChild(QObject, "windowChromeTitleDrag"))
        self.assertTrue(titlebar._in_drag_area(SimpleNamespace(position=lambda: QPointF(120, 18))))

        QTest.mouseDClick(titlebar, Qt.LeftButton, pos=QPoint(120, 18))
        _APP.processEvents()
        self.assertTrue(window.isMaximized())
        QTest.mouseDClick(titlebar, Qt.LeftButton, pos=QPoint(120, 18))
        _APP.processEvents()
        self.assertFalse(window.isMaximized())

        start = window.pos()
        native = window.windowHandle()
        assert native is not None
        original_system_move = native.startSystemMove
        had_instance_override = "startSystemMove" in native.__dict__
        # QTest does not press the OS mouse button required by a native system
        # move loop. Exercise the real geometric fallback deterministically.
        with mock.patch.object(native, "startSystemMove", return_value=False) as system_move:
            QTest.mousePress(titlebar, Qt.LeftButton, pos=QPoint(120, 18))
            QTest.mouseMove(titlebar, QPoint(145, 20), delay=30)
            QTest.mouseMove(titlebar, QPoint(175, 27), delay=30)
            QTest.mouseMove(titlebar, QPoint(195, 27), delay=30)
            QTest.mouseRelease(titlebar, Qt.LeftButton, pos=QPoint(195, 27))
            if QApplication.platformName().lower() not in ("offscreen", "minimal"):
                system_move.assert_called_once_with()
            else:
                system_move.assert_not_called()
        self.assertEqual(native.startSystemMove, original_system_move)
        self.assertEqual("startSystemMove" in native.__dict__, had_instance_override)
        _APP.processEvents()
        self.assertNotEqual(window.pos(), start, "dragging the shared titlebar did not move the film window")

        width = titlebar.width()
        QTest.mouseClick(titlebar, Qt.LeftButton,
                         pos=QPoint(width - 105, 20))
        _APP.processEvents()
        self.assertTrue(window.isMinimized())
        window.showNormal(); _APP.processEvents()

        width = titlebar.width()
        QTest.mouseClick(titlebar, Qt.LeftButton,
                         pos=QPoint(width - 68, 20))
        _APP.processEvents()
        self.assertTrue(window.isMaximized())
        width = titlebar.width()
        QTest.mouseClick(titlebar, Qt.LeftButton,
                         pos=QPoint(width - 68, 20))
        _APP.processEvents()
        self.assertFalse(window.isMaximized())

        dismissed = []
        closed = []
        window.dirty = True
        window.closed.connect(lambda: closed.append(True))
        def click_no_on_close_prompt():
            modal = QApplication.activeModalWidget()
            if isinstance(modal, QMessageBox):
                dismissed.append(True)
                QTest.mouseClick(modal.button(QMessageBox.No), Qt.LeftButton)
        QTimer.singleShot(0, click_no_on_close_prompt)
        width = titlebar.width()
        QTest.mouseClick(titlebar, Qt.LeftButton,
                         pos=QPoint(width - 31, 20))
        _APP.processEvents()
        self.assertTrue(dismissed, "the QML close button did not show the unsaved-changes prompt")
        self.assertTrue(window.isVisible(), "clicking No should cancel window closure")
        self.assertEqual(closed, [])

        window.dirty = False
        width = titlebar.width()
        QTest.mouseClick(titlebar, Qt.LeftButton,
                         pos=QPoint(width - 31, 20))
        _APP.processEvents()
        self.assertFalse(window.isVisible())
        self.assertEqual(closed, [True])

    def test_shared_project_chrome_native_drag_delegates_without_fallback(self):
        window = self.window
        window.setGeometry(80, 80, 1000, 650)
        window.show()
        _APP.processEvents()
        titlebar = window._titlebar_widget
        native = window.windowHandle()
        assert native is not None
        original_system_move = native.startSystemMove
        had_instance_override = "startSystemMove" in native.__dict__
        start = window.pos()
        anchor = titlebar.mapToGlobal(QPoint(120, 18))
        press = SimpleNamespace(
            button=lambda: Qt.MouseButton.LeftButton,
            position=lambda: QPointF(120, 18),
            globalPosition=lambda: QPointF(anchor), accept=mock.Mock())
        moves = [SimpleNamespace(
            globalPosition=lambda delta=delta: QPointF(anchor + delta),
            accept=mock.Mock()) for delta in (QPoint(25, 2), QPoint(75, 9))]
        release = SimpleNamespace(button=lambda: Qt.MouseButton.LeftButton, accept=mock.Mock())
        # Only the Python branch selector is controlled. The real window's
        # geometry/state remain intact; this unit check never enters an OS loop.
        with mock.patch.object(QApplication, "platformName", return_value="native-unit-test"), \
                mock.patch.object(native, "startSystemMove", return_value=True) as system_move, \
                mock.patch.object(window, "move") as fallback_move:
            titlebar.mousePressEvent(press)
            try:
                for event in moves:
                    titlebar.mouseMoveEvent(event)
                    event.accept.assert_called_once_with()
                system_move.assert_called_once_with()
                self.assertTrue(titlebar._drag_started)
                self.assertTrue(titlebar._system_move)
                fallback_move.assert_not_called()
                self.assertEqual(window.pos(), start)
            finally:
                titlebar.mouseReleaseEvent(release)
        self.assertEqual(native.startSystemMove, original_system_move)
        self.assertEqual("startSystemMove" in native.__dict__, had_instance_override)
        press.accept.assert_called_once_with()
        release.accept.assert_called_once_with()
        self.assertIsNone(titlebar._drag_anchor)
        self.assertFalse(titlebar._drag_started)
        self.assertFalse(titlebar._system_move)

    def test_schematic_is_on_demand_reused_and_tracks_edits(self):
        self.assertFalse(hasattr(self.window, "canvas"))
        self.assertIsNone(self.window._schematic_dialog)
        self.window.show()
        _APP.processEvents()

        QTest.mouseClick(self.window.schematic_button, Qt.LeftButton)
        _APP.processEvents()
        dialog = self.window._schematic_dialog
        self.assertIsNotNone(dialog)
        self.assertTrue(dialog.isVisible())

        self.assertEqual(len(self.window._schematic_figure.axes[0].patches), 2)

        self.window.tables["front"].setItem(0, 2, QTableWidgetItem("200"))
        _APP.processEvents()
        labels = [item.get_text() for item in self.window._schematic_figure.axes[0].texts]
        self.assertTrue(any("200 nm" in label for label in labels), labels)

        QTest.keyClick(dialog, Qt.Key_Escape)
        _APP.processEvents()
        self.assertFalse(dialog.isVisible())
        QTest.mouseClick(self.window.schematic_button, Qt.LeftButton)
        _APP.processEvents()
        self.assertIs(self.window._schematic_dialog, dialog)
        self.assertTrue(dialog.isVisible())

    def test_chrome_bridge_uses_current_window_handle_after_flag_changes(self):
        window = self.window
        window.show()
        _APP.processEvents()
        host = window._titlebar_host
        window.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        window.show()
        _APP.processEvents()
        self.assertIs(host.native, window.windowHandle())
        self.assertEqual(host.visibility, window.windowHandle().visibility().value)
        self.assertEqual((host.x, host.y, host.width, host.height),
                         (window.x(), window.y(), window.width(), window.height()))
        window._force_close = True
        window.close()
        _APP.processEvents()
        self.assertIsInstance(host.active, bool)
        self.assertIsInstance(host.visibility, int)

    def test_expression_template_load_replaces_only_active_expression(self):
        design = copy.deepcopy(self.design)
        design["name"] = "Full design to preserve"
        design["note"] = "Design note stays here"
        design["substrate"].update(sides="double", mode="incoherent")
        design["back_layers"] = [{
            "id": "back_existing", "material_id": "test_film",
            "optical_set_id": self.sets["test_film"][0]["id"],
            "thickness": {"value": 75, "unit": "nm"}, "note": "back stays",
        }]
        self.window.set_design(self.service.validate(design))
        self.window.layer_tabs.setCurrentIndex(1)
        self.window._updating = True
        self.window.expression_edit.setPlainText("g1 a\ng2 (0.5 a)^2")
        self.window.symbols_table.setRowCount(0)
        self.window._row(self.window.symbols_table, {
            "symbol": "A", "material_id": "test_film",
            "optical_set_id": self.sets["test_film"][1]["id"],
            "thickness": {"value": 1.25, "unit": "qw"},
            "note": "alias metadata",
        }, True)
        self.window._updating = False
        self.window._expression_changed()

        with mock.patch("coat_sim.ui.film_design.QInputDialog.getText",
                        return_value=("Quarter-wave pair", True)), \
             mock.patch("coat_sim.ui.film_design.QInputDialog.getMultiLineText",
                        return_value=("Template note", True)):
            saved = self.window._save_expression_template()
        self.assertEqual(saved["name"], "Quarter-wave pair")
        self.assertEqual(saved["note"], "Template note")
        self.assertEqual(saved["expression"]["symbols"]["A"]["optical_set_id"],
                         self.sets["test_film"][1]["id"])

        self.window.name_edit.setText("Working design name")
        self.window.note_edit.setText("Working design note")
        self.window.substrate_thickness.setValue(2.0)
        self.window.tables["front"].setItem(0, 2, QTableWidgetItem("140"))
        before_load = self.window.current_design()
        self.window.expression_edit.setPlainText("a")
        rows = self.service.list_expression_templates()
        label = f"{rows[0]['name']} — {rows[0]['note']} [{rows[0]['template_id']}]"
        with mock.patch("coat_sim.ui.film_design.QInputDialog.getItem",
                        return_value=(label, True)):
            loaded = self.window._load_expression_template()

        after_load = self.window.current_design()
        for key in ("name", "note", "substrate", "incident_medium", "exit_medium",
                    "front_layers", "back_layers", "first_surface", "use_k", "assume_lossless"):
            self.assertEqual(after_load[key], before_load[key], key)
        self.assertEqual(self.window.layer_tabs.currentIndex(), 1)
        self.assertEqual(loaded["expression"]["text"], "G1 A\nG2 (0.5 A)^2")
        self.assertEqual(after_load["expression"]["text"], "G1 A\nG2 (0.5 A)^2")
        self.assertFalse(after_load["expression"]["in_sync"])
        self.assertEqual(after_load["expression"]["symbols"]["A"]["material_id"], "test_film")
        self.assertEqual(after_load["expression"]["symbols"]["A"]["optical_set_id"],
                         self.sets["test_film"][1]["id"])

    def test_new_film_metadata_expression_and_library_controls(self):
        self.assertEqual(self.window.property_tabs.tabText(1), "基片与介质")
        self.assertEqual(self.window.incident_medium.current_medium(), {"kind": "constant", "n": 1.0})
        self.assertIn("空气", self.window._medium_label(self.window.incident_medium.current_medium()))
        table = self.window.tables["front"]
        self.assertEqual(table.columnCount(), 7)
        self.assertTrue(table.isColumnHidden(4))
        self.assertNotIn("备注", [table.horizontalHeaderItem(i).text() for i in range(4)])
        self.assertEqual(self.window.layer_tabs.tabText(0), "正面膜堆（1层）")
        self.assertEqual(self.window.current_design()["front_layers"][0]["note"], "first")
        self.window.note_edit.setText("cavity trial")
        self.window.use_k.setChecked(False)
        self.window.add_symbol()
        self.window.expression_toggle.setChecked(True)
        self.window.expression_edit.setPlainText("a\n(a)^2")
        current = self.window.current_design()
        self.assertEqual(current["note"], "cavity trial")
        self.assertFalse(current["use_k"])
        self.assertEqual(current["expression"]["text"], "A\n(A)^2")
        self.assertFalse(self.window.expression_body.isHidden())
        saved = self.service.save_saved_design(current)
        loaded = self.service.load_saved_design(saved["id"])
        self.assertEqual(loaded["note"], "cavity trial")
        self.assertFalse(loaded["use_k"])
        self.assertEqual(loaded["expression"]["text"], "A\n(A)^2")
        self.assertEqual(self.simulation.settings()["step_nm"], 1.0)
        self.assertTrue(self.simulation.simulation_settings_button.isVisible() or not self.simulation.simulation_settings_button.isHidden())
        catalog_root = Path(__file__).resolve().parents[1]
        sources = FilmSimulationWindow._load_cie_light_sources(catalog_root)
        self.assertEqual({row["id"] for row in sources}, {"A", "D50", "D65", "E"})
        d65 = next(row["source"] for row in sources if row["id"] == "D65")
        self.assertEqual(d65["wavelength_basis"], "standard_air")
        self.assertEqual(d65["data_license"], "CC-BY-SA-4.0")



if __name__ == "__main__":
    unittest.main()
