"""Cross-launcher navigation preserves the existing writer and its drafts."""
from __future__ import annotations

from copy import deepcopy
from threading import Event, get_ident
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QGuiApplication

from coat_sim.core.project_store import ProjectError, ProjectStore
from coat_sim.project_recovery import ProjectOwnerLease
from coat_sim.ui.control import UiControlError
from coat_sim.ui.project_runtime_owner import ProjectRuntimeOwner


pytestmark = [pytest.mark.contract, pytest.mark.qt]


def _wait(predicate, timeout=5):
    end = time.monotonic() + timeout
    while not predicate() and time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.005)
    assert predicate()
    QCoreApplication.processEvents()


class SharedClient:
    def __init__(self, store):
        self.main_thread = get_ident()
        self.rows = [{"client_id": "original-client", "generation": "original-client-generation",
                      "state": "alive", "project_sessions": [
                          {"project_id": store.manifest["id"], "generation": "original-session",
                           "root": str(store.root), "project_file": str(store.project_file), "name": "原工程"}]}]
        self.calls = []
        self.discovery_error = False
        self.activation_error = None
        self.started = Event()
        self.release = None

    def list_clients(self):
        assert get_ident() != self.main_thread, "discovery must not block the GUI callback thread"
        if self.discovery_error:
            raise UiControlError("ui_unavailable", "disconnected")
        return deepcopy(self.rows)

    def request(self, operation, **arguments):
        assert get_ident() != self.main_thread, "activation must not block owner status callbacks"
        assert operation == "activate_project"
        self.calls.append(dict(arguments))
        self.started.set()
        if self.release is not None:
            assert self.release.wait(5)
        if self.activation_error is not None:
            raise self.activation_error
        return {"activated": True, "instance_id": "original-window",
                **{key: arguments[key] for key in ("project_id", "session_generation", "client_generation")}}


@pytest.fixture
def shared_picker(tmp_path, monkeypatch):
    app = QGuiApplication.instance() or QGuiApplication([])
    monkeypatch.setattr("coat_sim.project_recovery.project_owner_runtime_root", lambda: tmp_path / "owners")
    store = ProjectStore.create(tmp_path / "Study", "Study")
    lease = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"])
    lease.checkpoint({"film": {"draft": "keep the original draft"}})
    adapter = ProjectRuntimeOwner(default_loader=lambda _: SimpleNamespace(state="empty_project"))
    client = SharedClient(store)
    adapter.connect_shared_projects(client, "new-launcher-generation")
    _wait(lambda: bool(adapter.projectEntries))
    yield adapter, client, store, lease
    adapter.stop()
    lease.close(clean=False)
    adapter.deleteLater()
    app.processEvents()


def test_list_and_open_reuse_remote_owner_without_registering_or_leasing(shared_picker, monkeypatch):
    adapter, client, store, _lease = shared_picker
    before = {p.name: p.read_bytes() for p in store.root.iterdir() if p.is_file()}
    def forbidden(*_args, **_kwargs):
        pytest.fail("remote activation must not construct a local writer or window")
    monkeypatch.setattr(ProjectOwnerLease, "acquire", forbidden)
    monkeypatch.setattr(adapter.desktop, "open_project", forbidden)
    assert adapter.projectEntries[0]["remote"] is True
    assert adapter.host.sessions() == () and adapter._leases == {}
    assert adapter.openProject(str(store.project_file))
    _wait(lambda: not adapter.projectBusy)
    assert adapter.projectError == ""
    assert adapter.selectedProjectId == store.manifest["id"]
    assert len(client.calls) == 1
    assert adapter.host.sessions() == () and adapter._leases == {}
    assert before == {p.name: p.read_bytes() for p in store.root.iterdir() if p.is_file()}


def test_remote_entry_activation_fences_generation_and_preserves_original_lease(shared_picker):
    adapter, client, store, _lease = shared_picker
    project_id = store.manifest["id"]
    assert not adapter.activateSharedProject(project_id, "stale-session")
    assert not client.calls and "更新" in adapter.projectError
    assert adapter.activateSharedProject(project_id, "original-session")
    _wait(lambda: not adapter.projectBusy)
    assert adapter.projectError == "" and len(client.calls) == 1
    adapter.stop()
    with pytest.raises(ProjectError, match="active writable owner"):
        ProjectOwnerLease.acquire(store.project_file, project_id)


def test_discovery_outage_keeps_known_owner_visible_and_unavailable(shared_picker):
    adapter, client, store, _lease = shared_picker
    client.discovery_error = True
    adapter.refreshProjects()
    _wait(lambda: not adapter.projectEntries[0]["available"])
    assert adapter.projectEntries[0]["projectId"] == store.manifest["id"]
    assert not adapter.activateSharedProject(store.manifest["id"], "original-session")
    assert "无法联系" in adapter.projectError and not client.calls
    assert adapter.host.sessions() == ()


@pytest.mark.parametrize("error", [
    UiControlError("invalid_operation", "unsupported"),
    UiControlError("ui_unavailable", "disconnected"),
])
def test_failed_activation_is_chinese_and_does_not_publish_remote_session(shared_picker, error):
    adapter, client, store, _lease = shared_picker
    client.activation_error = error
    assert adapter.openProject(str(store.project_file))
    _wait(lambda: not adapter.projectBusy)
    assert any("\u4e00" <= c <= "\u9fff" for c in adapter.projectError)
    assert adapter.selectedProjectId == ""
    assert adapter.host.sessions() == () and not adapter._leases


def test_stopped_adapter_does_not_publish_late_remote_activation(shared_picker):
    adapter, client, store, _lease = shared_picker
    client.release = Event()
    assert adapter.openProject(str(store.project_file))
    _wait(client.started.is_set)
    pending = adapter._pending
    adapter.stop()
    client.release.set()
    _wait(pending.done)
    assert adapter.selectedProjectId == ""
    assert adapter.host.sessions() == () and not adapter._leases


def test_unknown_held_owner_has_actionable_error_without_clearing_lock(shared_picker):
    adapter, client, store, _lease = shared_picker
    client.rows = []
    assert adapter.openProject(str(store.project_file))
    _wait(lambda: not adapter.projectBusy)
    assert "已由其他会话持有" in adapter.projectError
    assert "active writable owner" not in adapter.projectError
    assert not client.calls and not adapter._leases
    with pytest.raises(ProjectError, match="active writable owner"):
        ProjectOwnerLease.acquire(store.project_file, store.manifest["id"])
