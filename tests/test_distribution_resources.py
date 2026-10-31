from __future__ import annotations

import os
import hashlib
from pathlib import Path
import sys
import subprocess
from types import ModuleType, SimpleNamespace

import pytest

from tools import distribution_resources as builder


_DOCUMENTS = (
    "portable-bundle.md",
    "portable-assets.md",
    "installation.md",
    "assistant-knowledge-contract.md",
    "assistant-memory-research-contract.md",
    "research-library-provider-contract.md",
    "refractiveindex-local-contract.md",
    "refractiveindex-tools-contract.md",
    "refractiveindex-browser-contract.md",
    "vendor-optical-catalog-contract.md",
    "reference-sources.md",
    "film-resource-estimation-core-contract.md",
    "film-resource-estimation-ui-contract.md",
    "film-resource-prediction-core-contract.md",
)


def _make_minimal_source_tree(root: Path) -> None:
    (root / "coat_sim" / "ui" / "qml").mkdir(parents=True)
    (root / "coat_sim" / "ui" / "assets").mkdir(parents=True)
    (root / "coat_sim" / "agent" / "resources").mkdir(parents=True)
    (root / "coat_sim" / "core" / "resources").mkdir(parents=True)
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
        "coat_sim/agent/resources/research_library/manifest.json",
        "coat_sim/agent/resources/research_library/optics/thin_films.md",
        "coat_sim/core/resources/reference_sources.json",
    )
    for relative in files:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative.encode("utf-8"))
    (root / "coat_sim/version.txt").write_text("dev.20261023.01\n", encoding="utf-8")


def test_application_resources_snapshot_and_data_mapping_include_license_and_documentation(
    tmp_path: Path, monkeypatch
) -> None:
    source_root = tmp_path / "source"
    _make_minimal_source_tree(source_root)
    # Exercise the actual small model resource, without copying the full tree.
    model = "coat_sim/core/resources/film_resource_prediction_v1.json"
    model_bytes = (Path(__file__).resolve().parents[1] / model).read_bytes()
    (source_root / model).write_bytes(model_bytes)
    monkeypatch.setattr(builder, "ROOT", source_root)

    snapshot = tmp_path / "snapshot"
    records = builder.stage_source_snapshot(snapshot)
    staged_paths = {record["path"] for record in records}
    assert all(f"docs/{name}" in staged_paths for name in _DOCUMENTS)
    assert "LICENSE" in staged_paths
    assert model in staged_paths
    assert (snapshot / model).read_bytes() == model_bytes
    model_record = next(row for row in records if row["path"] == model)
    assert model_record["sha256"] == hashlib.sha256(model_bytes).hexdigest()
    # The existing agent/resources mapping must preserve nested public corpus
    # files and provenance, without a second mapping that could diverge.
    corpus_files = (
        "coat_sim/agent/resources/research_library/manifest.json",
        "coat_sim/agent/resources/research_library/optics/thin_films.md",
    )
    for relative in corpus_files:
        assert relative in staged_paths
        assert (snapshot / relative).read_bytes() == (source_root / relative).read_bytes()
    assert (snapshot / "LICENSE").read_bytes() == (source_root / "LICENSE").read_bytes()
    assert (snapshot / "coat_sim/version.txt").read_bytes() == (source_root / "coat_sim/version.txt").read_bytes()
    version_record = next(row for row in records if row["path"] == "coat_sim/version.txt")
    assert version_record["sha256"] == hashlib.sha256((source_root / "coat_sim/version.txt").read_bytes()).hexdigest()

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
    assert command[command.index("--hidden-import") + 1] == "yaml"

    mappings = {}
    for index, item in enumerate(command[:-1]):
        if item == "--add-data":
            source, destination = command[index + 1].rsplit(os.pathsep, 1)
            mappings[Path(source).name] = destination
    assert all(mappings[name] == "docs" for name in _DOCUMENTS)
    agent_mapping = str(snapshot / "coat_sim/agent/resources") + os.pathsep + "coat_sim/agent/resources"
    assert command.count(agent_mapping) == 1
    assert not any("resources/research_library" in item for item in command)
    assert mappings["version.txt"] == "coat_sim"
    assert mappings["LICENSE"] == "."
    assert str(snapshot / "coat_sim/core/resources") + os.pathsep + "coat_sim/core/resources" in command
    assert (snapshot / "coat_sim/core/resources/reference_sources.json").is_file()


