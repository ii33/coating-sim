from __future__ import annotations

import os
import hashlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

from tools import distribution_resources as builder


_DOCUMENTS = (
    "portable-bundle.md",
    "portable-assets.md",
    "installation.md",
)


def _make_minimal_source_tree(root: Path) -> None:
    (root / "coat_sim" / "ui" / "qml").mkdir(parents=True)
    (root / "coat_sim" / "ui" / "assets").mkdir(parents=True)
    (root / "coat_sim" / "agent" / "resources").mkdir(parents=True)
    (root / "tools").mkdir()
    (root / "docs").mkdir()

    files = (
        "launcher.py",
        "launcher.pyw",
        "README.md",
        "LICENSE",
        "tools/launch_desktop.pyw",
        "tools/build_desktop_host.py",
        "coat_sim/version.txt",
        "docs/algorithms.html",
        "docs/software_manual.html",
        *(f"docs/{name}" for name in _DOCUMENTS),
        "coat_sim/ui/qml/Main.qml",
        "coat_sim/ui/assets/icon.png",
        "coat_sim/agent/resources/software_manual.md",
    )
    for relative in files:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative.encode("utf-8"))


def test_application_resources_snapshot_and_data_mapping_include_license_and_documentation(
    tmp_path: Path, monkeypatch
) -> None:
    source_root = tmp_path / "source"
    _make_minimal_source_tree(source_root)
    monkeypatch.setattr(builder, "ROOT", source_root)

    snapshot = tmp_path / "snapshot"
    records = builder.stage_source_snapshot(snapshot)
    staged_paths = {record["path"] for record in records}
    assert all(f"docs/{name}" in staged_paths for name in _DOCUMENTS)
    assert "LICENSE" in staged_paths
    assert (snapshot / "LICENSE").read_bytes() == (source_root / "LICENSE").read_bytes()

    qml_imports = tmp_path / "qt-qml-imports"
    qml_imports.mkdir()
    fake_qt_core = ModuleType("PySide6.QtCore")
    fake_qt_core.__dict__["QLibraryInfo"] = SimpleNamespace(
        LibraryPath=SimpleNamespace(QmlImportsPath=object()),
        path=lambda _kind: str(qml_imports),
    )
    fake_qyside = ModuleType("PySide6")
    fake_qyside.__path__ = []
    monkeypatch.setitem(sys.modules, "PySide6", fake_qyside)
    monkeypatch.setitem(sys.modules, "PySide6.QtCore", fake_qt_core)

    data_stage = tmp_path / "data-stage"
    data_stage.mkdir()
    command: list[str] = []
    builder.add_qt_and_source_data_arguments(command, data_stage, snapshot)

    mappings = {}
    for index, item in enumerate(command[:-1]):
        if item == "--add-data":
            source, destination = command[index + 1].rsplit(os.pathsep, 1)
            mappings[Path(source).name] = destination
    assert all(mappings[name] == "docs" for name in _DOCUMENTS)
    assert mappings["version.txt"] == "coat_sim"
    assert mappings["LICENSE"] == "."


def test_application_resources_stages_cie_local_license_targets_at_data_root(
    tmp_path: Path,
) -> None:
    package_assets = tmp_path / "package-assets"
    copied = builder.prepare_static_data(package_assets, source_root=builder.ROOT)
    expected = {
        "film_design/cie-illuminants-20261006/NOTICE_DATA.md": "e2d8009de670266d08469e786a1e1db3d37b525bf866487f1e66746d2cbea351",
        "film_design/cie-illuminants-20261006/LICENSE_DATA_CC_BY_SA_4_0.txt": "091d08965bb70d444daccb62c5bcc4345cd4d6a65267da1f06564c95d25d9abb",
    }
    staged_data = package_assets / "data"
    for relative, expected_sha256 in expected.items():
        staged = staged_data / relative
        assert staged.is_file()
        assert f"data/{relative}" in copied
        assert hashlib.sha256(staged.read_bytes()).hexdigest() == expected_sha256
