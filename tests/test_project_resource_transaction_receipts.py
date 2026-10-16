"""Deterministic save receipts, verified deduplication and shared stage budgets."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest

from coat_sim.core.api import Document
from coat_sim.core.project_binary_assets import AssetReadLimits
from coat_sim.core.project_dependencies import ProjectError, json_bytes
from tests.test_project_simulation_resources import api, asset, ray

pytestmark = pytest.mark.contract


@pytest.mark.parametrize("pause_at", ["publish_return", "staging_exit"])
def test_save_receipt_keeps_its_revision_when_another_editor_commits_before_return(tmp_path, monkeypatch, pause_at):
    service = api(tmp_path / "project")
    initial = service.save_simulation_resource("raytrace_request", ray(), name="Initial")
    identity = Path(initial.path).stem
    editor = service.fork_editor()
    editor.load_document(identity)
    committed, resume = Event(), Event()
    original = service.store.stage_asset_publications
    publish = service.store._publish
    paused = False

    def pause_before_publish_returns(*args, **kwargs):
        nonlocal paused
        receipt = publish(*args, **kwargs)
        if not paused:
            paused = True
            committed.set()
            assert resume.wait(5)
        return receipt

    @contextmanager
    def pause_after_commit(*args, **kwargs):
        with original(*args, **kwargs) as prepared:
            yield prepared
        committed.set()
        assert resume.wait(5)

    with monkeypatch.context() as change, ThreadPoolExecutor(max_workers=1) as executor:
        if pause_at == "publish_return":
            change.setattr(service.store, "_publish", pause_before_publish_returns)
        else:
            change.setattr(service.store, "stage_asset_publications", pause_after_commit)
        pending = executor.submit(editor.save_simulation_resource, "raytrace_request", ray(),
                                  document_id=identity, name="Draft A")
        try:
            assert committed.wait(5)
            concurrent = service.load_document(identity)
            value = deepcopy(concurrent.data)
            value["name"] = "Concurrent B"
            service.save_resource(identity, value)
            concurrent_revision = service._record(identity)["sha256"]
        finally:
            resume.set()
        saved = pending.result(timeout=5)
    saved_revision = hashlib.sha256(json_bytes(saved.data)).hexdigest()
    assert saved.data["name"] == "Draft A"
    assert service.load_document(identity).data["name"] == "Concurrent B"
    assert saved.runtime is not None and saved.runtime["revision"] == saved_revision
    assert saved_revision != concurrent_revision
    assert editor._loaded_revisions[identity] == saved_revision
    with pytest.raises(ProjectError) as error:
        editor.save_simulation_resource("raytrace_request", ray(), document_id=identity,
                                        name="Stale A overwrite")
    assert error.value.code == "stale_document"
    assert service.load_document(identity).data["name"] == "Concurrent B"


def test_ui_save_uses_the_committed_receipt_without_reading_a_newer_row(tmp_path):
    from coat_sim.ui.project_raytrace_resources import _save_input
    identity, revision = "a" * 32, "b" * 64
    document = Document("raytrace_request", str(tmp_path / (identity + ".cray")), {},
                        runtime={"payload": ray(), "revision": revision})

    def forbid(*_args):
        raise AssertionError("A completed save must not read a newer document revision")

    service = SimpleNamespace(save_simulation_resource=lambda *_args, **_kwargs: document,
                              _record=forbid)
    result = _save_input(service, "raytrace_request",
        {"request_text": json.dumps(ray()["request"]), "deposition_asset": None},
        {"name": "Saved", "description": "", "notes": ""}, "", None)
    assert result == {"document": document, "identity": identity, "revision": revision}


def test_locked_save_receipt_only_projects_owned_paths_without_binary_io(tmp_path, monkeypatch):
    from coat_sim.core import project_binary_assets, project_simulation_resources, simulation_assets
    service = api(tmp_path / "project")
    source = asset(tmp_path / "external")
    original = project_simulation_resources.native_simulation_payload
    receipt_calls = []

    def forbid(*_args, **_kwargs):
        raise AssertionError("The final save receipt must not parse or hash binary input bytes")

    def project(store, kind, document, *, validate=True):
        assert validate is False and service._resource_lock._is_owned()
        receipt_calls.append(kind)
        with monkeypatch.context() as guard:
            for name in ("stream_asset", "validate_asset_content", "read_scalar_spectrum"):
                guard.setattr(project_binary_assets, name, forbid)
            guard.setattr(simulation_assets, "read_simulation_asset", forbid)
            return original(store, kind, document, validate=validate)

    monkeypatch.setattr(project_simulation_resources, "native_simulation_payload", project)
    saved = service.save_simulation_resource("raytrace_request", ray(source))
    assert receipt_calls == ["raytrace_request"]
    assert saved.runtime is not None
    assert Path(saved.runtime["payload"]["deposition_asset"]["path"]).is_relative_to(service.root)


def test_external_input_deduplication_rejects_same_size_corrupt_owned_bytes(tmp_path):
    source = asset(tmp_path / "external")
    service = api(tmp_path / "project")
    first = service.save_simulation_resource("raytrace_request", ray(source), name="First")
    assert first.runtime is not None
    owned = Path(first.runtime["payload"]["deposition_asset"]["path"])
    content = bytearray(owned.read_bytes())
    content[len(content) // 2] ^= 1
    owned.write_bytes(content)
    before = service.store.manifest
    with pytest.raises(ProjectError) as error:
        service.save_simulation_resource("raytrace_request", ray(source), name="Second")
    assert error.value.code == "integrity_error"
    assert service.store.manifest == before
    assert not list((service.root / "assets").glob(".project-asset-stage-*"))


def test_identical_spectrum_slots_reuse_the_verified_new_private_input(tmp_path):
    from coat_sim.core.project_simulation_resources import BOOL_DRAFT_FIELDS, NUMERIC_DRAFT_FIELDS, _sha
    source = asset(tmp_path / "external")
    spectrum = tmp_path / "spectrum.csv"
    spectrum.write_text("500,1\n600,2\n")
    draft: dict[str, Any] = {field: "1" for field in NUMERIC_DRAFT_FIELDS}
    draft.update({field: False for field in BOOL_DRAFT_FIELDS})
    sample = {"path": str(spectrum), "wavelength_nm": [500., 600.], "value": [1., 2.]}
    draft.update(area_kind="relative", environment_kind="relative",
                 area_spectrum=deepcopy(sample), environment_spectrum=deepcopy(sample))
    context = {"schema": "coat_sim.appearance_launch_context", "version": 1,
        "asset_path": str(source), "asset_root": str(tmp_path),
        "asset_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "process_profile": {}, "process_profile_sha256": _sha({}), "substrate_profile": {},
        "captured_step_ids": [], "layer_field_units": "nm", "layer_field_coordinate_frame": "substrate_local"}
    service = api(tmp_path / "project")
    saved = service.save_simulation_resource("appearance_configuration", {
        "schema": "coat_sim.appearance_configuration", "version": 1, "context": context,
        "draft": draft, "geometry_thickness_mode": "overridden", "backend": "cpu", "precision": "native"})
    assert len(service.store.manifest["documents"]) == 3
    assert saved.data["payload"]["draft"]["area_spectrum"]["path"] == saved.data["payload"]["draft"]["environment_spectrum"]["path"]
    assert service.load_document(Path(saved.path).stem).valid


@pytest.mark.parametrize("contender", ["apply", "preview"])
def test_staging_reserves_exchange_budget_against_concurrent_apply_and_preview(tmp_path, monkeypatch, contender):
    from coat_sim.core import project_binary_assets
    source, target = api(tmp_path / "source"), api(tmp_path / "target")
    spectrum = tmp_path / "spectrum.csv"
    spectrum.write_text("500,1\n600,2\n")
    captured = source.capture_simulation_asset("spd_csv", spectrum)
    size = captured["size"]
    target.store.asset_limits = AssetReadLimits(max_total_bytes=3 * size)
    catalog = target.inspect_resources(source.store.project_file)
    plans = [target.preview_resource_transfer(catalog["catalog_id"], [captured["document_id"]])
             for _ in range(2)]
    exchange = target.resource_exchange
    staged, resume = Event(), Event()
    original = project_binary_assets.capture_asset

    def pause_after_stage(source, metadata, destination):
        result = original(source, metadata, destination)
        if ".project-asset-stage-" in str(destination):
            staged.set()
            assert resume.wait(5)
        return result

    monkeypatch.setattr(project_binary_assets, "capture_asset", pause_after_stage)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(target.apply_resource_transfer, plans[0]["plan_id"])
        try:
            assert staged.wait(5)
            assert exchange._spool_bytes == 2 * size and exchange._staging_bytes == size
            with pytest.raises(ProjectError) as error:
                if contender == "apply":
                    target.apply_resource_transfer(plans[1]["plan_id"])
                else:
                    target.preview_resource_transfer(catalog["catalog_id"], [captured["document_id"]])
            assert error.value.code == "capacity_exceeded"
            assert len(list((target.root / "assets").glob(".project-asset-stage-*"))) == 1
        finally:
            resume.set()
        assert pending.result(timeout=5)["created"] == 1
    assert exchange._staging_bytes == 0 and exchange._spool_bytes == size
    target.cancel_resource_transfer(plans[1]["plan_id"])
    assert exchange._spool_bytes == 0


def test_staging_reservation_is_released_when_publication_fails(tmp_path, monkeypatch):
    source, target = api(tmp_path / "source"), api(tmp_path / "target")
    spectrum = tmp_path / "spectrum.csv"
    spectrum.write_text("500,1\n600,2\n")
    captured = source.capture_simulation_asset("spd_csv", spectrum)
    target.store.asset_limits = AssetReadLimits(max_total_bytes=2 * captured["size"])
    catalog = target.inspect_resources(source.store.project_file)
    plan = target.preview_resource_transfer(catalog["catalog_id"], [captured["document_id"]])
    exchange = target.resource_exchange

    def fail(*_args, **_kwargs):
        assert exchange._staging_bytes == captured["size"]
        raise ProjectError("io_error", "Injected publication failure")

    monkeypatch.setattr(target.store, "_publish", fail)
    with pytest.raises(ProjectError, match="Injected"):
        target.apply_resource_transfer(plan["plan_id"])
    assert exchange._staging_bytes == 0 and exchange._spool_bytes == captured["size"]
    assert not list((target.root / "assets").glob(".project-asset-stage-*"))
    assert target.store.manifest["documents"] == []
    target.cancel_resource_transfer(plan["plan_id"])
    assert exchange._spool_bytes == 0
