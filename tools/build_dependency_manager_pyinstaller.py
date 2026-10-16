#!/usr/bin/env python3
"""Retired installer packaging entry; no bundler is imported or executed."""
from __future__ import annotations

import sys


def _historical_validation(argv=None):
    """Compatibility entry: historical validation cannot bypass retirement."""
    return main(argv)


def main(argv=None):
    print("EXE packaging retired: use the online installer via install.bat or install.sh.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
