"""Selective transfer facade cancellation, draft isolation and Qt lifetime."""
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtTest import QTest

from coat_sim.core.project_resource_service import ProjectResourceService
from coat_sim.core.project_store import ProjectStore
from coat_sim.project_package import export_project
from coat_sim.ui.project_resource_adapter import ProjectResourceTransferController
from tests.test_input_snapshot import fixture
from tests.test_project_resource_adapter import project as project

pytestmark = [pytest.mark.contract, pytest.mark.qt]


def wait_transfer(controller):
    for _ in range(1200):
        if not controller.transferBusy:
            return
        QTest.qWait(10)
    pytest.fail("Resource transfer did not finish: " + str(controller.transferStatus))


@pytest.mark.parametrize("container", ["coatproj", "cproject"])
def test_selective_import_cancel_and_apply_preserve_active_dirty_draft(project, tmp_path, container):
    _app, stores, _host, _film, resources, view = project
    _window, controller = view("source")
    controller.bridge.setMetadata("description", "active unsaved source")
    draft = deepcopy(controller.document_data())
    assert controller.dirty and not controller.currentDocumentId

    external = tmp_path / "external"
    fixture(external)
    donor = ProjectStore.create(tmp_path / "donor", "Donor")
    process_id = donor.import_document(external / "recipe.cproc")
    donor.create_document("source", {"schema_version": 2, "kind": "point",
        "device_type": "custom", "source_exponent": 4., "description": "unselected unrelated source"})
    donor_path = donor.project_file.with_name("arbitrary-name.coatproj")
    donor.project_file.rename(donor_path)
    if container == "cproject":
        descriptor = donor_path
        donor_path = tmp_path / "read-only-source.cproject"
        export_project(ProjectStore.open(descriptor), donor_path)
    source_bytes = donor_path.read_bytes()
    donor_before = {str(p.relative_to(donor.root)): p.read_bytes()
                    for p in donor.root.rglob("*") if p.is_file()}
    target_before = deepcopy(stores["First"].manifest)

    assert controller.beginTransfer("import")
    wait_transfer(controller)
    assert controller.inspectTransferSource(QUrl.fromLocalFile(str(donor_path)).toString())
    wait_transfer(controller)
    rows = controller.transferCatalog["rows"]
    assert {row["kind"] for row in rows} >= {"process", "machine", "source", "substrate", "mask"}
    assert controller.previewTransfer([process_id], "project")
    wait_transfer(controller)
    assert controller.transferPlan["dependency_ids"]
    controller.cancelTransfer()
    assert not controller.transferActive
    assert controller.transferPlan == {} and controller.transferCatalog == {}
    assert stores["First"].manifest == target_before
    assert controller.document_data() == draft and controller.dirty
    assert not controller.currentDocumentId

    assert controller.beginTransfer("import")
    wait_transfer(controller)
    assert controller.inspectTransferSource(str(donor_path))
    wait_transfer(controller)
    assert controller.previewTransfer([process_id], "project")
    wait_transfer(controller)
    assert controller.applyTransfer({})
    wait_transfer(controller)
    assert not controller.transferActive
    assert resources.service.list_documents("process")
    assert controller.document_data() == draft and controller.dirty
    assert not controller.currentDocumentId
    assert {str(p.relative_to(donor.root)): p.read_bytes()
            for p in donor.root.rglob("*") if p.is_file()} == donor_before
    assert donor_path.read_bytes() == source_bytes
    reopened = ProjectResourceService(ProjectStore.open(stores["First"].project_file))
    assert len(reopened.list_documents("source")) == 1
    assert stores["First"].manifest["default_configuration_id"] is None
    assert not controller.applyTransfer({})


def test_transfer_plan_invalidation_and_failures_keep_target_unchanged(tmp_path):
    fixture(tmp_path / "source")
    donor = ProjectStore.create(tmp_path / "donor", "Donor")
    identity = donor.import_document(tmp_path / "source" / "parts" / "source.csrc")
    target = ProjectStore.create(tmp_path / "target", "Target")
    controller = ProjectResourceTransferController()
    controller.service = ProjectResourceService(target)
    before = deepcopy(target.manifest)
    assert controller.beginTransfer("import")
    wait_transfer(controller)
    assert controller.inspectTransferSource(str(donor.project_file))
    wait_transfer(controller)
    assert controller.previewTransfer([identity], "project")
    wait_transfer(controller)
    controller.clearTransferPreview()
    assert controller.transferPlan == {}
    assert not controller.applyTransfer({})
    assert target.manifest == before
    assert not controller.previewTransfer([identity], "global")
    assert target.manifest == before
    assert not controller.inspectTransferSource("https://example.com/private.coatproj")
    assert controller.transferCatalog == {}
    assert "本地" in str(controller.transferStatus)
    controller.cancelTransfer()
    assert target.manifest == before


