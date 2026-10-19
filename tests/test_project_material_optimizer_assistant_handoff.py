"""Project material -> ideal Air -> optimizer snapshot -> local compute handoff.

Integration chain under test (all on the real Qt thread, temporary data only):

1. A temporary ``ProjectStore`` owns its own material profiles and optical
   datasets, created exclusively through ``ProjectMaterialLibraryBridge`` so each
   write is a project copy-on-write and never touches the global catalog.
2. The same project's fresh film draft binds packaged ideal Air for its
   surrounding media (incident/exit), independent of the global catalog.
3. ``FilmDesignQuickController`` and ``FilmOptimizationQuickController`` are
   opened for that one ``ProjectFilmWorkspace``.
4. ``project_editor_snapshot`` for ``film_optimization`` exposes the exact
   serialized request the optimizer window would submit.
5. That exact request is handed to a small local compute task.

Named mock boundary (``_LocalSubmissionHarness``): only the *networked*
scheduler transport is replaced, using the existing protocol-shaped synthetic
transport from ``tests/test_film_compute_entrypoints.py``. Request
canonicalization/freezing, the real scheduler job-spec freezer, the durable
``SchedulerStore``, and the ``subprocess.Popen`` block are all production code.
No daemon, worker process, subprocess, or socket is started.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import subprocess
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QGuiApplication, QWindow

from coat_sim.core import material_business
from coat_sim.core.film_compute import canonical_film_parameters
from coat_sim.core.film_design_session import AIR_MATERIAL_ID, AIR_OPTICAL_SET_ID
from coat_sim.core.film_optimization_job import deserialize_request, serialize_request
from coat_sim.core.project_film_session import ProjectFilmWorkspace
from coat_sim.core.project_resource_service import ProjectResourceService
from coat_sim.core.project_store import ProjectStore
from coat_sim.project_session import ProjectSessionHost
from coat_sim.scheduler.jobs import freeze_spec
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from coat_sim.ui.project_application_client import ProjectApplicationClient
from coat_sim.ui.project_material_library import ProjectMaterialLibraryBridge
from tests.test_film_compute_entrypoints import _SyntheticSchedulerTransport
from tests.test_project_material_catalog import StaticCatalog

pytestmark = [pytest.mark.contract, pytest.mark.qt]

AIR = {"kind": "material", "material_id": AIR_MATERIAL_ID, "optical_set_id": AIR_OPTICAL_SET_ID}
# A tabulated dataset that fully covers the optimizer's default 400-700 nm target.
OPTICAL_TABLE = "400\t1.9\t0\n550\t2.0\t0\n700\t2.05\t0"


def _canonical_bytes(value) -> bytes:
    """Canonical JSON bytes; tuples and lists are intentionally equivalent here."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def _plain(value):
    """The JSON round-trip view a persisted job spec exposes (tuples become lists)."""
    return json.loads(_canonical_bytes(value))


class _MemorySettings:
    """Exchangeable settings seam so no real QSettings file is read or written."""

    def value(self, _key, default=None):
        return default

    def setValue(self, _key, _value):
        pass

    def sync(self):
        pass


class _ProjectWindow(QWindow):
    _project_film_controller: QObject


class _BroadcastBridge(QObject):
    launchError = Signal(str)
    project_launcher: SimpleNamespace
    project_lifecycle: object | None


