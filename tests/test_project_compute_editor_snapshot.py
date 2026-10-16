from hashlib import sha256
import json
from types import SimpleNamespace
from typing import Any

from coat_sim.ui.project_compute_snapshot import capture_compute_snapshot


def _available(state: dict[str, Any] | None, reason: str | None) -> dict[str, Any]:
    assert reason is None
    assert state is not None
    return state


class MainDraft:
    _fast_threshold_text: str
    _fast_threshold_invalid: bool
    _result_view_only = False
    _revision = 4
    _busy = False
    _observers_closed = False
    _project_input_resources = None
    _project_compute: Any = None
    _compute_target: Any = None
    _pending_information_edit: dict[str, str] | None = None

    def __init__(self):
        self.draft: dict[str, Any] = {
            "pending_information": None,
            "configuration_name": "沉积方案",
            "configuration_description": "项目备注",
            "configuration_notes": "需要保留的用户备注",
            "fields": {"time_steps": "5", "carrier_rpm": "12"},
            "device": "CPU", "algorithm": "fast", "algorithm_budget_s": 0,
            "fast_cycle": True, "fast_cycle_residual_percent": 100.0,
            "requested_time_step_s": "0.025", "time_basis": "time_step_s",
            "secret": "do-not-project",
        }
        self.calls = 0

    def _project_draft_signature(self):
        self.calls += 1
        return dict(self.draft)


class RayController:
    _closed = False
    _busy = False
    _pending_overwrite = None
    _pending_film_import = None
    _pending_information_edit = None
    _pending_project_task = None
    _request_error = ""
    _request_text = '{invalid json}'
    _request: dict[str, Any] = {}
    _selected_surface = "s1"
    _selected_layer = "l1"
    _selected_step = "step-a"
    _spectrum_index = 0
    _workspace_mode = "geometric"
    _request_path = "/private/path/request.json"
    _managed_input_id = "input-1"
    _managed_input_revision = 3
    _managed_input_selected = True
    _managed_input_information = {"name": "Optical input", "description": "", "notes": "visible note"}

    def __init__(self, request: dict[str, Any]):
        self._request = request
        self._request_text = "same sized text"
        self._asset: dict[str, Any] = {"path": "/project/data/asset.coatasset", "sha256": "a" * 64,
                       "fields": [{"step_id": "step-a", "values": [[1, 2]]}],
                       "appearance_context": {"asset_sha256": "a" * 64}}
        self._compute_target = SimpleNamespace(_host_id="local", _device_id="cpu:default")
        self.parse_calls = 0

    def _parse_request_text(self):
        self.parse_calls += 1
        raise AssertionError("snapshot must not validate/parse a request")


def test_main_snapshot_reuses_whitelisted_pure_draft_and_keeps_notes():
    controller = MainDraft()
    selection = {"configuration_id": "config-1", "process_document_id": "process-1"}
    canonical = {**selection, "parameters": controller.draft}
    baseline = sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    controller._project_compute = SimpleNamespace(
        configuration_id="config-1", process_document_id="process-1",
        _draft_baseline_signature=baseline)
    state, reason = capture_compute_snapshot(controller, object(), "main")
    state = _available(state, reason)
    assert controller.calls == 1
    assert state["collections"] == {}
    assert state["summary"]["draft"]["configuration_notes"] == "需要保留的用户备注"
    assert "secret" not in state["summary"]["draft"]
    assert "pending_information" in state["summary"]["draft"]
    assert state["signature"]["scope"] == "complete"
    assert state["summary"]["configuration_id"] == "config-1"
    assert state["summary"]["process_document_id"] == "process-1"
    assert state["summary"]["dirty"] is False
    controller.draft["fields"]["carrier_rpm"] = "15"
    edited, reason = capture_compute_snapshot(controller, None, "main")
    edited = _available(edited, reason)
    assert edited["summary"]["dirty"] is True


def test_main_snapshot_withholds_pending_information_edit():
    controller = MainDraft()
    controller._pending_information_edit = {"notes": "pending"}
    state, reason = capture_compute_snapshot(controller, None, "main")
    assert state is None
    assert reason == "pending_confirmation"