def test_all_six_real_resource_windows_create_and_destroy_in_subprocess():
    """A bad inherited Qt notifier can segfault before Python can assert it."""
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    command = [sys.executable, "-X", "faulthandler", "-m", "pytest", "-q",
               "tests/test_project_resource_adapter.py",
               "-k", "real_qml_resource_window_toolbar", "--disable-warnings"]
    result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True,
                            timeout=120, check=False)
    evidence = root / "artifacts" / "project-resource-toolbar-review"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "six-window-lifecycle.stdout").write_text(result.stdout, encoding="utf-8")
    (evidence / "six-window-lifecycle.stderr").write_text(result.stderr, encoding="utf-8")
    (evidence / "six-window-lifecycle.exit-code.txt").write_text(str(result.returncode), encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "6 passed" in result.stdout


def test_cancelled_catalog_is_nonblocking_and_late_result_is_released():
    from threading import Event
    from PySide6.QtCore import QTimer

    entered, release = Event(), Event()

    class SlowService:
        released = []

        def inspect_resources(self, _source):
            entered.set()
            assert release.wait(5)
            return {"catalog_id": "late-catalog", "rows": [], "categories": []}

        def release_resource_catalog(self, identity):
            self.released.append(identity)

    controller = ProjectResourceTransferController()
    service = SlowService()
    controller.service = service
    heartbeat = []
    QTimer.singleShot(10, lambda: heartbeat.append(True))
    assert controller.beginTransfer("import")
    assert controller.transferBusy and controller.transferCancellable
    QTest.qWait(40)
    assert entered.is_set() and heartbeat  # UI loop remains live during I/O.
    controller.cancelTransfer()
    assert not controller.transferActive and not controller.transferBusy
    assert controller.transferCatalog == {}
    release.set()
    for _ in range(500):
        if not controller._transfer_tasks:
            break
        QTest.qWait(10)
    assert not controller._transfer_tasks
    assert service.released == ["late-catalog"]
    assert controller.transferCatalog == {} and controller.transferPlan == {}


def test_started_apply_cannot_claim_to_cancel_and_reports_its_actual_result():
    from threading import Event

    entered, release = Event(), Event()

    class SlowApplyService:
        committed = False

        def inspect_resources(self, _source):
            return {"catalog_id": "catalog", "rows": [{"id": "one"}], "categories": []}

        def preview_resource_transfer(self, _catalog, _selected, _target):
            return {"plan_id": "plan", "rows": [], "selected_ids": ["one"], "dependency_ids": []}

        def apply_resource_transfer(self, _plan, _decisions):
            entered.set()
            assert release.wait(5)
            self.committed = True
            return {"created": ["one"]}

        def release_resource_catalog(self, _identity):
            pass

    controller = ProjectResourceTransferController()
    service = SlowApplyService()
    controller.service = service
    finished: list[bool] = []
    controller.transferFinished.connect(finished.append)
    assert controller.beginTransfer("import")
    wait_transfer(controller)
    assert controller.previewTransfer(["one"], "project")
    wait_transfer(controller)
    assert controller.applyTransfer({})
    QTest.qWait(30)
    assert entered.is_set() and controller.transferBusy
    assert not controller.transferCancellable
    assert not controller.applyTransfer({})
    controller.cancelTransfer()
    assert controller.transferActive and controller.transferBusy
    assert not service.committed
    assert "无法取消" in str(controller.transferStatus)
    release.set()
    wait_transfer(controller)
    assert service.committed and finished == [True]
    assert not controller.transferActive and not controller.transferBusy


def test_transfer_terminal_signal_matches_request_and_never_delivers_stale_read():
    from threading import Event

    entered, release = Event(), Event()

    class TaggedService:
        def __init__(self):
            self.released = []

        def inspect_resources(self, source):
            if source == "global":
                entered.set()
                assert release.wait(5)
                return {"catalog_id": "stale", "rows": [], "categories": []}
            if source == "broken":
                raise ValueError("deterministic read failure")
            return {"catalog_id": "current", "rows": [], "categories": []}

        def release_resource_catalog(self, identity):
            self.released.append(identity)

    controller = ProjectResourceTransferController()
    service = TaggedService()
    controller.service = service
    completed = []
    controller.transferOperationFinished.connect(
        lambda identity, kind, success, error: completed.append((identity, kind, success, error)))
    try:
        assert controller.beginTransfer("import")
        stale_id = controller.transferOperationId
        QTest.qWait(30)
        assert entered.is_set()
        controller.cancelTransfer()
        assert controller.beginTransfer("export")
        current_id = controller.transferOperationId
        assert current_id != stale_id
        wait_transfer(controller)
        assert completed == [(current_id, "inspect", True, "")]
        catalog = controller.property("transferCatalog")
        assert isinstance(catalog, dict) and catalog["catalog_id"] == "current"
        release.set()
        for _ in range(500):
            if not controller._transfer_tasks:
                break
            QTest.qWait(10)
        assert not controller._transfer_tasks and service.released == ["stale"]
        assert completed == [(current_id, "inspect", True, "")]
        assert controller.inspectTransferSource("broken")
        failed_id = controller.transferOperationId
        wait_transfer(controller)
        assert completed[-1] == (failed_id, "inspect", False, "deterministic read failure")
        assert failed_id != current_id and len(completed) == 2
        assert controller.transferCatalog == {}
    finally:
        release.set()
        controller.cancelTransfer()
        controller.deleteLater()
