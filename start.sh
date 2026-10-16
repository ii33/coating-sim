#!/bin/sh
# Cross-platform desktop wrapper; cloud desktops can use start-cloud.sh instead.
COAT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 2
if [ -n "${COAT_PYTHON:-}" ]; then
    : # Explicit caller override wins; it is a path/command, never shell code.
elif [ -x "$COAT_ROOT/.venv/bin/python" ]; then
    COAT_PYTHON="$COAT_ROOT/.venv/bin/python"
elif [ -x "$COAT_ROOT/.conda/bin/python" ]; then
    COAT_PYTHON="$COAT_ROOT/.conda/bin/python"
else
    COAT_PYTHON=python3
fi
if ! command -v "$COAT_PYTHON" >/dev/null 2>&1; then
    printf '%s\n' '找不到 Python；请按 README 创建项目 .venv，或设置 COAT_PYTHON。' >&2
    exit 2
fi
exec "$COAT_PYTHON" "$COAT_ROOT/launcher.pyw" "$@"