def test_main_invalid_threshold_raw_controls_are_pure_and_bound_to_signature():
    controller = MainDraft()
    controller._fast_threshold_text = ""
    controller._fast_threshold_invalid = True
    first, reason = capture_compute_snapshot(controller, None, "main")
    first = _available(first, reason)
    controller._fast_threshold_text = "unfinished"
    second, reason = capture_compute_snapshot(controller, None, "main")
    second = _available(second, reason)
    assert first["signature"]["sha256"] != second["signature"]["sha256"]
    assert second["summary"]["raw_controls"] == {
        "fast_threshold_text": "unfinished", "fast_threshold_invalid": True}
    assert controller._revision == 4
    assert controller.draft["fast_cycle_residual_percent"] == 100.0


def test_main_signature_tracks_existing_host_and_device_ids_only():
    controller = MainDraft()
    controller._compute_target = SimpleNamespace(
        _host_id="local", _device_id="cpu:0", _backend="cpu",
        _endpoint="https://private.example", _token="secret")
    before, reason = capture_compute_snapshot(controller, None, "main")
    before = _available(before, reason)
    controller._compute_target._host_id = "remote-host"
    controller._compute_target._device_id = "cpu:1"
    after, reason = capture_compute_snapshot(controller, None, "main")
    after = _available(after, reason)
    assert before["summary"]["compute_target"] == {"host_id": "local", "device_id": "cpu:0"}
    assert after["summary"]["compute_target"] == {
        "host_id": "remote-host", "device_id": "cpu:1"}
    assert before["signature"]["sha256"] != after["signature"]["sha256"]
    assert "private.example" not in str(after["summary"])
    assert "secret" not in str(after["summary"])


def test_ray_trace_invalid_draft_uses_stored_state_without_parse_or_getter_side_effect():
    request = {
        "schema": "coat_sim.geometric_raytrace_request", "schema_version": 1,
        "units": {"length": "mm", "wavelength": "nm", "index": "n+i*k", "credential": "secret"},
        "polarization": "unpolarized", "detector": {"z_mm": 20, "aperture_radius_mm": 4, "pixel_count": 11},
        "samples": [{"sample_id": "r1", "origin_xz_mm": [0, 0],
                      "direction_xz": [0, 1], "weight": 1}],
        "spectrum": [{"wavelength_nm": 500, "weight": 1}],
        "surfaces": [{"surface_id": "s1", "geometry": {"type": "plane", "z_mm": 3},
                       "aperture_radius_mm": 2,
                       "medium_before": [{"n": 1, "k": 0}],
                       "medium_after": [{"n": 1.5, "k": 0}],
                       "coating": {"indices": [[{"n": 1, "k": 0}, {"n": 2, "k": .1},
                                                    {"n": 1.5, "k": 0}]],
                                    "layers": [{"layer_id": "l1", "thickness_nm": 80}]}}],
    }
    controller = RayController(request)
    state, reason = capture_compute_snapshot(controller, None, "ray_trace")
    state = _available(state, reason)
    assert controller.parse_calls == 0
    assert set(state["collections"]) == {"samples", "surfaces", "spectrum", "layers"}
    assert state["collections"]["samples"][0]["sample_id"] == "r1"
    assert state["collections"]["layers"][0]["thickness_nm"] == 80
    surface = state["collections"]["surfaces"][0]
    assert surface["medium_before_sample_count"] == 1
    assert surface["coating_index_row_count"] == 1
    assert "medium_before" not in surface and "indices" not in surface
    assert all("n" not in row and "k" not in row for row in state["collections"]["surfaces"])
    assert "/private/path" not in str(state["summary"])
    assert state["summary"]["request_present"] is True
    assert "credential" not in str(state["summary"]["request"]["units"])
    assert state["summary"]["request_text_length"] == len("same sized text")


