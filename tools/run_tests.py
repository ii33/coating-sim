#!/usr/bin/env python3
"""Run the repository's pytest tiers with consistent headless/offline defaults."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
TIERS = ("quick", "algorithm", "contract", "qt", "all")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tier", choices=TIERS, help="test tier to collect and run")
    parser.add_argument(
        "--collect-only", action="store_true",
        help="collect tests and report nodeids without executing test bodies",
    )
    parser.add_argument(
        "--run-gpu", action="store_true",
        help="opt in to tests requiring real GPU hardware",
    )
    parser.add_argument(
        "--run-provider", action="store_true",
        help="opt in to real model-provider calls (may incur charges)",
    )
    parser.add_argument(
        "--run-remote", action="store_true",
        help="opt in to real remote-service calls",
    )
    parser.add_argument(
        "--collection-report", type=Path,
        help="write nodeid/tier/AST collection evidence to this JSON path",
    )
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if "--" in raw_args:
        separator = raw_args.index("--")
        runner_args = raw_args[:separator]
        pytest_args = raw_args[separator + 1:]
    else:
        runner_args = raw_args
        pytest_args = []
    args = parser.parse_args(runner_args)

    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["QT_QUICK_BACKEND"] = "software"
    env["QSG_RHI_BACKEND"] = "software"
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    command = [sys.executable, "-m", "pytest"]
    if args.tier != "all":
        command.extend(["-m", args.tier])
    if args.collect_only:
        command.append("--collect-only")
    if args.collection_report:
        command.extend([
            "--collection-report-json",
            str(args.collection_report),
        ])
    for option, enabled in (
        ("--run-gpu", args.run_gpu),
        ("--run-provider", args.run_provider),
        ("--run-remote", args.run_remote),
    ):
        if enabled:
            command.append(option)
    command.extend(["-q", "tests"])
    command.extend(pytest_args)
    with tempfile.TemporaryDirectory(prefix="deposition-sim-pytest-") as cache_directory:
        cache_root = Path(cache_directory)
        mpl_config = cache_root / "matplotlib"
        xdg_cache = cache_root / "xdg"
        mpl_config.mkdir()
        xdg_cache.mkdir()
        env["MPLCONFIGDIR"] = str(mpl_config)
        env["XDG_CACHE_HOME"] = str(xdg_cache)
        print("Running:", " ".join(command), flush=True)
        completed = subprocess.run(command, cwd=ROOT, env=env, check=False)
        if completed.returncode < 0:
            signal_number = -completed.returncode
            try:
                signal_name = signal.Signals(signal_number).name
            except ValueError:
                signal_name = f"signal {signal_number}"
            print(
                f"pytest terminated by {signal_name} (signal {signal_number}); "
                f"mapping to shell status {128 + signal_number}",
                file=sys.stderr,
            )
            return 128 + signal_number
        return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
