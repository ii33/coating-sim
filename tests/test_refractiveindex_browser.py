"""Real offline source reads and scoped Qt import/lifecycle contracts."""
from __future__ import annotations

from copy import deepcopy
import importlib
from pathlib import Path
from threading import Event
from typing import Any, cast

import pytest
from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication, QRawFont
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from coat_sim.core.material_business import MaterialLibrary
from coat_sim.core.refractiveindex_local import RefractiveIndexLocalStore, build_from_directory
from coat_sim.ui.global_resources import GlobalResourcesBridge
from coat_sim.ui.settings import apply_font_settings
from coat_sim.ui.theme_service import ThemeEngineBridge
from coat_sim.ui.theme_tokens import build_theme_snapshot
from tests.test_launcher_information_global_resources import global_root as global_root
from tests.test_project_last_window_close import desktop as desktop, drain_events, spin_until
from tests.test_scoped_resource_libraries import open_library, qml, settled

pytestmark = [pytest.mark.contract, pytest.mark.qt]
yaml = importlib.import_module("yaml")


@pytest.fixture(autouse=True)
def browser_fonts():
    application = cast(QGuiApplication, QGuiApplication.instance() or QGuiApplication([]))
    previous_font = QFont(application.font())
    try:
        # Match launcher startup before constructing any QML engines. Offscreen
        # Windows can start with no system fonts despite Theme naming Noto.
        apply_font_settings({}, app=application)
        family = str(build_theme_snapshot("light")["fontFamily"])
        assert family in QFontDatabase.families()
        font = QRawFont.fromFont(QFont(family))
        assert font.isValid() and font.familyName() == family
        assert all(font.supportsCharacter(ord(character)) for character in "材料折射率")
        yield
    finally:
        application.setFont(previous_font)


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    source, store = tmp_path / "upstream", tmp_path / "offline-store"
    source.mkdir()
    (source / "LICENSE").write_text("CC0 1.0 Universal\nTest source fixture.\n")
    entries = {
        "main/Glass/nk/known.yml": ("tabulated nk", "0.4 1.52 0.02\n0.6 1.50 0.01\n0.8 1.49 0.005"),
        "main/Glass/nk/unknown.yml": ("tabulated n", "0.4 1.52\n0.6 1.50\n0.8 1.49"),
        "main/Glass/nk/unsupported.yml": ("tabulated epsilon", "0.4 2.0\n0.6 2.1"),
        "main/Glass/n2/nonlinear.yml": ("tabulated n2", "0.4 1e-20\n0.6 2e-20"),
    }
    for name, (kind, data) in entries.items():
        target = source / "database/data" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump({"REFERENCES": "Fixture reference <script>never execute</script>",
            "COMMENTS": "Measured fixture; not a production recipe", "CONDITIONS": {"temperature": 293},
            "DATA": [{"type": kind, "data": data}]}), encoding="utf-8")
    for catalog in ("nk", "n2"):
        pages = [{"PAGE": Path(path).stem, "name": Path(path).stem, "data": path}
                 for path in entries if f"/{catalog}/" in path]
        catalog_document = [{"SHELF": "main", "name": "Main", "content": [
            {"BOOK": "Glass", "name": "Glass", "content": pages}]}]
        (source / f"database/catalog-{catalog}.yml").write_text(yaml.safe_dump(catalog_document), encoding="utf-8")
    result = build_from_directory(source, store, upstream_commit="a" * 40)
    monkeypatch.setattr("coat_sim.core.refractiveindex_local.default_store_root", lambda: store)
    return source, store, result


def idle(browser):
    spin_until(lambda: browser.busy, lambda value: not value, "RII background tasks")


def choose(browser, suffix="/known.yml", catalog="nk"):
    assert browser.search("", catalog, "", "", "", 0)
    idle(browser)
    row = next(row for row in browser.results["entries"] if row["source_path"].endswith(suffix))
    assert browser.selectEntry(row["entry_id"])
    idle(browser)
    assert browser.entry["entry_id"] == row["entry_id"]
    return row