class _LocalSubmissionHarness:
    """Mock boundary: replaces only the networked scheduler submit transport.

    Production behavior is preserved: ``canonical_film_parameters`` freezes the
    exact snapshot request against the same project-owned materials, and
    ``freeze_spec`` builds the real ``film_optimization`` job spec. The
    protocol-shaped synthetic transport then writes a durable queue job into a
    real ``SchedulerStore``. Nothing here starts a scheduler or a worker.
    """

    def __init__(self, service, transport: _SyntheticSchedulerTransport):
        self.service = service
        self.transport = transport
        self.received: dict | None = None
        self.received_bytes = b""
        self.canonical_request: dict | None = None
        self.spec: dict | None = None

    def submit(self, request: dict) -> dict:
        self.received = deepcopy(request)
        self.received_bytes = _canonical_bytes(request)
        canonical = canonical_film_parameters(self.service, {"request": deepcopy(request)})
        self.canonical_request = canonical["request"]
        self.spec = freeze_spec(self.service, "film_optimization",
                                {"request": deserialize_request(canonical["request"])})
        job_id = sha256(("local-handoff:" + self.service.project_id).encode("utf-8")).hexdigest()[:32]
        return self.transport.request("submit", {"spec": self.spec, "job_id": job_id,
                                                 "metadata": {}})


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    app = QGuiApplication.instance() or QGuiApplication([])
    provider = StaticCatalog([])
    # Deterministic, isolated catalog: the workspace service must never read the
    # real per-user global material directory during this test.
    monkeypatch.setattr(material_business, "application_material_library", lambda: provider)

    store = ProjectStore.create(tmp_path / "handoff-study", "Handoff study")
    # The host must resolve the exact same store instance it registered, so the
    # resource/film services and the bridge share one project owner.
    host = ProjectSessionHost(lambda _root: store)
    session = host.open_project(store.root)
    binding = session.binding
    workspace = ProjectFilmWorkspace.for_project(host, binding)

    windows: dict[str, QWindow] = {}
    controllers: dict[str, QObject] = {}
    bridge = ProjectMaterialLibraryBridge(
        ProjectResourceService(store, session_host=host, binding=binding),
        global_library=provider)
    client = None
    try:
        # 1) Project-owned material, written only through the project bridge.
        assert bridge.newMaterial(), bridge.property("status")
        bridge.setField("name", "Project handoff film")
        bridge.setCategory("film")
        assert bridge.addOpticalSet(json.dumps({
            "name": "Handoff visible", "notes": "", "source": "synthetic handoff fixture",
            "kind": "tabulated", "wavelength_unit": "nm",
            "wavelength_min": 400.0, "wavelength_max": 700.0, "table": OPTICAL_TABLE,
        })), bridge.property("status")
        material_id = bridge.draftData()["id"]
        assert bridge.save(), bridge.property("status")
        assert bridge.draftData()["id"] == material_id
        assert any(row["kind"] == "material_profile" for row in store.manifest["documents"])

        # A project-owned substrate makes the optimizer's frozen closure complete.
        assert bridge.newMaterial(), bridge.property("status")
        bridge.setField("name", "Project handoff substrate")
        bridge.setCategory("substrate")
        assert bridge.addOpticalSet(json.dumps({
            "name": "Handoff substrate set", "notes": "", "source": "synthetic handoff fixture",
            "kind": "tabulated", "wavelength_unit": "nm",
            "wavelength_min": 400.0, "wavelength_max": 700.0,
            "table": "400\t1.5\t0\n550\t1.5\t0\n700\t1.5\t0",
        })), bridge.property("status")
        substrate_id = bridge.draftData()["id"]
        assert bridge.save(), bridge.property("status")
        assert substrate_id != material_id

        # 2) Same project: refresh, then the fresh draft binds packaged ideal Air.
        workspace.service.refresh_materials()
        editor = workspace.editor
        assert editor.set_substrate_value("material_id", substrate_id), editor.snapshot().error
        default_design = editor.snapshot().design
        assert default_design["substrate"]["material_id"] == substrate_id
        assert default_design["substrate"]["optical_set_id"]
        assert default_design["incident_medium"] == AIR
        assert default_design["exit_medium"] == AIR

        # One film layer that references only the project-owned material.
        assert editor.add_layer("front")
        layer_id = editor.snapshot().design["front_layers"][-1]["id"]
        assert editor.set_layer_value("front", layer_id, "material_id", material_id), editor.snapshot().error
        layer = editor.snapshot().design["front_layers"][-1]
        assert layer["material_id"] == material_id
        optical_set_id = layer["optical_set_id"]
        assert optical_set_id

        # 3) Both windows for the same workspace.
        source = FilmDesignQuickController(
            service=workspace.service, edit_session=editor,
            geometry_settings=_MemorySettings(), project_context=workspace)
        optimization = FilmOptimizationQuickController(source, project_context=workspace)
        controllers["film_design"] = source
        controllers["film_optimization"] = optimization

        def mount(module: str) -> None:
            route = host.reserve_module(binding.project_id, module).route
            window = _ProjectWindow()
            window._project_film_controller = controllers[module]
            host.attach_window(route, window)
            windows[module] = window

        mount("film_design")
        mount("film_optimization")

        desktop = SimpleNamespace(controller_for=lambda _owner, module: controllers.get(module))
        owner = SimpleNamespace(host=host, desktop=desktop)
        broadcast = _BroadcastBridge()
        broadcast.project_launcher, broadcast.project_lifecycle = owner, None
        client = ProjectApplicationClient(
            broadcast, SimpleNamespace(dispatch=lambda *_args: pytest.fail("global fallback")))
        arguments = {
            "project_id": binding.project_id,
            "session_generation": binding.generation,
            "client_generation": client.generation,
            "project_root": str(store.root),
            "project_file": str(store.project_file),
            "module_key": "film_optimization",
        }

        transport = _SyntheticSchedulerTransport(tmp_path / "compute")
        harness = _LocalSubmissionHarness(workspace.service, transport)

        yield SimpleNamespace(
            app=app, store=store, host=host, workspace=workspace, editor=editor,
            material_id=material_id, substrate_id=substrate_id,
            optical_set_id=optical_set_id, layer_id=layer_id,
            source=source, optimization=optimization, client=client, arguments=arguments,
            transport=transport, harness=harness, provider=provider)
    finally:
        if client is not None:
            client.stop()
        for controller in controllers.values():
            detach = getattr(controller, "detach_observers", None)
            if callable(detach):
                detach()
        for window in windows.values():
            window.close()
        bridge.waitForBackgroundTasks()
        bridge.deleteLater()
        app.processEvents()


