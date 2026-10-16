"""Independent thin-film design and spectrum windows; physics lives in the service."""
from __future__ import annotations

import copy
import inspect
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import sys
import threading

from PySide6.QtCore import Qt, Signal, QObject, Slot, QSignalBlocker, Property, QUrl, QEvent
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QFormLayout, QTableWidget, QTableWidgetItem, QComboBox, QDoubleSpinBox,
    QPushButton, QLabel, QLineEdit, QPlainTextEdit, QCheckBox, QTabWidget, QSplitter,
    QFileDialog, QMessageBox, QHeaderView, QGroupBox, QDialog, QToolButton, QDialogButtonBox,
    QInputDialog, QScrollArea, QFrame, QSizePolicy, QMenu)
from PySide6.QtGui import QColor, QPalette
from PySide6.QtQuickWidgets import QQuickWidget
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib import font_manager, rcParams
from matplotlib.font_manager import FontProperties
from coat_sim.core.design_files import design_extension
from coat_sim.ui.fonts import FONT_ROOT
from .film_simulation_settings import _IntegerInput

_film_cjk_font = FONT_ROOT / "NotoSansCJKsc-Regular.otf"
_FILM_FONT_PROPERTIES = FontProperties(fname=str(_film_cjk_font)) if _film_cjk_font.is_file() else None


def _film_font_kwargs():
    if _FILM_FONT_PROPERTIES is None:
        raise RuntimeError("缺少内置 Noto CJK 字体，无法可靠绘制膜系文字。")
    return {"fontproperties": _FILM_FONT_PROPERTIES}


def _film_channel_report(channel, data):
    """The same channel coverage disclosure in the window and exported plot."""
    source = data.get("source_range_nm")
    if source is None:
        return f"{channel}：未知"
    text = f"{channel}：来源波段 {source[0]:g}–{source[1]:g} nm"
    if channel == "k" and not data.get("enabled", True):
        return text + "；未评估"
    effective = data.get("effective_input_range_nm")
    if effective:
        text += f"；有效输入 {effective[0]:g}–{effective[1]:g} nm"
    method = data.get("interpolation_method") or data.get("method", "未知")
    method = {"linear": "分段线性插值", "piecewise_linear": "分段线性插值",
              "single_point": "单点样本", "cauchy": "Cauchy 公式", "sellmeier": "Sellmeier 公式"}.get(method, method)
    if method.startswith("rii_formula_"):
        method = "RII 公式 " + method.removeprefix("rii_formula_")
    elif method.startswith("RII formula "):
        method = "RII 公式 " + method.removeprefix("RII formula ")
    text += "；方法 " + method
    segment = data.get("interpolated_segments_nm")
    if segment:
        text += f"；所用采样段包络 {segment[0]:g}–{segment[1]:g} nm"
    external = data.get("extrapolated_ranges_nm") or []
    if external:
        text += "；端点保持外推 " + "、".join(f"{lo:g}–{hi:g} nm" for lo, hi in external)
    else:
        text += "；无外推"
    return text


if _film_cjk_font.is_file():
    font_manager.fontManager.addfont(str(_film_cjk_font))
    rcParams["font.family"] = font_manager.FontProperties(fname=str(_film_cjk_font)).get_name()
    rcParams["axes.unicode_minus"] = False

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _combo(items):
    box = QComboBox()
    for text, value in items:
        box.addItem(text, value)
    return box


def _number(value=0., maximum=1e9, minimum=0.):
    box = QDoubleSpinBox()
    box.setRange(minimum, maximum)
    box.setDecimals(6)
    box.setValue(value)
    return box


def _select(box, value):
    index = box.findData(value)
    if index < 0 and value not in ("", None):
        box.addItem("缺失/不可用：" + str(value), value)
        index = box.count() - 1
    if index >= 0:
        box.setCurrentIndex(index)



class _MediumEditor(QWidget):
    changed = Signal()
    def __init__(self, options):
        super().__init__()
        self.options = options
        self._updating = False
        row = QHBoxLayout(self); row.setContentsMargins(0, 0, 0, 0)
        self.kind = _combo([("透明常数介质", "constant"), ("材料数据组", "material")])
        self.n = _number(1, minimum=0.000001)
        self.material = _combo([("请选择材料", "")] + [(item["name"], item["id"]) for item in options])
        self.optical_set = QComboBox(); self.optical_set.addItem("请选择光学常数组", "")
        for widget in (self.kind, self.n, self.material, self.optical_set): row.addWidget(widget)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self.material.currentIndexChanged.connect(self._material_changed)
        self.optical_set.currentIndexChanged.connect(self._changed)
        self.n.valueChanged.connect(self._changed)
        self._visibility()

    def _changed(self, *args):
        if not self._updating: self.changed.emit()

    def _visibility(self):
        constant = self.kind.currentData() == "constant"
        self.n.setVisible(constant)
        self.material.setVisible(not constant); self.optical_set.setVisible(not constant)

    def _kind_changed(self):
        self._visibility(); self._changed()

    def _material_changed(self):
        if self._updating: return
        FilmDesignWindow._sets(self.optical_set, self.options, self.material.currentData())
        self._changed()

    def set_medium(self, record):
        self._updating = True
        try:
            _select(self.kind, record.get("kind", "constant"))
            self.n.setValue(record.get("n", 1))
            _select(self.material, record.get("material_id", ""))
            FilmDesignWindow._sets(self.optical_set, self.options, record.get("material_id", ""), record.get("optical_set_id", ""))
        finally:
            self._updating = False
        self._visibility()

    def current_medium(self):
        if self.kind.currentData() == "constant":
            return {"kind": "constant", "n": self.n.value()}
        return {"kind": "material", "material_id": self.material.currentData(),
                "optical_set_id": self.optical_set.currentData()}

    def refresh_options(self, options):
        record = self.current_medium(); self.options = options
        self._updating = True
        self.material.clear(); self.material.addItem("请选择材料", "")
        for item in options: self.material.addItem(item["name"], item["id"])
        self._updating = False; self.set_medium(record)

class _WindowChromeHost(QObject):
    """Adapt a QWidget window to the project's shared Qt Quick chrome contract."""

    titleChanged = Signal()
    geometryChanged = Signal()
    visibilityChanged = Signal()
    activeChanged = Signal()
    paletteChanged = Signal()

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self._native = None
        window.windowTitleChanged.connect(lambda *_: self.titleChanged.emit())
        self._bind_native()
        window.installEventFilter(self)
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.paletteChanged.connect(self._application_palette_changed)

    @Slot(QPalette)
    def _application_palette_changed(self, _palette: QPalette) -> None:
        # A QObject receiver is disconnected when this host is destroyed. An
        # application-owned lambda would retain and call the deleted host.
        self.paletteChanged.emit()

    def _bind_native(self):
        from shiboken6 import isValid
        current = self.window.windowHandle() if isValid(self.window) else None
        if current is not None and not isValid(current):
            current = None
        if current is self._native:
            return current
        self._native = current
        if current is not None:
            for signal in (current.xChanged, current.yChanged, current.widthChanged,
                           current.heightChanged, current.screenChanged):
                signal.connect(lambda *_: self.geometryChanged.emit())
            current.visibilityChanged.connect(lambda *_: self.visibilityChanged.emit())
            current.activeChanged.connect(lambda *_: self.activeChanged.emit())
        return current

    @property
    def native(self):
        # QWidget may destroy/recreate its QWindow after close or flag changes.
        return self._bind_native()

    def eventFilter(self, watched, event):
        if watched is self.window and event.type() == QEvent.WinIdChange:
            self._bind_native()
            self.geometryChanged.emit()
            self.visibilityChanged.emit()
            self.activeChanged.emit()
        if watched is self.window and event.type() in (QEvent.ActivationChange, QEvent.PaletteChange,
                                                       QEvent.ApplicationPaletteChange):
            self.activeChanged.emit()
            self.paletteChanged.emit()
        return False

    @Property(str, notify=titleChanged)
    def title(self):
        return self.window.windowTitle()

    @Property(bool, constant=True)
    def useCustomBackground(self):
        return False

    @Property(bool, notify=activeChanged)
    def active(self):
        native = self.native
        return native is not None and native.isActive()

    @Property("QVariantMap", notify=paletteChanged)
    def palette(self):
        palette = self.window.palette()
        def role(group, name):
            return palette.color(group, getattr(QPalette.ColorRole, name)).name(QColor.NameFormat.HexArgb)
        return {
            "active": {"windowText": role(QPalette.ColorGroup.Active, "WindowText"),
                       "mid": role(QPalette.ColorGroup.Active, "Mid"),
                       "highlight": role(QPalette.ColorGroup.Active, "Highlight")},
            "inactive": {"windowText": role(QPalette.ColorGroup.Inactive, "WindowText"),
                         "mid": role(QPalette.ColorGroup.Inactive, "Mid"),
                         "highlight": role(QPalette.ColorGroup.Inactive, "Highlight")},
        }

    @Property(int, notify=geometryChanged)
    def x(self):
        return self.window.x()

    @x.setter
    def x(self, value):
        self.window.move(int(value), self.window.y())

    @Property(int, notify=geometryChanged)
    def y(self):
        return self.window.y()

    @y.setter
    def y(self, value):
        self.window.move(self.window.x(), int(value))

    @Property(int, notify=geometryChanged)
    def width(self):
        return self.window.width()

    @Property(int, notify=geometryChanged)
    def height(self):
        return self.window.height()

    @Property(int, notify=visibilityChanged)
    def visibility(self):
        native = self.native
        return native.visibility().value if native is not None else 0

    @Property("QVariantMap", notify=geometryChanged)
    def screen(self):
        native = self.native
        screen = native.screen() if native is not None else None
        if screen is None:
            return {"availableGeometry": {"y": 0}}
        return {"availableGeometry": {"y": screen.availableGeometry().y()}}

    @Slot(result=bool)
    def startSystemMove(self):
        native = self.native
        return native is not None and bool(native.startSystemMove())

    @Slot(int, result=bool)
    def startSystemResize(self, edges):
        native = self.native
        return native is not None and bool(native.startSystemResize(Qt.Edges(edges)))

    @Slot()
    def showMinimized(self):
        self.window.showMinimized()

    @Slot()
    def showMaximized(self):
        self.window.showMaximized()

    @Slot()
    def showNormal(self):
        self.window.showNormal()

    @Slot()
    def close(self):
        self.window.close()