@pytest.fixture
def global_browser(global_root, snapshot):
    owner: Any = GlobalResourcesBridge()
    settled(owner)
    assert owner.openRefractiveIndexBrowser()
    browser = owner.refractiveIndexBrowser
    idle(browser)
    yield owner, browser, global_root
    owner.dispose()
    owner.deleteLater()
    drain_events()


def test_real_offline_browse_raw_curves_unknown_k_and_reference_only(global_browser):
    owner, browser, root = global_browser
    assert browser.sourceStatus["status"] == "ready"
    assert browser.results["total"] == 3
    assert not list((root / "data/materials").glob("*.json"))
    choose(browser, "/unknown.yml")
    assert "<script>" in browser.entry["raw_yaml"]["text"]
    assert browser.preview["blocks"][0]["columns"] == ["wavelength_um", "n"]
    assert browser.previewImport(0, "Glass source with unknown k")
    idle(browser)
    assert browser.importSummary["missing"]
    profile = browser._plan["plan"]["records"][0]
    assert profile["optical_only"] and profile["resistance_model"] is None
    assert profile["electron_beam_model"] is None and "roles" not in profile
    assert profile["optical_constants"][0].get("k") is None
    browser.cancelImportPreview()
    choose(browser, "/nonlinear.yml", "n2")
    assert browser.preview["blocks"][0]["columns"] == ["wavelength_um", "n2"]
    assert browser.entry["conversion"]["status"] == "unsupported"
    assert not browser.previewImport(0, "Nonlinear must stay reference only")
    choose(browser, "/unsupported.yml")
    assert browser.entry["conversion"]["choices"] == []
    assert not browser.previewImport(0, "Unsupported")
    assert not list((root / "data/materials").glob("*.json"))
    assert owner.assetCancellable


def test_global_import_conflicts_default_skip_copy_and_stale_target(global_browser):
    owner, browser, root = global_browser
    choose(browser)
    assert browser.previewImport(0, "Imported glass")
    idle(browser)
    assert browser.importSummary["rows"][0]["conflict"] == ""
    identity = browser.importSummary["rows"][0]["id"]
    assert browser.applyImport("import")
    assert owner.assetApplying and not owner.assetCancellable
    assert not browser.close() and not browser.applyImport("import")
    idle(browser)
    settled(owner)
    library = MaterialLibrary(directory=root / "data/materials")
    saved = library.read(identity)["record"]
    assert saved["name"] == "Imported glass" and saved["optical_only"]
    assert len(owner.transferCatalog["rows"]) >= 1
    assert browser.previewImport(0, "Changed name")
    idle(browser)
    assert browser.importSummary["rows"][0]["conflict"] == "custom"
    assert not browser.applyImport("")
    assert browser.applyImport("skip")
    idle(browser)
    assert library.read(identity)["record"] == saved
    assert browser.previewImport(0, "Copy glass")
    idle(browser)
    assert browser.applyImport("copy")
    idle(browser)
    settled(owner)
    assert len(library.catalog_records()) == 2
    assert browser.previewImport(0, "Stale overwrite")
    idle(browser)
    externally_changed = deepcopy(saved)
    externally_changed["notes"] += "\nConcurrent explicit edit"
    library.write(externally_changed, update=True)
    assert browser.applyImport("overwrite")
    idle(browser)
    assert "changed" in browser.errorText.lower()
    assert library.read(identity)["record"] == externally_changed


def test_source_change_after_preview_rejects_apply(global_browser, snapshot):
    _owner, browser, root = global_browser
    source, store, _status = snapshot
    choose(browser)
    assert browser.previewImport(0, "Frozen glass")
    idle(browser)
    path = source / "database/data/main/Glass/nk/known.yml"
    path.write_text(path.read_text().replace("1.52", "1.62"))
    build_from_directory(source, store, upstream_commit="b" * 40)
    assert browser.applyImport("import")
    idle(browser)
    assert "来源" in browser.errorText and "改变" in browser.errorText
    assert not list((root / "data/materials").glob("*.json"))


