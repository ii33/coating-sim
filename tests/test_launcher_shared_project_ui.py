"""The lightweight launcher treats recent entries as file-open intent only."""
from __future__ import annotations

from pathlib import Path
import tempfile

import pytest
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from tests import test_launcher_project_manager_ui as launcher_ui


pytestmark = [pytest.mark.qt, pytest.mark.contract]


@pytest.fixture
def recent_ui():
    harness = launcher_ui.LauncherProjectManagerUiTests
    harness.setUpClass()
    case = harness(methodName="runTest")
    try:
        case.setUp()
        live = str(Path(tempfile.gettempdir()) / "original-study.coatproj")
        missing = str(Path(tempfile.gettempdir()) / "moved-study.coatproj")
        case.projects.set_entries([
            {"name": "Original study", "descriptorFile": live,
             "lastOpened": "2026-10-14T12:00:00+00:00", "available": True, "active": False},
            {"name": "Moved study", "descriptorFile": missing,
             "lastOpened": "2026-10-13T12:00:00+00:00", "available": False, "active": False},
        ])
        QTest.qWait(50)
        yield case, live, missing
    finally:
        case.tearDown()
        harness.tearDownClass()


def test_recent_open_uses_exact_descriptor_and_missing_entry_can_be_removed(recent_ui):
    case, live, missing = recent_ui
    case._click("launcherOpenRecentProject_0")
    assert case.projects.calls == [("open", live)]
    case.projects.finish()

    case._click("launcherOpenRecentProject_1")
    error = case._item("launcherProjectValidationError")
    assert error.isVisible()
    assert missing in error.property("text")
    assert case.projects.calls == [("open", live)]

    case._click("launcherRemoveRecentProject_1")
    assert case.projects.calls == [("open", live)]
    case._click("launcherConfirmRemoveProject")
    assert case.projects.calls[-1] == ("remove", missing)
    assert [row["descriptorFile"] for row in case.projects.projectEntries] == [live]


def test_repeated_open_keeps_the_same_descriptor_intent(recent_ui):
    case, live, _missing = recent_ui
    row = case.projects.projectEntries[0]
    assert set(row) == {"name", "descriptorFile", "lastOpened", "available", "active"}
    case._click("launcherOpenRecentProject_0")
    case.projects.finish(entry=row)
    case._click("launcherOpenRecentProject_0")
    assert case.projects.calls == [("open", live), ("open", live)]
    assert all(call == ("open", row["descriptorFile"]) for call in case.projects.calls)


def test_recent_launcher_exposes_no_owner_activation_or_close_controls(recent_ui):
    case, _live, _missing = recent_ui
    assert not hasattr(case.projects, "host")
    assert not hasattr(case.projects, "activateProject")
    assert not hasattr(case.projects, "activateSharedProject")
    assert case._item("launcherOpenRecentProject_0").isVisible()
    assert case.window.findChild(QQuickItem,
                                 "launcherActivateProject_0") is None
    assert case.window.findChild(QQuickItem,
                                 "launcherCloseProjectConfirm") is None