class _FilmChromeWidget(QQuickWidget):
    """Keep the shared QML chrome while using native titlebar mouse semantics."""

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self._drag_anchor = None
        self._drag_window_pos = None
        self._drag_started = False
        self._system_move = False
        self.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)

    def _in_drag_area(self, event):
        root = self.rootObject()
        if root is None:
            return False
        probe = root.findChild(QObject, "windowChromeTitleDrag")
        if probe is None:
            return False
        point = event.position()
        x, y = float(probe.property("x")), float(probe.property("y"))
        width, height = float(probe.property("width")), float(probe.property("height"))
        return x <= point.x() < x + width and y <= point.y() < y + height

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._in_drag_area(event):
            # A click must remain a click until the pointer actually moves. Starting
            # the Windows move loop here consumes the second click of a double-click.
            self._drag_anchor = event.globalPosition().toPoint()
            self._drag_window_pos = self.owner.pos()
            self._drag_started = False
            self._system_move = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_anchor is None:
            super().mouseMoveEvent(event)
            return
        current = event.globalPosition().toPoint()
        delta = current - self._drag_anchor
        if not self._drag_started and delta.manhattanLength() > 5:
            self._drag_started = True
            if self.owner.isMaximized() or self.owner.isFullScreen():
                pressed_x = self._drag_anchor.x() - self.owner.x()
                self.owner.showNormal()
                self.owner.move(current.x() - min(int(self.owner.width() * .45), pressed_x),
                                current.y() - 24)
                self._drag_anchor = current
                self._drag_window_pos = self.owner.pos()
                delta = current - self._drag_anchor
            native = self.owner.windowHandle()
            if native is not None and QApplication.platformName().lower() not in ("offscreen", "minimal"):
                self._system_move = bool(native.startSystemMove())
        if self._drag_started and not self._system_move:
            self.owner.move(self._drag_window_pos + delta)
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._drag_anchor is not None:
            if self._drag_started and not self._system_move and not self.owner.isMaximized():
                screen = self.owner.screen()
                if screen is not None and self.owner.y() <= screen.availableGeometry().y() + 12:
                    self.owner.showMaximized()
            self._drag_anchor = None
            self._drag_started = False
            self._system_move = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._in_drag_area(event):
            self._drag_anchor = None
            self._drag_started = False
            self._system_move = False
            if self.owner.isMaximized() or self.owner.isFullScreen():
                self.owner.showNormal()
            else:
                self.owner.showMaximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class _FilmWindow(QMainWindow):
    """A document bridge also used by the launcher's UI automation adapter."""
    closed = Signal()
    def __init__(self, root=None, service=None):
        super().__init__()
        if service is None:
            from coat_sim.core.film_design_service import FilmDesignService
            service = FilmDesignService(root or PROJECT_ROOT, restrict_to_root=False)
        self.service = service
        self._design = self.service.new_design()
        self._path = None
        self.dirty = False
        self.busy = False
        self.error = ""
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.resize(1180, 800)
        self.winId()
        self._titlebar_host = _WindowChromeHost(self)
        self._titlebar_widget = _FilmChromeWidget(self)
        from coat_sim.ui.theme_service import ThemeService

        ThemeService.instance().attach_engine(self._titlebar_widget.engine())
        self._titlebar_widget.setFixedHeight(40)
        self._titlebar_host.paletteChanged.connect(self._update_chrome_clear_color)
        self._titlebar_host.activeChanged.connect(self._update_chrome_clear_color)
        self._update_chrome_clear_color()
        chrome_path = PROJECT_ROOT / "coat_sim" / "ui" / "qml" / "WindowChrome.qml"
        self._titlebar_widget.setInitialProperties({"hostWindow": self._titlebar_host})
        self._titlebar_widget.setSource(QUrl.fromLocalFile(str(chrome_path)))
        chrome = self._titlebar_widget.rootObject()
        if chrome is None:
            raise RuntimeError("The shared WindowChrome component failed to load.")
        chrome.setProperty("hostWindow", self._titlebar_host)
        self.setMenuWidget(self._titlebar_widget)

    def _update_chrome_clear_color(self):
        group = QPalette.ColorGroup.Active if self.isActiveWindow() else QPalette.ColorGroup.Inactive
        self._titlebar_widget.setClearColor(self.palette().color(group, QPalette.ColorRole.Window))

    def _guard(self, action):
        try:
            self.error = ""
            result = action()
            return result
        except Exception as exc:
            self.error = str(exc)
            self.statusBar().showMessage(self.error)
            QMessageBox.warning(self, "膜系设计", self.error)
            return None

    def _load_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "加载膜系", str(Path(self.service.root) / "data"), f"膜系设计 (*{design_extension('film')});;全部文件 (*)")

        if path and self._confirm_replace():
            self._guard(lambda: self.load_path(path))

    def _confirm_replace(self):
        return not self.dirty or QMessageBox.question(self, "未保存修改",
            "丢弃当前未保存的膜系修改并继续？", QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No) == QMessageBox.Yes

    def load_path(self, path):
        self.set_design(self.service.load(path))
        self._path = str(path)
        self.dirty = False

    def current_design(self):
        return copy.deepcopy(self._design)

    def snapshot(self):
        try:
            data, error = self.current_design(), self.error
        except (ValueError, TypeError) as exc:
            data, error = None, str(exc)
        return {"dirty": self._has_dirty(), "busy": self.busy, "design_path": self._path,
                "data": data, "error": error}

    def _has_dirty(self):
        return self.dirty or any(library.dirty for _, library, _ in getattr(self, "_material_hosts", {}).values())

    def closeEvent(self, event):
        if self._has_dirty() and not getattr(self, "_force_close", False):
            answer = QMessageBox.question(self, "未保存修改", "关闭并丢弃膜系设计或材料库中的未保存修改？",
                                          QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        for engine, library, window in list(getattr(self, "_material_hosts", {}).values()):
            window.close()
            library.cancelPendingLoad()
            engine.deleteLater()
            library.deleteLater()
        if hasattr(self, "_material_hosts"):
            self._material_hosts.clear()
        event.accept()
        self.closed.emit()


class FilmDesignWindow(_FilmWindow):
    simulationRequested = Signal(object)

    def __init__(self, root=None, service=None):
        super().__init__(root, service)
        self.setWindowTitle("设计")
        self._updating = False
        self.film_options = self.service.material_options(purpose="film")
        self.substrate_options = self.service.material_options(purpose="substrate")
        self._material_hosts = {}
        self._schematic_dialog = None
        self._design = self._new_design()
        container = QWidget(); self.setCentralWidget(container)
        layout = QVBoxLayout(container)
        bar = QHBoxLayout()
        for label, name, action in [("新建", "new_design_button", self.new_document),
                                   ("加载", "load_design_button", self._load_dialog),
                                   ("保存", "save_design_button", self._save_dialog),
                                   ("发送到仿真计算", "send_simulation_button", self._send_simulation)]:
            button = QPushButton(label); button.setObjectName(name); button.clicked.connect(action); bar.addWidget(button)
        self.name_edit = QLineEdit(); self.name_edit.setPlaceholderText("膜系名称")
        self.name_edit.textEdited.connect(self._changed); bar.addWidget(self.name_edit)
        self.save_library_button = QPushButton("保存到设计库"); self.save_library_button.setObjectName("save_film_library_button")
        self.save_library_button.clicked.connect(self._save_to_library); bar.addWidget(self.save_library_button)
        self.load_library_button = QPushButton("从设计库加载"); self.load_library_button.setObjectName("load_film_library_button")
        self.load_library_button.clicked.connect(self._load_from_library); bar.addWidget(self.load_library_button)
        self.schematic_button = QPushButton("独立结构示意"); self.schematic_button.setObjectName("film_schematic_button")
        self.schematic_button.clicked.connect(self._show_schematic); bar.addWidget(self.schematic_button)
        layout.addLayout(bar)
        metadata = QHBoxLayout(); metadata.addWidget(QLabel("备注"))
        self.note_edit = QLineEdit(); self.note_edit.setObjectName("film_design_note")
        self.note_edit.setPlaceholderText("设计说明（可选）"); self.note_edit.textEdited.connect(self._changed)
        metadata.addWidget(self.note_edit, 1); layout.addLayout(metadata)
        self.substrate_material = self._materials(self.substrate_options)
        self.substrate_set = QComboBox()
        self.substrate_thickness = _number(1)
        self.substrate_unit = _combo([(x, x) for x in ("nm", "um", "mm")])
        self.sides = _combo([("单面", "single"), ("双面", "double")])
        self.mode = _combo([("自动判定", "auto")]); self.mode.hide()
        self.auto_substrate_hint = QLabel(); self.auto_substrate_hint.setWordWrap(True)
        self.lossless = QCheckBox("旧版无损标记（仅保留文件兼容）"); self.lossless.hide()
        self.use_k = QCheckBox("计算使用材料 k 值"); self.use_k.setObjectName("use_k_checkbox")
        self.reference = _number(550, minimum=1)
        media_options = list({item["id"]: item for item in self.film_options + self.substrate_options}.values())
        self.incident_medium = _MediumEditor(media_options)
        self.exit_medium = _MediumEditor(media_options)
        self.incident_n, self.exit_n = self.incident_medium.n, self.exit_medium.n
        self.property_tabs = QTabWidget(); self.property_tabs.setObjectName("film_property_tabs")
        stack_page = QWidget(); stack_layout = QVBoxLayout(stack_page)
        substrate_page = QWidget(); form = QFormLayout(substrate_page)
        row = QHBoxLayout(); row.addWidget(self.substrate_material); row.addWidget(self.substrate_set)
        form.addRow("基片材料 / 光学常数组", row)
        row = QHBoxLayout(); row.addWidget(self.substrate_thickness); row.addWidget(self.substrate_unit); row.addWidget(self.sides)
        form.addRow("实际厚度 / 面数", row)
        form.addRow("基底处理", self.auto_substrate_hint)
        row = QHBoxLayout(); row.addWidget(self.reference); row.addWidget(QLabel("nm（qw参考波长）")); row.addWidget(self.use_k)
        form.addRow("光学计算", row)
        form.addRow("入射介质", self.incident_medium)
        form.addRow("出射介质", self.exit_medium)
        self.property_tabs.addTab(stack_page, "膜堆"); self.property_tabs.addTab(substrate_page, "基片与介质")
        layout.addWidget(self.property_tabs, 1)
        self.substrate_material.currentIndexChanged.connect(self._substrate_material_changed)
        for box in (self.substrate_set, self.substrate_unit, self.sides, self.mode):
            box.currentIndexChanged.connect(self._changed)
        for box in (self.substrate_thickness, self.reference):
            box.valueChanged.connect(self._changed)
        self.lossless.toggled.connect(self._changed); self.use_k.toggled.connect(self._changed)
        self.incident_medium.changed.connect(self._changed)
        self.exit_medium.changed.connect(self._changed)
        stack_layout.addWidget(QLabel("层序：每个表从基片向外排列；入射端在最外层。"))
        self.layer_tabs = QTabWidget(); self.tables = {}
        for side, label in (("front", "正面膜堆"), ("back", "背面膜堆")):
            table = QTableWidget(0, 7); table.setObjectName(side + "_layers_table")
            table.setHorizontalHeaderLabels(["材料", "光学常数组", "厚度", "单位", "层 ID", "QW", "物理厚度（nm）"])
            for derived in (5, 6):
                table.horizontalHeader().setSectionResizeMode(derived, QHeaderView.ResizeToContents)
            table.hideColumn(4)
            table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
            table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
            table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
            table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
            for column, width in ((0, 130), (1, 170), (2, 100), (3, 64)): table.setColumnWidth(column, width)
            table.itemChanged.connect(self._changed)
            self.tables[side] = table; self.layer_tabs.addTab(table, label)
        stack_layout.addWidget(self.layer_tabs, 1)
        row = QHBoxLayout()
        for label, action in (("添加层", self.add_layer), ("删除层", self.delete_layer),
                              ("上移", lambda: self.move_layer(-1)), ("下移", lambda: self.move_layer(1))):
            button = QPushButton(label); button.clicked.connect(action); row.addWidget(button)
        stack_layout.addLayout(row)
        expression_box = QGroupBox("膜堆表达式"); expression_layout = QVBoxLayout(expression_box)
        self.expression_toggle = QToolButton(); self.expression_toggle.setObjectName("expression_expand_toggle")
        self.expression_toggle.setText("TFCalc 风格严格子集：字母、乘数、括号项整数次幂、G组；左侧邻基片")
        self.expression_toggle.setCheckable(True); self.expression_toggle.setChecked(False)
        self.expression_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon); self.expression_toggle.setArrowType(Qt.RightArrow)
        expression_layout.addWidget(self.expression_toggle)
        self.expression_body = QWidget(); body_layout = QVBoxLayout(self.expression_body)
        row = QHBoxLayout(); self.expression_edit = QPlainTextEdit(); self.expression_edit.setObjectName("stack_expression")
        self.expression_edit.setPlaceholderText("例如 G1 (HL)^5 G2 (HL)^5\n或 1.2 (0.5 A B 0.5 C)^2")
        self.expression_edit.setMaximumHeight(90); self.expression_edit.textChanged.connect(self._expression_changed)
        row.addWidget(self.expression_edit)
        expand = QPushButton("展开到当前膜堆"); expand.setObjectName("expand_expression_button"); expand.clicked.connect(lambda: self._guard(self.expand_expression)); row.addWidget(expand)
        body_layout.addLayout(row)
        self.symbols_table = QTableWidget(0, 5); self.symbols_table.setObjectName("expression_symbols")
        self.symbols_table.setHorizontalHeaderLabels(["符号 A–Z", "材料", "光学常数组", "厚度", "单位"])
        self.symbols_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.symbols_table.setMaximumHeight(135); self.symbols_table.itemChanged.connect(self._expression_changed)
        body_layout.addWidget(self.symbols_table)
        row = QHBoxLayout(); add_symbol = QPushButton("添加符号"); add_symbol.clicked.connect(self.add_symbol)
        row.addWidget(add_symbol)
        delete_symbol = QPushButton("删除符号"); delete_symbol.clicked.connect(self.delete_symbol)
        row.addWidget(delete_symbol)
        self.expression_status = QLabel(); row.addWidget(self.expression_status); row.addStretch()
        body_layout.addLayout(row)
        template_actions = QHBoxLayout()
        self.save_expression_template_button = QPushButton("保存表达式模板")
        self.save_expression_template_button.setObjectName("save_expression_template_button")
        self.save_expression_template_button.clicked.connect(
            lambda: self._guard(self._save_expression_template))
        self.load_expression_template_button = QPushButton("读取表达式模板")
        self.load_expression_template_button.setObjectName("load_expression_template_button")
        self.load_expression_template_button.clicked.connect(
            lambda: self._guard(self._load_expression_template))
        template_actions.addWidget(self.save_expression_template_button)
        template_actions.addWidget(self.load_expression_template_button)
        template_actions.addStretch()
        body_layout.addLayout(template_actions)
        expression_layout.addWidget(self.expression_body)
        self.expression_body.setVisible(False); self.expression_toggle.toggled.connect(self._toggle_expression)
        stack_layout.addWidget(expression_box)
        self.set_design(self._design)

    @staticmethod
    def _materials(options):
        return _combo([("请选择", "")] + [(item["name"], item["id"]) for item in options])

    @staticmethod
    def _sets(box, options, material_id, selected=""):
        box.clear(); box.addItem("请选择光学常数组", "")
        item = next((x for x in options if x["id"] == material_id), {})
        for record in item.get("optical_sets", []):
            box.addItem(record.get("name", record.get("id", "")), record.get("id", ""))
            tip = (str(record.get("notes", "")) + "\n有效波段：" +
                   str(record.get("wavelength_min", "")) + "–" + str(record.get("wavelength_max", "")) +
                   " " + str(record.get("wavelength_unit", "")) + "\n来源：" + str(record.get("source", "")))
            box.setItemData(box.count()-1, tip, Qt.ToolTipRole)
        _select(box, selected)

    def _substrate_material_changed(self):
        if self._updating: return
        self._sets(self.substrate_set, self.substrate_options, self.substrate_material.currentData())
        self._changed()

    def _row(self, table, layer, symbol=False):
        row = table.rowCount(); table.insertRow(row)
        offset = 1 if symbol else 0
        if symbol:
            table.setItem(row, 0, QTableWidgetItem(layer.get("symbol", "A")))
            table.item(row, 0).setData(Qt.UserRole, copy.deepcopy(layer))
        material = self._materials(self.film_options); sets = QComboBox()
        for box in (material, sets):
            box.setMinimumContentsLength(10)
            box.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        _select(material, layer.get("material_id", ""))
        self._sets(sets, self.film_options, material.currentData(), layer.get("optical_set_id", ""))
        table.setCellWidget(row, offset, material); table.setCellWidget(row, offset+1, sets)
        def material_changed():
            if self._updating: return
            self._sets(sets, self.film_options, material.currentData())
            (self._expression_changed if symbol else self._changed)()
        material.currentIndexChanged.connect(material_changed)
        sets.currentIndexChanged.connect(self._expression_changed if symbol else self._changed)
        thickness = layer.get("thickness", {"value": 100, "unit": "nm"})
        table.setItem(row, offset+2, QTableWidgetItem(str(thickness["value"])))
        unit = _combo([(x, x) for x in ("nm", "um", "qw")]); _select(unit, thickness["unit"])
        unit.currentIndexChanged.connect(self._expression_changed if symbol else self._changed)
        table.setCellWidget(row, offset+3, unit)
        if not symbol:
            table.setItem(row, 4, QTableWidgetItem(layer.get("id", "")))
            table.item(row, 4).setData(Qt.UserRole, copy.deepcopy(layer))

    def _read_rows(self, table, symbol=False):
        result = []; offset = 1 if symbol else 0
        for row in range(table.rowCount()):
            def text(column):
                item = table.item(row, column)
                return item.text() if item else ""
            record = {"material_id": table.cellWidget(row, offset).currentData(),
                      "optical_set_id": table.cellWidget(row, offset+1).currentData(),
                      "thickness": {"value": float(text(offset+2)), "unit": table.cellWidget(row, offset+3).currentData()}}
            if symbol:
                metadata = table.item(row, 0).data(Qt.UserRole) or {}
                record = dict(metadata, **record, symbol=text(0).strip())
            else:
                metadata = table.item(row, 4).data(Qt.UserRole) or {}
                record = dict(metadata, **record, id=text(4), note=metadata.get("note", ""))
            result.append(record)
        return result

    def current_design(self):
        design = copy.deepcopy(self._design)
        design["name"] = self.name_edit.text()
        design["note"] = self.note_edit.text()
        design["reference_wavelength_nm"] = self.reference.value()
        design["assume_lossless"] = self.lossless.isChecked()
        design["use_k"] = self.use_k.isChecked()
        design["substrate"].update(material_id=self.substrate_material.currentData(), optical_set_id=self.substrate_set.currentData(),
            thickness={"value": self.substrate_thickness.value(), "unit": self.substrate_unit.currentData()},
            sides=self.sides.currentData(), mode="auto")
        design["incident_medium"] = self.incident_medium.current_medium()
        design["exit_medium"] = self.exit_medium.current_medium()
        for side, table in self.tables.items():
            design[side + "_layers"] = self._read_rows(table)
        symbols = {}
        for record in self._read_rows(self.symbols_table, True):
            symbol = record.pop("symbol")
            if symbol in symbols:
                raise ValueError("表达式符号重复：" + symbol)
            symbols[symbol] = record
        design["expression"].update(text=self.expression_edit.toPlainText(), symbols=symbols)
        return design

    def set_design(self, design):
        self._updating = True
        try:
            self._design = copy.deepcopy(design)
            self.name_edit.setText(design.get("name", ""))
            self.note_edit.setText(design.get("note", ""))
            self.reference.setValue(design.get("reference_wavelength_nm", 550))
            self.lossless.setChecked(design.get("assume_lossless", False))
            self.use_k.setChecked(design.get("use_k", True))
            substrate = design["substrate"]
            _select(self.substrate_material, substrate["material_id"])
            self._sets(self.substrate_set, self.substrate_options, substrate["material_id"], substrate["optical_set_id"])
            self.substrate_thickness.setValue(substrate["thickness"]["value"])
            for box, value in ((self.substrate_unit, substrate["thickness"]["unit"]), (self.sides, substrate["sides"])):
                _select(box, value)
            self.incident_medium.set_medium(design.get("incident_medium", {"kind": "constant", "n": 1}))
            self.exit_medium.set_medium(design.get("exit_medium", {"kind": "constant", "n": 1}))
            for side, table in self.tables.items():
                table.setRowCount(0)
                for layer in design.get(side + "_layers", []): self._row(table, layer)
            expression = design.get("expression", {})
            self.expression_edit.setPlainText(expression.get("text", "")); self.symbols_table.setRowCount(0)
            for symbol, layer in expression.get("symbols", {}).items(): self._row(self.symbols_table, dict(layer, symbol=symbol), True)
        finally:
            self._updating = False
        self.dirty = False; self._refresh()

    def _changed(self, *args):
        if self._updating: return
        self.dirty = True
        if self._design.get("expression", {}).get("text"):
            self._design["expression"]["in_sync"] = False
        self._refresh()

    def _expression_changed(self, *args):
        if self._updating: return
        raw = self.expression_edit.toPlainText()
        normalized = raw.translate(str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
        if normalized != raw:
            cursor = self.expression_edit.textCursor(); position = cursor.position()
            blocker = QSignalBlocker(self.expression_edit)
            self.expression_edit.setPlainText(normalized)
            cursor = self.expression_edit.textCursor(); cursor.setPosition(min(position, len(normalized)))
            self.expression_edit.setTextCursor(cursor); del blocker
        self.dirty = True; self._design["expression"]["in_sync"] = False; self._refresh()

    def _toggle_expression(self, expanded):
        self.expression_body.setVisible(bool(expanded))
        self.expression_toggle.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)

    def _refresh(self):
        expression = self._design.get("expression", {})
        for index, side in enumerate(("front", "back")):
            label = "正面膜堆" if side == "front" else "背面膜堆"
            self.layer_tabs.setTabText(index, f"{label}（{self.tables[side].rowCount()}层）")
        self.layer_tabs.setTabEnabled(1, self.sides.currentData() == "double")
        single = self.sides.currentData() == "single"
        self.exit_medium.setEnabled(not single)
        self.auto_substrate_hint.setText("单面：半无限基底；出射介质与背面设计不参与计算。" if single else
            "双面：保留实际厚度与后界面；d > 10×计算波段最大波长时采用平均条纹近似，否则完整相干计算。")
        self._refresh_thickness_columns()
        if self._schematic_dialog is not None and self._schematic_dialog.isVisible():
            self._update_schematic()

    def _refresh_thickness_columns(self):
        """Derived read-only values; one n evaluation per unique reference."""
        from coat_sim.core.optical_constants import prepare_optical_set
        from coat_sim.core.film_design import physical_thickness_nm
        reference = self.reference.value()
        options = {item['id']:item for item in self.film_options}
        indices = {}
        for table in self.tables.values():
            blocker = QSignalBlocker(table)
            for row in range(table.rowCount()):
                physical = None; qw = None; reason = ''
                try:
                    raw = {'thickness':{'value':float(table.item(row,2).text()),
                           'unit':table.cellWidget(row,3).currentData()}}
                    if raw['thickness']['unit'] != 'qw':
                        physical = physical_thickness_nm(raw, reference, None)
                    key = (table.cellWidget(row,0).currentData(),table.cellWidget(row,1).currentData())
                    if key not in indices:
                        try:
                            material = options.get(key[0], {})
                            record = next(r for r in material.get('optical_sets',[]) if r['id']==key[1])
                            n, _k, _details = prepare_optical_set(record)(reference,'nm',include_k=False)
                            indices[key] = n
                        except (ValueError, StopIteration) as exc:
                            indices[key] = ValueError('参考波长处 n 不可用：' + str(exc))
                    n = indices[key]
                    if isinstance(n, Exception): raise n
                    if raw['thickness']['unit']=='qw':
                        physical = physical_thickness_nm(raw, reference, n)
                    qw = 4.0 * n * physical / reference
                except (ValueError, TypeError, AttributeError) as exc:
                    reason = str(exc)
                for column, value in ((5,qw),(6,physical)):
                    item = QTableWidgetItem('不可用' if value is None else f'{value:.6g}')
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    item.setToolTip(reason if value is None else 'QW = 4 Re[n(参考波长)] d / 参考波长；派生显示不修改厚度输入。')
                    table.setItem(row,column,item)
            del blocker
        self._thickness_reference_indices = (reference, indices)

    def _layer_option(self, layer):
        return next((item for item in self.film_options if item.get("id") == layer.get("material_id")), {})

    def _layer_thickness_nm(self, layer, design):
        from coat_sim.core.film_design import physical_thickness_nm
        n_reference = None
        if layer["thickness"]["unit"] == "qw":
            reference, indices = getattr(self, '_thickness_reference_indices', (None, {}))
            key = (layer['material_id'], layer['optical_set_id'])
            if reference == design['reference_wavelength_nm'] and key in indices:
                value = indices[key]
                if isinstance(value, Exception): raise value
                n_reference = float(value)
            else:
                value = self.service.materials.evaluate(layer['material_id'], layer['optical_set_id'],
                    design['reference_wavelength_nm'], assume_lossless=True)
                n_reference = float(complex(value).real)
        return physical_thickness_nm(layer, design["reference_wavelength_nm"], n_reference)

    @staticmethod
    def _medium_label(medium):
        if medium.get("kind", "constant") == "constant":
            n = float(medium.get("n", 1))
            if n == 1.0:
                return "空气近似（n=1）"
            return f"n={n:.6g}"
        return f"{medium.get('material_id', '?')} / {medium.get('optical_set_id', '?')}"

    def _draw_stack_schematic(self, figure, design):
        from matplotlib.patches import Rectangle
        ax = figure.add_subplot(111); ax.set_axis_off(); ax.set_ylim(0, 1)
        front = design.get("front_layers", []); back = design.get("back_layers", []) if design["substrate"]["sides"] == "double" else []
        scales = {side: [self._layer_thickness_nm(row, design) for row in rows] for side, rows in (("front", front), ("back", back))}
        totals = [sum(values) for values in scales.values() if values]
        scale = .33 / max(totals, default=1.0)
        substrate = design["substrate"]
        sub = next((item for item in self.substrate_options if item.get("id") == substrate.get("material_id")), {})
        subset = next((item for item in sub.get("optical_sets", []) if item.get("id") == substrate.get("optical_set_id")), {})
        ax.add_patch(Rectangle((.40, .33), .20, .34, facecolor="#bac9d8", edgecolor="#34495e"))
        substrate_label = sub.get("name", "基片未选") + "\n" + subset.get("name", "")
        if subset.get("wavelength_unit") in ("nm", "um") and subset.get("wavelength_min") is not None:
            substrate_label += f"\n{float(subset['wavelength_min']):g}–{float(subset['wavelength_max']):g} {('µm' if subset['wavelength_unit'] == 'um' else 'nm')}"
        ax.text(.50, .50, substrate_label, ha="center", va="center", fontsize=9, wrap=True, **_film_font_kwargs())
        colors = ("#4a91c2", "#e1ac51", "#78ae99", "#a38ac4", "#d08080", "#82a8ba")
        for side, rows, sign in (("front", front, -1), ("back", back, 1)):
            edge = .40 if sign < 0 else .60
            for idx, (layer, thickness) in enumerate(zip(rows, scales[side])):
                width = max(0.002, thickness * scale)
                if sign < 0: edge -= width; x = edge
                else: x = edge; edge += width
                ax.add_patch(Rectangle((x, .33), width, .34, facecolor=colors[idx % len(colors)], edgecolor="white"))
            medium = design.get("incident_medium", {"kind": "constant", "n": 1}) if (design.get("first_surface", "front") == side) else design.get("exit_medium", {"kind": "constant", "n": 1})
            side_name = "正面" if side == "front" else "背面"
            ax.text(.20 if sign < 0 else .80, .88, f"{side_name}侧 · {('入射' if design.get('first_surface', 'front') == side else '出射')}介质\n{self._medium_label(medium)}", ha="center", va="center", fontsize=9, **_film_font_kwargs())
            label_x = .20 if sign < 0 else .80
            text = []
            for idx, (layer, thickness) in enumerate(zip(rows, scales[side]), 1):
                item = self._layer_option(layer); optical = next((o for o in item.get("optical_sets", []) if o.get("id") == layer.get("optical_set_id")), {})
                source_domain = ""
                if optical.get("wavelength_unit") in ("nm", "um") and optical.get("wavelength_min") is not None:
                    source_domain = f" [{float(optical['wavelength_min']):g}–{float(optical['wavelength_max']):g} {('µm' if optical['wavelength_unit'] == 'um' else 'nm')}]"
                text.append(f"L{idx} {item.get('name', layer.get('material_id') or '未选')} / {optical.get('name', layer.get('optical_set_id') or '未选')}{source_domain} · {thickness:.6g} nm")
            ax.text(label_x, .23, "\n".join(text) if text else "（无膜层）", ha="center", va="top", fontsize=8, wrap=True, **_film_font_kwargs())
        ax.text(.5, .08, "层宽表示相对物理厚度；单位已统一为 nm。参考波长 " + f"{design['reference_wavelength_nm']:g} nm", ha="center", fontsize=8, **_film_font_kwargs())
        ax.set_xlim(0, 1); ax.set_title("层序：基片向外；独立示意按物理厚度比例绘制", **_film_font_kwargs())

    def _update_schematic(self):
        if self._schematic_dialog is None:
            return
        figure = self._schematic_figure
        figure.clear()
        try:
            self._draw_stack_schematic(figure, self.current_design())
        except Exception as exc:
            ax = figure.add_subplot(111)
            ax.text(.5, .5, "示意图暂时无法生成\n" + str(exc),
                    ha="center", va="center", wrap=True, **_film_font_kwargs())
            ax.set_axis_off()
        figure.tight_layout()
        self._schematic_canvas.draw_idle()

    def _show_schematic(self):
        if self._schematic_dialog is None:
            dialog = QDialog(self)
            dialog.setWindowTitle("膜堆结构示意")
            dialog.resize(1100, 620)
            dialog.setAttribute(Qt.WA_DeleteOnClose, False)
            layout = QVBoxLayout(dialog)
            self._schematic_figure = Figure(figsize=(11, 5.5))
            self._schematic_canvas = FigureCanvasQTAgg(self._schematic_figure)
            layout.addWidget(self._schematic_canvas, 1)
            buttons = QDialogButtonBox(QDialogButtonBox.Close)
            buttons.button(QDialogButtonBox.Close).setText("关闭")
            buttons.rejected.connect(dialog.close)
            buttons.accepted.connect(dialog.close)
            layout.addWidget(buttons)
            self._schematic_dialog = dialog
        self._update_schematic()
        dialog = self._schematic_dialog
        if dialog.isMinimized():
            dialog.showNormal()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _new_design(self):
        design = self.service.new_design()
        material = next((item for item in self.substrate_options if item.get("optical_sets")), None)
        if material:
            design["substrate"].update(material_id=material["id"], optical_set_id=material["optical_sets"][0]["id"])
        return design

    def new_document(self):
        if self._confirm_replace():
            self.set_design(self._new_design()); self._path = None

    def add_layer(self):
        import uuid
        table = self.layer_tabs.currentWidget()
        self._updating = True
        self._row(table, {"id": uuid.uuid4().hex, "material_id": "", "optical_set_id": "", "thickness": {"value": 100, "unit": "nm"}})
        self._updating = False; self._changed()

    def delete_layer(self):
        table = self.layer_tabs.currentWidget()
        if table.currentRow() >= 0: table.removeRow(table.currentRow()); self._changed()

    def move_layer(self, step):
        table = self.layer_tabs.currentWidget(); row = table.currentRow(); target = row + step
        if row < 0 or target < 0 or target >= table.rowCount(): return
        layers = self._read_rows(table); layers[row], layers[target] = layers[target], layers[row]
        self._updating = True; table.setRowCount(0)
        for layer in layers: self._row(table, layer)
        self._updating = False; table.selectRow(target); self._changed()

    def delete_symbol(self):
        row = self.symbols_table.currentRow()
        if row >= 0:
            self.symbols_table.removeRow(row); self._expression_changed()

    def add_symbol(self):
        if self.symbols_table.rowCount() >= 26:
            self.statusBar().showMessage("最多支持 26 个 A–Z 符号"); return
        self._updating = True
        used = {self.symbols_table.item(row, 0).text() for row in range(self.symbols_table.rowCount())}
        symbol = next(char for char in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if char not in used)
        self._row(self.symbols_table, {"symbol": symbol, "thickness": {"value": 1, "unit": "qw"}}, True)
        self._updating = False; self._expression_changed()

    def _save_expression_template(self):
        name, accepted = QInputDialog.getText(self, "保存表达式模板", "模板名称")
        if not accepted:
            return None
        note, accepted = QInputDialog.getMultiLineText(
            self, "表达式模板备注", "备注")
        if not accepted:
            return None
        import uuid
        expression = self.current_design()["expression"]
        template = {
            "schema_version": 1,
            "template_id": uuid.uuid4().hex,
            "name": name,
            "note": note,
            "expression": {
                "text": expression["text"],
                "symbols": copy.deepcopy(expression["symbols"]),
            },
        }
        saved = self.service.save_expression_template(template)
        self.statusBar().showMessage("表达式模板已保存：" + saved["name"])
        return saved

    def _apply_expression_template(self, template):
        expression = template["expression"]
        self._updating = True
        try:
            self.expression_edit.setPlainText(expression["text"])
            self.symbols_table.setRowCount(0)
            for symbol, layer in expression["symbols"].items():
                self._row(self.symbols_table, dict(layer, symbol=symbol), True)
        finally:
            self._updating = False
        self._design["expression"] = {
            "text": expression["text"],
            "symbols": copy.deepcopy(expression["symbols"]),
            "in_sync": False,
        }
        self.dirty = True
        self._refresh()
        side = "front" if self.layer_tabs.currentIndex() == 0 else "back"
        self.statusBar().showMessage(
            f"表达式模板已加载到当前{'正面' if side == 'front' else '背面'}表达式编辑器")

    def _load_expression_template(self):
        templates = self.service.list_expression_templates()
        if not templates:
            QMessageBox.information(self, "表达式模板", "尚未保存表达式模板。")
            return None
        labels = [f"{row['name']} — {row['note']} [{row['template_id']}]" for row in templates]
        label, accepted = QInputDialog.getItem(
            self, "读取表达式模板", "选择模板", labels, 0, False)
        if not accepted:
            return None
        template_id = templates[labels.index(label)]["template_id"]
        template = self.service.load_expression_template(template_id)
        self._apply_expression_template(template)
        return template

    def expand_expression(self):
        design = self.current_design(); expression = design["expression"]
        layers = self.service.expand(expression["text"], expression["symbols"])
        side = "front" if self.layer_tabs.currentIndex() == 0 else "back"
        design[side + "_layers"] = layers; expression["in_sync"] = True
        self.set_design(design); self.dirty = True

    def _save_to_library(self):
        saved = self.service.save_saved_design(self.current_design())
        self.set_design(saved); self.statusBar().showMessage("膜系已保存到本地设计库")

    def _load_from_library(self):
        designs = self.service.list_saved_designs()
        if not designs:
            QMessageBox.information(self, "设计库", "本地设计库中还没有膜系。"); return
        labels = [f"{row.get('name') or '未命名'} — {row.get('note') or ''} [{row['design_id']}]" for row in designs]
        label, ok = QInputDialog.getItem(self, "从设计库加载", "膜系：", labels, 0, False)
        if not ok: return
        if not self._confirm_replace(): return
        design = self.service.load_saved_design(designs[labels.index(label)]["design_id"])
        self.set_design(design); self._path = None

    def _save_dialog(self):
        directory = Path(self.service.root) / "data" / "film_designs"
        directory.mkdir(parents=True, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(self, "保存膜系", self._path or str(directory / f"film-design{design_extension('film')}"), f"膜系设计 (*{design_extension('film')})")

        if path: self._guard(lambda: self.save_path(path, overwrite=True))

    def save_path(self, path, overwrite=False):
        design = self.current_design(); saved = self.service.save(path, design, overwrite=overwrite)
        self.set_design(saved); self._path = str(path); self.dirty = False
        self.statusBar().showMessage("膜系已保存：" + str(path))

    def open_material_library(self, purpose):
        from PySide6.QtCore import QUrl, QMetaObject, QObject
        from PySide6.QtQml import QQmlApplicationEngine
        from .source_materials import MaterialLibraryBridge
        from coat_sim.ui.theme_service import ThemeService
        host = self._material_hosts.get(purpose)
        if host is None:
            library = MaterialLibraryBridge(directory=Path(self.service.root) / "data" / "materials", purpose=purpose)
            engine = QQmlApplicationEngine()
            ThemeService.instance().attach_engine(engine)
            engine.rootContext().setContextProperty("materialLibrary", library)
            title = "基片材料库" if purpose == "substrate" else "膜料库"
            source = ('import QtQuick\nimport QtQuick.Controls\nimport "qml"\n'
                'ApplicationWindow { id: host; visible: true; width: 1100; height: 820; title: "' + title + '"; '
                'function openLibrary() { show(); editor.open() } '
                'MaterialLibraryDialog { id: editor; library: materialLibrary; showUseAction: false; '
                'onClosed: host.hide(); Component.onCompleted: open() } }')
            engine.loadData(source.encode("utf-8"), QUrl.fromLocalFile(str(Path(__file__).parent / "FilmMaterialHost.qml")))
            if not engine.rootObjects(): raise RuntimeError("材料库界面加载失败")
            window = engine.rootObjects()[0]
            window.visibleChanged.connect(lambda visible: self.refresh_material_options() if not visible else None)
            host = (engine, library, window); self._material_hosts[purpose] = host
        else:
            QMetaObject.invokeMethod(host[2], "openLibrary")
        host[2].raise_(); host[2].requestActivate()

    def refresh_material_options(self):
        self.film_options = self.service.material_options(purpose="film")
        self.substrate_options = self.service.material_options(purpose="substrate")
        self._updating = True
        try:
            material_id, set_id = self.substrate_material.currentData(), self.substrate_set.currentData()
            self.substrate_material.clear()
            self.substrate_material.addItem("请选择", "")
            for item in self.substrate_options: self.substrate_material.addItem(item["name"], item["id"])
            _select(self.substrate_material, material_id)
            self._sets(self.substrate_set, self.substrate_options, material_id, set_id)
            for table, offset in [(table, 0) for table in self.tables.values()] + [(self.symbols_table, 1)]:
                for row in range(table.rowCount()):
                    material, sets = table.cellWidget(row, offset), table.cellWidget(row, offset+1)
                    material_id, set_id = material.currentData(), sets.currentData()
                    material.clear(); material.addItem("请选择", "")
                    for item in self.film_options: material.addItem(item["name"], item["id"])
                    _select(material, material_id); self._sets(sets, self.film_options, material_id, set_id)
            media_options = list({item["id"]: item for item in self.film_options + self.substrate_options}.values())
            self.incident_medium.refresh_options(media_options)
            self.exit_medium.refresh_options(media_options)
        finally:
            self._updating = False
        self._refresh()

    def _send_simulation(self):
        self._guard(lambda: self.simulationRequested.emit(self.current_design()))


class _CalculationSignals(QObject):
    completed = Signal(object)


def _simulate_with_optional_cancel(service, design, settings, cancel_event):
    try:
        parameters = inspect.signature(service.simulate).parameters.values()
        accepts_cancel = any(
            parameter.name == "cancel_event" or
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters)
    except (TypeError, ValueError):
        accepts_cancel = False
    if accepts_cancel:
        return service.simulate(design, settings, cancel_event=cancel_event)
    return service.simulate(design, settings)


class FilmSimulationWindow(_FilmWindow):
    def __init__(self, root=None, service=None, design=None):
        super().__init__(root, service)
        self.setWindowTitle("仿真计算")
        self.result = None
        self.result_stale = True
        self._extrapolation_policy = "endpoint_hold"
        self._generation = 0
        self._closed = False
        self._calculation_cancel_event = None
        self._executor = None
        self._signals = _CalculationSignals()
        self._signals.completed.connect(self._finish_calculation, Qt.QueuedConnection)
        container = QWidget(); self.setCentralWidget(container); layout = QVBoxLayout(container)
        bar = QHBoxLayout(); load = QPushButton("加载膜系"); load.clicked.connect(self._load_dialog); bar.addWidget(load)
        self.design_label = QLabel("尚未选择膜系"); bar.addWidget(self.design_label, 1); layout.addLayout(bar)
        self._light_sources = self._load_cie_light_sources(self.service.root)
        self._source_payloads = {}
        self._plot_settings = {
            "curves": ["R", "T", "A"],
            "x": {"auto": True, "min": 400.0, "max": 700.0, "major_tick_auto": True, "major_tick": 50.0},
            "y": {"auto": True, "min": 0.0, "max": 1.0, "major_tick_auto": True, "major_tick": 0.1},
            "grid": True,
        }
        settings_panel = QWidget(); form = QFormLayout(settings_panel); row = QHBoxLayout()
        self.minimum = _number(400, minimum=1); self.maximum = _number(700, minimum=1)
        self.samples = _IntegerInput(301)
        row.addWidget(self.minimum); row.addWidget(QLabel("—")); row.addWidget(self.maximum); row.addWidget(QLabel("nm；点数")); row.addWidget(self.samples); form.addRow("计算波段", row)
        row = QHBoxLayout(); self.angle = _number(0, maximum=89.999, minimum=0)
        self.polarization = _combo([("非偏振", "unpolarized"), ("s", "s"), ("p", "p")])
        self.first_surface = _combo([("正面先入射", "front"), ("背面先入射", "back")])
        row.addWidget(self.angle); row.addWidget(QLabel("°")); row.addWidget(self.polarization); row.addWidget(self.first_surface); form.addRow("入射角 / 偏振", row)
        row = QHBoxLayout(); self.source = _combo([("平坦光源", "flat"), ("黑体（相对归一化）", "blackbody")])
        self.temperature = _number(5778, minimum=1, maximum=1e7)
        for entry in self._light_sources:
            token = "cie:" + entry["id"]
            self.source.addItem(entry["name"], token); self._source_payloads[token] = copy.deepcopy(entry["source"])
        row.addWidget(self.source); row.addWidget(self.temperature); row.addWidget(QLabel("K")); form.addRow("光源", row)
        self.plot_kind = _combo([("本征 R / T / A", "intrinsic"), ("光源加权谱", "source")])
        form.addRow("兼容控制", self.plot_kind)
        settings_panel.hide(); layout.addWidget(settings_panel)
        settings_bar = QHBoxLayout()
        self.simulation_settings_button = QPushButton("仿真参数"); self.simulation_settings_button.setObjectName("simulation_settings_button")
        self.simulation_settings_button.clicked.connect(self._show_simulation_settings); settings_bar.addWidget(self.simulation_settings_button)
        self.settings_summary = QLabel(); self.settings_summary.setObjectName("simulation_settings_summary"); self.settings_summary.setWordWrap(True)
        settings_bar.addWidget(self.settings_summary, 1); layout.addLayout(settings_bar)
        row = QHBoxLayout(); self.calculate_button = QPushButton("计算并绘图"); self.calculate_button.setObjectName("simulate_button")
        self.calculate_button.clicked.connect(lambda: self._guard(self.start_calculation)); row.addWidget(self.calculate_button)
        self.plot_kind.currentIndexChanged.connect(self.plot_result)
        self.export_button = QToolButton()
        self.export_button.setObjectName("filmSimulationExportButton")
        self.export_button.setText("导出")
        self.export_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.export_menu = QMenu(self.export_button)
        self.export_csv_action = self.export_menu.addAction("导出 CSV")
        self.export_csv_action.setObjectName("filmSimulationExportCsvAction")
        self.export_csv_action.triggered.connect(self._export_csv)
        self.export_png_action = self.export_menu.addAction("导出 PNG")
        self.export_png_action.setObjectName("filmSimulationExportPngAction")
        self.export_png_action.triggered.connect(self._export_png)
        self.export_button.setMenu(self.export_menu)
        row.addWidget(self.export_button)
        self.export_buttons = [self.export_button]
        layout.addLayout(row)
        self.figure = Figure(figsize=(9, 5)); self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setMinimumHeight(180)
        self.canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self.canvas, 1)
        self.notes = QLabel("本征 R/T/A 与光源加权光谱分别展示；n 有效波段与数据格式由服务检查。")
        self.notes.setTextFormat(Qt.PlainText)
        self.notes.setWordWrap(True)
        self.notes.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.result_summary_area = QScrollArea()
        self.result_summary_area.setObjectName("film_computation_summary")
        self.result_summary_area.setFrameShape(QFrame.NoFrame)
        self.result_summary_area.setWidgetResizable(True)
        self.result_summary_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.result_summary_area.setFixedHeight(max(64, min(84, self.fontMetrics().lineSpacing() * 3 + 12)))
        self.result_summary_area.setWidget(self.notes)
        self.result_details_toggle = QToolButton()
        self.result_details_toggle.setObjectName("film_result_details_toggle")
        self.result_details_toggle.setText("仿真结果说明")
        self.result_details_toggle.setCheckable(True)
        self.result_details_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.result_details_toggle.setArrowType(Qt.RightArrow)
        self.result_details_toggle.toggled.connect(self.result_summary_area.setVisible)
        self.result_details_toggle.toggled.connect(
            lambda expanded: self.result_details_toggle.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow))
        layout.addWidget(self.result_details_toggle, 0, Qt.AlignLeft)
        layout.addWidget(self.result_summary_area)
        self.result_summary_area.hide()
        for box in (self.minimum, self.maximum, self.samples, self.angle, self.temperature):
            box.valueChanged.connect(self._parameters_changed)
        for box in (self.polarization, self.first_surface, self.source):
            box.currentIndexChanged.connect(self._parameters_changed)
        self.set_design(design or self._design)

    @staticmethod
    def _load_cie_light_sources(root):
        path = Path(root) / "data" / "film_design" / "cie-illuminants-20261006" / "data" / "cie_illuminants.json"
        if not path.is_file():
            return []
        try:
            catalog = json.loads(path.read_text(encoding="utf-8"))
            if catalog.get("schemaVersion") != 1 or catalog.get("dataLicense") != "CC-BY-SA-4.0":
                return []
            entries = []
            for identifier in catalog.get("illuminantOrder", []):
                dataset = catalog.get("datasets", {}).get(identifier)
                if not isinstance(dataset, dict) or dataset.get("wavelengthBasis") != "standard_air":
                    continue
                samples = dataset.get("samples")
                if not isinstance(samples, list) or len(samples) < 2 or len(samples) != dataset.get("sampleCount"):
                    continue
                wavelengths = [float(item[0]) for item in samples]
                intensities = [float(item[1]) for item in samples]
                if any(b <= a for a, b in zip(wavelengths, wavelengths[1:])) or min(intensities) < 0:
                    continue
                title = dataset.get("title", identifier)
                kind = dataset.get("kind", "")
                if kind == "equal_energy_reference":
                    title += " — E 等能参考（本包定义）"
                license_id = dataset.get("license", {}).get("spdx", catalog["dataLicense"])
                source = {
                    "kind": "tabulated", "wavelength_nm": wavelengths, "intensity": intensities,
                    "source": f"{title}; {dataset.get('citation', '')}; {dataset.get('doi', '')}; SPDX {license_id}",
                    "wavelength_basis": "standard_air", "dataset_id": identifier,
                    "snapshot_date": str(catalog.get("snapshotDate", "")),
                    "data_license": str(license_id), "source_page": str(dataset.get("sourcePage", "")),
                    "doi": str(dataset.get("doi", "")), "catalog_version": str(catalog.get("schemaVersion", "")),
                    "source_warnings": [str(item) for item in dataset.get("warnings", []) if isinstance(item, str)],
                }
                entries.append({"id": identifier, "name": title, "source": source})
            return entries
        except (OSError, ValueError, TypeError, KeyError, IndexError):
            return []

    def _show_simulation_settings(self):
        from .film_simulation_plot_settings import FilmSimulationAndPlotSettingsDialog
        dialog = FilmSimulationAndPlotSettingsDialog(
            self.settings(), self._plot_settings, self._light_sources, self)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                simulation, plot = dialog.values()
                from coat_sim.core.film_design_service import simulation_settings
                from .film_plot_settings import FilmPlotSettingsDialog
                payload = copy.deepcopy(simulation)
                payload.pop("step_nm", None)
                normalized_simulation = simulation_settings(payload)
                normalized_plot = FilmPlotSettingsDialog._merge_options(plot)
                keys = normalized_plot.get("curves", [])
                if not isinstance(keys, list):
                    raise ValueError("绘图曲线配置须为列表")
                simulation_changed = self._apply_simulation_settings(normalized_simulation)
                plot_changed = normalized_plot != self._plot_settings
                if plot_changed:
                    self._plot_settings = normalized_plot
                    blocker = QSignalBlocker(self.plot_kind)
                    _select(self.plot_kind, "intrinsic" if keys and all(
                        key in ("R", "T", "A") for key in keys) else "source")
                    del blocker
                    self.plot_result()
                elif simulation_changed:
                    self.plot_result()
        finally:
            dialog.deleteLater()

    def _apply_simulation_settings(self, settings):
        from coat_sim.core.film_design_service import simulation_settings
        payload = copy.deepcopy(settings)
        # The settings dialog has already synchronized step edits into a
        # discrete count; keep that integer count as the committed value.
        payload.pop("step_nm", None)
        normalized = simulation_settings(payload)
        before = simulation_settings(self.settings())
        if normalized == before:
            return False
        blockers = [QSignalBlocker(widget) for widget in
                    (self.minimum, self.maximum, self.samples, self.angle,
                     self.polarization, self.first_surface, self.source, self.temperature)]
        self.minimum.setValue(normalized["wavelength_min_nm"])
        self.maximum.setValue(normalized["wavelength_max_nm"])
        self.samples.setValue(normalized["samples"])
        self.angle.setValue(normalized["angle_deg"])
        _select(self.polarization, normalized["polarization"])
        _select(self.first_surface, normalized["first_surface"])
        source = normalized["light_source"]
        if source["kind"] == "tabulated":
            token = next((key for key, value in self._source_payloads.items() if value == source), None)
            if token is None:
                token = "loaded:" + str(len(self._source_payloads) + 1)
                self.source.addItem("已加载表格光源", token)
                self._source_payloads[token] = copy.deepcopy(source)
            _select(self.source, token)
        else:
            _select(self.source, source["kind"])
        if source["kind"] == "blackbody":
            self.temperature.setValue(source["temperature_k"])
        del blockers
        self._extrapolation_policy = normalized["extrapolation_policy"]
        self._refresh_settings_summary()
        self._parameters_changed()
        return True

    def _refresh_settings_summary(self):
        if not hasattr(self, "settings_summary"):
            return
        try:
            settings = self.settings()
        except (ValueError, TypeError, OverflowError) as exc:
            self.settings_summary.setText(f"仿真设置无效：{exc}")
            return
        self.settings_summary.setText(
            f"{settings['wavelength_min_nm']:g}–{settings['wavelength_max_nm']:g} nm · "
            f"{settings['samples']} 点 / {settings['step_nm']:g} nm · "
            f"{settings['angle_deg']:g}° · {self.polarization.currentText()} · {self.source.currentText()} · "
            f"n 超域：{'端点保持' if settings['extrapolation_policy'] == 'endpoint_hold' else '报错'}")

    def _show_plot_settings(self):
        self._show_simulation_settings()

    def _parameters_changed(self, *args):
        if self._calculation_cancel_event is not None:
            self._calculation_cancel_event.set()
        self._generation += 1
        self.result_stale = True
        self._update_buttons()
        self.statusBar().showMessage("参数已改变，请重新计算；已有光谱已过期")
        self._refresh_settings_summary()

    def _update_buttons(self):
        self.calculate_button.setEnabled(not self.busy)
        can_export = self.result is not None and not self.result_stale and not self.busy
        for button in self.export_buttons:
            button.setEnabled(can_export)
        self.export_csv_action.setEnabled(can_export)
        self.export_png_action.setEnabled(can_export)

    def set_design(self, design):
        self._design = copy.deepcopy(design); self.dirty = False; self.result = None
        _select(self.first_surface, design.get("first_surface", "front"))
        self._parameters_changed()
        name = design.get("name") or "未命名膜系"
        if name == "Untitled film design":
            name = "未命名膜系"  # Display the legacy placeholder without rewriting a saved name.
        self.design_label.setText(name if design["substrate"].get("material_id") else "请加载膜系或从设计窗口发送")
        self.notes.setText("本征 R/T/A 与光源加权光谱分别展示；n 有效波段与数据格式由服务检查。")
        self.figure.clear(); self.canvas.draw_idle()

    def settings(self):
        from coat_sim.core.film_design_service import simulation_settings
        selected = self.source.currentData()
        if selected in ("flat", "blackbody"):
            light_source = ({"kind": "blackbody", "temperature_k": self.temperature.value()}
                            if selected == "blackbody" else {"kind": "flat"})
        else:
            try:
                light_source = copy.deepcopy(self._source_payloads[selected])
            except KeyError as exc:
                raise ValueError("所选表格光源不可用。") from exc
        return simulation_settings({
            "wavelength_min_nm": self.minimum.value(), "wavelength_max_nm": self.maximum.value(),
            "samples": self.samples.value(),
            "angle_deg": self.angle.value(), "polarization": self.polarization.currentData(),
            "first_surface": self.first_surface.currentData(), "light_source": light_source,
            "extrapolation_policy": self._extrapolation_policy,
        })


    @staticmethod
    def _signature(design, settings):
        return json.dumps([design, settings], sort_keys=True, separators=(",", ":"), allow_nan=False)

    def _begin_calculation(self):
        if self.busy:
            raise ValueError("光谱计算正在进行")
        if self._closed:
            raise ValueError("仿真窗口已关闭")
        # Only the UI thread reads widgets. The worker receives detached values.
        design, settings = self.current_design(), self.settings()
        token = (self._generation, self._signature(design, settings), design)
        self._calculation_cancel_event = threading.Event()
        cancel_event = self._calculation_cancel_event
        self.busy = True; self.result_stale = True; self.result = None; self.error = ""
        self._update_buttons(); self.figure.clear(); self.canvas.draw_idle()
        self.notes.setText("正在后台计算光谱…")
        self.statusBar().showMessage("正在后台计算；可继续修改参数")
        return token, settings, cancel_event

    def start_calculation(self):
        """Start a local background spectrum calculation, without queue submission."""
        token, settings, cancel_event = self._begin_calculation()
        try:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="film-spectrum")
            future = self._executor.submit(
                _simulate_with_optional_cancel, self.service, token[2], settings,
                cancel_event)
            # The callback never captures or accesses this window or its widgets.
            signals = self._signals
            def completed(job):
                try:
                    payload = (token, job.result(), "")
                except Exception as exc:
                    payload = (token, None, str(exc))
                signals.completed.emit(payload)
            future.add_done_callback(completed)
        except Exception as exc:
            self.busy = False; self._calculation_cancel_event = None
            self.error = str(exc); self._update_buttons()
            self.notes.setText(self.error)
            raise

    @Slot(object)
    def _finish_calculation(self, payload):
        if self._closed:
            return
        token, result, error = payload
        self.busy = False
        self._calculation_cancel_event = None
        generation, signature, design = token
        try:
            current_signature = self._signature(self.current_design(), self.settings())
        except (ValueError, TypeError, OverflowError):
            current_signature = None
        if generation != self._generation or signature != current_signature:
            self.result_stale = True
            self.notes.setText("计算期间参数或膜系已改变，已丢弃旧光谱；请重新计算。")
            self.statusBar().showMessage("已丢弃过期计算结果")
            self._update_buttons()
            return
        if error:
            self.error = error; self.result = None; self.result_stale = True
            self.result_details_toggle.setChecked(True)
            self.notes.setText(error); self.statusBar().showMessage(error)
            self._update_buttons()
            QMessageBox.warning(self, "光谱计算失败", error)
            return
        self.result = result; self.result_stale = False; self.error = ""
        definition = ("半无限基片：T 为进入基片端口的通量，A 仅为膜层吸收。" if
            result.get('substrate_handling',{}).get('resolved_mode') == "semi_infinite" else
            "有限基片：T 为出射端通量，A 包含膜层和基片吸收。")
        report_lines = [definition + " 光源谱为相对单位。离散 n/k 数据在相邻样本之间使用线性插值。"]
        handling = result.get("substrate_handling", {})
        if handling.get("average_fringes_approximation"):
            report_lines.append("厚基底：平均条纹工程近似，保留体吸收与多次反射；不用于激光或高分辨平行片测量。")
        report_lines.extend(warning for warning in result.get("warnings", []) if warning.startswith("相干基片"))
        seen_channels = set()
        for item in result.get("provenance", []):
            evaluation = item.get("evaluation", {})
            for channel in ("n", "k"):
                data = evaluation.get(channel, {})
                if not data:
                    continue
                report = _film_channel_report(channel, data)
                if report not in seen_channels:
                    seen_channels.add(report)
                    report_lines.append(report)
        if result.get("warnings"):
            report_lines.append(f"计算提示共 {len(result['warnings'])} 项，完整内容随结果保留。")
        self.notes.setText("\n".join(report_lines))
        self.plot_result(); self.statusBar().showMessage("计算完成"); self._update_buttons()

    def calculate(self):
        """Synchronous helper for internal tests; UI and adapter use start_calculation."""
        token, settings, cancel_event = self._begin_calculation()
        try:
            result = _simulate_with_optional_cancel(
                self.service, token[2], settings, cancel_event)
        except Exception as exc:
            self.busy = False; self._calculation_cancel_event = None
            self.error = str(exc); self.notes.setText(self.error); self._update_buttons()
            raise
        self._finish_calculation((token, result, ""))
        return self.result

    def plot_result(self, *args):
        if self.result is None: return
        self.figure.clear(); axes = self.figure.add_subplot(111)
        labels = {
            "R": "R（反射）", "T": "T（透射）", "A": "A（吸收）",
            "source_spectrum": "光源谱", "reflected_spectrum": "反射光谱",
            "transmitted_spectrum": "透射光谱", "absorbed_spectrum": "吸收光谱",
        }
        waves = self.result["wavelength_nm"]
        curve_keys = list(self._plot_settings.get("curves", []))
        intrinsic = {"R", "T", "A"}
        source = {"source_spectrum", "reflected_spectrum", "transmitted_spectrum", "absorbed_spectrum"}
        if self.plot_kind.currentData() == "source" and not any(key in source for key in curve_keys):
            curve_keys = ["source_spectrum", "reflected_spectrum", "transmitted_spectrum", "absorbed_spectrum"]
        elif self.plot_kind.currentData() == "intrinsic" and not any(key in intrinsic for key in curve_keys):
            curve_keys = ["R", "T", "A"]
        for key in curve_keys:
            if key in self.result and key in labels:
                axes.plot(waves, self.result[key], label=labels[key])
        axes.set_xlabel("波长（真空，nm）", **_film_font_kwargs())
        axes.set_ylabel("反射 / 透射 / 吸收（%）或相对光谱（%）", **_film_font_kwargs())
        from matplotlib.ticker import PercentFormatter
        axes.yaxis.set_major_formatter(PercentFormatter(xmax=1.0))
        from .film_plot_settings import FilmPlotSettingsDialog
        options = FilmPlotSettingsDialog._merge_options(self._plot_settings)
        self._plot_settings = options
        if options['y'].get('auto', True): axes.set_ylim(0, 1)
        for axis, key in ((axes.xaxis, "x"), (axes.yaxis, "y")):
            setting = options.get(key, {})
            target = axes.get_xaxis() if key == "x" else axes.get_yaxis()
            if not setting.get("auto", True):
                limits = (setting.get("min"), setting.get("max"))
                (axes.set_xlim if key == "x" else axes.set_ylim)(*limits)
            if not setting.get("major_tick_auto", True):
                from matplotlib.ticker import MultipleLocator
                tick = float(setting.get("major_tick", 1))
                if tick > 0: axis.set_major_locator(MultipleLocator(tick))
        if options.get("grid", True):
            axes.grid(True, alpha=.25)
        else:
            axes.grid(False)
        if axes.lines: axes.legend(prop=_FILM_FONT_PROPERTIES)
        self.figure.tight_layout(); self.canvas.draw_idle()


    def _can_export(self):
        if self.result is None or self.result_stale or self.busy:
            self.statusBar().showMessage("请先用当前参数完成计算后导出")
            return False
        return True

    def _export_csv(self):
        if not self._can_export(): return
        path, _ = QFileDialog.getSaveFileName(self, "导出光谱数据", "spectrum.csv", "CSV (*.csv)")
        if path and self._can_export(): self._guard(lambda: self.service.export_csv(path, self.result, overwrite=True))

    def _export_png(self):
        if not self._can_export(): return
        path, _ = QFileDialog.getSaveFileName(self, "导出光谱图", "spectrum.png", "PNG (*.png)")
        if path and self._can_export(): self._guard(lambda: self.figure.savefig(path, dpi=160))

    def closeEvent(self, event):
        super().closeEvent(event)
        if event.isAccepted():
            self._closed = True
            self._generation += 1
            if self._calculation_cancel_event is not None:
                self._calculation_cancel_event.set()
            if self._executor is not None:
                self._executor.shutdown(wait=False, cancel_futures=True)