def test_selection_and_close_discard_late_results(global_browser, monkeypatch):
    _owner, browser, _root = global_browser
    rows = browser.results["entries"]
    first, second = rows[0]["entry_id"], rows[1]["entry_id"]
    started, release = Event(), Event()
    original = RefractiveIndexLocalStore.get_entry
    def delayed(store, identity, **kwargs):
        if identity == first:
            started.set()
            assert release.wait(6)
        return original(store, identity, **kwargs)
    monkeypatch.setattr(RefractiveIndexLocalStore, "get_entry", delayed)
    try:
        assert browser.selectEntry(first)
        assert started.wait(2)
        assert browser.selectEntry(second)
        idle(browser)
        assert browser.entry["entry_id"] == second
        release.set()
        QTest.qWait(80)
        assert browser.entry["entry_id"] == second
        assert browser.close() and not browser.active
        assert browser.open() and browser.entry["entry_id"] == second
    finally:
        release.set()


def test_search_reordering_and_cancelled_plan_cannot_publish(global_browser, monkeypatch):
    _owner, browser, root = global_browser
    started, release = Event(), Event()
    original = RefractiveIndexLocalStore.search
    def delayed(store, query="", **kwargs):
        if query == "known":
            started.set()
            assert release.wait(6)
        return original(store, query, **kwargs)
    monkeypatch.setattr(RefractiveIndexLocalStore, "search", delayed)
    try:
        assert browser.search("known", "nk", "", "", "", 0)
        assert started.wait(2)
        assert browser.search("unsupported", "nk", "", "", "", 0)
        idle(browser)
        release.set()
        QTest.qWait(60)
        assert browser.results["query"] == "unsupported"
        choose(browser)
        assert browser.previewImport(0, "Cancelled")
        browser.cancelImportPreview()
        QTest.qWait(80)
        assert browser.importSummary == {} and not browser.applyImport("import")
        assert not list((root / "data/materials").glob("*.json"))
    finally:
        release.set()


def test_missing_store_install_and_damaged_source_are_distinct(global_root, snapshot, monkeypatch):
    source, store, _status = snapshot
    missing = store.parent / "initially-missing"
    monkeypatch.setattr("coat_sim.core.refractiveindex_local.default_store_root", lambda: missing)
    owner: Any = GlobalResourcesBridge()
    settled(owner)
    try:
        assert owner.openRefractiveIndexBrowser()
        browser = owner.refractiveIndexBrowser
        idle(browser)
        assert browser.sourceStatus["status"] == "missing"
        assert "尚未安装" in browser.statusText
        assert browser.installSnapshot(str(source), "A" * 40)
        idle(browser)
        assert browser.sourceStatus["status"] == "ready" and browser.results["total"] == 3
        choose(browser)
        selected = browser.entry
        path = missing / "versions" / selected["snapshot_id"] / "source/database/data" / selected["source_path"]
        path.write_text("DATA: []\n")
        assert browser.selectEntry(selected["entry_id"])
        idle(browser)
        assert browser.errorText and not browser.entry
    finally:
        owner.dispose()
        owner.deleteLater()
        drain_events()


def test_dirty_material_draft_is_retained_and_blocks_import(global_browser):
    owner, browser, _root = global_browser
    assert owner._ensure_material_editor()
    spin_until(lambda: owner.materialLibrary.loading, lambda value: not value, "material catalog")
    owner.materialLibrary.setField("notes", "Retained unsaved material input")
    before = owner.materialLibrary.draftData()
    choose(browser)
    assert not browser.previewImport(0, "Blocked import")
    assert "未保存" in browser.errorText
    assert owner.materialLibrary.draftData() == before


