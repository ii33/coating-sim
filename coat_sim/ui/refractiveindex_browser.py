"""Bounded offline source browsing and explicit scope-bound material imports."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from threading import Event
from typing import Any

from PySide6.QtCore import QObject, Property, QTimer, QUrl, Signal, Slot

from coat_sim.core.material_business import MATERIAL_PACKAGE_KIND, MaterialLibrary
from coat_sim.core.materials import validate_material_profile
from .local_files import local_file_path


_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="refractiveindex-browser")


def _store_factory(root):
    from coat_sim.core.refractiveindex_local import RefractiveIndexLocalStore
    return RefractiveIndexLocalStore(root)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, allow_nan=False,
                                    sort_keys=True).encode("utf-8")).hexdigest()


class _VendorSourceAdapter:
    """Read-only view projection; the vendor core retains all numerical semantics."""

    def __init__(self, catalog):
        self.catalog = catalog
        self.metadata = catalog.status()

    def status(self):
        return {key: value for key, value in {**self.metadata, "status": "ready", "source_kind": "vendor"}.items()
                if key not in {"raw_header", "catalog_comments"}}

    def _row(self, row):
        conversion = deepcopy(row["conversion"])
        conversion["reason_codes"] = [conversion.get("reason", "")]
        conversion["choices"] = ([{"block_selection": [], "has_n": True, "has_k": False,
                                   "label": "AGF 系数条目 · k 未知"}]
                                 if conversion["status"] == "supported" else [])
        summary = {key: row.get(key) for key in ("entry_id", "grade", "formula_number", "formula_name",
                   "wavelength_range_um", "reference_temperature_c", "data_quality", "reference_warnings")}
        return {**summary, "name": self.metadata["manufacturer"] + " " + row["grade"],
                "source_path": self.metadata["source_filename"] + " / " + row["grade"],
                "data_types": ["AGF " + str(row["formula_number"]) + " · " + row["formula_name"]],
                "conversion": conversion}

    def search(self, query="", *, offset=0, limit=50, capability=None, **_filters):
        # AGF has no RII classification or selection-required state.
        if capability == "selection_required":
            return {"entries": [], "total": 0, "offset": offset, "limit": limit, "has_more": False}
        result = self.catalog.search(query, offset=offset, limit=limit, capability=capability)
        return {**result, "limit": limit, "has_more": offset + limit < result["total"],
                "entries": [self._row(row) for row in result["entries"]]}

    def get_entry(self, identity):
        result = self.catalog.detail(identity)
        entry, metadata = result["entry"], result["catalog"]
        return {**self._row(entry), "source_kind": "vendor",
                "source_sha256": metadata["source_sha256"], "source_url": metadata["source_url"],
                "classifications": {"manufacturer": metadata["manufacturer"],
                                    "catalog_version": metadata["catalog_version"], "format": "AGF"},
                "references": {key: value for key, value in metadata.items()
                               if key not in {"raw_header", "catalog_comments"}},
                "comments": result["warnings"] + entry["reference_warnings"],
                "conditions": {key: entry.get(key) for key in
                    ("reference_temperature_c", "reference_index_medium", "wavelength_reference")},
                "properties": {key: entry.get(key) for key in
                    ("internal_transmittance", "thermal_coefficients", "data_quality", "nd_consistency")},
                "raw_source": {"text": entry["raw_entry"][:20000], "truncated": len(entry["raw_entry"]) > 20000,
                               "total_chars": len(entry["raw_entry"])}}

    def preview(self, identity, *, max_points=200):
        entry = self.get_entry(identity)
        if entry["conversion"]["status"] != "supported":
            blocks = []
        else:
            result = self.catalog.preview(identity, max_points=max_points)
            blocks = [{"index": 0, "type": entry["data_types"][0], "quantity": "n",
                       "columns": result["columns"], "points": result["points"],
                       "source_point_count": None, "display_point_count": len(result["points"]),
                       "calculated": True, "sampled": True, "error": None}]
        return {"entry_id": identity, "source_sha256": entry["source_sha256"], "blocks": blocks}

    def convert_entry(self, identity, *, optical_set_id, name=None, block_selection=None,
                      allow_reference_air_approximation=False):
        if block_selection != []:
            raise ValueError("请选择完整的单条 AGF 系数记录。")
        result = self.catalog.prepare_import(identity, optical_set_id=optical_set_id, name=name,
            allow_reference_air_approximation=allow_reference_air_approximation)
        provenance = {**result["provenance"], "source_kind": "vendor", "block_selection": [],
                      "source_path": self.metadata["source_filename"]}
        return {**result, "provenance": provenance, "missing": ["k", "deposition_conditions"],
                "warnings": provenance["warnings"], "production_validation": "not_established"}


def _vendor_source(spec):
    from coat_sim.core.vendor_optical_catalog import load_vendor_catalog
    return _VendorSourceAdapter(load_vendor_catalog(**spec))


def _candidate(converted, name):
    provenance = converted["provenance"]
    vendor = provenance.get("source_kind") == "vendor"
    identity = ("vendor_import_" if vendor else "rii_import_") + _digest(
        [provenance["entry_id"], provenance["block_selection"]])[:32]
    notes = json.dumps({("vendor_source" if vendor else "refractiveindex_source"): provenance,
                        "missing": converted.get("missing", []),
                        "assumptions": converted.get("assumptions", []),
                        "warnings": converted.get("warnings", []),
                        "production_validation": "not_established"}, ensure_ascii=False, indent=2)
    return validate_material_profile({"id": identity, "name": name,
        "optical_only": True, "resistance_model": None, "electron_beam_model": None,
        "description": ("厂商 AGF 本地来源" if vendor else "refractiveindex.info 本地来源") + "；用途和沉积工艺尚未确认。",
        "notes": notes, "optical_constants": [deepcopy(converted["optical_set"])]})


class _NoGlobalPicker:
    def catalog_records(self):
        return []


class RefractiveIndexBrowserBridge(QObject):
    """Own only one view and its ephemeral plan, never a second material store."""

    changed = Signal()
    selectionReady = Signal()
    sourceChanged = Signal()
    importFinished = Signal(bool)

    def __init__(self, owner, *, store_root=None, store_factory=None):
        super().__init__(owner)
        if store_root is None:
            from coat_sim.core.refractiveindex_local import default_store_root
            store_root = default_store_root()
        self.owner: Any = owner
        self._root = Path(store_root)
        self._factory = store_factory or _store_factory
        self._source_kind = "rii"
        self._vendor_spec: dict = {}
        self._vendor_adapter = None
        self._references: dict = {}
        self._reference_detail: dict = {}
        self._reference_id = ""
        self._active = self._disposed = self._applying = False
        self._epochs: dict[str, int] = {}
        self._tasks: dict[str, tuple[int, Future, Event]] = {}
        self._source_status: dict = {}
        self._results: dict = {}
        self._entry: dict = {}
        self._preview: dict = {}
        self._selected_id = ""
        self._query = {"query": "", "catalog": "nk", "shelf": None, "book": None,
                       "capability": None, "offset": 0, "limit": 50}
        self._plan: dict | None = None
        self._import_summary: dict = {}
        self._error = self._status = ""
        self._refresh_after_commit = False
        self._timer = QTimer(self)
        self._timer.setInterval(15)
        self._timer.timeout.connect(self._poll)
        owner.libraryChanged.connect(self._owner_changed)

    @Property(bool, notify=changed)
    def active(self):
        return self._active

    @Property(bool, notify=changed)
    def applying(self):
        return self._applying

    @Property(bool, notify=changed)
    def busy(self):
        return bool(self._tasks)

    @Property(bool, notify=changed)
    def installing(self):
        return "install" in self._tasks

    @Property(bool, notify=changed)
    def searching(self):
        return "search" in self._tasks or "vendor" in self._tasks

    @Property(bool, notify=changed)
    def loadingEntry(self):
        return "detail" in self._tasks

    @Property(str, notify=changed)
    def sourceKind(self):
        return self._source_kind

    @Property(dict, notify=changed)
    def references(self):
        return deepcopy(self._references)

    @Property(str, notify=changed)
    def referenceId(self):
        return self._reference_id

    @Property(dict, notify=changed)
    def referenceDetail(self):
        return deepcopy(self._reference_detail)

    @Property(str, constant=True)
    def storePath(self):
        return str(self._root)

    @Property(str, constant=True)
    def targetLabel(self):
        if self.owner.libraryScope == "project":
            return "当前工程：" + str(self.owner.librarySourcePath)
        return "全局材料库：" + str(self.owner.service.root / "materials")

    @Property(str, notify=changed)
    def selectedId(self):
        return self._selected_id

    @Property(str, notify=changed)
    def errorText(self):
        return self._error

    @Property(str, notify=changed)
    def statusText(self):
        return self._status

    @Property(dict, notify=changed)
    def sourceStatus(self):
        return deepcopy(self._source_status)

    @Property(dict, notify=changed)
    def results(self):
        return deepcopy(self._results)

    @Property(dict, notify=changed)
    def entry(self):
        return deepcopy(self._entry)

    @Property(dict, notify=changed)
    def preview(self):
        return deepcopy(self._preview)

    @Property(dict, notify=changed)
    def importSummary(self):
        return deepcopy(self._import_summary)

    def _cancel(self, kind):
        self._epochs[kind] = self._epochs.get(kind, 0) + 1
        previous = self._tasks.pop(kind, None)
        if previous is not None:
            previous[2].set()
            previous[1].cancel()

    def _start(self, kind, operation):
        self._cancel(kind)
        generation = self._epochs[kind]
        cancelled = Event()
        try:
            future = _EXECUTOR.submit(operation, cancelled)
        except RuntimeError as exc:
            return self._fail(exc)
        self._tasks[kind] = (generation, future, cancelled)
        self._error = ""
        self._timer.start()
        self.changed.emit()
        return True

    def _fail(self, error):
        self._error = str(error)
        self.changed.emit()
        return False

    @Slot(result=bool)
    def open(self):
        if self._disposed or self.owner._disposed:
            return False
        self._active = True
        self.changed.emit()
        if not self._source_status or self._source_status.get("status") != "ready":
            return self.refresh()
        if self._selected_id and not self._entry:
            return self.selectEntry(self._selected_id)
        return True

    @Slot(result=bool)
    def close(self):
        if self._applying:
            return False
        if not self._active and not self._tasks and self._plan is None:
            return True
        self._active = False
        if self.installing or self.searching:
            self._source_status = {}
        for kind in tuple(self._tasks):
            self._cancel(kind)
        self._plan = None
        self._import_summary = {}
        self._timer.stop()
        self.changed.emit()
        return True

    def dispose(self):
        if self.close():
            self._disposed = True

    def _source_loader(self, *, fresh=False):
        if self._source_kind == "vendor":
            spec, adapter = deepcopy(self._vendor_spec), self._vendor_adapter
            return (lambda: _vendor_source(spec)) if fresh or adapter is None else (lambda: adapter)
        factory, root = self._factory, self._root
        return lambda: factory(root)

    def _clear_source(self, kind):
        self.cancelReads()
        self._source_kind = kind
        self._source_status, self._results, self._entry, self._preview = {}, {}, {}, {}
        self._selected_id = ""
        self._query = {"query": "", "catalog": "nk", "shelf": None, "book": None,
                       "capability": None, "offset": 0, "limit": 50}
        self.changed.emit()
        self.sourceChanged.emit()

    @Slot(result=bool)
    def showRiiSource(self):
        if not self._active or self._applying or self.installing:
            return False
        self._clear_source("rii")
        return self.refresh()

    @Slot(str, str, str, str, str, str, result=bool)
    def openVendorCatalog(self, path, manufacturer, version, source_url, license_note, encoding):
        if not self._active or self._disposed or self._applying or self.installing:
            return False
        path = local_file_path(QUrl(path)) if "://" in path else path
        if not path:
            return self._fail("请选择本地 AGF 文件。")
        self._clear_source("vendor")
        self._vendor_spec = {"path": path, "manufacturer": manufacturer,
                             "catalog_version": version, "source_url": source_url,
                             "license_note": license_note, "encoding": encoding or None}
        self._vendor_adapter = None
        loader, arguments = self._source_loader(fresh=True), dict(self._query)
        def read(_cancelled):
            source = loader()
            return source, source.status(), source.search(**arguments)
        self._status = "正在读取厂商 AGF；厂商与版本为用户来源声明。"
        return self._start("vendor", read)

    @Slot(result=bool)
    def loadReferences(self):
        if not self._active or self._applying:
            return False
        self._cancel("reference_detail")
        self._reference_detail, self._reference_id = {}, ""
        def read(_cancelled):
            from coat_sim.core.reference_sources import reference_sources_catalog
            return reference_sources_catalog(limit=50)
        return self._start("references", read)

    @Slot(str, result=bool)
    def selectReference(self, identity):
        if not self._active or self._applying:
            return False
        if not any(row["id"] == identity for row in self._references.get("items", [])):
            return self._fail("请从当前来源目录选择资料。")
        version = self._references.get("version")
        self._reference_detail, self._reference_id = {}, identity
        def read(_cancelled):
            from coat_sim.core.reference_sources import reference_sources_detail
            return reference_sources_detail(identity, expected_version=version)
        return self._start("reference_detail", read)

    @Slot(result=bool)
    def refresh(self):
        return self.search(self._query["query"], self._query["catalog"],
                           self._query["shelf"] or "", self._query["book"] or "",
                           self._query["capability"] or "", self._query["offset"])

    @Slot(str, str, str, str, str, int, result=bool)
    def search(self, query, catalog, shelf, book, capability, offset):
        if not self._active or self._disposed or self._applying or self.installing or "vendor" in self._tasks:
            return False
        self.cancelImportPreview()
        self._cancel("detail")
        self._entry, self._preview, self._selected_id, self._results = {}, {}, "", {}
        self._query = {"query": query, "catalog": catalog, "shelf": shelf or None,
                       "book": book or None, "capability": capability or None,
                       "offset": offset, "limit": 50}
        loader, arguments = self._source_loader(fresh=True), dict(self._query)
        def read(_cancelled):
            store = loader()
            status = store.status()
            result = store.search(**arguments) if status.get("status") == "ready" else {}
            return status, result, store
        self._status = "正在读取本地索引…"
        return self._start("search", read)

    @Slot(int, result=bool)
    def page(self, offset):
        if not self._active or self._applying or self.installing:
            return False
        self._query["offset"] = offset
        return self.refresh()

    @Slot(str, result=bool)
    def selectEntry(self, identity):
        if not self._active or self._disposed or self._applying or self.installing or self.searching:
            return False
        if not any(row["entry_id"] == identity for row in self._results.get("entries", [])):
            return self._fail("请从当前搜索结果选择来源。")
        self.cancelImportPreview()
        self._selected_id, self._entry, self._preview = identity, {}, {}
        loader = self._source_loader()
        def read(_cancelled):
            store = loader()
            entry, preview = store.get_entry(identity), store.preview(identity, max_points=200)
            if (entry.get("snapshot_id") != preview.get("snapshot_id")
                    or entry["source_sha256"] != preview["source_sha256"]):
                raise ValueError("来源在读取期间改变，请重新选择。")
            return entry, preview
        self._status = "正在读取来源及曲线…"
        return self._start("detail", read)

    @Slot()
    def cancelImportPreview(self):
        if self._applying:
            return
        self._cancel("plan")
        self._plan, self._import_summary = None, {}
        self.changed.emit()

    def _material_library(self):
        if self.owner.libraryScope == "project":
            from coat_sim.core.project_material_library import ProjectMaterialLibrary
            return ProjectMaterialLibrary(self.owner.service, global_library=_NoGlobalPicker())
        return MaterialLibrary(directory=self.owner.service.root / "materials")

    @Slot(int, str, result=bool)
    @Slot(int, str, bool, result=bool)
    def previewImport(self, choice_index, name, allow_reference_air_approximation=False):
        if not self._active or self.busy or self._disposed:
            return False
        self.cancelImportPreview()
        error = self.owner.refractiveIndexImportBlocker()
        if error:
            return self._fail(error)
        choices = self._entry.get("conversion", {}).get("choices", [])
        if not 0 <= choice_index < len(choices):
            return self._fail("请明确选择一个可转换的数据块。")
        name = name.strip()
        if not name or len(name) > 120:
            return self._fail("材料名称须为 1–120 字。")
        if self._source_kind == "vendor" and allow_reference_air_approximation is not True:
            return self._fail("AGF 使用参考空气中的 n 和波长；请先明确确认未作温压/空气折射率换算的近似，或仅查阅资料。")
        conversion_options = ({"allow_reference_air_approximation": True}
                              if self._source_kind == "vendor" else {})
        blocks = deepcopy(choices[choice_index]["block_selection"])
        entry = deepcopy(self._entry)
        identity = self._selected_id
        loader, library = self._source_loader(fresh=True), self._material_library()
        optical_id = ("vendor_data_" if self._source_kind == "vendor" else "rii_data_") + _digest([identity, blocks])[:32]
        def prepare(_cancelled):
            converted = loader().convert_entry(identity, optical_set_id=optical_id,
                                                     name=name, block_selection=blocks, **conversion_options)
            provenance = converted["provenance"]
            if (provenance.get("snapshot_id") != entry.get("snapshot_id")
                    or provenance["source_sha256"] != entry["source_sha256"]):
                raise ValueError("来源快照已改变，请重新读取所选条目。")
            candidate = _candidate(converted, name)
            plan = library.preview_import({"kind": MATERIAL_PACKAGE_KIND, "records": [candidate]})
            return {"plan": plan["plan"], "library": library, "loader": loader, "entry_id": identity,
                    "blocks": blocks, "name": name, "optical_id": optical_id,
                    "conversion_options": conversion_options,
                    "converted_digest": _digest(converted),
                    "summary": {"name": name, "rows": plan["rows"], "provenance": provenance,
                        "missing": converted.get("missing", []), "warnings": converted.get("warnings", []),
                        "assumptions": converted.get("assumptions", [])}}
        self._status = "正在校验数据并检查目标冲突…"
        return self._start("plan", prepare)

    @Slot(str, result=bool)
    def applyImport(self, decision):
        if not self._active or self.busy or self._plan is None or self._disposed:
            return False
        error = self.owner.refractiveIndexImportBlocker()
        if error:
            return self._fail(error)
        frozen = self._plan
        row = frozen["plan"]["rows"][0]
        if row.get("conflict"):
            if decision not in {"skip", "copy", "overwrite"}:
                return self._fail("存在冲突，请明确选择跳过、副本或覆盖。")
        elif decision != "import":
            return self._fail("请明确确认导入。")
        def publish(_cancelled):
            converted = frozen["loader"]().convert_entry(frozen["entry_id"],
                optical_set_id=frozen["optical_id"], name=frozen["name"], block_selection=frozen["blocks"],
                **frozen["conversion_options"])
            if _digest(converted) != frozen["converted_digest"]:
                raise ValueError("来源在预览后改变；请重新预览，尚未写入材料。")
            return frozen["library"].apply_import(frozen["plan"], {row["id"]: decision})
        self._applying = True
        self.owner._freeze_asset_presentation(True)
        self._status = "正在写入材料，开始后不可取消；请等待明确结果。"
        if self._start("apply", publish):
            return True
        self._applying = False
        self.owner._freeze_asset_presentation(False)
        self.changed.emit()
        return False

    @Slot(str, str, result=bool)
    def installSnapshot(self, folder, commit):
        if not self._active or self.busy or self._disposed or self._source_kind != "rii":
            return False
        path = local_file_path(QUrl(folder)) if "://" in folder else folder
        if not path:
            return self._fail("请选择已下载的官方数据库目录。")
        self.cancelImportPreview()
        self._entry, self._preview, self._results, self._selected_id = {}, {}, {}, ""
        root = self._root
        def install(cancelled):
            from coat_sim.core.refractiveindex_local import build_from_directory
            return build_from_directory(path, root, upstream_commit=commit.strip().lower(),
                                        activate=True, cancel_check=cancelled.is_set)
        self._status = "正在离线检查并建立索引；提交号是来源声明，不代表联网验证。"
        return self._start("install", install)

    @Slot()
    def cancelReads(self):
        if self._applying:
            return
        was_installing = self.installing
        for kind in tuple(self._tasks):
            self._cancel(kind)
        self._plan, self._import_summary = None, {}
        if was_installing:
            self._source_status = {}
        self._status = "已取消当前读取。重新刷新可检查实际本地快照。"
        self.changed.emit()

    @Slot()
    def _owner_changed(self):
        if self._refresh_after_commit and not self.owner.transferBusy:
            self._refresh_after_commit = False
            if self.owner.libraryFailed:
                self._status += " 资产目录刷新未完成，请使用资产库刷新；材料已保存，勿重复导入。"
                self.changed.emit()

    @Slot()
    def _poll(self):
        for kind, task in tuple(self._tasks.items()):
            generation, future, cancelled = task
            if not future.done():
                continue
            self._tasks.pop(kind, None)
            if cancelled.is_set() or generation != self._epochs.get(kind) or self._disposed:
                continue
            try:
                result = future.result()
            except Exception as exc:
                if kind == "apply":
                    self._applying = False
                    self.owner._freeze_asset_presentation(False)
                    self.importFinished.emit(False)
                    self._plan, self._import_summary = None, {}
                self._fail(exc)
                self._status = "操作未完成；原材料与已有草稿保持不变。"
                continue
            if kind == "search":
                self._source_status, self._results, source = result
                if self._source_kind == "vendor":
                    self._vendor_adapter = source
                self._status = (f"找到 {self._results.get('total', 0)} 条来源。"
                    if self._source_status.get("status") == "ready"
                    else "本地数据库尚未安装。请选择已下载的官方快照目录并建立离线库。")
            elif kind == "vendor":
                self._vendor_adapter, self._source_status, self._results = result
                self._status = "厂商目录已读取；请核对来源、参考空气与温度条件。"
            elif kind == "references":
                self._references = result
            elif kind == "reference_detail":
                self._reference_detail = result
            elif kind == "detail":
                self._entry, self._preview = result
                self._status = "来源仅供查阅；导入前请选择数据块并检查适用条件。"
                self.changed.emit()
                self.selectionReady.emit()
            elif kind == "plan":
                self._plan, self._import_summary = result, result["summary"]
                self._status = "预览已准备好，请核对来源、目标与冲突处理后确认。"
            elif kind == "install":
                self._source_status = result
                self.refresh()
            elif kind == "apply":
                self._applying = False
                self.owner._freeze_asset_presentation(False)
                self._plan, self._import_summary = None, {}
                count = sum(result.get(key, 0) for key in ("created", "copied", "overwritten"))
                self._status = f"材料已保存：新增 {result.get('created', 0)}，副本 {result.get('copied', 0)}，覆盖 {result.get('overwritten', 0)}。" if count else "已跳过；没有写入材料。"
                if count:
                    if self.owner._material_bridge is not None:
                        try:
                            self.owner._material_bridge.reload()
                        except Exception as exc:
                            self._status += " 材料列表刷新未完成：" + str(exc)
                    if self.owner.libraryScope == "project":
                        try:
                            self.owner.service.refresh_bound_film_materials()
                        except Exception as exc:
                            self._status += " 膜系材料选择器刷新未完成：" + str(exc)
                    try:
                        self._refresh_after_commit = True
                        self.owner._queue_catalog_refresh()
                    except Exception as exc:
                        self._refresh_after_commit = False
                        self._status += " 刷新未完成，材料已保存：" + str(exc)
                self.importFinished.emit(True)
            self.changed.emit()
        if not self._tasks:
            self._timer.stop()