@pytest.mark.parametrize("contents", [None, b"unknown\n", b"dev.20260230.01\n", b"\xff", b"x" * 129])
def test_staging_and_frozen_mapping_reject_missing_or_invalid_application_version(tmp_path, monkeypatch, contents):
    source = tmp_path / "source"
    _make_minimal_source_tree(source)
    version = source / "coat_sim/version.txt"
    if contents is None:
        version.unlink()
    else:
        version.write_bytes(contents)
    monkeypatch.setattr(builder, "ROOT", source)
    destination = tmp_path / "snapshot"
    with pytest.raises(RuntimeError, match="application version"):
        builder.stage_source_snapshot(destination)
    assert not destination.exists()
    command = ["unchanged"]
    with pytest.raises(RuntimeError, match="application version"):
        builder.add_qt_and_source_data_arguments(command, tmp_path / "data", source)
    assert command == ["unchanged"]


@pytest.mark.parametrize("value", [
    "dev.20261023.01", "dev.20261023.99", "dev.20261023.00", "dev.20260230.01",
    "dev.20261023.1", "dev.20261023.100", "dev.20261023.01\n", "unknown", "",
])
def test_standalone_package_version_grammar_matches_current_application_reader(tmp_path, value):
    from coat_sim.version import parse_internal_version

    version = tmp_path / "coat_sim/version.txt"
    version.parent.mkdir()
    version.write_text(value, encoding="utf-8")
    if parse_internal_version(value) is None:
        with pytest.raises(RuntimeError, match="application version"):
            builder.validate_application_version(tmp_path)
    else:
        assert builder.validate_application_version(tmp_path) == value.strip()


def test_distribution_helper_loads_by_file_without_source_root_on_module_path(tmp_path):
    script = Path(builder.__file__).resolve()
    root = script.parent.parent
    code = (
        "import importlib.util,sys; "
        f"spec=importlib.util.spec_from_file_location('standalone_distribution',{str(script)!r}); "
        "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        f"print(module.validate_application_version(module.Path({str(root)!r}))); "
        "assert 'coat_sim' not in sys.modules"
    )
    completed = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path,
                               check=True, capture_output=True, text=True)
    assert completed.stdout.strip() == (root / "coat_sim/version.txt").read_text(encoding="utf-8").strip()


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


def _make_static_source(root: Path, monkeypatch) -> None:
    """Synthetic, explicitly reviewed seeds; no checkout/user resources involved."""
    import json
    files = {
        "README.md": b"public readme\n",
        "optical_catalog.json": b'{"seed": "public"}',
        "materials/public-seed.json": b'{"name": "reviewed seed"}',
        "optical_catalog_sources/reference.txt": b"public reference",
        "film_design/cie-illuminants-20261006/reference.txt": b"public CIE",
    }
    for relative, content in files.items():
        path = root / "data" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    manifest = root / "reviewed-manifest.json"
    manifest.write_text(json.dumps({"schema": 1, "files": {
        name: hashlib.sha256(content).hexdigest() for name, content in files.items()
    }}))
    monkeypatch.setattr(builder, "STATIC_MANIFEST", manifest)


def test_static_manifest_excludes_custom_overrides_and_unreviewed_references_from_zip(tmp_path, monkeypatch):
    import zipfile
    source = tmp_path / "source"
    _make_static_source(source, monkeypatch)
    for name in ("materials/customer.json", "materials/.overrides/builtin.json",
                 "optical_catalog_sources/private.json"):
        path = source / "data" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('SYNTHETIC_PRIVATE')
    stage = tmp_path / "stage"
    copied = builder.prepare_static_data(stage, source)
    assert "data/materials/public-seed.json" in copied
    assert all("customer" not in name and "overrides" not in name and "private" not in name for name in copied)
    assets = tmp_path / "assets"
    assets.mkdir()
    archive = tmp_path / "resources.zip"
    builder.write_resource_zip(archive, stage, assets)
    with zipfile.ZipFile(archive) as stream:
        assert "data/materials/public-seed.json" in stream.namelist()
        assert all(b"SYNTHETIC_PRIVATE" not in stream.read(name) for name in stream.namelist())


def test_static_manifest_rejects_modified_seed_and_preserves_existing_stage(tmp_path, monkeypatch):
    import pytest
    source = tmp_path / "source"
    _make_static_source(source, monkeypatch)
    stage = tmp_path / "stage"
    sentinel = stage / "data/materials/customer.json"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("SYNTHETIC_PRIVATE")
    with pytest.raises(FileExistsError):
        builder.prepare_static_data(stage, source)
    assert sentinel.read_text() == "SYNTHETIC_PRIVATE"
    assert list((stage / "data").rglob("*.json")) == [sentinel]
    (source / "data/materials/public-seed.json").write_text("SYNTHETIC_OVERRIDE")
    with pytest.raises(RuntimeError, match="modified reviewed"):
        builder.prepare_static_data(tmp_path / "fresh", source)
    assert not (tmp_path / "fresh/data").exists()


def _symlink(link, target, *, directory=False):
    import pytest
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")