def test_project_import_uses_current_owner_and_preserves_designs(desktop, snapshot, global_root):
    first, second = desktop.open("RII Target"), desktop.open("RII Unrelated")
    _identity, _route, (_window, owner, *_rest) = open_library(desktop, first)
    before_second = deepcopy(second.store.manifest)
    before_film = first.workspace.editor.snapshot()
    assert owner.openRefractiveIndexBrowser()
    browser = owner.refractiveIndexBrowser
    idle(browser)
    choose(browser)
    assert browser.previewImport(0, "Project only glass")
    idle(browser)
    assert browser.applyImport("import")
    idle(browser)
    settled(owner)
    assert not browser.errorText, browser.errorText
    kinds = [row["kind"] for row in owner.service.store.manifest["documents"]]
    assert kinds.count("material_profile") == 1 and kinds.count("optical_material") == 1
    assert second.store.manifest == before_second
    after_film = first.workspace.editor.snapshot()
    assert after_film.design == before_film.design
    assert after_film.design_revision == before_film.design_revision
    assert after_film.dirty == before_film.dirty
    assert not list((global_root / "data/materials").glob("*.json"))
    browser.close()


def test_successful_write_and_failed_refresh_are_reported_separately(global_browser, monkeypatch):
    owner, browser, root = global_browser
    choose(browser)
    assert browser.previewImport(0, "Saved despite refresh problem")
    idle(browser)
    identity = browser.importSummary["rows"][0]["id"]
    def broken_refresh():
        raise OSError("fixture refresh failed")
    monkeypatch.setattr(owner, "_queue_catalog_refresh", broken_refresh)
    assert browser.applyImport("import")
    idle(browser)
    assert "材料已保存" in browser.statusText and "刷新未完成" in browser.statusText
    assert MaterialLibrary(directory=root / "data/materials").read(identity)["record"]
    assert not browser.applyImport("import")


def test_apply_is_non_cancellable_and_refresh_does_not_replace_clean_draft(global_browser, monkeypatch):
    owner, browser, root = global_browser
    assert owner._ensure_material_editor()
    spin_until(lambda: owner.materialLibrary.loading, lambda value: not value, "initial material load")
    original_draft = owner.materialLibrary.draftData()
    choose(browser)
    assert browser.previewImport(0, "Committed exactly once")
    idle(browser)
    started, release = Event(), Event()
    original = MaterialLibrary.apply_import
    calls = []
    def delayed(library, plan, decisions):
        calls.append(deepcopy(decisions))
        started.set()
        assert release.wait(6)
        return original(library, plan, decisions)
    monkeypatch.setattr(MaterialLibrary, "apply_import", delayed)
    try:
        assert browser.applyImport("import") and started.wait(2)
        assert owner.assetApplying and not owner.assetCancellable
        assert not browser.close()
        browser.cancelReads()
        owner.dispose()
        assert not owner._disposed
        assert not owner.browseLibrary()
        assert not browser.applyImport("import")
        assert not browser.search("", "nk", "", "", "", 0)
        assert not browser.page(50)
        assert not browser.installSnapshot("/unrelated", "a" * 40)
    finally:
        release.set()
    idle(browser)
    settled(owner)
    spin_until(lambda: owner.materialLibrary.loading, lambda value: not value, "refreshed material catalog")
    assert len(calls) == 1 and len(list((root / "data/materials").glob("*.json"))) == 1
    assert owner.materialLibrary.draftData() == original_draft
    assert any(row["name"] == "Committed exactly once" for row in owner.materialLibrary.entries)
    assert owner.assetCancellable and not owner.assetApplying


def test_async_catalog_failure_after_commit_still_reports_saved(global_browser, monkeypatch):
    owner, browser, root = global_browser
    choose(browser)
    assert browser.previewImport(0, "Saved before failed catalog")
    idle(browser)
    def fail_read(*_args):
        raise OSError("fixture catalog unavailable")
    monkeypatch.setattr(owner.service, "inspect_resources", fail_read)
    assert browser.applyImport("import")
    idle(browser)
    settled(owner)
    spin_until(lambda: owner.libraryFailed, bool, "failed catalog refresh")
    assert "材料已保存" in browser.statusText
    assert "资产目录刷新未完成" in browser.statusText
    assert len(list((root / "data/materials").glob("*.json"))) == 1


