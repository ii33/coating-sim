"""Install bounded diagnostics in a detached child before importing its entry."""
from __future__ import annotations

import argparse
import runpy
import sys
import traceback

from coat_sim.project_logging import CHANNELS, install_process_log


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", required=True, choices=sorted(CHANNELS))
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--stderr-only", action="store_true")
    parser.add_argument("--module", required=True, choices=(
        "coat_sim.assistant_process", "coat_sim.agent.worker", "coat_sim.ui.project_runtime"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    install_process_log(args.channel, args.project_root, stderr_only=args.stderr_only)
    from coat_sim.assistant_diagnostics import trace
    trace("child_process_start", role=args.channel)
    arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    sys.argv = [args.module, *arguments]
    try:
        runpy.run_module(args.module, run_name="__main__", alter_sys=True)
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