def test_project_material_to_optimizer_snapshot_submits_exact_local_request(handoff, monkeypatch):
    # No scheduler daemon or worker process may ever be started.
    def _forbidden_popen(*_args, **_kwargs):
        raise AssertionError("the handoff test must not spawn a scheduler or worker process")

    monkeypatch.setattr(subprocess, "Popen", _forbidden_popen)

    # 4) The snapshot exposes the exact serialized request the window would submit.
    state = handoff.client.dispatch("project_editor_snapshot", handoff.arguments)
    assert state["available"], state.get("reason")
    assert state["optimization"]["available"], state["optimization"]
    submission = state["optimization"]["submission"]
    assert submission["available"], submission
    assert submission["source"] == "current_window"
    request = submission["request"]
    assert request == serialize_request(handoff.optimization._build_request())
    assert request["design_revision"] == handoff.editor.snapshot().design_revision
    assert request["frozen_material_snapshot"] is None  # the window submits an unfrozen design

    # The request carries the project-owned material and the default Air media.
    design = request["design_snapshot"]
    (variable,) = request["variables"]
    assert variable["side"] == "front" and variable["layer_id"] == handoff.layer_id
    assert request["objectives"]
    references = {(row["material_id"], row["optical_set_id"])
                  for row in (design["substrate"], design["incident_medium"], design["exit_medium"],
                              *design["front_layers"], *design["back_layers"])}
    assert (handoff.material_id, handoff.optical_set_id) in references
    assert (handoff.substrate_id, design["substrate"]["optical_set_id"]) in references
    assert (AIR_MATERIAL_ID, AIR_OPTICAL_SET_ID) in references
    # The project copy-on-write never touched the (empty) read-only global catalog.
    assert handoff.provider.records == []

    # 5) Hand that exact request to the local compute task through the named mock boundary.
    manifest_before_submit = deepcopy(handoff.store.manifest)
    job = handoff.harness.submit(request)
    assert handoff.harness.received == request
    assert handoff.harness.received_bytes == _canonical_bytes(request)

    # Only the submit action was used: no network, no status polling, no worker.
    assert [action for action, _ in handoff.transport.calls] == ["submit"]
    assert job["kind"] == "film_optimization" and job["state"] == "queued"
    assert job["job_id"] == sha256(("local-handoff:" + handoff.workspace.service.project_id)
                                   .encode("utf-8")).hexdigest()[:32]

    # The frozen job spec preserves the snapshot request's business fields exactly.
    # ``freeze_spec`` returns a JSON-serializable spec, so tuples become lists and
    # only a canonical JSON comparison proves byte-for-byte identity.
    frozen = handoff.harness.spec["parameters"]["payload"]["request"]
    canonical = handoff.harness.canonical_request
    assert _canonical_bytes(frozen) == _canonical_bytes(canonical)
    for field in ("design_snapshot", "design_revision", "variables", "objectives",
                  "max_iterations", "max_evaluations", "solver", "seed"):
        assert frozen[field] == _plain(request[field]), field

    # Freezing only adds the immutable material closure, still owned by this project.
    snapshot = frozen["frozen_material_snapshot"]
    assert snapshot is not None
    assert len(snapshot["sha256"]) == 64
    frozen_ids = {row["id"] for row in snapshot["materials"]}
    assert {handoff.material_id, handoff.substrate_id, AIR_MATERIAL_ID} <= frozen_ids
    frozen_film = next(row for row in snapshot["materials"] if row["id"] == handoff.material_id)
    assert {row["id"] for row in frozen_film["optical_sets"]} == {handoff.optical_set_id}

    # The durable scheduler queue holds exactly one local job; nothing ran and the
    # project document set was not touched by canonicalization, freezing, or submit.
    assert handoff.transport.scheduler_store.status(job["job_id"])["state"] == "queued"
    assert handoff.store.manifest == manifest_before_submit
