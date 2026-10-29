"""Set up a private temporary root and Qt isolation before focused UI-test imports."""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
_stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
if "COAT_PROCESS_QA_DIR" in os.environ:
    # Preserve the explicit override for callers that need a known artifact path.
    QA_ROOT = Path(os.environ["COAT_PROCESS_QA_DIR"])
else:
    # Settings isolation uses TMPDIR below; keep that root outside the source
    # tree so defaults derived from XDG_DATA_HOME cannot become install-local.
    QA_ROOT = Path(tempfile.mkdtemp(
        prefix=f"coat-sim-qa-ui-process-{_stamp}_{uuid4().hex[:8]}-"))
QA_ROOT = QA_ROOT.resolve()
QA_ROOT.mkdir(parents=True, exist_ok=True)
TEMP_ROOT = QA_ROOT / "test_temp"
TEMP_ROOT.mkdir(parents=True, exist_ok=True)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
# Keep file-dialog and Qt test temporaries inside this private QA root.
for _variable in ("TEMP", "TMP", "TMPDIR"):
    os.environ[_variable] = str(TEMP_ROOT)
# mkdtemp() above may have cached the system temp path; make the new private
# root effective for subsequent TemporaryDirectory() calls too.
tempfile.tempdir = None
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

# Importing the guard after TMP is set makes its settings TemporaryDirectory
# QA-root-owned; it installs fail-closed QSettings behavior before app modules
# or QGuiApplication are imported.
qt_test_guard = importlib.import_module("tests.qt_test_guard")
