"""Export the frozen public film-synthesis suite as an isolated CPU bundle.

The exporter is the only component that imports the desktop request adapter.
The resulting archive contains frozen optical inputs and a Python import closure;
cloud execution never reads the user project, catalog, GUI, or credentials.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
from pathlib import Path, PurePosixPath
import sys
import zipfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUITE = ROOT / "benchmarks" / "film_synthesis" / "20261021" / "representative-suite-v2.1.json"
DEFAULT_PHASE_CASES = (
    "ar_v_visible", "ar_w_visible", "ar_broad_visible", "ar_ultrabroad_visible",
    "hr_narrow_visible", "splitter_narrow_normal", "bpf_narrow_visible", "mixed_hr_ar_visible",
)
ENTRY_MODULES = ("coat_sim.core.film_synthesis_io", "coat_sim.core.film_auto_synthesis")
ALLOWED_RESOURCE_PATHS = {
    "coat_sim/core/resources/zns_reference.json",
    "coat_sim/core/resources/researched_materials.json",
    "coat_sim/core/resources/optical_substrate_supplement.json",
    "coat_sim/core/resources/reference_sources.json",
}
FORBIDDEN_PARTS = {"ui", "qml", "agent", "scheduler", "credentials", "user_projects"}
DEPENDENCIES = {"numpy": ">=1.24", "scipy": ">=1.16,<2", "pydantic": ">=2.12,<3"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _module_path(module: str, root: Path) -> Path:
    return root.joinpath(*module.split(".")).with_suffix(".py")


def _relative_imports(path: Path, module: str, root: Path = ROOT) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[:len(base) - node.level + 1]
                if node.module:
                    base.extend(node.module.split("."))
                prefix = ".".join(base)
                if prefix == "coat_sim" or prefix.startswith("coat_sim."):
                    found.add(prefix)
                for alias in node.names:
                    candidate = f"{prefix}.{alias.name}"
                    if _module_path(candidate, root).is_file():
                        found.add(candidate)
            elif node.module and (node.module == "coat_sim" or node.module.startswith("coat_sim.")):
                found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names
                         if alias.name == "coat_sim" or alias.name.startswith("coat_sim."))
    return found


def import_closure(root: Path = ROOT) -> list[str]:
    pending = list(ENTRY_MODULES)
    visited: set[str] = set()
    while pending:
        module = pending.pop()
        if module in visited:
            continue
        # Core service files keep their optional durable-queue helpers behind
        # method-local imports. The cloud entry never calls those helpers.
        if (module == "coat_sim.scheduler" or module.startswith("coat_sim.scheduler.")
                or module == "coat_sim.agent" or module.startswith("coat_sim.agent.")):
            continue
        path = _module_path(module, root)
        if not path.is_file():
            package_init = root.joinpath(*module.split("."), "__init__.py")
            if package_init.is_file():
                visited.add(module)
                continue
            raise FileNotFoundError(f"Static import closure is incomplete: {module}")
        parts = set(module.split("."))
        if parts & FORBIDDEN_PARTS:
            raise ValueError(f"Pure bundle import closure contains forbidden module: {module}")
        visited.add(module)
        pending.extend(_relative_imports(path, module, root) - visited)
    return sorted(visited)


def _strict_json(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False))


def _control_identity(repo_root: Path) -> dict[str, Any]:
    path = repo_root / "benchmarks" / "film_synthesis" / "20261021" / "results" / "representative-v23-matching-final.json"
    return {"kind": "public_control_hash_only_not_copied", "path": path.relative_to(repo_root).as_posix(),
            "sha256": _sha(path.read_bytes()) if path.is_file() else None}


def build_bundle(suite_path: Path = DEFAULT_SUITE, *, case: str | None = None,
                 seed: int = 0, workers: int = 2, repo_root: Path = ROOT) -> dict[str, Any]:
    """Compile real production requests and return a JSON-only portable manifest."""
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from tools.run_film_synthesis_benchmarks import compile_preflight

    selected_cases = [case] if case is not None else list(DEFAULT_PHASE_CASES)
    compiled = compile_preflight(suite_path, case_id=selected_cases, seed=seed, workers=workers,
                                 repo_root=repo_root)
    suite_cases = {item["id"]: item for item in compiled["suite"]["cases"]}
    records = []
    for run in compiled["runs"]:
        spec = run["job_spec"]
        payload = spec["parameters"]["payload"]
        portable_request = payload["request"]
        # Export only the frozen production request. No scheduler/job wrapper state.
        request = portable_request["request"]
        if request.get("frozen_material_snapshot") is None:
            raise ValueError(f"{run['case']}: production preflight did not freeze optical inputs")
        records.append({
            "case": run["case"], "category": run.get("category"),
            "seed": run["seed"], "workers": run["workers"],
            "wall_seconds": run["wall_seconds"],
            "request_id": portable_request["request_id"],
            "request": _strict_json(request),
            "request_sha256": run["request_sha256"],
            "qualification": _strict_json(suite_cases[run["case"]].get("qualification")),
        })
    if not records:
        raise ValueError("No frozen synthesis requests were produced")
    source_paths = {f"{module.replace('.', '/')}.py" for module in import_closure(repo_root)}
    source_paths.update({
        "tools/run_film_synthesis_benchmarks.py",
        "tools/run_film_synthesis_acceptance.py",
        "coat_sim/ui/film_synthesis_quick_adapter.py",
    })
    source_hashes = {path: compiled["source_hashes"][path]
                     for path in sorted(source_paths) if path in compiled["source_hashes"]}
    source_identity = {"source_hashes": source_hashes,
                       "source_sha256": _sha(json.dumps(source_hashes, sort_keys=True,
                           separators=(",", ":")).encode())}
    dependency_versions: dict[str, str | None] = {}
    for name in DEPENDENCIES:
        try:
            dependency_versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            dependency_versions[name] = None
    return {
        "schema_version": 1, "kind": "film_synthesis_pure_cpu_bundle",
        "suite_id": compiled["suite_id"], "suite_sha256": compiled["suite_sha256"],
        "source_suite_case_count": len(compiled["suite"]["cases"]),
        "suite_case_ids": [entry["case"] for entry in records],
        "suite_request_count": len(records),
        "bundle_scope": "single_explicit_case" if case is not None else "default_eight_representative_cases",
        "catalog_sha256_at_preflight": compiled["catalog_sha256"],
        "compiled_utc": compiled["compiled_utc"],
        "defaults": {"math_threads": 1, "wall_seconds": 120,
                     "workers": workers, "seed": seed},
        "dependencies": DEPENDENCIES,
        "source_identity": source_identity,
        "entries": records,
        "dependency_versions_at_export": dependency_versions,
        "resource_policy": {"allowed_project_resources": sorted(ALLOWED_RESOURCE_PATHS),
                            "included_resources": []},
        "freeze_provenance": "GUI request builder and production preflight froze optics locally; cloud execution bypasses GUI and runs the same serialized core request.",
        "v23_matching_public_control": _control_identity(repo_root),
        "smoke_bundle": case is not None,
        "execution_contract": {
            "freeze_provenance": "GUI request builder and production preflight froze optics locally; cloud execution bypasses GUI and runs the same serialized core request.",
            "entrypoint": "coat_sim.core.film_auto_synthesis.synthesize_film_design",
            "request_decoder": "coat_sim.core.film_synthesis_io.film_synthesis_request_from_payload",
            "mode_values": ["clustered", "v23_matching"],
            "timeout_source": "frozen request manifest; CLI may reduce the wall budget",
            "acceptance_note": "raw training and dense/offset/extrema audit remain independent; means are descriptive only",
            "install_command": "python -m pip install -r requirements-cloud.txt",
        },
    }


def _bundle_files(repo_root: Path) -> dict[str, bytes]:
    modules = import_closure(repo_root)
    files = {f"{module.replace('.', '/')}.py": _module_path(module, repo_root).read_bytes()
             for module in modules if _module_path(module, repo_root).is_file()}
    # Both package initializers are deliberately minimal so importing the pure
    # core package cannot initialize desktop services or load plugins.
    files["coat_sim/__init__.py"] = b'"""Pure film synthesis cloud bundle."""\n'
    files["coat_sim/core/__init__.py"] = b'"""Pure algorithm core."""\n'
    files["run_film_synthesis_cloud.py"] = (repo_root / "tools" / "run_film_synthesis_cloud.py").read_bytes()
    files["requirements-cloud.txt"] = (
        "numpy>=1.24\nscipy>=1.16,<2\npydantic>=2.12,<3\n"
    ).encode("utf-8")
    for rel in ALLOWED_RESOURCE_PATHS:
        source = repo_root / Path(*PurePosixPath(rel).parts)
        if source.exists():
            # Frozen requests already carry optical constants; copying is only
            # allowed when a bundled source explicitly needs one of these files.
            if any(rel in data.decode("utf-8", errors="ignore") for data in files.values()):
                files[rel] = source.read_bytes()
    return files


def export_bundle(output: Path, *, suite_path: Path = DEFAULT_SUITE, case: str | None = None,
                  seed: int = 0, workers: int = 2, repo_root: Path = ROOT) -> Path:
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}")
    manifest = build_bundle(suite_path, case=case, seed=seed, workers=workers, repo_root=repo_root)
    source_files = _bundle_files(repo_root)
    source_hashes = {name: _sha(content) for name, content in sorted(source_files.items())}
    manifest["bundle_source_sha256"] = _sha(json.dumps(source_hashes, sort_keys=True,
                                                       separators=(",", ":")).encode())
    manifest["bundle_source_files"] = source_hashes
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.name + ".partial")
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False))
            for name, content in sorted(source_files.items()):
                archive.writestr(name, content)
        temp.replace(output)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return output


def export_git_requests(output: Path, *, suite_path: Path = DEFAULT_SUITE,
                        repo_root: Path = ROOT) -> Path:
    """Freeze the eight public production requests as a normal source JSON file.

    No Library file is read. The final Git SHA is selected at runtime, avoiding
    a circular commit hash inside the committed request manifest itself.
    """
    repo_root, output = repo_root.resolve(), output.resolve()
    if not output.is_relative_to(repo_root):
        raise ValueError("Git frozen requests must be inside the source repository")
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}")
    manifest = build_bundle(suite_path, seed=0, workers=2, repo_root=repo_root)
    manifest["kind"] = "film_synthesis_git_requests"
    manifest["source_identity"]["identity_note"] = (
        "Local freeze provenance only; runtime source identity is the explicitly selected Git commit.")
    for entry in manifest["entries"]:
        qualification = entry.get("qualification")
        if isinstance(qualification, dict) and isinstance(qualification.get("evidence"), str):
            for private_name in ("reference-qualification-v1.1.json", "de-production-case-v2.json", "de-cancellation-case-v2.json"):
                qualification["evidence"] = qualification["evidence"].replace(
                    private_name, private_name.replace(".json", "-public.json"))
    manifest.pop("resource_policy", None)
    manifest.pop("smoke_bundle", None)
    manifest["git_identity_contract"] = (
        "Exact --expect-commit must equal HEAD; declared UTF-8 sources and this manifest "
        "must match committed blobs after CRLF-to-LF normalization. Unrelated dirty files are allowed.")
    paths = {path.relative_to(repo_root).as_posix()
             for path in (repo_root / "coat_sim/core").rglob("*.py")}
    paths.update(f"{module.replace('.', '/')}.py" for module in import_closure(repo_root)
                 if _module_path(module, repo_root).is_file())
    paths.update({"coat_sim/__init__.py", "coat_sim/core/__init__.py",
        "coat_sim/ui/film_synthesis_quick_adapter.py", "tools/film_synthesis_git_source.py",
        "tools/export_film_synthesis_cloud.py", "tools/run_film_synthesis_cloud.py",
        "tools/run_film_synthesis_benchmarks.py", "tools/run_film_synthesis_acceptance.py",
        "data/optical_catalog.json", "requirements.txt", suite_path.resolve().relative_to(repo_root).as_posix(),
        "benchmarks/film_synthesis/20261021/requirements-cpu.txt", output.relative_to(repo_root).as_posix()})
    manifest["git_source_paths"] = sorted(paths)
    manifest["execution_contract"]["install_command"] = (
        "python -m pip install -r benchmarks/film_synthesis/20261021/requirements-cpu.txt")
    manifest["execution_contract"]["source_mode"] = "git_checkout_exact_commit"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--git-requests", action="store_true",
                        help="Write eight fresh public frozen requests for Git-source mode")
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--case", help="Export one case; omit to export all 8 public cases")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True, help="New .zip output path")
    args = parser.parse_args(argv)
    if args.git_requests:
        if args.case is not None or args.seed != 0 or args.workers != 2:
            parser.error("Git confirmation requests freeze eight cases, seed=0 and workers=2")
        print(export_git_requests(args.output, suite_path=args.suite))
    else:
        print(export_bundle(args.output, suite_path=args.suite, case=args.case,
                            seed=args.seed, workers=args.workers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
