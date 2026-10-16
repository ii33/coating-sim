"""Explicit portable asset inspection/export shared by every design editor."""
from pathlib import Path
from PySide6.QtCore import QObject, Property, Signal, Slot, QUrl


class AssetBrowserBridge(QObject):
    changed = Signal()
    overwriteRequested = Signal()

    def __init__(self, service, kind, open_design, parent=None):
        super().__init__(parent)
        self.service, self.kind, self.open_design = service, kind, open_design
        self._path = None
        self._content = {}
        self._status = ''
        self._pending = None

    @Property('QVariantList', notify=changed)
    def designs(self):
        return [{k: row.get(k) for k in ('path', 'kind', 'sha256')} for row in self._content.get('designs', [])]

    @Property(str, notify=changed)
    def status(self):
        return self._status

    @Property(str, constant=True)
    def editorKind(self):
        return self.kind

    @Slot(QUrl, result=bool)
    def inspect(self, url):
        self._pending = None
        try:
            path = Path(url.toLocalFile())
            content = self.service.read_simulation_asset(path, detail='full')
            self._pending = None
            self._path, self._content = path, content
            self._status = '已读取资产包。选择设计可查看；导出会保留完整相对路径和依赖。'
            self.changed.emit()
            return True
        except Exception as exc:
            self._status = f'读取资产包失败：{exc}'
            self.changed.emit()
            return False

    @Slot(int, result=str)
    def preview(self, index):
        import json
        rows = self._content.get('designs', [])
        return json.dumps(rows[index].get('data', {}), ensure_ascii=False, indent=2) if 0 <= index < len(rows) else ''

    @Slot(QUrl, int, bool, bool)
    def export(self, directory, index, include_results, open_after):
        self._pending = (self._path, self._content, directory, index, include_results, open_after)
        self._export(False)

    @Slot(bool)
    def confirmExport(self, overwrite):
        if overwrite:
            self._export(True)
        else:
            self._pending = None

    def _export(self, overwrite):
        try:
            if not self._path or not self._pending:
                raise ValueError('请先选择资产包和导出目录')
            asset_path, content, directory, index, include_results, open_after = self._pending
            destination = Path(directory.toLocalFile())
            designs = content.get('designs', [])
            selected = designs[index] if 0 <= index < len(designs) else None
            if open_after and selected and selected['kind'] != self.kind:
                raise ValueError('选择的设计类型与当前设计器不一致；请选择对应类型或取消导出后打开')
            members = [selected['path']] if selected else [row['path'] for row in designs]
            if include_results:
                members.extend(row['path'] for row in content.get('members', []) if row.get('role') != 'design')
            result = self.service.export_simulation_asset(asset_path, destination,
                members=members, include_dependencies=True, overwrite=overwrite)
            self._status = f'已导出到：{destination}'
            if open_after and selected:
                self.open_design(destination / selected['path'])
            self._pending = None
        except FileExistsError:
            self.overwriteRequested.emit()
            self._status = '导出目标已有同名文件，请确认是否覆盖'
        except Exception as exc:
            self._status = f'导出失败：{exc}'
        self.changed.emit()
