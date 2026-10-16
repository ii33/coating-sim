"""Command line entry point for a self-contained appearance job JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .render import render_job


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Render a wavelength-sampled thin-film appearance frame")
    parser.add_argument("job", type=Path, help="self-contained appearance job JSON")
    parser.add_argument("output", type=Path, help="directory for float spectra, XYZ, sRGB, and manifest")
    parser.add_argument("--display-exposure", type=float, default=0.01,
                        help="display-only exposure before the tone map; saved linear data is unchanged")
    parser.add_argument("--workers", type=int, default=1,
                        help="independent wavelength CPU processes (1-64); default 1")
    parser.add_argument("--progress", action="store_true", help="write progress and time estimates as JSON lines to stderr")
    args = parser.parse_args(argv)
    with args.job.open("r", encoding="utf-8") as stream:
        job = json.load(stream)
    options = {"display_exposure": args.display_exposure, "workers": args.workers}
    if args.progress:
        options["progress_callback"] = lambda event: print(json.dumps(event), file=sys.stderr, flush=True)
    result = render_job(job, args.output, **options)
    result["radiance_spectrum"]._mmap.close()
    print(json.dumps({"output_dir": str(args.output.resolve()),
                      "input_sha256": result["manifest"]["input_sha256"],
                      "film_rgb_equality_max_abs": result["manifest"]["film_rgb_equality_max_abs"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
