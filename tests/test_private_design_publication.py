from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import subprocess
import tarfile
import zipfile

import pytest

from tools import distribution_resources as installer


@pytest.mark.parametrize(
    "tree_name,relative",
    [
        ("app", Path("sheji/private.json")),
        ("app", Path("nested/sheji/private.json")),
        ("app", Path("nested/SheJi/private.json")),
        ("app", Path("nested/SHEJI/private.json")),
        ("assets", Path("data/sheji/private.json")),
        ("assets", Path("data/nested/SheJi/private.json")),
    ],
)
def test_payload_zip_rejects_private_design_paths_before_zip_creation(
    tmp_path: Path, tree_name: str, relative: Path
) -> None:
    app_root = tmp_path / "app"
    assets = tmp_path / "assets"
    (app_root / "safe").mkdir(parents=True)
    assets.mkdir()
    (app_root / "safe" / "app.txt").write_text("application", encoding="utf-8")
    private_file = (app_root if tree_name == "app" else assets) / relative
    private_file.parent.mkdir(parents=True)
    private_file.write_text("private", encoding="utf-8")
    payload = tmp_path / "payload.zip"

    with pytest.raises(RuntimeError, match="private design path"):
        installer.write_resource_zip(payload, app_root, assets)

    assert not payload.exists()
    assert private_file.read_text(encoding="utf-8") == "private"


def test_package_tree_allows_similarly_named_benign_paths(tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    assets = tmp_path / "assets"
    app_root.mkdir()
    assets.mkdir()
    (app_root / "sheji_notes.txt").write_text("application note", encoding="utf-8")
    (assets / "my_sheji_reference.txt").write_text("asset note", encoding="utf-8")
    payload = tmp_path / "payload.zip"

    payload_bytes, records = installer.write_resource_zip(payload, app_root, assets)

    assert payload_bytes > 0
    assert {record["path"] for record in records} == {
        "sheji_notes.txt",
        "package-info/my_sheji_reference.txt",
    }
    with zipfile.ZipFile(payload) as archive:
        assert set(archive.namelist()) == {"sheji_notes.txt", "package-info/my_sheji_reference.txt"}


def test_snapshot_records_rejects_private_design_paths_in_reused_inputs(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    private_file = snapshot / "resources" / "sHeJi" / "secret.json"
    private_file.parent.mkdir(parents=True)
    private_file.write_text("secret", encoding="utf-8")

    with pytest.raises(RuntimeError, match="private design path"):
        installer.snapshot_records(snapshot)


def test_source_snapshot_and_static_staging_leave_root_sheji_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = Path(__file__).resolve().parents[1]
    project = tmp_path / "project"
    (project / "coat_sim").mkdir(parents=True)
    (project / "coat_sim" / "core.py").write_text("value = 1\n", encoding="utf-8")
    (project / "coat_sim" / "version.txt").write_bytes((project_root / "coat_sim" / "version.txt").read_bytes())
    for relative in (
        "launcher.py",
        "launcher.pyw",
        "README.md",
        "tools/launch_desktop.pyw",
        "tools/build_desktop_host.py",
        "docs/algorithms.html",
        "docs/software_manual.html",
        "docs/portable-bundle.md",
        "docs/portable-assets.md",
        "docs/installation.md",
    ):
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")

    root_design = project / "sheji" / "design.json"
    root_design.parent.mkdir()
    root_design.write_bytes(b"local design bytes\x00\xff")
    original = root_design.read_bytes()
    monkeypatch.setattr(installer, "ROOT", project)

    snapshot = project / "build" / "snapshot"
    snapshot.parent.mkdir()
    records = installer.stage_source_snapshot(snapshot)
    assert not any(
        part.casefold() == "sheji"
        for record in records
        for part in Path(str(record["path"])).parts
    )
    assert not (snapshot / "sheji").exists()
    assert root_design.read_bytes() == original

    data = project / "data"
    # Exercise the production content pins with reviewed checkout bytes, not
    # dummy resources or a replacement manifest that bypasses those pins.
    reviewed_files = json.loads(installer.STATIC_MANIFEST.read_text(encoding="utf-8"))["files"]
    expected_resources = {
        "data/" + relative: (project_root / "data" / relative).read_bytes()
        for relative in reviewed_files
    }
    for relative, content in expected_resources.items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    ignored_local = data / "machines" / "sheji" / "local.json"
    ignored_local.parent.mkdir(parents=True)
    ignored_local.write_text("unselected local data", encoding="utf-8")
    ignored_bytes = ignored_local.read_bytes()
    stage = project / "build" / "assets"

    copied = installer.prepare_static_data(stage, source_root=project)

    assert "data/README.md" in copied
    assert set(copied) == set(expected_resources)
    assert {
        path.relative_to(stage).as_posix(): path.read_bytes()
        for path in stage.rglob("*")
        if path.is_file()
    } == expected_resources
    assert not any(
        part.casefold() == "sheji"
        for path in stage.rglob("*")
        for part in path.relative_to(stage).parts
    )
    assert not (stage / "sheji").exists()
    assert root_design.read_bytes() == original
    assert ignored_local.read_bytes() == ignored_bytes
    assert not (stage / "data" / "machines" / "sheji").exists()


def test_selected_static_catalog_with_private_component_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    data = project / "data"
    (data / "optical_catalog_sources" / "sheji").mkdir(parents=True)
    (data / "optical_catalog_sources" / "sheji" / "private.json").write_text("private", encoding="utf-8")
    (data / "film_design" / "cie-illuminants-20261006").mkdir(parents=True)
    (data / "README.md").write_text("readme", encoding="utf-8")
    (data / "optical_catalog.json").write_text("{}", encoding="utf-8")
    stage = project / "build" / "assets"

    with pytest.raises(RuntimeError, match="private design path"):
        installer.prepare_static_data(stage, source_root=project)


def test_gitignore_and_export_ignore_exclude_tracked_and_new_sheji(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    repo = tmp_path / "synthetic-repo"
    repo.mkdir()
    for name in (".gitignore", ".gitattributes"):
        (repo / name).write_bytes((project_root / name).read_bytes())
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Publication regression"], cwd=repo, check=True)

    tracked = repo / "sheji" / "tracked.json"
    tracked.parent.mkdir()
    tracked.write_text("tracked local design", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", ".gitattributes"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-f", "sheji/tracked.json"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)

    new_file = repo / "sheji" / "new.json"
    new_file.write_text("new local design", encoding="utf-8")
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", "sheji/new.json"], cwd=repo, check=False
    )
    assert ignored.returncode == 0
    assert subprocess.run(["git", "ls-files", "--error-unmatch", "sheji/tracked.json"], cwd=repo, capture_output=True).returncode == 0

    archive_bytes = subprocess.check_output(["git", "archive", "--format=tar", "HEAD"], cwd=repo)
    with tarfile.open(fileobj=BytesIO(archive_bytes), mode="r:") as archive:
        member_names = archive.getnames()

    assert not any(name == "sheji" or name.startswith("sheji/") for name in member_names)
    assert tracked.read_text(encoding="utf-8") == "tracked local design"
    assert new_file.read_text(encoding="utf-8") == "new local design"
