#!/usr/bin/env python3
"""Build a PyInstaller validation bundle; native redistribution audit is separate."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PIN = "6.22.3"


def _historical_validation(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "build/dependency_manager/pyinstaller-validation", help="Fresh internal build directory")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-build", action="store_true")
    args = parser.parse_args(argv)
    output = args.output.absolute()
    command = [sys.executable, "-m", "PyInstaller", "--clean", "--distpath",
               str(output / "dist"), "--workpath", str(output / "work"),
               str(ROOT / "tools/dependency_manager/pyinstaller.spec")]
    if args.dry_run:
        print(json.dumps({"argv": command, "packager": "PyInstaller", "version": PIN,
                          "redistribution_verified": False, "layout": "onedir",
                          "project_python_included": False}))
        return 0
    if not args.test_build:
        print("Release blocked: new PyInstaller native payload needs its own audit and acceptance.", file=sys.stderr)
        return 2
    if output.exists() or output.is_symlink():
        print("Refusing to overwrite an existing output directory.", file=sys.stderr)
        return 2
    if importlib.metadata.version("pyinstaller") != PIN:
        raise RuntimeError("Use the pinned isolated PyInstaller build environment")
    output.mkdir(parents=True, exist_ok=False)
    subprocess.run(command, cwd=ROOT, check=True)
    executable = output / "dist/deposition-dependency-manager" / (
        "deposition-deps.exe" if sys.platform == "win32" else "deposition-deps")
    run = subprocess.run([str(executable), "doctor", "--json"], capture_output=True,
                         text=True, timeout=30, check=True)
    doctor = json.loads(run.stdout)
    if doctor.get("status") != "success" or not doctor.get("frozen"):
        raise RuntimeError("Independent runtime smoke failed")
    (output / "validation.json").write_text(json.dumps({"doctor": doctor,
        "redistribution_verified": False, "packager": "PyInstaller", "version": PIN},
        indent=2), encoding="utf-8")
    print(executable.parent)
    return 0


def main(argv=None):
    print("EXE packaging retired by project policy; use the source launcher.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
