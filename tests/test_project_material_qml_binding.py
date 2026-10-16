"""Regression coverage for material-bridge Python/QML notify signals."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.contract


def test_project_material_selection_properties_bind_without_qt_crash(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = r'''
from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent
from coat_sim.core.project_store import ProjectStore
from coat_sim.core.project_resource_service import ProjectResourceService
from coat_sim.core.materials import default_material_profile
from coat_sim.ui.project_material_library import ProjectMaterialLibraryBridge

class BrokenCatalog:
    def catalog_records(self):
        raise ValueError("bad global JSON fixture")

app = QGuiApplication([])
store = ProjectStore.create(__import__("pathlib").Path(__import__("sys").argv[1]), "QML signal regression")
service = ProjectResourceService(store)
profile = default_material_profile("Owned profile", id="owned-profile")
store.create_document("material_profile", {
    "kind": "coat_sim.project_material_profile", "version": 1, "material": profile,
})
bridge = ProjectMaterialLibraryBridge(service, global_library=BrokenCatalog())
assert "bad global JSON fixture" in bridge.status
assert "owned-profile" in bridge._project_material_ids
assert bridge.project_library.read("owned-profile")["record"] == profile
engine = QQmlApplicationEngine()
engine.rootContext().setContextProperty("bridge", bridge)
component = QQmlComponent(engine)
qml = b"""import QtQml 2.0\nQtObject {
    property string selectedId: bridge.currentId
    property bool canDeleteMaterial: bridge.canDelete
    property bool globalSelected: bridge.globalSourceSelected
    property string statusText: bridge.status
}"""
component.setData(qml, QUrl())
obj = component.create()
assert obj is not None, [error.toString() for error in component.errors()]
assert obj.property("selectedId") == ""
assert obj.property("canDeleteMaterial") is False
assert obj.property("globalSelected") is False
engine.deleteLater()
bridge.deleteLater()
app.processEvents()
'''
    env = os.environ.copy()
    env.update(QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software")
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "project")],
        cwd=root, env=env, capture_output=True, text=True, timeout=45,
    )
    assert completed.returncode == 0, (
        f"Qt child exited {completed.returncode}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