def test_cancelled_install_finishes_without_reopening_disposed_view(global_root, snapshot, monkeypatch):
    source, _store, _status = snapshot
    target = source.parent / "cancelled-store"
    monkeypatch.setattr("coat_sim.core.refractiveindex_local.default_store_root", lambda: target)
    started, release, finished = Event(), Event(), Event()
    def delayed(*args, **kwargs):
        started.set()
        assert release.wait(6)
        try:
            return build_from_directory(*args, **kwargs)
        finally:
            finished.set()
    monkeypatch.setattr("coat_sim.core.refractiveindex_local.build_from_directory", delayed)
    owner: Any = GlobalResourcesBridge()
    settled(owner)
    assert owner.openRefractiveIndexBrowser()
    browser = owner.refractiveIndexBrowser
    idle(browser)
    try:
        assert browser.installSnapshot(str(source), "a" * 40)
        assert started.wait(2)
        owner.dispose()
        assert owner._disposed and not browser.active
    finally:
        release.set()
    assert finished.wait(3)
    QTest.qWait(50)
    assert not (target / "CURRENT").exists()
    assert not browser.active and not browser.busy
    owner.deleteLater()
    drain_events()


def test_pagination_and_filters_use_core_index(global_browser, snapshot):
    _owner, browser, _root = global_browser
    source, store, _status = snapshot
    for number in range(55):
        path = source / f"database/data/uncatalogued/nk/sample{number:03d}.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("DATA:\n  - type: tabulated n\n    data: |\n      0.4 1.5\n      0.8 1.4\n")
    build_from_directory(source, store, upstream_commit="b" * 40)
    assert browser.search("", "nk", "", "", "", 0)
    idle(browser)
    assert len(browser.results["entries"]) == 50 and browser.results["has_more"]
    first_ids = {row["entry_id"] for row in browser.results["entries"]}
    assert browser.page(50)
    idle(browser)
    assert browser.results["offset"] == 50 and len(browser.results["entries"]) == 8
    assert not first_ids.intersection(row["entry_id"] for row in browser.results["entries"])
    assert browser.search("", "nk", "main", "Glass", "supported", 0)
    idle(browser)
    assert browser.results["total"] == 2
    assert browser.search("no-such-source", "nk", "", "", "", 0)
    idle(browser)
    assert browser.results["entries"] == [] and not browser.errorText


@pytest.mark.parametrize("theme_name,width,font_scale", [("light", 1100, 1.0), ("dark", 680, 1.0), ("light", 680, 1.6)])
def test_actual_qml_browser_layout_preview_close_and_reopen(global_browser, theme_name, width, font_scale, tmp_path):
    owner, browser, _root = global_browser
    engine = QQmlEngine()
    theme = ThemeEngineBridge(build_theme_snapshot(theme_name, font_scale=font_scale), 0, theme_name, theme_name,
                              "qa", "QA", {}, parent=engine)
    engine.rootContext().setContextProperty("Theme", theme)
    engine.rootContext().setContextProperty("bridge", owner)
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(
        Path(__file__).parents[1] / "coat_sim/ui/qml/ResourceLibraryWindow.qml")))
    window = cast(Any, component.create())
    try:
        assert window is not None, component.errorString()
        window.resize(width, 820)
        window.show()
        QTest.qWait(100)
        assert window.findChild(QQuickItem, "refractiveIndexDatabaseButton") is not None
        choose(browser)
        QTest.qWait(80)
        popup = window.findChild(QObject, "refractiveIndexBrowserDialog")
        assert popup is not None and popup.property("visible")
        assert popup.property("width") <= window.width()
        assert popup.property("height") <= window.height()
        field = window.findChild(QQuickItem, "riiImportMaterialName")
        assert field is not None and field.property("text") == browser.entry["name"]
        block = cast(Any, window.findChild(QQuickItem, "riiBlockChoice"))
        assert block.property("currentIndex") == -1
        block.setProperty("currentIndex", 0)
        assert browser.previewImport(0, str(field.property("text")))
        idle(browser)
        QTest.qWait(80)
        assert popup.property("visible") and browser.importSummary["rows"]
        image = window.grabWindow()
        assert not image.isNull()
        assert image.save(str(tmp_path / f"rii-browser-{theme_name}-{width}-{font_scale}.png"))
        assert browser.close()
        QTest.qWait(40)
        assert not popup.property("visible")
        assert owner.openRefractiveIndexBrowser()
        QTest.qWait(40)
        assert popup.property("visible") and browser.entry["entry_id"]
        assert qml(engine, window, "resourceBridge.refractiveIndexBrowser.active")
    finally:
        browser.close()
        window.deleteLater()
        engine.deleteLater()
        drain_events()


