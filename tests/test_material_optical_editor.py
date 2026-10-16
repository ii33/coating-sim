"""Transactional end-to-end coverage for complete material optical-set editing."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")

from pathlib import Path
import json
from copy import deepcopy
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import Q_ARG, QCoreApplication, QEvent, QMetaObject, QObject, QPoint, QPointF, Property, Qt, QUrl, Signal
from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication, QWheelEvent
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlEngine, QQmlExpression
from PySide6.QtQuick import QQuickWindow
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from coat_sim.core.api import CoatService
from coat_sim.core import materials
from coat_sim.core.materials import read_material_profile
from coat_sim.ui.fonts import bundled_font_family
from coat_sim.ui.source_substrate import SourceBridge
from coat_sim.ui.theme_service import ThemeService
from coat_sim.ui.theme_tokens import build_theme_snapshot
from tests.material_test_helpers import MaterialQtCase
from tests.qt_test_guard import NoNetworkAccessManagerFactory, qml_item_window

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parents[1]
REAL_BUILTIN_PROFILES = {profile["id"]: profile for profile in materials.builtin_material_profiles()}


def _send_wheel_event(window, target, *, pixel_delta=0, angle_delta=0, local_pos=None):
    target_window = qml_item_window(target) or window
    center = local_pos or QPointF(min(float(target.property("width")) / 2, 100.0),
                                  float(target.property("height")) / 2)
    global_pos = target.mapToGlobal(center)
    rounded = target_window.mapFromGlobal(QPoint(round(global_pos.x()), round(global_pos.y())))
    local = QPointF(rounded)
    QTest.mouseMove(target_window, rounded, 5)
    event = QWheelEvent(
        local,
        QPointF(global_pos),
        QPoint(0, pixel_delta),
        QPoint(0, angle_delta),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    event.ignore()
    QCoreApplication.sendEvent(target_window, event)
    APP.processEvents()
    return event.isAccepted()


def _find_visual_item(root, object_name):
    pending = [root]
    while pending:
        item = pending.pop()
        if item.objectName() == object_name:
            return item
        try:
            pending.extend(item.childItems())
        except AttributeError:
            pass
    return None


def _to_variant(value):
    return value.toVariant() if hasattr(value, "toVariant") else value


def _close_qml_test_window(engine, window, dialog=None):
    try:
        if dialog is not None and dialog.property("visible"):
            QMetaObject.invokeMethod(dialog, "close")
        if window is not None:
            window.close()
        QTest.qWait(30)
    finally:
        if window is not None:
            window.deleteLater()
        if engine is not None:
            engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()



class _OpticalEditorTheme(QObject):
    snapshotChanged = Signal()

    @Property("QVariantMap", notify=snapshotChanged)
    def snapshot(self):
        return build_theme_snapshot("dark")


class MaterialOpticalEditorTests(MaterialQtCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, value in (("PROJECT_ROOT", self.root), ("DATA_ROOT", self.root / "data")):
            patcher = patch("coat_sim.core.data_files." + name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.service = CoatService(root=self.root, restrict_to_root=False)
        self.source = self.make_source(self.service)
        self.lib = self.source.materialLibrary

    def open_material_library(self, window, engine):
        self.lib.prepareForSource()
        self.assertTrue(QMetaObject.invokeMethod(window, "openMaterialLibrary"),
                        "SourceWindow.openMaterialLibrary was not invokable")
        deadline = time.monotonic() + 8.0
        controller = engine.incubationController()
        dialog = None
        while time.monotonic() < deadline:
            QCoreApplication.processEvents()
            if controller is not None:
                # Offscreen Qt Quick may have no render ticks to advance an
                # asynchronous QML incubator. Give the engine a small,
                # bounded slice while keeping the real 8-second open deadline.
                controller.incubateFor(2)
            dialog = window.findChild(QObject, "materialLibraryDialog")
            if dialog is not None and dialog.property("visible"):
                return dialog
            QTest.qWait(10)
        incubator_expression = QQmlExpression(
            QQmlEngine.contextForObject(window), window,
            "materialLibraryDialogIncubator ? materialLibraryDialogIncubator.status : -1",
        )
        status_value, status_undefined = incubator_expression.evaluate()
        incubator_status = ("undefined" if status_undefined else _to_variant(status_value))
        if incubator_expression.hasError():
            incubator_status = "error: " + incubator_expression.error().toString()
        instance = window.property("materialLibraryDialogInstance")
        self.fail(
            "SourceWindow did not open its lazy material library within 8 seconds; "
            f"opening={window.property('materialLibraryOpening')}, "
            f"requested={window.property('materialLibraryOpenRequested')}, "
            f"incubatorStatus={incubator_status}, controller={controller is not None}, "
            f"instanceCreated={instance is not None}, dialog={dialog!r}, "
            f"visible={dialog.property('visible') if dialog is not None else None}"
        )

    def find_optical_card(self, window, dialog, identity):
        object_name = "opticalDatasetCard_" + identity
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            card = window.findChild(QObject, object_name)
            if card is not None:
                return card
            # Popup delegates are QQuickItems and may belong to the popup's
            # visual tree without being QObject children of SourceWindow.
            roots = [window.contentItem()]
            popup_content = dialog.property("contentItem") if dialog is not None else None
            if popup_content is not None:
                roots.append(popup_content)
            # Qt may also expose native QWindows (for example platform
            # helpers) in allWindows(). Only Quick windows own contentItems.
            roots.extend(surface.contentItem() for surface in QGuiApplication.allWindows()
                         if surface is not window and isinstance(surface, QQuickWindow))
            for root_item in roots:
                if root_item is None:
                    continue
                card = _find_visual_item(root_item, object_name)
                if card is not None:
                    return card
            QCoreApplication.processEvents()
            QTest.qWait(10)
        return None

    @staticmethod
    def synthetic_optical_only_profile(identity):
        return {
            "schema_version": 1,
            "id": identity,
            "name": "Synthetic optical-only " + identity,
            "description": "Synthetic test fixture; no external material data.",
            "notes": "Two deliberately distinct optical datasets for UI tests.",
            "optical_only": True,
            "resistance_model": None,
            "electron_beam_model": None,
            # The material-library dialog defaults to the film role filter.
            # Optical-only records still have a film role; the separate
            # optical_only flag keeps them out of the source-bindable list.
            "roles": ["film"],
            "optical_constants": [
                {
                    "id": identity + "-table-nm",
                    "name": "Synthetic visible n/k (nm)",
                    "notes": "Synthetic 400-700 nm data.",
                    "source": "Synthetic test fixture",
                    "kind": "tabulated",
                    "wavelength_unit": "nm",
                    "wavelength_min": 400.0,
                    "wavelength_max": 700.0,
                    "process_parameters": [
                        {"name": "Substrate temperature", "value": "250", "unit": "°C"},
                        {"name": "Ion assistance", "value": "Off", "unit": ""},
                    ],
                    "wavelengths": [400.0, 550.0, 700.0],
                    "n": [1.50, 1.52, 1.54],
                    "k": [0.010, 0.015, 0.020],
                },
                {
                    "id": identity + "-table-um",
                    "name": "Synthetic infrared n/k (um)",
                    "notes": "Synthetic 0.45-0.75 um data.",
                    "source": "Synthetic test fixture",
                    "kind": "tabulated",
                    "wavelength_unit": "um",
                    "wavelength_min": 0.45,
                    "wavelength_max": 0.75,
                    "process_parameters": [
                        {"name": "Deposition rate", "value": "1.2", "unit": "Å/s"},
                        {"name": "Chamber pressure", "value": "3e-4", "unit": "Pa"},
                    ],
                    "wavelengths": [0.45, 0.60, 0.75],
                    "n": [1.60, 1.62, 1.65],
                    "k": [0.030, 0.020, 0.010],
                },
            ],
        }

    def write_synthetic_optical_only_profile(self, identity):
        profile = self.synthetic_optical_only_profile(identity)
        user_folder = self.root / "data" / "materials"
        user_folder.mkdir(parents=True, exist_ok=True)
        path = user_folder / (identity + ".json")
        path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
        return profile, path

    @staticmethod
    def cauchy_form(identity="", *, name="Cauchy / independent k", a="1.5", b="0.01",
                    k_table="0.4\t0.01\n0.6\t0.02\n0.8\t0", notes=None):
        return {
            "id": identity,
            "name": name,
            "notes": notes if notes is not None else "参考条件：镀膜方法：电子束蒸发；基片温度：200 °C；本底压力：未提供；反应气流量：未提供。",
            "source": "测试来源：local-source.txt",
            "kind": "cauchy",
            "wavelength_unit": "um",
            "wavelength_min": "0.4",
            "wavelength_max": "0.7",
            "parameters": {"A": a, "B": b, "C": "0"},
            "has_independent_k": True,
            "k_table": k_table,
            "k_wavelength_unit": "um",
            "k_wavelength_min": "0.4",
            "k_wavelength_max": "0.8",
            "k_notes": "独立吸收表备注",
        }

    def create_user_material(self):
        self.assertTrue(self.lib.newMaterial())
        self.lib.setField("name", "完整光学编辑测试")
        self.lib.setField("notes", "材料备注第一行\n材料备注第二行")
        table = {
            "name": "离散 n/k",
            "notes": "离散光学数据集的简短介绍。",
            "process_parameters": [
                {"name": "工艺类型", "value": "热蒸发", "unit": ""},
                {"name": "基片温度", "value": "室温", "unit": ""},
                {"name": "工作气体流量", "value": "", "unit": "sccm"},
            ],
            "source": "local-table.txt",
            "kind": "tabulated",
            "wavelength_unit": "nm",
            "wavelength_min": "400",
            "wavelength_max": "700",
            "table": "400\t1.50\t0.01\n550\t1.51\t0\n700\t1.52\t0.02",
        }
        self.assertTrue(self.lib.addOpticalSet(json.dumps(table, ensure_ascii=False)), self.lib.status)
        discrete_id = self.lib.opticalSets[0]["id"]
        self.assertTrue(self.lib.addOpticalSet(json.dumps(self.cauchy_form(), ensure_ascii=False)), self.lib.status)
        cauchy_id = next(row["id"] for row in self.lib.opticalSets if row["kind"] == "cauchy")
        self.assertTrue(self.lib.save(), self.lib.status)
        material_id = self.lib.currentId
        self.settle_materials()
        return material_id, discrete_id, cauchy_id

    def select_saved_material_again(self, material_id):
        builtins = [row for row in self.lib.entries if row["builtin"]]
        self.assertTrue(builtins)
        self.assertTrue(self.lib.selectMaterial("builtin:" + builtins[0]["id"]))
        self.settle_materials()
        self.assertTrue(self.lib.selectMaterial("user:" + material_id))
        self.settle_materials()

    def test_create_edit_save_reload_design_snapshot_and_atomic_rejection(self):
        material_id, discrete_id, cauchy_id = self.create_user_material()
        path = self.root / "data" / "materials" / (material_id + ".json")
        initial = read_material_profile(path)
        self.assertEqual(initial["notes"], "材料备注第一行\n材料备注第二行")
        self.assertEqual(initial["optical_constants"][0]["id"], discrete_id)
        self.assertEqual(initial["optical_constants"][1]["id"], cauchy_id)

        self.select_saved_material_again(material_id)
        discrete = self.lib.opticalSetDetails(discrete_id)
        new_table = "400\t1.49\t0.03\n500\t1.505\t0\n700\t1.53\t0.04"
        table_form = {"id": discrete_id, "name": "离散 n/k 已编辑",
                      "notes": "手动沉积薄膜数据集的简短介绍。",
                      "process_parameters": [
                          {"name": "工艺类型", "value": "手动", "unit": ""},
                          {"name": "基片温度", "value": "250", "unit": "°C"},
                          {"name": "真空/压力", "value": "未提供", "unit": ""},
                          {"name": "工作气体流量", "value": "未提供", "unit": "sccm"},
                      ],
                      "source": discrete["source"], "kind": "tabulated", "wavelength_unit": "nm",
                      "wavelength_min": "450", "wavelength_max": "650", "table": new_table}
        self.assertTrue(self.lib.updateOpticalSet(discrete_id, json.dumps(table_form, ensure_ascii=False)), self.lib.status)
        self.assertEqual(self.lib.opticalSetDetails(discrete_id)["id"], discrete_id)
        self.assertEqual(self.lib.opticalSetDetails(discrete_id)["wavelength_min"], 450)

        cauchy = self.lib.opticalSetDetails(cauchy_id)
        formula_form = self.cauchy_form(cauchy_id, name="Cauchy 参数已编辑", a="1.6", b="0.02",
            k_table="0.4\t0.015\n0.6\t0\n0.8\t0.005", notes="温度记录\n真空记录")
        formula_form["source"] = cauchy["source"]
        formula_form["k_notes"] = cauchy["k_data"]["notes"]
        self.assertTrue(self.lib.updateOpticalSet(cauchy_id, json.dumps(formula_form, ensure_ascii=False)), self.lib.status)
        self.lib.setField("notes", "材料备注第一行\n材料备注第二行")
        self.assertTrue(self.lib.save(), self.lib.status)
        self.settle_materials()

        saved = read_material_profile(path)
        self.assertEqual(saved["notes"], "材料备注第一行\n材料备注第二行")
        saved_table, saved_formula = saved["optical_constants"]
        self.assertEqual(saved_table["id"], discrete_id)
        self.assertEqual(saved_table["name"], "离散 n/k 已编辑")
        self.assertEqual(saved_table["wavelengths"], [400, 500, 700])
        self.assertEqual(saved_table["n"], [1.49, 1.505, 1.53])
        self.assertEqual(saved_table["k"], [0.03, 0, 0.04])
        self.assertEqual(saved_table["wavelength_min"], 450)
        self.assertEqual(saved_table["wavelength_max"], 650)
        self.assertEqual(saved_table["process_parameters"], table_form["process_parameters"])
        self.assertEqual(saved_formula["id"], cauchy_id)
        self.assertEqual(saved_formula["parameters"]["A"], 1.6)
        self.assertEqual(saved_formula["k_data"]["k"], [0.015, 0, 0.005])
        self.assertEqual(saved_formula["k_data"]["notes"], "独立吸收表备注")

        self.select_saved_material_again(material_id)
        self.assertEqual(self.lib.opticalSetDetails(cauchy_id)["parameters"]["B"], 0.02)
        sections = json.dumps(self.lib.referenceSections, ensure_ascii=False)
        self.assertIn("local-table.txt", sections)
        self.assertIn("local-source.txt", sections)
        discrete_summary = next(row for row in self.lib.opticalSets if row["id"] == discrete_id)
        condition_values = {row["label"]: row["value"] for row in discrete_summary["conditions"]}
        self.assertEqual(condition_values["工艺类型"], "手动")
        self.assertEqual(condition_values["基片温度"], "250")
        self.assertEqual(condition_values["真空/压力"], "未提供")
        self.assertEqual(condition_values["工作气体流量"], "未提供")
        self.assertIn("域内129点采样估计", next(row["summary"] for row in self.lib.opticalSets if row["id"] == cauchy_id))

        before_file = path.read_bytes()
        before_draft = self.lib.opticalSetDetails(cauchy_id)
        invalid = self.cauchy_form(cauchy_id, a="not-a-number")
        invalid["source"] = before_draft["source"]
        self.assertFalse(self.lib.updateOpticalSet(cauchy_id, json.dumps(invalid, ensure_ascii=False)))
        self.assertEqual(self.lib.opticalSetDetails(cauchy_id), before_draft)
        self.assertEqual(path.read_bytes(), before_file)

        self.assertTrue(self.lib.useMaterial())
        snapshot = self.source.documentData()
        self.assertIn(discrete_id, json.dumps(snapshot, ensure_ascii=False))
        changed_again = dict(table_form, name="Library only", table="400\t1.6\t0.1\n500\t1.7\t0.2\n700\t1.8\t0.3")
        self.assertTrue(self.lib.updateOpticalSet(discrete_id, json.dumps(changed_again, ensure_ascii=False)))
        self.assertTrue(self.lib.save(), self.lib.status)
        self.settle_materials()
        self.assertEqual(self.source.documentData(), snapshot)

    def test_grid_payload_keeps_unknown_k_independent_k_and_cell_errors(self):
        material_id, discrete_id, cauchy_id = self.create_user_material()
        self.select_saved_material_again(material_id)
        previous = self.lib.opticalSetDetails(discrete_id)
        form = {
            "id": discrete_id, "name": previous["name"], "notes": previous["notes"],
            "source": previous["source"], "kind": "tabulated", "wavelength_unit": "nm",
            "wavelength_min": "400", "wavelength_max": "700",
            "table_rows": [
                {"wavelength": "400", "n": "1.49", "k": ""},
                {"wavelength": "500", "n": "1.51", "k": ""},
                {"wavelength": "700", "n": "1.53", "k": ""},
            ],
        }
        self.assertTrue(self.lib.updateOpticalSet(discrete_id, json.dumps(form, ensure_ascii=False)), self.lib.status)
        unknown = self.lib.opticalSetDetails(discrete_id)
        self.assertEqual(unknown["id"], discrete_id)
        self.assertIsNone(unknown["k"])
        self.assertEqual(unknown["wavelengths"], [400.0, 500.0, 700.0])

        for rows, expected in (
            ([{"wavelength": "400", "n": "1.49", "k": ""},
              {"wavelength": "400", "n": "1.51", "k": ""}], "第 2 行波长重复"),
            ([{"wavelength": "wave?", "n": "1.49", "k": ""},
              {"wavelength": "500", "n": "1.51", "k": ""}], "第 1 行 波长 单元格不是有效数字"),
            ([{"wavelength": "400", "n": "1.49", "k": ""},
              {"wavelength": "500", "n": "", "k": ""}], "第 2 行 n 单元格为空"),
            ([{"wavelength": "400", "n": "1.49", "k": "0.01"},
              {"wavelength": "500", "n": "1.51", "k": ""}], "第 2 行 k 单元格为空"),
        ):
            invalid = dict(form, table_rows=rows)
            self.assertFalse(self.lib.updateOpticalSet(discrete_id, json.dumps(invalid, ensure_ascii=False)))
            self.assertIn(expected, self.lib.status)
            self.assertEqual(self.lib.opticalSetDetails(discrete_id)["wavelengths"], [400.0, 500.0, 700.0])
            self.assertIsNone(self.lib.opticalSetDetails(discrete_id)["k"])

        formula = self.lib.opticalSetDetails(cauchy_id)
        k_form = {
            "id": cauchy_id, "name": formula["name"], "notes": formula["notes"], "source": formula["source"],
            "kind": formula["kind"], "wavelength_unit": formula["wavelength_unit"],
            "wavelength_min": str(formula["wavelength_min"]), "wavelength_max": str(formula["wavelength_max"]),
            "parameters": formula["parameters"], "has_independent_k": True,
            "k_wavelength_unit": "um", "k_wavelength_min": "0.45", "k_wavelength_max": "0.75",
            "k_rows": [
                {"wavelength": "0.45", "k": "0.015"},
                {"wavelength": "0.65", "k": "0.025"},
                {"wavelength": "0.75", "k": "0.005"},
            ],
        }
        self.assertTrue(self.lib.updateOpticalSet(cauchy_id, json.dumps(k_form, ensure_ascii=False)), self.lib.status)
        updated_formula = self.lib.opticalSetDetails(cauchy_id)
        self.assertEqual(updated_formula["id"], cauchy_id)
        self.assertEqual(updated_formula["k_data"]["wavelengths"], [0.45, 0.65, 0.75])
        self.assertEqual(updated_formula["k_data"]["wavelength_unit"], "um")

    def test_utf8_csv_tsv_whitespace_import_and_core_model_catalog(self):
        self.assertTrue(self.lib.newMaterial())
        self.lib.setField("name", "TXT 导入验证")
        path = self.root / "光学样本.txt"
        path.write_text("wavelength,n,k\n400,1.5,0\n550,1.51,0.01\n700,1.52,0.02\n", encoding="utf-8")
        preview = json.loads(self.lib.previewOpticalFile(QUrl.fromLocalFile(str(path)).toString(), "测试数据", "nm"))
        self.assertEqual(preview["kind"], "tabulated")
        self.assertEqual(preview["source"], path.name)
        self.assertEqual(preview["wavelengths_text"].splitlines()[0].split("\t"), ["400", "1.5", "0"])
        form = {"name": preview["name"], "notes": "UTF-8 样品", "source": preview["source"],
                "kind": "tabulated", "wavelength_unit": "nm", "wavelength_min": preview["wavelength_min"],
                "wavelength_max": preview["wavelength_max"], "table": preview["wavelengths_text"]}
        self.assertTrue(self.lib.addOpticalSet(json.dumps(form, ensure_ascii=False)), self.lib.status)
        summary = self.lib.opticalSets[0]["summary"]
        self.assertIn("波段 400–700 nm", summary)
        self.assertIn("k=0–0.02", summary)
        catalog = self.lib.opticalModelCatalog
        self.assertEqual(len([row for row in catalog if row["kind"] == "rii_formula"]), 9)
        self.assertTrue(all(row["coefficient_names"] for row in catalog if row["kind"] == "rii_formula"))
        for model in (row for row in catalog if row["kind"] != "tabulated"):
            formula_url = self.lib.opticalFormulaImage(model["kind"], model.get("formula_number", 0))
            self.assertTrue(formula_url, model["label"])
            self.assertTrue(Path(QUrl(formula_url).toLocalFile()).is_file(), model["label"])

        bad = self.root / "bad.csv"
        bad.write_text("400,1.5\ninvalid,1.6\n700,1.7\n", encoding="utf-8")
        failed = json.loads(self.lib.previewOpticalFile(QUrl.fromLocalFile(str(bad)).toString(), "bad", "nm"))
        self.assertIn("第 2 行", failed["error"])
        self.assertEqual(len(self.lib.opticalSets), 1)

        bulk = dict(form, name="明确标注块材", notes="样品形态：块材")
        self.assertTrue(self.lib.addOpticalSet(json.dumps(bulk, ensure_ascii=False)), self.lib.status)
        bulk_id = next(row["id"] for row in self.lib.opticalSets if row["name"] == bulk["name"])
        bulk_conditions = {row["label"]: row["value"] for row in self.lib.opticalSetDetails(bulk_id)["conditions"]}
        self.assertEqual(bulk_conditions, {})
        unknown = dict(form, name="来源未提供工艺条件", notes="")
        self.assertTrue(self.lib.addOpticalSet(json.dumps(unknown, ensure_ascii=False)), self.lib.status)
        unknown_id = next(row["id"] for row in self.lib.opticalSets if row["name"] == unknown["name"])
        unknown_conditions = {row["label"]: row["value"] for row in self.lib.opticalSetDetails(unknown_id)["conditions"]}
        self.assertEqual(unknown_conditions, {})

    def test_synthetic_optical_only_conditions_show_only_recorded_values(self):
        identity = "condition-check-optical"
        profile, _path = self.write_synthetic_optical_only_profile(identity)
        self.lib.reload()
        self.settle_materials()
        self.assertTrue(self.lib.selectMaterial("user:" + identity))
        self.settle_materials()

        self.assertTrue(self.lib.opticalOnly)
        self.assertIsNone(self.lib.draftData()["resistance_model"])
        self.assertIsNone(self.lib.draftData()["electron_beam_model"])
        self.assertEqual(len(self.lib.opticalSets), 2)
        for record in profile["optical_constants"]:
            conditions = self.lib.opticalSetDetails(record["id"])["conditions"]
            expected = record["process_parameters"]
            self.assertEqual(
                [(row["name"], row["value"], row["unit"]) for row in conditions],
                [(row["name"], row["value"], row["unit"]) for row in expected],
            )

    def test_real_optical_only_records_stay_visible_unbound_and_null_models_survive_edit(self):
        identities = ("synthetic-optical-visible", "synthetic-optical-save")
        profiles = {}
        user_folder = self.root / "data" / "materials"
        user_folder.mkdir(parents=True, exist_ok=True)
        for identity in identities:
            profile = self.synthetic_optical_only_profile(identity)
            self.assertTrue(profile["optical_only"])
            self.assertIsNone(profile["resistance_model"])
            self.assertIsNone(profile["electron_beam_model"])
            profiles[identity] = profile
            (user_folder / (identity + ".json")).write_text(
                json.dumps(profile, ensure_ascii=False), encoding="utf-8")
        self.lib.reload()
        self.settle_materials()

        for identity, profile in profiles.items():
            key = "user:" + identity
            entry = next(row for row in self.lib.entries if row["key"] == key)
            self.assertTrue(entry["opticalOnly"])
            self.assertFalse(entry["sourceBindable"])
            self.assertIn(key, {row["key"] for row in self.lib.filteredEntries})
            self.assertNotIn(key, {row["key"] for row in self.lib.filmEntries})
            self.assertTrue(self.lib.selectMaterial(key))
            self.settle_materials()
            self.assertTrue(self.lib.opticalOnly)
            self.assertFalse(self.lib.sourceBindable)
            self.assertFalse(self.lib.canUseMaterial)
            self.assertFalse(self.lib.useMaterial())
            self.assertEqual(self.lib.draftData()["resistance_model"], None)
            self.assertEqual(self.lib.draftData()["electron_beam_model"], None)
            self.assertEqual(len(self.lib.opticalSets), 2)
            for optical_set in profile["optical_constants"]:
                details = self.lib.opticalSetDetails(optical_set["id"])
                self.assertEqual(
                    [(row["name"], row["value"], row["unit"]) for row in details["conditions"]],
                    [(row["name"], row["value"], row["unit"])
                     for row in optical_set["process_parameters"]],
                )

        identity = identities[0]
        self.assertTrue(self.lib.selectMaterial("user:" + identity))
        self.settle_materials()
        first_set = self.lib.opticalSets[0]
        original_details = self.lib.opticalSetDetails(first_set["id"])
        second_set_before = self.lib.opticalSetDetails(self.lib.opticalSets[1]["id"])
        updated_name = original_details["name"] + " / UI edit"
        self.assertTrue(self.lib.editOpticalMetadata(first_set["id"], updated_name, original_details["notes"]))
        self.assertTrue(self.lib.save(), self.lib.status)
        self.settle_materials()
        saved = read_material_profile(user_folder / (identity + ".json"))
        self.assertTrue(saved["optical_only"])
        self.assertIsNone(saved["resistance_model"])
        self.assertIsNone(saved["electron_beam_model"])
        self.assertEqual(len(saved["optical_constants"]), 2)
        self.assertEqual(saved["optical_constants"][0]["id"], first_set["id"])
        self.assertEqual(saved["optical_constants"][0]["name"], updated_name)
        self.assertEqual(saved["optical_constants"][1]["process_parameters"],
                         profiles[identity]["optical_constants"][1]["process_parameters"])

        font_file = Path("C:/Windows/Fonts/Noto Sans SC (TrueType).otf")
        font_id = QFontDatabase.addApplicationFont(str(font_file)) if font_file.is_file() else -1
        loaded_families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
        families = {name.casefold(): name for name in QFontDatabase.families()}
        cjk_family = next((families[name.casefold()] for name in
                           ("Arial Unicode MS", "Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans SC", "DengXian", "SimHei")
                           if name.casefold() in families), loaded_families[0] if loaded_families else None)
        if cjk_family:
            QGuiApplication.setFont(QFont(cjk_family))
        if os.environ.get("COAT_REQUIRE_NATIVE_FONT_SCREENSHOT"):
            self.assertEqual(QGuiApplication.platformName().casefold(), "windows")
            self.assertEqual(cjk_family, "Arial Unicode MS")
        engine = QQmlApplicationEngine()
        warnings = []
        engine.warnings.connect(lambda messages: warnings.extend(message.toString() for message in messages))
        engine.rootContext().setContextProperty("bridge", self.source)
        engine.load(QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/qml/SourceWindow.qml")))
        self.assertTrue(engine.rootObjects(), warnings)
        window = engine.rootObjects()[0]
        dialog = None
        try:
            window.showNormal()
            window.resize(1180, 860)
            dialog = self.open_material_library(window, engine)
            QTest.qWait(80)
            self.assertTrue(self.lib.selectMaterial("user:" + identity))
            self.settle_materials()
            notice = window.findChild(QObject, "materialOpticalOnlyNotice")
            self.assertTrue(notice.property("visible"))
            tabs = window.findChild(QObject, "materialContentTabs")
            self.assertEqual(tabs.property("currentIndex"), 0)
            use_button = window.findChild(QObject, "useMaterialButton")
            self.assertFalse(use_button.property("enabled"))
            resistance_tab = window.findChild(QObject, "resistanceModelTab")
            electron_beam_tab = window.findChild(QObject, "electronBeamModelTab")
            self.assertIsNotNone(resistance_tab)
            self.assertIsNotNone(electron_beam_tab)
            self.assertFalse(resistance_tab.property("enabled"))
            self.assertFalse(electron_beam_tab.property("enabled"))
            for optical_set in profiles[identity]["optical_constants"]:
                card = self.find_optical_card(window, dialog, optical_set["id"])
                self.assertIsNotNone(card, f"missing synthetic optical card {optical_set['id']}")
                self.assertTrue(card.property("visible"), f"optical card {optical_set['id']} is hidden")
                self.assertGreater(float(card.property("width")), 0.0)
                self.assertGreater(float(card.property("height")), 0.0)
                header = _find_visual_item(card, "opticalSetDisclosureHeader")
                self.assertIsNotNone(header)
                self.assertTrue(QMetaObject.invokeMethod(header, "click"))
                loader = _find_visual_item(card, "opticalSetExpandedDetailsLoader")
                self.assertIsNotNone(loader)
                deadline = time.monotonic() + 4.0
                while time.monotonic() < deadline:
                    QCoreApplication.processEvents()
                    if (loader.property("active") and float(loader.property("height")) > 0
                            and len(_to_variant(card.property("curvePoints"))) >= 2):
                        break
                    QTest.qWait(10)
                self.assertTrue(loader.property("active"))
                self.assertGreater(float(loader.property("height")), 0)
                self.assertGreaterEqual(len(_to_variant(card.property("curvePoints"))), 2)
                for index, condition in enumerate(optical_set["process_parameters"]):
                    name_label = _find_visual_item(card, f"opticalSetProcessCellName_{index}")
                    value_label = _find_visual_item(card, f"opticalSetProcessCellValue_{index}")
                    self.assertIsNotNone(name_label, f"condition {index} was not rendered")
                    self.assertIsNotNone(value_label, f"condition value {index} was not rendered")
                    self.assertTrue(name_label.property("visible"))
                    self.assertTrue(value_label.property("visible"))
                    self.assertEqual(name_label.property("text"), condition["name"])
                    expected_value = " ".join(value for value in (condition["value"], condition["unit"]) if value)
                    self.assertEqual(value_label.property("text"), expected_value)
            screenshot_dir = os.environ.get("COAT_PREVIEW_SCREENSHOT_DIR")
            if screenshot_dir:
                directory = Path(screenshot_dir)
                directory.mkdir(parents=True, exist_ok=True)
                image = QQuickWindow.grabWindow(window)
                self.assertFalse(image.isNull())
                self.assertTrue(image.save(str(directory / "material-optical-only-cn.png")))
        finally:
            _close_qml_test_window(engine, window, dialog)

    def test_real_qml_material_editor_and_condition_details_render(self):
        material_id, _discrete_id, cauchy_id = self.create_user_material()
        self.select_saved_material_again(material_id)
        font_file = Path("C:/Windows/Fonts/Noto Sans SC (TrueType).otf")
        font_id = QFontDatabase.addApplicationFont(str(font_file)) if font_file.is_file() else -1
        loaded_families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
        families = {name.casefold(): name for name in QFontDatabase.families()}
        cjk_family = next((families[name.casefold()] for name in
                           ("Arial Unicode MS", "Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans SC", "DengXian", "SimHei")
                           if name.casefold() in families), loaded_families[0] if loaded_families else None)
        if cjk_family:
            QGuiApplication.setFont(QFont(cjk_family))
        if os.environ.get("COAT_REQUIRE_NATIVE_FONT_SCREENSHOT"):
            self.assertEqual(QGuiApplication.platformName().casefold(), "windows")
            self.assertEqual(cjk_family, "Arial Unicode MS")
        engine = QQmlApplicationEngine()
        warnings = []
        engine.warnings.connect(lambda messages: warnings.extend(message.toString() for message in messages))
        engine.rootContext().setContextProperty("bridge", self.source)
        theme = ThemeService.instance()
        theme.set_theme("light")
        theme.attach_engine(engine, "Theme")
        engine.load(QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/qml/SourceWindow.qml")))
        self.assertTrue(engine.rootObjects(), warnings)
        window = engine.rootObjects()[0]
        try:
            window.showNormal()
            window.resize(1180, 860)
            dialog = self.open_material_library(window, engine)
            QTest.qWait(100)
            self.assertTrue(dialog.property("visible"))
            self.assertTrue(self.lib.hasReferenceData)
            tabs = window.findChild(QObject, "materialContentTabs")
            self.assertIsNotNone(tabs)
            tabs.setProperty("currentIndex", 0)
            QTest.qWait(50)
            repeater = window.findChild(QObject, "opticalSetsRepeater")
            self.assertIsNotNone(repeater, warnings)
            self.assertEqual(repeater.property("count"), 2)
            scroll = window.findChild(QObject, "materialResearchScroll")
            self.assertIsNotNone(scroll)
            flickable = next((child for child in scroll.findChildren(QObject)
                              if child.metaObject().className() == "QQuickFlickable"), None)
            self.assertIsNotNone(flickable)
            screenshot_dir = os.environ.get("COAT_PREVIEW_SCREENSHOT_DIR")
            if screenshot_dir:
                directory = Path(screenshot_dir)
                directory.mkdir(parents=True, exist_ok=True)
                image = QQuickWindow.grabWindow(window)
                self.assertFalse(image.isNull())
                self.assertTrue(image.save(str(directory / "material-library-overview-cn.png")))
            max_content_y = max(0, float(flickable.property("contentHeight")) - float(flickable.property("height")))
            flickable.setProperty("contentY", max_content_y)
            QTest.qWait(60)
            if screenshot_dir:
                image = QQuickWindow.grabWindow(window)
                self.assertFalse(image.isNull())
                self.assertTrue(image.save(str(directory / "material-library-datasets-cn.png")))
            card = self.find_optical_card(window, dialog, _discrete_id)
            self.assertIsNotNone(card, "first optical Repeater delegate was not created")
            self.assertTrue(card.property("visible"))
            self.assertGreater(float(card.property("width")), 0.0)
            self.assertGreater(float(card.property("height")), 0.0)
            card_window = qml_item_window(card)
            card_point = card.mapToItem(card_window.contentItem(), QPointF(float(card.property("width")) / 2, 16))
            QTest.mouseClick(card_window, Qt.LeftButton, Qt.NoModifier,
                             QPoint(round(card_point.x()), round(card_point.y())))
            curve_deadline = time.monotonic() + 4.0
            while time.monotonic() < curve_deadline:
                QCoreApplication.processEvents()
                if len(_to_variant(card.property("curvePoints"))) > 2:
                    break
                QTest.qWait(10)
            self.assertGreater(len(_to_variant(card.property("curvePoints"))), 2)
            details_loader = _find_visual_item(card, "opticalSetExpandedDetailsLoader")
            self.assertIsNotNone(details_loader)
            self.assertTrue(details_loader.property("active"))
            self.assertGreater(float(details_loader.property("height")), 0)
            self.assertIn(_discrete_id, _to_variant(dialog.property("expandedOpticalSetIds")))
            self.assertTrue(any(row["label"] == "基片温度" for row in self.lib.opticalSetDetails(_discrete_id)["conditions"]))
            arrow = _find_visual_item(card, "opticalSetDisclosureArrow")
            self.assertIsNotNone(arrow)
            arrow_point = arrow.mapToItem(card_window.contentItem(), QPointF(float(arrow.property("width")) / 2,
                                                                              float(arrow.property("height")) / 2))
            QTest.mouseClick(card_window, Qt.LeftButton, Qt.NoModifier,
                             QPoint(round(arrow_point.x()), round(arrow_point.y())))
            QTest.qWait(40)
            self.assertNotIn(_discrete_id, _to_variant(dialog.property("expandedOpticalSetIds")))
            QTest.mouseClick(card_window, Qt.LeftButton, Qt.NoModifier,
                             QPoint(round(card_point.x()), round(card_point.y())))
            QTest.qWait(40)
            self.assertIn(_discrete_id, _to_variant(dialog.property("expandedOpticalSetIds")))

            # Expanding the dataset grows the scroll content. Recompute its
            # lower bound so the screenshot also shows the process rows.
            expanded_max_content_y = max(
                0, float(flickable.property("contentHeight")) - float(flickable.property("height"))
            )
            flickable.setProperty("contentY", expanded_max_content_y)
            QTest.qWait(60)
            if screenshot_dir:
                image = QQuickWindow.grabWindow(window)
                self.assertFalse(image.isNull())
                self.assertTrue(image.save(str(directory / "material-library-expanded-cn.png")))

            self.assertTrue(QMetaObject.invokeMethod(dialog, "openReferenceDetails"))
            QTest.qWait(60)
            references = dialog.property("referenceDialogInstance")
            self.assertIsNotNone(references, "the lazy reference dialog was not created")
            self.assertTrue(references.property("visible"))
            if screenshot_dir:
                image = QQuickWindow.grabWindow(window)
                self.assertFalse(image.isNull())
                self.assertTrue(image.save(str(directory / "material-reference-details-cn.png")))
            self.assertTrue(QMetaObject.invokeMethod(references, "close"))
            QTest.qWait(30)

            self.assertTrue(QMetaObject.invokeMethod(dialog, "openSetEditor", Q_ARG("QVariant", cauchy_id)))
            QTest.qWait(80)
            editor = window.findChild(QObject, "materialEditor")
            self.assertTrue(editor.property("visible"))
            model_combo = window.findChild(QObject, "opticalEditorModel")
            self.assertEqual(model_combo.property("count"), 12)
            self.assertEqual(editor.property("editingId"), cauchy_id)
            self.assertEqual(self.lib.opticalSetDetails(cauchy_id)["parameters"]["A"], 1.5)
            if screenshot_dir:
                image = QQuickWindow.grabWindow(window)
                self.assertFalse(image.isNull())
                self.assertTrue(image.save(str(Path(screenshot_dir) / "material-editor-cauchy-cn.png")))
            self.assertEqual(editor.property("objectName"), "materialEditor")
        finally:
            _close_qml_test_window(engine, window, locals().get("dialog"))

    def test_qml_editable_tables_keyboard_paste_cancel_save_and_reload(self):
        material_id, discrete_id, cauchy_id = self.create_user_material()
        self.select_saved_material_again(material_id)
        profile_path = self.root / "data" / "materials" / (material_id + ".json")
        before_bytes = profile_path.read_bytes()
        before_record = self.lib.opticalSetDetails(discrete_id)
        font_families = {name.casefold(): name for name in QFontDatabase.families()}
        cjk_family = next((font_families[name.casefold()] for name in
                           ("Arial Unicode MS", "Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans SC")
                           if name.casefold() in font_families), None)
        if cjk_family:
            QGuiApplication.setFont(QFont(cjk_family))
        if os.environ.get("COAT_REQUIRE_NATIVE_FONT_SCREENSHOT"):
            self.assertEqual(QGuiApplication.platformName().casefold(), "windows")
            self.assertEqual(cjk_family, "Arial Unicode MS")

        engine = QQmlApplicationEngine()
        warnings = []
        engine.warnings.connect(lambda messages: warnings.extend(message.toString() for message in messages))
        engine.rootContext().setContextProperty("bridge", self.source)
        theme = _OpticalEditorTheme()
        engine.rootContext().setContextProperty("Theme", theme)
        engine.load(QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/qml/SourceWindow.qml")))
        self.assertTrue(engine.rootObjects(), warnings)
        window = engine.rootObjects()[0]
        try:
            window.showNormal()
            window.resize(1180, 860)
            window.requestActivate()
            QTest.qWait(80)
            dialog = self.open_material_library(window, engine)
            QTest.qWait(80)
            first_card = self.find_optical_card(window, dialog, discrete_id)
            self.assertIsNotNone(first_card)
            self.assertTrue(first_card.property("visible"))
            self.assertGreater(float(first_card.property("width")), 0.0)
            self.assertGreater(float(first_card.property("height")), 0.0)
            focus_return_target = _find_visual_item(first_card, "opticalSetDisclosureHeader")
            self.assertIsNotNone(focus_return_target)
            focus_return_target.forceActiveFocus()
            QTest.qWait(20)
            self.assertTrue(focus_return_target.property("activeFocus"))

            self.assertTrue(QMetaObject.invokeMethod(
                dialog, "openSetEditorFromControl", Q_ARG("QVariant", discrete_id),
                Q_ARG("QVariant", focus_return_target)))
            QTest.qWait(80)
            editor = window.findChild(QObject, "materialEditor")
            n_model = window.findChild(QObject, "opticalEditorTableModel")
            process_model = window.findChild(QObject, "opticalEditorProcessTableModel")
            self.assertTrue(editor.property("visible"))
            self.assertIsNotNone(n_model)
            self.assertIsNotNone(process_model)
            self.assertEqual(n_model.property("rowCount"), len(before_record["wavelengths"]))
            self.assertEqual(process_model.property("rowCount"), 3)
            table_view = window.findChild(QObject, "opticalEditorTable")
            self.assertIsNotNone(table_view, warnings)

            def click_cell(view, row, column, columns):
                target_window = qml_item_window(view) or window
                prefix = "opticalEditorProcessCell" if view.objectName() == "opticalEditorProcessTable" else "opticalEditorCell"
                cell_object_name = f"{prefix}_{row}_{column}"
                cell_item = window.findChild(QObject, cell_object_name) or view.findChild(QObject, cell_object_name)
                if cell_item is not None:
                    point = cell_item.mapToItem(target_window.contentItem(), QPointF(float(cell_item.property("width")) / 2,
                                                                                      float(cell_item.property("height")) / 2))
                else:
                    point = view.mapToItem(target_window.contentItem(), QPointF((column + 0.5) * float(view.property("width")) / columns,
                                                                                row * 44 + 21))
                QTest.mouseClick(target_window, Qt.LeftButton, Qt.NoModifier, QPoint(round(point.x()), round(point.y())))
                if cell_item is not None:
                    cell_item.forceActiveFocus()
                    QTest.qWait(10)
                QTest.qWait(20)

            screenshot_dir = os.environ.get("COAT_PREVIEW_SCREENSHOT_DIR")
            if screenshot_dir:
                directory = Path(screenshot_dir)
                directory.mkdir(parents=True, exist_ok=True)
                debug_image = QQuickWindow.grabWindow(window)
                self.assertFalse(debug_image.isNull())
                self.assertTrue(debug_image.save(str(directory / "material-editor-table-debug.png")))
                if os.environ.get("COAT_NATIVE_TABLE_SCREENSHOT_ONLY"):
                    self.assertTrue(debug_image.save(str(directory / "material-editor-table-cn.png")))
                    return

            # A typed edit followed by Cancel must leave the controller and file untouched.
            click_cell(table_view, 0, 1, 3)
            if not editor.property("nGridFocused"):
                target_cell = window.findChild(QObject, "opticalEditorCell_0_1") or table_view.findChild(QObject, "opticalEditorCell_0_1")
                self.assertIsNotNone(target_cell, "editable QML cell delegate was not instantiated")
                target_cell.forceActiveFocus()
                QTest.qWait(20)
            self.assertTrue(editor.property("nGridFocused"), "the clicked table cell did not receive focus")
            QTest.keyClick(qml_item_window(table_view), Qt.Key_A, Qt.ControlModifier)
            for key in (Qt.Key_1, Qt.Key_Period, Qt.Key_6, Qt.Key_6):
                QTest.keyClick(qml_item_window(table_view), key)
            self.assertEqual(n_model.property("rows")[0]["n"], "1.66")
            cancel = window.findChild(QObject, "opticalEditorCancelButton")
            self.assertTrue(QMetaObject.invokeMethod(cancel, "click"))
            QTest.qWait(40)
            self.assertFalse(editor.property("visible"))
            self.assertTrue(focus_return_target.property("activeFocus"),
                            "cancelling the optical editor did not return focus to its invoking page control")
            self.assertEqual(profile_path.read_bytes(), before_bytes)
            self.assertEqual(self.lib.opticalSetDetails(discrete_id), before_record)

            # Multi-cell Excel-style paste is routed through the focused cell and extends rows.
            self.assertTrue(QMetaObject.invokeMethod(dialog, "openSetEditor", Q_ARG("QVariant", discrete_id)))
            QTest.qWait(60)
            import_path = self.root / "table-import.txt"
            import_path.write_text("410\t1.49\n520\t1.50\n680\t1.51", encoding="utf-8")
            imported = json.loads(self.lib.previewOpticalFile(QUrl.fromLocalFile(str(import_path)).toString(),
                                                                "TXT table", "nm"))
            self.assertNotIn("error", imported)
            self.assertIsNone(imported["k"])
            self.assertTrue(QMetaObject.invokeMethod(editor, "loadImportedNTable",
                                                     Q_ARG("QVariant", imported["wavelengths_text"])))
            self.assertEqual(n_model.property("rows")[0]["wavelength"], "410")
            self.assertEqual(n_model.property("rows")[0]["k"], "")
            QGuiApplication.clipboard().setText("0.45\t1.49\t0.015\n0.50\t1.50\t0\n0.60\t1.52\t0.02")
            click_cell(table_view, 0, 0, 3)
            self.assertTrue(editor.property("nGridFocused"), "the pasted table cell did not receive focus")
            self.assertIn("0.45", self.lib.clipboardText())
            QTest.keyClick(qml_item_window(table_view), Qt.Key_V, Qt.ControlModifier)
            QTest.qWait(50)
            self.assertEqual(n_model.property("rowCount"), 3)
            self.assertEqual(n_model.property("rows")[0]["wavelength"], "0.45")
            self.assertEqual(n_model.property("rows")[2]["k"], "0.02")

            screenshot_dir = os.environ.get("COAT_PREVIEW_SCREENSHOT_DIR")
            if screenshot_dir:
                directory = Path(screenshot_dir)
                directory.mkdir(parents=True, exist_ok=True)
                image = QQuickWindow.grabWindow(window)
                self.assertFalse(image.isNull())
                self.assertTrue(image.save(str(directory / "material-editor-table-cn.png")))

            add_row = window.findChild(QObject, "opticalEditorAddRow")
            click_cell(table_view, 1, 1, 3)
            self.assertTrue(editor.property("nGridFocused"), "the edited table cell did not receive focus")
            QTest.keyClick(qml_item_window(table_view), Qt.Key_A, Qt.ControlModifier)
            for key in (Qt.Key_1, Qt.Key_Period, Qt.Key_5, Qt.Key_5):
                QTest.keyClick(qml_item_window(table_view), key)
            QTest.qWait(20)
            self.assertEqual(n_model.property("rows")[1]["n"], "1.55")
            self.assertTrue(QMetaObject.invokeMethod(add_row, "click"))
            QTest.qWait(20)
            self.assertEqual(n_model.property("rowCount"), 4)
            delete_row = window.findChild(QObject, "opticalEditorDeleteRow")
            self.assertTrue(QMetaObject.invokeMethod(delete_row, "click"))
            QTest.qWait(20)
            self.assertEqual(n_model.property("rowCount"), 3)

            process_table_view = window.findChild(QObject, "opticalEditorProcessTable")
            self.assertIsNotNone(process_table_view)
            self.assertTrue(QMetaObject.invokeMethod(editor, "setProcessCell",
                                                     Q_ARG("QVariant", 1), Q_ARG("QVariant", 1),
                                                     Q_ARG("QVariant", "315 nm/min")))
            QTest.qWait(20)
            self.assertEqual(process_model.property("rows")[1]["value"], "315 nm/min")

            unit = window.findChild(QObject, "opticalEditorUnit")
            unit.setProperty("currentIndex", 2)
            window.findChild(QObject, "opticalEditorRangeMin").setProperty("text", "0.45")
            window.findChild(QObject, "opticalEditorRangeMax").setProperty("text", "0.60")
            save_editor = window.findChild(QObject, "opticalEditorSaveButton")
            self.assertTrue(QMetaObject.invokeMethod(save_editor, "click"))
            QTest.qWait(50)
            self.assertFalse(editor.property("visible"))
            self.assertTrue(self.lib.save(), self.lib.status)
            saved_profile = read_material_profile(profile_path)
            saved_set = next(row for row in saved_profile["optical_constants"] if row["id"] == discrete_id)
            self.assertEqual(saved_set["wavelength_unit"], "um")
            self.assertEqual(saved_set["wavelengths"], [0.45, 0.5, 0.6])
            self.assertEqual(saved_set["k"], [0.015, 0.0, 0.02])
            self.assertEqual(saved_set["process_parameters"][1]["value"], "315")
            self.assertEqual(saved_set["process_parameters"][1]["unit"], "nm/min")

            self.lib.reload()
            self.settle_materials()
            self.assertTrue(self.lib.selectMaterial("user:" + material_id))
            self.settle_materials()
            self.assertEqual(self.lib.opticalSetDetails(discrete_id)["id"], discrete_id)
            self.assertEqual(self.lib.opticalSetDetails(discrete_id)["n"], [1.49, 1.55, 1.52])
            self.assertTrue(QMetaObject.invokeMethod(dialog, "openSetEditor", Q_ARG("QVariant", discrete_id)))
            QTest.qWait(60)
            self.assertEqual(n_model.property("rowCount"), 3)
            self.assertEqual(n_model.property("rows")[1]["wavelength"], "0.5")
            self.assertEqual(process_model.property("rows")[1]["value"], "315 nm/min")
            self.assertTrue(QMetaObject.invokeMethod(window.findChild(QObject, "opticalEditorCancelButton"), "click"))
            QTest.qWait(30)

            # The formula's independent k table remains a separate two-column grid and wavelength axis.
            self.assertTrue(QMetaObject.invokeMethod(dialog, "openSetEditor", Q_ARG("QVariant", cauchy_id)))
            QTest.qWait(60)
            k_model = window.findChild(QObject, "opticalEditorKTableModel")
            self.assertIsNotNone(k_model)
            self.assertEqual(k_model.property("rowCount"), 3)
            equation_image = window.findChild(QObject, "opticalEditorEquationImage")
            self.assertIsNotNone(equation_image)
            QTest.qWait(100)
            self.assertEqual(str(equation_image.property("renderState")), "ready",
                             f"the themed SVG did not load: source={equation_image.property('source')}; "
                             f"themeColor={equation_image.property('formulaColorDebug')}; warnings={warnings}; "
                             f"size={equation_image.property('width')}x{equation_image.property('height')}; "
                             f"paintedWidth={equation_image.property('paintedWidth')}")
            self.assertGreater(float(equation_image.property("paintedWidth")), 0.0)
            formula_source = equation_image.property("source")
            self.assertTrue(formula_source)
            formula_path = Path(QUrl(formula_source).toLocalFile())
            self.assertEqual(formula_path.suffix, ".svg")
            self.assertTrue(formula_path.is_file())
            formula_color = str(equation_image.property("formulaColorDebug"))
            self.assertEqual(formula_color, build_theme_snapshot("dark")["colorText"])
            self.assertIn(formula_color.lower(), formula_path.read_text(encoding="utf-8").lower())
            screenshot_dir = os.environ.get("COAT_PREVIEW_SCREENSHOT_DIR")
            if screenshot_dir:
                directory = Path(screenshot_dir)
                directory.mkdir(parents=True, exist_ok=True)
                image = QQuickWindow.grabWindow(window)
                self.assertFalse(image.isNull())
                self.assertTrue(image.save(str(directory / "material-editor-cauchy-svg-themed.png")))
            parameter_grid = window.findChild(QObject, "opticalEditorParameterGrid")
            parameter_repeater = window.findChild(QObject, "opticalEditorParameterRepeater")
            self.assertIsNotNone(parameter_grid)
            self.assertIsNotNone(parameter_repeater)
            self.assertEqual(parameter_grid.property("columns"), 3)
            self.assertEqual(parameter_repeater.property("count"), 3)
            formula_unit_label = window.findChild(QObject, "opticalEditorFormulaWavelengthUnit")
            wavelength_unit = window.findChild(QObject, "opticalEditorUnit")
            model_combo = window.findChild(QObject, "opticalEditorModel")
            self.assertIsNotNone(formula_unit_label)
            self.assertIsNotNone(wavelength_unit)
            self.assertIsNotNone(model_combo)
            self.assertIn(str(wavelength_unit.property("currentText")),
                          str(formula_unit_label.property("text")))
            self.assertNotIn("wavelength_unit", str(formula_unit_label.property("text")))
            original_unit_index = int(wavelength_unit.property("currentIndex"))
            wavelength_unit.setProperty("currentIndex", 1)
            QTest.qWait(20)
            self.assertIn("nm", str(formula_unit_label.property("text")))
            wavelength_unit.setProperty("currentIndex", original_unit_index)
            rii_index = next(index for index, row in enumerate(self.lib.opticalModelCatalog)
                             if row["kind"] == "rii_formula")
            original_model_index = int(model_combo.property("currentIndex"))
            model_combo.setProperty("currentIndex", rii_index)
            QTest.qWait(20)
            self.assertIn("um", str(formula_unit_label.property("text")))
            model_combo.setProperty("currentIndex", original_model_index)
            QTest.qWait(20)
            scroll = window.findChild(QObject, "materialEditorScrollView")
            self.assertIsNotNone(scroll)
            flickable = next((child for child in scroll.findChildren(QObject)
                              if child.metaObject().className() == "QQuickFlickable"), None)
            self.assertIsNotNone(flickable)
            flickable.setProperty("contentY", max(0, float(flickable.property("contentHeight")) -
                                                    float(flickable.property("height"))))
            QTest.qWait(30)
            QGuiApplication.clipboard().setText("0.45\t0.015\n0.65\t0.025\n0.75\t0.005")
            k_table_view = window.findChild(QObject, "opticalEditorKTable")
            click_cell(k_table_view, 0, 0, 2)
            self.assertTrue(editor.property("kGridFocused"), "the independent-k table cell did not receive focus")
            QTest.keyClick(qml_item_window(k_table_view), Qt.Key_V, Qt.ControlModifier)
            QTest.qWait(30)
            self.assertEqual(k_model.property("rows")[2]["k"], "0.005")
            window.findChild(QObject, "opticalEditorKRangeMin").setProperty("text", "0.45")
            window.findChild(QObject, "opticalEditorKRangeMax").setProperty("text", "0.75")
            self.assertTrue(QMetaObject.invokeMethod(window.findChild(QObject, "opticalEditorSaveButton"), "click"))
            QTest.qWait(30)
            self.assertTrue(self.lib.save(), self.lib.status)
            self.lib.reload()
            self.settle_materials()
            self.assertTrue(self.lib.selectMaterial("user:" + material_id))
            self.settle_materials()
            final_formula = self.lib.opticalSetDetails(cauchy_id)
            self.assertEqual(final_formula["k_data"]["wavelengths"], [0.45, 0.65, 0.75])
            self.assertEqual(final_formula["k_data"]["wavelength_unit"], "um")
        finally:
            _close_qml_test_window(engine, window, locals().get("dialog"))

    def test_qml_cancel_unsaved_optical_edit_preserves_draft_and_returns_focus(self):
        # This UI lifecycle check deliberately stays in an unsaved user draft:
        # it does not depend on the host's atomic file-publishing capability.
        self.assertTrue(self.lib.newMaterial())
        self.lib.setField("name", "Cancel lifecycle fixture")
        table_form = {
            "name": "Discrete cancel fixture", "notes": "Synthetic editor cancel test.",
            "process_parameters": [], "source": "synthetic-test", "kind": "tabulated",
            "wavelength_unit": "nm", "wavelength_min": "400", "wavelength_max": "700",
            "table": "400\t1.50\t0.01\n550\t1.51\t0\n700\t1.52\t0.02",
        }
        self.assertTrue(self.lib.addOpticalSet(
            json.dumps(table_form, ensure_ascii=False)), self.lib.status)
        second_form = dict(table_form, name="Second cancel fixture")
        self.assertTrue(self.lib.addOpticalSet(
            json.dumps(second_form, ensure_ascii=False)), self.lib.status)
        identities = [row["id"] for row in self.lib.opticalSets]
        identity, second_identity = identities
        before_draft = self.lib.draftData()

        engine = QQmlEngine()
        network_factory = NoNetworkAccessManagerFactory()
        engine.setNetworkAccessManagerFactory(network_factory)
        warnings = []
        engine.warnings.connect(lambda messages: warnings.extend(message.toString() for message in messages))
        engine.rootContext().setContextProperty("materialLibrary", self.lib)
        theme = ThemeService.instance()
        previous_theme_mode = theme.current_preference_mode
        previous_theme_id = theme.current_theme_id
        theme.set_theme("light")
        theme.attach_engine(engine, "Theme")
        component = QQmlComponent(engine)
        component.setData(b'''\
import QtQuick
import QtQuick.Controls
import QtQuick.Window
import "../coat_sim/ui/qml" as CoatUi
Window {
    width: 1180; height: 860; visible: true
    CoatUi.MaterialLibraryDialog {
        id: materialDialog
        objectName: "materialLibraryDialog"
        library: materialLibrary
        visible: true
    }
}
''', QUrl.fromLocalFile(str(ROOT / "tests" / "material_library_editor_cancel_host.qml")))
        self.assertEqual(component.status(), QQmlComponent.Status.Ready,
                         [message.toString() for message in component.errors()])
        window = component.create()
        self.assertIsNotNone(window, [message.toString() for message in component.errors()])
        try:
            window.show()
            QTest.qWait(80)
            dialog = window.findChild(QObject, "materialLibraryDialog")
            self.assertIsNotNone(dialog, warnings)
            QTest.qWait(80)
            card = dialog.property("firstOpticalSetCard")
            self.assertIsNotNone(card, f"no card: opticalSets={self.lib.opticalSets!r}")
            header = card.findChild(QObject, "opticalSetDisclosureHeader")
            self.assertIsNotNone(header)
            header.forceActiveFocus()
            QTest.qWait(20)
            self.assertTrue(header.property("activeFocus"))
            self.assertTrue(QMetaObject.invokeMethod(header, "click"))
            QTest.qWait(40)
            repeater = dialog.findChild(QObject, "opticalSetsRepeater")
            self.assertIsNotNone(repeater)
            second_card_expr = QQmlExpression(engine.rootContext(), repeater, "itemAt(1)")
            second_card_value, undefined = second_card_expr.evaluate()
            self.assertFalse(second_card_expr.hasError(), second_card_expr.error().toString())
            self.assertFalse(undefined)
            second_card = (second_card_value.toVariant()
                           if hasattr(second_card_value, "toVariant") else second_card_value)
            second_header = second_card.findChild(QObject, "opticalSetDisclosureHeader")
            self.assertIsNotNone(second_header)
            self.assertTrue(QMetaObject.invokeMethod(second_header, "click"))

            def curve_state(set_id):
                expression = QQmlExpression(
                    engine.rootContext(), dialog,
                    f'opticalCurveState("{set_id}")')
                result, is_undefined = expression.evaluate()
                self.assertFalse(expression.hasError(), expression.error().toString())
                self.assertFalse(is_undefined)
                return result.toVariant()

            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                QCoreApplication.processEvents()
                first_state = curve_state(identity)
                second_state = curve_state(second_identity)
                if (not first_state["loading"] and not second_state["loading"]
                        and len(first_state["points"]) >= 2
                        and len(second_state["points"]) >= 2):
                    break
                QTest.qWait(10)
            self.assertGreaterEqual(len(first_state["points"]), 2)
            self.assertGreaterEqual(len(second_state["points"]), 2)
            expanded = dialog.property("expandedOpticalSetIds")
            expanded = expanded.toVariant() if hasattr(expanded, "toVariant") else expanded
            self.assertEqual(set(expanded), set(identities))
            first_card = dialog.property("firstOpticalSetCard")
            self.assertEqual(len(first_card.property("curvePoints")), 65)
            self.assertEqual(len(second_card.property("curvePoints")), 65)

            edit_button = first_card.findChild(QObject, "editOpticalSetButton")
            self.assertIsNotNone(edit_button)
            edit_button.forceActiveFocus()
            QTest.qWait(20)
            self.assertTrue(edit_button.property("activeFocus"))
            self.assertTrue(QMetaObject.invokeMethod(edit_button, "click"))
            QTest.qWait(80)
            editor = dialog.property("setEditorInstance")
            n_model = editor.findChild(QObject, "opticalEditorTableModel")
            self.assertTrue(editor.property("visible"))
            self.assertIsNotNone(n_model)
            engine.rootContext().setContextProperty("nModelForTest", n_model)
            edit_expression = QQmlExpression(
                engine.rootContext(), editor,
                'setGridCell(nModelForTest, 0, 1, "1.66", false)')
            edit_expression.evaluate()
            self.assertFalse(edit_expression.hasError(), edit_expression.error().toString())
            QTest.qWait(20)
            self.assertEqual(n_model.property("rows")[0]["n"], "1.66")

            cancel = editor.findChild(QObject, "opticalEditorCancelButton")
            self.assertTrue(QMetaObject.invokeMethod(cancel, "click"))
            QTest.qWait(40)
            self.assertFalse(editor.property("visible"))
            self.assertEqual(self.lib.draftData(), before_draft)
            expanded = dialog.property("expandedOpticalSetIds")
            expanded = expanded.toVariant() if hasattr(expanded, "toVariant") else expanded
            self.assertEqual(set(expanded), set(identities))
            self.assertEqual(len(curve_state(identity)["points"]), 65)
            self.assertEqual(len(curve_state(second_identity)["points"]), 65)
            self.assertEqual(len(first_card.property("curvePoints")), 65)
            self.assertEqual(len(second_card.property("curvePoints")), 65)
            self.assertTrue(edit_button.property("activeFocus"),
                            "cancelling the optical editor did not return focus to its invoking page control")
            for _ in range(2):
                self.assertTrue(QMetaObject.invokeMethod(edit_button, "click"))
                QTest.qWait(60)
                self.assertTrue(editor.property("visible"))
                self.assertTrue(QMetaObject.invokeMethod(cancel, "click"))
                QTest.qWait(60)
                self.assertFalse(editor.property("visible"))
                self.assertTrue(edit_button.property("activeFocus"),
                                "repeated cancellation did not restore the current invoking control")
                self.assertEqual(self.lib.draftData(), before_draft)

            # Closing the page while the editor is open invalidates its queued
            # focus return instead of focusing a control on the hidden page.
            self.assertTrue(QMetaObject.invokeMethod(edit_button, "click"))
            QTest.qWait(60)
            self.assertTrue(editor.property("visible"))
            focus_generation_before_close = int(dialog.property("editorFocusGeneration"))
            self.assertTrue(QMetaObject.invokeMethod(dialog, "close"))
            QTest.qWait(100)
            self.assertFalse(dialog.property("visible"))
            self.assertFalse(editor.property("visible"))
            self.assertGreater(int(dialog.property("editorFocusGeneration")), focus_generation_before_close)
            self.assertIsNone(dialog.property("editorFocusReturnTarget"))
            self.assertFalse(edit_button.property("activeFocus"),
                             "closing the page allowed a late editor callback to steal focus")
        finally:
            try:
                dialog = locals().get("dialog")
                if dialog is not None and dialog.property("visible"):
                    QMetaObject.invokeMethod(dialog, "close")
                window.close()
                QTest.qWait(30)
            finally:
                window.deleteLater()
                engine.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                QApplication.instance().processEvents()
                theme.configure(mode=previous_theme_mode, theme_package_id=previous_theme_id)

    def test_formula_theme_pair_renders_with_bundled_cjk_and_tracks_color(self):
        material_id, _discrete_id, cauchy_id = self.create_user_material()
        self.select_saved_material_again(material_id)
        cjk_family = bundled_font_family()
        self.assertTrue(cjk_family, "the bundled Noto CJK font was not registered")
        previous_font = APP.font()
        APP.setFont(QFont(cjk_family))

        theme_service = ThemeService.instance()
        previous_mode = theme_service.current_preference_mode
        previous_theme = theme_service.current_theme_id
        theme_service.configure(mode="light", theme_package_id="")
        engine = QQmlApplicationEngine()
        engine.rootContext().setContextProperty("bridge", self.source)
        theme_bridge = theme_service.attach_engine(engine)
        engine.load(QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/qml/SourceWindow.qml")))
        self.assertTrue(engine.rootObjects())
        window = engine.rootObjects()[0]
        try:
            window.showNormal()
            window.resize(1180, 860)
            window.requestActivate()
            QTest.qWait(80)
            dialog = self.open_material_library(window, engine)
            QTest.qWait(60)
            self.assertTrue(QMetaObject.invokeMethod(dialog, "openSetEditor", Q_ARG("QVariant", cauchy_id)))
            QTest.qWait(100)

            formula_image = window.findChild(QObject, "opticalEditorEquationImage")
            formula_unit = window.findChild(QObject, "opticalEditorFormulaWavelengthUnit")
            parameter_grid = window.findChild(QObject, "opticalEditorParameterGrid")
            parameter_repeater = window.findChild(QObject, "opticalEditorParameterRepeater")
            self.assertIsNotNone(formula_image)
            self.assertIsNotNone(formula_unit)
            self.assertIsNotNone(parameter_grid)
            self.assertIsNotNone(parameter_repeater)
            self.assertEqual(parameter_grid.property("columns"), 3)
            self.assertEqual(parameter_repeater.property("count"), 3)
            parameter_widths = []
            for name in ("A", "B", "C"):
                label = _find_visual_item(parameter_grid, f"opticalEditorParameterLabel_{name}")
                self.assertIsNotNone(label, f"missing coefficient label {name}")
                self.assertEqual(str(label.property("text")), name)
                field = _find_visual_item(parameter_grid, f"opticalEditorParameter_{name}")
                self.assertIsNotNone(field, f"missing coefficient input {name}")
                parameter_widths.append(float(field.property("width")))
            self.assertLess(max(parameter_widths) - min(parameter_widths), 2.0)

            unit_text = str(formula_unit.property("text"))
            self.assertIn("公式中的 λ 单位：", unit_text)
            self.assertIn("um", unit_text)
            self.assertNotIn("wavelength_unit", unit_text)
            screenshots = Path(os.environ.get(
                "COAT_PREVIEW_SCREENSHOT_DIR", ROOT / "build" / "material_editor_theme_pair_qa"
            ))
            screenshots.mkdir(parents=True, exist_ok=True)
            sources = {}
            colors = {}
            for mode in ("light", "dark"):
                theme_service.configure(mode=mode, theme_package_id="")
                QTest.qWait(120)
                APP.processEvents()
                self.assertEqual(theme_bridge.property("mode"), mode)
                self.assertEqual(str(formula_image.property("renderState")), "ready")
                color = str(formula_image.property("formulaColorDebug"))
                self.assertEqual(color, build_theme_snapshot(mode)["colorText"])
                source_value = formula_image.property("source")
                source = (source_value.toString() if isinstance(source_value, QUrl)
                          else str(source_value))
                self.assertTrue(source)
                svg_path = Path(QUrl(source).toLocalFile())
                svg = svg_path.read_text(encoding="utf-8").lower()
                self.assertIn(color.lower(), svg)
                sources[mode] = source
                colors[mode] = color
                image = QQuickWindow.grabWindow(window)
                self.assertFalse(image.isNull())
                screenshot = screenshots / f"material-editor-optical-{mode}.png"
                self.assertTrue(image.save(str(screenshot)))
                self.assertGreater(screenshot.stat().st_size, 1000)
            self.assertNotEqual(colors["light"], colors["dark"])
            self.assertNotEqual(sources["light"], sources["dark"])
        finally:
            _close_qml_test_window(engine, window, locals().get("dialog"))
            theme_service.configure(mode=previous_mode, theme_package_id=previous_theme)
            APP.setFont(previous_font)
            APP.processEvents()

    def test_real_wheel_events_stay_inside_all_three_tables_at_both_edges(self):
        material_id, discrete_id, cauchy_id = self.create_user_material()
        self.select_saved_material_again(material_id)
        engine = QQmlApplicationEngine()
        warnings = []
        engine.warnings.connect(lambda messages: warnings.extend(message.toString() for message in messages))
        engine.rootContext().setContextProperty("bridge", self.source)
        engine.load(QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/qml/SourceWindow.qml")))
        self.assertTrue(engine.rootObjects(), warnings)
        window = engine.rootObjects()[0]
        editor = None
        dialog = None
        try:
            window.showNormal()
            window.resize(1180, 860)
            window.requestActivate()
            QTest.qWait(80)
            dialog = self.open_material_library(window, engine)
            QTest.qWait(80)
            self.assertTrue(QMetaObject.invokeMethod(dialog, "openSetEditor", Q_ARG("QVariant", discrete_id)))
            QTest.qWait(80)
            editor = window.findChild(QObject, "materialEditor")
            self.assertTrue(editor.property("visible"))

            n_table = window.findChild(QObject, "opticalEditorTable")
            n_model = window.findChild(QObject, "opticalEditorTableModel")
            n_add = window.findChild(QObject, "opticalEditorAddRow")
            process_table = window.findChild(QObject, "opticalEditorProcessTable")
            process_model = window.findChild(QObject, "opticalEditorProcessTableModel")
            process_add = window.findChild(QObject, "opticalEditorAddProcessRow")
            self.assertIsNotNone(n_table)
            self.assertIsNotNone(process_table)
            for _ in range(9):
                self.assertTrue(QMetaObject.invokeMethod(n_add, "click"))
                self.assertTrue(QMetaObject.invokeMethod(process_add, "click"))
            QTest.qWait(60)

            editor_scroll = window.findChild(QObject, "materialEditorScrollView")
            outer = next((child for child in editor_scroll.findChildren(QObject)
                          if child.metaObject().className() == "QQuickFlickable"), None)
            self.assertIsNotNone(outer)

            def reveal(view):
                surface_window = qml_item_window(view) or window
                for _ in range(4):
                    point = view.mapToItem(
                        surface_window.contentItem(), QPointF(float(view.property("width")) / 2,
                                                              float(view.property("height")) / 2)
                    )
                    desired = max(24.0, min(float(surface_window.height()) - 24.0,
                                            float(surface_window.height()) * 0.65))
                    shift = point.y() - desired
                    if abs(shift) < 4:
                        return
                    maximum = max(0.0, float(outer.property("contentHeight")) - float(outer.property("height")))
                    outer.setProperty("contentY", max(0.0, min(maximum, float(outer.property("contentY")) + shift)))
                    QTest.qWait(25)
                point = view.mapToItem(surface_window.contentItem(), QPointF(float(view.property("width")) / 2,
                                                                              float(view.property("height")) / 2))
                self.assertGreaterEqual(point.y(), 0)
                self.assertLess(point.y(), surface_window.height())

            def assert_isolated_wheel(view, cell_prefix, row_count, value_model=None):
                reveal(view)
                self.assertTrue(view.isVisible(), view.objectName())
                values_before = deepcopy(value_model.property("rows")) if value_model is not None else None
                maximum = max(0.0, float(view.property("contentHeight")) - float(view.property("height")))
                self.assertGreater(maximum, 0.0, view.objectName())
                outer_maximum = max(0.0, float(outer.property("contentHeight")) - float(outer.property("height")))
                self.assertGreater(outer_maximum, 0.0,
                                   f"{view.objectName()} has no outer scroll range to exercise")

                first_cell = _find_visual_item(view, f"{cell_prefix}_0_0")
                self.assertIsNotNone(first_cell, f"missing first visible cell in {view.objectName()}")
                view.setProperty("contentY", 0.0)
                outer_before = float(outer.property("contentY"))
                event_accepted = _send_wheel_event(window, first_cell, pixel_delta=-48)
                QTest.qWait(25)
                point = first_cell.mapToItem(qml_item_window(view).contentItem(), QPointF(
                    float(first_cell.property("width")) / 2, float(first_cell.property("height")) / 2))
                root_item = qml_item_window(view).contentItem()
                hit = root_item.childAt(point.x(), point.y())
                hit_path = []
                for _ in range(16):
                    if hit is None:
                        break
                    hit_path.append(f"{hit.metaObject().className()}:{hit.objectName()}")
                    local = hit.mapFromItem(root_item, point)
                    child = hit.childAt(local.x(), local.y())
                    if child is None or child == hit:
                        break
                    hit = child
                ancestors = []
                item = view
                for _ in range(8):
                    if item is None:
                        break
                    origin = item.mapToItem(root_item, QPointF(0, 0))
                    ancestors.append(
                        f"{item.metaObject().className()}:{item.objectName()}"
                        f"@{origin.x():.0f},{origin.y():.0f}"
                        f"/{item.property('width'):.0f}x{item.property('height'):.0f}"
                        f"/visible={item.isVisible()}"
                    )
                    item = item.parentItem()
                self.assertGreater(float(view.property("contentY")), 0.0,
                                   f"{view.objectName()} did not move after pixel wheel; accepted={event_accepted}; "
                                   f"outer={outer_before}->{float(outer.property('contentY'))}; "
                                   f"window={qml_item_window(view).objectName()}; point={point.x(), point.y()}; "
                                   f"hit={' > '.join(hit_path)}; ancestors={' > '.join(ancestors)}")
                self.assertAlmostEqual(float(outer.property("contentY")), outer_before, places=2)
                if value_model is not None:
                    self.assertEqual(value_model.property("rows"), values_before)

                view.setProperty("contentY", maximum)
                QTest.qWait(25)
                last_cell = _find_visual_item(view, f"{cell_prefix}_{row_count - 1}_0")
                self.assertIsNotNone(last_cell, f"missing last visible cell in {view.objectName()}")
                outer_before = float(outer.property("contentY"))
                _send_wheel_event(window, last_cell, pixel_delta=-48)
                QTest.qWait(25)
                self.assertAlmostEqual(float(view.property("contentY")), maximum, places=2)
                self.assertAlmostEqual(float(outer.property("contentY")), outer_before, places=2)
                if value_model is not None:
                    self.assertEqual(value_model.property("rows"), values_before)

                view.setProperty("contentY", 0.0)
                outer_before = float(outer.property("contentY"))
                _send_wheel_event(window, first_cell, angle_delta=120)
                QTest.qWait(25)
                self.assertEqual(float(view.property("contentY")), 0.0)
                self.assertAlmostEqual(float(outer.property("contentY")), outer_before, places=2)
                if value_model is not None:
                    self.assertEqual(value_model.property("rows"), values_before)

                # The last pixel inside the viewport is still owned by the table.
                boundary = QPointF(float(view.property("width")) - 1.0,
                                   float(view.property("height")) / 2)
                _send_wheel_event(window, view, angle_delta=-120, local_pos=boundary)
                QTest.qWait(25)
                self.assertGreater(float(view.property("contentY")), 0.0,
                                   f"{view.objectName()} did not scroll at its right boundary")
                self.assertAlmostEqual(float(outer.property("contentY")), outer_before, places=2)
                if value_model is not None:
                    self.assertEqual(value_model.property("rows"), values_before)

                # Just beyond the viewport is blank form area; its wheel goes to the outer scroll view.
                view.setProperty("contentY", 0.0)
                QTest.qWait(20)
                outer_maximum = max(0.0, float(outer.property("contentHeight")) - float(outer.property("height")))
                self.assertGreater(outer_maximum, 0.0,
                                   f"{view.objectName()} has no outer scroll range to exercise")
                outer_before = float(outer.property("contentY"))
                blank = QPointF(float(view.property("width")) + 12,
                                float(view.property("height")) / 2)
                blank_in_window = view.mapToItem(window.contentItem(), blank)
                blank_name = {
                    "opticalEditorTable": "opticalEditorNTableBlankArea",
                    "opticalEditorProcessTable": "opticalEditorProcessTableBlankArea",
                    "opticalEditorKTable": "opticalEditorKTableBlankArea",
                }[view.objectName()]
                blank_area = window.findChild(QObject, blank_name)
                self.assertIsNotNone(blank_area, f"missing outer-scroll blank area for {view.objectName()}")
                blank_local = view.mapToItem(blank_area, blank)
                self.assertGreaterEqual(blank_local.x(), 0.0)
                self.assertLess(blank_local.x(), float(blank_area.property("width")))
                self.assertGreaterEqual(blank_local.y(), 0.0)
                self.assertLess(blank_local.y(), float(blank_area.property("height")))
                self.assertLess(blank_in_window.x(), float(window.contentItem().property("width")))
                if outer_before >= outer_maximum - 1.0:
                    # At the lower boundary, wheel upward to verify blank
                    # form space still reaches the outer Flickable.
                    blank_event_accepted = _send_wheel_event(window, view, pixel_delta=48, local_pos=blank)
                    outer_moved = lambda: float(outer.property("contentY")) < outer_before
                else:
                    blank_event_accepted = _send_wheel_event(window, view, pixel_delta=-48, local_pos=blank)
                    outer_moved = lambda: float(outer.property("contentY")) > outer_before
                QTest.qWait(25)
                root_item = window.contentItem()
                hit = root_item.childAt(blank_in_window.x(), blank_in_window.y())
                hit_path = []
                for _ in range(16):
                    if hit is None:
                        break
                    hit_path.append(f"{hit.metaObject().className()}:{hit.objectName()}")
                    local = hit.mapFromItem(root_item, blank_in_window)
                    child = hit.childAt(local.x(), local.y())
                    if child is None or child == hit:
                        break
                    hit = child
                geometry = (f"point={blank_in_window.x(), blank_in_window.y()}; "
                            f"table={view.objectName()}@{view.mapToItem(root_item, QPointF(0, 0)).x():.1f},"
                            f"{view.mapToItem(root_item, QPointF(0, 0)).y():.1f}/"
                            f"{view.property('width')}x{view.property('height')}; "
                            f"hit={' > '.join(hit_path)}; outer={outer.property('contentY')}/{outer_maximum}; "
                            f"inner={view.property('contentY')}; accepted={blank_event_accepted}; "
                            f"scrollContent={editor_scroll.property('contentItem')}; "
                            f"scrollContentType={editor_scroll.property('contentItem').metaObject().className() if editor_scroll.property('contentItem') else None}; "
                            f"scrollContentY={editor_scroll.property('contentItem').property('contentY') if editor_scroll.property('contentItem') else None}")
                self.assertEqual(float(view.property("contentY")), 0.0,
                                 f"the blank-space event still moved the inner table; {geometry}")
                self.assertTrue(outer_moved(),
                                   f"blank space to the right of the table did not scroll the outer form; {geometry}")
                self.assertEqual(float(view.property("contentY")), 0.0)
                if value_model is not None:
                    self.assertEqual(value_model.property("rows"), values_before)

            assert_isolated_wheel(n_table, "opticalEditorCell", 12, n_model)
            process_table = window.findChild(QObject, "opticalEditorProcessTable")
            self.assertIsNotNone(process_table, "process table was recreated after the n-table wheel check")
            assert_isolated_wheel(process_table, "opticalEditorProcessCell", 12, process_model)

            self.assertTrue(QMetaObject.invokeMethod(window.findChild(QObject, "opticalEditorCancelButton"), "click"))
            QTest.qWait(40)
            self.assertTrue(QMetaObject.invokeMethod(dialog, "openSetEditor", Q_ARG("QVariant", cauchy_id)))
            QTest.qWait(80)
            editor = window.findChild(QObject, "materialEditor")
            k_table = window.findChild(QObject, "opticalEditorKTable")
            k_model = window.findChild(QObject, "opticalEditorKTableModel")
            self.assertIsNotNone(k_table)
            for _ in range(9):
                self.assertTrue(QMetaObject.invokeMethod(editor, "appendGridRow",
                    Q_ARG("QVariant", k_model), Q_ARG("QVariant", True)))
            QTest.qWait(60)
            assert_isolated_wheel(k_table, "opticalEditorKCell", 12, k_model)
        finally:
            _close_qml_test_window(engine, window, dialog)


if __name__ == "__main__":
    unittest.main()
