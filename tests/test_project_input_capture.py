"""Explicit input imports precede and survive unsaved simulation drafts."""
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from coat_sim.core.project_binary_assets import AssetReadLimits
from coat_sim.core.project_dependencies import ProjectError
from tests.test_project_simulation_resources import api, asset, ray

pytestmark = pytest.mark.contract


def test_file_acceptance_imports_input_without_config_and_first_save_survives_deleted_original(tmp_path):
    source = asset(tmp_path / "external")
    service = api(tmp_path / "project")
    receipt = service.capture_simulation_asset("deposition_asset", source)
    assert receipt["reused"] is False
    assert service.store.manifest["default_configuration_id"] is None
    assert [row["kind"] for row in service.store.manifest["documents"]] == ["binary_asset"]
    assert Path(receipt["path"]).is_relative_to(service.root / "assets")
    source.unlink()
    # Abandoning a draft does not remove this explicitly imported resource.
    document = service.save_simulation_resource("raytrace_request", ray(receipt["path"]), name="First save")
    assert len(service.store.manifest["documents"]) == 2
    assert Path(document.runtime["payload"]["deposition_asset"]["path"]).exists()
    assert service.store.manifest["default_configuration_id"] is None


def test_identical_content_and_role_reuses_identity_without_new_revision_or_metadata_overwrite(tmp_path):
    source = asset(tmp_path / "external")
    service = api(tmp_path / "project")
    first = service.capture_simulation_asset("deposition_asset", source)
    before = service.store.manifest
    renamed = tmp_path / "Different filename.coatasset"
    renamed.write_bytes(source.read_bytes())
    second = service.capture_simulation_asset("deposition_asset", renamed, expected_sha256=first["sha256"])
    assert second == {**first, "reused": True}
    assert service.store.manifest == before
    csv = tmp_path / "spectrum.csv"
    csv.write_text("500,1\n600,2\n")
    spectrum = service.capture_simulation_asset("spd_csv", csv)
    csv.unlink()
    assert Path(spectrum["path"]).read_text() == "500,1\n600,2\n"
    assert spectrum["document_id"] != first["document_id"]


@pytest.mark.parametrize("failure", ["cancelled", "bad_digest", "bad_role", "bad_content", "publish"])
def test_cancel_invalid_input_and_publish_failure_leave_no_project_change_or_stage(tmp_path, monkeypatch, failure):
    from coat_sim.core import project_binary_assets, project_store
    source = asset(tmp_path / "external")
    service = api(tmp_path / "project")
    before = service.store.manifest
    event = Event()
    kwargs: dict[str, Any] = {"cancel_event": event}
    role = "deposition_asset"
    if failure == "cancelled":
        original = project_binary_assets.capture_asset
        def cancel_after_capture(*args, **kwargs):
            result = original(*args, **kwargs)
            event.set()
            return result
        monkeypatch.setattr(project_binary_assets, "capture_asset", cancel_after_capture)
    elif failure == "bad_digest":
        kwargs["expected_sha256"] = "0" * 64
    elif failure == "bad_role":
        role = "arbitrary_attachment"
    elif failure == "bad_content":
        source.write_bytes(b"not an asset archive")
    else:
        def fail(*_args):
            raise OSError("injected descriptor replacement failure")
        monkeypatch.setattr(project_store, "replace_with_retry", fail)
    with pytest.raises(ProjectError):
        service.capture_simulation_asset(role, source, **kwargs)
    assert service.store.manifest == before
    assert not list((service.root / "assets").rglob("*.coatasset"))
    assert not list((service.root / "assets").glob(".project-asset-stage-*"))


def test_exchange_owner_budget_is_cumulative_and_released_on_cancel(tmp_path):
    source = api(tmp_path / "source")
    document = source.save_simulation_resource("raytrace_request", ray(asset(tmp_path / "external")))
    target = api(tmp_path / "target")
    size = Path(document.runtime["payload"]["deposition_asset"]["path"]).stat().st_size
    target.store.asset_limits = AssetReadLimits(max_total_bytes=size)
    catalog = target.inspect_resources(source.store.project_file)
    first = target.preview_resource_transfer(catalog["catalog_id"], [Path(document.path).stem])
    assert target.resource_exchange._spool_bytes == size
    with pytest.raises(ProjectError) as error:
        target.preview_resource_transfer(catalog["catalog_id"], [Path(document.path).stem])
    assert error.value.code == "capacity_exceeded"
    assert target.resource_exchange._spool_bytes == size
    target.cancel_resource_transfer(first["plan_id"])
    assert target.resource_exchange._spool_bytes == 0
    second = target.preview_resource_transfer(catalog["catalog_id"], [Path(document.path).stem])
    target.cancel_resource_transfer(second["plan_id"])
    target.cancel_resource_transfer(second["plan_id"])
    assert target.resource_exchange._spool_bytes == 0


def test_concurrent_previews_reserve_owner_budget_before_copying(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from coat_sim.core import project_binary_assets
    source, target = api(tmp_path / "source"), api(tmp_path / "target")
    document = source.save_simulation_resource("raytrace_request", ray(asset(tmp_path / "external")))
    size = Path(document.runtime["payload"]["deposition_asset"]["path"]).stat().st_size
    target.store.asset_limits = AssetReadLimits(max_total_bytes=size)
    catalog = target.inspect_resources(source.store.project_file)
    entered, resume = Event(), Event()
    original = project_binary_assets.capture_asset
    def delayed(*args, **kwargs):
        entered.set()
        assert resume.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(project_binary_assets, "capture_asset", delayed)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(target.preview_resource_transfer, catalog["catalog_id"], [Path(document.path).stem])
        try:
            assert entered.wait(5)
            with pytest.raises(ProjectError) as error:
                target.preview_resource_transfer(catalog["catalog_id"], [Path(document.path).stem])
            assert error.value.code == "capacity_exceeded"
        finally:
            resume.set()
        plan = pending.result(timeout=5)
    target.release_resource_catalog(catalog["catalog_id"])
    target.cancel_resource_transfer(plan["plan_id"])
    assert target.resource_exchange._spool_bytes == 0
