"""UI state for bulk layer input; parsing/generation live in the pure core."""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
import math
from PySide6.QtCore import QObject, Property, QUrl, QThread, Signal, Slot


def _qml_integer(value, label):
    """Qt's JavaScript numbers arrive as doubles even for integer controls.

    Normalize only fields whose UI contracts are integers; fractions, booleans
    and non-finite values still fail instead of weakening the public schema.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}必须为整数")
    if not math.isfinite(value) or value != int(value):
        raise ValueError(f"{label}必须为整数")
    return int(value)


def _qml_film_stack_parameters(parameters):
    payload = deepcopy(dict(parameters))
    for key in ("layer_count", "repeat_count"):
        if key in payload:
            payload[key] = _qml_integer(payload[key], key)
    for material in [*payload.get("materials", []), *payload.get("sources", [])]:
        if "rate_angstrom_s" in material:
            if "rate_nm_s" in material:
                raise ValueError("沉积速率只能指定一种单位")
            value = material.pop("rate_angstrom_s")
            if isinstance(value, bool):
                raise ValueError("沉积速率必须为有限数值")
            material["rate_nm_s"] = float(value) * 0.1
        for key in ("source_slot", "monitor_slot"):
            if key in material and material[key] is not None:
                material[key] = _qml_integer(material[key], key)
        if material.get("mask_slots") is not None:
            material["mask_slots"] = [_qml_integer(value, "修正板位次") for value in material["mask_slots"]]
    for key, value in payload.get("columns", {}).items():
        if isinstance(value, (int, float)):
            payload["columns"][key] = _qml_integer(value, "层表列位次")
    return payload

class _ImportWorker(QThread):
    ready = Signal(object)
    failed = Signal(str)
    def __init__(self, path, options, parent):
        super().__init__(parent)
        self.path, self.options = path, options
    def run(self):
        try:
            from coat_sim.core.film_stack import parse_film_stack_file, film_stack_import_sheets
            if self.path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError("层表文件不能超过 16 MiB")
            content = self.path.read_bytes()
            sheets = film_stack_import_sheets(self.path.name, content)
            options = dict(self.options)
            if sheets and not options.get("sheet"):
                options["sheet"] = sheets[0]
            table = parse_film_stack_file(self.path.name, content, **options)
            table["sheets"] = sheets
            self.ready.emit(table)
        except Exception as exc:
            self.failed.emit(str(exc))

class FilmStackController(QObject):
    changed = Signal()
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self._table = None
        self._material_column = 0
        self._worker = None
        self._view = {"busy": False, "message": "选择机器中已保存的蒸发源，或导入层表并明确匹配源设计。", "columns": [], "preview": [], "materials": [], "sheets": []}
    @Property("QVariantMap", notify=changed)
    def state(self):
        return deepcopy(self._view)
    @Slot(QUrl, "QVariantMap")
    def inspectFile(self, url, options):
        if self._worker is not None:
            return
        try:
            path = self.editor.service._path(url.toLocalFile())
            allowed = {k: v for k, v in dict(options).items() if k in ("sheet", "header_row", "delimiter", "encoding", "has_header") and v is not None}
            if "header_row" in allowed:
                allowed["header_row"] = _qml_integer(allowed["header_row"], "表头行")
            self._table = None
            self._view.update(busy=True, message="正在读取层表…", columns=[], preview=[], materials=[], row_count=0)
            self.changed.emit()
            worker = _ImportWorker(path, allowed, self)
            worker.ready.connect(self._ready)
            worker.failed.connect(self._failed)
            worker.finished.connect(self._finished)
            self._worker = worker
            worker.start()
        except Exception as exc:
            self._failed(str(exc))
    @Slot(object)
    def _ready(self, table):
        self._table = table
        rows = table.get("rows", [])
        self._view.update(columns=table.get("columns", []), preview=rows[:8], row_count=len(rows),
                          sheets=table.get("sheets", []), message="已读取" + ("工作表：" + str(table.get("sheet", "")) + "；" if table.get("sheets") else "层表；") + "请选择材料列、厚度列、单位与层序。")
        self.selectMaterialColumn(min(self._material_column, len(table.get("columns", [])) - 1))
        self.changed.emit()
    @Slot(str)
    def _failed(self, message):
        self._table = None
        self._view.update(message="层表读取失败：" + message)
        self.changed.emit()
    @Slot()
    def _finished(self):
        worker, self._worker = self._worker, None
        self._view["busy"] = False
        self.changed.emit()
        if worker is not None:
            worker.deleteLater()
    @Slot(int)
    def selectMaterialColumn(self, column):
        if column < 0:
            return
        self._material_column = column
        if not self._table:
            return
        try:
            from coat_sim.core.film_stack import film_stack_import_materials
            names = film_stack_import_materials(self._table, column)
            if len(names) > 64:
                raise ValueError("材料种类超过 64；请先整理材料列。")
        except ValueError as exc:
            names = []
            message = "请选择有效材料列：" + str(exc)
            if self._view.get("message") != message:
                self._view["message"] = message
                self.changed.emit()
        if self._view.get("materials") != names:
            self._view["materials"] = names
            self.changed.emit()
    @Slot("QVariantMap", bool, result=bool)
    def build(self, parameters, from_import=False):
        try:
            from coat_sim.core.film_stack import build_film_stack, build_imported_film_stack
            payload = _qml_film_stack_parameters(parameters)
            current = self.editor._data
            payload.update(machine_file=current.get("machine_file", ""), substrate_file=current.get("substrate_file", ""),
                           name=current.get("name", "新工艺"), description=current.get("description", ""), notes=current.get("notes", ""))
            owner = self.editor._path or self.editor._base() / "new_process.json"
            machine_path = self.editor.service.resolve_reference(owner, payload["machine_file"])
            machine = self.editor.service.load("machine", machine_path).runtime
            if from_import:
                if self._table is None:
                    raise ValueError("请先读取层表")
                result = build_imported_film_stack(self._table, payload, machine=machine)
            else:
                result = build_film_stack(payload, machine=machine)
            self.editor._data = deepcopy(result["profile"])
            self.editor._step_page = 0
            self.editor._edit()
            count = len(self.editor._data["steps"])
            self.editor._set_status(f"已批量生成 {count} 层，尚未保存；请确认后保存。")
            self._view["message"] = f"已生成 {count} 层；保留当前机器和基片引用。"
            self.changed.emit()
            return True
        except Exception as exc:
            self._view["message"] = "无法生成层表：" + str(exc)
            self.changed.emit()
            return False
    def close(self):
        if self._worker is not None:
            self._worker.wait()
