#!/usr/bin/env python3
"""Scan all repository sources; block new diagnostics against a reviewed baseline."""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from dataclasses import asdict, dataclass
from functools import lru_cache
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path("tools/static_analysis/baseline.json")
PRODUCER = "deposition-sim-static-analysis"
EXCLUDED = frozenset({
    "other", ".git", ".venv", "venv", ".conda", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "artifacts", "build", "dist", "history",
    "runtime", "snapshots", "results", "credentials", "secrets", ".aws", ".codex",
    ".agents", "data", "config", "结果", ".venv-analysis", "env",
})
SUFFIXES = frozenset({".py", ".pyi", ".pyw", ".qml"})
VERSIONS = {"ruff": "0.17.0", "mypy": "2.4.0", "PySide6-Essentials": "6.12.0",
            "PySide6-Addons": "6.12.0"}


class AnalysisError(RuntimeError):
    """A failed analyzer/configuration is not a diagnostic-free scan."""


@dataclass(frozen=True)
class Diagnostic:
    tool: str
    path: str
    code: str
    message: str
    line: int
    column: int
    severity: str = "error"


def confined_path(value: str | Path, root: Path = ROOT) -> Path:
    candidate = Path(value)
    candidate = candidate if candidate.is_absolute() else root / candidate
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise AnalysisError(f"Path escapes repository: {value}") from exc
    return candidate


def relative_source(value: str, root: Path = ROOT) -> str:
    path = confined_path(value, root).resolve().relative_to(root.resolve())
    if any(part in EXCLUDED for part in path.parts) or path.suffix not in SUFFIXES:
        raise AnalysisError(f"Analyzer reported an excluded/non-source path: {value}")
    return path.as_posix()


def discover_sources(root: Path = ROOT) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root, text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise AnalysisError("Cannot enumerate tracked and untracked sources: " + result.stderr)
    sources = set()
    for name in result.stdout.split("\0"):
        path = Path(name)
        if not name or path.suffix not in SUFFIXES or any(x in EXCLUDED for x in path.parts):
            continue
        absolute = confined_path(path, root)
        if any((root / Path(*path.parts[:index])).is_symlink()
               for index in range(1, len(path.parts) + 1)):
            raise AnalysisError(f"Source symlinks are not accepted: {name}")
        if absolute.is_file():
            sources.add(path.as_posix())
    return sorted(sources)


def source_digest(sources: list[str]) -> str:
    """Hash the analyzed file set and exact source bytes for snapshot checks."""
    digest = hashlib.sha256()
    for name in sorted(sources):
        path = confined_path(name)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise AnalysisError(f"Cannot read source during snapshot check: {name}") from exc
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def require_unchanged_sources(sources: list[str], before_digest: str) -> None:
    """Reject results if analyzer execution overlapped source edits or file churn."""
    current_sources = discover_sources()
    if current_sources != sources:
        raise AnalysisError(
            "Source file set changed during analysis; rerun against a stable source snapshot"
        )
    if source_digest(current_sources) != before_digest:
        raise AnalysisError(
            "Source content changed during analysis; rerun against a stable source snapshot"
        )


