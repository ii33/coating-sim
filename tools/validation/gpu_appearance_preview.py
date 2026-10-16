#!/usr/bin/env python3
"""Compatibility entrypoint for the canonical production CUDA appearance renderer.

The validated full-image implementation lives in coat_sim.appearance.gpu. This
alias keeps research imports (including monkeypatch-based regression tests) on
that one implementation rather than allowing two optics kernels to diverge.
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from coat_sim.appearance import gpu as _gpu

if __name__ == "__main__":
    raise SystemExit(_gpu.main())
sys.modules[__name__] = _gpu