def test_static_staging_rejects_file_root_parent_and_destination_links(tmp_path, monkeypatch):
    import pytest
    source = tmp_path / "source"
    _make_static_source(source, monkeypatch)
    outside = tmp_path / "outside.txt"
    outside.write_text("SYNTHETIC_OUTSIDE")
    link = source / "data/materials/linked.json"
    _symlink(link, outside)
    with pytest.raises(RuntimeError, match="link or reparse"):
        builder.prepare_static_data(tmp_path / "stage", source)
    link.unlink()
    alias = tmp_path / "alias"
    _symlink(alias, source, directory=True)
    for source_path in (alias, alias / "."):
        with pytest.raises(RuntimeError, match="link or reparse"):
            builder.prepare_static_data(tmp_path / "stage", source_path)
    stage_alias = tmp_path / "stage-alias"
    real_stage = tmp_path / "real-stage"
    real_stage.mkdir()
    _symlink(stage_alias, real_stage, directory=True)
    with pytest.raises(RuntimeError, match="link or reparse"):
        builder.prepare_static_data(stage_alias, source)
    assert not list(real_stage.iterdir())


def test_package_roots_and_assets_links_are_rejected_before_zip_publication(tmp_path):
    import pytest
    app = tmp_path / "app"
    assets = tmp_path / "assets"
    app.mkdir()
    assets.mkdir()
    (app / "main.py").write_text("public source")
    external = tmp_path / "external.txt"
    external.write_text("SYNTHETIC_OUTSIDE")
    link = assets / "reference.txt"
    _symlink(link, external)
    output = tmp_path / "package.zip"
    output.write_bytes(b"existing output")
    with pytest.raises(RuntimeError, match="link or reparse"):
        builder.write_resource_zip(output, app, assets)
    assert output.read_bytes() == b"existing output"
    with pytest.raises(RuntimeError, match="link or reparse"):
        builder.snapshot_records(assets)
    link.unlink()
    alias = tmp_path / "alias"
    _symlink(alias, app, directory=True)
    with pytest.raises(RuntimeError, match="link or reparse"):
        builder.snapshot_records(alias)
    with pytest.raises(RuntimeError, match="link or reparse"):
        builder.copy_tree(alias, tmp_path / "copy")
    with pytest.raises(RuntimeError, match="link or reparse"):
        builder.write_resource_zip(output, alias, assets)


def test_reparse_attribute_rejected_without_following(tmp_path, monkeypatch):
    import pytest
    path = tmp_path / "junction"
    path.mkdir()
    original = Path.lstat
    def fake_lstat(self, *args, **kwargs):
        info = original(self, *args, **kwargs)
        if self == path:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(Path, "lstat", fake_lstat)
    with pytest.raises(RuntimeError, match="link or reparse"):
        builder.validate_package_tree(path / "child", context="synthetic Windows junction")


def test_readmes_link_authoritative_current_version_without_duplicate_literal():
    import re
    for name in ("README.md", "README.en.md"):
        introduction = (builder.ROOT / name).read_text(encoding="utf-8").split("##", 1)[0]
        assert "(coat_sim/version.txt)" in introduction
        assert not re.search(r"dev\.\d{8}\.\d+", introduction)


def test_failed_zip_write_preserves_existing_archive(tmp_path, monkeypatch):
    import pytest
    import zipfile
    app = tmp_path / "app"
    assets = tmp_path / "assets"
    app.mkdir()
    assets.mkdir()
    (app / "safe.txt").write_text("public")
    output = tmp_path / "out.zip"
    output.write_bytes(b"previous complete artifact")
    def fail_write(*args, **kwargs):
        raise OSError("synthetic write failure")
    monkeypatch.setattr(zipfile.ZipFile, "write", fail_write)
    with pytest.raises(OSError, match="synthetic"):
        builder.write_resource_zip(output, app, assets)
    assert output.read_bytes() == b"previous complete artifact"
    assert not list(tmp_path.glob(".resource-*"))


def test_snapshot_individual_document_and_source_parent_links_are_rejected(tmp_path, monkeypatch):
    import pytest
    source = tmp_path / "source"
    _make_minimal_source_tree(source)
    outside = tmp_path / "outside.txt"
    outside.write_text("SYNTHETIC_OUTSIDE")
    (source / "README.md").unlink()
    _symlink(source / "README.md", outside)
    monkeypatch.setattr(builder, "ROOT", source)
    with pytest.raises(RuntimeError, match="link or reparse"):
        builder.stage_source_snapshot(tmp_path / "snapshot")
    alias = tmp_path / "alias"
    _symlink(alias, tmp_path / "source", directory=True)
    with pytest.raises(RuntimeError, match="link or reparse"):
        builder.copy_tree(alias / "coat_sim", tmp_path / "copy")