def execute(command: list[str], root: Path = ROOT) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    # Source discovery and Qt imports are explicit, never a user's runtime QML path.
    for name in ("MYPYPATH", "QML_IMPORT_PATH", "QML2_IMPORT_PATH"):
        env.pop(name, None)
    try:
        return subprocess.run(command, cwd=root, env=env, text=True,
                              capture_output=True, check=False, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AnalysisError(f"Analyzer could not run: {command[0]}: {exc}") from exc


def runtime_details(python: str) -> dict[str, Any]:
    names = [line.split("==")[0] for line in
             (ROOT / "requirements-analysis-lock.txt").read_text(encoding="utf-8").splitlines()
             if line and not line.startswith("#")]
    code = (
        "import importlib.metadata as m,json,sys; "
        f"names={names!r}; "
        "versions={n:m.version(n) for n in names}; "
        "print(json.dumps({'python':list(sys.version_info[:2]),'versions':versions,"
        "'pyside':str(m.distribution('PySide6-Essentials').locate_file('PySide6'))}))"
    )
    result = execute([python, "-c", code])
    if result.returncode:
        raise AnalysisError("Incomplete dependency interpreter: " + result.stderr.strip())
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise AnalysisError("Invalid dependency interpreter metadata") from exc


def toolchain(python: str) -> tuple[dict[str, Any], Path, Path]:
    versions = {}
    for name in ("ruff", "mypy"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise AnalysisError(f"Missing {name}; install requirements-analysis.txt") from exc
        if versions[name] != VERSIONS[name]:
            raise AnalysisError(f"{name} version {versions[name]} != pinned {VERSIONS[name]}")
    details = runtime_details(python)
    if details["python"] != [3, 12]:
        raise AnalysisError("Analysis dependency interpreter must use Python 3.12")
    for name in ("PySide6-Essentials", "PySide6-Addons"):
        if details["versions"][name.replace("-", "_")] != VERSIONS[name]:
            raise AnalysisError(f"{name} must match the pinned Qt {VERSIONS[name]}")
    pyside = Path(details.pop("pyside"))
    qmllint = pyside / ("qmllint.exe" if os.name == "nt" else "qmllint")
    qmlimports = pyside / "Qt" / "qml"
    if not qmllint.is_file() or not (qmlimports / "QtQuick" / "qmldir").is_file():
        raise AnalysisError("Missing native qmllint or QtQuick import metadata")
    result = execute([str(qmllint), "--version"])
    if result.returncode or result.stdout.strip() != "qmllint 6.12.0":
        raise AnalysisError("Native qmllint version probe failed: " + result.stdout + result.stderr)
    details["tools"] = versions | {"qmllint": "6.12.0"}
    return details, qmllint, qmlimports


def save_raw(output: Path, name: str, result: subprocess.CompletedProcess[str]) -> None:
    (output / f"{name}.stdout").write_text(result.stdout, encoding="utf-8")
    (output / f"{name}.stderr").write_text(result.stderr, encoding="utf-8")
    (output / f"{name}.exit-code.txt").write_text(str(result.returncode) + "\n", encoding="utf-8")


def parse_ruff(result: subprocess.CompletedProcess[str]) -> list[Diagnostic]:
    if result.returncode not in (0, 1):
        raise AnalysisError("Ruff execution failure: " + result.stderr.strip())
    try:
        rows = json.loads(result.stdout)
        if not isinstance(rows, list):
            raise AnalysisError("Ruff JSON root must be a diagnostic list")
        diagnostics = [Diagnostic("ruff", relative_source(x["filename"]),
                                  x["code"] or "invalid-syntax", x["message"],
                                  x["location"]["row"], x["location"]["column"])
                       for x in rows]
    except (ValueError, KeyError, TypeError) as exc:
        raise AnalysisError("Invalid Ruff diagnostic JSON") from exc
    if result.returncode == 1 and not diagnostics:
        raise AnalysisError("Ruff failed without diagnostics")
    return diagnostics


def parse_mypy(result: subprocess.CompletedProcess[str]) -> list[Diagnostic]:
    if result.returncode not in (0, 1, 2):
        raise AnalysisError("Mypy execution failure: " + result.stderr.strip())
    diagnostics = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            diagnostics.append(Diagnostic("mypy", relative_source(row["file"]), row["code"],
                                          row["message"], row["line"], row["column"] + 1,
                                          row["severity"]))
        except (ValueError, KeyError, TypeError) as exc:
            # mypy's syntax-error path can still print text under --output=json.
            match = re.fullmatch(r"(.+?):(\d+):(?:\d+:)? error: (.+)  \[syntax\]", line)
            if not match:
                raise AnalysisError("Unparseable mypy output: " + line) from exc
            diagnostics.append(Diagnostic("mypy", relative_source(match[1]), "syntax",
                                          match[3], int(match[2]), 1))
    if result.stderr.strip() or (result.returncode and not diagnostics):
        raise AnalysisError("Mypy failed: " + (result.stderr or result.stdout).strip())
    if result.returncode == 2 and not any(x.code == "syntax" for x in diagnostics):
        raise AnalysisError("Mypy configuration/internal failure (exit 2)")
    return diagnostics


def parse_qml(result: subprocess.CompletedProcess[str], output: Path,
              expected: list[str]) -> list[Diagnostic]:
    # Qt returns -1 (255 on POSIX) for diagnosed warnings under -W 0.
    if result.returncode not in (0, 1, 255):
        raise AnalysisError(f"qmllint execution failure ({result.returncode}): {result.stderr}")
    try:
        rows = json.loads(output.read_text(encoding="utf-8"))["files"]
        scanned = {relative_source(x["filename"]) for x in rows}
        if scanned != set(expected):
            raise AnalysisError("qmllint omitted files or reported unexpected files")
        diagnostics = [Diagnostic("qmllint", relative_source(f["filename"]),
                                  x["id"], x["message"], x["line"], x["column"], x["type"])
                       for f in rows for x in f["warnings"]]
        if any(not f["success"] and not f["warnings"] for f in rows):
            raise AnalysisError("qmllint failed a file without diagnostics")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AnalysisError("Missing/invalid qmllint JSON") from exc
    if result.stderr.strip() or (result.returncode and not diagnostics):
        raise AnalysisError("qmllint failed: " + (result.stderr or result.stdout).strip())
    # A broken analysis import configuration must never be grandfathered.
    if any(x.code == "import" for x in diagnostics):
        raise AnalysisError("qmllint import metadata failure; inspect qmllint.json")
    return diagnostics


@lru_cache(maxsize=None)
def source_lines(path: str) -> tuple[str, ...]:
    return tuple((ROOT / path).read_text(encoding="utf-8-sig").splitlines())


@lru_cache(maxsize=None)
def scopes(path: str) -> tuple[tuple[int, int, str], ...]:
    if Path(path).suffix not in {".py", ".pyi", ".pyw"}:
        return ()
    try:
        tree = ast.parse("\n".join(source_lines(path)))
    except SyntaxError:
        return ()
    result = []

    def walk(node: ast.AST, parents: tuple[str, ...]) -> None:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            parents += (node.name,)
            result.append((node.lineno, node.end_lineno or node.lineno, ".".join(parents)))
        for child in ast.iter_child_nodes(node):
            walk(child, parents)

    walk(tree, ())
    return tuple(result)


def identity(diagnostic: Diagnostic) -> dict[str, Any]:
    lines = source_lines(diagnostic.path)
    if not 1 <= diagnostic.line <= len(lines):
        raise AnalysisError(f"Diagnostic has invalid source line: {diagnostic}")
    scope = ""
    for start, end, name in scopes(diagnostic.path):
        if start <= diagnostic.line <= end:
            scope = name
    # No absolute paths or source positions: blank-line insertion cannot reset debt.
    message = diagnostic.message.replace(str(ROOT), "<repository>")
    message = re.sub(r"(?<=at )\d+:\d+", "<location>", message)
    message = re.sub(r"(?<=on line )\d+", "<line>", message)
    return {"tool": diagnostic.tool, "path": diagnostic.path, "code": diagnostic.code,
            "message": message, "scope": scope,
            "anchor": " ".join(lines[diagnostic.line - 1].split())}


def fingerprint(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def diagnostic_index(diagnostics: list[Diagnostic]) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for diagnostic in diagnostics:
        value = identity(diagnostic)
        key = fingerprint(value)
        if key not in entries:
            entries[key] = value | {"fingerprint": key, "count": 0}
        entries[key]["count"] += 1
    return entries


def compare(current: dict[str, dict[str, Any]], baseline: dict[str, dict[str, Any]]) -> tuple[dict[str, int], dict[str, int]]:
    new = {key: max(0, entry["count"] - baseline.get(key, {}).get("count", 0))
           for key, entry in current.items()}
    resolved = {key: max(0, entry["count"] - current.get(key, {}).get("count", 0))
                for key, entry in baseline.items()}
    return ({k: n for k, n in new.items() if n}, {k: n for k, n in resolved.items() if n})


def config_digest() -> str:
    digest = hashlib.sha256()
    for name in ("ruff.toml", "mypy.ini", "requirements-analysis.txt", "requirements-analysis-lock.txt", "tools/run_static_analysis.py", "tools/static_analysis/qml/CoatUi/qmldir",
                 "tools/static_analysis/qml/CoatUi/plugins.qmltypes"):
        digest.update(name.encode())
        digest.update((ROOT / name).read_bytes())
    digest.update(json.dumps(sorted(EXCLUDED)).encode())
    return digest.hexdigest()


def read_baseline(path: Path, details: dict[str, Any]) -> dict[str, Any]:
    try:
        baseline = json.loads(path.read_text(encoding="utf-8"))
        if baseline["schema"] != 1 or baseline["config_digest"] != config_digest():
            raise AnalysisError("Baseline schema/configuration differs; review explicitly")
        if baseline["environment"] != details:
            raise AnalysisError("Analysis dependencies differ from the reviewed baseline")
        entries = baseline["diagnostics"]
        keys = set()
        for entry in entries:
            key = entry["fingerprint"]
            value = {k: v for k, v in entry.items() if k not in {"count", "fingerprint"}}
            if key in keys or key != fingerprint(value) or not isinstance(entry["count"], int) or entry["count"] < 1:
                raise AnalysisError("Invalid/duplicate baseline fingerprint or count")
            relative_source(entry["path"])
            keys.add(key)
        return baseline
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AnalysisError("Cannot read a valid reviewed baseline") from exc


def scan(sources: list[str], python: str, output: Path,
         qmllint: Path, qmlimports: Path) -> list[Diagnostic]:
    python_files = [x for x in sources if Path(x).suffix in {".py", ".pyi", ".pyw"}]
    qml_files = [x for x in sources if Path(x).suffix == ".qml"]
    if not python_files or not qml_files:
        raise AnalysisError("Expected Python and QML sources; scan is incomplete")
    ruff = execute([sys.executable, "-m", "ruff", "check", "--config", "ruff.toml",
                    "--no-cache", "--output-format", "json", *python_files])
    save_raw(output, "ruff", ruff)
    diagnostics = parse_ruff(ruff)
    # .pyw is checked individually to avoid launcher.py/launcher.pyw module collisions.
    mypy_files = [x for x in python_files if Path(x).suffix != ".pyw"]
    groups = [mypy_files] + [[x] for x in python_files if Path(x).suffix == ".pyw"]
    for index, group in enumerate(groups):
        mypy = execute([sys.executable, "-m", "mypy", "--config-file", "mypy.ini",
                        "--python-executable", python, "--no-incremental",
                        "--cache-dir", str(output / "mypy-cache"), "--output", "json", *group])
        save_raw(output, f"mypy-{index}", mypy)
        diagnostics.extend(x for x in parse_mypy(mypy) if index == 0 or x.path in group)
    qml_output = output / "qmllint.json"
    qml = execute([str(qmllint), "--ignore-settings", "--json", str(qml_output), "-W", "0",
                   "-I", str(qmlimports), "-I", "coat_sim/ui/qml", "-I", "tools/static_analysis/qml", *qml_files])
    save_raw(output, "qmllint", qml)
    diagnostics.extend(parse_qml(qml, qml_output, qml_files))
    return sorted(diagnostics, key=lambda x: (x.tool, x.path, x.line, x.column, x.code, x.message))



def prepare_output(output: Path) -> None:
    """Remove only recognized reports owned by this runner, never directory contents."""
    output.mkdir(parents=True, exist_ok=True)
    for name in ("report.json", "failure.json"):
        path = output / name
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_file():
            raise AnalysisError(f"Refusing non-file/symlink output product: {path}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AnalysisError(f"Refusing unrecognized output product: {path}") from exc
        owned = isinstance(value, dict) and value.get("producer") == PRODUCER
        # Recognize reports from the initial runner, before producer tags existed.
        legacy_report = isinstance(value, dict) and name == "report.json" and (
            value.get("schema") == 1 and value.get("mode") in {"check", "scan", "baseline", "prune"}
            and isinstance(value.get("sources"), list)
            and isinstance(value.get("diagnostics"), list)
            and "diagnostic_counts" in value and "fatal_count" in value
        )
        legacy_failure = isinstance(value, dict) and name == "failure.json" and (
            set(value) == {"exit_code", "error"} and value["exit_code"] == 2
            and isinstance(value["error"], str)
            and output.relative_to(ROOT).parts[:2] == ("artifacts", "static-analysis")
        )
        if not (owned or legacy_report or legacy_failure):
            raise AnalysisError(f"Refusing unrecognized output product: {path}")
        try:
            path.unlink()
        except OSError as exc:
            raise AnalysisError(f"Cannot clear previous analysis product: {path}") from exc


def write_json(path: Path, value: dict[str, Any]) -> None:
    """Publish a complete report atomically or fail closed without a success report."""
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "scan", "baseline", "prune"), nargs="?", default="check")
    parser.add_argument("--python-executable", default=sys.executable,
                        help="Python 3.12 interpreter containing the analysis runtime dependencies")
    parser.add_argument("--output", type=Path, default=Path("artifacts/static-analysis/latest"))
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    parser.add_argument("--reason", help="required audit reason for explicit baseline/prune writes")
    args = parser.parse_args(argv)
    output_prepared = False
    run_id = uuid4().hex
    try:
        output = confined_path(args.output)
        baseline_path = confined_path(args.baseline)
        if args.mode in {"baseline", "prune"}:
            if os.environ.get("CI"):
                raise AnalysisError("CI cannot create, refresh, or prune the baseline")
            if not args.reason or not args.reason.strip():
                raise AnalysisError("Baseline writes require --reason and explicit local review")
        prepare_output(output)
        output_prepared = True
        details, qmllint, qmlimports = toolchain(args.python_executable)
        sources = discover_sources()
        sources_digest = source_digest(sources)
        # A long-lived caller/test process may have cached anchors from an
        # earlier run. Fingerprints must read the source snapshot just scanned.
        source_lines.cache_clear()
        scopes.cache_clear()
        diagnostics = scan(sources, args.python_executable, output, qmllint, qmlimports)
        require_unchanged_sources(sources, sources_digest)
        # A mocked/custom scan may also populate these helpers; clear once more
        # after the snapshot check so comparison uses the verified source bytes.
        source_lines.cache_clear()
        scopes.cache_clear()
        current = diagnostic_index(diagnostics)
        fatal = [x for x in diagnostics if x.code in {"invalid-syntax", "syntax"} or x.code.startswith("syntax.")]
        old = None
        new: dict[str, int] = {}
        resolved: dict[str, int] = {}
        if args.mode in {"check", "prune"}:
            old = read_baseline(baseline_path, details)
            previous = {x["fingerprint"]: x for x in old["diagnostics"]}
            new, resolved = compare(current, previous)
        exit_code = 1 if fatal or (args.mode == "check" and (new or resolved)) or (args.mode == "prune" and new) else 0
        summary = {
            "schema": 1, "producer": PRODUCER, "run_id": run_id, "mode": args.mode, "environment": details,
            "source_count": len(sources), "sources": sources,
            "diagnostic_counts": dict(Counter(x.tool for x in diagnostics)),
            "rule_counts": dict(sorted(Counter(f"{x.tool}:{x.code}" for x in diagnostics).items())),
            "new_count": sum(new.values()), "resolved_count": sum(resolved.values()),
            "fatal_count": len(fatal), "exit_code": exit_code,
            "diagnostics": [asdict(x) | {"fingerprint": fingerprint(identity(x)),
                            "is_new": fingerprint(identity(x)) in new} for x in diagnostics],
            "resolved": resolved,
        }
        # Identity generation reads source anchors after analyzer execution too.
        # Check again before publishing reports or mutating a baseline so a
        # concurrent edit cannot be mistaken for a new or resolved diagnostic.
        require_unchanged_sources(sources, sources_digest)
        print(f"Static analysis: {len(sources)} sources; " + "; ".join(f"{name} {count}" for name, count in summary["diagnostic_counts"].items()))
        print(f"New: {sum(new.values())}; resolved baseline debt: {sum(resolved.values())}; fatal syntax: {len(fatal)}")
        for diagnostic in diagnostics:
            if fingerprint(identity(diagnostic)) in new or diagnostic in fatal:
                print(f"{diagnostic.path}:{diagnostic.line}:{diagnostic.column}: {diagnostic.tool} {diagnostic.code}: {diagnostic.message}")
        if args.mode == "baseline" and fatal:
            print("Syntax defects cannot be accepted into a baseline.")
        if args.mode == "baseline" and not fatal:
            commit = execute(["git", "rev-parse", "HEAD"]).stdout.strip()
            baseline = {"schema": 1, "source_commit": commit, "reason": args.reason,
                        "config_digest": config_digest(), "environment": details,
                        "diagnostics": [current[k] for k in sorted(current)]}
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            require_unchanged_sources(sources, sources_digest)
            write_json(baseline_path, baseline)
            print("Wrote explicit reviewed baseline:", baseline_path.relative_to(ROOT))
        if args.mode == "prune":
            assert old is not None
            old["diagnostics"] = [x | {"count": min(x["count"], current[x["fingerprint"]]["count"])}
                                  for x in old["diagnostics"] if x["fingerprint"] in current]
            old["last_prune_reason"] = args.reason
            require_unchanged_sources(sources, sources_digest)
            write_json(baseline_path, old)
            print("Pruned resolved entries only; new issues were not added.")
        if resolved and args.mode == "check":
            print("Use explicit local prune --reason after reviewing resolved entries; CI never rewrites debt.")
        write_json(output / "report.json", summary)
        return exit_code
    except (AnalysisError, OSError) as exc:
        print(f"Static analysis execution/configuration failed: {exc}", file=sys.stderr)
        try:
            if output_prepared:
                write_json(output / "failure.json", {"producer": PRODUCER, "run_id": run_id,
                                                     "exit_code": 2, "error": str(exc)})
        except OSError:
            pass
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
