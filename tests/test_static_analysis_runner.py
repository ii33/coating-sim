"""Offline contracts for incremental analysis gates and analyzer failure handling."""
from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import replace
import importlib.metadata
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any
import unittest
from unittest import mock

from tools import run_static_analysis as analysis


def completed(stdout: str = "", code: int = 0,
              stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["offline-analyzer"], code, stdout, stderr)


def qml_row(filename: str, warnings: list[dict[str, Any]] | None = None,
            success: bool = True) -> dict[str, Any]:
    return {"filename": filename, "warnings": warnings or [], "success": success}


def qml_warning(code: str = "unused-imports", severity: str = "warning") -> dict[str, Any]:
    return {"id": code, "message": "An offline diagnostic", "line": 1,
            "column": 1, "type": severity}


class StaticAnalysisRunnerTests(unittest.TestCase):
    """Use synthetic sources and mocked processes; never require installed analyzers."""

    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temporary = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(temporary)
        self.output = self.root / "artifacts" / "static-analysis"
        self.baseline_path = self.root / "tools" / "static_analysis" / "baseline.json"
        self.details: dict[str, Any] = {"python": [3, 12], "tools": {"offline": "1"}}
        self.stack.enter_context(mock.patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(mock.patch.object(analysis, "ROOT", self.root))
        confined = analysis.confined_path
        relative = analysis.relative_source
        # These public helpers capture ROOT in their default arguments. Bind the
        # synthetic repository explicitly instead of changing production files.
        self.stack.enter_context(mock.patch.object(
            analysis, "confined_path",
            side_effect=lambda value, root=None: confined(
                value, self.root if root is None else root),
        ))
        self.stack.enter_context(mock.patch.object(
            analysis, "relative_source",
            side_effect=lambda value, root=None: relative(
                value, self.root if root is None else root),
        ))
        analysis.source_lines.cache_clear()
        analysis.scopes.cache_clear()
        self.addCleanup(analysis.source_lines.cache_clear)
        self.addCleanup(analysis.scopes.cache_clear)
        self.write("sample.py", "def sample():\n    unknown_name()\n")
        self.write("Main.qml", "import QtQuick\nItem {}\n")
        self.write("requirements-analysis-lock.txt",
                   "PySide6_Essentials==6.12.0\nPySide6_Addons==6.12.0\n")

    def write(self, name: str, text: str = "pass\n") -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def diagnostic(self, **changes: Any) -> analysis.Diagnostic:
        return replace(analysis.Diagnostic(
            "ruff", "sample.py", "F821", "Undefined name `unknown_name`", 2, 5,
        ), **changes)

    def write_qml_output(self, rows: list[dict[str, Any]]) -> Path:
        return self.write("qml-output.json", json.dumps({"files": rows}))

    def write_baseline(self, diagnostics: list[analysis.Diagnostic]) -> bytes:
        index = analysis.diagnostic_index(diagnostics)
        value = {"schema": 1, "config_digest": "offline-config",
                 "environment": self.details, "reason": "Reviewed offline fixture",
                 "source_commit": "offline-commit",
                 "diagnostics": list(index.values())}
        self.write(str(self.baseline_path.relative_to(self.root)), json.dumps(value))
        return self.baseline_path.read_bytes()

    def invoke_main(self, mode: str, diagnostics: list[analysis.Diagnostic],
                    reason: str | None = None, *,
                    scan_side_effect: Any | None = None,
                    discover_side_effect: Any | None = None) -> int:
        arguments = [mode, "--output", str(self.output),
                     "--baseline", str(self.baseline_path)]
        if reason is not None:
            arguments.extend(["--reason", reason])
        discover_patch = (
            mock.patch.object(analysis, "discover_sources", side_effect=discover_side_effect)
            if discover_side_effect is not None else
            mock.patch.object(analysis, "discover_sources",
                              return_value=["sample.py", "Main.qml"])
        )
        scan_patch = (
            mock.patch.object(analysis, "scan", side_effect=scan_side_effect)
            if scan_side_effect is not None else
            mock.patch.object(analysis, "scan", return_value=diagnostics)
        )
        with (
            mock.patch.object(analysis, "toolchain", return_value=(
                self.details, self.root / "qmllint", self.root / "qml-imports")),
            discover_patch,
            scan_patch,
            mock.patch.object(analysis, "config_digest", return_value="offline-config"),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            return analysis.main(arguments)

    def invoke_analyzer_failure(self, error: str = "Missing qmllint") -> int:
        arguments = ["scan", "--output", str(self.output),
                     "--baseline", str(self.baseline_path)]
        with (
            mock.patch.object(analysis, "toolchain", side_effect=analysis.AnalysisError(error)),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            return analysis.main(arguments)

    def assert_tagged_product(self, value: dict[str, Any]) -> None:
        self.assertEqual(value["producer"], analysis.PRODUCER)
        self.assertRegex(value["run_id"], r"^[0-9a-f]{32}$")

    def assert_no_temporary_products(self) -> None:
        self.assertEqual([path for path in self.output.iterdir() if path.suffix == ".tmp"], [])

    def test_fingerprint_survives_line_drift_and_incidental_whitespace(self) -> None:
        diagnostic = self.diagnostic(
            message=f"Undefined name at 2:5 on line 2 in {self.root}/sample.py")
        before = analysis.identity(diagnostic)
        # Preserve the anchor's tokens: indentation and spacing are irrelevant.
        self.write("sample.py", "\n\nclass Marker:\n    pass\n\ndef sample():\n"
                   "\tunknown_name()   \n")
        analysis.source_lines.cache_clear()
        analysis.scopes.cache_clear()
        after = analysis.identity(replace(
            diagnostic, line=7, column=2,
            message=f"Undefined name at 7:2 on line 7 in {self.root}/sample.py",
        ))
        self.assertEqual(before, after)
        self.assertEqual(analysis.fingerprint(before), analysis.fingerprint(after))
        self.assertEqual(after["scope"], "sample")
        self.assertNotIn(str(self.root), after["message"])

    def test_same_anchor_in_different_scopes_is_new_debt(self) -> None:
        self.write("sample.py", "def first():\n    unknown_name()\n"
                   "def second():\n    unknown_name()\n")
        first = analysis.identity(self.diagnostic())
        second = analysis.identity(self.diagnostic(line=4))
        self.assertEqual(first["anchor"], second["anchor"])
        self.assertNotEqual(analysis.fingerprint(first), analysis.fingerprint(second))

    def test_diagnostic_outside_source_lines_is_rejected(self) -> None:
        for line in (0, -1, 3):
            with self.subTest(line=line), self.assertRaises(analysis.AnalysisError):
                analysis.identity(self.diagnostic(line=line))

    def test_duplicate_diagnostic_counts_detect_additions_and_resolutions(self) -> None:
        diagnostic = self.diagnostic()
        one = analysis.diagnostic_index([diagnostic])
        two = analysis.diagnostic_index([diagnostic, diagnostic])
        key = next(iter(one))
        self.assertEqual(two[key]["count"], 2)
        self.assertEqual(analysis.compare(two, one), ({key: 1}, {}))
        self.assertEqual(analysis.compare(one, two), ({}, {key: 1}))
        self.assertEqual(analysis.compare(two, two), ({}, {}))

    def test_check_blocks_duplicate_growth_without_rewriting_baseline(self) -> None:
        diagnostic = self.diagnostic()
        original = self.write_baseline([diagnostic])
        self.assertEqual(self.invoke_main("check", [diagnostic, diagnostic]), 1)
        report = json.loads((self.output / "report.json").read_text())
        self.assertEqual(report["new_count"], 1)
        self.assertEqual(report["diagnostic_counts"], {"ruff": 2})
        self.assertEqual(self.baseline_path.read_bytes(), original)

    def test_source_content_changes_during_analysis_block_all_baseline_writes(self) -> None:
        diagnostic = self.diagnostic()
        original = self.write_baseline([diagnostic])

        def edit_source(*_args: Any, **_kwargs: Any) -> list[analysis.Diagnostic]:
            self.write("sample.py", "def sample():\n    unknown_name()\n# edited during scan\n")
            return [diagnostic]

        for mode in ("check", "baseline", "prune"):
            with self.subTest(mode=mode):
                self.write("sample.py", "def sample():\n    unknown_name()\n")
                self.baseline_path.write_bytes(original)
                reason = "Reviewed offline snapshot" if mode in {"baseline", "prune"} else None
                self.assertEqual(self.invoke_main(
                    mode, [diagnostic], reason, scan_side_effect=edit_source), 2)
                self.assertEqual(self.baseline_path.read_bytes(), original)
                failure = json.loads((self.output / "failure.json").read_text())
                self.assertIn("Source content changed during analysis", failure["error"])
                self.assertFalse((self.output / "report.json").exists())

    def test_source_file_set_changes_during_analysis_block_pruning(self) -> None:
        diagnostic = self.diagnostic()
        original = self.write_baseline([diagnostic])
        discoveries = iter((
            ["sample.py", "Main.qml"],
            ["sample.py", "Main.qml", "added.py"],
        ))

        def add_source(*_args: Any, **_kwargs: Any) -> list[analysis.Diagnostic]:
            self.write("added.py", "pass\n")
            return []

        self.assertEqual(self.invoke_main(
            "prune", [], "Reviewed changed file set", scan_side_effect=add_source,
            discover_side_effect=lambda: next(discoveries)), 2)
        self.assertEqual(self.baseline_path.read_bytes(), original)
        failure = json.loads((self.output / "failure.json").read_text())
        self.assertIn("Source file set changed during analysis", failure["error"])
        self.assertFalse((self.output / "report.json").exists())

    def test_resolved_debt_blocks_check_until_explicit_prune(self) -> None:
        diagnostic = self.diagnostic()
        original = self.write_baseline([diagnostic])
        self.assertEqual(self.invoke_main("check", []), 1)
        report = json.loads((self.output / "report.json").read_text())
        self.assertEqual(report["resolved_count"], 1)
        self.assertEqual(self.baseline_path.read_bytes(), original)
        self.assertEqual(self.invoke_main("prune", [], "Reviewed resolved warning"), 0)
        self.assertEqual(self.invoke_main("check", []), 0)

    def test_prune_never_adds_new_fingerprints_or_increases_existing_counts(self) -> None:
        kept = self.diagnostic()
        resolved = self.diagnostic(code="B018", message="Unused expression")
        added = self.diagnostic(code="E702", message="Multiple statements")
        self.write_baseline([kept, kept, resolved])
        self.assertEqual(self.invoke_main(
            "prune", [kept, added], "Reviewed resolved entries"), 1)
        after = json.loads(self.baseline_path.read_text())
        expected = list(analysis.diagnostic_index([kept]).values())
        self.assertEqual(after["diagnostics"], expected)
        self.assertEqual(after["reason"], "Reviewed offline fixture")
        self.assertEqual(after["source_commit"], "offline-commit")
        self.assertEqual(after["last_prune_reason"], "Reviewed resolved entries")
        # Repeated existing issues cannot be accepted by increasing their budget.
        self.assertEqual(self.invoke_main(
            "prune", [kept, kept, kept], "No new diagnostic budget"), 1)
        self.assertEqual(json.loads(self.baseline_path.read_text())["diagnostics"], expected)
        self.assertEqual(self.invoke_main("check", [kept, kept]), 1)

    def test_baseline_writes_require_reason_and_are_forbidden_in_ci(self) -> None:
        for mode in ("baseline", "prune"):
            for reason in (None, "   "):
                with self.subTest(mode=mode, reason=reason):
                    self.assertEqual(self.invoke_main(mode, [], reason), 2)
            with mock.patch.dict(os.environ, {"CI": "true"}):
                self.assertEqual(self.invoke_main(mode, [], "Reviewed locally"), 2)
        self.assertFalse(self.baseline_path.exists())

    def test_syntax_is_fatal_even_when_fingerprint_is_in_baseline(self) -> None:
        for tool, code, path, line in (
            ("ruff", "invalid-syntax", "sample.py", 2),
            ("mypy", "syntax", "sample.py", 2),
            ("qmllint", "syntax", "Main.qml", 2),
            ("qmllint", "syntax.unexpected-token", "Main.qml", 2),
        ):
            diagnostic = self.diagnostic(tool=tool, code=code, path=path, line=line)
            for mode in ("check", "scan", "prune", "baseline"):
                with self.subTest(tool=tool, code=code, mode=mode):
                    original = self.write_baseline([diagnostic])
                    self.assertEqual(self.invoke_main(
                        mode, [diagnostic], "Must not accept syntax"), 1)
                    report = json.loads((self.output / "report.json").read_text())
                    self.assertEqual(report["fatal_count"], 1)
                    self.assertEqual(report["exit_code"], 1)
                    self.assert_tagged_product(report)
                    self.assertFalse((self.output / "failure.json").exists())
                    if mode == "baseline":
                        self.assertEqual(self.baseline_path.read_bytes(), original)

    def test_discovery_requests_tracked_and_untracked_sources_and_filters_runtime(self) -> None:
        included = ["tracked.py", "untracked.py", "stubs.pyi", "launcher.pyw",
                    "qml/Main.qml", "tools/utility.py", "tests/test_new.py"]
        ignored = ["runtime/generated.py", "other/vendor.py", "data/cache.py",
                   "results/run.py", "artifacts/report.py", "nested/runtime/cache.qml",
                   "credentials/key.py", "docs/readme.md"]
        for name in included + ignored:
            self.write(name)
        names = included + ignored + ["tracked.py", "removed.py", ""]
        with mock.patch.object(analysis.subprocess, "run",
                               return_value=completed("\0".join(names))) as run:
            self.assertEqual(analysis.discover_sources(self.root), sorted(included))
        run.assert_called_once_with(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=self.root, text=True, capture_output=True, check=False,
        )

    def packaged_source(self) -> str:
        content = b"upstream_unknown()\n"
        files = [{"path": "database/tools/explorer.py", "bytes": len(content),
                  "sha256": hashlib.sha256(content).hexdigest()}]
        encoded = json.dumps(files, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        commit = "a" * 40
        snapshot = f"{commit}-{digest[:16]}-v1"
        folder = f"coat_sim/core/resources/refractiveindex/versions/{snapshot}"
        manifest = {"schema_version": 1, "snapshot_id": snapshot,
                    "upstream_commit": commit, "source_content_sha256": digest,
                    "files": files}
        self.write(f"{folder}/manifest.json", json.dumps(manifest))
        name = f"{folder}/source/database/tools/explorer.py"
        self.write(name).write_bytes(content)
        return name

    def test_packaged_reference_boundary_preserves_project_sources(self) -> None:
        vendor = self.packaged_source()
        own = "coat_sim/core/resources/refractiveindex/own.py"
        self.write(own)
        with mock.patch.object(analysis.subprocess, "run",
                               return_value=completed("\0".join([vendor, own, "sample.py"]))):
            self.assertEqual(analysis.discover_sources(self.root), sorted([own, "sample.py"]))
        with self.assertRaises(analysis.AnalysisError):
            analysis.relative_source(vendor)

    def test_packaged_reference_changed_or_unlisted_source_fails_closed(self) -> None:
        vendor = self.packaged_source()
        self.write(vendor, "changed()\n")
        with self.assertRaises(analysis.AnalysisError):
            analysis.packaged_reference_source(Path(vendor), self.root)
        unlisted = str(Path(vendor).with_name("unlisted.py"))
        self.write(unlisted)
        with self.assertRaises(analysis.AnalysisError):
            analysis.packaged_reference_source(Path(unlisted), self.root)

    def test_discovery_failure_is_not_an_empty_success(self) -> None:
        with (
            mock.patch.object(analysis.subprocess, "run",
                              return_value=completed(code=128, stderr="not a repository")),
            self.assertRaises(analysis.AnalysisError),
        ):
            analysis.discover_sources(self.root)

    def test_discovery_rejects_source_symlink_and_parent_directory_symlink(self) -> None:
        self.write("actual/inside.py")
        (self.root / "linked.py").symlink_to(self.root / "actual" / "inside.py")
        (self.root / "linked-dir").symlink_to(self.root / "actual", target_is_directory=True)
        for name in ("linked.py", "linked-dir/inside.py"):
            with (
                self.subTest(name=name),
                mock.patch.object(analysis.subprocess, "run",
                                  return_value=completed(name + "\0")),
                self.assertRaises(analysis.AnalysisError),
            ):
                analysis.discover_sources(self.root)

    def test_paths_cannot_escape_repository(self) -> None:
        for value in ("../outside.py", self.root.parent / "outside.py"):
            with self.subTest(value=value), self.assertRaises(analysis.AnalysisError):
                analysis.confined_path(value, self.root)
        (self.root / "outside-dir").symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(analysis.AnalysisError):
            analysis.confined_path("outside-dir/outside.py", self.root)

    def test_analyzer_paths_reject_excluded_non_source_and_escaped_files(self) -> None:
        for value in ("other/tool.py", "runtime/generated.qml", "docs/page.md", "../bad.py"):
            with self.subTest(value=value), self.assertRaises(analysis.AnalysisError):
                analysis.relative_source(value, self.root)
        self.assertEqual(analysis.relative_source(str(self.root / "sample.py"), self.root),
                         "sample.py")

    def test_execute_removes_runtime_import_overrides(self) -> None:
        overrides = {"MYPYPATH": "unreviewed", "QML_IMPORT_PATH": "unreviewed",
                     "QML2_IMPORT_PATH": "unreviewed", "KEEP_THIS": "value"}
        with (
            mock.patch.dict(os.environ, overrides),
            mock.patch.object(analysis.subprocess, "run", return_value=completed()) as run,
        ):
            analysis.execute(["offline-analyzer"], self.root)
        arguments = run.call_args.kwargs
        for name in ("MYPYPATH", "QML_IMPORT_PATH", "QML2_IMPORT_PATH"):
            self.assertNotIn(name, arguments["env"])
        self.assertEqual(arguments["env"]["KEEP_THIS"], "value")
        self.assertEqual(arguments["timeout"], 600)
        self.assertEqual(arguments["cwd"], self.root)

    def test_missing_or_timed_out_executable_cannot_pass(self) -> None:
        for failure in (FileNotFoundError("tool missing"),
                        subprocess.TimeoutExpired("offline-analyzer", 600)):
            with (
                self.subTest(failure=type(failure).__name__),
                mock.patch.object(analysis.subprocess, "run", side_effect=failure),
                self.assertRaises(analysis.AnalysisError),
            ):
                analysis.execute(["offline-analyzer"], self.root)

    def test_missing_or_unpinned_python_tools_are_rejected(self) -> None:
        for name in ("ruff", "mypy"):
            def version(package: str, missing: str = name) -> str:
                if package == missing:
                    raise importlib.metadata.PackageNotFoundError(package)
                return analysis.VERSIONS[package]

            with (
                self.subTest(name=name),
                mock.patch.object(analysis.importlib.metadata, "version", side_effect=version),
                self.assertRaises(analysis.AnalysisError),
            ):
                analysis.toolchain("offline-python")
        with (
            mock.patch.object(analysis.importlib.metadata, "version", return_value="0.0.0"),
            self.assertRaises(analysis.AnalysisError),
        ):
            analysis.toolchain("offline-python")

    def test_runtime_metadata_failure_or_invalid_json_cannot_pass(self) -> None:
        for result in (completed(code=1, stderr="dependency missing"),
                       completed("not JSON")):
            with (
                self.subTest(result=result),
                mock.patch.object(analysis, "execute", return_value=result),
                self.assertRaises(analysis.AnalysisError),
            ):
                analysis.runtime_details("offline-python")

    def test_missing_qmllint_or_qtquick_metadata_is_rejected(self) -> None:
        pyside = self.root / "PySide6"
        details = {"python": [3, 12], "pyside": str(pyside),
                   "versions": {name.replace("-", "_"): analysis.VERSIONS[name]
                                for name in ("PySide6-Essentials", "PySide6-Addons")}}
        for native_present in (False, True):
            if native_present:
                executable = "qmllint.exe" if os.name == "nt" else "qmllint"
                self.write(f"PySide6/{executable}", "offline executable")
            with (
                self.subTest(native_present=native_present),
                mock.patch.object(analysis.importlib.metadata, "version",
                                  side_effect=lambda name: analysis.VERSIONS[name]),
                mock.patch.object(analysis, "runtime_details", return_value=dict(details)),
                self.assertRaises(analysis.AnalysisError),
            ):
                analysis.toolchain("offline-python")

    def test_ruff_invalid_json_or_non_diagnostic_exit_cannot_pass(self) -> None:
        for result in (completed("not JSON"), completed("{}"),
                       completed("[]", code=1), completed("[]", code=2, stderr="bad config")):
            with self.subTest(result=result), self.assertRaises(analysis.AnalysisError):
                analysis.parse_ruff(result)
        self.assertEqual(analysis.parse_ruff(completed("[]")), [])

    def test_ruff_syntax_code_is_preserved_as_fatal_category(self) -> None:
        row = {"filename": str(self.root / "sample.py"), "code": None,
               "message": "Expected a statement", "location": {"row": 2, "column": 5}}
        diagnostics = analysis.parse_ruff(completed(json.dumps([row]), code=1))
        self.assertEqual(diagnostics[0].code, "invalid-syntax")
        self.assertEqual(diagnostics[0].path, "sample.py")

    def test_mypy_invalid_json_stderr_or_non_diagnostic_exit_cannot_pass(self) -> None:
        generic = json.dumps({"file": "sample.py", "code": "name-defined",
                              "message": "Undefined name", "line": 2,
                              "column": 4, "severity": "error"})
        for result in (completed("not JSON"), completed("{}"), completed(code=1),
                       completed(code=2), completed(generic, code=2),
                       completed(code=3), completed(stderr="internal problem")):
            with self.subTest(result=result), self.assertRaises(analysis.AnalysisError):
                analysis.parse_mypy(result)
        self.assertEqual(analysis.parse_mypy(completed()), [])
        self.assertEqual(analysis.parse_mypy(completed("\n \n")), [])
        diagnostic = analysis.parse_mypy(completed(generic, code=1))[0]
        self.assertEqual(diagnostic.column, 5)

    def test_mypy_text_syntax_fallback_is_parsed_instead_of_ignored(self) -> None:
        result = completed("sample.py:2:5: error: Invalid syntax  [syntax]\n", code=2)
        diagnostics = analysis.parse_mypy(result)
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0].code, "syntax")
        self.assertEqual(diagnostics[0].line, 2)

    def test_qmllint_requires_the_complete_expected_file_set(self) -> None:
        for rows in ([qml_row("Main.qml")],
                     [qml_row("Main.qml"), qml_row("Unexpected.qml")], []):
            output = self.write_qml_output(rows)
            with self.subTest(rows=rows), self.assertRaises(analysis.AnalysisError):
                analysis.parse_qml(completed(), output, ["Main.qml", "Other.qml"])
        output = self.write_qml_output([qml_row("Other.qml"), qml_row("Main.qml")])
        self.assertEqual(analysis.parse_qml(completed(), output, ["Main.qml", "Other.qml"]), [])

    def test_qmllint_missing_or_invalid_json_cannot_pass(self) -> None:
        missing = self.root / "missing-output.json"
        with self.assertRaises(analysis.AnalysisError):
            analysis.parse_qml(completed(), missing, ["Main.qml"])
        for content in ("not JSON", "{}", '{"files": null}', '{"files": [{}]}'):
            output = self.write("qml-output.json", content)
            with self.subTest(content=content), self.assertRaises(analysis.AnalysisError):
                analysis.parse_qml(completed(), output, ["Main.qml"])

    def test_qmllint_non_diagnostic_failures_never_pass(self) -> None:
        output = self.write_qml_output([qml_row("Main.qml")])
        for result in (completed(code=1), completed(code=255), completed(code=2),
                       completed(stderr="could not analyze import path")):
            with self.subTest(result=result), self.assertRaises(analysis.AnalysisError):
                analysis.parse_qml(result, output, ["Main.qml"])
        output = self.write_qml_output([qml_row("Main.qml", success=False)])
        with self.assertRaises(analysis.AnalysisError):
            analysis.parse_qml(completed(), output, ["Main.qml"])

    def test_qmllint_import_failures_cannot_be_grandfathered(self) -> None:
        output = self.write_qml_output([qml_row("Main.qml", [qml_warning("import")], False)])
        for code in (0, 1, 255):
            with self.subTest(code=code), self.assertRaises(analysis.AnalysisError):
                analysis.parse_qml(completed(code=code), output, ["Main.qml"])

    def test_qmllint_diagnostic_exit_and_syntax_are_preserved(self) -> None:
        output = self.write_qml_output([qml_row("Main.qml", [qml_warning("syntax")], False)])
        for code in (0, 1, 255):
            with self.subTest(code=code):
                diagnostics = analysis.parse_qml(completed(code=code), output, ["Main.qml"])
                self.assertEqual(diagnostics[0].code, "syntax")
                self.assertEqual(diagnostics[0].tool, "qmllint")

    def test_scan_uses_complete_sources_separates_pyw_and_keeps_duplicate_counts(self) -> None:
        sources = ["sample.py", "stubs.pyi", "launcher.pyw", "Main.qml", "Other.qml"]
        self.output.mkdir(parents=True)
        commands: list[list[str]] = []
        row = {"filename": "sample.py", "code": "F821", "message": "Undefined name",
               "location": {"row": 2, "column": 5}}

        def execute(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            if "ruff" in command:
                return completed(json.dumps([row, row]), code=1)
            if "mypy" in command:
                return completed()
            qml_output = Path(command[command.index("--json") + 1])
            qml_output.write_text(json.dumps({"files": [qml_row("Main.qml"),
                                                         qml_row("Other.qml")]}))
            return completed()

        with mock.patch.object(analysis, "execute", side_effect=execute):
            diagnostics = analysis.scan(sources, "offline-python", self.output,
                                        self.root / "qmllint", self.root / "Qt" / "qml")
        self.assertEqual(len(diagnostics), 2)
        self.assertEqual(next(iter(analysis.diagnostic_index(diagnostics).values()))["count"], 2)
        self.assertEqual(commands[0][-3:], ["sample.py", "stubs.pyi", "launcher.pyw"])
        mypy_commands = [command for command in commands if "mypy" in command]
        self.assertEqual(len(mypy_commands), 2)
        self.assertEqual(mypy_commands[0][-2:], ["sample.py", "stubs.pyi"])
        self.assertEqual(mypy_commands[1][-1:], ["launcher.pyw"])
        qml_command = commands[-1]
        self.assertEqual(qml_command[-2:], ["Main.qml", "Other.qml"])
        self.assertIn("--ignore-settings", qml_command)
        import_paths = [qml_command[index + 1]
                        for index, value in enumerate(qml_command) if value == "-I"]
        self.assertIn("tools/static_analysis/qml", import_paths)
        self.assertIn("coat_sim/ui/qml", import_paths)
        self.assertTrue((self.output / "ruff.stdout").is_file())
        self.assertTrue((self.output / "mypy-1.exit-code.txt").is_file())
        self.assertTrue((self.output / "qmllint.stderr").is_file())

    def test_scan_rejects_missing_python_or_qml_sources_before_running_tools(self) -> None:
        for sources in (["sample.py"], ["Main.qml"], []):
            with (
                self.subTest(sources=sources),
                mock.patch.object(analysis, "execute") as execute,
                self.assertRaises(analysis.AnalysisError),
            ):
                analysis.scan(sources, "offline-python", self.output,
                              self.root / "qmllint", self.root / "qml-imports")
            execute.assert_not_called()

    def test_additional_pyw_scans_preserve_own_duplicates_without_recounting_imports(self) -> None:
        sources = ["sample.py", "launcher.pyw", "tools/helper.pyw", "Main.qml"]
        self.write("launcher.pyw", "unknown_name()\n")
        self.write("tools/helper.pyw", "unknown_name()\n")
        self.output.mkdir(parents=True)

        def mypy_row(path: str, line: int = 1) -> str:
            return json.dumps({"file": path, "code": "name-defined",
                               "message": "Undefined name", "line": line,
                               "column": 0, "severity": "error"})

        imported = mypy_row("sample.py", 2)

        def execute(command: list[str]) -> subprocess.CompletedProcess[str]:
            if "ruff" in command:
                return completed("[]")
            if "mypy" in command:
                path = command[-1]
                rows = [imported]
                if path.endswith(".pyw"):
                    rows.extend([mypy_row(path), mypy_row(path)])
                return completed("\n".join(rows), code=1)
            qml_output = Path(command[command.index("--json") + 1])
            qml_output.write_text(json.dumps({"files": [qml_row("Main.qml")]}))
            return completed()

        with mock.patch.object(analysis, "execute", side_effect=execute):
            diagnostics = analysis.scan(sources, "offline-python", self.output,
                                        self.root / "qmllint", self.root / "qml-imports")
        self.assertEqual(len(diagnostics), 5)
        counts = {entry["path"]: entry["count"]
                  for entry in analysis.diagnostic_index(diagnostics).values()}
        self.assertEqual(counts, {"sample.py": 1, "launcher.pyw": 2, "tools/helper.pyw": 2})

    def test_main_records_analyzer_failure_as_exit_two(self) -> None:
        self.assertEqual(self.invoke_analyzer_failure(), 2)
        failure = json.loads((self.output / "failure.json").read_text())
        self.assertEqual(failure["exit_code"], 2)
        self.assertEqual(failure["error"], "Missing qmllint")
        self.assert_tagged_product(failure)

    def test_success_replaces_previous_failure_in_same_output_directory(self) -> None:
        self.assertEqual(self.invoke_analyzer_failure(), 2)
        failure_path = self.output / "failure.json"
        previous = json.loads(failure_path.read_text())
        self.assert_tagged_product(previous)
        self.assertFalse((self.output / "report.json").exists())
        self.assertEqual(self.invoke_main("scan", []), 0)
        self.assertFalse(failure_path.exists())
        report = json.loads((self.output / "report.json").read_text())
        self.assert_tagged_product(report)
        self.assertEqual(report["exit_code"], 0)
        self.assertNotEqual(report["run_id"], previous["run_id"])
        self.assert_no_temporary_products()

    def test_failure_removes_previous_success_report_in_same_output_directory(self) -> None:
        self.assertEqual(self.invoke_main("scan", []), 0)
        report_path = self.output / "report.json"
        previous = json.loads(report_path.read_text())
        self.assert_tagged_product(previous)
        self.assertEqual(self.invoke_analyzer_failure(), 2)
        self.assertFalse(report_path.exists())
        failure = json.loads((self.output / "failure.json").read_text())
        self.assert_tagged_product(failure)
        self.assertEqual(failure["exit_code"], 2)
        self.assertNotEqual(failure["run_id"], previous["run_id"])
        self.assert_no_temporary_products()

    def test_output_reuse_preserves_unrelated_user_files(self) -> None:
        self.output.mkdir(parents=True)
        notes = self.output / "user-notes.json"
        notes.write_bytes(b'{"private": "keep this file"}\n')
        nested = self.output / "user-folder"
        nested.mkdir()
        keep = nested / "report.json"
        keep.write_bytes(b"unrelated nested report\n")
        originals = {notes: notes.read_bytes(), keep: keep.read_bytes()}
        self.assertEqual(self.invoke_main("scan", []), 0)
        self.assertEqual(self.invoke_analyzer_failure(), 2)
        self.assertEqual(self.invoke_main("scan", []), 0)
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original)

    def test_ci_wrapper_open_log_and_exit_code_survive_failed_and_successful_runs(self) -> None:
        self.output.mkdir(parents=True)
        exit_code_path = self.output / "exit-code.txt"
        original_exit_code = b"wrapper-owned exit code\n"
        exit_code_path.write_bytes(original_exit_code)
        log_path = self.output / "analysis.log"
        expected_log = "CI wrapper started\n"
        with log_path.open("w", encoding="utf-8") as log_stream:
            log_stream.write(expected_log)
            log_stream.flush()
            original_log_identity = log_path.stat().st_ino
            self.assertEqual(self.invoke_analyzer_failure(), 2)
            self.assertEqual(exit_code_path.read_bytes(), original_exit_code)
            self.assertEqual(log_path.read_text(encoding="utf-8"), expected_log)
            self.assertEqual(log_path.stat().st_ino, original_log_identity)
            failure = json.loads((self.output / "failure.json").read_text())
            self.assert_tagged_product(failure)
            self.assertFalse((self.output / "report.json").exists())
            log_stream.write("CI wrapper recorded failure\n")
            log_stream.flush()
            expected_log += "CI wrapper recorded failure\n"
            self.assertEqual(self.invoke_main("scan", []), 0)
            self.assertEqual(exit_code_path.read_bytes(), original_exit_code)
            self.assertEqual(log_path.read_text(encoding="utf-8"), expected_log)
            self.assertEqual(log_path.stat().st_ino, original_log_identity)
            self.assertFalse((self.output / "failure.json").exists())
            report = json.loads((self.output / "report.json").read_text())
            self.assert_tagged_product(report)
            self.assertFalse(log_stream.closed)
            log_stream.write("CI wrapper recorded success\n")
            log_stream.flush()
            expected_log += "CI wrapper recorded success\n"
            self.assertEqual(log_path.read_text(encoding="utf-8"), expected_log)
        self.assertEqual(log_path.read_text(encoding="utf-8"), expected_log)
        self.assertEqual(exit_code_path.read_bytes(), original_exit_code)
        self.assert_no_temporary_products()

    def test_unrecognized_fixed_products_fail_closed_without_overwriting_user_files(self) -> None:
        for name in ("report.json", "failure.json"):
            for index, content in enumerate((b"not JSON\n", b"{}\n",
                                             b'{"producer": "another-application"}\n')):
                with self.subTest(name=name, content=content):
                    self.output = self.root / "artifacts" / "static-analysis" / name / str(index)
                    self.output.mkdir(parents=True)
                    product = self.output / name
                    product.write_bytes(content)
                    notes = self.output / "user-notes.txt"
                    notes.write_bytes(b"preserve me\n")
                    self.assertEqual(self.invoke_main("scan", []), 2)
                    self.assertEqual(product.read_bytes(), content)
                    self.assertEqual(notes.read_bytes(), b"preserve me\n")
                    other = self.output / ("failure.json" if name == "report.json" else "report.json")
                    self.assertFalse(other.exists())
                    self.assert_no_temporary_products()

    def test_fixed_product_symlinks_and_directories_are_never_deleted(self) -> None:
        target = self.write("user-target.json", '{"private": "keep"}\n')
        original = target.read_bytes()
        for name in ("report.json", "failure.json"):
            for kind in ("symlink", "directory"):
                with self.subTest(name=name, kind=kind):
                    self.output = self.root / "artifacts" / "static-analysis" / name / kind
                    self.output.mkdir(parents=True)
                    product = self.output / name
                    if kind == "symlink":
                        product.symlink_to(target)
                    else:
                        product.mkdir()
                        (product / "user-file.txt").write_bytes(b"keep directory contents\n")
                    self.assertEqual(self.invoke_main("scan", []), 2)
                    if kind == "symlink":
                        self.assertTrue(product.is_symlink())
                    else:
                        self.assertTrue(product.is_dir())
                        self.assertEqual((product / "user-file.txt").read_bytes(),
                                         b"keep directory contents\n")
                    self.assertEqual(target.read_bytes(), original)
                    self.assert_no_temporary_products()

    def test_product_cleanup_unlink_failure_is_exit_two_and_preserves_previous_file(self) -> None:
        self.assertEqual(self.invoke_main("scan", []), 0)
        report_path = self.output / "report.json"
        original = report_path.read_bytes()
        unlink = Path.unlink

        def fail_cleanup(path: Path, *args: Any, **kwargs: Any) -> None:
            if path == report_path:
                raise PermissionError("Previous report cannot be removed")
            unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", autospec=True, side_effect=fail_cleanup):
            self.assertEqual(self.invoke_main("scan", []), 2)
        self.assertEqual(report_path.read_bytes(), original)
        self.assertFalse((self.output / "failure.json").exists())
        self.assert_no_temporary_products()

    def test_report_staging_write_failure_is_exit_two_without_report_or_temp_leftovers(self) -> None:
        self.assertEqual(self.invoke_main("scan", []), 0)
        write_text = Path.write_text
        attempts: list[Path] = []

        def fail_report_write(path: Path, text: str, *args: Any, **kwargs: Any) -> int:
            if path.name.startswith(".report.json.") and path.suffix == ".tmp":
                attempts.append(path)
                self.assertFalse((self.output / "report.json").exists())
                write_text(path, "partially written JSON", encoding="utf-8")
                raise OSError("Report staging disk failure")
            return write_text(path, text, *args, **kwargs)

        with mock.patch.object(Path, "write_text", autospec=True, side_effect=fail_report_write):
            self.assertEqual(self.invoke_main("scan", []), 2)
        self.assertEqual(len(attempts), 1)
        self.assertFalse((self.output / "report.json").exists())
        failure = json.loads((self.output / "failure.json").read_text())
        self.assertEqual(failure["exit_code"], 2)
        self.assertIn("Report staging disk failure", failure["error"])
        self.assert_tagged_product(failure)
        self.assert_no_temporary_products()

    def test_report_atomic_publish_failure_is_exit_two_and_removes_staging_file(self) -> None:
        replace_path = Path.replace

        def fail_publish(path: Path, target: str | Path) -> Path:
            if Path(target).name == "report.json":
                self.assertTrue(path.is_file())
                self.assertEqual(json.loads(path.read_text())["producer"], analysis.PRODUCER)
                self.assertFalse((self.output / "report.json").exists())
                raise OSError("Atomic report publish failed")
            return replace_path(path, target)

        with mock.patch.object(Path, "replace", autospec=True, side_effect=fail_publish):
            self.assertEqual(self.invoke_main("scan", []), 2)
        self.assertFalse((self.output / "report.json").exists())
        failure = json.loads((self.output / "failure.json").read_text())
        self.assertEqual(failure["exit_code"], 2)
        self.assert_tagged_product(failure)
        self.assert_no_temporary_products()

    def test_baseline_and_prune_publish_failure_preserves_old_baseline_and_only_failure(self) -> None:
        replace_path = Path.replace
        attempts: list[Path] = []

        def fail_baseline_publish(path: Path, target: str | Path) -> Path:
            if Path(target) == self.baseline_path:
                attempts.append(path)
                self.assertTrue(path.is_file())
                self.assertFalse((self.output / "report.json").exists())
                raise OSError("Baseline atomic publish failed")
            return replace_path(path, target)

        for mode in ("baseline", "prune"):
            with self.subTest(mode=mode):
                original = self.write_baseline([self.diagnostic()])
                self.assertEqual(self.invoke_main("scan", []), 0)
                with (
                    mock.patch.object(Path, "replace", autospec=True,
                                      side_effect=fail_baseline_publish),
                    mock.patch.object(analysis, "execute",
                                      return_value=completed("offline-new-commit\n")),
                ):
                    self.assertEqual(self.invoke_main(mode, [], "Reviewed changes"), 2)
                self.assertEqual(self.baseline_path.read_bytes(), original)
                self.assertFalse((self.output / "report.json").exists())
                failure = json.loads((self.output / "failure.json").read_text())
                self.assertEqual(failure["exit_code"], 2)
                self.assertIn("Baseline atomic publish failed", failure["error"])
                self.assert_tagged_product(failure)
                self.assert_no_temporary_products()
                self.assertEqual([path for path in self.baseline_path.parent.iterdir()
                                  if path.suffix == ".tmp"], [])
        self.assertEqual(len(attempts), 2)

    def test_legacy_default_artifact_products_are_safely_migrated(self) -> None:
        self.output = self.root / "artifacts" / "static-analysis" / "latest"
        self.output.mkdir(parents=True)
        report_path = self.output / "report.json"
        legacy_report = {"schema": 1, "mode": "check", "sources": ["sample.py", "Main.qml"],
                         "diagnostics": [], "diagnostic_counts": {}, "fatal_count": 0}
        report_path.write_text(json.dumps(legacy_report))
        failure_path = self.output / "failure.json"
        failure_path.write_text(json.dumps({"exit_code": 2, "error": "Legacy analyzer failure"}))
        notes = self.output / "user-notes.txt"
        notes.write_bytes(b"keep my notes\n")
        self.assertEqual(self.invoke_main("scan", []), 0)
        self.assertFalse(failure_path.exists())
        report = json.loads(report_path.read_text())
        self.assert_tagged_product(report)
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(notes.read_bytes(), b"keep my notes\n")
        self.assert_no_temporary_products()

    def test_legacy_failure_outside_artifact_directory_is_not_treated_as_owned(self) -> None:
        self.output = self.root / "custom-user-output"
        self.output.mkdir()
        failure_path = self.output / "failure.json"
        original = b'{"exit_code": 2, "error": "User application failure"}\n'
        failure_path.write_bytes(original)
        self.assertEqual(self.invoke_main("scan", []), 2)
        self.assertEqual(failure_path.read_bytes(), original)
        self.assertFalse((self.output / "report.json").exists())

    def test_baseline_rejects_tampering_and_invalid_counts(self) -> None:
        self.write_baseline([self.diagnostic()])
        original = json.loads(self.baseline_path.read_text())
        for change in ("fingerprint", "duplicate", "count-zero", "count-string", "config"):
            baseline = json.loads(json.dumps(original))
            entry = baseline["diagnostics"][0]
            if change == "fingerprint":
                entry["message"] = "Tampered warning"
            elif change == "duplicate":
                baseline["diagnostics"].append(dict(entry))
            elif change == "count-zero":
                entry["count"] = 0
            elif change == "count-string":
                entry["count"] = "1"
            else:
                baseline["config_digest"] = "unreviewed-config"
            self.baseline_path.write_text(json.dumps(baseline))
            with (
                self.subTest(change=change),
                mock.patch.object(analysis, "config_digest", return_value="offline-config"),
                self.assertRaises(analysis.AnalysisError),
            ):
                analysis.read_baseline(self.baseline_path, self.details)


if __name__ == "__main__":
    unittest.main()
