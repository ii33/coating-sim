"""Read-only, bounded distribution graph and source-resource ZIP validation.

This is an audit helper, not an installer or a dead-code detector. It parses
source without importing application modules or executing the fork scripts.
The optional package check calls only the application source/static
data staging helpers; it does not run PyInstaller, install, or launch the app.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from datetime import datetime, timezone
from html.parser import HTMLParser
import hashlib
import importlib.util
from importlib import metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
import zipfile
from urllib.parse import unquote, urlsplit


FORK_SCRIPTS = (
    "tools/extract_ai.py", "tools/finalize_ai_extraction.py",
    "tools/fix_ai_paths.py", "tools/fix_ai_integration.py",
    "tools/fix_ai_verification.py", "tools/write_ai_docs.py",
)
FORK_SCRIPT_DETAILS = {
    "tools/extract_ai.py": {"purpose": "Copy and transform coat_sim source/assets to create other/ai",
                            "inputs": ["coat_sim"], "outputs": ["other/ai"],
                            "main_guard": True, "top_level_mutation_on_import": False},
    "tools/finalize_ai_extraction.py": {"purpose": "Finalize existing independent other/ai application and resources",
                                       "inputs": ["other/ai"], "outputs": ["other/ai"],
                                       "main_guard": False, "top_level_mutation_on_import": True},
    "tools/fix_ai_paths.py": {"purpose": "Patch other/ai service discovery, state paths and fork tests",
                              "inputs": ["other/ai"], "outputs": ["other/ai"],
                              "main_guard": False, "top_level_mutation_on_import": True},
    "tools/fix_ai_integration.py": {"purpose": "Patch other/ai runtime, control, MCP and smoke integration",
                                    "inputs": ["other/ai"], "outputs": ["other/ai"],
                                    "main_guard": False, "top_level_mutation_on_import": True},
    "tools/fix_ai_verification.py": {"purpose": "Patch other/ai provider headers, MCP wrappers and fork tests",
                                     "inputs": ["other/ai"], "outputs": ["other/ai"],
                                     "main_guard": False, "top_level_mutation_on_import": True},
    "tools/write_ai_docs.py": {"purpose": "Write other/ai documentation and modify main-project documentation",
                              "inputs": ["docs/algorithms.html", "docs/workflows.md", "README.md", "other/ai/agent.md"],
                              "outputs": ["other/ai", "docs/algorithms.html", "README.md"],
                              "main_guard": False, "top_level_mutation_on_import": True},
}
MISSING_BUILD_SURFACES = (
    "tools/build_portable_bundle.py", "tools/portable_bootstrap.py",
    "tools/portable_preflight.py", "tools/portable_delta.py",
    "scripts/build_remote_service_distribution.py",
)
RESOURCE_ROOTS = (
    "coat_sim/ui/qml", "coat_sim/ui/assets", "coat_sim/agent/resources",
    "data/optical_catalog_sources", "data/film_design/cie-illuminants-20261006",
)
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", "tmp", "cache"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".log", ".sqlite", ".sqlite3", ".db", ".lock"}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_files(root: Path, folder: str, suffixes: set[str] | None) -> list[Path]:
    base = root / folder
    if not base.is_dir():
        return []
    result = []
    for parent, dirs, names in os.walk(base, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_PARTS and not d.startswith(".tmp"))
        for name in sorted(names):
            path = Path(parent) / name
            if (suffixes is None or path.suffix in suffixes) and not path.is_symlink():
                result.append(path)
    return sorted(result)


def module_name(path: str) -> str:
    stem = str(PurePosixPath(path).with_suffix("")).replace("/", ".")
    return stem.removesuffix(".__init__")


def relative_module(source: str, imported: str | None, level: int) -> str:
    if not level:
        return imported or ""
    package = module_name(source)
    if not source.endswith("/__init__.py"):
        package = package.rsplit(".", 1)[0] if "." in package else ""
    pieces = package.split(".") if package else []
    if level > len(pieces):
        return "<unresolved-relative>"
    prefix = pieces[: len(pieces) - level + 1]
    return ".".join(prefix + ([imported] if imported else []))


def declared_dependencies(root: Path) -> list[dict[str, str]]:
    rows = []
    for filename in ("requirements.txt", "requirements-test.txt", "requirements-sandbox.txt",
                     "requirements-remote-service.txt"):
        path = root / filename
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            value = line.split("#", 1)[0].strip()
            match = re.match(r"([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?", value)
            if match:
                rows.append({"distribution": match.group(1), "declaration": value,
                             "source": filename, "line": number})
    environment = root / "environment.yml"
    if environment.is_file():
        for number, line in enumerate(environment.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip() == "- torch-directml":
                rows.append({"distribution": "torch-directml", "declaration": "torch-directml",
                             "source": "environment.yml", "line": number})
    return rows


def fork_reference_search(root: Path) -> dict:
    """Content search is a second layer, not proof about external workflows."""
    suffixes = {".py", ".pyw", ".js", ".mjs", ".sh", ".bat", ".cmd", ".md",
                ".rst", ".html", ".txt", ".json", ".yaml", ".yml", ".toml", ".cs"}
    paths = []
    for folder in ("coat_sim", "tests", "tools", "scripts", "docs"):
        paths += source_files(root, folder, suffixes)
    paths += [root / "README.md"] if (root / "README.md").is_file() else []
    terms = [PurePosixPath(p).stem for p in FORK_SCRIPTS] + ["other/ai", "harness_app"]
    pattern = re.compile("|".join(re.escape(term) for term in terms), re.IGNORECASE)
    hits, failures, searched = [], [], []
    for path in sorted(set(paths)):
        relative = path.relative_to(root).as_posix()
        if relative.startswith(("docs/audits/", "docs/remediation/")):
            continue
        try:
            value = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            failures.append({"path": relative, "error": str(exc)})
            continue
        searched.append({"path": relative, "sha256": digest(path)})
        for number, line in enumerate(value.splitlines(), 1):
            if pattern.search(line):
                classification = ("candidate_script_self_reference" if relative in FORK_SCRIPTS
                                  else "audit_inventory_only" if relative in {
                                      "tools/audit_distribution_graph.py", "tests/test_distribution_graph_audit.py"}
                                  else "requires_review")
                hits.append({"source": relative, "line": number, "classification": classification,
                             "matched_terms": [term for term in terms if term.lower() in line.lower()]})
    return {"terms": terms, "searched_files": searched, "hits": hits, "read_failures": failures,
            "external_manual_use_excluded": False,
            "production_or_build_reference_hits": [h for h in hits if h["classification"] == "requires_review"]}


def collect_graph(root: Path) -> dict:
    root = root.resolve()
    python_files = source_files(root, "coat_sim", {".py"})
    python_files += source_files(root, "tools", {".py", ".pyw"})
    python_files += source_files(root, "scripts", {".py", ".pyw"})
    python_files += [root / p for p in ("launcher.py", "launcher.pyw") if (root / p).is_file()]
    resources = {}
    for folder in RESOURCE_ROOTS:
        for path in source_files(root, folder, None):
            if path.suffix in SKIP_SUFFIXES:
                continue
            relative = path.relative_to(root).as_posix()
            resources[relative] = {"path": relative, "bytes": path.stat().st_size, "sha256": digest(path)}
    modules = {module_name(p.relative_to(root).as_posix()): p.relative_to(root).as_posix()
               for p in python_files}
    lookup = dict(modules)
    # Tools deliberately import siblings as top-level modules. Tests are
    # lookup-only here; their source is not added to runtime reachability.
    for path in python_files:
        if path.parent == root / "tools":
            lookup.setdefault(path.stem, path.relative_to(root).as_posix())
    for path in source_files(root, "tests", {".py"}):
        lookup.setdefault(module_name(path.relative_to(root).as_posix()), path.relative_to(root).as_posix())
        if path.parent == root / "tests":
            lookup.setdefault(path.stem, path.relative_to(root).as_posix())
    basenames = defaultdict(list)
    for path in resources:
        basenames[PurePosixPath(path).name].append(path)
    edges, dynamic_calls, failures, unresolved_internal, registered, qml_types = [], [], [], [], [], []
    external = defaultdict(list)
    for path in python_files:
        source = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=source)
        except (OSError, UnicodeError, SyntaxError) as exc:
            failures.append({"path": source, "error": str(exc)})
            continue
        parents = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                owner = node.targets[0].id
                if owner in {"_ROLE_MODULES", "QUICK_FILM_MODULES"}:
                    try:
                        value = ast.literal_eval(node.value)
                    except (ValueError, TypeError, SyntaxError):
                        registered.append({"source": source, "line": node.lineno, "registry": owner,
                                           "status": "UNKNOWN: non-literal registry requires runtime evidence"})
                        continue
                    for key, entry in value.items():
                        module = entry if owner == "_ROLE_MODULES" else entry[0]
                        registered.append({"source": source, "line": node.lineno, "registry": owner,
                                           "key": key, "python_module": module,
                                           "resolved_source": modules.get(module),
                                           "class": None if owner == "_ROLE_MODULES" else entry[1],
                                           "qml": None if owner == "_ROLE_MODULES" else entry[2]})
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "ModuleSpec":
                values = [arg.value for arg in node.args if isinstance(arg, ast.Constant)]
                if len(values) == 6:
                    registered.append({"source": source, "line": node.lineno, "registry": "ModuleSpec",
                                       "key": values[0], "python_module": values[3],
                                       "resolved_source": modules.get(values[3]), "class": values[4], "qml": values[5]})
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                optional = False
                cursor = node
                while cursor in parents:
                    cursor = parents[cursor]
                    if isinstance(cursor, ast.Try):
                        optional = True
                names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                         else [relative_module(source, node.module, node.level)])
                for name in names:
                    target = lookup.get(name)
                    if target:
                        edges.append({"source": source, "target": target, "kind": "python_import",
                                      "line": node.lineno, "inside_try": optional})
                    elif name.startswith("coat_sim."):
                        unresolved_internal.append({"source": source, "module": name, "line": node.lineno})
                    elif name and name.split(".")[0] not in sys.stdlib_module_names | {"__future__"}:
                        external[name.split(".")[0]].append({"source": source, "module": name,
                                                            "line": node.lineno, "inside_try": optional})
                if isinstance(node, ast.ImportFrom):
                    base = relative_module(source, node.module, node.level)
                    for alias in node.names:
                        target = lookup.get(base + "." + alias.name)
                        if target:
                            edges.append({"source": source, "target": target,
                                          "kind": "python_from_submodule", "line": node.lineno})
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value
                if value in modules:
                    edges.append({"source": source, "target": modules[value],
                                  "kind": "module_literal_candidate_not_execution", "line": node.lineno})
                if value in resources:
                    edges.append({"source": source, "target": value,
                                  "kind": "resource_exact_literal", "line": node.lineno})
                elif value in basenames:
                    for target in basenames[value]:
                        edges.append({"source": source, "target": target,
                                      "kind": "resource_basename_candidate_not_proof",
                                      "line": node.lineno})
            if isinstance(node, ast.Call):
                function = ast.unparse(node.func)
                if function.endswith("qmlRegisterType") and len(node.args) >= 5:
                    values = [arg.value if isinstance(arg, ast.Constant) else ast.unparse(arg) for arg in node.args]
                    qml_types.append({"source": source, "line": node.lineno, "python_type": values[0],
                                      "uri": values[1], "major": values[2], "minor": values[3], "qml_type": values[4]})
                if function.endswith(("import_module", "__import__", "run_module", "run_path", "module_command")):
                    dynamic_calls.append({"source": source, "line": node.lineno, "function": function,
                                          "argument": ast.unparse(node.args[0]) if node.args else None,
                                          "expression_requires_runtime_resolution": bool(node.args and not isinstance(node.args[0], ast.Constant))})
    qml_plugins = defaultdict(list)
    unresolved_local = []
    for source in sorted(p for p in resources if p.endswith((".qml", ".js"))):
        path = root / source
        text = path.read_text(encoding="utf-8-sig")
        local_dirs = {path.parent}
        aliases = {}
        for number, line in enumerate(text.splitlines(), 1):
            quoted = re.match(r'\s*import\s+"([^"]+)"(?:\s+as\s+(\w+))?', line)
            plugin = re.match(r"\s*import\s+([\w.]+)(?:\s+([\d.]+))?", line)
            if quoted:
                candidate = (path.parent / quoted.group(1)).resolve()
                if not candidate.is_relative_to(root):
                    unresolved_local.append({"source": source, "line": number, "value": quoted.group(1), "reason": "outside_scope"})
                elif candidate.is_dir():
                    local_dirs.add(candidate)
                    if quoted.group(2):
                        aliases[quoted.group(2)] = candidate
                    for item in sorted(candidate.iterdir()):
                        if item.is_file() and item.suffix in {".qml", ".js"}:
                            edges.append({"source": source, "target": item.relative_to(root).as_posix(),
                                          "kind": "qml_directory_import_visibility_not_instantiation", "line": number})
                elif candidate.is_file():
                    edges.append({"source": source, "target": candidate.relative_to(root).as_posix(),
                                  "kind": "qml_file_import", "line": number})
                else:
                    unresolved_local.append({"source": source, "line": number, "value": quoted.group(1), "reason": "missing"})
            elif plugin:
                qml_plugins[plugin.group(1)].append({"source": source, "line": number,
                                                   "version": plugin.group(2) or "unversioned"})
            for value in re.findall(r'["\']([^"\'\n]+\.(?:qml|js|png|ico|wav|otf|svg))["\']', line):
                candidate = (path.parent / value).resolve()
                if candidate.is_relative_to(root) and candidate.is_file():
                    target = candidate.relative_to(root).as_posix()
                    if target in resources:
                        edges.append({"source": source, "target": target, "kind": "qml_resource_literal", "line": number})
                elif not re.match(r"\w+:|^/", value):
                    unresolved_local.append({"source": source, "line": number, "value": value, "reason": "missing_literal_or_dynamic_prefix"})
            for alias, component in re.findall(r"\b(?:(\w+)\.)?([A-Z]\w*)\s*\{", line):
                directories = {aliases[alias]} if alias in aliases else local_dirs if not alias else set()
                for directory in directories:
                    candidate = directory / (component + ".qml")
                    if candidate.is_file():
                        edges.append({"source": source, "target": candidate.relative_to(root).as_posix(),
                                      "kind": "qml_component_instantiation", "line": number})
    documentation = []
    docs = root / "docs"
    if docs.is_dir():
        documentation += [p for p in docs.iterdir() if p.is_file() and p.suffix in {".html", ".md"}]
    documentation += [root / "README.md"] if (root / "README.md").is_file() else []
    for path in documentation:
        source = path.relative_to(root).as_posix()
        for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            refs = re.findall(r'(?:href|src)\s*=\s*["\']([^"\']+)["\']', line)
            refs += re.findall(r"\]\(([^)\s]+)(?:\s+[^)]*)?\)", line)
            for ref in refs:
                url = urlsplit(ref)
                if url.scheme or url.netloc or not url.path:
                    continue
                target = (path.parent / unquote(url.path)).resolve()
                if target.is_relative_to(root) and target.is_file():
                    name = target.relative_to(root).as_posix()
                    if name in resources:
                        edges.append({"source": source, "target": name,
                                      "kind": "documentation_resource_link", "line": number})
    incoming = Counter(edge["target"] for edge in edges)
    for resource in resources.values():
        resource["static_incoming_edges"] = incoming[resource["path"]]
        resource["cleanup_decision"] = "retain; static absence does not establish non-use"
    aliases = {"PIL": ["Pillow"], "PySide6": ["PySide6-Essentials", "PySide6-Addons"],
               "torch_directml": ["torch-directml"], "pydantic_ai": ["pydantic-ai-slim"],
               "pydantic_ai_harness": ["pydantic-ai-harness"], "fastmcp": ["fastmcp-slim"]}
    declared = declared_dependencies(root)
    normalized = {r["distribution"].lower().replace("_", "-") for r in declared}
    installed_mapping = metadata.packages_distributions()
    declared_metadata = []
    for name in sorted({r["distribution"] for r in declared}):
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            declared_metadata.append({"distribution": name, "installed": False})
        else:
            declared_metadata.append({"distribution": name, "installed": True,
                                      "version": distribution.version,
                                      "requires_metadata_unresolved_markers": distribution.requires or []})
    dependencies = []
    for name, references in sorted(external.items()):
        distributions = installed_mapping.get(name) or aliases.get(name, [name.replace("_", "-")])
        parents = []
        for parent in declared_metadata:
            for requirement in parent.get("requires_metadata_unresolved_markers", []):
                dependency = re.match(r"([A-Za-z0-9_.-]+)", requirement)
                if dependency and dependency.group(1).lower().replace("_", "-") in {d.lower().replace("_", "-") for d in distributions}:
                    parents.append({"distribution": parent["distribution"], "requires_metadata": requirement})
        dependencies.append({"import_root": name, "distribution_candidates": distributions,
                             "explicitly_declared": any(d.lower().replace("_", "-") in normalized for d in distributions),
                             "references": references,
                             "installed_mapping_observed": installed_mapping.get(name, []),
                             "declared_parent_metadata_edges_markers_not_evaluated": parents,
                             "cleanup_decision": "retain or investigate; no dependency removal authorized"})
    fork_inventory = []
    for source in FORK_SCRIPTS:
        path = root / source
        fork_inventory.append({"path": source, "present": path.is_file(),
                               "sha256": digest(path) if path.is_file() else None,
                               **FORK_SCRIPT_DETAILS[source],
                               "ownership": "external fork owner and out-of-repository workflows UNKNOWN",
                               "executed": False, "decision": "retain in place pending owner retirement confirmation"})
    return {
        "schema": "coat-sim-distribution-audit-v1", "captured_utc": datetime.now(timezone.utc).isoformat(),
        "scope": {"python": ["coat_sim/**/*.py", "tools/**/*.py(w)", "scripts/**/*.py(w)", "launcher.py(w)"],
                  "resources": list(RESOURCE_ROOTS), "documentation": ["docs/*.{html,md}", "README.md"], "state_read": False,
                  "excludes": ["virtualenv", "caches", "databases", "logs", "user project assets", "external fork contents", "external/manual workflows"]},
        "source_files": [{"path": p.relative_to(root).as_posix(), "sha256": digest(p)} for p in python_files],
        "resources": list(resources.values()), "edges": edges, "dynamic_calls": dynamic_calls,
        "registered_entrypoints": registered, "unresolved_internal_imports": unresolved_internal,
        "dependency_declarations": declared, "declared_installed_metadata": declared_metadata,
        "external_imports": dependencies,
        "qml_type_registrations": qml_types,
        "qml_plugin_imports": [{"module": name, "references": refs,
                                "python_registration_evidence": [r for r in qml_types if r["uri"] == name],
                                "packaging_evidence": "existing installer includes installed PySide6 QML tree" if name.startswith("Qt") else "Python registration; runtime verification not performed"}
                               for name, refs in sorted(qml_plugins.items())],
        "unresolved_local_literals": unresolved_local, "parse_failures": failures,
        "fork_scripts": fork_inventory,
        "fork_content_reference_search": fork_reference_search(root),
        "build_surface_coverage_gaps": [{"path": p, "present": (root / p).is_file(),
                                         "status": "visible" if (root / p).is_file() else "absent in inspected checkout; repository history and external build assets not inspected"}
                                        for p in MISSING_BUILD_SURFACES],
        "limitations": ["AST and QML literal graph is not an exhaustive runtime reachability proof.",
                        "Directory visibility and basename candidates are labeled separately from execution.",
                        "Qt plugins, conditional imports, format/fallback promises, and external manual entrypoints still require owner/runtime evidence.",
                        "No file or dependency is classified safe to delete."],
    }


def validated_member_name(name: str) -> PurePosixPath:
    """Require one unambiguous, relative file name for the audit ZIP layout.

    The staged package targets Windows as well as the Linux audit host; a colon
    in any component is therefore rejected (drive/alternate data stream syntax).
    Also reject Windows illegal characters, reserved device names and trailing
    dots/spaces instead of letting an extractor silently alias file names.
    This is an audit-archive policy, not a guarantee about other extractors.
    """
    relative = PurePosixPath(name)
    if (not name or relative.is_absolute() or not relative.parts
            or relative.as_posix() != name or ".." in relative.parts
            or "\\" in name or any(character in '<>:"|?*' or ord(character) < 32 for character in name)
            or any(part.endswith((".", " ")) for part in relative.parts)
            or any(part.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL",
                   *("COM" + str(i) for i in range(1, 10)), *("LPT" + str(i) for i in range(1, 10))}
                   for part in relative.parts)):
        raise ValueError("Unsafe or non-canonical ZIP member: " + repr(name))
    return relative


def reject_linked_extraction(extraction: Path) -> None:
    # Check parents without resolving/following a link. Tests and production
    # audits are isolated from concurrent writers; this is not a race-proof OS
    # sandbox or a safe extractor for adversarially modified shared directories.
    for parent in (extraction, *extraction.parents):
        if parent.is_symlink():
            raise ValueError("Symbolic link in extraction path: " + str(parent))
    if extraction.exists():
        if not extraction.is_dir():
            raise ValueError("Extraction target is not a directory")
        for path in extraction.rglob("*"):
            if path.is_symlink():
                raise ValueError("Symbolic link in existing extraction: " + str(path))


def validate_archive(archive: Path, manifest: list[dict], extraction: Path) -> dict:
    expected = {item["path"]: item for item in manifest}
    if len(expected) != len(manifest):
        raise ValueError("Duplicate manifest member")
    for name in expected:
        validated_member_name(name)
    # ZIP/file names are case-insensitive on the intended Windows layout.
    if len({name.casefold() for name in expected}) != len(expected):
        raise ValueError("Case-colliding archive members")
    folded = {name.casefold() for name in expected}
    for name in expected:
        relative = PurePosixPath(name)
        if any(parent.as_posix().casefold() in folded for parent in relative.parents
               if parent.as_posix() != "."):
            raise ValueError("File/directory archive member collision")
    reject_linked_extraction(extraction)
    with zipfile.ZipFile(archive) as stream:
        members = stream.infolist()
        names = [item.filename for item in members]
        if len(set(names)) != len(names):
            raise ValueError("Duplicate ZIP member")
        if set(names) != set(expected):
            raise ValueError("ZIP inventory differs from manifest")
        for member in members:
            validated_member_name(member.filename)
            if (member.is_dir()
                    or (member.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError("Non-file or linked ZIP member")
            data = stream.read(member)
            record = expected[member.filename]
            if len(data) != record["size_bytes"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise ValueError("ZIP member hash/length mismatch: " + member.filename)
        if extraction.exists():
            actual_names = {p.relative_to(extraction).as_posix() for p in extraction.rglob("*") if p.is_file()}
            if actual_names != set(expected):
                raise ValueError("Existing extraction inventory differs from manifest")
        else:
            extraction.mkdir(parents=True, exist_ok=False)
            stream.extractall(extraction)
    for name, record in expected.items():
        target = extraction.joinpath(*PurePosixPath(name).parts)
        if target.stat().st_size != record["size_bytes"] or digest(target) != record["sha256"]:
            raise ValueError("Extracted file hash/length mismatch: " + name)
    return {"member_count": len(expected), "archive_bytes": archive.stat().st_size,
            "archive_sha256": digest(archive), "member_bytes_and_extracted_bytes_verified": True,
            "canonical_file_names_verified": True, "extraction_symlinks_rejected": True,
            "safety_scope": "This audit helper only; no concurrent-writer or other-extractor guarantee."}


class DocumentationParser(HTMLParser):
    """Collect actual HTML attributes without interpreting reference text."""
    def __init__(self) -> None:
        super().__init__()
        self.references: list[dict] = []
        self.anchors: set[str] = set()
        self.remote_render_dependencies: list[dict] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        for key in ("id", "name"):
            if values.get(key):
                self.anchors.add(values[key])
        for attribute in ("href", "src"):
            value = values.get(attribute)
            if not value:
                continue
            record = {"line": self.getpos()[0], "value": value,
                      "tag": tag, "attribute": attribute}
            self.references.append(record)
            url = urlsplit(value)
            if (url.scheme in {"http", "https"} or url.netloc) and (
                    attribute == "src" or tag == "link"):
                self.remote_render_dependencies.append(record)


DOCUMENTATION_FILES = (
    "README.md", "docs/algorithms.html", "docs/software_manual.html",
    "docs/portable-bundle.md", "docs/portable-assets.md", "docs/installation.md",
    "coat_sim/agent/resources/software_manual.md",
)
REQUIRED_DOCUMENT_LINKS = (
    ("docs/algorithms.html", "data/film_design/cie-illuminants-20261006/NOTICE_DATA.md"),
    ("docs/algorithms.html", "data/film_design/cie-illuminants-20261006/LICENSE_DATA_CC_BY_SA_4_0.txt"),
    ("README.md", "docs/portable-bundle.md"),
    ("README.md", "docs/portable-assets.md"),
    ("README.md", "docs/installation.md"),
    ("docs/software_manual.html", "docs/portable-bundle.md"),
)


def documentation_validation(layout: Path, manifest: list[dict], source_root: Path) -> dict:
    """Check selected guide/license links against real extracted ZIP bytes.

    Missing other source/research references are reported, not silently treated
    as required runtime assets. No external reference URL is requested.
    """
    layout = layout.resolve()
    records = {record["path"]: record for record in manifest}
    parsed, texts, documents, missing_documents, links = {}, {}, [], [], []
    for source in DOCUMENTATION_FILES:
        path = layout / source
        if source not in records or not path.is_file():
            missing_documents.append(source)
            continue
        text = path.read_text(encoding="utf-8-sig")
        texts[source] = text
        record = records[source]
        original = source_root / source
        documents.append({"path": source, "sha256": digest(path),
                          "matches_manifest": digest(path) == record["sha256"] and path.stat().st_size == record["size_bytes"],
                          "matches_source": original.is_file() and digest(original) == digest(path)})
        if source.endswith(".html"):
            parser = DocumentationParser()
            parser.feed(text)
            parsed[source] = parser
        else:
            parsed[source] = None
    remote_render = []
    for source, parser in parsed.items():
        if parser is not None:
            references = parser.references
            remote_render.extend({"source": source, **r} for r in parser.remote_render_dependencies)
            for match in re.finditer(r'(?:url\(|@import\s+)[\s\"\']*(https?://[^\s\)\"\']+)', texts[source]):
                remote_render.append({"source": source, "line": texts[source].count("\n", 0, match.start()) + 1,
                                      "value": match.group(1), "tag": "css", "attribute": "url_or_import"})
        else:
            references = [{"line": number, "value": value, "tag": "markdown", "attribute": "link"}
                          for number, line in enumerate(texts[source].splitlines(), 1)
                          for value in re.findall(r"\]\(([^)\s]+)(?:\s+[^)]*)?\)", line)]
        for reference in references:
            url = urlsplit(reference["value"])
            if url.scheme or url.netloc:
                continue
            target = (layout / source).parent / unquote(url.path) if url.path else layout / source
            target = target.resolve()
            row = {"source": source, **reference, "fragment": unquote(url.fragment)}
            if not target.is_relative_to(layout):
                row.update(target=None, status="outside_layout")
            else:
                target_name = target.relative_to(layout).as_posix()
                row["target"] = target_name
                original_target = source_root / target_name
                row["target_present_in_source_checkout"] = original_target.is_file()
                record = records.get(target_name)
                if record is None:
                    row["status"] = "not_in_manifest"
                elif not target.is_file():
                    row["status"] = "missing_extracted_target"
                elif digest(target) != record["sha256"] or target.stat().st_size != record["size_bytes"]:
                    row["status"] = "extracted_target_manifest_mismatch"
                elif url.fragment and target_name.endswith(".html"):
                    if target_name not in parsed or parsed[target_name] is None:
                        parser = DocumentationParser()
                        parser.feed(target.read_text(encoding="utf-8-sig"))
                    else:
                        parser = parsed[target_name]
                    row["status"] = "verified" if unquote(url.fragment) in parser.anchors else "missing_html_anchor"
                else:
                    row["status"] = "verified"
            links.append(row)
    required = []
    for source, target in REQUIRED_DOCUMENT_LINKS:
        observed = [row for row in links if row["source"] == source and row["target"] == target]
        required.append({"source": source, "target": target, "observed_references": observed,
                         "verified": bool(observed) and all(row["status"] == "verified" for row in observed)})
    assistant = "coat_sim/agent/resources/software_manual.md"
    guide = "docs/portable-bundle.md"
    assistant_mention = {"source": assistant, "target": guide, "kind": "plain_text_app_root_path_mention",
                         "verified": guide in texts.get(assistant, "") and guide in records and (layout / guide).is_file()}
    licenses = []
    for name, record in sorted(records.items()):
        basename = PurePosixPath(name).name.lower()
        if any(term in basename for term in ("license", "licence", "notice", "copying")):
            target = layout / name
            licenses.append({"path": name, "size_bytes": record["size_bytes"], "sha256": record["sha256"],
                             "extracted_bytes_match_manifest": target.is_file() and digest(target) == record["sha256"]})
    passed = (not missing_documents and all(row["matches_manifest"] and row["matches_source"] for row in documents)
              and all(row["verified"] for row in required) and assistant_mention["verified"] and not remote_render
              and all(row["extracted_bytes_match_manifest"] for row in licenses))
    return {"kind": "selected-offline-guide-license-layout-validation-only", "passed": passed,
            "documents": documents, "missing_documents": missing_documents,
            "required_document_links": required, "assistant_guide_mention": assistant_mention,
            "local_references": links, "local_reference_count": len(links),
            "unresolved_local_references": [row for row in links if row["status"] != "verified"],
            "all_local_references_closed": all(row["status"] == "verified" for row in links),
            "source_checkout_missing_local_targets": [row for row in links if row.get("target_present_in_source_checkout") is False],
            "remote_html_render_dependencies": remote_render, "license_notice_inventory": licenses,
            "external_urls_requested": False, "markdown_fragment_anchors_checked": False,
            "runtime_third_party_license_completeness_verified": False,
            "limits": ["Only the selected staging layout is validated, not a runnable Windows release.",
                       "Other source/research references missing from this resource-only archive remain reported.",
                       "Installed runtime/Qt/plugin license completeness, first launch and migration are not validated."]}


def reference_manifest_validation(layout: Path, relative_manifest: str) -> dict:
    """Compare packaged source-data bytes with their preserved provenance manifest.

    A CRLF-equivalent diagnostic is never used to turn a byte mismatch into a
    pass. This function does not rewrite data, metadata or provenance claims.
    """
    path = layout / relative_manifest
    if not path.is_file():
        return {"manifest": relative_manifest, "passed": False, "error": "missing source-data manifest", "files": []}
    records = json.loads(path.read_text(encoding="utf-8-sig"))
    files = []
    for name, expected in sorted(records.items()):
        relative = validated_member_name(name)
        target = path.parent.joinpath(*relative.parts)
        row = {"path": (PurePosixPath(relative_manifest).parent / relative).as_posix(),
               "expected_bytes": expected["bytes"], "expected_sha256": expected["sha256"],
               "exact_bytes_match": False}
        if target.is_file():
            data = target.read_bytes()
            row.update(actual_bytes=len(data), actual_sha256=hashlib.sha256(data).hexdigest())
            row["exact_bytes_match"] = len(data) == expected["bytes"] and row["actual_sha256"] == expected["sha256"]
            if not row["exact_bytes_match"] and b"\r" not in data and b"\n" in data:
                crlf = data.replace(b"\n", b"\r\n")
                row["lf_to_crlf_only_would_match_recorded_bytes"] = (
                    len(crlf) == expected["bytes"] and hashlib.sha256(crlf).hexdigest() == expected["sha256"])
        else:
            row["error"] = "missing referenced file"
        files.append(row)
    return {"manifest": relative_manifest, "manifest_sha256": digest(path),
            "file_count": len(files), "passed": bool(files) and all(row["exact_bytes_match"] for row in files),
            "files": files, "mismatches": [row for row in files if not row["exact_bytes_match"]],
            "data_or_manifest_modified": False, "newline_equivalence_is_not_byte_identity": True}


def package_validation(root: Path, output: Path) -> dict:
    """Validate staging/selection only; no runtime or release is produced."""
    script = root / "tools/distribution_resources.py"
    spec = importlib.util.spec_from_file_location("distribution_audit_staging", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load application resource staging helpers")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    builder.ROOT = root
    with tempfile.TemporaryDirectory(prefix="coat-distribution-audit-") as temporary:
        stage = Path(temporary)
        snapshot = stage / "source"
        source_records = builder.stage_source_snapshot(snapshot)
        static_paths = builder.prepare_static_data(stage / "assets", source_root=root)
        command = []
        builder.add_qt_and_source_data_arguments(command, stage / "assets/data", snapshot)
        mappings = []
        selected = {}
        for position in range(0, len(command), 2):
            if command[position] != "--add-data":
                raise RuntimeError("Unexpected staging command")
            source, destination = command[position + 1].rsplit(os.pathsep, 1)
            source_path = Path(source)
            external_qt = destination == "PySide6/Qt/qml"
            mappings.append({"source": "PySide6 installed QML imports" if external_qt else str(source_path.relative_to(stage)),
                             "destination": destination, "included_in_validation_archive": not external_qt})
            if external_qt:
                continue
            files = [source_path] if source_path.is_file() else sorted(p for p in source_path.rglob("*") if p.is_file())
            for path in files:
                name = (PurePosixPath(destination) / (path.name if source_path.is_file() else path.relative_to(source_path).as_posix())).as_posix()
                selected[name] = path
        archive = output / "current-source-resource-validation.zip"
        manifest = [{"path": name, "size_bytes": path.stat().st_size, "sha256": digest(path)}
                    for name, path in sorted(selected.items())]
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as stream:
            for name, path in sorted(selected.items()):
                stream.write(path, name)
        extraction = output / ("extracted-" + digest(archive)[:12])
        verification = validate_archive(archive, manifest, extraction)
        documentation = documentation_validation(extraction, manifest, root)
        reference_manifest = reference_manifest_validation(
            extraction, "data/film_design/cie-illuminants-20261006/MANIFEST_SHA256.json")
        (output / "cie-reference-manifest-validation.json").write_text(
            json.dumps(reference_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (output / "documentation-validation.json").write_text(
            json.dumps(documentation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        forbidden = [name for name in selected if (PurePosixPath(name).suffix in SKIP_SUFFIXES
                                                   or name in FORK_SCRIPTS)]
        if forbidden:
            raise RuntimeError("State or candidate scripts entered validation archive: " + repr(forbidden))
        return {"kind": "application-source-resource-staging-validation-only",
                "not_a_release": True, "runtime_executed": False, "pyinstaller_executed": False,
                "qt_binaries_or_plugins_in_archive": False, "existing_builder_sha256": digest(script),
                "source_snapshot_manifest": source_records, "static_data_paths": static_paths,
                "builder_data_mappings": mappings, "archive_manifest": manifest,
                "fork_scripts_in_source_snapshot": [r["path"] for r in source_records if r["path"] in FORK_SCRIPTS],
                "extracted_validation_directory": extraction.name,
                "excluded_state_or_candidates_in_archive": forbidden, "verification": verification,
                "documentation_validation": documentation, "reference_manifest_validation": reference_manifest}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--package-validation", action="store_true")
    args = parser.parse_args()
    root, output = args.root.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    graph = collect_graph(root)
    (output / "distribution-graph.json").write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {"source_files": len(graph["source_files"]), "resources": len(graph["resources"]),
               "edges": len(graph["edges"]), "dynamic_calls": len(graph["dynamic_calls"]),
               "external_import_roots": len(graph["external_imports"]),
               "qml_plugin_modules": len(graph["qml_plugin_imports"]),
               "parse_failures": len(graph["parse_failures"]),
               "registered_entrypoints": len(graph["registered_entrypoints"]),
               "unresolved_internal_imports": len(graph["unresolved_internal_imports"]),
               "unresolved_local_literals": len(graph["unresolved_local_literals"]),
               "cleanup_actions": 0}
    if args.package_validation:
        package = package_validation(root, output)
        packaged = {r["path"]: r for r in package["archive_manifest"]}
        missing = [r["path"] for r in graph["resources"] if r["path"] not in packaged]
        drift = [r["path"] for r in graph["resources"] if r["path"] in packaged and r["sha256"] != packaged[r["path"]]["sha256"]]
        package["resource_graph_archive_consistency"] = {"resource_count": len(graph["resources"]),
                                                         "missing": missing, "hash_drift": drift,
                                                         "matched": not missing and not drift}
        (output / "package-validation.json").write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        summary["package"] = package["verification"]
        summary["resource_graph_archive_match"] = not missing and not drift
        summary["selected_offline_documentation_verified"] = package["documentation_validation"]["passed"]
        summary["cie_reference_manifest_exact_bytes_verified"] = package["reference_manifest_validation"]["passed"]
        if (missing or drift or not package["documentation_validation"]["passed"]
                or not package["reference_manifest_validation"]["passed"]):
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if graph["parse_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