@pytest.fixture
def vendor_file(tmp_path):
    from tests.test_vendor_optical_catalog import HEADER, SELLMEIER
    path = tmp_path / "synthetic.agf"
    path.write_text(HEADER + SELLMEIER + "NM UNIMPLEMENTED 5 000000 1.5 50\nCD 1 2 3 4\nLD .4 2.5\n")
    return path


def vendor_choose(browser, path, grade="SYNTH-A"):
    assert browser.openVendorCatalog(str(path), "Synthetic vendor", "v1", "https://example.invalid/catalog", "Fixture only", "")
    idle(browser)
    assert not browser.errorText, browser.errorText
    assert browser.sourceKind == "vendor" and "upstream_commit" not in browser.sourceStatus
    row = next(row for row in browser.results["entries"] if row["grade"] == grade)
    assert browser.selectEntry(row["entry_id"])
    idle(browser)
    assert not browser.errorText, browser.errorText
    return row


def test_vendor_real_core_preview_import_preserves_air_temperature_unknown_k(global_browser, vendor_file):
    import json
    from coat_sim.core.optical_constants import evaluate_optical_set
    owner, browser, root = global_browser
    row = vendor_choose(browser, vendor_file)
    assert row["data_quality"] == "source_inconsistent"
    assert browser.entry["conditions"]["reference_temperature_c"] == 22
    assert browser.entry["conditions"]["reference_index_medium"] == "relative_to_reference_air"
    assert "<script>" in browser.entry["raw_source"]["text"]
    assert all(point[2] is None for point in browser.preview["blocks"][0]["points"])
    assert not browser.previewImport(0, "AGF fixture")
    assert "参考空气" in browser.errorText
    assert browser.previewImport(0, "AGF fixture", True)
    idle(browser)
    assert not browser.errorText, browser.errorText
    profile = browser._plan["plan"]["records"][0]
    provenance = json.loads(profile["notes"])["vendor_source"]
    assert provenance["manufacturer"] == "Synthetic vendor" and provenance["catalog_version"] == "v1"
    assert provenance["data_quality"] == "source_inconsistent" and provenance["warnings"]
    assert provenance["block_selection"] == [] and provenance["internal_transmittance_policy"] == "reference_only"
    assert provenance["reference_air_approximation"] is True
    assert any(row["name"] == "reference_air_approximation"
               for row in profile["optical_constants"][0]["process_parameters"])
    assert evaluate_optical_set(profile["optical_constants"][0], 600, "nm")[1] is None
    assert browser.applyImport("import")
    idle(browser)
    settled(owner)
    paths = list((root / "data/materials").glob("*.json"))
    assert len(paths) == 1 and '"vendor_source"' in paths[0].read_text().replace('\\"', '"')
    assert browser.close() and browser.open()
    assert browser.sourceKind == "vendor" and browser.entry["entry_id"] == row["entry_id"]
    assert browser.showRiiSource()
    idle(browser)
    assert browser.sourceKind == "rii" and browser.sourceStatus["upstream_commit"] == "a" * 40