def test_ray_trace_signature_hashes_raw_request_and_asset_without_projecting_optical_tables():
    req = {"spectrum": [{"wavelength_nm": 500, "weight": 1}], "surfaces": [
        {"surface_id": "s", "geometry": {"type": "plane", "z_mm": 1},
         "medium_before": [{"n": 1, "k": 0}], "medium_after": [{"n": 1, "k": 0}],
         "coating": {"indices": [[{"n": 1, "k": 0}, {"n": 1, "k": 0}]], "layers": []}}]}
    controller = RayController(req)
    before, reason = capture_compute_snapshot(controller, None, "ray_trace")
    before = _available(before, reason)
    controller._request["surfaces"][0]["coating"]["indices"][0][1]["n"] = 1.25
    after, reason = capture_compute_snapshot(controller, None, "ray_trace")
    after = _available(after, reason)
    assert before["signature"]["sha256"] != after["signature"]["sha256"]
    assert before["collections"]["surfaces"] == after["collections"]["surfaces"]


def test_film_appearance_projects_spectra_metadata_and_camera_gesture_draft():
    table = {"path": "/private/area.csv", "wavelength_nm": [400, 500, 600], "value": [1, 3, 1]}
    owner = SimpleNamespace(
        _closed=False, _draft={"camera_x": "0", "area_enabled": True,
                   "area_spectrum": table, "environment_spectrum": None,
                   "opaque": [1, 2, 3]},
        _context={"asset_path": "/project/a.coatasset", "asset_sha256": "b" * 64,
                  "context_hash": "ctx", "process_profile_sha256": "c" * 64,
                  "captured_step_ids": ["step-1", "step-2"],
                  "substrate_profile": {"outline": {"kind": "rectangle", "width_mm": 8, "height_mm": 6},
                                        "surface": {"kind": "flat"}}},
        _context_hash="ctx", _geometry_thickness_overridden=False,
        _backend="cpu", _precision="native", _camera_gesture_active=True,
        _preview_camera={"position_mm": [0, 0, -3], "target_mm": [0, 0, 0], "up": [0, 1, 0]},
        _process=None, _probe_process=None, _pending_project_finish=None,
        _project_input_resources=None, _compute_target=SimpleNamespace(_host_id="local", _device_id="cpu:default"),
        _phase="ready", _status="ready")
    state, reason = capture_compute_snapshot(owner, None, "film_appearance")
    assert state is None and reason == "native_geometry_draft"
    owner._camera_gesture_active = False
    state, reason = capture_compute_snapshot(owner, None, "film_appearance")
    state = _available(state, reason)
    assert set(state["collections"]) == {"captured_steps"}
    assert state["collections"]["captured_steps"] == [{"step_id": "step-1"}, {"step_id": "step-2"}]
    assert state["summary"]["context"]["context_hash"] == "ctx"
    assert state["summary"]["spectra"]["area_spectrum"]["sample_count"] == 3
    assert state["summary"]["spectra"]["area_spectrum"]["wavelength_domain_nm"] == [400, 600]
    assert "value" not in str(state["summary"]["spectra"])
    assert state["summary"]["camera_gesture_active"] is False
    assert "native_geometry_draft" not in state["summary"]
    changed = {**table, "value": [1, 2, 1]}
    owner._draft["area_spectrum"] = changed
    next_state, reason = capture_compute_snapshot(owner, None, "film_appearance")
    next_state = _available(next_state, reason)
    assert state["signature"]["sha256"] != next_state["signature"]["sha256"]


def test_film_appearance_busy_or_transfer_is_unavailable():
    owner = SimpleNamespace(_draft={}, _context={}, _process=object(), _probe_process=None,
                            _pending_project_finish=None, _closed=False)
    state, reason = capture_compute_snapshot(owner, None, "film_appearance")
    assert state is None and reason == "busy"
    owner._process = None
    owner._project_input_resources = SimpleNamespace(_transfer_busy=True, _resource_busy=False)
    state, reason = capture_compute_snapshot(owner, None, "film_appearance")
    assert state is None and reason == "resource_busy"


