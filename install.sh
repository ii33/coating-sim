#!/bin/sh
# Standard-library terminal installer. No pip install is needed for this script.
set -u
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 2
for PYTHON in "$ROOT/.runtime/python/bin/python3" "$ROOT/.runtime/python/bin/python" python3 python; do
    if command -v "$PYTHON" >/dev/null 2>&1 && "$PYTHON" -c 'import sys,venv,ensurepip; assert sys.implementation.name == "cpython" and (3,10) <= sys.version_info[:2] <= (3,13)' >/dev/null 2>&1; then
        exec "$PYTHON" "$ROOT/dependency_manager.py" "$@"
    fi
done
printf '%s\n' 'No compatible full CPython 3.10-3.13 was found (venv/pip required).' 'Install an official Python for your platform, then rerun install.sh.' 'https://www.python.org/downloads/' 'No system packages or security settings were changed.' >&2
exit 2
