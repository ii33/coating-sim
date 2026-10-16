"""Run the real offscreen generator interactions with isolated test settings."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
QA = ROOT / "artifacts" / "film_design_generator_validation"
sys.path[:0] = [str(ROOT / "tests"), str(ROOT)]


def main():
    QA.mkdir(parents=True, exist_ok=True)
    temp_root = QA / "root-test-temp"
    temp_root.mkdir(exist_ok=True)
    os.environ["TEMP"] = str(temp_root)
    os.environ["TMP"] = str(temp_root)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_QUICK_BACKEND", "software")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication
    app = QGuiApplication.instance() or QGuiApplication([])
    families = []
    for path in (Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/segoeui.ttf")):
        if path.exists():
            font_id = QFontDatabase.addApplicationFont(str(path))
            if font_id >= 0:
                families.extend(QFontDatabase.applicationFontFamilies(font_id))
    if families:
        app.setFont(QFont(families[0], 10))
    suite = unittest.defaultTestLoader.loadTestsFromName("test_film_design_generator_qt")
    with (QA / "generator-qt-unittest.log").open("w", encoding="utf-8") as log:
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    passed = result.wasSuccessful() and not result.skipped and result.testsRun == 3
    report = {"status": "passed" if passed else "failed",
              "timestamp_utc": datetime.now(timezone.utc).isoformat(),
              "run": result.testsRun, "failures": len(result.failures),
              "errors": len(result.errors), "skipped": len(result.skipped),
              "loaded_font_families": families, "qt_platform": app.platformName(),
              "device_pixel_ratio": app.primaryScreen().devicePixelRatio(),
              "qt_quick_backend": os.environ.get("QT_QUICK_BACKEND")}
    (QA / "generator-qt-validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