def test_queue_projects_only_owned_cached_rows_and_never_reads_store_or_payload():
    class ForbiddenStore:
        def __getattr__(self, name):
            raise AssertionError(f"snapshot must not read persistent queue store: {name}")

    class Queue:
        _closed = False
        _project_input_resources = None
        _active_bucket = "history"
        _pages_loaded = {"recent": 1, "history": 2}
        _has_more = {"recent": False, "history": True}
        _fetching = True
        _loading = True
        _cursors = {"recent": None, "history": "opaque cursor"}
        _jobs_by_bucket = {
            "recent": [{"job_id": "recent-1", "state": "running", "kind": "ray_trace",
                         "progress": {"fraction": 0.5, "stage": "trace", "credential": "hidden"},
                         "result": {"secret": "hidden"}}],
            "history": [
                {"job_id": "owned-1", "state": "succeeded", "kind": "film",
                 "summary": "我的任务", "stateLabel": "已完成",
                 "created_at": "2026-01-01T00:00:00Z",
                 "resultAvailable": True, "result": {"secret": "must not project"},
                 "payload": {"token": "hidden"}},
                {"job_id": "foreign-1", "state": "failed", "origin": {"project_id": "other"},
                 "payload": {"token": "hidden"}},
            ],
        }
        store = ForbiddenStore()

        @staticmethod
        def _owns_job(row):
            return row.get("job_id") != "foreign-1"

    queue = Queue()
    state, reason = capture_compute_snapshot(queue, None, "compute_queue")
    state = _available(state, reason)
    assert state["summary"]["scope"] == "cached_view"
    assert state["summary"]["loaded_total"] == 1
    assert state["summary"]["has_more_persisted"] is True
    assert state["summary"]["fetching"] is True
    jobs = state["collections"]["jobs"]
    assert {row["id"] for row in jobs} == {"owned-1"}
    assert jobs[0]["result_available"] is True
    assert jobs[0]["label"] == "我的任务"
    assert jobs[0]["state_label"] == "已完成"
    assert all("result" not in row and "payload" not in row for row in jobs)
    assert "credential" not in str(jobs)


def test_compute_queue_unloaded_tab_is_unavailable_even_with_other_tab_cache():
    queue = SimpleNamespace(
        _closed=False, _project_input_resources=None, _active_bucket="history",
        _pages_loaded={"recent": 1, "history": 0}, _has_more={"recent": False, "history": False},
        _fetching=True, _loading=True, _jobs_by_bucket={
            "recent": [{"job_id": "recent-cached", "state": "running", "kind": "ray"}],
            "history": [{"job_id": "old-cache", "state": "succeeded", "kind": "ray"}],
        })
    state, reason = capture_compute_snapshot(queue, None, "compute_queue")
    assert state is None
    assert reason == "initializing"


def test_compute_queue_recent_uses_visible_filter_and_page_cap():
    class Queue:
        _closed = False
        _project_input_resources = None
        _active_bucket = "recent"
        PAGE_SIZE = 2
        _pages_loaded = {"recent": 1, "history": 0}
        _has_more = {"recent": False, "history": False}
        _fetching = False
        _jobs_by_bucket = {"recent": [
            {"job_id": "live-1", "state": "running", "summary": "正在运行"},
            {"job_id": "live-2", "state": "queued", "summary": "排队中"},
            {"job_id": "live-3", "state": "running", "summary": "第三条"},
            {"job_id": "expired", "state": "succeeded", "finished_at": "2000-01-01T00:00:00Z"},
        ], "history": []}

        @staticmethod
        def _owns_job(row):
            return True

    state, reason = capture_compute_snapshot(Queue(), None, "compute_queue")
    state = _available(state, reason)
    jobs = state["collections"]["jobs"]
    assert [job["id"] for job in jobs] == ["live-1", "live-2"]
    assert [job["label"] for job in jobs] == ["正在运行", "排队中"]
    assert state["summary"]["loaded_total"] == 2


def test_appearance_outer_owner_mismatch_and_missing_controller_fail_closed():
    owner = SimpleNamespace(_draft={}, _context={"asset_sha256": "x"}, project_context=object(),
                            _project_compute=object(), _process=None, _probe_process=None,
                            _pending_project_finish=None, _closed=False)
    outer = SimpleNamespace(_appearance=owner, project_context=object(), _project_compute=object(),
                            _closed=False, _busy=False, _pending_overwrite=None,
                            _pending_film_import=None, _pending_information_edit=None,
                            _project_input_resources=None)
    state, reason = capture_compute_snapshot(outer, None, "film_appearance")
    assert state is None and reason == "editor_owner_mismatch"
    state, reason = capture_compute_snapshot(SimpleNamespace(_appearance=None, _closed=False,
        _busy=False, _pending_overwrite=None, _pending_film_import=None,
        _pending_information_edit=None, _project_input_resources=None), None, "film_appearance")
    assert state is None and reason == "appearance_unavailable"


