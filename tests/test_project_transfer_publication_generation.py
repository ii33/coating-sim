"""Transfer CAS retains the generation even when membership is reconstructed."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, current_thread
from typing import Any

import pytest

from coat_sim.core.project_resource_service import ProjectResourceService
from coat_sim.core.project_store import ProjectError, ProjectStore

pytestmark = pytest.mark.contract


def source(name: str) -> dict[str, Any]:
    return {"schema_version": 2, "kind": "point", "device_type": "custom",
            "source_exponent": 1., "name": name}


@pytest.mark.parametrize("explicit_target", [False, True])
def test_transfer_rejects_generation_changed_after_service_admission(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_target: bool) -> None:
    origin = ProjectStore.create(tmp_path / "origin", "source")
    selected = origin.create_document("source", source("transfer A"))
    target = ProjectStore.create(tmp_path / "target", "target")
    service = ProjectResourceService(target)
    concurrent = service.fork_editor()
    catalog = service.inspect_resources(origin.project_file)
    plan = service.preview_resource_transfer(catalog["catalog_id"], [selected],
                target=str(target.project_file) if explicit_target else "project")
    entered, resume = Event(), Event()
    publish = target._publish

    def delayed(*args: Any, **kwargs: Any) -> dict[str, str]:
        if current_thread().name.startswith("transfer"):
            entered.set()
            assert resume.wait(5)
        return publish(*args, **kwargs)

    monkeypatch.setattr(target, "_publish", delayed)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="transfer") as pool:
        transfer = pool.submit(service.apply_resource_transfer, plan["plan_id"])
        try:
            assert entered.wait(5)
            other = concurrent.create_resource("source", source("concurrent B"))
        finally:
            resume.set()
        with pytest.raises(ProjectError) as error:
            transfer.result(timeout=5)
        assert error.value.code == "stale_project"
    snapshot = ProjectStore.open(target.project_file).snapshot_saved_inputs()
    assert set(snapshot["documents"]) == {other}
    assert snapshot["documents"][other]["name"] == "concurrent B"
    assert snapshot["manifest"]["revision"] == 1