class FilmWindowAdapter:
    """Whitelisted document/actions for the existing authenticated UI control server."""
    def __init__(self, window_type, window, bridge, root=PROJECT_ROOT):
        from coat_sim.core.api import CoatService
        self.kind, self.window, self.bridge = window_type, window, bridge
        self.bounded = CoatService(root)

    def snapshot(self):
        from coat_sim.ui.control import _window_contract
        value = self.bridge.snapshot()
        value["document"] = value.pop("data")
        value.update(window_type=self.kind, state="ready", pid=__import__("os").getpid(),
                     visible=self.window.isVisible(), path=value["design_path"])
        value.update(_window_contract(self.kind))
        value["action_arguments"] = {
            "new": ["force"], "save": ["path", "overwrite"],
            "add_layer": ["side", "layer"], "delete_layer": ["side", "index"],
            "move_layer": ["side", "index", "step"],
            "expand_expression": ["side", "expression", "symbols"],
            "send_to_simulation": [], "calculate": [],
            "export_csv": ["path", "overwrite"], "export_png": ["path", "overwrite"]}
        value["action_arguments"] = {name: value["action_arguments"][name] for name in value["actions"]}
        if self.kind == "film_simulation":
            value.update(fields=dict(self.bridge.settings(), plot_kind=self.bridge.plot_kind.currentData(),
                plot_settings=copy.deepcopy(self.bridge._plot_settings)), has_result=self.bridge.result is not None, result_stale=self.bridge.result_stale,
                warnings=self.bridge.result.get("warnings", []) if self.bridge.result else [],
                provenance=self.bridge.result.get("provenance") if self.bridge.result else None)
        else:
            data = value["document"] or {}
            value["fields"] = {key: data.get(key) for key in ("name", "note", "reference_wavelength_nm", "assume_lossless", "use_k")}
        value["editable_fields"] = list(value["fields"])
        return value

    def update(self, *, fields=None, design_path=None, data=None, intended_path=None,
               pointers=None, force=False):
        from coat_sim.ui.control import UiControlError
        if type(force) is not bool:
            raise UiControlError("invalid_force", "force 须为布尔值")
        if design_path is not None and data is not None:
            raise UiControlError("invalid_request", "design_path 与 data 只能指定一项")
        if pointers:
            raise UiControlError("unsupported_operation", "膜系编辑使用完整 data 文档")
        if self.bridge.dirty and (design_path is not None or data is not None) and not force:
            raise UiControlError("unsaved_changes", "窗口有未保存修改；替换文档须明确 force=true")
        if design_path is not None:
            self.bridge.load_path(str(self.bounded._path(design_path)))
        if data is not None:
            normalized = self.bridge.service.validate(data)
            self.bridge.set_design(normalized)
            self.bridge._path = str(self.bounded._path(intended_path)) if intended_path else None
            self.bridge.dirty = self.kind == "film_design"
        elif intended_path is not None:
            self.bridge._path = str(self.bounded._path(intended_path))
        if fields:
            if not isinstance(fields, dict):
                raise UiControlError("invalid_field", "fields 须为对象")
            if self.kind == "film_design":
                allowed = {"name", "note", "reference_wavelength_nm", "assume_lossless", "use_k"}
                if set(fields) - allowed:
                    raise UiControlError("invalid_field", "未知膜系字段")
                prospective = self.bridge.current_design()
                prospective.update(fields)
                self.bridge.set_design(self.bridge.service.validate(prospective))
                self.bridge.dirty = True
            else:
                self._settings(fields)
        return self.snapshot()

    def _settings(self, fields):
        import math
        from coat_sim.core.film_design_service import simulation_settings
        from coat_sim.ui.control import UiControlError
        if not isinstance(fields, dict):
            raise UiControlError("invalid_field", "fields 须为对象")
        allowed = {
            "wavelength_min_nm", "wavelength_max_nm", "samples", "step_nm",
            "angle_deg", "polarization", "first_surface", "light_source",
            "plot_kind", "plot_settings",
        }
        if set(fields) - allowed:
            raise UiControlError("invalid_field", "未知仿真设置字段")
        settings = self.bridge.settings()
        plot_settings = copy.deepcopy(self.bridge._plot_settings)
        plot_changed = False
        simulation_fields = {key: value for key, value in fields.items()
                             if key not in ("plot_kind", "plot_settings")}
        if "plot_kind" in fields:
            if fields["plot_kind"] not in ("intrinsic", "source"):
                raise UiControlError("invalid_field", "plot_kind 选项无效")
            plot_settings["curves"] = (["R", "T", "A"] if fields["plot_kind"] == "intrinsic" else
                ["source_spectrum", "reflected_spectrum", "transmitted_spectrum", "absorbed_spectrum"])
            plot_changed = True
        if "plot_settings" in fields:
            if not isinstance(fields["plot_settings"], dict):
                raise UiControlError("invalid_field", "plot_settings 须为对象")
            plot_settings = fields["plot_settings"]
            plot_changed = True
        for key, value in simulation_fields.items():
            if key in ("wavelength_min_nm", "wavelength_max_nm", "step_nm", "angle_deg"):
                if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
                    raise UiControlError("invalid_field", key + " 须为有限数值")
            elif key == "samples" and (type(value) is not int or not 2 <= value <= sys.maxsize):
                raise UiControlError("invalid_field", "samples 须为至少 2 且不超过当前运行时可寻址数组长度的整数")
            elif key == "polarization" and value not in ("s", "p", "unpolarized"):
                raise UiControlError("invalid_field", "polarization 选项无效")
            elif key == "first_surface" and value not in ("front", "back"):
                raise UiControlError("invalid_field", "first_surface 选项无效")
            elif key == "light_source" and not isinstance(value, dict):
                raise UiControlError("invalid_field", "light_source 须为对象")
            settings[key] = value
        if "samples" in simulation_fields and "step_nm" not in simulation_fields:
            settings.pop("step_nm", None)
        if simulation_fields:
            try:
                normalized = simulation_settings(settings)
                self.bridge._apply_simulation_settings(normalized)
            except (ValueError, TypeError) as exc:
                raise UiControlError("invalid_field", str(exc)) from exc
        if plot_changed:
            from coat_sim.ui.film_plot_settings import FilmPlotSettingsDialog
            try:
                dialog = FilmPlotSettingsDialog(plot_settings, self.window)
                plot_settings = dialog.values()
            except (ValueError, TypeError) as exc:
                raise UiControlError("invalid_field", str(exc)) from exc
            self.bridge._plot_settings = plot_settings
            keys = plot_settings.get("curves", [])
            blocker = QSignalBlocker(self.bridge.plot_kind)
            _select(self.bridge.plot_kind, "intrinsic" if keys and all(k in ("R", "T", "A") for k in keys) else "source")
            del blocker
            self.bridge.plot_result()

    def action(self, name, arguments=None):
        import uuid
        from coat_sim.ui.control import UiControlError, _ACTIONS
        args = arguments or {}
        if not isinstance(args, dict) or name not in _ACTIONS[self.kind]:
            raise UiControlError("unsupported_action", "此膜系窗口不支持该动作")
        if "force" in args and type(args["force"]) is not bool:
            raise UiControlError("invalid_force", "force 须为布尔值")
        if name == "new":
            if self.bridge.dirty and not args.get("force", False):
                raise UiControlError("unsaved_changes", "新建须先保存修改或显式 force=true")
            self.bridge.set_design(self.bridge._new_design()); self.bridge._path = None
        elif name in ("save", "export_csv", "export_png"):
            if type(args.get("overwrite")) is not bool:
                raise UiControlError("overwrite_required", "保存/导出须显式指定 overwrite 布尔值")
            path = self.bounded._path(args.get("path") or self.bridge._path or "")
            if name == "save":
                self.bridge.save_path(path, overwrite=args["overwrite"])
            else:
                if self.bridge.result is None:
                    raise UiControlError("no_result", "请先计算")
                if self.bridge.busy or self.bridge.result_stale:
                    raise UiControlError("stale_result", "参数已改变或计算尚未完成，请重新计算后导出")
                if name == "export_csv":
                    self.bridge.service.export_csv(path, self.bridge.result, overwrite=args["overwrite"])
                else:
                    import os
                    import tempfile
                    # Render next to the target, then publish atomically without clobbering.
                    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".png", delete=False) as stream:
                        temporary = Path(stream.name)
                    try:
                        self.bridge.figure.savefig(temporary, format="png", dpi=160)
                        if args["overwrite"]:
                            os.replace(temporary, path)
                        else:
                            try:
                                os.link(temporary, path)
                            except FileExistsError as exc:
                                raise UiControlError("file_exists", "导出文件已存在") from exc
                    finally:
                        temporary.unlink(missing_ok=True)
        elif name == "calculate":
            if self.bridge.busy:
                raise UiControlError("busy", "光谱计算正在进行")
            self.bridge.start_calculation()
        elif name == "send_to_simulation":
            self.bridge.simulationRequested.emit(self.bridge.current_design())
        else:
            side = args.get("side", "front")
            if side not in ("front", "back"):
                raise UiControlError("invalid_field", "side 须为 front 或 back")
            design = self.bridge.current_design(); layers = design[side + "_layers"]
            if name == "add_layer":
                layer = args.get("layer", {"material_id": "", "optical_set_id": "",
                    "thickness": {"value": 100, "unit": "nm"}, "note": ""})
                if not isinstance(layer, dict):
                    raise UiControlError("invalid_field", "layer 须为对象")
                layers.append(dict(layer, id=layer.get("id") or uuid.uuid4().hex))
            elif name in ("delete_layer", "move_layer"):
                index = args.get("index")
                if type(index) is not int or not 0 <= index < len(layers):
                    raise UiControlError("invalid_field", "层索引无效")
                if name == "delete_layer": layers.pop(index)
                else:
                    step = args.get("step")
                    if type(step) is not int or step not in (-1, 1) or not 0 <= index + step < len(layers):
                        raise UiControlError("invalid_field", "移动方向或目标无效")
                    layers[index], layers[index+step] = layers[index+step], layers[index]
            elif name == "expand_expression":
                expression = design["expression"]
                expression.update(text=args.get("expression", expression["text"]),
                                  symbols=args.get("symbols", expression["symbols"]))
                design[side + "_layers"] = self.bridge.service.expand(expression["text"], expression["symbols"])
                expression["in_sync"] = True
            if name != "expand_expression" and design["expression"]["text"]:
                design["expression"]["in_sync"] = False
            self.bridge.set_design(self.bridge.service.validate(design)); self.bridge.dirty = True
        return self.snapshot()
