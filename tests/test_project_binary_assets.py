"""Streaming role, identity, digest and path constraints for binary inputs."""
from dataclasses import replace
import hashlib
import io
import zipfile

import pytest

from coat_sim.core.project_binary_assets import (
    AssetReadLimits, capture_asset, metadata_for_file, normalize_binary_asset,
    read_scalar_spectrum, stream_asset,
)
from coat_sim.core.project_dependencies import ProjectError

pytestmark = pytest.mark.contract


def _metadata(data=b"wavelength_nm,value\n500,1\n600,2\n"):
    return {"kind": "coat_sim.project_binary_asset", "version": 1, "role": "spd_csv",
        "payload_path": "assets/" + "a" * 32 + "/" + "b" * 32 + ".csv",
        "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def test_copy_hashes_exact_bytes_and_csv_samples(tmp_path):
    raw = b"wavelength_nm,value\n500,1\n600,2\n"
    source = tmp_path / "spectrum.csv"
    source.write_bytes(raw)
    metadata = metadata_for_file(source, "spd_csv", _metadata()["payload_path"])
    copy = tmp_path / "captured.csv"
    assert capture_asset({"path": str(source)}, metadata, copy) == {"path": str(copy)}
    assert copy.read_bytes() == raw
    assert read_scalar_spectrum(copy) == {"wavelength_nm": [500., 600.], "value": [1., 2.]}
    source.write_bytes(b"changed")
    assert copy.read_bytes() == raw


@pytest.mark.parametrize("path", ["../escape.csv", "/absolute.csv", "assets/a/b.csv",
    "assets/" + "a" * 32 + "/../b.csv", "assets/" + "a" * 32 + "/" + "b" * 32 + ".coatasset",
    "assets\\" + "a" * 32 + "\\" + "b" * 32 + ".csv"])
def test_binary_paths_are_strictly_typed_owned_paths(path):
    with pytest.raises(ProjectError):
        normalize_binary_asset({**_metadata(), "payload_path": path})


@pytest.mark.parametrize("change", [{"role": "arbitrary_file"}, {"size": True}, {"size": 2_000_001},
    {"sha256": "A" * 64}, {"version": True}, {"unexpected": 1}])
def test_invalid_role_size_digest_and_fields_are_rejected(change):
    with pytest.raises(ProjectError):
        normalize_binary_asset({**_metadata(), **change})


def test_digest_failure_and_invalid_csv_leave_no_captured_file(tmp_path):
    source = tmp_path / "source.csv"
    source.write_bytes(b"different")
    destination = tmp_path / "copy.csv"
    with pytest.raises(ProjectError):
        capture_asset({"path": str(source)}, _metadata(), destination)
    assert not destination.exists()
    source.write_bytes(b"500,1\n400,2\n")
    metadata = metadata_for_file(source, "spd_csv", _metadata()["payload_path"])
    with pytest.raises(ProjectError):
        capture_asset({"path": str(source)}, metadata, destination)
    assert not destination.exists()


def test_linked_source_and_destination_are_never_followed(tmp_path):
    source = tmp_path / "source.csv"
    source.write_bytes(b"500,1\n600,2\n")
    linked = tmp_path / "link.csv"
    linked.symlink_to(source)
    with pytest.raises(ProjectError) as error:
        metadata_for_file(linked, "spd_csv", _metadata()["payload_path"])
    assert error.value.code == "unsafe_path"
    output = tmp_path / "output.csv"
    output.symlink_to(source)
    metadata = metadata_for_file(source, "spd_csv", _metadata()["payload_path"])
    with pytest.raises(ProjectError):
        capture_asset({"path": str(source)}, metadata, output)
    assert source.read_bytes() == b"500,1\n600,2\n"


def test_exact_archive_member_streams_with_size_and_digest_guard(tmp_path):
    raw = b"wavelength_nm,value\n500,1\n600,2\n"
    path = tmp_path / "source.cproject"
    member = _metadata()["payload_path"]
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(member, raw)
    output = io.BytesIO()
    stream_asset({"package": str(path), "member": member}, _metadata(), output)
    assert output.getvalue() == raw
    with pytest.raises(ProjectError):
        stream_asset({"package": str(path), "member": member}, {**_metadata(), "size": 1})


def test_budget_is_explicit_read_limit_and_diagnoses_actual_bytes():
    budget = AssetReadLimits(max_total_bytes=10)
    budget.check_total(10)
    with pytest.raises(ProjectError, match="11.*10"):
        budget.check_total(11)
    replace(budget, max_total_bytes=2_000_000_000).check_total(1_500_000_000)