def test_vendor_changed_after_preview_and_unsupported_do_not_write(global_browser, vendor_file):
    _owner, browser, root = global_browser
    vendor_choose(browser, vendor_file, "UNIMPLEMENTED")
    assert browser.entry["raw_source"]["text"] and not browser.preview["blocks"]
    assert not browser.previewImport(0, "Unsupported")
    vendor_choose(browser, vendor_file)
    assert browser.previewImport(0, "Changed file", True)
    idle(browser)
    vendor_file.write_text(vendor_file.read_text() + "! changed after preview\n")
    assert browser.applyImport("import")
    idle(browser)
    assert browser.errorText and not list((root / "data/materials").glob("*.json"))
    pdf = vendor_file.with_suffix(".pdf")
    pdf.write_bytes(b"%PDF fixture, not optical constants")
    assert browser.openVendorCatalog(str(pdf), "Merck", "fixture", "", "", "")
    idle(browser)
    assert browser.errorText and not browser.results and not browser.preview
    assert browser.sourceStatus.get("status") != "ready"


def test_vendor_late_read_cannot_replace_rii_source(global_browser, vendor_file, monkeypatch):
    import coat_sim.ui.refractiveindex_browser as module
    _owner, browser, _root = global_browser
    real, started, release = module._vendor_source, Event(), Event()
    def delayed(spec):
        started.set()
        assert release.wait(5)
        return real(spec)
    monkeypatch.setattr(module, "_vendor_source", delayed)
    assert browser.openVendorCatalog(str(vendor_file), "Vendor", "v1", "", "", "")
    assert started.wait(2)
    try:
        assert not browser.search("", "nk", "", "", "", 0)
        assert browser.showRiiSource()
        idle(browser)
        assert browser.sourceKind == "rii"
    finally:
        release.set()
    QTest.qWait(80)
    assert browser.sourceKind == "rii" and browser.sourceStatus["upstream_commit"] == "a" * 40


def test_merck_metadata_reference_is_separate_from_installed_sources(global_browser):
    _owner, browser, root = global_browser
    previous = deepcopy(browser.sourceStatus)
    assert browser.loadReferences()
    idle(browser)
    assert any(row["id"] == "merck-patinal" for row in browser.references["items"])
    assert browser.selectReference("merck-patinal")
    idle(browser)
    detail = browser.referenceDetail
    assert detail["metadata_only"] and detail["host_installation"] == "not_checked"
    assert detail["source"]["integration"]["bundled_numeric_data"] is False
    assert browser.sourceStatus == previous and not list((root / "data/materials").glob("*.json"))