def test_appearance_outer_busy_fence_runs_before_nested_controller_access():
    class Outer:
        _closed = False
        _busy = True
        _pending_overwrite = None
        _pending_film_import = None
        _pending_information_edit = None
        _project_input_resources = None
        project_context = None
        _project_compute = None

        @property
        def _appearance(self):
            raise AssertionError("must fence outer busy state before reading inner bridge")

    state, reason = capture_compute_snapshot(Outer(), None, "film_appearance")
    assert state is None and reason == "busy"


def test_unknown_signature_objects_fail_closed():
    controller = RayController({"samples": [], "surfaces": [], "spectrum": []})
    controller._asset["opaque"] = object()
    state, reason = capture_compute_snapshot(controller, None, "ray_trace")
    assert state is None and reason == "invalid_editor_state"


def test_ray_and_appearance_existing_close_confirmation_popup_withholds_draft():
    from PySide6.QtCore import QObject
    window = QObject()
    dialog = QObject(window)
    dialog.setObjectName("rayTraceCloseDialog")
    dialog.setProperty("visible", True)
    controller = RayController({"samples": [], "surfaces": [], "spectrum": []})
    for module in ("ray_trace", "film_appearance"):
        assert capture_compute_snapshot(controller, window, module) == (None, "pending_confirmation")
        assert dialog.property("visible") is True
    dialog.setProperty("visible", False)
    state, reason = capture_compute_snapshot(controller, window, "ray_trace")
    assert state is not None and reason is None


def test_ray_request_path_identity_distinguishes_same_named_identical_documents():
    controller = RayController({"samples": [], "surfaces": [], "spectrum": []})
    controller._request_path = "/private/one/request.json"
    first = _available(*capture_compute_snapshot(controller, None, "ray_trace"))
    controller._request_path = "/private/two/request.json"
    second = _available(*capture_compute_snapshot(controller, None, "ray_trace"))
    assert first["summary"]["request_path_name"] == second["summary"]["request_path_name"]
    assert first["signature"] != second["signature"]
    assert "/private/" not in str(first["summary"])
    assert "/private/" not in str(second["summary"])
    assert controller.parse_calls == 0


def test_queue_visible_cancellation_and_status_invalidate_cached_view_without_refresh():
    queue = SimpleNamespace(_closed=False, _active_bucket="history", _pages_loaded={"history": 1},
        _has_more={"history": False}, _fetching=False, _loading=False, _status="",
        _pending_cancels=set(), _confirmed_cancels=set(), _owns_job=lambda row: row["job_id"] == "owned",
        _jobs_by_bucket={"history": [{"job_id": "owned", "state": "running"},
                                      {"job_id": "foreign", "state": "running"}]})
    first = _available(*capture_compute_snapshot(queue, None, "compute_queue"))
    queue._pending_cancels.update({"owned", "foreign"})
    queue._status = "正在请求取消…"
    pending = _available(*capture_compute_snapshot(queue, None, "compute_queue"))
    assert first["signature"] != pending["signature"]
    assert pending["summary"]["loading"] is True
    assert pending["summary"]["status"] == "正在请求取消…"
    assert pending["collections"]["jobs"][0]["cancel_pending"] is True
    assert "foreign" not in str(pending)
    queue._pending_cancels.clear()
    queue._confirmed_cancels.add("owned")
    queue._status = "后台已确认取消请求。"
    confirmed = _available(*capture_compute_snapshot(queue, None, "compute_queue"))
    assert confirmed["signature"] != pending["signature"]
    assert confirmed["summary"]["loading"] is False
    assert confirmed["collections"]["jobs"][0]["cancel_pending"] is True
