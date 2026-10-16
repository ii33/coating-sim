"""Smoke tests for the bounded, headless project persistence CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "coat_sim.project_cli", *map(str, arguments)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def success(*arguments: str) -> dict:
    completed = run_cli(*arguments)
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    return json.loads(completed.stdout)


def project_snapshot(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def assert_cli_error(completed: subprocess.CompletedProcess[str], code: str) -> None:
    assert completed.returncode != 0
    assert completed.stdout == ""
    error = json.loads(completed.stderr)
    assert set(error) == {"error"}
    assert error["error"]["code"] == code
    assert isinstance(error["error"]["message"], str)
    assert error["error"]["message"]


def test_empty_project_create_inspect_and_resolve(tmp_path: Path) -> None:
    project = tmp_path / "empty project"
    project_file = project / "project.coatproj"

    created = success("create", str(project), "--name", "Empty")
    assert created["ok"] is True
    assert isinstance(created["project"], dict)

    inspected = success("inspect", str(project_file))
    assert inspected["project"] == created["project"]

    resolved = success("resolve", str(project_file))
    assert resolved["ok"] is True
    assert resolved["document_id"] is None
    assert resolved["graph"] is None


def test_import_and_resolve_point_source(tmp_path: Path) -> None:
    project = tmp_path / "project"
    success("create", str(project), "--name", "Source import")
    project_file = project / "project.coatproj"

    # This standalone point source has no external dependencies, so the smoke
    # test exercises typed import and graph resolution without peeking into the
    # graph's domain-specific fields.
    source = tmp_path / "source.csrc"
    source.write_text(
        json.dumps({
            "schema_version": 2,
            "kind": "point",
            "device_type": "custom",
            "source_exponent": 1.0,
        }),
        encoding="utf-8",
    )

    imported = success("import", str(project_file), str(source))
    document_id = imported["document_id"]
    assert isinstance(document_id, str) and document_id

    resolved = success("resolve", str(project_file), document_id)
    assert resolved["document_id"] == document_id
    assert isinstance(resolved["graph"], dict)

    destination = tmp_path / "export"
    exported = success("export", str(project_file), document_id, str(destination))
    assert exported["destination"] == str(destination)
    assert exported["root_document_id"] == document_id
    assert isinstance(exported["documents"], list)


def test_save_updates_an_existing_project_document(tmp_path: Path) -> None:
    project = tmp_path / "project"
    success("create", str(project), "--name", "Source edit")
    project_file = project / "project.coatproj"
    source = tmp_path / "source.csrc"
    original = {
        "schema_version": 2,
        "kind": "point",
        "device_type": "custom",
        "source_exponent": 1.0,
    }
    source.write_text(json.dumps(original), encoding="utf-8")
    imported = success("import", str(project_file), str(source))
    document_id = imported["document_id"]
    old_revision = imported["project"]["revision"]

    edited = {**original, "source_exponent": 2.0}
    draft = tmp_path / "edited.json"
    draft.write_text(json.dumps(edited), encoding="utf-8")
    saved = success("save", str(project_file), document_id, str(draft))
    assert saved["document_id"] == document_id
    assert saved["project"]["revision"] == old_revision + 1


def test_bad_json_input_is_reported_as_structured_stderr(tmp_path: Path) -> None:
    project = tmp_path / "project"
    success("create", str(project), "--name", "Bad input test")
    project_file = project / "project.coatproj"
    parameters = tmp_path / "parameters.json"
    parameters.write_text('{"rate": NaN}', encoding="utf-8")

    completed = run_cli(
        "configure", str(project_file), "not-a-real-process", "--name", "Bad",
        "--parameters", str(parameters),
    )
    assert completed.returncode != 0
    assert completed.stdout == ""
    error = json.loads(completed.stderr)
    assert set(error) == {"error"}
    assert error["error"]["code"] == "invalid_input"
    assert isinstance(error["error"]["message"], str)
    assert error["error"]["message"]


def test_help_does_not_import_gui_modules() -> None:
    script = r'''
import runpy
import sys
sys.argv = ["coat_sim.project_cli", "--help"]
try:
    runpy.run_module("coat_sim.project_cli", run_name="__main__")
except SystemExit as exc:
    assert exc.code == 0
else:
    raise AssertionError("--help did not exit")
assert "coat_sim.ui" not in sys.modules
assert "coat_sim.core.project_store" not in sys.modules
assert not any(name.startswith("PySide") for name in sys.modules)
'''
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout.lower()
    assert "error" not in completed.stderr.lower()


def test_managed_document_subcommand_help_is_headless() -> None:
    for command in ("create-document", "save"):
        script = f'''\
import runpy
import sys
sys.argv = ["coat_sim.project_cli", {command!r}, "--help"]
try:
    runpy.run_module("coat_sim.project_cli", run_name="__main__")
except SystemExit as exc:
    assert exc.code == 0
else:
    raise AssertionError("{command} --help did not exit")
assert "coat_sim.ui" not in sys.modules
assert "coat_sim.core.project_store" not in sys.modules
assert not any(name.startswith("PySide") for name in sys.modules)
'''
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert "usage:" in completed.stdout.lower()
        assert "--materials" in completed.stdout
        assert completed.stderr == ""


def test_cli_creates_managed_template_and_saves_new_explicit_material(tmp_path):
    from coat_sim.core.project_store import ProjectStore
    from tests.test_project_templates import expression_template, material
    root = tmp_path / "project"
    store = ProjectStore.create(root, "Template CLI")
    baseline = project_snapshot(root)
    document = expression_template("cli-template")
    template_file = tmp_path / "template.json"
    template_file.write_text(json.dumps(document), encoding="utf-8")
    materials_file = tmp_path / "materials.json"
    materials_file.write_text(json.dumps({"symbol_only": material("symbol_only")}), encoding="utf-8")

    # The CLI must reject a symbol-only resource that was not explicitly
    # supplied, without leaving either a document or a material behind.
    missing_resources = run_cli("create-document", str(store.project_file), "expression_template", str(template_file))
    assert_cli_error(missing_resources, "missing_dependency")
    assert project_snapshot(root) == baseline

    created = run_cli("create-document", str(store.project_file), "expression_template", str(template_file),
                      "--materials", str(materials_file))
    assert created.returncode == 0, created.stderr
    identity = json.loads(created.stdout)["document_id"]

    # Save transfers the newly required symbol material with the template.
    document["expression"]["symbols"]["A"]["material_id"] = "new-symbol-material"
    template_file.write_text(json.dumps(document), encoding="utf-8")
    materials_file.write_text(json.dumps({"new-symbol-material": material("new-symbol-material")}), encoding="utf-8")
    saved = run_cli("save", str(store.project_file), identity, str(template_file), "--materials", str(materials_file))
    assert saved.returncode == 0, saved.stderr
    graph = ProjectStore.open(store.project_file).resolve_document(identity)
    assert [row["id"] for row in graph["materials"]] == ["new-symbol-material"]
    assert next(row for row in graph["documents"] if row["kind"] == "expression_template")["data"]["template_id"] == "cli-template"

    committed = project_snapshot(root)
    # A rejected resource replacement must leave the edited template,
    # dependency records, manifest revision, and all bytes untouched.
    document["name"] = "This must not be committed"
    template_file.write_text(json.dumps(document), encoding="utf-8")
    materials_file.write_text(
        json.dumps({"new-symbol-material": material("new-symbol-material", n=3.0)}),
        encoding="utf-8",
    )
    failed_save = run_cli(
        "save", str(store.project_file), identity, str(template_file), "--materials", str(materials_file)
    )
    assert_cli_error(failed_save, "dependency_conflict")
    assert project_snapshot(root) == committed
    unchanged = ProjectStore.open(store.project_file).resolve_document(identity)
    template_row = next(row for row in unchanged["documents"]
                        if row["kind"] == "expression_template")
    assert template_row["data"]["name"] == "Quarter wave"
    assert unchanged["materials"] == [material("new-symbol-material")]