@pytest.mark.parametrize("mode,width,font_scale", [("vendor", 1100, 1.0), ("vendor", 680, 1.6), ("n2", 1100, 1.0)])
def test_actual_vendor_and_nonlinear_qml_are_truthful(global_browser, vendor_file, mode, width, font_scale, tmp_path):
    from PySide6.QtCore import Qt
    owner, browser, _root = global_browser
    engine = QQmlEngine()
    diagnostics: list[str] = []
    engine.warnings.connect(lambda warnings: diagnostics.extend(warning.toString() for warning in warnings))
    theme = ThemeEngineBridge(build_theme_snapshot("dark", font_scale=font_scale), 0, "dark", "dark",
                              "qa", "QA", {}, parent=engine)
    engine.rootContext().setContextProperty("Theme", theme)
    engine.rootContext().setContextProperty("bridge", owner)
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(
        Path(__file__).parents[1] / "coat_sim/ui/qml/ResourceLibraryWindow.qml")))
    window = cast(Any, component.create())
    try:
        assert window is not None, component.errorString()
        window.resize(width, 820)
        window.show()
        window.requestActivate()
        QTest.qWait(50)
        if mode == "vendor":
            vendor_choose(browser, vendor_file)
            consent = window.findChild(QQuickItem, "vendorReferenceAirConsent")
            assert consent is not None and not consent.property("checked")
            window.findChild(QQuickItem, "riiBlockChoice").setProperty("currentIndex", 0)
            assert not window.findChild(QQuickItem, "riiPreviewImportButton").property("enabled")
            consent.setProperty("checked", True)
            assert window.findChild(QQuickItem, "riiPreviewImportButton").property("enabled")
            assert browser.previewImport(0, "A long descriptive vendor material name " * 2, True)
            idle(browser)
        else:
            choose(browser, "/nonlinear.yml", "n2")
            window.findChild(QQuickItem, "riiCatalogFilter").setProperty("currentIndex", 1)
        QTest.qWait(80)
        def visual_items(item):
            found = [item]
            for child in item.childItems():
                found.extend(visual_items(child))
            return found
        items = visual_items(window.contentItem())
        target_label = window.findChild(QQuickItem, "riiImportTargetLabel")
        assert target_label.x() >= 0
        assert target_label.x() + target_label.width() <= target_label.parentItem().width()
        summaries = [item for item in items if item.objectName() == "riiCurveSummary"]
        assert any("λ：" in item.property("text") and "µm" in item.property("text") for item in summaries)
        if mode == "vendor":
            assert any("0.400000–2.50000 µm（参考空气）" in item.property("text") for item in summaries)
        curves = [item for item in items if item.objectName() == "riiSourceCurve"]
        if mode == "n2":
            assert not any(curve.property("visible") for curve in curves)
            rows = [item for item in items if item.objectName() == "riiNonlinearRawValues"]
            assert any("单位尚未核实" in str(row.property("text")) for row in rows)
            assert not window.findChild(QQuickItem, "riiPreviewImportButton").property("enabled")
        else:
            assert any(curve.property("visible") for curve in curves)
            assert window.findChild(QQuickItem, "riiConfirmImportButton").property("visible")
        scroll = window.findChild(QQuickItem, "riiBrowserPageScroll")
        flickable = scroll.property("contentItem")
        flickable.setProperty("contentY", max(0, flickable.property("contentHeight") - flickable.property("height")))
        QTest.qWait(80)
        assert window.grabWindow().save(str(tmp_path / f"optical-browser-{mode}-{width}-{font_scale}.png"))
        if mode == "vendor" and width == 1100:
            assert browser.loadReferences()
            idle(browser)
            reference_choice = window.findChild(QQuickItem, "opticalReferenceSourceChoice")
            assert reference_choice.property("currentIndex") == -1
            assert browser.selectReference("merck-patinal")
            idle(browser)
            assert "Merck" in reference_choice.property("currentText")
        assert not diagnostics, "\n".join(diagnostics)
        QTest.keyClick(window, Qt.Key.Key_Escape)
        QTest.qWait(80)
        assert not browser.active
    finally:
        browser.close()
        window.deleteLater()
        engine.deleteLater()
        drain_events()


def test_vendor_display_is_bounded_and_oversized_provenance_is_not_discarded(global_browser, vendor_file):
    _owner, browser, root = global_browser
    original = vendor_file.read_text()
    vendor_file.write_text("CC " + "header" * 5000 + "\n" + original.replace(
        "GC Test material", "GC " + "long source comment " * 1200 + "\nGC Test material"))
    vendor_choose(browser, vendor_file)
    assert "raw_header" not in browser.sourceStatus
    assert "raw_entry" not in browser.entry
    assert len(browser.entry["raw_source"]["text"]) == 20000
    assert browser.entry["raw_source"]["truncated"]
    assert browser.previewImport(0, "Large provenance", True)
    idle(browser)
    assert browser.errorText and not browser.importSummary
    assert not list((root / "data/materials").glob("*.json"))
