"""Set up a private temporary root and Qt isolation before focused UI-test imports."""
from __future__ import annotations

import importlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
_stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
QA_ROOT = Path(os.environ.get(
    "COAT_PROCESS_QA_DIR",
    ROOT / "build" / f"qa_ui_process_{_stamp}_{uuid4().hex[:8]}"))
QA_ROOT = QA_ROOT.resolve()
QA_ROOT.mkdir(parents=True, exist_ok=True)
TEMP_ROOT = QA_ROOT / "test_temp"
TEMP_ROOT.mkdir(parents=True, exist_ok=True)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
# Keep the file dialog and Qt test temporaries inside the workspace.
for _variable in ("TEMP", "TMP", "TMPDIR"):
    os.environ[_variable] = str(TEMP_ROOT)
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

# Importing the guard after TMP is set makes its settings TemporaryDirectory
# workspace-owned; it installs the fail-closed QSettings behavior before app
# modules or QGuiApplication are imported.
qt_test_guard = importlib.import_module("tests.qt_test_guard")
