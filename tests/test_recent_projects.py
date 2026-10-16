from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QSettings

from coat_sim.ui.recent_projects import RecentProjects


@pytest.fixture(scope="module", autouse=True)
def _qt_app():
    QCoreApplication.instance() or QCoreApplication([])


def _settings(path: Path) -> QSettings:
    value = QSettings(str(path), QSettings.Format.IniFormat)
    value.clear()
    value.sync()
    return value


def _existing_settings(path: Path) -> QSettings:
    value = QSettings(str(path), QSettings.Format.IniFormat)
    value.sync()
    return value


def _stored(settings: QSettings):
    settings.sync()
    settings.beginGroup("recentProjects/v1")
    try:
        return settings.value("entries", [])
    finally:
        settings.endGroup()


def test_record_persists_only_three_fields_and_deduplicates_canonical_path(tmp_path):
    store = tmp_path / "settings.ini"
    descriptor = tmp_path / "Study" / "plan.coatproj"
    descriptor.parent.mkdir()
    descriptor.write_text('{"project":"fixture"}', encoding="utf-8")
    history = RecentProjects(_settings(store))

    history.record(descriptor, "First name")
    history.record(descriptor.parent / "." / descriptor.name, "Updated name")

    saved = _stored(_existing_settings(store))
    assert len(saved) == 1
    assert set(saved[0]) == {"descriptorFile", "name", "lastOpened"}
    assert saved[0]["descriptorFile"] == str(descriptor.resolve())
    assert saved[0]["name"] == "Updated name"
    assert saved[0]["lastOpened"]
    assert history.entries() == [{
        "descriptorFile": str(descriptor.resolve()), "name": "Updated name",
        "lastOpened": saved[0]["lastOpened"], "available": True,
    }]


def test_history_is_shared_across_instances_and_refreshes_settings(tmp_path):
    store = tmp_path / "settings.ini"
    first = tmp_path / "first.coatproj"
    second = tmp_path / "second.coatproj"
    first.touch()
    second.touch()

    one = RecentProjects(_settings(store))
    two = RecentProjects(_settings(store))
    one.record(first, "First")
    two.record(second, "Second")
    one.refresh()

    assert [row["descriptorFile"] for row in one.entries()] == [
        str(second.resolve()), str(first.resolve())]


def test_missing_descriptor_stays_visible_as_unavailable_and_can_be_removed(tmp_path):
    settings_path = tmp_path / "settings.ini"
    descriptor = tmp_path / "gone.coatproj"
    descriptor.write_text("preserve me", encoding="utf-8")
    history = RecentProjects(_settings(settings_path))
    history.record(descriptor, "Gone")
    descriptor.unlink()

    rows = history.entries()
    assert len(rows) == 1
    assert rows[0]["descriptorFile"] == str(descriptor.resolve())
    assert rows[0]["available"] is False

    history.remove(descriptor)
    assert history.entries() == []


def test_remove_only_changes_history_and_leaves_project_untouched(tmp_path):
    settings_path = tmp_path / "settings.ini"
    descriptor = tmp_path / "project.coatproj"
    contents = '{"name":"keep"}'
    descriptor.write_text(contents, encoding="utf-8")
    history = RecentProjects(_settings(settings_path))
    history.record(descriptor, "Keep")

    history.remove(descriptor)

    assert history.entries() == []
    assert descriptor.read_text(encoding="utf-8") == contents


def test_record_rejects_non_descriptor_paths(tmp_path):
    history = RecentProjects(_settings(tmp_path / "settings.ini"))
    with pytest.raises(ValueError, match="coatproj"):
        history.record(tmp_path / "folder", "Not a project")

