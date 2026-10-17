"""Record deliverable and read-only dependency hashes at the handoff time."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QA = ROOT / "artifacts" / "film_design_generator_validation"
OWNED = (
    "coat_sim/core/film_design_templates.py",
    "coat_sim/core/film_design_session.py",
    "coat_sim/ui/film_quick_controller.py",
    "coat_sim/ui/qml/FilmDesignQuick.qml",
    "coat_sim/ui/qml/FilmDesignGeneratorPanel.qml",
    "coat_sim/ui/qml/FilmDesignExpressionPanel.qml",
    "coat_sim/ui/qml/FilmDesignLayerRow.qml",
    "tests/test_film_design_templates.py",
    "tests/test_film_design_generator_session.py",
    "tests/test_film_design_generator_qt.py",
    "tests/test_film_quick_controller.py",
    "docs/film-design-generator-contract.md",
    "docs/film-design-generator-guide.md",
    "docs/film-design-quick-migration.md",
    "docs/film-design-generator-stage-report-20261011.md",
    "docs/algorithms.html",
    "README.md",
    "docs/development/handoffs/film-design-generator-ownership-20261011.md",
    "tools/verify_film_design_generator.py",
    "tools/verify_film_design_generator_docs.py",
    "tools/verify_film_design_generator_bridge.py",
    "tools/verify_film_design_generator_qt.py",
    "tools/verify_film_design_generator_qt_regressions.py",
    "tools/verify_film_design_generator_storage_regressions.py",
    "tools/record_film_design_generator_manifest.py",
)
DEPENDENCIES = (
    "coat_sim/ui/qml/AppWindow.qml",
    "coat_sim/ui/qml/WindowChrome.qml",
    "coat_sim/ui/qml/theme_controls/ThemeSidebar.qml",
    "coat_sim/ui/qml/theme_controls/ThemeButton.qml",
    "coat_sim/ui/qml/theme_controls/ThemeField.qml",
    "coat_sim/ui/qml/theme_controls/ThemeComboBox.qml",
    "coat_sim/ui/film_simulation_quick_controller.py",
    "coat_sim/ui/film_optimization_quick_controller.py",
    "coat_sim/core/film_design_service.py",
    "coat_sim/core/film_design.py",
    "coat_sim/core/film_optics.py",
    "docs/window-status-sidebar-contract.md",
    "docs/quick-theme-controls.md",
    "docs/window-geometry-policy.md",
)


def digest(path):
    raw = path.read_bytes()
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def main():
    files = {relative: digest(ROOT / relative) for relative in OWNED}
    dependencies = {relative: digest(ROOT / relative) for relative in DEPENDENCIES}
    evidence = {str(path.relative_to(ROOT)).replace("\\", "/"): digest(path)
                for path in sorted(QA.iterdir())
                if path.is_file() and path.name != "manifest.json"}
    report = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Current workspace snapshot for this finite generator handoff. "
                 "Shared dependencies are read-only references, not exclusive ownership; "
                 "individual reports retain their actual verification timestamps.",
        "owned_or_shared_document_files": files,
        "read_only_dependency_snapshot": dependencies,
        "evidence_files_including_preserved_failure_logs": evidence,
        "commit_push_publish_performed": False,
    }
    (QA / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"manifest": str(QA / "manifest.json"),
                      "files": len(files), "dependencies": len(dependencies),
                      "evidence": len(evidence)}))


if __name__ == "__main__":
    main()
