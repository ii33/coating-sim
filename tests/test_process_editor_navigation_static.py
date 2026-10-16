"""Static wiring checks for same-process design navigation and send decisions."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "coat_sim/ui"
QML = UI / "qml"


def _read(path):
    return path.read_text(encoding="utf-8")


def test_launcher_path_entrypoints_load_explicit_documents():
    process = _read(UI / "process.py")
    main = _read(UI / "main.py")
    machine_mask = _read(UI / "machine_mask.py")
    source_substrate = _read(UI / "source_substrate.py")

    assert "def openDocument(self, path: str)" in process
    assert "self.openPath(QUrl.fromLocalFile(str(_local_path(path))))" in process
    assert 'def openDocument(self, path: str)' in main
    assert 'self.chooseFile("process", QUrl.fromLocalFile(str(local)))' in main
    assert "def openDocument(self, path: str)" in machine_mask
    assert "self.loadDocument(path)" in machine_mask
    assert "def openDocument(self, path: str)" in source_substrate
    assert "self.openPath(path)" in source_substrate


def test_each_design_bridge_type_exposes_the_launcher_open_method():
    from coat_sim.ui.main import MainBridge
    from coat_sim.ui.machine_mask import MachineBridge, MaskBridge
    from coat_sim.ui.process import ProcessBridge
    from coat_sim.ui.source_substrate import SourceBridge, SubstrateBridge

    for bridge in (MainBridge, ProcessBridge, MachineBridge, MaskBridge,
                   SourceBridge, SubstrateBridge):
        assert callable(getattr(bridge, "openDocument", None)), bridge.__name__


def test_process_machine_source_mask_and_substrate_routes_use_shared_registry():
    main = _read(QML / "MainWindow.qml")
    process = _read(QML / "ProcessWindow.qml")
    machine = _read(QML / "MachineWindow.qml")
    mask = _read(QML / "MaskWindow.qml")

    assert 'objectName: "processEditorActionButton"' in main
    assert 'launcher.openWindow("process", bridge.paths.process || "")' in main
    assert 'text: bridge.paths.process.length > 0 ? "编辑工艺" : "新建工艺"' in main
    assert 'readonly property var moduleLauncher: typeof launcher !== "undefined" ? launcher : null' in process
    assert 'window.projectResources.openDependencyEditor("machine_file")' in process
    assert 'window.projectResources.openDependencyEditor("substrate_file")' in process
    assert 'window.moduleLauncher.openWindow(' in process
    assert '"machine", window.editorBridge.dependencyPath("machine_file")' in process
    assert '"substrate", window.editorBridge.dependencyPath("substrate_file")' in process
    assert 'win.editorBridge.dependencyPath("/sources/" + sourceSlot.slotIndex + "/file", sourceSlot.modelData.file || "")' in machine
    assert 'win.projectResources.openDependencyEditor("/sources/" + sourceSlot.slotIndex + "/file")' in machine
    assert 'win.moduleLauncher.openWindow("source", sourceEditorPath)' in machine
    assert 'win.editorBridge.dependencyPath("/masks/" + maskSlot.slotIndex + "/file", maskSlot.modelData.file || "")' in machine
    assert 'win.projectResources.openDependencyEditor("/masks/" + maskSlot.slotIndex + "/file")' in machine
    assert 'win.moduleLauncher.openWindow("mask", maskEditorPath)' in machine
    assert 'launcher.openWindow("machine", editorBridge.machinePath)' in mask
    assert 'win.projectResources.openDependencyEditor("machine")' in mask


def test_dirty_process_send_requires_explicit_save_and_then_uses_saved_path():
    process = _read(QML / "ProcessWindow.qml")
    request = process.split("function sendToSimulation()", 1)[1].split(
        "function saveAndSendToSimulation()", 1)[0]
    save = process.split("function saveAndSendToSimulation()", 1)[1].split(
        "Component.onCompleted", 1)[0]

    assert "if (bridge.dirty)" in request
    assert "saveBeforeSendDialog.open()" in request
    assert 'launcher.openWindow("main", bridge.currentPath)' in request
    assert "saveBeforeSendAsDialog.open()" in request
    assert "bridge.save()" in save
    assert "if (!bridge.dirty && bridge.currentPath)" in save
    assert 'launcher.openWindow("main", bridge.currentPath)' in save
    assert "当前工艺有未保存修改" in process
    assert "保存完成后再点“发送至仿真”" in process


def test_dirty_process_replacement_confirms_and_rejects_stale_decisions():
    process = _read(UI / "process.py")
    qml = _read(QML / "ProcessWindow.qml")

    assert "documentReplaceRequested.emit()" in process
    assert "expected_data != self._data" in process
    assert "expected_path != self._path" in process
    assert "function onDocumentReplaceRequested()" in qml
    assert "bridge.confirmDocumentReplace(true)" in qml
    assert "bridge.confirmDocumentReplace(false)" in qml


def test_design_dialogs_only_offer_canonical_file_extensions():
    expected = {
        "MainWindow.qml": "*.cproc",
        "ProcessWindow.qml": "*.cproc",
        "MachineWindow.qml": "*.cmac",
        "MaskWindow.qml": "*.cmsk",
        "SourceWindow.qml": "*.csrc",
        "SubstrateWindow.qml": "*.csub",
    }
    legacy = ("*.coatprocess", "*.coatsource", "*.coatsubstrate",
              "*.coatmachine", "*.coatmask", "*.json")

    for name, canonical in expected.items():
        contents = _read(QML / name)
        filters = [line for line in contents.splitlines() if "nameFilters" in line]
        assert any(canonical in line for line in filters), name
        assert not any(token in line for line in filters for token in legacy), name


def test_uniformity_sidebar_keeps_point_hint_and_global_status():
    main = _read(QML / "MainWindow.qml")

    assert 'text: "均匀性分析"' in main
    assert "ThemeControls.ThemeSidebar {" in main
    assert 'objectName: "uniformityRowsGrid"' in main
    assert "model: bridge.uniformityRows" in main
    assert 'objectName: "uniformityLabel_" + modelData.key' in main
    assert 'objectName: "uniformityValue_" + modelData.key' in main
    assert "wrapMode: Text.Wrap" in main
    assert 'objectName: "depositionSelectionInfo"' in main
    assert "statusText: bridge.status" in main
